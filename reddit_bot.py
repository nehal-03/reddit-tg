import os
import re
import json
import time
import html
import random
import logging

import feedparser
import requests
import telebot
from telebot import apihelper
from telebot.types import InputMediaPhoto
import yt_dlp

# --- CONFIGURATION (all from environment variables — never hardcode secrets) ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]          # required
GROUP_CHAT_ID = os.environ["GROUP_CHAT_ID"]            # required, e.g. -1001234567890


# Comma-separated list, e.g. "aww,earthporn,mildlyinteresting"
SUBREDDITS = [s.strip() for s in os.environ.get("SUBREDDITS", "aww").split(",") if s.strip()]

# Send images/videos with Telegram's spoiler blur applied
SPOILER_MEDIA = os.environ.get("SPOILER_MEDIA", "true").lower() == "true"

MAX_FILE_SIZE = 48 * 1024 * 1024  # 48 MB Telegram upload ceiling
STATE_FILE = "posted.json"        # tracks last posted id per subreddit to avoid duplicates
REQUEST_TIMEOUT = 20

# --- Rate-limit handling knobs (env-overridable) ---
MIN_SUB_DELAY = float(os.environ.get("MIN_SUB_DELAY", "40"))    # base delay between subreddits (s)
MAX_SUB_DELAY = float(os.environ.get("MAX_SUB_DELAY", "100"))   # jittered upper bound (s)
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))          # per-request retry attempts
BASE_BACKOFF = float(os.environ.get("BASE_BACKOFF", "5"))      # base seconds for exponential backoff

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit_bot")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Reuse a single session so connections are pooled instead of reopened each call
session = requests.Session()
session.headers.update(REDDIT_HEADERS)


