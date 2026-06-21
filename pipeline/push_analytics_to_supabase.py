#!/usr/bin/env python3
"""
Push the analytics outputs (research grades, regime, probability features,
triggers, formulas) into Supabase so the app can read them cross-device.

Stdlib only (urllib) — matches the rest of the pipeline (no pip installs needed).

The repo JSON stays the source of truth / backup; this just MIRRORS it to
Supabase. Run it as the LAST step of the analytics workflow, after the JSON
files are written.

Env / GitHub secrets required:
    SUPABASE_URL          e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  the service-role key (bypasses RLS for writes)
                          — NOT the anon key; keep it a secret, server-side only.

Optional env:
    REGIME_BASE_URL       raw base for regime files if not local
                          (default reads ./data, then falls back to TRAPP2-1 raw)

Tables written (see analytics_schema.sql):
    research_grades   (ticker pk, grades jsonb)
    regime_snapshots  (id pk, snapshot jsonb)   — 'current' + one row per history date
    analytics_kv      (key pk, value jsonb)      — probability features, triggers, formulas
"""
import json
import os
import sys
import urllib.request
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jmcczdgbnadkycnyisvh.supabase.co").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImptY2N6ZGdibmFka3ljbnlpc3ZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE5NTQ1MDEsImV4cCI6MjA5NzUzMDUwMX0.RxoxaeXaz1LXBhzl2Q0WZQaFjgkJ8v0JUiedY_Wocfs")
REGIME_BASE_URL = os.environ.get("REGIME_BASE_URL", "").rstrip("/")
TRAPP2_1_RAW = "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-1/main/data"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


import datetime


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _headers():
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        # merge-duplicates = upsert on the primary key.
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


# Primary-key column for each table — PostgREST needs ?on_conflict=<pk> so an
# upsert updates the existing row instead of erroring on a duplicate PK.
_TABLE_PK = {
    "research_grades": "ticker",
    "regime_snapshots": "id",
    "analytics_kv": "key",
}


def _upsert(table, rows, chunk=500):
    """POST rows to PostgREST in chunks, upserting on the table's PK.

    Uses ?on_conflict=<pk> + Prefer: resolution=merge-duplicates so re-running
    updates rows rather than failing. Raises on a hard failure so the caller (and
    the workflow log) shows a clear error instead of silently leaving tables empty.
    """
    if not rows:
        print(f"  [{table}] nothing to upsert (no rows built — check the source file exists)")
        return 0
    pk = _TABLE_PK.get(table, "id")
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={pk}"
    sent = 0
    errors = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        body = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
            sent += len(batch)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:500]
            print(f"  [{table}] HTTP {e.code} at row {i}: {msg}", file=sys.stderr)
            errors += 1
        except Exception as e:
            print(f"  [{table}] error at row {i}: {e}", file=sys.stderr)
            errors += 1
    status = "OK" if errors == 0 else f"{errors} batch error(s)"
    print(f"  [{table}] upserted {sent}/{len(rows)} rows  [{status}]")
    return sent


# Extra disk locations to check before falling back to the network. The
# analytics workflow checks the data books out under ./books/<REPO>, so regime
# files (authored in TRAPP2-1) are on disk there during the run.
_EXTRA_LOCAL_DIRS = [
    DATA_DIR,
    os.path.join(os.getcwd(), "books", "TRAPP2-1", "data"),
    os.path.join(os.getcwd(), "books", "TRAPP2", "data"),
]


def _load_local_or_url(filename, url):
    """Prefer a local copy on disk (pipeline output or a checked-out book);
    fall back to a raw URL only if no local copy exists."""
    for d in _EXTRA_LOCAL_DIRS:
        local = os.path.join(d, filename)
        if os.path.exists(local):
            try:
                with open(local, "r") as f:
                    return json.load(f)
            except Exception:
                pass
    if url:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  (could not fetch {url}: {e})", file=sys.stderr)
    return None


ANALYTICS_RAW = "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-ANALYTICS/main/data"


def push_research_grades():
    # Local copy (written by compute_research.py in the same workflow run) first;
    # else fetch the published file from the repo so the script also works when
    # run standalone (e.g. a manual one-off seed).
    rg = _load_local_or_url("research_grades.json", f"{ANALYTICS_RAW}/research_grades.json")
    if not rg or "byTicker" not in rg:
        print("  research_grades.json missing or has no byTicker — skipping")
        return 0
    rows = []
    for ticker, obj in rg["byTicker"].items():
        if not ticker:
            continue
        rows.append({"ticker": ticker.upper(), "grades": obj, "updated_at": _now_iso()})
    # Also store the non-equity grades if present (keyed with a suffix so they
    # don't collide with equities of the same symbol).
    for ticker, obj in (rg.get("nonEquity") or {}).items():
        if ticker:
            rows.append({"ticker": f"{ticker.upper()}", "grades": obj, "updated_at": _now_iso()})
    return _upsert("research_grades", rows)


def push_regime():
    base = REGIME_BASE_URL or TRAPP2_1_RAW
    current = _load_local_or_url("regime_current.json", f"{base}/regime_current.json")
    history = _load_local_or_url("regime_history.json", f"{base}/regime_history.json")
    rows = []
    if current:
        # one canonical 'current' row + a dated row so history is complete
        rows.append({"id": "current", "snapshot": current, "updated_at": _now_iso()})
        d = current.get("date")
        if d:
            rows.append({"id": d, "snapshot": current, "updated_at": _now_iso()})
    if isinstance(history, list):
        for snap in history:
            d = snap.get("date")
            if d:
                rows.append({"id": d, "snapshot": snap, "updated_at": _now_iso()})
    return _upsert("regime_snapshots", rows)


def push_kv():
    """Push probability features, triggers, and formulas into analytics_kv.
    Each is optional — only pushed if the file exists. Add your own files here
    as the analytics layer grows (bot_triggers.json, formulas.json, etc.)."""
    rows = []
    mapping = [
        ("probability_features.json", "probability"),
        ("bot_triggers.json", "triggers"),
        ("formulas.json", "formula"),
        ("composite_grades.json", "composite"),
    ]
    for filename, prefix in mapping:
        data = _load_local_or_url(filename, None)
        if data is None:
            continue
        if isinstance(data, dict):
            for k, v in data.items():
                rows.append({"key": f"{prefix}:{k}", "value": v if isinstance(v, dict) else {"value": v}})
        elif isinstance(data, list):
            for i, v in enumerate(data):
                key = v.get("id") or v.get("key") or str(i) if isinstance(v, dict) else str(i)
                rows.append({"key": f"{prefix}:{key}", "value": v if isinstance(v, dict) else {"value": v}})
    return _upsert("analytics_kv", rows)


def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set — skipping Supabase push (this is non-fatal).")
        return 0
    print(f"Pushing analytics to {SUPABASE_URL} …")
    total = 0
    total += push_research_grades()
    total += push_regime()
    total += push_kv()
    print(f"Done — {total} rows upserted to Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
