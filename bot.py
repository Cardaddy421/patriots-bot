import os, time, textwrap, logging, hashlib, re
from urllib.parse import urlparse
from io import BytesIO

import feedparser
import tweepy
import requests
from dotenv import load_dotenv

# =========================
# SETTINGS (tweak these)
# =========================
FEEDS = [
    "https://www.patspulpit.com/rss/index.xml",
    "https://nesn.com/feed/",
    "https://patriotswire.usatoday.com/feed/",
    "https://www.nbcsportsboston.com/feed/",
]
MAX_POSTS_PER_RUN = 4
DEFAULT_TAGS = " #Patriots #NFL"
POST_IMAGES = True          # <— turn off if you don’t want images
TIME_BETWEEN_TWEETS = 3     # seconds
SEEN_FILE = "seen.txt"      # keeps hashes of posted items

# Light rewording templates (we’ll pick the first that fits 280 chars)
REWORD_PREFIXES = [
    "Update:",
    "Report:",
    "New:",
    "Latest:",
]

# =========================
# LOGGING
# =========================
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
console = logging.getLogger("console")
console.setLevel(logging.INFO)

# =========================
# AUTH
# =========================
load_dotenv()
API_KEY     = os.getenv("X_API_KEY")
API_SECRET  = os.getenv("X_API_SECRET")
ACC_TOKEN   = os.getenv("X_ACCESS_TOKEN")
ACC_SECRET  = os.getenv("X_ACCESS_SECRET")
if not all([API_KEY, API_SECRET, ACC_TOKEN, ACC_SECRET]):
    raise SystemExit("❌ Missing X keys in .env.")

# v2 client for create_tweet
client = tweepy.Client(
    consumer_key=API_KEY,
    consumer_secret=API_SECRET,
    access_token=ACC_TOKEN,
    access_token_secret=ACC_SECRET
)

# v1.1 API just for media upload (images)
auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACC_TOKEN, ACC_SECRET)
api_v1 = tweepy.API(auth)

# =========================
# UTILITIES
# =========================
def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for h in sorted(seen):
            f.write(h + "\n")

def normalize_url(u: str) -> str:
    try:
        p = urlparse(u)
        # strip query/fragment for dedupe
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return u

def item_hash(title: str, url: str) -> str:
    norm = normalize_url(url)
    key = (title or "").strip().lower()
    raw = f"{norm}|{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def looks_like_patriots(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ["patriot", "new england", "foxboro", "gillette"])

def clean_title(t: str) -> str:
    # Remove trailing site names like “- Site Name”
    return re.sub(r"\s*[-–—]\s*[^-–—]{2,}$", "", (t or "").strip())

def build_tweet(title: str, source: str, url: str) -> str:
    title = clean_title(title)

    # light rewording: add a prefix if it still fits
    for prefix in [""] + REWORD_PREFIXES:
        maybe = f"{prefix} {title}".strip()
        text = f"{maybe} — {source}\n{url}{DEFAULT_TAGS}"
        if len(text) <= 280:
            return text

    # If still too long, trim the title only
    trimmed = textwrap.shorten(title, width=200, placeholder="...")
    return f"{trimmed} — {source}\n{url}{DEFAULT_TAGS}"

def extract_image_url(entry) -> str | None:
    # Try common RSS fields
    # 1) media_content
    media = entry.get("media_content") or []
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]

    # 2) links with rel=enclosure (often images)
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and "image" in (link.get("type") or ""):
            return link.get("href")

    # 3) media_thumbnail
    thumbs = entry.get("media_thumbnail") or []
    if thumbs and isinstance(thumbs, list) and thumbs[0].get("url"):
        return thumbs[0]["url"]

    # 4) content HTML <img> (last resort; crude)
    content_list = entry.get("content") or []
    if content_list:
        html = content_list[0].get("value") or ""
        m = re.search(r'<img[^>]+src="([^"]+)"', html)
        if m:
            return m.group(1)

    # 5) summary HTML <img>
    summary = entry.get("summary", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', summary or "")
    if m:
        return m.group(1)

    return None

def upload_image_to_x(image_url: str) -> str | None:
    try:
        r = requests.get(image_url, timeout=10)
        r.raise_for_status()
        bio = BytesIO(r.content)
        media = api_v1.media_upload(filename="image.jpg", file=bio)
        return media.media_id_string
    except Exception as e:
        logging.warning(f"Image upload failed: {e}")
        return None

# =========================
# MAIN
# =========================
def run_bot():
    seen = load_seen()
    posted = 0

    for feed_url in FEEDS:
        if posted >= MAX_POSTS_PER_RUN:
            break

        parsed = feedparser.parse(feed_url)
        source = parsed.feed.get("title", "Source")

        for entry in parsed.entries[:12]:
            if posted >= MAX_POSTS_PER_RUN:
                break

            title = (entry.get("title") or "").strip()
            link  = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            if not looks_like_patriots(title):
                continue

            h = item_hash(title, link)
            if h in seen:
                continue

            tweet_text = build_tweet(title, source, link)

            media_ids = None
            if POST_IMAGES:
                img = extract_image_url(entry)
                if img:
                    mid = upload_image_to_x(img)
                    if mid:
                        media_ids = [mid]

            # Post (v2)
            try:
                if media_ids:
                    client.create_tweet(text=tweet_text, media_ids=media_ids)
                else:
                    client.create_tweet(text=tweet_text)
                seen.add(h)
                posted += 1
                logging.info(f"✅ Posted: {title} | {normalize_url(link)}")
                console.info(f"✅ Posted: {title}")
            except tweepy.errors.Forbidden as e:
                logging.error(f"Forbidden (permissions/tier?): {e}")
                console.error("❌ Forbidden error — check app Write permission & tokens.")
            except Exception as e:
                logging.error(f"Tweet failed: {e}")
                console.error(f"❌ Tweet failed: {e}")

            time.sleep(TIME_BETWEEN_TWEETS)

    save_seen(seen)
    if posted == 0:
        logging.info("Nothing new to post right now.")
        console.info("Nothing new to post right now.")

if __name__ == "__main__":
    run_bot()