# --- STATE (dedup so the same "top post of the day" isn't reposted every run) ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting fresh.", STATE_FILE)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- HELPERS ---
def request_with_retry(url, *, stream=False, max_retries=MAX_RETRIES):
    """
    GET a URL with exponential backoff + jitter on 429/5xx.
    Honors the Retry-After header when Reddit sends one.
    Returns the Response on success, or None if all retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, stream=stream, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log.warning("Request error for %s (attempt %d/%d): %s", url, attempt, max_retries, e)
            resp = None

        if resp is not None and resp.status_code == 200:
            return resp

        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = BASE_BACKOFF * (2 ** (attempt - 1))
            else:
                wait = BASE_BACKOFF * (2 ** (attempt - 1))
            wait += random.uniform(0, 2)  # jitter to avoid thundering herd
            log.warning(
                "429 from %s (attempt %d/%d), backing off %.1fs",
                url, attempt, max_retries, wait,
            )
            time.sleep(wait)
            continue

        if resp is not None and 500 <= resp.status_code < 600:
            wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning(
                "%s from %s (attempt %d/%d), retrying in %.1fs",
                resp.status_code, url, attempt, max_retries, wait,
            )
            time.sleep(wait)
            continue

        if resp is not None:
            log.warning("Non-retryable status %s for %s", resp.status_code, url)
            return None

        # request exception path: back off before retrying too
        wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2)
        time.sleep(wait)

    log.error("Giving up on %s after %d attempts.", url, max_retries)
    return None


def download_image(url, filename):
    resp = request_with_retry(url, stream=True)
    if resp is None:
        return False
    try:
        with open(filename, "wb") as f:
            for chunk in resp.iter_content(1024 * 64):
                f.write(chunk)
        return True
    except OSError as e:
        log.warning("Failed writing image %s: %s", filename, e)
        return False
    finally:
        resp.close()


def download_video(url, base_filename):
    """Downloads a Reddit video/gif and returns the exact filename created, or None."""
    outtmpl_path = f"{base_filename}.%(ext)s"
    ydl_opts = {
        "outtmpl": outtmpl_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "too many requests" in msg:
                wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2)
                log.warning(
                    "yt-dlp 429 for %s (attempt %d/%d), backing off %.1fs",
                    url, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            # Non-rate-limit failure: no point retrying
            break
    log.info("Video download skipped/failed for %s: %s", url, last_err)
    return None


def cleanup_files(file_list):
    for file in file_list:
        try:
            if file and os.path.exists(file):
                os.remove(file)
        except OSError as e:
            log.warning("Could not delete temp file %s: %s", file, e)


def build_caption(title, sub, post_url):
    # HTML parse mode is far more forgiving than Markdown for arbitrary Reddit titles
    safe_title = html.escape(title)
    return f'<b>{safe_title}</b>\n\nFrom r/{sub}\n<a href="{html.escape(post_url)}">View on Reddit</a>'


def extract_image_urls(content_html):
    urls = re.findall(r'href="(https://[^"]+\.(?:jpg|jpeg|png|gif))"', content_html)
    return list(dict.fromkeys(urls))  # de-dupe, preserve order


# --- MAIN LOGIC ---
def process_subreddit(sub, state):
    log.info("Scanning r/%s via RSS...", sub)
    files_to_cleanup = []
    opened_handles = []

    try:
        rss_url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day"
        response = request_with_retry(rss_url)
        if response is None:
            log.error("Could not fetch RSS for r/%s after retries, skipping this run.", sub)
            return
        feed = feedparser.parse(response.content)

        if not feed.entries:
            log.info("No posts found for r/%s.", sub)
            return

        post = feed.entries[0]

        if state.get(sub) == post.id:
            log.info("Top post for r/%s unchanged since last run, skipping.", sub)
            return

        title = post.title
        post_url = post.link
        content_html = post.get("summary", "")
        caption = build_caption(title, sub, post_url)

        image_urls = extract_image_urls(content_html)

        if image_urls:
            media_group = []
            for i, url in enumerate(image_urls[:5]):  # Telegram album cap
                if i > 0:
                    time.sleep(random.uniform(1.5, 3.5))  # avoid bursting the image CDN
                filename = f"temp_img_{sub}_{i}.jpg"
                if download_image(url, filename):
                    files_to_cleanup.append(filename)
                    fh = open(filename, "rb")
                    opened_handles.append(fh)
                    if i == 0:
                        media_group.append(
                            InputMediaPhoto(
                                fh, caption=caption, parse_mode="HTML", has_spoiler=SPOILER_MEDIA
                            )
                        )
                    else:
                        media_group.append(InputMediaPhoto(fh, has_spoiler=SPOILER_MEDIA))

            if len(media_group) == 1:
                bot.send_photo(
                    GROUP_CHAT_ID,
                    opened_handles[0],
                    caption=caption,
                    parse_mode="HTML",
                    has_spoiler=SPOILER_MEDIA,
                )
            elif len(media_group) > 1:
                bot.send_media_group(GROUP_CHAT_ID, media=media_group)
            else:
                bot.send_message(GROUP_CHAT_ID, caption, parse_mode="HTML")

            log.info("Sent %d image(s) for r/%s.", len(media_group), sub)

        else:
            base_filename = f"temp_video_{sub}_{post.id}"
            actual_video_file = download_video(post_url, base_filename)

            if actual_video_file and os.path.exists(actual_video_file):
                files_to_cleanup.append(actual_video_file)
                if os.path.getsize(actual_video_file) < MAX_FILE_SIZE:
                    with open(actual_video_file, "rb") as video:
                        bot.send_video(
                            GROUP_CHAT_ID,
                            video,
                            caption=caption,
                            parse_mode="HTML",
                            has_spoiler=SPOILER_MEDIA,
                        )
                    log.info("Sent video for r/%s.", sub)
                else:
                    log.info("Video too large for r/%s, sending link instead.", sub)
                    bot.send_message(
                        GROUP_CHAT_ID,
                        f"{caption}\n\n<i>Video too large to upload locally.</i>",
                        parse_mode="HTML",
                    )
            else:
                bot.send_message(GROUP_CHAT_ID, caption, parse_mode="HTML")
                log.info("Sent text fallback for r/%s.", sub)

        state[sub] = post.id

    except Exception as e:
        log.error("Error processing r/%s: %s", sub, e)

    finally:
        for fh in opened_handles:
            try:
                fh.close()
            except OSError:
                pass
        cleanup_files(files_to_cleanup)


def main():
    state = load_state()
    for sub in SUBREDDITS:
        process_subreddit(sub, state)
        save_state(state)  # save incrementally so one bad subreddit doesn't lose earlier progress
        delay = 50 + random.uniform(0, 50)  # jitter to avoid looking like a bot
        log.info("Sleeping %.1fs before next subreddit...", delay)
        time.sleep(delay)


if __name__ == "__main__":
    main()