# Reddit → Telegram Daily Bot

Fetches the top-of-the-day post from one or more subreddits via RSS and posts it
(image, gallery, video, or text) into a Telegram group, once a day, via GitHub Actions.
Media can optionally be sent with Telegram's spoiler blur.

## 1. Rotate your bot token first

If a token was ever pasted into chat, a doc, or committed to a repo, treat it as
burned: open Telegram, message **@BotFather**, run `/mybots` → your bot → **API Token** →
**Revoke current token**, and grab the new one. Never hardcode it in the script —
this version reads everything from environment variables.

## 2. Get your Telegram values

- **Bot token**: from @BotFather as above.
- **Group chat ID**: add your bot to the group, send any message, then visit
  `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and look for `"chat":{"id": ...}`.
  For supergroups this id is negative and usually looks like `-100xxxxxxxxxx`.
- Make sure the bot is an **admin** in the group if you want it to post media reliably.

## 3. Push this folder to a GitHub repo

```
git init
git add .
git commit -m "Initial commit: reddit -> telegram bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## 4. Add secrets and variables in GitHub

Repo → **Settings** → **Secrets and variables** → **Actions**:

**Secrets** (sensitive):
- `TELEGRAM_TOKEN` — your bot token
- `GROUP_CHAT_ID` — your group chat id

**Variables** (optional, non-sensitive):
- `SUBREDDITS` — comma-separated, e.g. `aww,earthporn,mildlyinteresting` (defaults to `aww`)
- `SPOILER_MEDIA` — `true` or `false` (defaults to `true`)

## 5. Schedule

`.github/workflows/daily-post.yml` runs daily at **09:00 UTC**. Edit the cron line
to change the time (cron is always UTC on GitHub Actions). You can also trigger a
run manually from the **Actions** tab using **Run workflow** (workflow_dispatch).

## How duplicate-avoidance works

The script writes the last-posted post id per subreddit to `posted.json` and the
workflow commits that file back to the repo after each run. If the "top post of
the day" hasn't changed since the last run, it's skipped instead of reposted.

## Local testing

```
export TELEGRAM_TOKEN="..."
export GROUP_CHAT_ID="..."
export SUBREDDITS="aww"
export SPOILER_MEDIA="true"
pip install -r requirements.txt
python reddit_bot.py
```

## Notes / limitations

- Telegram's spoiler feature requires Bot API 6.7+; `pyTelegramBotAPI>=4.14.0`
  (pinned in `requirements.txt`) supports it via the `has_spoiler` parameter.
- Reddit's RSS/video CDN occasionally rate-limits or blocks datacenter IPs
  (GitHub Actions runners included) — if downloads intermittently fail, that's
  usually why; the script logs and falls back to a text-only post rather than crashing.
- Videos over 48 MB can't be uploaded via the Bot API, so the script falls back
  to posting the caption + a link.
