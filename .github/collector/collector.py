#!/usr/bin/env python3
"""District 83 incident logger: polls the City of Houston HEC active-incident feed,
logs every HFD incident + unit in SQLite, exports stats.json and regenerates the dashboard."""
import csv, json, os, re, sqlite3, sys, time, traceback, urllib.request, urllib.parse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

BASE = os.environ.get("D83_BASE", "/workspace/d83")
DB = f"{BASE}/incidents.db"
STATS = f"{BASE}/stats.json"
DASH = f"{BASE}/District83_Dashboard.html"
TEMPLATE = f"{BASE}/dashboard_template.html"
SCHEDULE = os.environ.get("D83_SCHEDULE", "/workspace/hfd_schedule.csv")
LOG = f"{BASE}/collector.log"
POLL_SECONDS = 60   # was 120 until 2026-10-21; the City feed itself changes about every 5 min (measured), never go below 60
TZ = ZoneInfo("America/Chicago")
SHIFT_CHANGE = (6, 30)          # HFD shift change, local time (HH, MM). Calls before this belong to the previous date's shift.
STATIONS = ["10", "69", "76", "83"]
EXCLUDE_PREFIXES = {"MD"}       # medical director units are not station units
# Fire types: any type containing FIRE counts as a working-fire candidate unless listed here.
NON_WORKING_FIRE_TYPES = {"AUTOMATIC ALARM", "FIRE ALARM", "FIRE EVENT", "STAND-BY FIRE",
                          "STANDBY FIRE", "FIRE INVESTIGATION", "FIRE WATCH", "FIRE CHECK"}
ALARM_COUNTS_AS_WORKING = True  # ALARM_LEVEL >= 1 counts as a working fire regardless of type
FEED = ("https://mycity2.houstontx.gov/gisweb01/rest/services/HEC/HEC_Active_Incidents/MapServer/0/query?"
        + urllib.parse.urlencode({"where": "Agency='F'", "outFields": "*", "returnGeometry": "false", "f": "json"}))
UNIT_RE = re.compile(r"^([A-Z]+)0*(\d+)$")
import d83rules as R   # shared unit + fire-type rules (same as alert_check.py and the dashboard)
import firstdue as FD  # first-due station from the City box map (boxes.json); out-of-area rule
WORKING_FIRE_MIN_MINUTES = R.WF_MIN   # v44: 50 (was 45); a fire box that has stayed in the CAD feed this long (from call time) is a working fire
ALIAS_WINDOW_MS = 15 * 60000      # a re-keyed incident must reappear within this long after the old key dropped out
ALIAS_CT_MS = 30 * 60000          # ...and its call time must be within this of the old call time

def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S %Z} {msg}\n")

def unit_station(u):
    """Allow-listed station-unit prefixes with a District 83 station number, plus D83. See d83rules.py."""
    return R.unit_station(u)

def reclassify(c):
    """Re-apply unit_station() to stored rows so old data follows the current unit rule (idempotent, cheap)."""
    ch = 0
    for (u, s) in c.execute("SELECT DISTINCT unit, station FROM unit_incidents").fetchall():
        ns = unit_station(u)
        if ns != s:
            ch += c.execute("UPDATE unit_incidents SET station=? WHERE unit=? AND station IS ?", (ns, u, s)).rowcount
    d = c.execute("""UPDATE incidents SET district=CASE WHEN EXISTS(SELECT 1 FROM unit_incidents ui WHERE ui.ikey=incidents.ikey
                     AND ui.station IS NOT NULL) THEN 1 ELSE 0 END
                     WHERE district IS NOT (CASE WHEN EXISTS(SELECT 1 FROM unit_incidents ui WHERE ui.ikey=incidents.ikey
                     AND ui.station IS NOT NULL) THEN 1 ELSE 0 END)""").rowcount
    c.commit()
    if ch or d: log(f"reclassify: {ch} unit rows, {d} incidents changed district flag")
    try: merge_recreated(c)   # v62: history follows the re-created-call rule too (idempotent)
    except Exception: log("merge_recreated error: " + traceback.format_exc())

# ---- v62: one incident when CAD re-creates a call under new keys (location edits) ------------------------------------
RECREATE_GAP_MS = 120000        # the unit moves to the new key within 2 polls of leaving the old one (overlap up to the same)
RECREATE_DIST_M = 750           # new location within 750 m of the old one
RECREATE_A_MAX_MS = 20 * 60000  # no-same-call-time cases only link a call that was open 20 min or less
RECREATE_B_MAX_MS = 6 * 60000   # ...and a new key that itself lasted 6 min or less (a pure address edit, not a new run)

def _hav_m(a, b):
    import math
    if not (a[0] and a[1] and b[0] and b[1]): return None
    p = math.pi / 180; dl = (b[0] - a[0]) * p; dn = (b[1] - a[1]) * p
    h = math.sin(dl / 2) ** 2 + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin(dn / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))

def recreate_link(A, B, ua, ub):
    """True when incident B is CAD's re-creation of incident A (A: earlier first_seen). A, B = dicts with ikey, ct, addr, cs,
    type, lat, lon, fs, ls; ua, ub = {unit: (first_seen, last_seen)}. All must hold: same type; a shared unit that moves from
    A to B within RECREATE_GAP_MS (B first seen no later than that after A's last sighting); locations within RECREATE_DIST_M;
    and one of: (a) the same CAD call-time minute; (b) two or more shared units, B adds no new unit, call time not earlier
    (crew moved together); (c) B's call time falls inside A's life, B adds no unit, A was open <= 20 min and B <= 6 min."""
    if (A["type"] or "").strip().upper() != (B["type"] or "").strip().upper(): return False
    if not (A["fs"] <= B["fs"] <= A["ls"] + RECREATE_GAP_MS): return False
    sh = set(ua) & set(ub)
    if not sh: return False
    ug = min(ub[u][0] - ua[u][1] for u in sh)
    if not -RECREATE_GAP_MS <= ug <= RECREATE_GAP_MS: return False
    if (A["addr"], A["cs"]) == (B["addr"], B["cs"]): d = 0
    else:
        d = _hav_m((A["lat"], A["lon"]), (B["lat"], B["lon"]))
        if d is None or d > RECREATE_DIST_M: return False
    if (A["ct"] or 0) // 60000 == (B["ct"] or 0) // 60000: return True
    if set(ub) - set(ua) or (A["ct"] or 0) > (B["ct"] or 0): return False
    if len(sh) >= 2: return A["ls"] - A["fs"] <= RECREATE_A_MAX_MS
    return (B["ct"] <= A["ls"] and A["ls"] - A["fs"] <= RECREATE_A_MAX_MS and B["ls"] - B["fs"] <= RECREATE_B_MAX_MS)

def recreate_groups(incs, units):
    """Union incidents linked by recreate_link. incs: list of dicts sorted by first_seen. Returns [[ikey, ...], ...] (size >= 2)."""
    par = {i["ikey"]: i["ikey"] for i in incs}
    def find(x):
        while par[x] != x: par[x] = par[par[x]]; x = par[x]
        return x
    for i, A in enumerate(incs):
        for B in incs[i + 1:]:
            if B["fs"] > A["ls"] + RECREATE_GAP_MS:
                if B["fs"] > A["fs"] + 7 * 3600000: break   # sorted by first_seen: nothing later can attach to A
                continue
            if recreate_link(A, B, units.get(A["ikey"], {}), units.get(B["ikey"], {})):
                par[find(B["ikey"])] = find(A["ikey"])
    g = {}
    for k in par: g.setdefault(find(k), []).append(k)
    return [v for v in g.values() if len(v) > 1]

