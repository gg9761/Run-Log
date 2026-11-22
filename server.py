"""Local server: shows your Garmin data in the Run Log dashboard and keeps it up to date.

  python server.py                    open http://127.0.0.1:5000
  python server.py --sync-every 720   check Garmin every 12 hours instead of every 30 minutes
  python server.py --sync-every 0     never sync automatically (the Sync now button still works)

It only listens on your own computer and has no login, so don't expose it to the internet.
It reads runs.db (created by garmin_sync.py) and groups runs into routes after each sync.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, request, send_from_directory

import analysis
import garmin_sync

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", "runs.db")
MAP_POINTS = 150

app = Flask(__name__)

# ---------- background sync ----------

STATE = {"running": False, "finished_at": None, "ok": None, "message": "Waiting for the first sync.",
         "new_runs": 0, "interval_min": 0}
_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_sync_once():
    """Sync from Garmin, then regroup routes. Returns False if a sync is already running."""
    if not _lock.acquire(blocking=False):
        return False
    STATE["running"] = True
    try:
        result = garmin_sync.auto_sync(days=14)
        try:
            analysis.cmd_match(analysis.db())
        except Exception as e:
            print(f"Route matching skipped: {e}")
        STATE.update(ok=result["ok"], message=result["message"], new_runs=result["new_runs"])
    except Exception as e:
        STATE.update(ok=False, message=f"Sync failed: {e}", new_runs=0)
    finally:
        STATE["finished_at"] = now_iso()
        STATE["running"] = False
        _lock.release()
    print(f"[sync] {STATE['message']}")
    return True


def sync_loop(interval_min):
    while True:
        run_sync_once()
        time.sleep(interval_min * 60)


def start_thread(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


@app.route("/api/status")
def api_status():
    resp = jsonify(STATE)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/sync", methods=["POST"])
def api_sync():
    # Only accept requests that come from this page on this computer.
    host = request.host.split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        abort(403)
    if STATE["running"]:
        return jsonify(started=False, running=True)
    start_thread(run_sync_once)
    return jsonify(started=True, running=True)


# ---------- data ----------

def columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def route_points(conn, rep_id):
    """Real GPS points of a route's first run, thinned for the map."""
    row = conn.execute("SELECT polyline_json FROM runs WHERE activity_id = ?", (rep_id,)).fetchone()
    pts = analysis.points_from_json(row["polyline_json"]) if row else None
    if not pts:
        return None
    step = max(1, len(pts) // MAP_POINTS)
    thin = pts[::step]
    if thin[-1] != pts[-1]:
        thin.append(pts[-1])
    return [[round(a, 5), round(b, 5)] for a, b in thin]


# Garmin's personal-record type ids and the range of seconds each one can plausibly hold.
# A record outside its range is ignored, so a wrong id can't show a wrong time.
RECORD_TYPES = {1: ("1K", 120, 900), 3: ("5K", 600, 4500), 4: ("10K", 1200, 9000),
                5: ("Half marathon", 2700, 18000), 6: ("Marathon", 5400, 36000)}


def record_date(o):
    for k, v in o.items():
        kl = k.lower()
        if "start" in kl and ("gmt" in kl or "local" in kl or "time" in kl):
            if isinstance(v, (int, float)) and v > 1e11:
                return datetime.fromtimestamp(v / 1000, timezone.utc).date().isoformat()
            if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
                return v[:10]
    return None


def parse_records(raw):
    found = {}

    def walk(o):
        if isinstance(o, dict):
            spec = RECORD_TYPES.get(o.get("typeId"))
            val = o.get("value")
            if spec and isinstance(val, (int, float)) and spec[1] <= val <= spec[2]:
                best = found.get(spec[0])
                if best is None or val < best["sec"]:
                    found[spec[0]] = {"sec": float(val), "date": record_date(o)}
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    try:
        walk(json.loads(raw))
    except (TypeError, ValueError):
        pass
    return found


def load():
    conn = connect()
    cols = columns(conn, "runs")
    if not cols:
        return {"runs": [], "routes": [], "records": {}}

    def col(name, default="NULL"):
        return name if name in cols else f"{default} AS {name}"

    runs = conn.execute(
        f"""SELECT activity_id, name, start_local, distance_m, duration_s, moving_s, elevation_gain_m,
                   avg_hr, max_hr, {col('source', "'garmin'")}, {col('route_id')}, {col('outline_json')}
            FROM runs WHERE distance_m > 0 ORDER BY start_local""").fetchall()

    zones = {}
    if columns(conn, "run_zones"):
        for z in conn.execute("SELECT activity_id, zone, seconds FROM run_zones"):
            zones.setdefault(z["activity_id"], [0.0] * 5)[z["zone"] - 1] = z["seconds"]

    out = []
    for r in runs:
        sec = r["moving_s"] or r["duration_s"]
        if not sec:
            continue
        out.append({
            "id": r["activity_id"], "name": r["name"], "date": r["start_local"],
            "km": r["distance_m"] / 1000, "sec": sec, "hr": r["avg_hr"], "elev": r["elevation_gain_m"],
            "src": "Garmin" if r["source"] == "garmin" else "File",
            "route": r["route_id"], "z": zones.get(r["activity_id"]),
            "outline": r["outline_json"] and json.loads(r["outline_json"]),
        })

    routes = []
    if columns(conn, "routes"):
        for r in conn.execute(
            """SELECT r.route_id, r.name, r.rep_activity_id, u.outline_json, u.distance_m FROM routes r
               LEFT JOIN runs u ON u.activity_id = r.rep_activity_id"""):
            routes.append({
                "id": r["route_id"], "name": r["name"], "km": (r["distance_m"] or 0) / 1000,
                "outline": r["outline_json"] and json.loads(r["outline_json"]),
                "points": route_points(conn, r["rep_activity_id"]),
            })

    records = {}
    if columns(conn, "meta"):
        row = conn.execute("SELECT value FROM meta WHERE key = 'personal_records'").fetchone()
        if row:
            records = parse_records(row["value"])
    return {"runs": out, "routes": routes, "records": records}


@app.route("/api/data")
def api_data():
    resp = jsonify(load())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    resp = send_from_directory(BASE, "dashboard.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--sync-every", type=int, default=30, metavar="MINUTES",
                   help="how often to check Garmin for new runs (default 30, 0 turns it off)")
    p.add_argument("--no-match", action="store_true", help="skip grouping runs into routes at startup")
    args = p.parse_args()
    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found. Run this from the folder where you ran garmin_sync.py, "
                 "or run:  python garmin_sync.py sync")
    if not args.no_match:
        try:
            analysis.cmd_match(analysis.db())
        except Exception as e:  # route matching is optional; the dashboard still works without it
            print(f"Route matching skipped: {e}")
    STATE["interval_min"] = args.sync_every
    if args.sync_every > 0:
        start_thread(sync_loop, args.sync_every)
        print(f"Checking Garmin for new runs now and then every {args.sync_every} minutes.")
    else:
        STATE["message"] = "Automatic sync is off."
    print(f"Open http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
