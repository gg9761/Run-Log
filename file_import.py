"""Import runs from exported Garmin files (.fit, .gpx, or .zip files containing them) into runs.db.

This is the backup route for when the unofficial Garmin login stops working.
It needs no Garmin login at all.
"""
import hashlib
import io
import json
import math
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ZONES_FILE = "zones.json"   # {"zone_lower_bpm": [114, 133, 152, 171, 190]}
MAX_GAP_S = 30              # gaps longer than this (pauses) don't count as moving or zone time
MAX_ROUTE_POINTS = 300

EXPORT_HELP = """To export runs from Garmin Connect (in a web browser):
  - Open a run, then use the gear/menu icon on the activity page.
  - Choose "Export Original" (gives a .zip with a .fit file) or "Export to GPX".
  - Save the files into the exports folder, then run the sync again.
For your whole history, Garmin's account Data Management page has an "Export Your Data"
request that emails you a download link. Unzip it into the exports folder.
FIT and GPX files both work. FIT files are more accurate."""


# ---------- parsing ----------

def local(tag):
    return tag.rsplit("}", 1)[-1]


def to_utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_gpx(data):
    root = ET.fromstring(data)
    name = type_ = None
    for el in root.iter():
        n = local(el.tag)
        if n == "name" and name is None and el.text:
            name = el.text.strip()
        elif n == "type" and type_ is None and el.text:
            type_ = el.text.strip().lower()
    if type_ and "run" not in type_:
        return None, {}, name  # not a run
    samples = []
    for pt in root.iter():
        if local(pt.tag) != "trkpt":
            continue
        s = {"t": None, "lat": float(pt.get("lat")), "lon": float(pt.get("lon")), "hr": None, "ele": None}
        for c in pt.iter():
            cn = local(c.tag)
            if not c.text:
                continue
            if cn == "time":
                s["t"] = to_utc(datetime.fromisoformat(c.text.strip().replace("Z", "+00:00")))
            elif cn == "ele":
                s["ele"] = float(c.text)
            elif cn in ("hr", "heartrate"):
                s["hr"] = int(float(c.text))
        samples.append(s)
    return samples, {}, name


def parse_fit(data):
    try:
        import fitdecode
    except ImportError:
        raise RuntimeError("fitdecode isn't installed. Run: pip install fitdecode")

    def val(frame, field):
        try:
            return frame.get_value(field)
        except KeyError:
            return None

    samples, session = [], {}
    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record":
                lat, lon, t = val(frame, "position_lat"), val(frame, "position_long"), val(frame, "timestamp")
                samples.append({
                    "t": to_utc(t) if t else None,
                    "lat": lat * 180 / 2**31 if lat is not None else None,   # FIT stores semicircles
                    "lon": lon * 180 / 2**31 if lon is not None else None,
                    "hr": val(frame, "heart_rate"),
                    "ele": val(frame, "enhanced_altitude") if val(frame, "enhanced_altitude") is not None else val(frame, "altitude"),
                })
            elif frame.name == "session":
                session = {k: val(frame, k) for k in (
                    "sport", "start_time", "total_distance", "total_timer_time",
                    "total_elapsed_time", "total_ascent", "avg_heart_rate", "max_heart_rate")}
    sport = str(session.get("sport") or "").lower()
    if sport and "run" not in sport:
        return None, {}, None
    return samples, session, None


# ---------- calculations ----------

def haversine_m(a, b):
    r = 6371000
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def zone_seconds(samples, bounds):
    """Seconds in zones 1 to 5, given the lower heart rate bound of each zone."""
    secs = [0.0] * 5
    for a, b in zip(samples, samples[1:]):
        dt = (b["t"] - a["t"]).total_seconds()
        if a["hr"] is None or not 0 < dt <= MAX_GAP_S:
            continue
        zone = sum(1 for low in bounds if a["hr"] >= low)  # 0 means below zone 1
        if zone:
            secs[zone - 1] += dt
    return secs