def merge_recreated(c, since=None):
    """v62: when dispatch edits a call's location CAD re-creates it under a new key (new call time/address) and the feed
    shows it as a new incident. Link those keys into ONE incident (one run per unit): keep the earliest key, earliest call
    time/first_seen, latest last_seen, and the LATEST location/type; the other keys become aliases of it so later polls land
    on the same row. Idempotent (a merged key no longer exists as an incident); since = only look at incidents seen since then.
    Returns [(survivor_key, [merged keys])]."""
    lo = (since if since is not None else 0)
    rows = c.execute("SELECT ikey,call_time,address,cross_street,type,lat,lon,first_seen,last_seen FROM incidents WHERE last_seen>=? ORDER BY first_seen, call_time, ikey", (lo - RECREATE_GAP_MS,)).fetchall()
    incs = [dict(ikey=r[0], ct=r[1], addr=(r[2] or ""), cs=(r[3] or ""), type=r[4], lat=r[5], lon=r[6], fs=r[7], ls=r[8]) for r in rows]
    if len(incs) < 2: return []
    keys = {i["ikey"] for i in incs}; units = {}
    for k, u, f, l in c.execute("SELECT ikey,unit,first_seen,last_seen FROM unit_incidents WHERE ikey IN (SELECT ikey FROM incidents WHERE last_seen>=?)", (lo - RECREATE_GAP_MS,)):
        units.setdefault(k, {})[u] = (f, l)
    by = {i["ikey"]: i for i in incs}; done = []
    for grp in recreate_groups(incs, units):
        mem = sorted((by[k] for k in grp), key=lambda i: (i["fs"], i["ct"] or 0, i["ikey"]))
        sv = mem[0]["ikey"]; fin = max(mem, key=lambda i: (i["ls"], i["fs"], i["ikey"]))
        others = [m["ikey"] for m in mem if m["ikey"] != sv]
        ph = ",".join("?" * len(grp))
        agg = c.execute(f"SELECT MIN(call_time), MAX(max_alarm), MAX(district), MIN(first_seen), MAX(last_seen) FROM incidents WHERE ikey IN ({ph})", grp).fetchone()
        fr = c.execute("SELECT address,cross_street,type,keymap,lat,lon FROM incidents WHERE ikey=?", (fin["ikey"],)).fetchone()
        sd, sh_ = shift_date_of(agg[0]) if agg[0] else (None, None)
        c.execute("""UPDATE incidents SET call_time=?, max_alarm=?, district=?, first_seen=?, last_seen=?, address=?, cross_street=?, type=?,
                     keymap=?, lat=?, lon=?, shift_date=COALESCE(?, shift_date), shift=COALESCE(?, shift) WHERE ikey=?""",
                  (agg[0], agg[1], agg[2], agg[3], agg[4], *fr, sd, sh_, sv))
        for o in others:
            for u, st, f, l in c.execute("SELECT unit,station,first_seen,last_seen FROM unit_incidents WHERE ikey=?", (o,)).fetchall():
                c.execute("""INSERT INTO unit_incidents VALUES(?,?,?,?,?) ON CONFLICT(ikey, unit) DO UPDATE SET
                             last_seen=MAX(last_seen, excluded.last_seen), first_seen=MIN(first_seen, excluded.first_seen)""", (sv, u, st, f, l))
            c.execute("DELETE FROM unit_incidents WHERE ikey=?", (o,))
            c.execute("DELETE FROM incidents WHERE ikey=?", (o,))
            c.execute("UPDATE aliases SET ikey=? WHERE ikey=?", (sv, o))
            c.execute("INSERT OR REPLACE INTO aliases VALUES(?,?,?)", (o, sv, int(time.time() * 1000)))
        log(f"recreated call linked: {others} -> {sv} (location edited in CAD; shown at its final location)")
        done.append((sv, others))
    c.commit()
    return done

def load_schedule():
    sched = {}
    with open(SCHEDULE) as f:
        for r in csv.DictReader(f):
            sched[r["date"]] = r["shift_on_duty"].strip()
    return sched
SCHED = load_schedule()
_dates = sorted(SCHED)
_PERIOD = 8  # verified A A B B ... style repeat in the calendar; used only to extrapolate past its end

def shift_for_date(ds):
    if ds in SCHED: return SCHED[ds]
    d0 = date.fromisoformat(_dates[0]); d = date.fromisoformat(ds)
    off = (d - d0).days % _PERIOD
    return SCHED[(d0 + timedelta(days=off)).isoformat()]

def shift_date_of(ms):
    t = datetime.fromtimestamp(ms / 1000, TZ)
    if (t.hour, t.minute) < SHIFT_CHANGE: t -= timedelta(days=1)
    ds = t.date().isoformat()
    return ds, shift_for_date(ds)

