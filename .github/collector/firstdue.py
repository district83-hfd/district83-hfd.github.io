"""First-due lookup from the cached City HFD box polygons (boxes.json, see fetch_boxes.py). Pure python.
locate(lat, lon) -> {"box", "fd" (first-due station), "due" (D1..D5), "edge_m" (distance to the box edge)} or None."""
import json, math, os
BOXES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boxes.json")
_IDX = None; _MT = None; CELL = 0.01
def _load():
    """Grid index of the boxes; reloaded when boxes.json changes (weekly refresh)."""
    global _IDX, _MT
    mt = os.path.getmtime(BOXES)
    if _IDX is not None and _MT == mt: return _IDX
    _MT = mt
    boxes = json.load(open(BOXES)); grid = {}
    for b in boxes:
        xs = [p[0] for r in b["rings"] for p in r]; ys = [p[1] for r in b["rings"] for p in r]
        b["bb"] = (min(xs), min(ys), max(xs), max(ys))
        for i in range(int(math.floor(b["bb"][0] / CELL)), int(math.floor(b["bb"][2] / CELL)) + 1):
            for j in range(int(math.floor(b["bb"][1] / CELL)), int(math.floor(b["bb"][3] / CELL)) + 1):
                grid.setdefault((i, j), []).append(b)
    _IDX = grid; return grid
