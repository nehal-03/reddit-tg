import os
import re
import json
import time
import html
import random
import shutil
import base64
import logging
import tempfile
import subprocess
from urllib.parse import urlparse

import feedparser
import requests
import telebot
from telebot import apihelper
from telebot.types import InputMediaPhoto
import yt_dlp  # fallback only, for non-Reddit-hosted video links (redgifs, gfycat, streamable, etc.)

# --- CONFIGURATION (all from environment variables — never hardcode secrets) ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]          # required
GROUP_CHAT_ID = os.environ["GROUP_CHAT_ID"]            # required, e.g. -1001234567890

# No Reddit API app/keys needed. We read Reddit's public, unauthenticated JSON endpoints
# (the same ones your own browser uses), with old.reddit.com as a second host to try if
# www.reddit.com is rate-limited/blocked, and RSS as a last-resort fallback below that.
JSON_HOSTS = ["https://www.reddit.com", "https://old.reddit.com"]

# Comma-separated list, e.g. "aww,earthporn,mildlyinteresting"
SUBREDDITS = [s.strip() for s in os.environ.get("SUBREDDITS", "aww").split(",") if s.strip()]

# Send images/videos with Telegram's spoiler blur applied
SPOILER_MEDIA = os.environ.get("SPOILER_MEDIA", "true").lower() == "true"

# How many of today's top posts to scan looking for one with real media we haven't posted yet.
POSTS_TO_CHECK = int(os.environ.get("POSTS_TO_CHECK", "10"))
# How many posted ids to remember per subreddit (rolling window, prevents unbounded growth
# and reposts if a post falls out of the "top of day" listing).
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "50"))

MAX_FILE_SIZE = 48 * 1024 * 1024  # 48 MB Telegram upload ceiling
STATE_FILE = "posted.json"        # tracks recently-posted ids per subreddit to avoid duplicates
REQUEST_TIMEOUT = 20
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "180"))

# --- Rate-limit handling knobs (env-overridable) ---
MIN_SUB_DELAY = float(os.environ.get("MIN_SUB_DELAY", "40"))    # base delay between subreddits (s)
MAX_SUB_DELAY = float(os.environ.get("MAX_SUB_DELAY", "100"))   # jittered upper bound (s)
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))           # per-request retry attempts
BASE_BACKOFF = float(os.environ.get("BASE_BACKOFF", "5"))       # base seconds for exponential backoff

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit_bot")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# A real-browser UA tends to fare better on the public (non-API) endpoints than an
# API-style "script:app:version" UA, which is what Reddit's *official* API wants instead.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

session = requests.Session()
session.headers.update({
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
})

if shutil.which("ffmpeg") is None:
    log.warning(
        "ffmpeg was not found on PATH. Reddit-hosted videos (v.redd.it) cannot be downloaded "
        "without it. GitHub Actions 'ubuntu-latest' runners normally ship with ffmpeg "
        "preinstalled; if yours doesn't, add a step: "
        "`sudo apt-get update && sudo apt-get install -y ffmpeg`."
    )

# Optional: base64-encoded Netscape-format cookies.txt, used ONLY for the yt-dlp fallback
# path (external hosts like redgifs/gfycat), never for reddit.com itself.
YTDLP_COOKIES_FILE = None
_ytdlp_cookies_b64 = os.environ.get("YTDLP_COOKIES_B64", "")
if _ytdlp_cookies_b64:
    try:
        YTDLP_COOKIES_FILE = os.path.join(tempfile.gettempdir(), "ytdlp_cookies.txt")
        with open(YTDLP_COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(_ytdlp_cookies_b64))
        log.info("Loaded yt-dlp cookies from YTDLP_COOKIES_B64 for external-host fallback downloads.")
    except Exception as e:
        log.warning("Could not decode YTDLP_COOKIES_B64, ignoring: %s", e)
        YTDLP_COOKIES_FILE = None


