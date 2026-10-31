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
# D83 roads, matched on the road part of the TranStar title ("IH-69 Southwest Southbound At Bissonnet St - Accident").
# (road regex, optional cross-street regex the item must also match). Edit this list to tune what counts as D83.
D83_ROADS = [
    (r"\bWest Sam Houston Tollway\b|\bBeltway 8\b|\bBW-?8\b", None),
    (r"\bWestpark Tollway\b", None),
    (r"\bIH-69 Southwest\b|\bUS-59 Southwest\b", None),
    (r"\bIH-10 Katy\b", r"Wilcrest|Kirkwood|Eldridge|Dairy Ashford|Sam Houston|Beltway|Gessner|Bunker Hill|Barker|Antoine|Wirt|Silber|Blalock|Echo|Bingle|Voss|Campbell|Addicks|Park Ten|Fry|Grand Pkwy"),
    (r"\bSH-6\b|\bHwy 6\b|\bHighway 6\b", None),
    (r"\bWestheimer\b|\bFM-1093\b", None),
    (r"\bRichmond\b|\bBriar Forest\b|\bDairy Ashford\b|\bEldridge\b|\bKirkwood\b|\bWilcrest\b|\bGessner\b|\bBellaire\b|\bBissonnet\b|\bHillcroft\b|\bFondren\b|\bBoone\b|\bBeechnut\b|\bBeltway\b", None),
]
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

def on_d83_road(it, roads=None):
    """The road part of the title matches a D83 road (and, for roads that run far outside D83, its cross street matches too)."""
    for rx, cross in (roads or D83_ROADS):
        if re.search(rx, it["road"], re.I) and (cross is None or re.search(cross, it["at"], re.I)): return True
    return False

def wanted(it):
    """Open (not cleared) items worth a line: crashes and hazards always; a stall only when it blocks a main lane."""
    if re.search(r"clear", it["status"], re.I): return False
    if SERIOUS.search(it["kind"]): return True
    if re.search(r"stall|truck", it["kind"], re.I):
        return bool(it["lanes"]) and not re.fullmatch(r"(?:\d+ )?(?:Left |Right )?Shoulder Lane\(s\)(?:,? ?(?:\d+ )?(?:Left |Right )?Shoulder Lane\(s\))*", it["lanes"], re.I) and not re.search(r"^\s*(?:\d+ )?(?:Left |Right )?Shoulder", it["lanes"], re.I)
    return False

def parse_rss(xml_text, roads=None):
    """Incidents RSS text -> {"items": [matched D83 items], "total": N items in the feed, "pub": RFC date}. Raises on bad XML."""
    root = ET.fromstring(xml_text)
    chan = root.find("channel")
    items = []; total = 0
    for e in chan.findall("item"):
        total += 1
        it = parse_incident(e.findtext("title"), e.findtext("description"))
        if it and on_d83_road(it, roads) and wanted(it): items.append(it)
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
        try: m["traffic"] = parse_rss(_get("https://traffic.houstontranstar.org/data/rss/incidents_rss.xml")); m["tr_ok"] = now
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