def week_of(sd):
    """Run Counts week for a shift date: the Monday..Sunday tours, i.e. Mon 06:30 CT to the next Mon 06:30 CT.
    Works on shift dates (already CT- and DST-correct), so a call belongs to its tour's week; tours never split.
    Returns (monday_sd, next_monday_sd, start_ms, end_ms)."""
    d = date.fromisoformat(sd); m = d - timedelta(days=d.weekday()); n = m + timedelta(days=7)
    ms = lambda x: int(datetime(x.year, x.month, x.day, *SHIFT_CHANGE, tzinfo=TZ).timestamp() * 1000)
    return m.isoformat(), n.isoformat(), ms(m), ms(n)

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS incidents(
      ikey TEXT PRIMARY KEY, call_time INTEGER, address TEXT, cross_street TEXT, type TEXT,
      max_alarm INTEGER, keymap TEXT, lat REAL, lon REAL, shift_date TEXT, shift TEXT,
      first_seen INTEGER, last_seen INTEGER, district INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS unit_incidents(
      ikey TEXT, unit TEXT, station TEXT, first_seen INTEGER, last_seen INTEGER,
      PRIMARY KEY(ikey, unit));
    CREATE TABLE IF NOT EXISTS polls(ts INTEGER PRIMARY KEY, ok INTEGER, n INTEGER, err TEXT);
    CREATE INDEX IF NOT EXISTS ix_ui_station ON unit_incidents(station);
    CREATE INDEX IF NOT EXISTS ix_inc_sd ON incidents(shift_date);
    CREATE TABLE IF NOT EXISTS aliases(raw_key TEXT PRIMARY KEY, ikey TEXT, created INTEGER);
    CREATE INDEX IF NOT EXISTS ix_inc_addr ON incidents(address, cross_street);
    """)
    return c

def _fetch_timeout(*_): raise TimeoutError("feed fetch exceeded 75 s hard cap")

def fetch():
    # v36: urlopen's timeout is per socket operation; SIGALRM caps the whole request (connect + slow-drip read) at 75 s
    import signal
    req = urllib.request.Request(FEED + f"&_={int(time.time())}", headers={"User-Agent": "D83-logger/1.0"})
    old = signal.signal(signal.SIGALRM, _fetch_timeout); signal.alarm(75)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.load(r)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old)
    if "error" in j: raise RuntimeError(str(j["error"]))
    return [f["attributes"] for f in j.get("features", [])]

def resolve_aliases(c, raw, now):
    """CAD sometimes edits CALL_TIME, which changes our key. Map such a re-keyed incident back to the original row:
    same address + cross street, the old key dropped out of the feed within ALIAS_WINDOW_MS, call times within
    ALIAS_CT_MS, and at least one shared unit. Known aliases are reused on later polls. Returns {raw_key: ikey}."""
    out, claimed = {}, set()
    live = {r[4] for r in raw}
    for rk, ik in c.execute("SELECT raw_key, ikey FROM aliases WHERE raw_key IN (%s)" % ",".join("?" * len(live)), list(live)).fetchall() if live else []:
        out[rk] = ik; claimed.add(ik)
    for a, ct, addr, cs, rk, units in raw:
        if rk in out or not units: continue
        if c.execute("SELECT 1 FROM incidents WHERE ikey=?", (rk,)).fetchone(): continue
        for ik, oct_ in c.execute("""SELECT ikey, call_time FROM incidents WHERE address=? AND cross_street=? AND ikey<>?
                                     AND last_seen<? AND last_seen>=? ORDER BY last_seen DESC""",
                                  (addr, cs, rk, now, now - ALIAS_WINDOW_MS)).fetchall():
            if ik in live or ik in claimed or abs((oct_ or 0) - ct) > ALIAS_CT_MS: continue
            ou = {r[0] for r in c.execute("SELECT unit FROM unit_incidents WHERE ikey=?", (ik,))}
            if not ou & set(units): continue
            c.execute("INSERT OR REPLACE INTO aliases VALUES(?,?,?)", (rk, ik, now))
            out[rk] = ik; claimed.add(ik)
            log(f"alias: {rk} -> {ik} (call time edited in CAD)")
            break
    return out

def poll(c):
    now = int(time.time() * 1000)
    try:
        feats = fetch()
    except Exception as e:
        c.execute("INSERT OR REPLACE INTO polls VALUES(?,?,?,?)", (now, 0, 0, repr(e)[:300])); c.commit()
        log(f"poll error: {e!r}"); return False
    ingest(c, feats, now, 1)
    return True

def ingest(c, feats, now, ok):
    """Store one feed snapshot seen at time `now`. ok=1 live poll, ok=2 backup-logger capture (v37).
    Order-independent upserts: earliest first_seen, latest last_seen, type from the newest sighting."""
    if not c.execute("SELECT v FROM meta WHERE k='logging_start'").fetchone():
        c.execute("INSERT INTO meta VALUES('logging_start',?)", (str(now),))
    raw = []
    for a in feats:
        ct = a.get("CALL_TIME") or 0
        addr = (a.get("Address") or "").strip(); cs = (a.get("CrossStreet") or "").strip()
        units = [u.strip().upper() for u in (a.get("Units") or "").split(",") if u.strip()]
        raw.append((a, ct, addr, cs, f"{ct}|{addr}|{cs}", units))
    keymap = resolve_aliases(c, raw, now)
    for a, ct, addr, cs, rk, units in raw:
        ikey = keymap.get(rk, rk)
        dist = int(any(unit_station(u) for u in units))
        sd, sh = shift_date_of(ct)
        alarm = int(a.get("ALARM_LEVEL") or 0)
        c.execute("""INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(ikey) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen),
          first_seen=MIN(first_seen, excluded.first_seen),
          type=CASE WHEN excluded.last_seen>=last_seen THEN excluded.type ELSE type END,
          call_time=MIN(call_time, excluded.call_time),
          address=CASE WHEN excluded.last_seen>=last_seen THEN excluded.address ELSE address END,
          cross_street=CASE WHEN excluded.last_seen>=last_seen THEN excluded.cross_street ELSE cross_street END,
          lat=CASE WHEN excluded.last_seen>=last_seen THEN COALESCE(excluded.lat, lat) ELSE lat END,
          lon=CASE WHEN excluded.last_seen>=last_seen THEN COALESCE(excluded.lon, lon) ELSE lon END,
          keymap=CASE WHEN excluded.last_seen>=last_seen THEN COALESCE(excluded.keymap, keymap) ELSE keymap END,
          max_alarm=MAX(max_alarm, excluded.max_alarm), district=MAX(district, excluded.district)""",
          (ikey, ct, addr, cs, a.get("IncidentType") or "", alarm, a.get("KeyMap"), a.get("LATITUDE"),
           a.get("LONGITUDE"), sd, sh, now, now, dist))
        for u in units:
            c.execute("""INSERT INTO unit_incidents VALUES(?,?,?,?,?)
              ON CONFLICT(ikey, unit) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen),
              first_seen=MIN(first_seen, excluded.first_seen)""",
              (ikey, u, unit_station(u), now, now))
    c.execute("INSERT OR REPLACE INTO polls VALUES(?,?,?,?)", (now, ok, len(feats), None if ok == 1 else "backup")); c.commit()

MANUAL_UNITS = f"{BASE}/manual_units.json"   # v39: crew-reported units the City feed dropped before we polled
MANUAL_MATCH_MS = 30 * 60000
def _manual_ct(v):
    if isinstance(v, (int, float)): return int(v)
    return int(datetime.strptime(str(v).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp() * 1000)

def manual_entries():
    try: items = json.load(open(MANUAL_UNITS))
    except FileNotFoundError: return []
    except (OSError, ValueError) as e: log(f"manual_units.json unreadable: {e!r}"); return []
    return [m for m in (items.get("units", []) if isinstance(items, dict) else items) if isinstance(m, dict) and (m.get("unit") or "").strip()]

def resolve_manual(c, m):
    """The incident a manual entry belongs to: its ikey (or an alias of it), else the same address + cross street
    with the call time within 30 min (closest wins), so a CAD re-key or a DB rebuild does not lose it."""
    k = (m.get("ikey") or "").strip()
    if k:
        if c.execute("SELECT 1 FROM incidents WHERE ikey=?", (k,)).fetchone(): return k
        a = c.execute("SELECT ikey FROM aliases WHERE raw_key=?", (k,)).fetchone()
        if a and c.execute("SELECT 1 FROM incidents WHERE ikey=?", (a[0],)).fetchone(): return a[0]
    try: ct = _manual_ct(m.get("call_time")) if m.get("call_time") is not None else int(k.split("|")[0])
    except (ValueError, IndexError): return None
    addr = (m.get("address") or (k.split("|")[1] if k.count("|") == 2 else "")).strip().upper()
    cs = (m.get("cross_street") if m.get("cross_street") is not None else (k.split("|")[2] if k.count("|") == 2 else "")).strip().upper()
    r = c.execute("""SELECT ikey FROM incidents WHERE address=? AND cross_street=? AND ABS(call_time-?)<=?
                     ORDER BY ABS(call_time-?) LIMIT 1""", (addr, cs, ct, MANUAL_MATCH_MS, ct)).fetchone()
    return r[0] if r else None

def apply_manual_units(c):
    """v39: add each manual_units.json unit to its incident, every cycle (idempotent). first_seen = last_seen = the
    incident's earliest unit first_seen, so it counts as a run everywhere (Run Counts, fire and working-fire counts,
    out of first-due, Metrics, Report, Heat Map) but adds no committed time and never makes a station look stripped.
    A row the feed itself logged is never overwritten (DO NOTHING). Returns [(ikey, unit, note)] now in the DB."""
    out = []
    for m in manual_entries():
        u = m["unit"].strip().upper(); ik = resolve_manual(c, m)
        if not ik:
            if (ik_ := m.get("ikey") or m.get("call_time")) not in _MANUAL_MISS: _MANUAL_MISS.add(ik_); log(f"manual unit {u}: no matching incident for {ik_}")
            continue
        fs = c.execute("SELECT MIN(first_seen) FROM unit_incidents WHERE ikey=? AND unit<>?", (ik, u)).fetchone()[0] \
             or c.execute("SELECT first_seen FROM incidents WHERE ikey=?", (ik,)).fetchone()[0]
        cur = c.execute("INSERT INTO unit_incidents VALUES(?,?,?,?,?) ON CONFLICT(ikey, unit) DO NOTHING", (ik, u, unit_station(u), fs, fs))
        if cur.rowcount: log(f"manual unit {u} added to {ik} ({m.get('note') or 'added per crew report'})")
        if unit_station(u): c.execute("UPDATE incidents SET district=1 WHERE ikey=? AND district=0", (ik,))
        out.append((ik, u, m.get("note") or "added per crew report"))
    c.commit()
    return out
_MANUAL_MISS = set()

def manual_applied(c):
    """Read-only: manual entries that resolve to an incident (for stats.json)."""
    out = []
    for m in manual_entries():
        ik = resolve_manual(c, m)
        if ik: out.append({"k": ik, "u": m["unit"].strip().upper()})
    return out

BACKUP_DIR = f"{BASE}/site/backup"   # v37: captures from the GitHub Actions backup logger (pulled by publish.sh)
def merge_backup(c):
    """Merge backup-logger captures into the DB (idempotent: same upserts as live polls), then archive them and
    delete them from the repo copy (publish.sh commits the deletion)."""
    import glob, shutil
    files = sorted(glob.glob(f"{BACKUP_DIR}/cap-*.json"))
    if not files: return 0
    os.makedirs(f"{BASE}/backup_merged", exist_ok=True); n = rows = 0
    for f in files:
        try: cap = json.load(open(f))
        except (OSError, ValueError): continue   # half-written by a git checkout; next cycle
        ts = int(cap.get("ts") or 0)
        if ts:
            ingest(c, cap.get("rows") or [], ts, 2); rows += len(cap.get("rows") or [])
        shutil.copy2(f, f"{BASE}/backup_merged/"); os.remove(f); n += 1
    log(f"merged {n} backup capture(s), {rows} rows ")
    return n

MULTI_ALARM_RE = re.compile(r"^\s*[1-8]11\s+FIRE\b")   # HFD "111 FIRE" ... "811 FIRE": an additional alarm was called (code shown as is)
def is_multi_alarm(t): return bool(MULTI_ALARM_RE.match((t or "").upper()))
# Fire box = a structure-fire box type or an x11 FIRE code. Same lists as the dashboard ticker.
STRUCT_RE = re.compile(r"\b(HOUSE|HOME|RESIDENTIAL|RESIDENCE|DWELLING|COMMERCIAL|BUSINESS|STORE|SHOP|RESTAURANT|APARTMENT|APTS?|BUILDING|BLDG|STRUCTURE|HIGH[ -]?RISE|TOWNHOMES?|TOWNHOUSE|CONDO|DUPLEX|WAREHOUSE|INDUSTRIAL|CHURCH|SCHOOL|HOSPITAL|HOTEL|MOTEL)\b")
NOT_BOX_RE = re.compile(r"ALARM|STAND-?BY|FIRE EVENT|INVESTIGAT|CHECK|WATCH|DETECTOR")
def is_fire_box(t):
    t = (t or "").upper().strip()
    return is_multi_alarm(t) or ("FIRE" in t and bool(STRUCT_RE.search(t)) and not NOT_BOX_RE.search(t))
def cad_minutes(call_time, first_seen, last_seen):
    """Minutes the incident has been in the CAD feed: call time (or first seen if missing) to last seen."""
    start = call_time if call_time and call_time <= first_seen else first_seen
    return round((last_seen - start) / 60000)
def is_working_fire(t, cad_min): return is_fire_box(t) and cad_min >= WORKING_FIRE_MIN_MINUTES

def is_working_type(t, alarm):   # kept for older callers: "is this a fire box"
    return is_fire_box(t)

def fmt(ms): return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M")

def now_ms():
    """Current time in ms; D83_NOW_MS overrides it (used only to simulate a year rollover in tests)."""
    v = os.environ.get("D83_NOW_MS")
    return int(v) if v else int(time.time() * 1000)

def year_bounds(year):
    """Calendar year in Central time: Jan 1 00:00:00 CT to the next Jan 1 00:00:00 CT (exclusive)."""
    return (int(datetime(year, 1, 1, tzinfo=TZ).timestamp() * 1000), int(datetime(year + 1, 1, 1, tzinfo=TZ).timestamp() * 1000))

def district_rows(c, where, params):
    """District 83 incidents (with all units on each call) matching a WHERE clause on incidents."""
    hb = has_backfill(c)
    cols = "ikey,call_time,address,cross_street,type,max_alarm,keymap,lat,lon,shift_date,shift,first_seen,last_seen"
    dist = "(district=1 OR ikey IN (SELECT ikey FROM bf.unit_incidents WHERE station IS NOT NULL))" if hb else "district=1"
    incs = c.execute(f"SELECT {cols} FROM incidents WHERE {dist} AND ({where})", params).fetchall()
    bfk = set()
    if hb:   # v43b: Jan 1 -> live start (and feed gaps) from HFD unit reports, block-level location only
        bi = c.execute(f"SELECT {cols} FROM bf.incidents WHERE district=1 AND ({where})", params).fetchall()
        bfk = {r[0] for r in bi}; incs = incs + bi
    incs.sort(key=lambda r: -(r[1] or 0))
    if not incs: return []
    keys = [r[0] for r in incs]; urows = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]; ph = ','.join('?' * len(chunk))
        for ik, u, s, f, l in c.execute(f"SELECT ikey,unit,station,first_seen,last_seen FROM unit_incidents WHERE ikey IN ({ph})", chunk):
            urows.setdefault(ik, {})[u] = (s, f, l)
        if hb:   # report units the feed dropped (or never showed) -- the feed's own row always wins
            for ik, u, s, f, l in c.execute(f"SELECT ikey,unit,station,first_seen,last_seen FROM bf.unit_incidents WHERE ikey IN ({ph})", chunk):
                urows.setdefault(ik, {}).setdefault(u, (s, f, l))
    units = {}; spans = {}; uspans = {}
    for ik, m in urows.items():
        for u, (s, f, l) in sorted(m.items(), key=lambda z: (z[1][1], z[0])):
            units.setdefault(ik, []).append({"u": u, "d": bool(s), "min": round((l - f) / 60000)})
            spans.setdefault(ik, []).append((f, l)); uspans.setdefault(ik, []).append((u, f, l))
    hose = backfill_hose(c) if hb else set()
    sfg = backfill_sf_group(c) if hb else {}   # v50: first-dispatch group (LB/MB) for fires CAD retyped to 211/311
    tgm = backfill_type_group(c, sfg) if hb else {}   # v52: house / high-rise group for fire-type labels (report codes)
    fds = {}
    for r in incs:
        try:
            fd, n = FD.call_fd(r[7], r[8]); fds[r[0]] = (fd, n) + (FD.call_district(r[7], r[8], fd) if fd else (None, 0))
        except Exception: fds[r[0]] = (None, 0, None, 0)
    return [{"k": r[0], "t": r[1], "addr": r[2], "cs": r[3], "type": r[4], "alarm": r[5], "km": r[6],
             "fd": fds[r[0]][0], "fdn": fds[r[0]][1], "fdd": fds[r[0]][2], "fddn": fds[r[0]][3],
             "lat": r[7], "lon": r[8], "sd": r[9], "sh": r[10], "fs": r[11], "ls": r[12],
             "min": round((r[12] - r[11]) / 60000), "units": units.get(r[0], []),
             "cad": cad_minutes(r[1], r[11], r[12]), "wt": is_fire_box(r[4]),
             "xwf": R.is_wf_excluded(r[0]), "ov2": round(R.overlap2_ms(spans.get(r[0], [])) / 60000),
             "why": _why(r, spans, uspans, bfk, hose), **({"bf": 1} if r[0] in bfk else {}),
             **({"og": sfg[r[0]]} if r[0] in sfg and re.match(r"^[23]11\b", str(r[4] or "")) else {}),
             **({"tg": tgm[r[0]]} if r[0] in tgm and not re.match(r"^[23]11\b", str(r[4] or "")) and (is_fire_box(r[4]) or r[0] in hose) else {})} for r in incs]

HOSE_WHY = "5-inch hose on the ground (HFD report)"
def _why(r, spans, uspans, bfk, hose):
    """Live rules for feed calls; HFD's own 5-inch-hose list is the truth for report calls and also tags any feed call on it."""
    if R.is_wf_excluded(r[0]): return None
    if r[0] in bfk: return HOSE_WHY if r[0] in hose else None
    return R.wf_reason(r[4], cad_minutes(r[1], r[11], r[12]), spans.get(r[0], []), uspans.get(r[0], []), r[1], r[11], r[0]) or (HOSE_WHY if r[0] in hose else None)

