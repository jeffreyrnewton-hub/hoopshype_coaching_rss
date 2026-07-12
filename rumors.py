#!/usr/bin/env python3
"""
HoopsHype coaching-rumor RSS feed.

Fetches the coaching rumors, merges any new ones into a running archive, and
regenerates docs/feed.xml. GitHub Pages serves that file; you subscribe in any
RSS reader. No credentials anywhere.

HoopsHype is a Next.js app that server-renders its GraphQL results into a
__NEXT_DATA__ <script> tag, so one plain HTTP GET already contains the rumor
JSON. No browser, no API, no CSS selectors.

    python3 rumors.py            # fetch -> merge -> write feed
    python3 rumors.py --dry-run  # show what's new, write nothing
"""

import json
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

TOPIC = "e300653b-ba27-432e-b06f-e14978b768fa"  # "Coaching" tag
URL = f"https://www.hoopshype.com/rumors/?topic={TOPIC}"

# --- Set this to your Pages URL after step 2 of the README -----------------
FEED_URL = "https://YOUR-USERNAME.github.io/YOUR-REPO/feed.xml"

ARCHIVE = Path("items.json")   # running state: every rumor we've ever seen
FEED = Path("docs/feed.xml")   # published output
MAX_ARCHIVE = 300              # cap the state file
MAX_FEED = 50                  # items in the published feed

# Full rumor text in the feed, or a short excerpt + link?
#   None  -> full text (fine for a private/personal feed)
#   200   -> excerpt, the polite pattern if you ever share the URL
EXCERPT_CHARS = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------
def get_next_data():
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        raise RuntimeError("__NEXT_DATA__ not found — page structure changed.")
    return json.loads(tag.string)


def extract_assets(next_data):
    """
    Pull the rumor list out of the dehydrated React Query cache. Scans every
    cached query rather than hardcoding an index, preferring the one scoped to
    our topic UUID — so this survives HoopsHype adding or reordering queries.
    """
    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )

    def assets_of(query):
        data = (query.get("state") or {}).get("data") or {}
        pages = data.get("pages")
        if not isinstance(pages, list):
            return []
        out = []
        for page in pages:
            out.extend(((page or {}).get("searchAssets") or {}).get("assets") or [])
        return out

    for q in queries:
        if TOPIC in json.dumps(q.get("queryKey", "")):
            hits = assets_of(q)
            if hits:
                return hits
    for q in queries:
        hits = assets_of(q)
        if hits:
            return hits
    return []


def parse_body(asset):
    """contentBody is an HTML blockquote: the rumor text plus a <cite> credit."""
    html = "\n".join(b.get("value") or "" for b in (asset.get("contentBody") or []))
    if not html:
        return (asset.get("headline") or "").replace("\u2026", "...").strip(), "", ""

    soup = BeautifulSoup(html, "html.parser")
    source_name, source_url = "", ""
    cite = soup.find("cite")
    if cite:
        link = cite.find("a")
        if link:
            source_name = link.get_text(strip=True)
            source_url = link.get("href", "")
        cite.decompose()
    return soup.get_text(" ", strip=True), source_name, source_url


def primary_team(asset):
    orgs = [
        t for t in (asset.get("tags") or [])
        if (t.get("tag") or {}).get("vocabulary") == "Organizations"
    ]
    for t in orgs:
        if t.get("isPrimary"):
            return (t["tag"].get("displayName") or "").strip()
    return (orgs[0]["tag"].get("displayName") or "").strip() if orgs else ""


def normalize(asset):
    text, source_name, source_url = parse_body(asset)
    team = primary_team(asset)

    # The blockquote usually opens with "Miami Heat : ..." — drop it, since the
    # team is already its own field.
    if team and text.startswith(team):
        text = text[len(team):].lstrip(" :·-").strip()

    return {
        "id": str(asset.get("id", "")),
        "text": text,
        "team": team,
        "published": asset.get("initialPublishDate") or "",
        "url": (asset.get("pageURL") or {}).get("long", ""),
        "source_name": source_name,
        "source_url": source_url,
    }


