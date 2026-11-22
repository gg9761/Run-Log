"""Garmin import: log in once, then copy your runs into a local SQLite database.

Commands:
  python garmin_sync.py login            sign in and save tokens (asks for an MFA code if needed)
  python garmin_sync.py sync             fetch runs, zones and route lines; if Garmin login
                                         fails, import exported files from the exports folder
  python garmin_sync.py import-files     import exported .fit/.gpx/.zip files only
  python garmin_sync.py records          fetch your Garmin personal records (fastest 1K, 5K, ...)
  python garmin_sync.py summary          check what was imported
  python garmin_sync.py inspect          print one run's raw Garmin data (to check field names)

This uses the unofficial `garminconnect` library, which can break when Garmin changes
its login. Your password is never saved; only Garmin's login tokens are, in ~/.garminconnect.
"""
import argparse
import getpass
import json
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

from garminconnect import Garmin

TOKEN_DIR = os.path.expanduser(os.environ.get("GARMINTOKENS", "~/.garminconnect"))
DB_PATH = os.environ.get("DB_PATH", "runs.db")
PAUSE_S = 1.0       # polite pause between Garmin requests
WINDOW_DAYS = 90    # fetch activities in chunks of this many days

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    activity_id      INTEGER PRIMARY KEY,
    name             TEXT,
    type_key         TEXT,
    start_local      TEXT,
    start_gmt        TEXT,
    distance_m       REAL,
    duration_s       REAL,
    moving_s         REAL,
    elevation_gain_m REAL,
    avg_hr           REAL,
    max_hr           REAL,
    start_lat        REAL,
    start_lon        REAL,
    polyline_json    TEXT,
    details_fetched  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_zones (
    activity_id INTEGER NOT NULL REFERENCES runs(activity_id),
    zone        INTEGER NOT NULL,
    seconds     REAL NOT NULL,
    low_bpm     INTEGER,
    PRIMARY KEY (activity_id, zone)
);
CREATE TABLE IF NOT EXISTS imported_files (
    file_hash TEXT PRIMARY KEY,
    label     TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------- helpers ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "source" not in [r["name"] for r in conn.execute("PRAGMA table_info(runs)")]:
        conn.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'garmin'")
    return conn


def num(x):
    return float(x) if x is not None else None


def call(fn, *args, **kwargs):
    """Call Garmin with retries. Waits longer if we look rate limited."""
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # the library raises several different error types
            if attempt == 3:
                raise
            wait = 60 if "429" in str(e) or "too many" in str(e).lower() else 5 * 2 ** attempt
            print(f"  Garmin error ({e}); retrying in {wait}s")
            time.sleep(wait)


class GarminUnavailable(Exception):
    pass


def get_client():
    client = Garmin()
    try:
        client.login(TOKEN_DIR)
    except Exception as e:
        raise GarminUnavailable(
            f"Couldn't log in to Garmin with saved tokens ({e}). Try:  python garmin_sync.py login")
    return client


def is_run(activity):
    key = ((activity.get("activityType") or {}).get("typeKey") or "").lower()
    return "running" in key or key in ("run", "virtual_run")


# ---------- commands ----------

def cmd_login(_args):
    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ")
    password = getpass.getpass("Garmin password: ")
    client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))
    client.login(TOKEN_DIR)
    print(f"Logged in as {client.get_full_name()}. Tokens saved in {TOKEN_DIR}")
    print("Treat that folder like a password and never share or commit it.")


def save_run(conn, a):
    conn.execute(
        """INSERT INTO runs (activity_id, name, type_key, start_local, start_gmt, distance_m,
                             duration_s, moving_s, elevation_gain_m, avg_hr, max_hr, start_lat, start_lon)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(activity_id) DO UPDATE SET
             name=excluded.name, type_key=excluded.type_key, distance_m=excluded.distance_m,
             duration_s=excluded.duration_s, moving_s=excluded.moving_s,
             elevation_gain_m=excluded.elevation_gain_m, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr""",
        (
            a["activityId"], a.get("activityName"), (a.get("activityType") or {}).get("typeKey"),
            a.get("startTimeLocal"), a.get("startTimeGMT"), num(a.get("distance")),
            num(a.get("duration")), num(a.get("movingDuration")), num(a.get("elevationGain")),
            num(a.get("averageHR")), num(a.get("maxHR")), num(a.get("startLatitude")),
            num(a.get("startLongitude")),
        ),
    )


def parse_zones(data):
    """Turn Garmin's time-in-zone response into (zone, seconds, low_bpm) rows."""
    rows = []
    for z in data or []:
        zone, secs = z.get("zoneNumber"), z.get("secsInZone")
        if zone is not None and secs is not None:
            rows.append((int(zone), float(secs), z.get("zoneLowBoundary")))
    return rows


def cmd_sync(args):
    """Try Garmin first. If the login or the activity fetch fails, switch to exported files."""
    try:
        sync_from_garmin(args)
    except GarminUnavailable as e:
        fall_back(args, str(e))
    except Exception as e:
        fall_back(args, f"Garmin request failed ({e})")