BACKFILL_DB = f"{BASE}/backfill.db"   # private (never published): built from HFD unit reports by source_data/build_backfill.py
def has_backfill(c):
    if any(r[1] == "bf" for r in c.execute("PRAGMA database_list")): return True
    if not os.path.exists(BACKFILL_DB): return False
    try: c.execute("ATTACH DATABASE ? AS bf", (BACKFILL_DB,)); return True
    except sqlite3.Error as e: log(f"backfill attach failed: {e!r}"); return False
def backfill_hose(c): return {r[0] for r in c.execute("SELECT ikey FROM bf.hose")}
def backfill_sf_group(c):
    try: return {k: g for k, g in c.execute("SELECT ikey, grp FROM bf.sf_group") if g in ("LB", "MB")}
    except sqlite3.Error: return {}
def backfill_type_group(c, sfg):
    """v52: label group for report fires -- "HR" high-rise box, "MB" house box (or a fire HFD lists with house fires). Labels only."""
    out = {k: "MB" for k, g in sfg.items() if g == "MB"}
    try:
        for k, rt in c.execute("SELECT DISTINCT ikey, raw_type FROM bf.runs WHERE raw_type LIKE 'FFMB%' OR raw_type LIKE 'FFHR%'"):
            code = str(rt).split(" ")[0]
            if code == "FFMB": out.setdefault(k, "MB")
            elif code == "FFHR" and "highrise on fire" in str(rt).lower(): out[k] = "HR"
    except sqlite3.Error: pass
    return out
def backfill_meta(c, k):
    if not has_backfill(c): return None
    r = c.execute("SELECT v FROM bf.meta WHERE k=?", (k,)).fetchone()
    return int(r[0]) if r else None

BUSIEST_EXCLUDE = {"D", "DC", "AS"}   # district chiefs and EMS supervisors are not ranked as "busiest"

def unit_intervals(c, keys):
    """{(ikey, unit): (first_seen, last_seen)} for district units on the given incidents."""
    iv, keys = {}, list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for ik, u, f, l in c.execute(f"SELECT ikey,unit,first_seen,last_seen FROM unit_incidents WHERE station IS NOT NULL AND ikey IN ({','.join('?' * len(chunk))})", chunk):
            iv[(ik, u)] = (f, l)
        if has_backfill(c):
            for ik, u, f, l in c.execute(f"SELECT ikey,unit,first_seen,last_seen FROM bf.unit_incidents WHERE station IS NOT NULL AND ikey IN ({','.join('?' * len(chunk))})", chunk):
                iv.setdefault((ik, u), (f, l))
    return iv

def union_ms(spans):
    """Total length of the union of [start, end] spans, so overlapping time is counted once."""
    tot, cur_s, cur_e = 0, None, None
    for s_, e_ in sorted(x for x in spans if x[1] > x[0]):
        if cur_e is None or s_ > cur_e:
            if cur_e is not None: tot += cur_e - cur_s
            cur_s, cur_e = s_, e_
        else: cur_e = max(cur_e, e_)
    return tot + ((cur_e - cur_s) if cur_e is not None else 0)

