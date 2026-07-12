#!/usr/bin/env python3
"""
HoopsHype coaching-rumor digest feed.

Each run collects any rumors that are new since last time and publishes them as
ONE feed item. So your reader shows a single unread entry -- click it and every
new rumor is right there.

HoopsHype is a Next.js app that server-renders its GraphQL results into a
__NEXT_DATA__ <script> tag, so one plain HTTP GET already contains the rumor
JSON. No browser, no API, no CSS selectors.

    python3 rumors.py            # fetch -> digest -> write feed
    python3 rumors.py --dry-run  # show what's new, write nothing
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

TOPIC = "e300653b-ba27-432e-b06f-e14978b768fa"  # "Coaching" tag
URL = f"https://www.hoopshype.com/rumors/?topic={TOPIC}"

# --- Set this to your Pages URL once it's live -----------------------------
FEED_URL = "https://YOUR-USERNAME.github.io/YOUR-REPO/feed.xml"

STATE = Path("state.json")     # digests + anything waiting to be published
FEED = Path("docs/feed.xml")   # published output

# How long to let rumors pile up before publishing a digest.
#   0  -> publish every run that finds something (with a 6h cron: up to 4/day)
#   20 -> poll every 6h but publish at most one digest per day
MIN_HOURS_BETWEEN_DIGESTS = 0

MAX_DIGESTS = 100  # keep in state.json
MAX_FEED = 50      # publish in feed.xml

# Full rumor text, or a short excerpt + link?
#   None -> full text (fine for a personal feed)
#   200  -> excerpt, the polite pattern if you ever share the URL
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
# State
# --------------------------------------------------------------------------
def load_state():
    if not STATE.exists():
        return {"digests": [], "pending": []}
    try:
        data = json.loads(STATE.read_text())
        data.setdefault("digests", [])
        data.setdefault("pending", [])
        return data
    except json.JSONDecodeError:
        print("state.json unreadable; starting fresh.", file=sys.stderr)
        return {"digests": [], "pending": []}


def parse_dt(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def known_ids(state):
    """Every rumor we've already published or queued — the dedupe set."""
    ids = {r["id"] for r in state["pending"]}
    for digest in state["digests"]:
        ids.update(r["id"] for r in digest.get("rumors", []))
    return ids


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------
def rumor_html(r):
    body = r["text"]
    truncated = False
    if EXCERPT_CHARS and len(body) > EXCERPT_CHARS:
        body = body[:EXCERPT_CHARS].rstrip() + "..."
        truncated = True

    when = parse_dt(r["published"]).strftime("%b %d, %H:%M UTC")
    meta = f"{escape(r['team'])} · {when}" if r["team"] else when
    if r.get("source_url"):
        meta += (
            f" · <a href=\"{escape(r['source_url'])}\">"
            f"via {escape(r.get('source_name') or 'source')}</a>"
        )

    tail = (
        f" <a href=\"{escape(r['url'])}\">Read more →</a>"
        if truncated else
        f" <a href=\"{escape(r['url'])}\">HoopsHype →</a>"
    )

    return (
        "<div style=\"margin:0 0 20px;padding-bottom:16px;"
        "border-bottom:1px solid #e5e5e5\">"
        f"<div style=\"font-size:12px;color:#888;margin-bottom:5px\">{meta}</div>"
        f"<div style=\"font-size:15px;line-height:1.5\">{escape(body)}</div>"
        f"<div style=\"font-size:12px;margin-top:5px\">{tail}</div>"
        "</div>"
    )


def digest_title(digest):
    n = len(digest["rumors"])
    teams = [r["team"] for r in digest["rumors"] if r["team"]]
    unique = sorted(set(teams))

    when = parse_dt(digest["built_at"]).strftime("%b %d")
    noun = "rumor" if n == 1 else "rumors"

    if 1 <= len(unique) <= 3:
        return f"{n} coaching {noun} — {', '.join(unique)} ({when})"
    return f"{n} coaching {noun} — {when}"


def build_feed(digests):
    now = format_datetime(datetime.now(timezone.utc))
    entries = []

    for digest in digests[:MAX_FEED]:
        rumors = sorted(digest["rumors"], key=lambda r: parse_dt(r["published"]), reverse=True)
        body = "".join(rumor_html(r) for r in rumors)
        link = rumors[0]["url"] if rumors else URL

        entries.append(
            "    <item>\n"
            f"      <title>{escape(digest_title(digest))}</title>\n"
            f"      <link>{escape(link)}</link>\n"
            f"      <guid isPermaLink=\"false\">digest-{escape(digest['id'])}</guid>\n"
            f"      <pubDate>{format_datetime(parse_dt(digest['built_at']))}</pubDate>\n"
            f"      <description><![CDATA[{body}]]></description>\n"
            "    </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>HoopsHype — NBA Coaching Rumors</title>\n"
        f"    <link>{escape(URL)}</link>\n"
        "    <description>Coaching rumors from HoopsHype, batched into digests. "
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
    now = datetime.now(timezone.utc)

    raw = extract_assets(get_next_data())
    if not raw:
        # Fail loudly. A silent zero would freeze the feed and you'd never learn
        # why; a failed job makes GitHub notify you instead.
        print("Parsed 0 rumors — page structure changed.", file=sys.stderr)
        sys.exit(1)

    scraped = [r for r in (normalize(a) for a in raw) if r["id"] and r["text"]]

    state = load_state()
    seen = known_ids(state)
    new = [r for r in scraped if r["id"] not in seen]

    print(f"{len(scraped)} on page, {len(new)} new, "
          f"{len(state['pending'])} already pending, "
          f"{len(state['digests'])} digests published.")

    if dry_run:
        for r in new:
            label = f"{r['team']} · " if r["team"] else ""
            print(f"\n  [{label}{r['published']}]\n  {r['text'][:160]}")
        return

    state["pending"].extend(new)

    # Publish a digest if anything is waiting and enough time has passed.
    should_publish = bool(state["pending"])
    if should_publish and MIN_HOURS_BETWEEN_DIGESTS and state["digests"]:
        last = parse_dt(state["digests"][0]["built_at"])
        if now - last < timedelta(hours=MIN_HOURS_BETWEEN_DIGESTS):
            should_publish = False
            print(f"Holding {len(state['pending'])} rumor(s) — "
                  f"last digest was {(now - last).total_seconds() / 3600:.1f}h ago.")

    if should_publish:
        digest = {
            "id": now.strftime("%Y%m%dT%H%M%SZ"),
            "built_at": now.isoformat().replace("+00:00", "Z"),
            "rumors": state["pending"],
        }
        state["digests"].insert(0, digest)
        state["digests"] = state["digests"][:MAX_DIGESTS]
        state["pending"] = []
        print(f"Published digest {digest['id']} with {len(digest['rumors'])} rumor(s).")
    elif not state["pending"]:
        print("Nothing new.")

    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    FEED.parent.mkdir(parents=True, exist_ok=True)
    FEED.write_text(build_feed(state["digests"]), encoding="utf-8")
    print(f"Wrote {FEED} — {min(len(state['digests']), MAX_FEED)} digest item(s).")


if __name__ == "__main__":
    main()
