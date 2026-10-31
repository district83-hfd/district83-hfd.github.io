#!/usr/bin/env python3
"""Build d83_xings.json: where the City's thoroughfare centerlines cross each other around District 83 (used by extras.py to place
Houston TranStar incidents, which give only 'road At cross street' text). Source: City of Houston GIS HoustonMap/Transportation layer 3
(Major Thoroughfare Plan centerlines, public). Only crossings inside or within ~1.2 km of D83 are kept.
Run by hand when roads change (rarely):  python3 build_xings.py [mtfp_raw.json]   (needs boxes.json + districts.json next to it)."""
import json, math, os, sys, urllib.parse, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import extras as X, firstdue as FD
URL = "https://mycity2.houstontx.gov/gisweb02/rest/services/HoustonMap/Transportation/MapServer/3/query"
BBOX = "-95.68,29.62,-95.46,29.83"
SNAP_M = 20          # a line that ends within 20 m of another line counts as meeting it (T intersections)
KEEP_M = 1200        # keep crossings inside D83 or this close to it
KX, KY = 111320 * math.cos(math.radians(29.72)), 110540
def fetch():
    feats, off = [], 0
    while True:
        q = urllib.parse.urlencode(dict(where="1=1", geometry=BBOX, geometryType="esriGeometryEnvelope", inSR=4326, outSR=4326, spatialRel="esriSpatialRelIntersects",
                                        returnGeometry="true", outFields="NAME,FULL_NAME,ST_TYPE,ST_STATUS", resultOffset=off, resultRecordCount=1000, geometryPrecision=5, f="json"))
        d = json.load(urllib.request.urlopen(URL + "?" + q, timeout=90)); fs = d.get("features", []); feats += fs; off += len(fs)
        if not fs or not d.get("exceededTransferLimit"): return feats
def segs(feats):
    out = []
    for f in feats:
        a = f["attributes"]; k = X.canon(a.get("FULL_NAME") or a.get("NAME"), a.get("ST_TYPE"))
        if not k: continue
        for p in f["geometry"]["paths"]:
            for i in range(1, len(p)): out.append((k, (p[i - 1][0], p[i - 1][1]), (p[i][0], p[i][1])))
    return out
def xy(p): return (p[0] * KX, p[1] * KY)
def seg_x(a, b, c, d):
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = map(xy, (a, b, c, d))
    r = (bx - ax, by - ay); s = (dx - cx, dy - cy); den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-9: return None
    t = ((cx - ax) * s[1] - (cy - ay) * s[0]) / den; u = ((cx - ax) * r[1] - (cy - ay) * r[0]) / den
    if 0 <= t <= 1 and 0 <= u <= 1: return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
def pt_seg_m(p, c, d):
    (px, py), (cx, cy), (dx, dy) = xy(p), xy(c), xy(d); L = (dx - cx) ** 2 + (dy - cy) ** 2
    t = 0 if L == 0 else max(0, min(1, ((px - cx) * (dx - cx) + (py - cy) * (dy - cy)) / L))
    return math.hypot(px - (cx + t * (dx - cx)), py - (cy + t * (dy - cy))), (c[0] + t * (d[0] - c[0]), c[1] + t * (d[1] - c[1]))
def near_d83(lat, lon):
    if X.inside_d83(lat, lon, 0): return True
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (.7, .7), (-.7, .7), (.7, -.7), (-.7, -.7)):
        if X.inside_d83(lat + dy * KEEP_M / KY, lon + dx * KEEP_M / KX, 0): return True
    return False
def build(feats):
    S = segs(feats); grid = {}; C = 0.004
    for i, (k, a, b) in enumerate(S):
        for gx in range(int(min(a[0], b[0]) / C) - 1, int(max(a[0], b[0]) / C) + 2):
            for gy in range(int(min(a[1], b[1]) / C) - 1, int(max(a[1], b[1]) / C) + 2): grid.setdefault((gx, gy), []).append(i)
    pts = {}
    for i, (k, a, b) in enumerate(S):
        cand = set()
        for gx in range(int(min(a[0], b[0]) / C) - 1, int(max(a[0], b[0]) / C) + 2):
            for gy in range(int(min(a[1], b[1]) / C) - 1, int(max(a[1], b[1]) / C) + 2): cand.update(grid.get((gx, gy), ()))
        for j in cand:
            if j <= i: continue
            k2, c, d = S[j]
            if k2 == k: continue
            p = seg_x(a, b, c, d)
            if not p:
                for e in (a, b):
                    m, q = pt_seg_m(e, c, d)
                    if m <= SNAP_M: p = q; break
                if not p:
                    for e in (c, d):
                        m, q = pt_seg_m(e, a, b)
                        if m <= SNAP_M: p = q; break
            if p: pts.setdefault(X.xkey(k, k2), []).append((round(p[1], 5), round(p[0], 5)))   # (lat, lon)
    out = {}
    for key, ps in pts.items():
        uniq = []
        for p in ps:
            if all(math.hypot((p[0] - q[0]) * KY, (p[1] - q[1]) * KX) > 60 for q in uniq): uniq.append(p)
        keep = [list(p) for p in uniq if near_d83(*p)]
        if keep: out[key] = keep
    return out
if __name__ == "__main__":
    feats = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else fetch()
    x = build(feats)
    json.dump({"source": "City of Houston GIS HoustonMap/Transportation layer 3 (MTFP centerlines)", "bbox": BBOX, "n": len(x), "x": dict(sorted(x.items()))},
              open(os.path.join(HERE, "d83_xings.json"), "w"), separators=(",", ":"))
    print(len(S := segs(feats)), "segments;", len(x), "road pairs;", sum(len(v) for v in x.values()), "points")
