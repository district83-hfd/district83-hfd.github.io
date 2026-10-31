"""v66 live extras published inside stats.json: NWS weather (st["wx"]) and Houston TranStar crashes (st["traffic"]).

Both are fetched server-side by the collector, cached in memory and in extras_cache.json, and fail soft: any error
keeps the last good value (the page shows its age / hides it when too old) and never raises into the collector.
No personal data anywhere. Sources:
  weather  https://api.weather.gov  (National Weather Service, public domain, needs a User-Agent)
  traffic  https://traffic.houstontranstar.org/data/rss/incidents_rss.xml  (Houston TranStar RSS, free to use under
           https://traffic.houstontranstar.org/disclaimer.aspx; the page shows the attribution and links back)
"""
import json, os, re, time, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.environ.get("D83_BASE", "/workspace/d83")
CACHE = f"{BASE}/extras_cache.json"
UA = "District83Dashboard/1.0 (district83-hfd.github.io; HFD District 83 live dashboard)"
TZ = ZoneInfo("America/Chicago")
ZONE = "TXZ213"                        # NWS public forecast zone for Harris County
STATIONS = ["KSGR", "KHOU"]            # nearest first (Sugar Land, then Hobby); first one with a fresh reading wins
WX_EVERY, ALERT_EVERY, TRAFFIC_EVERY = 600, 300, 150   # seconds between fetches
OBS_MAX_AGE = 90 * 60                  # an observation older than this is not used
ALERT_KEEP = 30 * 60                   # on fetch failure keep the last good alerts this long

# ---------- weather ----------
def c2f(c): return None if c is None else c * 9 / 5 + 32
def kmh2mph(k): return None if k is None else k * 0.621371
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
def compass(deg): return None if deg is None else COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]

def parse_obs(j, now=None):
    """NWS observations/latest JSON -> compact dict, or None when unusable (no temperature, or too old)."""
    now = now or time.time()
    p = j["properties"]; v = lambda k: (p.get(k) or {}).get("value")
    t = v("temperature")
    if t is None: return None
    ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")).timestamp()
    if now - ts > OBS_MAX_AGE: return None
    temp = round(c2f(t))
    out = {"t": temp, "obs": int(ts * 1000), "cond": (p.get("textDescription") or "").strip()}
    hi, wc = c2f(v("heatIndex")), c2f(v("windChill"))
    if hi is not None and temp >= 80 and hi - temp >= 3: out["feels"] = round(hi)        # heat index only when meaningfully above
    elif wc is not None and temp <= 50 and temp - wc >= 5: out["feels"] = round(wc)      # wind chill, same idea
    sp, gu = kmh2mph(v("windSpeed")), kmh2mph(v("windGust"))
    if sp is not None:
        out["wind"] = round(sp); out["dir"] = compass(v("windDirection")) if round(sp) > 0 else None
        if gu is not None and round(gu) >= round(sp) + 5: out["gust"] = round(gu)       # a gust is only worth showing when it is well above the wind
    return out

# alert kind: red = life-safety warnings, amber = heat / wind / fire weather, blue = water and cold, gray = other
ALERT_RULES = [(r"tornado|severe thunderstorm warning|flash flood warning|extreme wind|hurricane warning|storm surge warning|red flag", "red"),
               (r"warning", "red"),
               (r"heat|wind|fire weather|dust|smoke|air quality", "amber"),
               (r"flood|rain|surf|rip current|tropical|hurricane|storm surge|freeze|frost|winter|\bice\b|cold|chill|fog|marine", "blue")]
LEVEL = lambda e: 0 if re.search(r"warning|emergency", e, re.I) else 1 if re.search(r"watch", e, re.I) else 2
def alert_kind(event):
    for rx, k in ALERT_RULES:
        if re.search(rx, event, re.I): return k
    return "gray"

def parse_alerts(j, now=None):
    now = now or time.time(); out = []
    for f in (j.get("features") or []):
        p = f.get("properties") or {}; ev = (p.get("event") or "").strip()
        if not ev: continue
        exp = p.get("ends") or p.get("expires")
        if exp:
            try:
                if datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp() < now: continue
            except ValueError: pass
        out.append({"e": ev, "k": alert_kind(ev), "h": (p.get("headline") or "")[:200], "x": exp})
    out.sort(key=lambda a: (LEVEL(a["e"]), a["e"]))
    seen, res = set(), []
    for a in out:
        if a["e"] not in seen: seen.add(a["e"]); res.append(a)
    return res[:4]

