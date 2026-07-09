import os
import html
import logging
from urllib.parse import quote

import feedparser
import requests

# --- CONFIGURATION (all from environment variables — never hardcode secrets) ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]      # required
GROUP_CHAT_ID = os.environ["GROUP_CHAT_ID"]        # required, e.g. -1001234567890

# Comma-separated list, e.g. "aww,animalsbeingderps,beautiful,eli5"
SUBREDDITS = [
    s.strip() for s in os.environ.get(
        "SUBREDDITS", "aww,animalsbeingderps,beautiful,eli5"
    ).split(",") if s.strip()
]

REQUEST_TIMEOUT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit_digest")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; RedditDigestBot/1.0)"
})


def get_top_post(sub):
    """Fetch the #1 'top of day' post title + link for a subreddit via RSS.
    Returns (title, link) or None if unavailable.
    """
    url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to fetch RSS for r/%s: %s", sub, e)
        return None

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        log.warning("No entries in RSS feed for r/%s", sub)
        return None

    entry = feed.entries[0]
    title = entry.get("title", "(no title)")
    link = entry.get("link", "")
    return title, link


def build_digest_message(results):
    """results: list of (subreddit, title, link) tuples."""
    lines = ["<b>📰 Daily Top Posts</b>", ""]
    for sub, title, link in results:
        safe_title = html.escape(title)
        lines.append(f'• <b>r/{sub}</b>: <a href="{html.escape(link)}">{safe_title}</a>')
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": GROUP_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = session.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        log.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
        resp.raise_for_status()
    log.info("Digest sent successfully.")


def main():
    results = []
    for sub in SUBREDDITS:
        post = get_top_post(sub)
        if post is None:
            continue
        title, link = post
        results.append((sub, title, link))
        log.info("r/%s -> %s", sub, title)

    if not results:
        log.warning("No posts fetched from any subreddit, skipping send.")
        return

    message = build_digest_message(results)
    send_telegram_message(message)


if __name__ == "__main__":
    main()