# --- STATE (dedup so the same posts aren't reposted every run) ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                for k, v in list(data.items()):
                    if isinstance(v, str):  # migrate from an older single-id format
                        data[k] = [v]
                return data
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting fresh.", STATE_FILE)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- GENERIC HTTP GET WITH RETRY (used for both JSON and RSS fetches) ---
def request_with_retry(url, *, stream=False, max_retries=MAX_RETRIES):
    """GET a URL with exponential backoff + jitter on 429/5xx. Honors Retry-After.
    Returns the Response on success, or None if retries are exhausted / a hard block occurs.
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
            try:
                wait = float(retry_after) if retry_after else BASE_BACKOFF * (2 ** (attempt - 1))
            except ValueError:
                wait = BASE_BACKOFF * (2 ** (attempt - 1))
            wait += random.uniform(0, 2)
            log.warning("429 from %s (attempt %d/%d), backing off %.1fs", url, attempt, max_retries, wait)
            time.sleep(wait)
            continue

        if resp is not None and resp.status_code in (403, 451):
            # Reddit sometimes hard-blocks datacenter IPs (e.g. CI runners) on public
            # endpoints. Retrying the same host won't help — caller should try another host.
            log.warning("%s from %s — this host may be blocking the request's IP/UA.", resp.status_code, url)
            return None

        if resp is not None and 500 <= resp.status_code < 600:
            wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning("%s from %s (attempt %d/%d), retrying in %.1fs",
                        resp.status_code, url, attempt, max_retries, wait)
            time.sleep(wait)
            continue

        if resp is not None:
            log.warning("Non-retryable status %s for %s", resp.status_code, url)
            return None

        time.sleep(BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2))

    log.error("Giving up on %s after %d attempts.", url, max_retries)
    return None


# --- FETCHING POSTS: layered fallback, no API key required ---
def fetch_listing_json(sub, limit):
    """Tier 1: the subreddit's public top/.json listing. Richest + cheapest (one call
    gets full data for many posts, including gallery/video info). Returns a list of post
    dicts on success, or None if every host was blocked/failed (distinct from '[]' = no posts).
    """
    for host in JSON_HOSTS:
        url = f"{host}/r/{sub}/top/.json?t=day&limit={limit}&raw_json=1"
        resp = request_with_retry(url)
        if resp is None:
            continue
        try:
            data = resp.json()
        except ValueError:
            log.warning("Non-JSON response from %s for r/%s listing.", host, sub)
            continue
        children = data.get("data", {}).get("children", [])
        return [c["data"] for c in children if "data" in c]
    return None


def fetch_post_json(permalink):
    """Tier 2 helper: fetch ONE post's full JSON directly. Sometimes still works even when
    the subreddit listing endpoint above is blocked, since it's a different URL pattern.
    """
    for host in JSON_HOSTS:
        url = f"{host}{permalink}.json?raw_json=1"
        resp = request_with_retry(url, max_retries=2)
        if resp is None:
            continue
        try:
            data = resp.json()
            children = data[0]["data"]["children"]
            if children:
                return children[0]["data"]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return None


def fetch_rss_listing(sub, limit):
    """Tier 3: RSS, used only to enumerate today's top post links when JSON listing is
    blocked entirely. For each entry we try Tier 2 (per-post JSON) to recover full media
    info; if that also fails we fall back to a crude image-only regex over the RSS body
    (no video support at that point — we skip videos rather than posting a bare link).
    """
    url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day"
    resp = request_with_retry(url)
    if resp is None:
        return []

    feed = feedparser.parse(resp.content)
    posts = []
    for entry in feed.entries[:limit]:
        link = entry.get("link", "")
        path = urlparse(link).path  # e.g. /r/sub/comments/abc123/title/
        match = re.search(r"/comments/([a-z0-9]+)/", path)
        pid = match.group(1) if match else path

        full_post = fetch_post_json(path) if path else None
        if full_post:
            posts.append(full_post)
            continue

        # Last-ditch: pull whatever inline images the RSS body links to.
        content_html = entry.get("summary", "")
        image_urls = re.findall(r'href="(https://[^"]+\.(?:jpg|jpeg|png|gif))"', content_html)
        image_urls = list(dict.fromkeys(image_urls))
        posts.append({
            "id": pid,
            "title": entry.get("title", ""),
            "permalink": path,
            "_rss_fallback_images": image_urls,
        })
    return posts


def get_top_posts(sub, limit):
    posts = fetch_listing_json(sub, limit)
    if posts is not None:
        return posts
    log.warning("JSON listing blocked/unavailable for r/%s, falling back to RSS.", sub)
    return fetch_rss_listing(sub, limit)


# --- MEDIA EXTRACTION ---
def _unescape(url):
    return html.unescape(url) if url else url


EXTERNAL_VIDEO_HOSTS = ("redgifs.com", "gfycat.com", "streamable.com", "clips.twitch.tv")


def extract_media(post, _from_crosspost=False):
    """Classify a post's media. Returns a dict with 'type': 'gallery', 'image',
    'reddit_video', 'external_video', or 'none'.
    """
    # Bare-bones RSS-only fallback post (no full Reddit JSON was obtainable for it).
    if "_rss_fallback_images" in post:
        imgs = post["_rss_fallback_images"]
        if not imgs:
            return {"type": "none"}
        return {"type": "gallery", "urls": imgs} if len(imgs) > 1 else {"type": "image", "url": imgs[0]}

    if post.get("is_gallery") and post.get("media_metadata"):
        urls = []
        items = post.get("gallery_data", {}).get("items", [])
        for item in items:
            meta = post["media_metadata"].get(item.get("media_id"), {})
            if meta.get("status") != "valid":
                continue
            src = meta.get("s", {})
            u = src.get("u") or src.get("gif") or src.get("mp4")
            if u:
                urls.append(_unescape(u))
        if urls:
            return {"type": "gallery", "urls": urls}

    reddit_video = (post.get("secure_media") or post.get("media") or {}).get("reddit_video")
    if post.get("is_video") and reddit_video:
        return {
            "type": "reddit_video",
            "hls_url": reddit_video.get("hls_url"),
            "fallback_url": reddit_video.get("fallback_url"),
        }

    post_hint = post.get("post_hint", "")
    url = post.get("url_overridden_by_dest") or post.get("url", "") or ""

    if post_hint == "image" or re.search(r"\.(jpg|jpeg|png|webp|gif)(\?.*)?$", url, re.I):
        return {"type": "image", "url": url}

    if (post_hint == "rich:video"
            or any(host in url for host in EXTERNAL_VIDEO_HOSTS)
            or url.lower().endswith(".gifv")):
        return {"type": "external_video", "url": url}

    if not _from_crosspost and post.get("crosspost_parent_list"):
        return extract_media(post["crosspost_parent_list"][0], _from_crosspost=True)

    return {"type": "none"}


# --- DOWNLOAD HELPERS ---
def download_file(url, filename, max_retries=MAX_RETRIES):
    """Stream a URL to disk with retry/backoff. Works for images and plain mp4 bytes."""
    for attempt in range(1, max_retries + 1):
        resp = request_with_retry(url, stream=True, max_retries=1)
        if resp is None:
            if attempt < max_retries:
                time.sleep(BASE_BACKOFF * attempt + random.uniform(0, 2))
                continue
            return False
        try:
            with open(filename, "wb") as f:
                for chunk in resp.iter_content(1024 * 64):
                    f.write(chunk)
            return True
        except OSError as e:
            log.warning("Failed writing %s: %s", filename, e)
            return False
        finally:
            resp.close()
    return False


def download_reddit_video(hls_url, fallback_url, base_filename):
    """Download+mux a v.redd.it video. Prefers the HLS master playlist (bundles audio+video
    in one CDN fetch — this is not the page-scraping path that requires login).
    Falls back to the video-only fallback_url (no audio) if ffmpeg/HLS isn't usable.
    """
    out_path = f"{base_filename}.mp4"

    if hls_url and shutil.which("ffmpeg"):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-user_agent", BROWSER_USER_AGENT,
                        "-i", hls_url,
                        "-c", "copy", "-bsf:a", "aac_adtstoasc",
                        out_path,
                    ],
                    timeout=FFMPEG_TIMEOUT,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
                log.warning("ffmpeg attempt %d/%d failed (rc=%s): %s",
                            attempt, MAX_RETRIES, result.returncode, result.stderr[-500:])
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg timed out on attempt %d/%d for %s", attempt, MAX_RETRIES, hls_url)
            time.sleep(BASE_BACKOFF * attempt)

    if fallback_url:
        log.info("Falling back to video-only stream (no audio) for this post.")
        if download_file(fallback_url, out_path):
            return out_path

    return None


def download_external_video(url, base_filename):
    """yt-dlp fallback for non-Reddit-hosted video links (redgifs, gfycat, streamable...).
    Never used for reddit.com URLs, so Reddit's login-required scraping path is never hit.
    """
    outtmpl_path = f"{base_filename}.%(ext)s"
    ydl_opts = {
        "outtmpl": outtmpl_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
    }
    if YTDLP_COOKIES_FILE:
        ydl_opts["cookiefile"] = YTDLP_COOKIES_FILE

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
                log.warning("yt-dlp 429 for %s (attempt %d/%d), backing off %.1fs",
                            url, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            break

    log.info("External video download skipped/failed for %s: %s", url, last_err)
    return None


def cleanup_files(file_list):
    for file in file_list:
        try:
            if file and os.path.exists(file):
                os.remove(file)
        except OSError as e:
            log.warning("Could not delete temp file %s: %s", file, e)


def build_caption(title, sub, post_url):
    safe_title = html.escape(title)
    return f'<b>{safe_title}</b>\n\nFrom r/{sub}\n<a href="{html.escape(post_url)}">View on Reddit</a>'


# --- SENDING ---
def send_media_post(media, caption, sub, pid):
    """Attempts to send actual media to Telegram. Returns True on success, False otherwise
    (caller moves on to the next candidate post rather than falling back to a bare link).
    """
    files_to_cleanup = []
    opened_handles = []
    try:
        if media["type"] == "gallery":
            media_group = []
            for i, url in enumerate(media["urls"][:5]):  # Telegram album cap
                if i > 0:
                    time.sleep(random.uniform(1.5, 3.5))  # avoid bursting the image CDN
                filename = f"temp_img_{sub}_{pid}_{i}.jpg"
                if download_file(url, filename):
                    files_to_cleanup.append(filename)
                    fh = open(filename, "rb")
                    opened_handles.append(fh)
                    if i == 0:
                        media_group.append(InputMediaPhoto(
                            fh, caption=caption, parse_mode="HTML", has_spoiler=SPOILER_MEDIA))
                    else:
                        media_group.append(InputMediaPhoto(fh, has_spoiler=SPOILER_MEDIA))

            if not media_group:
                return False
            if len(media_group) == 1:
                bot.send_photo(GROUP_CHAT_ID, opened_handles[0], caption=caption,
                                parse_mode="HTML", has_spoiler=SPOILER_MEDIA)
            else:
                bot.send_media_group(GROUP_CHAT_ID, media=media_group)
            log.info("Sent %d image(s) for r/%s post %s.", len(media_group), sub, pid)
            return True

        if media["type"] == "image":
            filename = f"temp_img_{sub}_{pid}.jpg"
            if not download_file(media["url"], filename):
                return False
            files_to_cleanup.append(filename)
            with open(filename, "rb") as fh:
                bot.send_photo(GROUP_CHAT_ID, fh, caption=caption,
                                parse_mode="HTML", has_spoiler=SPOILER_MEDIA)
            log.info("Sent image for r/%s post %s.", sub, pid)
            return True

        if media["type"] == "reddit_video":
            base_filename = f"temp_video_{sub}_{pid}"
            video_file = download_reddit_video(media.get("hls_url"), media.get("fallback_url"), base_filename)
            if not video_file or not os.path.exists(video_file):
                return False
            files_to_cleanup.append(video_file)
            if os.path.getsize(video_file) >= MAX_FILE_SIZE:
                log.info("Reddit video for %s in r/%s exceeds Telegram's size limit, skipping.", pid, sub)
                return False
            with open(video_file, "rb") as v:
                bot.send_video(GROUP_CHAT_ID, v, caption=caption,
                                parse_mode="HTML", has_spoiler=SPOILER_MEDIA)
            log.info("Sent Reddit-hosted video for r/%s post %s.", sub, pid)
            return True

        if media["type"] == "external_video":
            base_filename = f"temp_extvid_{sub}_{pid}"
            video_file = download_external_video(media["url"], base_filename)
            if not video_file or not os.path.exists(video_file):
                return False
            files_to_cleanup.append(video_file)
            if os.path.getsize(video_file) >= MAX_FILE_SIZE:
                log.info("External video for %s in r/%s exceeds Telegram's size limit, skipping.", pid, sub)
                return False
            with open(video_file, "rb") as v:
                bot.send_video(GROUP_CHAT_ID, v, caption=caption,
                                parse_mode="HTML", has_spoiler=SPOILER_MEDIA)
            log.info("Sent external video for r/%s post %s.", sub, pid)
            return True

        return False

    except Exception as e:
        log.error("Error sending media for post %s in r/%s: %s", pid, sub, e)
        return False

    finally:
        for fh in opened_handles:
            try:
                fh.close()
            except OSError:
                pass
        cleanup_files(files_to_cleanup)


# --- MAIN LOGIC ---
def process_subreddit(sub, state):
    log.info("Scanning r/%s...", sub)
    posted = state.get(sub, [])

    try:
        posts = get_top_posts(sub, POSTS_TO_CHECK)
    except Exception as e:
        log.error("Could not fetch posts for r/%s: %s", sub, e)
        return

    if not posts:
        log.info("No posts found for r/%s (all fetch tiers exhausted or subreddit empty).", sub)
        return

    for post in posts:
        pid = post.get("id")
        if not pid or pid in posted:
            continue

        media = extract_media(post)
        if media["type"] == "none":
            log.info("Skipping non-media post %s in r/%s.", pid, sub)
            continue

        title = post.get("title", "")
        permalink = post.get("permalink", "")
        post_url = "https://www.reddit.com" + permalink if permalink else f"https://redd.it/{pid}"
        caption = build_caption(title, sub, post_url)

        if send_media_post(media, caption, sub, pid):
            posted.append(pid)
            state[sub] = posted[-HISTORY_SIZE:]
            return  # one post per subreddit per run, same as before

        log.warning("Failed to send media for post %s in r/%s, trying next candidate.", pid, sub)

    log.info("No new postable media found for r/%s this run.", sub)


def main():
    state = load_state()
    for sub in SUBREDDITS:
        process_subreddit(sub, state)
        save_state(state)  # save incrementally so one bad subreddit doesn't lose earlier progress
        delay = MIN_SUB_DELAY + random.uniform(0, max(MAX_SUB_DELAY - MIN_SUB_DELAY, 0))
        log.info("Sleeping %.1fs before next subreddit...", delay)
        time.sleep(delay)


if __name__ == "__main__":
    main()