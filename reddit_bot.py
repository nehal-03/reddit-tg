import os
import html
import time
import random
import logging

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
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "4"))
BASE_BACKOFF = float(os.environ.get("BASE_BACKOFF", "5"))

# Two hosts to try — old.reddit.com sometimes stays reachable when www.reddit.com
# is rate-limiting a given IP, and vice versa.
RSS_HOSTS = ["https://www.reddit.com", "https://old.reddit.com"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit_digest")

session = requests.Session()
# A genuine browser UA blends in with normal traffic. A UA that explicitly announces
# itself as a bot (e.g. "compatible; RedditDigestBot/1.0") is an easy signal for
# Reddit's anti-scraping systems to key stricter rate limits off of.
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
})


def _fetch_with_retry(url, max_retries=MAX_RETRIES):
    """GET a URL with exponential backoff + jitter on 429/5xx, honoring Retry-After.
    Returns the Response on success, or None if retries are exhausted / non-retryable.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log.warning("Request error for %s (attempt %d/%d): %s", url, attempt, max_retries, e)
            time.sleep(BASE_BACKOFF * attempt + random.uniform(0, 2))
            continue

        if resp.status_code == 200:
            return resp

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else BASE_BACKOFF * (2 ** (attempt - 1))
            except ValueError:
                wait = BASE_BACKOFF * (2 ** (attempt - 1))
            wait += random.uniform(0, 2)
            log.warning("429 from %s (attempt %d/%d), backing off %.1fs", url, attempt, max_retries, wait)
            time.sleep(wait)
            continue

        if 500 <= resp.status_code < 600:
            wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning("%s from %s (attempt %d/%d), retrying in %.1fs",
                        resp.status_code, url, attempt, max_retries, wait)
            time.sleep(wait)
            continue

        log.warning("Non-retryable status %s for %s", resp.status_code, url)
        return None

    log.warning("Giving up on %s after %d attempts.", url, max_retries)
    return None


def get_top_post(sub):
    """Fetch the #1 'top of day' post title + link for a subreddit via RSS.
    Tries www.reddit.com first, then old.reddit.com if that's exhausted.
    Returns (title, link) or None if unavailable from either host.
    """
    for host in RSS_HOSTS:
        url = f"{host}/r/{sub}/top/.rss?t=day"
        resp = _fetch_with_retry(url)
        if resp is None:
            continue

        feed = feedparser.parse(resp.content)
        if not feed.entries:
            log.warning("No entries in RSS feed for r/%s via %s", sub, host)
            continue

        entry = feed.entries[0]
        title = entry.get("title", "(no title)")
        link = entry.get("link", "")
        return title, link

    log.warning("Failed to fetch r/%s from all hosts.", sub)
    return None


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
        delay = 50 + random.uniform(0, 50)  # jitter to avoid bursting requests
        log.info("Sleeping %.1fs before next subreddit...", delay)
        time.sleep(delay)

    if not results:
        log.warning("No posts fetched from any subreddit, skipping send.")
        return

    message = build_digest_message(results)
    send_telegram_message(message)


if __name__ == "__main__":
    main()