def fall_back(args, reason):
    import file_import
    print(f"\nGarmin sync isn't working: {reason}")
    if args.no_fallback:
        sys.exit(1)
    print(f"Switching to exported files in '{args.folder}'.")
    print("(Update the library with: pip install --upgrade garminconnect curl_cffi, then try sync again.)\n")
    os.makedirs(args.folder, exist_ok=True)
    imported, had, failed = file_import.import_folder(db(), args.folder)
    print(f"\nFiles: {imported} new runs imported, {had} already in the database, {failed} failed.")
    if not (imported or had or failed):
        print("\nNo new files found.\n" + file_import.EXPORT_HELP)


def cmd_import_files(args):
    import file_import
    os.makedirs(args.folder, exist_ok=True)
    imported, had, failed = file_import.import_folder(db(), args.folder)
    print(f"Files: {imported} new runs imported, {had} already in the database, {failed} failed.")
    if not (imported or had or failed):
        print("No new files found.\n" + file_import.EXPORT_HELP)


def fetch_records(client, conn):
    """Save Garmin's own personal records (fastest 1K, 5K and so on). Failure here is not fatal."""
    try:
        data = call(client.get_personal_record)
    except Exception as e:
        print(f"  Couldn't fetch personal records ({e}); the dashboard will estimate them instead.")
        return
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('personal_records', ?)", (json.dumps(data),))
    print("  Saved personal records.")


def sync_from_garmin(args):
    client = get_client()
    conn = db()
    today = date.today()
    runs_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    if args.since:
        start = date.fromisoformat(args.since)
    else:
        start = today - timedelta(days=args.days)

    # Phase 1: activity summaries, in date windows
    print(f"Fetching activities from {start} to {today}...")
    found = 0
    window_start = start
    while window_start <= today:
        window_end = min(window_start + timedelta(days=WINDOW_DAYS - 1), today)
        activities = call(client.get_activities_by_date, window_start.isoformat(), window_end.isoformat())
        runs = [a for a in activities if is_run(a)]
        for a in runs:
            save_run(conn, a)
        conn.commit()
        found += len(runs)
        print(f"  {window_start} to {window_end}: {len(runs)} runs")
        window_start = window_end + timedelta(days=1)
        time.sleep(PAUSE_S)
    print(f"Saved {found} runs.")

    # Phase 2: zones and route line for runs we haven't done yet (safe to stop and restart)
    limit = getattr(args, "max_details", None)   # the background sync caps this per round
    todo = conn.execute(
        "SELECT activity_id, start_local FROM runs WHERE details_fetched = 0 ORDER BY start_local DESC"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    print(f"Fetching heart rate zones and routes for {len(todo)} runs...")
    failed = 0
    for i, row in enumerate(todo, 1):
        aid = row["activity_id"]
        try:
            zones = parse_zones(call(client.get_activity_hr_in_timezones, aid))
            time.sleep(PAUSE_S)
            try:
                polyline = json.dumps(call(client.get_activity_polyline, aid))
            except Exception:
                polyline = None  # indoor runs have no route
            time.sleep(PAUSE_S)
            with conn:
                conn.execute("DELETE FROM run_zones WHERE activity_id = ?", (aid,))
                conn.executemany(
                    "INSERT INTO run_zones (activity_id, zone, seconds, low_bpm) VALUES (?, ?, ?, ?)",
                    [(aid, z, s, low) for z, s, low in zones],
                )
                conn.execute(
                    "UPDATE runs SET polyline_json = ?, details_fetched = 1 WHERE activity_id = ?",
                    (polyline, aid),
                )
            print(f"  [{i}/{len(todo)}] {row['start_local']}: {len(zones)} zones")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(todo)}] {row['start_local']}: skipped ({e})")
    print(f"Done. {failed} runs failed and will be retried next time." if failed else "Done.")

    new_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] - runs_before
    has_records = conn.execute("SELECT 1 FROM meta WHERE key = 'personal_records'").fetchone()
    if new_runs > 0 or not has_records:
        fetch_records(client, conn)


def auto_sync(days=14, folder="exports", max_details=30):
    """Used by the dashboard server. Never exits the program. Returns {"ok", "message", "new_runs"}."""
    conn = db()
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    args = argparse.Namespace(since=None, days=days, folder=folder, no_fallback=True, max_details=max_details)
    try:
        sync_from_garmin(args)
        new = db().execute("SELECT COUNT(*) FROM runs").fetchone()[0] - before
        return {"ok": True, "new_runs": new,
                "message": f"Synced from Garmin. {new} new run{'s' if new != 1 else ''}." if new else "Synced from Garmin. No new runs."}
    except Exception as e:
        reason = str(e)
    # Garmin didn't work: try exported files, if the backup importer is installed and there are any
    imported = 0
    try:
        import file_import
        os.makedirs(folder, exist_ok=True)
        imported, _had, _failed = file_import.import_folder(db(), folder)
    except ImportError:
        pass   # file_import.py isn't in this folder; the backup is optional
    except Exception as e2:
        reason += f" (file import also failed: {e2})"
    hint = "" if "garmin_sync.py login" in reason else " If your Garmin login has expired, run: python garmin_sync.py login"
    if imported:
        return {"ok": False, "new_runs": imported,
                "message": f"Garmin sync isn't working, so {imported} runs were imported from exported files.{hint}"}
    return {"ok": False, "new_runs": 0, "message": f"Garmin sync isn't working ({reason}).{hint}"}