def _inside(x, y, rings):   # even-odd over all rings handles holes
    c = False
    for r in rings:
        n = len(r)
        for k in range(n):
            x1, y1 = r[k]; x2, y2 = r[k - 1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1: c = not c
    return c
def _edge_m(x, y, rings):
    kx = 111320 * math.cos(math.radians(y)); ky = 110540; best = 1e18
    for r in rings:
        for k in range(len(r)):
            ax, ay = (r[k - 1][0] - x) * kx, (r[k - 1][1] - y) * ky; bx, by = (r[k][0] - x) * kx, (r[k][1] - y) * ky
            dx, dy = bx - ax, by - ay; L = dx * dx + dy * dy
            t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
            px, py = ax + t * dx, ay + t * dy; best = min(best, px * px + py * py)
    return math.sqrt(best)
def locate(lat, lon):
    if lat is None or lon is None or not lat or not lon: return None
    x, y = lon, lat
    for b in _load().get((int(math.floor(x / CELL)), int(math.floor(y / CELL))), []):
        bb = b["bb"]
        if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and _inside(x, y, b["rings"]):
            return {"box": b["box"], "fd": b["due"][0] if b["due"] else None, "due": b["due"], "edge_m": round(_edge_m(x, y, b["rings"]))}
    return None
def district_area(stations=("10", "69", "76", "83")):
    """Rings of every box first-due to one of these stations (for drawing the outline)."""
    return [b for b in json.load(open(BOXES)) if b["due"] and b["due"][0] in stations]
def other_fd_m(lat, lon, fd, radius=150, key=None):
    """Distance (m) to the nearest box with a different first-due station, if within radius; else None.
    key: optional station -> group function (e.g. station -> district); then 'different' means a different group."""
    g = _load(); x, y = lon, lat; dx = radius / (111320 * math.cos(math.radians(y))); dy = radius / 110540
    seen, best = set(), None
    k0 = key(fd) if key else fd
    for i in range(int(math.floor((x - dx) / CELL)), int(math.floor((x + dx) / CELL)) + 1):
        for j in range(int(math.floor((y - dy) / CELL)), int(math.floor((y + dy) / CELL)) + 1):
            for b in g.get((i, j), []):
                if id(b) in seen or not b["due"] or (key(b["due"][0]) if key else b["due"][0]) == k0: continue
                seen.add(id(b)); bb = b["bb"]
                if bb[0] - dx <= x <= bb[2] + dx and bb[1] - dy <= y <= bb[3] + dy:
                    d = 0 if _inside(x, y, b["rings"]) else _edge_m(x, y, b["rings"])
                    if d <= radius and (best is None or d < best): best = round(d)
    return best

def _q(p, nd): return (round(p[0], nd), round(p[1], nd))
def outline(stations=("10", "69", "76", "83"), nd=5):
    """Outer edge of the combined first-due area of these stations, as polylines [[lat, lon], ...].
    Edges shared by two boxes in the area cancel out; what is left is the boundary."""
    cnt = {}
    for b in json.load(open(BOXES)):
        if not b["due"] or b["due"][0] not in stations: continue
        for r in b["rings"]:
            for k in range(len(r)):
                a, c = _q(r[k - 1], nd), _q(r[k], nd)
                if a == c: continue
                e = (a, c) if a < c else (c, a); cnt[e] = cnt.get(e, 0) + 1
    edges = [e for e, n in cnt.items() if n == 1]
    adj = {}
    for a, c in edges: adj.setdefault(a, []).append(c); adj.setdefault(c, []).append(a)
    used, lines = set(), []
    for a, c in edges:
        if (a, c) in used: continue
        used.add((a, c)); line = [a, c]
        while True:
            nxt = [p for p in adj[line[-1]] if ((line[-1], p) if line[-1] < p else (p, line[-1])) not in used]
            if not nxt: break
            p = nxt[0]; used.add((line[-1], p) if line[-1] < p else (p, line[-1])); line.append(p)
        lines.append([[pt[1], pt[0]] for pt in line])
    return lines
def client_boxes(bbox=(-95.80, 29.58, -95.36, 29.88), nd=4):
    """Boxes near District 83 for in-browser first-due lookup (Live tab): [[fd, [rings in lon/lat]], ...]."""
    out = []
    for b in json.load(open(BOXES)):
        if not b["due"]: continue
        xs = [p[0] for r in b["rings"] for p in r]; ys = [p[1] for r in b["rings"] for p in r]
        if max(xs) < bbox[0] or min(xs) > bbox[2] or max(ys) < bbox[1] or min(ys) > bbox[3]: continue
        out.append([b["due"][0], [[[round(p[0], nd), round(p[1], nd)] for p in r] for r in b["rings"]]])
    return out

def _chain(edges):
    adj = {}
    for a, c in edges: adj.setdefault(a, []).append(c); adj.setdefault(c, []).append(a)
    used, lines = set(), []
    key = lambda a, c: (a, c) if a < c else (c, a)
    for a, c in edges:
        if key(a, c) in used: continue
        used.add(key(a, c)); line = [a, c]
        while True:
            nxt = [p for p in adj[line[-1]] if key(line[-1], p) not in used]
            if not nxt: break
            used.add(key(line[-1], nxt[0])); line.append(nxt[0])
        lines.append(line)
    return lines
def station_areas(bbox=(-95.80, 29.58, -95.36, 29.88), nd=4):
    """First-due areas per station near District 83, dissolved to their edges, for the in-browser lookup.
    {station: {"b": [polyline,...] edges facing another station, "c": [polyline,...] edges facing no first-due box}}
    Point-in-area = ray-crossing parity over b+c edges (open polylines; no implicit closing)."""
    es = {}
    for b in json.load(open(BOXES)):
        xs = [p[0] for r in b["rings"] for p in r]; ys = [p[1] for r in b["rings"] for p in r]
        if max(xs) < bbox[0] - .05 or min(xs) > bbox[2] + .05 or max(ys) < bbox[1] - .05 or min(ys) > bbox[3] + .05: continue
        st = b["due"][0] if b["due"] else ""
        for r in b["rings"]:
            for k in range(len(r)):
                a, c = _q(r[k - 1], nd), _q(r[k], nd)
                if a == c: continue
                es.setdefault((a, c) if a < c else (c, a), []).append(st)
    per = {}
    for e, sts in es.items():
        for s in set(sts):
            if not s or sts.count(s) % 2 == 0: continue          # interior edge of s (shared by two s boxes)
            others = [o for o in sts if o != s]
            kind = "b" if any(others) else "c"
            per.setdefault(s, {"b": [], "c": []})[kind].append(e)
    out = {}
    for s, d in per.items():
        xs = [p[0] for e in d["b"] + d["c"] for p in e]; ys = [p[1] for e in d["b"] + d["c"] for p in e]
        if max(xs) < bbox[0] or min(xs) > bbox[2] or max(ys) < bbox[1] or min(ys) > bbox[3]: continue
        out[s] = {k: [[list(p) for p in l] for l in _chain(v)] for k, v in d.items() if v}
    return out

# ---- Out of first-due area rule (mirrored in dashboard_template.html: ooaDest) ----
# Fire apparatus only: engines, ladders/towers, squads, rescues. Ambulances, medics and EMS supervisors are not
# assigned by fire box first-due, so they are left out. D083 (district chief) is out of area off District 83's boxes.
OOA_PREFIXES = {"E", "L", "T", "TW", "SQ", "R", "RS", "RE", "MPE", "MPL"}
BOUNDARY_M = 150   # a call this close to another station's first-due area is too close to call: not counted
D83 = ("10", "69", "76", "83")
CLIENT_BBOX = (-95.72, 29.60, -95.42, 29.84)
def call_fd(lat, lon):
    """(first-due station or None, near_boundary 0/1) for a call location."""
    L = locate(lat, lon)
    if not L or not L["fd"]: return None, 0
    return L["fd"], int(other_fd_m(lat, lon, L["fd"], BOUNDARY_M) is not None)
def ooa_dest(unit, fd, near):
    """First-due station a District 83 unit ran into, when the run is out of its own first-due area; else None."""
    import d83rules as R
    if not fd or near: return None
    st = R.unit_station(unit); m = R.UNIT_RE.match((unit or "").strip().upper())
    if not st or not m: return None
    if m.group(1) == "D": return fd if fd not in D83 else None
    return fd if m.group(1) in OOA_PREFIXES and fd != st else None
DISTRICTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "districts.json")
_DT = [None, None]
def districts():
    """{station: district} from districts.json (fetch_districts.py); {} if missing."""
    try:
        mt = os.path.getmtime(DISTRICTS)
        if _DT[1] != mt: _DT[0] = json.load(open(DISTRICTS))["d"]; _DT[1] = mt
        return _DT[0]
    except (OSError, ValueError, KeyError): return {}