def tour_split(spans):
    """{tour date: ms} for the union of spans, cut at every 06:30 CT shift change (DST-correct)."""
    out, cur = {}, None
    merged = []
    for s_, e_ in sorted(x for x in spans if x[1] > x[0]):
        if merged and s_ <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], e_)
        else: merged.append([s_, e_])
    for s_, e_ in merged:
        while s_ < e_:
            sd, _ = shift_date_of(s_); d = date.fromisoformat(sd) + timedelta(days=1)
            nxt = int(datetime(d.year, d.month, d.day, *SHIFT_CHANGE, tzinfo=TZ).timestamp() * 1000)
            cut = min(e_, nxt); out[sd] = out.get(sd, 0) + cut - s_; s_ = cut
    return out

def run_table(rows, wf_keys, iv=None, hosp=None, days=False):
    """Per-unit runs by shift, working fires, and hours committed for one period.
    hours = union of (a) each call's first-seen -> last-seen window for the unit in the City feed and
    (b) for A/M units, non-stale hospital visits (arrival -> drop-off from the hospital report) arriving in the period."""
    runs, spans = {}, {}
    for d in rows:
        for u in {x["u"] for x in d["units"] if x["d"]}:
            r = runs.setdefault(u, {"unit": u, "station": unit_station(u), "kind": unit_kind(u), "A": 0, "B": 0, "C": 0, "D": 0, "total": 0, "wf": 0, "mvi": 0, "mvi_sh": {"A": 0, "B": 0, "C": 0, "D": 0}})
            r[d["sh"]] = r.get(d["sh"], 0) + 1; r["total"] += 1
            if days: dd = r.setdefault("days", {}); dd[d["sd"]] = dd.get(d["sd"], 0) + 1
            if u in wf_keys.get(d["k"], ()): r["wf"] += 1
            if R.is_mvi(d.get("type")):
                r["mvi"] += 1; r["mvi_sh"][d["sh"]] = r["mvi_sh"].get(d["sh"], 0) + 1
                if days: md = r.setdefault("mvi_days", {}); md[d["sd"]] = md.get(d["sd"], 0) + 1
            to = FD.ooa_dest(u, d.get("fd"), d.get("fdn"))
            if to:
                r["ooa"] = r.get("ooa", 0) + 1; ot = r.setdefault("ooa_to", {}); ot[to] = ot.get(to, 0) + 1
            td = FD.ood_dest(u, d.get("fdd"), d.get("fddn"))   # out of District 83 (D83 units only)
            if td:
                r["ood"] = r.get("ood", 0) + 1; od = r.setdefault("ood_to", {}); od[td] = od.get(td, 0) + 1
            if iv and (d["k"], u) in iv: spans.setdefault(u, []).append(iv[(d["k"], u)])
    for v in hosp or []:
        if v["u"] in runs and re.match(r"^(A|M)\d", v["u"]): spans.setdefault(v["u"], []).append((v["a"], v["end"]))
    for u, r in runs.items():
        r["hrs"] = round(union_ms(spans.get(u, [])) / 3600000, 1)
        # Shift / day hours: the same merged time, cut at each 06:30 shift change and credited to the tour on duty,
        # so a unit's shift hours always add up to its Hrs.
        byd = tour_split(spans.get(u, []))
        r["hsh"] = {sh: round(sum(v for sd, v in byd.items() if shift_for_date(sd) == sh) / 3600000, 2) for sh in "ABCD"}
        if days: r["hday"] = {sd: round(v / 3600000, 2) for sd, v in byd.items()}
    return sorted(runs.values(), key=unit_sortkey)

def inc_table(rows):
    """v59: District 83 incidents by shift for one period (same rows and shift as run_table). An incident is one call row
    (CAD re-keys are already linked to one row by the aliases table) with at least one District 83 unit on it, so a call
    that E76, L76 and M76 all ran counts once here but as 3 runs in run_table."""
    t = {"A": 0, "B": 0, "C": 0, "D": 0, "total": 0}
    for d in rows:
        if any(x["d"] for x in d["units"]):
            t[d["sh"]] = t.get(d["sh"], 0) + 1; t["total"] += 1
    return t

OOA_FIRE = FD.OOA_PREFIXES | {"D"}   # units the out-of-area rule applies to (fire apparatus + D083), the share's denominator
def ooa_shift(rows, months=False):
    """Out-of-first-due runs by shift for one period, same rule as run_table (FD.ooa_dest: fire units only, 150 m buffer,
    D83 units only). by[sh] = [ooa runs, fire-unit runs]; list = [call ms, unit, first-due station, shift, call type, address] newest first;
    months (Year views) = {"YYYY-MM" by call time CT: {sh: [ooa, runs]}}."""
    by = {sh: [0, 0] for sh in "ABCD"}; lst = []; mon = {}
    for d in rows:
        sh = d["sh"]
        mk = datetime.fromtimestamp(d["t"] / 1000, TZ).strftime("%Y-%m") if months else None
        for u in sorted({x["u"] for x in d["units"] if x["d"]}):
            m = UNIT_RE.match(u)
            if not m or m.group(1) not in OOA_FIRE: continue
            to = FD.ooa_dest(u, d.get("fd"), d.get("fdn"))
            by.setdefault(sh, [0, 0])[1] += 1; by[sh][0] += bool(to)
            if months:
                mm = mon.setdefault(mk, {s: [0, 0] for s in "ABCD"}); mm[sh][1] += 1; mm[sh][0] += bool(to)
            if to: lst.append([d["t"], u, to, sh, d.get("type") or "", (d.get("addr") or "") + (" @ " + d["cs"] if d.get("cs") else "")])
    lst.sort(key=lambda x: (-x[0], x[1]))
    out = {"by": by, "list": lst}
    if months: out["months"] = mon
    return out

def busiest(table):
    """Per station: most runs, tiebreak hours committed; chiefs (D/DC) and EMS supervisors (AS) excluded."""
    out = {}
    for r in table:
        if (re.match(r"^[A-Z]+", r["unit"]).group(0)) in BUSIEST_EXCLUDE or not r["total"]: continue
        b = out.get(r["station"])
        if not b or (r["total"], r.get("hrs", 0)) > (b["total"], b["hrs"]): out[r["station"]] = {"unit": r["unit"], "total": r["total"], "hrs": r.get("hrs", 0)}
    return out

def hosp_spans(c, lo, hi):
    """Non-stale hospital visits (from transport.py) arriving in [lo, hi): unit, arrival, end."""
    try:
        import transport as T
        T.init(c)
        latest = c.execute("SELECT MAX(report_ts) FROM hosp_reports").fetchone()[0]
        if not latest: return []
        out = []
        for v in T._visits(c, lo, hi, latest):
            if v["stale"]: continue
            out.append({"u": v["u"], "a": v["a"], "end": v["a"] + v["m"] * 60000})
        return out
    except Exception:
        log("hospital spans error: " + traceback.format_exc()); return []

def working_fires(rows):
    wf, other_long = [], []
    for d in rows:
        # Working fire (d83rules.wf_reason): a fire box on CAD 50+ min, a District Chief + the full initial box still on
        # CAD 25 min after the call, or a structure-fire box with 2+ units on it at the same time for 60+ min. Every District 83 unit on it gets credit.
        du = [u["u"] for u in d["units"] if u["d"]]
        if not du: continue
        if d.get("why"): wf.append({**d, "long_units": du}); continue
        if d.get("cad", 0) < WORKING_FIRE_MIN_MINUTES: continue
        if "FIRE" in d["type"].upper() or "ALARM" in d["type"].upper():
            other_long.append({**d, "long_units": du})
    return wf, other_long

def live_multi_unit_wf(c):
    """Citywide incidents in the latest successful poll that are working fires under the 2+ units / 60 min rule (structure boxes) or the District Chief + full box / 25 min rule.
    Keys are the feed's own CALL_TIME|Address|CrossStreet (aliases included) so the Live ticker can match them."""
    last = c.execute("SELECT MAX(ts) FROM polls WHERE ok=1").fetchone()[0]
    if not last: return []
    out = []
    for ik, ty, ct, fs in c.execute("SELECT ikey, type, call_time, first_seen FROM incidents WHERE last_seen=?", (last,)).fetchall():
        if not R.is_fire_call(ty) or R.is_wf_excluded(ik): continue
        us = c.execute("SELECT unit, first_seen, last_seen FROM unit_incidents WHERE ikey=?", (ik,)).fetchall()
        sp = [(f, l) for _, f, l in us]
        if R.is_multi_unit_wf(ty, sp) or R.is_box_wf(ty, ct, fs, us) or R.is_support_wf(ty, us):   # rule 3, 4, or 5 (Rehab/Cascade unit)
            out.append(ik); out += [r[0] for r in c.execute("SELECT raw_key FROM aliases WHERE ikey=?", (ik,))]
    return sorted(set(out))