def cmd_records(_args):
    """Fetch Garmin's personal records now and show what the dashboard will display."""
    client = get_client()
    conn = db()
    fetch_records(client, conn)
    row = conn.execute("SELECT value FROM meta WHERE key = 'personal_records'").fetchone()
    if not row:
        sys.exit("No records were saved (see the message above).")
    import server  # reuses the same reader the dashboard uses
    found = server.parse_records(row["value"])
    if not found:
        print("Records were saved, but none matched 1K, 5K, 10K, half marathon or marathon.")
        print("Run:  python garmin_sync.py inspect   and send the PERSONAL RECORDS section.")
        return
    order = ["1K", "5K", "10K", "Half marathon", "Marathon"]
    for k in order:
        if k in found:
            sec = found[k]["sec"]
            t = f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{sec % 60:04.1f}" if sec >= 3600 else f"{int(sec // 60)}:{sec % 60:04.1f}"
            print(f"  {k:14s} {t}  {found[k]['date'] or ''}")
    print("Reload the dashboard to see them.")


def cmd_summary(_args):
    conn = db()
    total = conn.execute("SELECT COUNT(*) c, SUM(distance_m) d, MIN(start_local) a, MAX(start_local) b FROM runs").fetchone()
    if not total["c"]:
        print("No runs yet. Run:  python garmin_sync.py sync")
        return
    print(f"{total['c']} runs, {total['d'] / 1000:.0f} km, {total['a'][:10]} to {total['b'][:10]}")
    with_zones = conn.execute("SELECT COUNT(DISTINCT activity_id) FROM run_zones").fetchone()[0]
    print(f"{with_zones} runs have heart rate zone data")

    km = {r["wk"]: (r["n"], r["km"]) for r in conn.execute(
        """SELECT strftime('%Y-%W', start_local) wk, COUNT(*) n, SUM(distance_m) / 1000 km
           FROM runs GROUP BY wk ORDER BY wk DESC LIMIT 6""")}
    zmin = {}
    for r in conn.execute(
        """SELECT strftime('%Y-%W', r.start_local) wk, z.zone, SUM(z.seconds) / 60 mins
           FROM run_zones z JOIN runs r USING (activity_id) GROUP BY wk, z.zone"""):
        zmin.setdefault(r["wk"], {})[r["zone"]] = r["mins"]
    print("\nWeek     runs     km   minutes in zones 1 to 5")
    for wk, (n, d) in km.items():
        z = " ".join(f"{zmin.get(wk, {}).get(k, 0):5.0f}" for k in range(1, 6))
        print(f"{wk}  {n:4d} {d:6.1f}   {z}")


def cmd_inspect(_args):
    """Print raw Garmin responses for the latest run so we can check field names."""
    client = get_client()
    activities = call(client.get_activities_by_date, (date.today() - timedelta(days=60)).isoformat(), date.today().isoformat())
    runs = [a for a in activities if is_run(a)]
    if not runs:
        sys.exit("No runs found in the last 60 days.")
    a = runs[-1]
    print("SUMMARY (selected fields):")
    keys = ["activityId", "activityName", "startTimeLocal", "activityType", "distance", "duration",
            "movingDuration", "elevationGain", "averageHR", "maxHR", "startLatitude", "startLongitude"]
    print(json.dumps({k: a.get(k) for k in keys}, indent=2))
    print("\nHR ZONES:")
    print(json.dumps(call(client.get_activity_hr_in_timezones, a["activityId"]), indent=2))
    print("\nPOLYLINE (first 600 characters):")
    print(json.dumps(call(client.get_activity_polyline, a["activityId"]))[:600])
    print("\nPERSONAL RECORDS (first 1500 characters):")
    try:
        print(json.dumps(call(client.get_personal_record))[:1500])
    except Exception as e:
        print(f"Couldn't fetch: {e}")


def main():
    p = argparse.ArgumentParser(description="Import Garmin runs into a local database.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    s = sub.add_parser("sync")
    s.add_argument("--since", help="start date, YYYY-MM-DD (default: last --days days)")
    s.add_argument("--days", type=int, default=365, help="how many days back to fetch (default 365)")
    s.add_argument("--folder", default="exports", help="folder of exported files used as the backup (default: exports)")
    s.add_argument("--no-fallback", action="store_true", help="stop with an error instead of using exported files")
    s.set_defaults(fn=cmd_sync)
    f = sub.add_parser("import-files")
    f.add_argument("--folder", default="exports", help="folder of exported .fit/.gpx/.zip files (default: exports)")
    f.set_defaults(fn=cmd_import_files)
    sub.add_parser("records").set_defaults(fn=cmd_records)
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    sub.add_parser("inspect").set_defaults(fn=cmd_inspect)
    args = p.parse_args()
    try:
        args.fn(args)
    except GarminUnavailable as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