# --------------------------------------------------------------------------
# Archive
# --------------------------------------------------------------------------
def load_archive():
    if not ARCHIVE.exists():
        return []
    try:
        return json.loads(ARCHIVE.read_text())
    except json.JSONDecodeError:
        print("items.json unreadable; starting fresh.", file=sys.stderr)
        return []


def sort_key(item):
    try:
        return datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
    except (ValueError, KeyError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------
def pub_date(item):
    try:
        dt = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
    except (ValueError, KeyError, AttributeError):
        dt = datetime.now(timezone.utc)
    return format_datetime(dt)


def title_of(item):
    body = item["text"]
    snippet = body if len(body) <= 90 else body[:87].rstrip() + "..."
    return f"{item['team']}: {snippet}" if item["team"] else snippet


def description_of(item):
    body = item["text"]
    truncated = False
    if EXCERPT_CHARS and len(body) > EXCERPT_CHARS:
        body = body[:EXCERPT_CHARS].rstrip() + "..."
        truncated = True

    parts = [f"<p>{escape(body)}</p>"]
    if item.get("source_url"):
        parts.append(
            f"<p>Source: <a href=\"{escape(item['source_url'])}\">"
            f"{escape(item.get('source_name') or 'link')}</a></p>"
        )
    if truncated:
        parts.append(f"<p><a href=\"{escape(item['url'])}\">Read the full rumor →</a></p>")
    return "".join(parts)


def build_feed(items):
    now = format_datetime(datetime.now(timezone.utc))
    entries = []
    for item in items[:MAX_FEED]:
        entries.append(
            "    <item>\n"
            f"      <title>{escape(title_of(item))}</title>\n"
            f"      <link>{escape(item['url'])}</link>\n"
            f"      <guid isPermaLink=\"false\">hoopshype-{escape(item['id'])}</guid>\n"
            f"      <pubDate>{pub_date(item)}</pubDate>\n"
            + (f"      <category>{escape(item['team'])}</category>\n" if item["team"] else "")
            + f"      <description><![CDATA[{description_of(item)}]]></description>\n"
            "    </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>HoopsHype — NBA Coaching Rumors</title>\n"
        f"    <link>{escape(URL)}</link>\n"
        "    <description>Coaching rumors aggregated from HoopsHype. "
        "Unofficial personal feed; all content belongs to HoopsHype and the "
        "original reporters.</description>\n"
        "    <language>en-us</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f'    <atom:link href="{escape(FEED_URL)}" rel="self" type="application/rss+xml"/>\n'
        + "\n".join(entries)
        + "\n  </channel>\n</rss>\n"
    )


# --------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv

    raw = extract_assets(get_next_data())
    if not raw:
        # Fail loudly. A silent zero would freeze the feed and you'd never
        # learn why; a failed job makes GitHub notify you instead.
        print("Parsed 0 rumors — page structure changed.", file=sys.stderr)
        sys.exit(1)

    scraped = [normalize(a) for a in raw]
    scraped = [r for r in scraped if r["id"] and r["text"]]

    archive = load_archive()
    known = {item["id"] for item in archive}
    new = [r for r in scraped if r["id"] not in known]

    print(f"{len(scraped)} on page, {len(new)} new, {len(archive)} in archive.")

    if dry_run:
        for r in new:
            label = f"{r['team']} · " if r["team"] else ""
            print(f"\n  [{label}{r['published']}]\n  {r['text'][:160]}")
        return

    merged = sorted(archive + new, key=sort_key, reverse=True)[:MAX_ARCHIVE]

    ARCHIVE.write_text(json.dumps(merged, indent=1, ensure_ascii=False))
    FEED.parent.mkdir(parents=True, exist_ok=True)
    FEED.write_text(build_feed(merged), encoding="utf-8")

    print(f"Wrote {FEED} with {min(len(merged), MAX_FEED)} items (+{len(new)} new).")


if __name__ == "__main__":
    main()