def live_ticker_keys(c):
    """v41: citywide structure fires in the latest successful poll with 3+ heavy apparatus ever assigned (R.is_ticker_fire).
    Feed keys (aliases included) so the Live ticker keeps a box that reached 3 heavies even after units clear."""
    last = c.execute("SELECT MAX(ts) FROM polls WHERE ok=1").fetchone()[0]
    if not last: return []
    out = []
    for ik, ty in c.execute("SELECT ikey, type FROM incidents WHERE last_seen=?", (last,)).fetchall():
        if not R.is_structure_fire(ty): continue
        us = [r[0] for r in c.execute("SELECT unit FROM unit_incidents WHERE ikey=?", (ik,))]
        if R.is_ticker_fire(ty, us):
            out.append(ik); out += [r[0] for r in c.execute("SELECT raw_key FROM aliases WHERE ikey=?", (ik,))]
    return sorted(set(out))

def years_with_data(c):
    lo, hi = c.execute("SELECT MIN(call_time), MAX(call_time) FROM incidents WHERE district=1").fetchone()
    if not lo: return []
    return list(range(datetime.fromtimestamp(lo / 1000, TZ).year, datetime.fromtimestamp(hi / 1000, TZ).year + 1))

HEADER_DAY_START = (6, 0)   # v57 header: the City's official daily fire-activity window is 06:00 to 05:59
HFD_RESPONSE_PREFIXES = set(R.STATION_PREFIXES) | {"D"}

def city_incident_count(c, a, b):
    """HFD incidents with call time in [a, b), counted like the City's official daily count: a call needs at least one
    HFD response unit (drops stand-by records and calls with only special or non-HFD units), and a call CAD re-creates
    at the same location while the first is still open, with a unit carried over, counts once."""
    lo = a - 7200000
    rows = c.execute("SELECT ikey, call_time, UPPER(COALESCE(address,'')), UPPER(COALESCE(cross_street,'')), first_seen, last_seen "
                     "FROM incidents WHERE call_time>=? AND call_time<?", (lo, b)).fetchall()
    units = {}
    for k, u in c.execute("SELECT u.ikey, u.unit FROM unit_incidents u JOIN incidents i ON i.ikey=u.ikey "
                          "WHERE i.call_time>=? AND i.call_time<?", (lo, b)):
        units.setdefault(k, set()).add(u)
    def hfd(k):
        for u in units.get(k, ()):
            m = UNIT_RE.match((u or "").strip().upper())
            if m and m.group(1) in HFD_RESPONSE_PREFIXES: return True
        return False
    by = {}
    for r in rows:
        if hfd(r[0]): by.setdefault((r[2], r[3]), []).append(r)
    n = 0
    for L in by.values():
        L.sort(key=lambda r: r[1])
        for i, y in enumerate(L):
            if y[1] < a: continue
            if not any(x[1] <= y[1] and y[4] <= (x[5] or 0) + 120000 and units[x[0]] & units[y[0]] for x in L[:i]): n += 1
    return n

def city_counts(c, now):
    """Header (v57): incidents today (since 06:00) and yesterday (06:00 to 05:59), citywide and District 83.
    District 83 uses the site's own rule (district_rows: a call with a Station 10/69/76/83 unit or D83). Counts only."""
    t = datetime.fromtimestamp(now / 1000, TZ)
    if (t.hour, t.minute) < HEADER_DAY_START: t -= timedelta(days=1)
    day = t.date(); prev = day - timedelta(days=1)
    t0 = int(datetime(day.year, day.month, day.day, *HEADER_DAY_START, tzinfo=TZ).timestamp() * 1000)
    y0 = int(datetime(prev.year, prev.month, prev.day, *HEADER_DAY_START, tzinfo=TZ).timestamp() * 1000)
    d83 = lambda a, b: len(district_rows(c, "call_time>=? AND call_time<?", (a, b)))
    start = c.execute("SELECT MIN(ts) FROM polls WHERE ok=1").fetchone()[0] or now
    return {"day": day.isoformat(), "since": "%02d:%02d" % HEADER_DAY_START,
            "city_today": city_incident_count(c, t0, now + 1), "d83_today": d83(t0, now + 1),
            "prev_day": prev.isoformat(), "city_prev": city_incident_count(c, y0, t0), "d83_prev": d83(y0, t0),
            "prev_complete": start <= y0}


def stats(c, year=None):
    """Stats for one calendar year (CT). A call belongs to the year of its call time, so a 12/31 tour that runs past
    midnight is split: calls before 00:00 count toward the old year, calls after count toward the new one."""
    now = now_ms(); cur_year = datetime.fromtimestamp(now / 1000, TZ).year
    year = year or cur_year; is_current = year == cur_year
    start = int(c.execute("SELECT v FROM meta WHERE k='logging_start'").fetchone()[0])
    y0, y1 = year_bounds(year)
    last_ok = c.execute("SELECT MAX(ts) FROM polls WHERE ok=1").fetchone()[0]
    npolls = c.execute("SELECT COUNT(*), SUM(ok=1) FROM polls").fetchone()
    district = district_rows(c, "call_time>=? AND call_time<?", (y0, y1))
    wf, other_long = working_fires(district)
    wf_keys = {w["k"]: set(w["long_units"]) for w in wf}
    iv = unit_intervals(c, [d["k"] for d in district])
    hv = hosp_spans(c, y0, y1)
    runs_periods = {"ytd": run_table(district, wf_keys, iv, hv)}
    inc_periods = {"ytd": inc_table(district)}
    ooa_periods = {"ytd": ooa_shift(district, months=True)}
    cur_sd, cur_sh = shift_date_of(now); current_week = None
    if is_current:
        # "This shift" = the whole current tour (all its calls, even ones before midnight on Jan 1);
        # "This month" = the current calendar month by call time.
        tour_rows = district_rows(c, "shift_date=?", (cur_sd,))
        twf, _ = working_fires(tour_rows)
        tiv = unit_intervals(c, [d["k"] for d in tour_rows])
        thv = [v for v in hosp_spans(c, now - 3 * 86400000, now + 1) if shift_date_of(v["a"])[0] == cur_sd]
        runs_periods["tour"] = run_table(tour_rows, {w["k"]: set(w["long_units"]) for w in twf}, tiv, thv)
        inc_periods["tour"] = inc_table(tour_rows)
        # "This week" = the Monday..Sunday tours (Mon 06:30 CT to the next Mon 06:30 CT), crossing years if needed.
        wk0, wk1, wks, wke = week_of(cur_sd)
        week_rows = district_rows(c, "shift_date>=? AND shift_date<?", (wk0, wk1))
        wwf, _ = working_fires(week_rows)
        wiv = unit_intervals(c, [d["k"] for d in week_rows])
        whv = [v for v in hosp_spans(c, wks - 86400000, now + 1) if wk0 <= shift_date_of(v["a"])[0] < wk1]
        runs_periods["week"] = run_table(week_rows, {w["k"]: set(w["long_units"]) for w in wwf}, wiv, whv, days=True)
        inc_periods["week"] = inc_table(week_rows)
        current_week = {"start_sd": wk0, "end_sd": wk1, "start": wks, "end": wke}
        n = datetime.fromtimestamp(now / 1000, TZ)
        m0 = int(datetime(n.year, n.month, 1, tzinfo=TZ).timestamp() * 1000)
        runs_periods["month"] = run_table([d for d in district if d["t"] >= m0], wf_keys, iv, [v for v in hv if v["a"] >= m0])
        inc_periods["month"] = inc_table([d for d in district if d["t"] >= m0])
        ooa_periods["week"] = ooa_shift(week_rows); ooa_periods["month"] = ooa_shift([d for d in district if d["t"] >= m0])
        # Change vs the previous week / month: unit -> area pair counts by shift, only when that period was fully logged.
        pm = (n.replace(day=1) - timedelta(days=1)); pm0 = int(datetime(pm.year, pm.month, 1, tzinfo=TZ).timestamp() * 1000)
        pw0 = (date.fromisoformat(wk0) - timedelta(days=7)).isoformat()
        for k, lo, rows_fn in (("week", wks - 7 * 86400000, lambda: district_rows(c, "shift_date>=? AND shift_date<?", (pw0, wk0))),
                               ("month", pm0, lambda: district_rows(c, "call_time>=? AND call_time<?", (pm0, m0)))):
            if lo < min(start, backfill_meta(c, "history_start") or start): continue
            pv = {}
            for t, u, to, sh, *_ in ooa_shift(rows_fn())["list"]: kk = f"{u}|{to}|{sh}"; pv[kk] = pv.get(kk, 0) + 1
            ooa_periods[k]["prev"] = pv
    # v31: polling breaks > 15 min (no data logged), for report data notes
    feed_gaps = []
    try:
        prev = None
        for (ts,) in c.execute("SELECT ts FROM polls WHERE ok=1 AND ts>=? AND ts<? ORDER BY ts", (y0, y1)):
            if prev is not None and ts - prev > 15 * 60000: feed_gaps.append([prev, ts])
            prev = ts
        # v36: label each gap; failed polls inside it = the City feed was down, no polls at all = our collector/box was down
        for gp in feed_gaps:
            bad = c.execute("SELECT COUNT(*) FROM polls WHERE ok=0 AND ts>? AND ts<?", (gp[0], gp[1])).fetchone()[0]
            bk = [r[0] for r in c.execute("SELECT ts FROM polls WHERE ok=2 AND ts>? AND ts<? ORDER BY ts", (gp[0], gp[1]))]
            if bk:   # v37: backup-logger captures inside the gap (D83 + fire calls only)
                pts = [gp[0]] + bk + [gp[1]]
                gp.append("covered by backup" if max(b - a for a, b in zip(pts, pts[1:])) <= 15 * 60000 else "partly covered by backup")
            else: gp.append("feed errors" if bad else "collector outage")
        try:
            json.dump([{"from": fmt(a), "to": fmt(b), "minutes": round((b - a) / 60000), "reason": r, "from_ms": a, "to_ms": b}
                       for a, b, r in feed_gaps], open(f"{BASE}/gaps.json", "w"), indent=1)
        except Exception: pass
    except Exception: feed_gaps = []
    g = now_ms()
    return {
        "generated": g, "feed_gaps": feed_gaps[-20:], "generated_str": fmt(g), "year": year, "is_current_year": is_current,
        "year_start": y0, "year_end": y1,
        "logging_start": start, "logging_start_str": fmt(start), "last_ok_poll": last_ok,
        "history_start": backfill_meta(c, "history_start"), "history_end": backfill_meta(c, "live_start"),
        "history_end_str": datetime.fromtimestamp(backfill_meta(c, "live_start") / 1000, TZ).strftime("%b %-d %H:%M") if backfill_meta(c, "live_start") else None,
        "last_ok_poll_str": fmt(last_ok) if last_ok else None, "polls": npolls[0], "polls_ok": npolls[1] or 0,
        "total_incidents_logged": c.execute("SELECT COUNT(*) FROM incidents WHERE call_time>=? AND call_time<?", (y0, y1)).fetchone()[0],
        "district_incidents": len(district),
        "shift_change": "%02d:%02d" % SHIFT_CHANGE, "working_fire_min": WORKING_FIRE_MIN_MINUTES,
        "multi_unit_wf_min": R.MULTI_UNIT_WF_MIN, "dc_wf_min": R.DC_WF_MIN, "support_wf_units": "RH, CC", "crew_added": manual_applied(c), "ooa_boundary_m": FD.BOUNDARY_M, "districts": FD.districts(), "wf_live": live_multi_unit_wf(c) if is_current else [], "tk_live": live_ticker_keys(c) if is_current else [], "ticker_min_heavy": R.TICKER_MIN_HEAVY,
        "non_working_fire_types": sorted(NON_WORKING_FIRE_TYPES), "mvi_re": R.MVI_RE.pattern, "fire_word_re": R.FIRE_WORD_RE.pattern, "box_words": STRUCT_RE.pattern, "box_exclude": NOT_BOX_RE.pattern,
        "current_tour": {"sd": cur_sd, "sh": cur_sh}, "current_week": current_week, "city": city_counts(c, now) if is_current else None,
        "runs": runs_periods["ytd"], "runs_periods": runs_periods, "incidents_periods": inc_periods, "ooa_periods": ooa_periods,
        "busiest": {k: busiest(v) for k, v in runs_periods.items()},
        "tour_counts": {sd: n for sd, n in sorted(__import__("collections").Counter(d["sd"] for d in district).items())},
        "working_fires": wf,
        "_district": district,
    }