def build_run(samples, session, name, label, bounds):
    samples = sorted((s for s in samples if s["t"]), key=lambda s: s["t"])
    if len(samples) < 2:
        return None
    start = to_utc(session["start_time"]) if session.get("start_time") else samples[0]["t"]
    pos = [s for s in samples if s["lat"] is not None and s["lon"] is not None]
    gaps = [(b["t"] - a["t"]).total_seconds() for a, b in zip(samples, samples[1:])]

    distance = session.get("total_distance")
    if distance is None:
        distance = sum(haversine_m(a, b) for a, b in zip(pos, pos[1:]))
    elapsed = session.get("total_elapsed_time") or (samples[-1]["t"] - samples[0]["t"]).total_seconds()
    moving = session.get("total_timer_time") or sum(g for g in gaps if 0 < g <= MAX_GAP_S)
    gain = session.get("total_ascent")
    if gain is None:
        eles = [s["ele"] for s in samples if s["ele"] is not None]
        gain = sum(max(0, b - a) for a, b in zip(eles, eles[1:])) if eles else None  # rough for GPX
    hrs = [s["hr"] for s in samples if s["hr"] is not None]
    avg_hr = session.get("avg_heart_rate") or (sum(hrs) / len(hrs) if hrs else None)
    max_hr = session.get("max_heart_rate") or (max(hrs) if hrs else None)

    match = re.search(r"(\d{6,})", label)
    activity_id = int(match.group(1)) if match else -int(start.timestamp())  # negative = not a Garmin id

    step = max(1, len(pos) // MAX_ROUTE_POINTS)
    route = [[round(p["lat"], 6), round(p["lon"], 6)] for p in pos[::step]]
    return {
        "activity_id": activity_id,
        "name": name or f"Run {start:%Y-%m-%d}",
        "start_gmt": f"{start:%Y-%m-%d %H:%M:%S}",
        "start_local": f"{start:%Y-%m-%d %H:%M:%S}",  # files only carry UTC, so local time is approximate
        "distance_m": float(distance), "duration_s": float(elapsed), "moving_s": float(moving),
        "elevation_gain_m": float(gain) if gain is not None else None,
        "avg_hr": float(avg_hr) if avg_hr is not None else None,
        "max_hr": float(max_hr) if max_hr is not None else None,
        "start_lat": pos[0]["lat"] if pos else None, "start_lon": pos[0]["lon"] if pos else None,
        "route_json": json.dumps({"format": "latlon_list", "points": route}) if route else None,
        "zones": zone_seconds(samples, bounds) if bounds else None,
    }


def load_zone_bounds(conn):
    """Zone lower bounds from zones.json, else from zones seen in earlier Garmin imports."""
    bounds = None
    if os.path.exists(ZONES_FILE):
        with open(ZONES_FILE) as f:
            bounds = json.load(f).get("zone_lower_bpm")
    else:
        row = conn.execute(
            """SELECT activity_id FROM run_zones WHERE low_bpm IS NOT NULL
               GROUP BY activity_id HAVING COUNT(*) = 5 ORDER BY activity_id DESC LIMIT 1""").fetchone()
        if row:
            bounds = [r["low_bpm"] for r in conn.execute(
                "SELECT low_bpm FROM run_zones WHERE activity_id = ? ORDER BY zone", (row["activity_id"],))]
    if bounds and len(bounds) == 5 and all(isinstance(b, (int, float)) for b in bounds) and bounds == sorted(bounds):
        return bounds
    return None


def save_run(conn, run):
    """Insert a run. Returns False if we already have a run starting within 90 seconds."""
    near = conn.execute(
        "SELECT 1 FROM runs WHERE ABS(strftime('%s', start_gmt) - strftime('%s', ?)) < 90",
        (run["start_gmt"],)).fetchone()
    if near or conn.execute("SELECT 1 FROM runs WHERE activity_id = ?", (run["activity_id"],)).fetchone():
        return False
    with conn:
        conn.execute(
            """INSERT INTO runs (activity_id, name, type_key, start_local, start_gmt, distance_m, duration_s,
                                 moving_s, elevation_gain_m, avg_hr, max_hr, start_lat, start_lon,
                                 polyline_json, details_fetched, source)
               VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'file')""",
            (run["activity_id"], run["name"], run["start_local"], run["start_gmt"], run["distance_m"],
             run["duration_s"], run["moving_s"], run["elevation_gain_m"], run["avg_hr"], run["max_hr"],
             run["start_lat"], run["start_lon"], run["route_json"]))
        if run["zones"]:
            conn.executemany(
                "INSERT INTO run_zones (activity_id, zone, seconds, low_bpm) VALUES (?, ?, ?, NULL)",
                [(run["activity_id"], i + 1, s) for i, s in enumerate(run["zones"])])
    return True


# ---------- folder import ----------

def iter_files(folder):
    """Yield (label, bytes) for each .fit/.gpx file, including ones inside .zip files."""
    for root, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            path, ext = os.path.join(root, fn), os.path.splitext(fn)[1].lower()
            if ext in (".fit", ".gpx"):
                with open(path, "rb") as f:
                    yield fn, f.read()
            elif ext == ".zip":
                try:
                    with zipfile.ZipFile(path) as z:
                        for member in z.namelist():
                            if member.lower().endswith((".fit", ".gpx")):
                                yield os.path.basename(member), z.read(member)
                except zipfile.BadZipFile:
                    print(f"  {fn}: not a valid zip file, skipped")


def import_folder(conn, folder):
    """Import new files from the folder. Returns (imported, already_had, failed)."""
    bounds = load_zone_bounds(conn)
    if bounds is None:
        print("  No heart rate zone settings found, so zone times will be left out for now.\n"
              f"  To include them, copy zones.example.json to {ZONES_FILE} and enter your zone lower limits.")
    imported = had = failed = 0
    for label, data in iter_files(folder):
        digest = hashlib.sha1(data).hexdigest()
        if conn.execute("SELECT 1 FROM imported_files WHERE file_hash = ?", (digest,)).fetchone():
            continue
        try:
            parser = parse_fit if label.lower().endswith(".fit") else parse_gpx
            samples, session, name = parser(data)
            run = build_run(samples, session, name, label, bounds) if samples else None
            if run is None:
                print(f"  {label}: not a run or no usable data, skipped")
            elif save_run(conn, run):
                imported += 1
                print(f"  {label}: imported ({run['distance_m'] / 1000:.1f} km, {run['start_gmt'][:10]})")
            else:
                had += 1
            with conn:
                conn.execute("INSERT INTO imported_files (file_hash, label) VALUES (?, ?)", (digest, label))
        except Exception as e:
            failed += 1
            print(f"  {label}: failed ({e})")
    return imported, had, failed
