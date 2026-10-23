#!/usr/bin/env python3
"""District 83 backup logger (GitHub Actions, every 5 min). Does nothing while the main logger is fresh.
When the repo's stats.json is over 10 min old it polls the City of Houston public active-incident feed and saves
District 83 rows (any D83 unit) plus citywide fire-box rows to backup/cap-<ms>.json; the main logger merges and
removes them when it is back. Over 15 min stale it sends one 'stopped updating' push, then one 'back up' push.
State: backup/state.json (repo). Public status for the site: backup_status.json. Env: NTFY_TOPIC (secret), SIMULATE."""
import json, os, re, time, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Chicago")
FEED = ("https://mycity2.houstontx.gov/gisweb01/rest/services/HEC/HEC_Active_Incidents/MapServer/0/query?"
        + urllib.parse.urlencode({"where": "Agency='F'", "outFields": "*", "returnGeometry": "false", "f": "json"}))
CAPTURE_MIN, ALERT_MIN = 10, 15
KEEP = ("CALL_TIME", "Address", "CrossStreet", "Units", "ALARM_LEVEL", "IncidentType", "KeyMap", "LATITUDE", "LONGITUDE")
# same rules as d83rules.py on the box (unit_station, is_fire_box)
STATIONS = {"10", "69", "76", "83"}
PREFIXES = {"E", "L", "T", "TW", "SQ", "R", "RS", "RE", "A", "M", "AS", "MPE", "MPL"}
UNIT_RE = re.compile(r"^([A-Z]+)0*(\d+)$")
MULTI_RE = re.compile(r"^\s*[1-8]11\s+FIRE\b")
STRUCT_RE = re.compile(r"\b(HOUSE|HOME|RESIDENTIAL|RESIDENCE|DWELLING|COMMERCIAL|BUSINESS|STORE|SHOP|RESTAURANT|APARTMENT|APTS?|BUILDING|BLDG|STRUCTURE|HIGH[ -]?RISE|TOWNHOMES?|TOWNHOUSE|CONDO|DUPLEX|WAREHOUSE|INDUSTRIAL|CHURCH|SCHOOL|HOSPITAL|HOTEL|MOTEL)\b")
NOT_BOX_RE = re.compile(r"ALARM|STAND-?BY|FIRE EVENT|INVESTIGAT|CHECK|WATCH|DETECTOR")
def d83_unit(u):
    m = UNIT_RE.match((u or "").strip().upper())
    if not m: return False
    p, n = m.group(1), str(int(m.group(2)))
    return (p, n) == ("D", "83") or (p in PREFIXES and n in STATIONS)
def fire_box(t):
    t = (t or "").upper().strip()
    return bool(MULTI_RE.match(t)) or ("FIRE" in t and bool(STRUCT_RE.search(t)) and not NOT_BOX_RE.search(t))
def ct(ms): return datetime.fromtimestamp(ms / 1000, TZ).strftime("%-m/%-d %H:%M")
def load(p, d):
    try: return json.load(open(p))
    except (OSError, ValueError): return d
def save(p, o):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True); json.dump(o, open(p, "w"), indent=1, sort_keys=True); open(p, "a").write("\n")
def fetch():
    last = None
    for _ in range(2):
        try:
            req = urllib.request.Request(FEED + f"&_={int(time.time())}", headers={"User-Agent": "D83-backup/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r: j = json.load(r)
            if "error" in j: raise RuntimeError(str(j["error"])[:200])
            return [f["attributes"] for f in j.get("features", [])]
        except Exception as e: last = e; time.sleep(5)
    raise last
def push(title, msg, prio, tags):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic: print("alert skipped: no NTFY_TOPIC secret"); return False
    data = json.dumps({"topic": topic, "title": title, "message": msg, "priority": prio, "tags": tags,
                       "click": "https://district83-hfd.github.io/"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/", data=data, headers={"Content-Type": "application/json"}), timeout=20).read()
        print(f"alert sent: {title}"); return True
    except Exception as e: print(f"alert FAILED: {type(e).__name__}"); return False
def main():
    now = int(time.time() * 1000); sim = os.environ.get("SIMULATE", "") == "true"; tp = "[Test] " if sim else ""
    st = load("backup/state.json", {"alerted": False})
    gen = (load("stats.json", {}) or {}).get("generated") or 0
    age = 99 * 60 if sim else (now - gen) / 60000 if gen else 9999
    print(f"main logger data age: {age:.1f} min{' (simulated)' if sim else ''}")
    out = os.environ.get("GITHUB_OUTPUT"); flip = False
    status = load("backup_status.json", {"active": False})
    if age > CAPTURE_MIN:
        if not st.get("outage_since"): st["outage_since"] = gen or now; st["test"] = sim
        try:
            feats = fetch()
            rows = [{k: a.get(k) for k in KEEP} for a in feats
                    if any(d83_unit(u) for u in (a.get("Units") or "").split(",")) or fire_box(a.get("IncidentType"))]
            save(f"backup/cap-{now}.json", {"ts": now, "feed_n": len(feats), "rows": rows, "test": sim})
            st["captures"] = st.get("captures", 0) + 1; st["last_capture"] = now; st.pop("last_error", None)
            print(f"captured {len(rows)} of {len(feats)} feed rows")
        except Exception as e:
            st["last_error"] = f"{now}: {type(e).__name__}: {str(e)[:150]}"; print("feed fetch failed:", st["last_error"])
        if not status.get("active"):
            status = {"active": True, "since": st["outage_since"]}; flip = True
        status.update({"last": st.get("last_capture"), "n": st.get("captures", 0)})
        if age > ALERT_MIN and not st.get("alerted"):
            if push(f"{tp}District 83 Dashboard Stopped Updating",
                    f"No update from the main logger since {ct(st['outage_since'])} CT. The backup logger is capturing "
                    "District 83 and fire calls every 5-15 min; history fills in when the main logger is back.", 4, ["warning"]):
                st["alerted"] = True; st["alerted_at"] = now
    else:
        if st.get("alerted"):
            mins = round((now - (st.get("outage_since") or now)) / 60000)
            if push(f"{'[Test] ' if st.get('test') else ''}District 83 Dashboard Back Up",
                    f"The main logger is updating again (down about {mins // 60} h {mins % 60} min since "
                    f"{ct(st.get('outage_since') or now)} CT). {st.get('captures', 0)} backup capture(s) will be merged.", 3, ["white_check_mark"]):
                st["alerted"] = False
        if not st.get("alerted"):
            if st.get("outage_since"): st = {"alerted": False, "last_outage": {"from": st["outage_since"], "to": now, "captures": st.get("captures", 0), "test": st.get("test", False)}}
        if status.get("active"): status = {"active": False, "last_end": now}; flip = True
    save("backup/state.json", st); save("backup_status.json", status)
    if out: open(out, "a").write(f"flip={'1' if flip else '0'}\n")
if __name__ == "__main__": main()
