"""Route matching for runs.db: finds runs that follow the same route, so they can be compared.

Commands:
  python analysis.py check             how many runs have usable route data (safe to paste the output)
  python analysis.py match             group runs into routes (run after each sync)
  python analysis.py routes            list the routes found
  python analysis.py rename 3 "Park loop"   give route 3 a name of your choice
"""
import json
import math
import os
import re
import sqlite3
import sys

DB_PATH = os.environ.get("DB_PATH", "runs.db")

# What counts as "the same route". Loosen these if you see one route split in two.
MAX_LENGTH_DIFF = 0.10    # route lengths within 10% of each other
MAX_START_GAP_M = 300     # starting points within 300 m
MAX_MEAN_DEV_M = 40       # on average, the paths stay within 40 m of each other
MAX_PEAK_DEV_M = 200      # and never stray more than 200 m (GPS noise allowance)
SAMPLES, DENSE, OUTLINE_POINTS = 60, 400, 60


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(runs)")]
    if not cols:
        sys.exit("No runs table found. Run garmin_sync.py sync first.")
    for col, kind in (("route_id", "INTEGER"), ("outline_json", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")
    conn.execute("""CREATE TABLE IF NOT EXISTS routes (
        route_id INTEGER PRIMARY KEY, name TEXT NOT NULL, rep_activity_id INTEGER)""")
    return conn


# ---------- finding GPS points in whatever shape Garmin sent ----------

def decode_polyline(s, precision):
    pts, i, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while i < len(s):
        for axis in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        pts.append((lat / f, lon / f))
    return pts


def decode_encoded_samples(enc, info):
    """Garmin's {encodedSamples, minLat, maxLat, ...} block. Picks the precision that fits its bounding box."""
    for precision in (5, 6):
        try:
            pts = decode_polyline(enc, precision)
        except Exception:
            continue
        if not valid(pts):
            continue
        lo, hi = info.get("minLat"), info.get("maxLat")
        if lo is not None and hi is not None and not all(lo - 0.01 <= a <= hi + 0.01 for a, _ in pts):
            continue
        return pts
    return None


def valid(pts):
    return len(pts) >= 10 and all(-90 <= a <= 90 and -180 <= b <= 180 for a, b in pts)


def find_points(obj, key=""):
    """Look through Garmin's JSON for a list of (lat, lon) points."""
    if isinstance(obj, str):
        if "polyline" in key.lower() and len(obj) > 20:
            for precision in (5, 6):
                try:
                    pts = decode_polyline(obj, precision)
                except Exception:
                    continue
                if valid(pts):
                    return pts
        return None
    if isinstance(obj, dict):
        enc = obj.get("encodedSamples")
        if isinstance(enc, str) and len(enc) > 20:
            pts = decode_encoded_samples(enc, obj)
            if pts:
                return pts
        for k, v in obj.items():
            if "level" in k.lower():   # encodedLevels is detail info, not coordinates
                continue
            pts = find_points(v, k)
            if pts:
                return pts
    elif isinstance(obj, list) and obj:
        pts = []
        if all(isinstance(p, dict) for p in obj[:5]):
            for p in obj:
                lat, lon = p.get("lat", p.get("latitude")), p.get("lon", p.get("lng", p.get("longitude")))
                if lat is not None and lon is not None:
                    pts.append((float(lat), float(lon)))
        elif all(isinstance(p, (list, tuple)) and len(p) >= 2 and all(isinstance(x, (int, float)) for x in p[:2]) for p in obj[:5]):
            pts = [(float(p[0]), float(p[1])) for p in obj if isinstance(p, (list, tuple)) and len(p) >= 2]
        if valid(pts):
            return pts
        for item in obj:
            found = find_points(item, key)
            if found:
                return found
    return None


def points_from_json(text):
    if not text:
        return None
    try:
        return find_points(json.loads(text))
    except ValueError:
        return None


def shape(o, depth=0):
    """Describe the structure of some JSON without showing any values."""
    if isinstance(o, dict):
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:12]} if depth < 4 else "dict"
    if isinstance(o, list):
        return f"list of {len(o)}" if not o or depth >= 4 else [f"list of {len(o)}", shape(o[0], depth + 1)]
    if isinstance(o, str):
        return f"text, {len(o)} characters"
    return type(o).__name__


# ---------- geometry ----------

def project(pts, origin):
    lat0, lon0 = origin
    kx = 111320 * math.cos(math.radians(lat0))
    return [((lo - lon0) * kx, (la - lat0) * 110540) for la, lo in pts]