# ---------- traffic ----------
# v67: an incident counts only when its LOCATION is inside District 83. The TranStar RSS has no coordinates, only the road and a
# cross street ("IH-10 Katy Eastbound At Gessner Dr"), so the pair (road, cross street) is looked up in d83_xings.json: the points
# where the City's thoroughfare centerlines (MTFP layer) cross each other near D83 (built by build_xings.py). Each point is then
# checked against the City's box/district layer (firstdue.py): it counts when it is first-due to a District 83 station OR sits on the
# D83 edge (a D83 box within EDGE_AT_M / EDGE_NEAR_M). Anything that cannot be placed (unknown road, unknown cross street, no box map) is left out.
XINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "d83_xings.json")
EDGE_AT_M, EDGE_NEAR_M = 150, 500      # "At X": a D83 box within 150 m of the intersection; "Before/After/Near/Between": within 500 m (the crash can be up to a mile off the cross street)

_SFX = {"rd", "road", "dr", "drive", "st", "street", "ave", "avenue", "blvd", "boulevard", "ln", "lane", "pkwy", "parkway", "fwy", "freeway",
        "pl", "place", "ct", "court", "cir", "circle", "trl", "trail", "way", "hwy", "expy", "expressway", "tollway", "toll", "tollroad"}
# TranStar and the City spell the same freeway differently; both sides are reduced to one key.  Order matters.
_ALIAS = [(r"\bih 10 east\b", "ih10east"), (r"\b(?:ih|i|interstate) ?10\b", "ih10"),
          (r"\b(?:ih|i|interstate) ?610\b|\bloop 610\b|\b610 (?:west|south|north|east) loop\b", "ih610"),
          (r"\b(?:ih 69|us ?59) (?:eastex|north)\b|\beastex\b", "eastex"),
          (r"\b(?:ih|us|i) ?69\b|\bus ?59\b|\bsouthwest (?:fwy|freeway)\b", "us59"),
          (r"\bbeltway ?8\b|\bsam houston (?:tollway|toll road|pkwy|parkway)\b|\bbw ?8\b", "bw8"),
          (r"\b(?:sh|hwy|highway|texas) ?6\b", "sh6"),
          (r"\bwestpark toll", "westparktoll"), (r"\bus (?:hwy )?90a\b", "us90a")]
def canon(name, st_type=""):
    """Road / cross-street text (TranStar or City spelling) -> short comparison key; '' when there is nothing to compare."""
    s = re.sub(r"[.,'\u2019]", "", (name or "").lower()); s = re.sub(r"[-/]", " ", s); s = re.sub(r"\s+", " ", s).strip()
    for rx, key in _ALIAS:
        if re.search(rx, s): return key
    s = re.sub(r"^(?:n|s|e|w|ne|nw|se|sw) (?=\S)", "", s)
    w = s.split()
    while len(w) > 1 and w[-1] in _SFX: w.pop()
    k = " ".join(w)
    if k == "westpark" and re.search(r"freeway|toll", st_type or "", re.I): return "westparktoll"
    return k
def xkey(a, b): return "|".join(sorted((a, b)))

DIRS = {"Northbound": "NB", "Southbound": "SB", "Eastbound": "EB", "Westbound": "WB", "Inbound": "IB", "Outbound": "OB"}
KINDS_SHOWN = re.compile(r"accident|crash|collision|fire|hazard|debris|spill|pedestrian|wrong[- ]way|overturn|jackknif|rollover|flood|closed|closure|truck|stall", re.I)
SERIOUS = re.compile(r"accident|crash|collision|fire|hazard|debris|spill|pedestrian|wrong[- ]way|overturn|jackknif|rollover|flood|closed|closure", re.I)
TITLE_RE = re.compile(r"^(?P<road>.*?)\s+(?P<dir>Northbound|Southbound|Eastbound|Westbound|Inbound|Outbound)?\s*(?P<rest>(?:At|Before|After|Near|Between|Past)\b.*?)?\s+-\s+(?P<kind>[^-].*)$")

