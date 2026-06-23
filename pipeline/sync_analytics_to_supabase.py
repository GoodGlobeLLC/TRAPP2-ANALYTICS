#!/usr/bin/env python3
"""
sync_analytics_to_supabase.py — push research_grades + regime + kv → Supabase.

Server-side (GitHub Actions). SERVICE ROLE key. Because this runs server-side
with the service key, the analytics_anon_write.sql policies are NOT needed for
THIS path (they're only needed when the browser writes with the anon key).

Env (repo secrets):
  SUPABASE_URL          the ANALYTICS project URL
  SUPABASE_SERVICE_KEY  the service_role key
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def _req(method, path, body=None, headers=None):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if headers: h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def _upsert(table, rows, conflict):
    sent = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i+200]
        st, body = _req("POST", f"/rest/v1/{table}?on_conflict={conflict}", chunk,
                        {"Prefer": "resolution=merge-duplicates,return=minimal"})
        if st in (200, 201, 204): sent += len(chunk)
        else: print(f"  {table} chunk {i} → HTTP {st}: {body[:160]}")
    return sent

def main():
    if not URL or not KEY:
        print("✗ SUPABASE_URL / SUPABASE_SERVICE_KEY not set"); return 1
    now = datetime.now(timezone.utc).isoformat()
    total = 0

    # research_grades.json → research_grades (ticker pk, grades jsonb)
    if os.path.exists("data/research_grades.json"):
        rg = json.load(open("data/research_grades.json"))
        bt = rg.get("byTicker", rg) if isinstance(rg, dict) else {}
        rows = [{"ticker": t, "grades": g, "updated_at": now} for t, g in bt.items() if t]
        total += _upsert("research_grades", rows, "ticker")
        print(f"  research_grades: {len(rows)} rows")

    # regime_history.json / regime_current.json → regime_snapshots (id pk, snapshot jsonb)
    reg_rows = []
    if os.path.exists("data/regime_current.json"):
        cur = json.load(open("data/regime_current.json"))
        reg_rows.append({"id": "current", "snapshot": cur, "updated_at": now})
        if cur.get("date"): reg_rows.append({"id": cur["date"], "snapshot": cur, "updated_at": now})
    if os.path.exists("data/regime_history.json"):
        hist = json.load(open("data/regime_history.json"))
        arr = hist if isinstance(hist, list) else hist.get("history", [])
        for snap in arr:
            if snap.get("date"): reg_rows.append({"id": snap["date"], "snapshot": snap, "updated_at": now})
    # de-dup by id (last wins)
    seen = {}
    for r in reg_rows: seen[r["id"]] = r
    if seen:
        total += _upsert("regime_snapshots", list(seen.values()), "id")
        print(f"  regime_snapshots: {len(seen)} rows")

    print(f"✓ analytics → Supabase: {total} row(s) upserted")
    return 0

if __name__ == "__main__":
    sys.exit(main())