def resample(xy, n):
    d = [0.0]
    for a, b in zip(xy, xy[1:]):
        d.append(d[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = d[-1]
    if total == 0:
        return None
    out, j = [], 0
    for k in range(n):
        target = total * k / (n - 1)
        while j < len(d) - 2 and d[j + 1] < target:
            j += 1
        seg = (d[j + 1] - d[j]) or 1
        t = min(1, max(0, (target - d[j]) / seg))
        out.append((xy[j][0] + t * (xy[j + 1][0] - xy[j][0]), xy[j][1] + t * (xy[j + 1][1] - xy[j][1])))
    return out, total


def features(pts, origin):
    xy = project(pts, origin)
    r = resample(xy, DENSE)
    if r is None:
        return None
    dense, total = r
    samp = resample(xy, SAMPLES)[0]
    return {"length": total, "start": xy[0], "samp": samp, "dense": dense, "xy": xy}


def deviation(samp, dense):
    ds = [min(math.hypot(x - u, y - v) for u, v in dense) for x, y in samp]
    return sum(ds) / len(ds), max(ds)


def same_route(a, b):
    if abs(a["length"] - b["length"]) > MAX_LENGTH_DIFF * max(a["length"], b["length"]):
        return False
    if math.hypot(a["start"][0] - b["start"][0], a["start"][1] - b["start"][1]) > MAX_START_GAP_M:
        return False
    # Same direction? A reversed loop is a different run for comparison purposes.
    q1, q3 = SAMPLES // 4, 3 * SAMPLES // 4
    d1 = math.hypot(a["samp"][q1][0] - b["samp"][q1][0], a["samp"][q1][1] - b["samp"][q1][1])
    d3 = math.hypot(a["samp"][q1][0] - b["samp"][q3][0], a["samp"][q1][1] - b["samp"][q3][1])
    if d1 > d3 + 50:
        return False
    for first, second in ((a["samp"], b["dense"]), (b["samp"], a["dense"])):
        mean, peak = deviation(first, second)
        if mean > MAX_MEAN_DEV_M or peak > MAX_PEAK_DEV_M:
            return False
    return True


def outline(xy):
    """A small 0-100 picture of the route. It holds no real-world coordinates."""
    pts = resample(xy, OUTLINE_POINTS)[0]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    scale = 84 / span
    ox = 50 - (max(xs) + min(xs)) / 2 * scale
    oy = 50 + (max(ys) + min(ys)) / 2 * scale
    return [[round(x * scale + ox, 1), round(oy - y * scale, 1)] for x, y in pts]  # north is up


# ---------- commands ----------

def cmd_check(conn):
    rows = conn.execute("SELECT activity_id, polyline_json FROM runs").fetchall()
    have = [r for r in rows if r["polyline_json"]]
    ok = [r for r in have if points_from_json(r["polyline_json"])]
    print(f"{len(rows)} runs, {len(have)} with route data, {len(ok)} with usable GPS points")
    bad = [r for r in have if r not in ok]
    if bad:
        print("\nStructure of one route response that couldn't be read (no values shown):")
        print(json.dumps(shape(json.loads(bad[0]["polyline_json"])), indent=2))
    elif not ok:
        print("No route data at all. Indoor runs have none; otherwise run sync again.")


def cmd_match(conn):
    # Keep names you chose yourself; default "Route N" names are regenerated each time.
    old_names = {r["rep_activity_id"]: r["name"] for r in conn.execute("SELECT rep_activity_id, name FROM routes")
                 if not re.fullmatch(r"Route \d+( \([\d.]+ km\))?", r["name"])}
    rows = conn.execute("SELECT activity_id, distance_m, polyline_json FROM runs ORDER BY start_gmt").fetchall()
    origin, clusters, skipped = None, [], 0
    assignment = {}
    for row in rows:
        pts = points_from_json(row["polyline_json"])
        if not pts:
            skipped += 1
            assignment[row["activity_id"]] = (None, None)
            continue
        origin = origin or pts[0]
        f = features(pts, origin)
        if f is None:
            skipped += 1
            assignment[row["activity_id"]] = (None, None)
            continue
        out = json.dumps(outline(f["xy"]))
        for i, c in enumerate(clusters):
            if same_route(f, c["feat"]):
                c["members"] += 1
                assignment[row["activity_id"]] = (i + 1, out)
                break
        else:
            clusters.append({"rep": row["activity_id"], "feat": f, "km": row["distance_m"] / 1000, "members": 1})
            assignment[row["activity_id"]] = (len(clusters), out)
    with conn:
        conn.execute("DELETE FROM routes")
        for i, c in enumerate(clusters, 1):
            name = old_names.get(c["rep"]) or f"Route {i}"
            conn.execute("INSERT INTO routes (route_id, name, rep_activity_id) VALUES (?, ?, ?)", (i, name, c["rep"]))
        conn.executemany("UPDATE runs SET route_id = ?, outline_json = ? WHERE activity_id = ?",
                         [(rid, out, aid) for aid, (rid, out) in assignment.items()])
    repeated = sum(1 for c in clusters if c["members"] >= 2)
    print(f"{len(rows) - skipped} runs matched into {len(clusters)} routes ({repeated} run more than once).")
    if skipped:
        print(f"{skipped} runs had no usable route data (indoor runs, or the format wasn't recognised).")
    print("See them with: python analysis.py routes")


def cmd_routes(conn):
    rows = conn.execute(
        """SELECT r.route_id, r.name, COUNT(u.activity_id) n, AVG(u.distance_m) / 1000 km, MAX(u.start_local) last
           FROM routes r LEFT JOIN runs u ON u.route_id = r.route_id GROUP BY r.route_id ORDER BY n DESC""").fetchall()
    if not rows:
        print("No routes yet. Run:  python analysis.py match")
    for r in rows:
        print(f"{r['route_id']:4d}  {r['n']:3d} runs  {r['km']:5.1f} km  last {str(r['last'])[:10]}  {r['name']}")


def cmd_rename(conn, route_id, name):
    with conn:
        n = conn.execute("UPDATE routes SET name = ? WHERE route_id = ?", (name, int(route_id))).rowcount
    print("Renamed." if n else "No route with that number. See: python analysis.py routes")


def main():
    args = sys.argv[1:]
    conn = db()
    if args[:1] == ["check"]:
        cmd_check(conn)
    elif args[:1] == ["match"]:
        cmd_match(conn)
    elif args[:1] == ["routes"]:
        cmd_routes(conn)
    elif len(args) == 3 and args[0] == "rename":
        cmd_rename(conn, args[1], args[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