def parse_incident(title, desc):
    """One RSS item -> dict, or None when it does not parse. Status comes from 'Status: Cleared at 8:40 PM - Lanes Affected: ...'."""
    title = re.sub(r"\s+", " ", (title or "").strip())
    m = TITLE_RE.match(title)
    if not m: return None
    d = re.sub(r"\s+", " ", (desc or "").strip())
    st = re.search(r"Status:\s*([A-Za-z ]+?)(?: at (\d{1,2}:\d{2} [AP]M))?(?: - |$)", d)
    lanes = re.search(r"Lanes Affected:\s*(.+?)(?: - |$)", d)
    return {"road": m.group("road").strip(), "dir": DIRS.get(m.group("dir") or "", ""), "at": (m.group("rest") or "").strip(),
            "kind": m.group("kind").strip(), "status": st.group(1).strip() if st else "", "time": st.group(2) if st and st.group(2) else "",
            "lanes": lanes.group(1).strip() if lanes else ""}

_XG = {}
def xings():
    """{'a|b': [[lat, lon], ...]} from d83_xings.json ({} when missing -> nothing can be placed -> nothing is shown)."""
    if "d" not in _XG:
        try: _XG["d"] = json.load(open(XINGS_FILE)).get("x", {})
        except (OSError, ValueError): _XG["d"] = {}
    return _XG["d"]

def inside_d83(lat, lon, margin_m):
    """True when the point is first-due to a District 83 station, or sits on the D83 edge: a D83 box lies within margin_m (City box map)."""
    try:
        import firstdue as FD, math
        L = FD.locate(lat, lon)
        if L and L["fd"] and FD.district_of(L["fd"]) == "83": return True
        g = FD._load(); x, y = lon, lat; dx = margin_m / (111320 * math.cos(math.radians(y))); dy = margin_m / 110540
        seen = set()
        for i in range(int(math.floor((x - dx) / FD.CELL)), int(math.floor((x + dx) / FD.CELL)) + 1):
            for j in range(int(math.floor((y - dy) / FD.CELL)), int(math.floor((y + dy) / FD.CELL)) + 1):
                for bx in g.get((i, j), []):
                    if id(bx) in seen or not bx["due"] or FD.district_of(bx["due"][0]) != "83": continue
                    seen.add(id(bx)); bb = bx["bb"]
                    if bb[0] - dx <= x <= bb[2] + dx and bb[1] - dy <= y <= bb[3] + dy:
                        if FD._inside(x, y, bx["rings"]) or FD._edge_m(x, y, bx["rings"]) <= margin_m: return True
        return False
    except Exception: return False

def cross_names(at):
    """'At Gessner Dr' -> ('at', ['Gessner Dr']); 'Between A and B' -> ('between', ['A', 'B']); 'Before Bingle Rd/ Voss' -> ('before', [...])."""
    m = re.match(r"^(At|Before|After|Near|Between|Past)\s+(.*)$", at or "", re.I)
    if not m: return "", []
    kind = m.group(1).lower()
    parts = re.split(r"\s+and\s+", m.group(2), flags=re.I) if kind == "between" else [m.group(2)]
    names = [n.strip() for p in parts for n in re.split(r"\s*/\s*", p) if n.strip()]
    return kind, names

def _known_road(road, table=None):
    k = canon(road); return bool(k) and any(k in p.split("|") for p in (xings() if table is None else table))

def place(it, locator=None, table=None):
    """True = inside District 83; False = placed and outside; None = could not be placed (unknown road / cross street). Only True is shown."""
    locator = locator or inside_d83; table = xings() if table is None else table
    road = canon(it["road"]); kind, names = cross_names(it["at"])
    if not road or not names: return None
    margin = EDGE_AT_M if kind == "at" else EDGE_NEAR_M
    found = 0
    for n in names:
        pts = table.get(xkey(road, canon(n))) if canon(n) else None
        if not pts:
            if kind == "between": return None            # both ends must be placed
            continue
        if not all(locator(la, lo, margin) for la, lo in pts): return False
        found += 1
    return True if found else None

def on_d83_road(it, locator=None, table=None):
    return place(it, locator, table)

def wanted(it):
    """Open (not cleared) items worth a line: crashes and hazards always; a stall only when it blocks a main lane."""
    if re.search(r"clear", it["status"], re.I): return False
    if SERIOUS.search(it["kind"]): return True
    if re.search(r"stall|truck", it["kind"], re.I):
        return bool(it["lanes"]) and not re.fullmatch(r"(?:\d+ )?(?:Left |Right )?Shoulder Lane\(s\)(?:,? ?(?:\d+ )?(?:Left |Right )?Shoulder Lane\(s\))*", it["lanes"], re.I) and not re.search(r"^\s*(?:\d+ )?(?:Left |Right )?Shoulder", it["lanes"], re.I)
    return False