def district_of(st): return districts().get(st) if st else None
def call_district(lat, lon, fd):
    """(first-due district or None, near 0/1): near = within BOUNDARY_M of a box first-due to another district."""
    d = district_of(fd)
    if not d: return None, 0
    return d, int(other_fd_m(lat, lon, fd, BOUNDARY_M, key=district_of) is not None)
def ood_dest(unit, fdd, near):
    """District a District 83 fire unit (or D083) ran into, when the call's first-due district is not District 83."""
    import d83rules as R
    if not fdd or near or fdd == "83": return None
    m = R.UNIT_RE.match((unit or "").strip().upper())
    if not R.unit_station(unit) or not m: return None
    return fdd if (m.group(1) in OOA_PREFIXES or m.group(1) == "D") else None

def write_client_files(base):
    """fdareas.json (station areas near D83, for the Live tab) and d83area.json (District 83 outline for the map).
    Rebuilt only when boxes.json is newer."""
    src = os.path.getmtime(BOXES)
    try: src = max(src, os.path.getmtime(DISTRICTS))
    except OSError: pass
    for name, fn in (("fdareas.json", lambda: station_areas(bbox=CLIENT_BBOX)), ("d83area.json", lambda: outline(nd=5)),
                     ("neighbors.json", lambda: neighbor_districts())):
        p = os.path.join(base, name)
        if os.path.exists(p) and os.path.getmtime(p) >= src: continue
        tmp = p + ".tmp"; json.dump(fn(), open(tmp, "w"), separators=(",", ":")); os.replace(tmp, p)
def neighbor_districts(home="83", nd=5):
    """Outlines of the districts that border District 83 (map context only; no counts): [{"d", "lines", "at"}]."""
    boxes = json.load(open(BOXES)); dist = district_of; edge_d = {}
    for b in boxes:
        d = dist(b["due"][0]) if b["due"] else None
        for r in b["rings"]:
            for k in range(len(r)):
                a, c = _q(r[k - 1], nd), _q(r[k], nd)
                if a != c: edge_d.setdefault((a, c) if a < c else (c, a), set()).add(d)
    nb = sorted({d for s in edge_d.values() if home in s for d in s if d and d != home}, key=int)
    out = []
    for d in nb:
        sts = tuple(s for s, v in districts().items() if v == d)
        lines = outline(stations=sts, nd=nd)
        mine = [b for b in boxes if b["due"] and dist(b["due"][0]) == d]
        xs = [sum(p[0] for p in b["rings"][0]) / len(b["rings"][0]) for b in mine]; ys = [sum(p[1] for p in b["rings"][0]) / len(b["rings"][0]) for b in mine]
        # label at the box nearest the district's middle that is closest to D83 (so it shows in the map view)
        cx, cy = -95.575, 29.725
        j = min(range(len(mine)), key=lambda i: (xs[i] - cx) ** 2 + (ys[i] - cy) ** 2)
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        at = [round(ys[j] * .5 + my * .5, 5), round(xs[j] * .5 + mx * .5, 5)]
        out.append({"d": d, "lines": lines, "at": at})
    return out
