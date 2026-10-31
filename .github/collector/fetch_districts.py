#!/usr/bin/env python3
"""Station -> HFD fire district table, cached in districts.json. Refreshed weekly with the box map (fetch_boxes.py).
Primary source: HFD Fire Stations page (Fire Dist. column). Cross-check: City GIS HoustonMap/Public_Safety layer 10
(IN_DIST / Admin), recorded only (differences listed in the file). Airport (ARFF) stations have no district.
On any failure the old file is kept."""
import html, json, os, re, sys, time, urllib.request
PAGE = "https://www.houstontx.gov/fire/about/firestations.html"
GIS = ("https://mycity2.houstontx.gov/gisweb02/rest/services/HoustonMap/Public_safety/MapServer/10/query?"
       "where=1%3D1&outFields=LABEL,IN_DIST,DC,Admin&returnGeometry=false&f=json")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "districts.json")
def _num(v):
    try: return str(int(float(str(v).strip())))
    except (TypeError, ValueError): return None
def page_table():
    s = urllib.request.urlopen(urllib.request.Request(PAGE, headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read().decode("utf-8", "ignore")
    out = {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", s, re.S):
        c = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if len(c) >= 5 and _num(c[0]) and _num(c[4]) and not c[1].lower().startswith("closed,"): out[_num(c[0])] = _num(c[4])
    return out
def gis_table():
    d = json.load(urllib.request.urlopen(GIS, timeout=60)); out = {}
    for f in d.get("features", []):
        a = f["attributes"]; s = _num(a.get("LABEL")); v = _num(a.get("Admin")) or _num(a.get("IN_DIST"))
        if s and v: out[s] = v
    return out
def main(force=False):
    if not force and os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < 7 * 86400: return
    p = page_table()
    try: g = gis_table()
    except Exception: g = {}
    if len(p) < 80 or any(p.get(s) != "83" for s in ("10", "69", "76", "83")):
        raise SystemExit(f"station page parse looks wrong ({len(p)} stations); keeping old districts.json")
    table = dict(p)
    diff = {s: {"page": p[s], "gis": g[s]} for s in p if s in g and g[s] != p[s]}
    data = {"source": PAGE, "fetched": int(time.time() * 1000), "d": dict(sorted(table.items(), key=lambda x: int(x[0]))),
            "page_vs_gis_differ": diff, "gis_only": sorted((s for s in g if s not in p), key=int)}
    tmp = OUT + ".tmp"; json.dump(data, open(tmp, "w"), separators=(",", ":")); os.replace(tmp, OUT)
    print(f"saved {len(table)} stations ({len(p)} from page, {len(diff)} differ from GIS)")
if __name__ == "__main__": main("--force" in sys.argv)