def parse_rss(xml_text, locator=None, table=None, unplaced=None):
    """Incidents RSS text -> {"items": [matched D83 items], "n": N, "total": N items in the feed}. Raises on bad XML.
    unplaced: optional list that receives 'road dir at - kind' for open items worth showing that could not be placed (for the log)."""
    root = ET.fromstring(xml_text)
    chan = root.find("channel")
    items = []; total = 0
    for e in chan.findall("item"):
        total += 1
        it = parse_incident(e.findtext("title"), e.findtext("description"))
        if not it or not wanted(it): continue
        pl = place(it, locator, table)
        if pl: items.append(it)
        elif pl is None and unplaced is not None and _known_road(it["road"]): unplaced.append(f"{it['road']} {it['dir']} {it['at']} - {it['kind']}")
    items.sort(key=lambda i: (0 if SERIOUS.search(i["kind"]) else 1, i["road"]))
    return {"items": items[:12], "n": len(items), "total": total}

# ---------- fetch + cache ----------
def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json, application/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.read().decode("utf8", "replace")

_MEM = {}
def _load():
    if not _MEM:
        try: _MEM.update(json.load(open(CACHE)))
        except (OSError, ValueError): pass
    return _MEM
def _save():
    try:
        tmp = CACHE + ".tmp"; json.dump(_MEM, open(tmp, "w")); os.replace(tmp, CACHE)
    except OSError: pass

def fetch_weather(now=None, log=print):
    m = _load(); now = now or time.time()
    if now - m.get("wx_try", 0) >= WX_EVERY:
        m["wx_try"] = now
        for s in STATIONS:
            try:
                o = parse_obs(json.loads(_get(f"https://api.weather.gov/stations/{s}/observations/latest")), now)
                if o: o["st"] = s; m["wx"] = o; m["wx_ok"] = now; break
            except Exception as e: log(f"weather {s}: {e!r}")
    if now - m.get("al_try", 0) >= ALERT_EVERY:
        m["al_try"] = now
        try: m["alerts"] = parse_alerts(json.loads(_get(f"https://api.weather.gov/alerts/active?zone={ZONE}")), now); m["al_ok"] = now
        except Exception as e: log(f"weather alerts: {e!r}")
    wx = m.get("wx")
    if not wx and not m.get("alerts"): return None
    out = dict(wx or {}); out["fetched"] = int(m.get("wx_ok", 0) * 1000)
    out["alerts"] = m.get("alerts", []) if now - m.get("al_ok", 0) <= ALERT_KEEP else []
    if wx and now - wx["obs"] / 1000 > OBS_MAX_AGE * 2: out = {"alerts": out["alerts"]}     # reading far too old: hide the line, keep alerts
    return out

def fetch_traffic(now=None, log=print):
    m = _load(); now = now or time.time()
    if now - m.get("tr_try", 0) >= TRAFFIC_EVERY:
        m["tr_try"] = now
        try:
            left = []; m["traffic"] = parse_rss(_get("https://traffic.houstontranstar.org/data/rss/incidents_rss.xml"), unplaced=left); m["tr_ok"] = now
            if left and left != m.get("tr_left"): log(f"traffic: {len(left)} open incident(s) on D83-area roads with an unknown cross street, left out: " + "; ".join(left[:6]))
            m["tr_left"] = left
        except Exception as e: log(f"traffic: {e!r}")
    t = m.get("traffic")
    if not t or now - m.get("tr_ok", 0) > 30 * 60: return None        # feed down for 30 min: hide rather than show old crashes
    return {**t, "fetched": int(m["tr_ok"] * 1000)}

def add_to_stats(st, now=None, log=print):
    """Called by the collector once per render. Never raises."""
    for key, fn in (("wx", fetch_weather), ("traffic", fetch_traffic)):
        try:
            v = fn(now, log)
            if v: st[key] = v
            else: st.pop(key, None)
        except Exception as e: log(f"extras {key}: {e!r}"); st.pop(key, None)
    try: _save()
    except Exception: pass
    return st
