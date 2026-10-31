#!/usr/bin/env python3
"""Cache the City's HFD Fire Districts (box) polygons to boxes.json (lon/lat, rounded to 5 decimals).
Public layer: HoustonMap/Public_Safety/MapServer/11. Refreshes at most weekly unless --force."""
import json, os, sys, time, urllib.request, urllib.parse
L = "https://mycity2.houstontx.gov/gisweb02/rest/services/HoustonMap/Public_safety/MapServer/11/query"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boxes.json")
def main():
    if "--force" not in sys.argv and os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < 7*86400:
        print("boxes.json fresh"); return
    feats, off = [], 0
    while True:
        q = urllib.parse.urlencode(dict(where="1=1", outFields="OBJECTID_12,TEXT,D1,D2,D3,D4,D5", outSR=4326,
                                        returnGeometry="true", resultOffset=off, resultRecordCount=1000,
                                        orderByFields="OBJECTID_12", geometryPrecision=5, f="json"))
        d = json.load(urllib.request.urlopen(L + "?" + q, timeout=60))
        fs = d.get("features", [])
        for f in fs:
            a = f["attributes"]; rings = (f.get("geometry") or {}).get("rings") or []
            if not rings: continue
            due = [str(int(a[k])) for k in ("D1","D2","D3","D4","D5") if a.get(k) not in (None, "", 0)]
            feats.append({"box": (a.get("TEXT") or "").strip(), "due": due, "rings": rings})
        off += len(fs)
        if not fs or not d.get("exceededTransferLimit"): break
    if len(feats) < 4000: raise SystemExit(f"only {len(feats)} boxes; keeping old cache")
    tmp = OUT + ".tmp"; json.dump(feats, open(tmp, "w"), separators=(",", ":")); os.replace(tmp, OUT)
    print(f"saved {len(feats)} boxes")
if __name__ == "__main__": main()
