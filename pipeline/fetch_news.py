#!/usr/bin/env python3
"""
fetch_news.py — backend news puller for TRAPP2-ANALYTICS.

Pulls recent news per ticker via yfinance (free, keyless) for every ticker in
the three master.json files and writes one compact corpus:

    data/news/latest.json
      { generatedAt, count, items: [ {url, headline, summary, ticker,
                                      source, datetime, _fetchedByTicker} ] }

The frontend merges this corpus by URL into its feed (repo articles backfill
what the browser hasn't pulled; live API calls become a supplement, not the
workhorse). Capped per ticker + globally so the file stays lean.
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
]
OUT = Path(__file__).resolve().parent.parent / "data" / "news" / "latest.json"
PER_TICKER = 4
GLOBAL_CAP = 2500
SLEEP = 0.12


def tickers_from(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ValuatioAnalytics"}), timeout=30) as r:
            d = json.load(r)
        rows = d if isinstance(d, list) else (d.get("rows") or list(d.values()))
        if isinstance(rows, dict):
            rows = list(rows.values())
        return [(row.get("ticker") or "").upper() for row in rows if row.get("ticker")]
    except Exception as e:
        print(f"  ✗ {url.split('/')[4]}: {e}", file=sys.stderr)
        return []


def main():
    tickers, seen = [], set()
    for u in REPOS:
        for t in tickers_from(u):
            if t not in seen:
                seen.add(t)
                tickers.append(t)
    print(f"Pulling news for {len(tickers)} tickers …")

    items, have_urls = [], set()
    for i, t in enumerate(tickers, 1):
        try:
            raw = yf.Ticker(t).news or []
        except Exception:
            raw = []
        for n in raw[:PER_TICKER]:
            c = n.get("content") or n      # yfinance >=0.2.5x nests under content
            url = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or n.get("link")
            title = c.get("title") or n.get("title")
            if not url or not title or url in have_urls:
                continue
            have_urls.add(url)
            ts = c.get("pubDate") or n.get("providerPublishTime")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
            items.append({
                "url": url, "headline": title,
                "summary": (c.get("summary") or c.get("description") or "")[:400],
                "ticker": t,
                "source": ((c.get("provider") or {}).get("displayName")
                           or n.get("publisher") or "Yahoo"),
                "datetime": ts,
                "_fetchedByTicker": True,
            })
        if i % 50 == 0:
            print(f"  … {i}/{len(tickers)} · {len(items)} articles")
        time.sleep(SLEEP)
        if len(items) >= GLOBAL_CAP:
            print("  global cap reached")
            break

    items.sort(key=lambda a: a.get("datetime") or "", reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items), "items": items,
    }, separators=(",", ":")))
    print(f"✓ news/latest.json: {len(items)} articles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
