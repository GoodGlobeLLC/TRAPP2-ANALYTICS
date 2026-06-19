#!/usr/bin/env python3
"""
fetch_news.py — APPEND-ONLY news archive for TRAPP2-ANALYTICS.

Pulls recent news per ticker via yfinance (free, keyless) for every ticker in
the master.json files, and ARCHIVES everything permanently. Nothing is ever
deleted: each run merges new articles into a monthly shard, keeping every
article ever seen so you can study price behavior since each headline.

Outputs (all under data/news/):
    archive/YYYY-MM.json      — monthly shards, append-only. Each article:
        { id, url, headline, summary, ticker, source, datetime,
          firstSeenAt,          # when WE first archived it (ISO, UTC)
          priceAtFirstSeen,     # ticker price captured when first archived
          priceAtFirstSeenAt }  # exact time that price was captured (ISO, UTC)
    latest.json               — the most recent ~2500 across all shards, the
                                lean file the frontend loads by default (same
                                shape as before, so nothing downstream breaks).
    index.json                — { months: [...], totalArticles, generatedAt }

Why monthly shards: "never delete" means unbounded growth. Sharding by
publish-month keeps each file small, lets the frontend lazy-load only the
months it needs, and means a refresh only rewrites the CURRENT month's shard +
latest.json — old shards are never refetched or rewritten (exactly the
"new refreshes push with old ones, old ones just stored" behavior).
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

REPOS = [
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-1/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-2/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-3/main/data/master.json",
]
DATA = Path(__file__).resolve().parent.parent / "data" / "news"
ARCHIVE_DIR = DATA / "archive"
LATEST = DATA / "latest.json"
INDEX = DATA / "index.json"
PER_TICKER = 6
SLEEP = 0.12
LATEST_CAP = 2500


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "ValuatioAnalytics"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def master_rows(url):
    try:
        d = _get(url)
        rows = d if isinstance(d, list) else (d.get("rows") or list(d.values()))
        if isinstance(rows, dict):
            rows = list(rows.values())
        return rows
    except Exception as e:
        print(f"  x {url.split('/')[4]}: {e}", file=sys.stderr)
        return []


def shard_path(dt_iso):
    """Monthly shard path for an article's publish datetime (UTC)."""
    month = (dt_iso or "")[:7] or datetime.now(timezone.utc).strftime("%Y-%m")
    return ARCHIVE_DIR / f"{month}.json", month


def load_shard(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {"articles": []}
    return {"articles": []}


def main():
    # 1. Collect tickers + a price map (current price per ticker) so we can stamp
    #    each newly-archived article with the price at the moment we saw it.
    tickers, seen, price_map = [], set(), {}
    for u in REPOS:
        for row in master_rows(u):
            t = (row.get("ticker") or "").upper()
            if not t or t in seen:
                continue
            seen.add(t)
            tickers.append(t)
            # current price + the backend's own fetch time for it
            px = row.get("price") or row.get("fmpprice") or row.get("last price")
            try:
                px = float(px) if px not in (None, "") else None
            except Exception:
                px = None
            price_map[t] = {
                "price": px,
                "pricedAt": row.get("fetched_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
    print(f"Pulling news for {len(tickers)} tickers ...")

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 2. Pull fresh articles. Group new ones by their month shard.
    new_by_month = {}          # month -> { url -> article }
    have_global = set()        # de-dup within this run
    pulled = 0
    for i, t in enumerate(tickers, 1):
        try:
            raw = yf.Ticker(t).news or []
        except Exception:
            raw = []
        for n in raw[:PER_TICKER]:
            c = n.get("content") or n
            url = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or n.get("link")
            title = c.get("title") or n.get("title")
            if not url or not title or url in have_global:
                continue
            have_global.add(url)
            ts = c.get("pubDate") or n.get("providerPublishTime")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
            ts = ts or now_iso
            pm = price_map.get(t, {})
            art = {
                "id": url,
                "url": url,
                "headline": title,
                "summary": (c.get("summary") or c.get("description") or "")[:400],
                "ticker": t,
                "source": ((c.get("provider") or {}).get("displayName")
                           or n.get("publisher") or "Yahoo"),
                "datetime": ts,
                # archive provenance + price-at-first-seen (used by the frontend
                # to show "return since the article came out")
                "firstSeenAt": now_iso,
                "priceAtFirstSeen": pm.get("price"),
                "priceAtFirstSeenAt": pm.get("pricedAt"),
            }
            _, month = shard_path(ts)
            new_by_month.setdefault(month, {})[url] = art
            pulled += 1
        if i % 50 == 0:
            print(f"  ... {i}/{len(tickers)} - {pulled} candidate articles")
        time.sleep(SLEEP)

    # 3. Merge into each affected monthly shard (APPEND-ONLY: existing articles
    #    are NEVER overwritten or dropped; only genuinely-new URLs are added).
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    touched_months, added_total = [], 0
    for month, new_arts in new_by_month.items():
        path = ARCHIVE_DIR / f"{month}.json"
        shard = load_shard(path)
        existing = {a.get("url"): a for a in shard.get("articles", [])}
        added = 0
        for url, art in new_arts.items():
            if url not in existing:          # never re-add / never overwrite
                existing[url] = art
                added += 1
        if added:
            merged = sorted(existing.values(), key=lambda a: a.get("datetime") or "", reverse=True)
            path.write_text(json.dumps({
                "month": month,
                "count": len(merged),
                "updatedAt": now_iso,
                "articles": merged,
            }, separators=(",", ":")))
            touched_months.append(month)
            added_total += added
            print(f"  + {month}: +{added} new (now {len(merged)} archived)")

    # 4. Rebuild latest.json from the newest shards (lean file the app loads).
    all_months = sorted([p.stem for p in ARCHIVE_DIR.glob("*.json")], reverse=True)
    latest_items, total = [], 0
    for m in all_months:
        shard = load_shard(ARCHIVE_DIR / f"{m}.json")
        arts = shard.get("articles", [])
        total += len(arts)
        for a in arts:
            if len(latest_items) < LATEST_CAP:
                latest_items.append(a)
    LATEST.write_text(json.dumps({
        "generatedAt": now_iso,
        "count": len(latest_items),
        "items": latest_items,
    }, separators=(",", ":")))

    # 5. Index of all months for the frontend's lazy-loader.
    INDEX.write_text(json.dumps({
        "generatedAt": now_iso,
        "months": all_months,
        "totalArticles": total,
        "latestCount": len(latest_items),
    }, separators=(",", ":")))

    print(f"OK archive: +{added_total} new this run across {len(touched_months)} month(s); "
          f"{total} total archived across {len(all_months)} month(s).")
    print(f"OK latest.json: {len(latest_items)} most-recent articles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
