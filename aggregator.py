"""
AI News Aggregator Bot
----------------------
Pulls fresh AI news from RSS feeds, Hacker News, Reddit, arXiv and
(optionally) YouTube, de-duplicates against previously seen items,
and pushes new items to your Telegram.

Required environment variables:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your chat id (see README)

Optional:
  YOUTUBE_API_KEY     - only needed if you want competitor-channel tracking
"""

import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG - edit these lists to taste
# ---------------------------------------------------------------------------

RSS_FEEDS = {
    "OpenAI":            "https://openai.com/news/rss.xml",
    "Google DeepMind":   "https://deepmind.google/blog/rss.xml",
    "Anthropic":         "https://www.anthropic.com/rss.xml",
    "Hugging Face":      "https://huggingface.co/blog/feed.xml",
    "The Verge AI":      "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "VentureBeat AI":    "https://venturebeat.com/category/ai/feed/",
    "MIT Tech Review AI":"https://www.technologyreview.com/topic/artificial-intelligence/feed",
}

SUBREDDITS = ["LocalLLaMA", "MachineLearning", "singularity"]
REDDIT_MIN_SCORE = 200          # only surface posts with at least this many upvotes

HN_QUERY_TERMS = ["AI", "LLM", "GPT", "Claude", "Gemini", "open source model"]
HN_MIN_POINTS = 100

ARXIV_CATEGORIES = ["cs.AI", "cs.CL", "cs.LG"]
ARXIV_MAX_RESULTS = 10          # newest N papers per run (they are filtered by dedupe)

# YouTube channel IDs of competitors to track (find via channel page source or
# tools like commentpicker.com/youtube-channel-id.php). Leave empty to skip.
YOUTUBE_CHANNEL_IDS = [
    # "UCXZCJLdBC09xxGZ6gcdrc6A",   # example: OpenAI's channel
]

SEEN_FILE = Path("seen_items.json")
MAX_ITEMS_PER_RUN = 25          # safety cap so Telegram isn't flooded
USER_AGENT = "ai-news-aggregator/1.0 (personal project)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    # keep the file from growing forever: cap at 5000 most recent ids
    SEEN_FILE.write_text(json.dumps(list(seen)[-5000:], indent=0))


def strip_tags(text):
    """Very light HTML tag remover for RSS descriptions."""
    out, inside = [], False
    for ch in text:
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            out.append(ch)
    return "".join(out).strip()

# ---------------------------------------------------------------------------
# Source fetchers - each returns a list of dicts:
#   {"id": unique str, "source": str, "title": str, "url": str, "extra": str}
# ---------------------------------------------------------------------------

def fetch_rss():
    items = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for name, url in RSS_FEEDS.items():
        try:
            root = ET.fromstring(http_get(url))
            # RSS 2.0
            for entry in root.iter("item"):
                title = (entry.findtext("title") or "").strip()
                link = (entry.findtext("link") or "").strip()
                if title and link:
                    items.append({"id": link, "source": name, "title": title,
                                  "url": link, "extra": ""})
            # Atom
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                link_el = entry.find("atom:link", ns)
                link = link_el.get("href") if link_el is not None else ""
                if title and link:
                    items.append({"id": link, "source": name, "title": title,
                                  "url": link, "extra": ""})
        except Exception as e:
            print(f"[warn] RSS {name} failed: {e}")
    return items


def fetch_hackernews():
    """Uses the free Algolia HN search API - no key needed."""
    items = []
    since = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    for term in HN_QUERY_TERMS:
        q = urllib.parse.quote(term)
        url = (f"https://hn.algolia.com/api/v1/search?query={q}"
               f"&tags=story&numericFilters=points>{HN_MIN_POINTS},created_at_i>{since}")
        try:
            data = json.loads(http_get(url))
            for hit in data.get("hits", []):
                story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
                items.append({
                    "id": f"hn-{hit['objectID']}",
                    "source": "Hacker News",
                    "title": hit.get("title", "(no title)"),
                    "url": story_url,
                    "extra": f"{hit.get('points', 0)} points",
                })
        except Exception as e:
            print(f"[warn] HN search '{term}' failed: {e}")
        time.sleep(0.5)
    return items


def fetch_reddit():
    """Reads public JSON endpoints - no OAuth needed for read-only."""
    items = []
    for sub in SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=25"
        try:
            data = json.loads(http_get(url))
            for child in data["data"]["children"]:
                post = child["data"]
                if post.get("stickied") or post.get("score", 0) < REDDIT_MIN_SCORE:
                    continue
                items.append({
                    "id": f"reddit-{post['id']}",
                    "source": f"r/{sub}",
                    "title": post["title"],
                    "url": "https://www.reddit.com" + post["permalink"],
                    "extra": f"{post['score']} upvotes",
                })
        except Exception as e:
            print(f"[warn] Reddit r/{sub} failed: {e}")
        time.sleep(1)  # be polite; reddit rate-limits anonymous clients
    return items


def fetch_arxiv():
    items = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cats = "+OR+".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    url = (f"http://export.arxiv.org/api/query?search_query={cats}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results={ARXIV_MAX_RESULTS}")
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        print(f"[warn] arXiv failed: {e}")
        return items
    for entry in root.findall("atom:entry", ns):
        link = entry.findtext("atom:id", namespaces=ns, default="").strip()
        title = " ".join((entry.findtext("atom:title", namespaces=ns) or "").split())
        if link and title:
            items.append({"id": link, "source": "arXiv", "title": title,
                          "url": link, "extra": ""})
    return items


def fetch_youtube():
    """Latest uploads from competitor channels via YouTube Data API v3."""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key or not YOUTUBE_CHANNEL_IDS:
        return []
    items = []
    for channel_id in YOUTUBE_CHANNEL_IDS:
        url = (f"https://www.googleapis.com/youtube/v3/search?key={api_key}"
               f"&channelId={channel_id}&part=snippet&order=date&maxResults=5&type=video")
        try:
            data = json.loads(http_get(url))
            for it in data.get("items", []):
                vid = it["id"]["videoId"]
                items.append({
                    "id": f"yt-{vid}",
                    "source": f"YT: {it['snippet']['channelTitle']}",
                    "title": it["snippet"]["title"],
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "extra": "",
                })
        except Exception as e:
            print(f"[warn] YouTube channel {channel_id} failed: {e}")
    return items

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen()
    all_items = []
    all_items += fetch_rss()
    all_items += fetch_hackernews()
    all_items += fetch_reddit()
    all_items += fetch_arxiv()
    all_items += fetch_youtube()

    new_items = [i for i in all_items if i["id"] not in seen]
    print(f"Fetched {len(all_items)} items, {len(new_items)} new.")

    for item in new_items[:MAX_ITEMS_PER_RUN]:
        extra = f" ({item['extra']})" if item["extra"] else ""
        msg = f"[{item['source']}]{extra}\n{item['title']}\n{item['url']}"
        try:
            send_telegram(msg)
            seen.add(item["id"])
            time.sleep(1.2)  # respect Telegram's ~1 msg/sec limit
        except Exception as e:
            print(f"[warn] Telegram send failed: {e}")

    # mark overflow items as seen too, so they don't pile up forever
    for item in new_items[MAX_ITEMS_PER_RUN:]:
        seen.add(item["id"])

    save_seen(seen)


if __name__ == "__main__":
    main()