FIRE_PREFIX_ORDER = ["E", "MPE", "L", "MPL", "T", "R", "RS", "RE", "D", "DC"]
EMS_PREFIX_ORDER = ["A", "M", "AS"]
def unit_kind(u):
    m = UNIT_RE.match(u); p = m.group(1) if m else ""
    return "ems" if p in EMS_PREFIX_ORDER else "fire"
def unit_sortkey(r):
    m = UNIT_RE.match(r["unit"]); p = m.group(1) if m else r["unit"]
    order = FIRE_PREFIX_ORDER + EMS_PREFIX_ORDER
    return (int(r["station"]), order.index(p) if p in order else 99, r["unit"])

TOURS_DIR = f"{BASE}/tours"
POINTS = f"{BASE}/points.json"
ARCHIVE_DIR = f"{BASE}/archive"
ARCHIVE_GRACE_MS = 6 * 3600 * 1000   # re-export a finished year until 6 h after it ends (late-clearing calls)

def write_if_changed(path, data):
    try:
        if open(path).read() == data: return
    except OSError: pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"; open(tmp, "w").write(data); os.replace(tmp, path)

# ---- v29: Station Stripped (every unit of a D83 station committed on CAD at once) ----
STRIP_GAP_MS = 15 * 60000   # polling breaks longer than this split intervals (no presence is assumed across them)
STRIP_MIN_MS = 2 * 60000    # ignore overlaps shorter than 2 minutes (one-poll artifacts)
STRIP_MIN_RUNS = 3          # a unit must have this many runs in the period to count as housed at the station
STRIP_SKIP = {"D", "AS"}    # district chief / EMS supervisor: not part of "all units" unless a station has nothing else

def _coverage(c, t0, t1):
    """Contiguous polling segments [(start, end)] in [t0, t1]; a gap > STRIP_GAP_MS between ok polls breaks a segment."""
    seg, s, prev = [], None, None
    for (ts,) in c.execute("SELECT ts FROM polls WHERE ok>=1 AND ts>=? AND ts<? ORDER BY ts", (t0, t1)):   # v37: backup captures count
        if prev is None or ts - prev > STRIP_GAP_MS:
            if s is not None: seg.append((s, prev))
            s = ts
        prev = ts
    if s is not None: seg.append((s, prev))
    return seg

def _clip(iv, seg):
    out = []
    for f, l in iv:
        for a, b in seg:
            x, y = max(f, a), min(l, b)
            if y > x: out.append((x, y))
    return out

def _union(iv):
    out = []
    for f, l in sorted(iv):
        if out and f <= out[-1][1]: out[-1] = (out[-1][0], max(out[-1][1], l))
        else: out.append((f, l))
    return out

def _intersect(a, b):
    i = j = 0; out = []
    while i < len(a) and j < len(b):
        x, y = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if y > x: out.append((x, y))
        if a[i][1] < b[j][1]: i += 1
        else: j += 1
    return out

def _slot(u):
    """E083 and MPE083 -> 'E083/MPE083' (same for L/MPL); other units unchanged."""
    m = R.UNIT_RE.match(u or "")
    if not m: return u
    p, n = m.group(1), m.group(2).zfill(3)
    heavy = R.MANPOWER.get(p, p if p in R.MANPOWER.values() else None)
    if not heavy: return u
    mp = [k for k, v in R.MANPOWER.items() if v == heavy][0]
    return f"{heavy}{n}/{mp}{n}"

def stripped(c, t0, t1):
    """{"units": {st: [units]}, "ev": [[st, start_ms, end_ms, shift_date, shift, ongoing]]} for calls in [t0, t1)."""
    seg = _coverage(c, t0 - 86400000, t1 + 86400000)
    last_poll = (c.execute("SELECT MAX(ts) FROM polls WHERE ok=1").fetchone() or [0])[0] or 0
    rows = c.execute("""SELECT ui.unit, ui.station, ui.first_seen, ui.last_seen FROM unit_incidents ui JOIN incidents i ON i.ikey=ui.ikey
                        WHERE ui.station IS NOT NULL AND i.call_time>=? AND i.call_time<?""", (t0, t1)).fetchall()
    runs, iv = {}, {}
    for u, st, f, l in rows:
        u = _slot(u)   # MPE0xx stands in for E0xx, MPL0xx for L0xx: one slot, busy when either is committed
        runs[(st, u)] = runs.get((st, u), 0) + 1; iv.setdefault(u, []).append((f, l))
    units, ev = {}, []
    for st in STATIONS:
        cand = sorted(u for (s_, u), n in runs.items() if s_ == st and n >= STRIP_MIN_RUNS)
        core = [u for u in cand if R.UNIT_RE.match(u.split("/")[0]).group(1) not in STRIP_SKIP] or cand
        units[st] = core
        if not core: continue
        cur = _union(_clip(iv[core[0]], seg))
        for u in core[1:]: cur = _intersect(cur, _union(_clip(iv[u], seg)))
        for f, l in cur:
            if l - f < STRIP_MIN_MS: continue
            sd, sh = shift_date_of(f)
            ev.append([st, f, l, sd, sh, int(l >= last_poll)])
    ev.sort(key=lambda e: -e[1])
    return {"units": units, "ev": ev}

