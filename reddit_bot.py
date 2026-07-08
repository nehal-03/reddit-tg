import os
import re
import json
import time
import html
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
def download_image(url, filename):
    try:
        response = requests.get(url, headers=REDDIT_HEADERS, stream=True, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            with open(filename, "wb") as f:
                for chunk in response.iter_content(1024 * 64):
                    f.write(chunk)
            return True
        log.warning("Image download got status %s for %s", response.status_code, url)
    except requests.RequestException as e:
        log.warning("Image download failed for %s: %s", url, e)
    return False


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
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    except Exception as e:
        log.info("Video download skipped/failed for %s: %s", url, e)
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
        response = requests.get(rss_url, headers=REDDIT_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
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
        time.sleep(3)  # be gentle with Telegram rate limits


if __name__ == "__main__":
    main()