def points_json(district, strip=None):
    units, types, locs, ui, ti, li, pts = [], [], [], {}, {}, {}, []
    def idx(m, lst, v):
        if v not in m: m[v] = len(lst); lst.append(v)
        return m[v]
    for d in district:
        dd = [x for x in d["units"] if x["d"]]
        du = [idx(ui, units, x["u"]) for x in dd]
        pts.append([d["t"], d["lat"], d["lon"], (d["km"] or "").strip(), d["sh"], idx(ti, types, d["type"]), du,
                    idx(li, locs, (d["addr"] or "") + "\u0000" + (d["cs"] or "")), [x["min"] for x in dd], d["sd"]])
    out = {"cols": ["t", "lat", "lon", "km", "sh", "type", "units", "loc", "umin", "sd"], "units": units, "types": types,
           "locs": [l.split("\u0000") for l in locs], "p": pts}
    if strip is not None: out["strip"] = strip
    return json.dumps(out, separators=(",", ":"))

def export_tours(c, district):
    """tours/<shift date>.json. A tour holds every call of that shift date regardless of calendar year (tours never split).
    Recent tours are rebuilt every poll; older ones only if their file is missing."""
    sds = {d["sd"] for d in district} | {r[0] for r in c.execute("SELECT DISTINCT shift_date FROM incidents WHERE district=1 AND call_time>=?", (now_ms() - 3 * 86400000,))}
    recent = (datetime.fromtimestamp(now_ms() / 1000, TZ) - timedelta(days=3)).date().isoformat()
    for sd in sds:
        path = f"{TOURS_DIR}/{sd}.json"
        if sd < recent and os.path.exists(path): continue
        write_if_changed(path, json.dumps({"sd": sd, "incidents": district_rows(c, "shift_date=?", (sd,))}, separators=(",", ":")))

def export_archives(c, cur_year):
    """archive/<year>/stats.json + points.json for each finished year. Automatic: written on the first poll after New Year,
    refreshed during a short grace window, then left alone. Raw data stays in incidents.db forever."""
    done = []
    for y in years_with_data(c):
        if y >= cur_year: continue
        sp = f"{ARCHIVE_DIR}/{y}/stats.json"
        fresh = False
        try: fresh = json.load(open(sp)).get("generated", 0) >= year_bounds(y)[1] + ARCHIVE_GRACE_MS
        except (OSError, ValueError): pass
        if not fresh:
            st = stats(c, y); district = st.pop("_district")
            write_if_changed(f"{ARCHIVE_DIR}/{y}/points.json", points_json(district, stripped(c, *year_bounds(y))))
            write_if_changed(sp, json.dumps(st, separators=(",", ":")))
            log(f"archived {y}: {len(district)} district incidents")
        done.append(y)
    return done

HYDRANTS = f"{BASE}/hydrants.json"
VERSION = f"{BASE}/version.json"

def build_id(tpl, sched_js):
    """Changes only when page code/config changes (template, calendar, shift constant, hydrant data), never on stats updates."""
    import hashlib
    h = hashlib.sha1(tpl.encode()); h.update(sched_js.encode()); h.update(repr(SHIFT_CHANGE).encode())
    for f in (HYDRANTS, f"{BASE}/stations.json", f"{BASE}/fdareas.json", f"{BASE}/d83area.json", f"{BASE}/neighbors.json"):
        try: h.update(open(f, "rb").read())
        except OSError: pass
    return h.hexdigest()[:12]

BOX_CHECK = [0]
def refresh_boxes():
    """Weekly refresh of the City box map (fetch_boxes.py keeps the old cache on any failure), then client files."""
    if time.time() - BOX_CHECK[0] > 6 * 3600:
        BOX_CHECK[0] = time.time()
        try:
            import fetch_boxes; fetch_boxes.main()
        except BaseException as e: log(f"box map refresh error: {e!r}")
        try:
            import fetch_districts; fetch_districts.main()
        except BaseException as e: log(f"district table refresh error: {e!r}")
    try: FD.write_client_files(BASE)
    except Exception: log("first-due files error: " + traceback.format_exc())

def refresh(c):
    refresh_boxes()
    st = stats(c)
    district = st.pop("_district", [])
    export_tours(c, district)
    try: strip = stripped(c, *year_bounds(st["year"]))
    except Exception: strip = None; log("stripped error: " + traceback.format_exc())
    write_if_changed(POINTS, points_json(district, strip))
    archived = export_archives(c, st["year"])
    try:
        import transport
        archived = list(archived) + transport.export(c, st["year"])
    except Exception: log("transport export error: " + traceback.format_exc())
    st["years"] = sorted(set(archived) | {st["year"]}, reverse=True)
    render(st)
    return st

def _ntfy():
    try:
        t = json.load(open(f"{BASE}/ntfy_topics.json")); return {"server": t["server"], "d83": t["d83"], "city": t["city"]}
    except (OSError, ValueError, KeyError): return None

def render(st):
    st.pop("_district", None)
    tpl = open(TEMPLATE).read()
    sched_js = json.dumps(SCHED, separators=(",", ":"))
    build = build_id(tpl, sched_js + json.dumps(_ntfy()))
    html = (tpl.replace('/*__BUILD__*/null', json.dumps(build)).replace("/*__STATS__*/null", json.dumps(st, separators=(",", ":")).replace("</", "<\\/"))
               .replace("/*__SCHED__*/null", sched_js)
               .replace("/*__SHIFT_CHANGE__*/null", json.dumps(list(SHIFT_CHANGE)))
               .replace("/*__NTFY__*/null", json.dumps(_ntfy())))
    for path, data in ((DASH, html), (STATS, json.dumps(st, indent=1)), (VERSION, json.dumps({"build": build}))):
        tmp = path + ".tmp"
        open(tmp, "w").write(data); os.replace(tmp, path)

def cycle(c):
    try: merge_backup(c)
    except Exception: log("backup merge error: " + traceback.format_exc())
    tm = [time.time()]; poll(c); tm.append(time.time())
    try: merge_recreated(c, now_ms() - 6 * 3600000)   # v62: re-created calls (location edited in CAD) become one incident
    except Exception: log("merge_recreated error: " + traceback.format_exc())
    try: apply_manual_units(c)
    except Exception: log("manual units error: " + traceback.format_exc())
    if os.path.exists(f"{BASE}/PUSH_ENABLED"):   # ntfy push alerts: off for now (create PUSH_ENABLED to turn back on)
        try:
            import push_alerts; push_alerts.run(c)
        except Exception as e: log(f"push alerts error: {e!r}")
    try:
        import transport; transport.poll(c)
    except Exception as e: log(f"transport poll error: {e!r}")
    tm.append(time.time())
    try: refresh(c)
    except Exception: log("render error: " + traceback.format_exc())
    tm.append(time.time())
    if tm[-1] - tm[0] > 120:   # v39: tells a slow/hung step apart from a frozen box (the watchdog logs freezes)
        log(f"slow cycle {tm[-1] - tm[0]:.0f}s: feed {tm[1] - tm[0]:.0f}s, transport {tm[2] - tm[1]:.0f}s, render {tm[3] - tm[2]:.0f}s")

def main():
    once = "--once" in sys.argv
    import fcntl
    global _LOCK
    _LOCK = open(f"{BASE}/collector.lock", "w")
    try: fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("collector already running (lock held); exiting"); return
    c = db(); log(f"collector start pid={os.getpid()} once={once}")
    try: reclassify(c)
    except Exception: log("reclassify error: " + traceback.format_exc())
    open(f"{BASE}/collector.pid", "w").write(str(os.getpid()))
    while True:
        t0 = time.time()
        try: cycle(c)
        except Exception:
            log("cycle error: " + traceback.format_exc())
            try: c.close()
            except Exception: pass
            c = db()
        try: open(f"{BASE}/collector.hb", "w").write(str(int(time.time())))   # v36: watchdog freshness check
        except Exception: pass
        if once: break
        time.sleep(max(5, POLL_SECONDS - (time.time() - t0)))

if __name__ == "__main__":
    main()
