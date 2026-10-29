"""HFD Units at Hospital -> transport history + transport.json.

Source: https://houstontx.gov/fire/HFDUnitsatHospital.pdf (embedded by https://houstontx.gov/fire/ambulances-pdf.html).
It is a live snapshot only (SSRS report, regenerated about every minute): one row per HFD unit currently listed at a
hospital with hospital name, unit, transport-arrived date/time and elapsed time. No history is published, so we build it:
every poll stores the snapshot; a 'visit' = (unit, arrived) and it ends when the unit drops off the report.
Nothing here is estimated: counts are visits seen, times are arrived -> last report that still listed the unit (+-2 min).
"""
import json, os, re, subprocess, urllib.request, statistics
from datetime import datetime, timedelta
import collector as C

SRC_PAGE = "https://houstontx.gov/fire/ambulances-pdf.html"
SRC_PDF = "https://houstontx.gov/fire/HFDUnitsatHospital.pdf"
OUT = f"{C.BASE}/transport.json"
LONG_MIN = 90          # v60: a District 83 unit listed at a hospital longer than 1:30 is logged as a long stay
LONG_KEEP = 150        # newest long stays kept per period in transport.json (the counts cover all of them)
STALE_MIN = 4 * 60     # listed > 4 h: record not cleared in CAD; dropped from the now list, excluded from times/counts
RUN_RE = re.compile(r"Report run on:\s*(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)")
TOT_RE = re.compile(r"Total Count of Units at Hospitals\s*-\s*(\d+)")
ROW = r"(?P<u>[A-Z]{1,3}\d{1,4})\s+(?P<dt>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s+(?P<dur>\d+:\d{2})\s*$"
HOSP_RE = re.compile(r"^(?P<h>\S.*?)\s{2,}(?P<n>\d+)\s+" + ROW)
CONT_RE = re.compile(r"^\s+" + ROW)

def _ms(s):
    return int(datetime.strptime(re.sub(r"\s+", " ", s), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=C.TZ).timestamp() * 1000)

class BadPDF(ValueError):
    """The City returned something that is not a complete PDF (empty, an HTML error page, or a file cut off while the
    report was being regenerated). Skipped quietly; the next poll a minute later normally gets a good copy."""

def _timeout(*_): raise TimeoutError("hospital PDF fetch exceeded 45 s hard cap")

def fetch_pdf():
    """v39: one fetch with a hard 45 s cap (connect + slow-drip read) and a basic sanity check."""
    import signal
    req = urllib.request.Request(SRC_PDF + f"?_={int(datetime.now().timestamp())}", headers={"User-Agent": "Mozilla/5.0 (District83 dashboard)", "Cache-Control": "no-cache"})
    old = signal.signal(signal.SIGALRM, _timeout); signal.alarm(45)
    try:
        with urllib.request.urlopen(req, timeout=20) as r: pdf = r.read()
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old)
    if len(pdf) < 200 or not pdf.lstrip()[:5] == b"%PDF-": raise BadPDF(f"not a PDF ({len(pdf)} bytes, starts {pdf[:12]!r})")
    if b"%%EOF" not in pdf[-2048:]: raise BadPDF(f"truncated PDF ({len(pdf)} bytes, no %%EOF)")
    return pdf

def parse(pdf_bytes):
    try:
        txt = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf_bytes, capture_output=True, check=True, timeout=30).stdout.decode("utf-8", "replace")
    except subprocess.CalledProcessError as e:
        raise BadPDF(f"pdftotext exit {e.returncode}: {(e.stderr or b'').decode('utf-8', 'replace').strip()[:120]}")
    except subprocess.TimeoutExpired: raise BadPDF("pdftotext timed out")
    m = RUN_RE.search(txt); t = TOT_RE.search(txt)
    if not m: raise ValueError("no 'Report run on' line")
    rows, hosp = [], None
    for line in txt.splitlines():
        mh = HOSP_RE.match(line)
        if mh: hosp = mh.group("h").strip(); mm = mh
        else:
            mm = CONT_RE.match(line)
            if not mm or not hosp: continue
        rows.append((hosp, mm.group("u"), _ms(mm.group("dt"))))
    return _ms(m.group(1)), (int(t.group(1)) if t else None), rows

def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS hosp_reports(report_ts INTEGER PRIMARY KEY, fetched_ts INTEGER, declared INTEGER, parsed INTEGER, recent INTEGER);
    CREATE TABLE IF NOT EXISTS hosp_visits(unit TEXT, arrived INTEGER, hospital TEXT, first_report INTEGER, last_report INTEGER, PRIMARY KEY(unit, arrived));
    CREATE INDEX IF NOT EXISTS hv_arr ON hosp_visits(arrived);""")

def poll(c):
    init(c)
    # v39: a bad/empty/cut-off PDF (pdftotext CalledProcessError) is retried once after 5 s, then this poll is skipped.
    # Never raises for that case; a summary line is logged at most every 30 min instead of one error per minute.
    import time
    err = None
    for i in range(2):
        try:
            rts, declared, rows = parse(fetch_pdf()); err = None; break
        except (BadPDF, ValueError, OSError, TimeoutError) as e:
            err = e
            if i == 0: time.sleep(5)
    if err is not None:
        _SKIPS.append(f"{err!r}"[:160]); now = time.time()
        if now - _SKIP_LOG[0] >= 1800:
            C.log(f"transport: skipped {len(_SKIPS)} bad hospital-report fetch(es) since last note; latest: {_SKIPS[-1]}")
            _SKIPS.clear(); _SKIP_LOG[0] = now
        return
    if c.execute("SELECT 1 FROM hosp_reports WHERE report_ts=?", (rts,)).fetchone(): return
    recent = sum(1 for h, u, a in rows if (rts - a) / 60000 <= STALE_MIN)
    c.execute("INSERT INTO hosp_reports VALUES(?,?,?,?,?)", (rts, C.now_ms(), declared, len(rows), recent))
    for h, u, a in rows:
        c.execute("""INSERT INTO hosp_visits VALUES(?,?,?,?,?) ON CONFLICT(unit, arrived) DO UPDATE SET last_report=excluded.last_report, hospital=excluded.hospital""",
                  (u, a, h, rts, rts))
    c.commit()
    if declared is not None and declared != len(rows): C.log(f"transport: parsed {len(rows)} rows but report says {declared}")

_SKIPS, _SKIP_LOG = [], [0.0]

def _med(v): return round(statistics.median(v)) if v else None

def _visits(c, lo, hi, latest):
    out = []
    for u, a, h, fr, lr in c.execute("SELECT unit, arrived, hospital, first_report, last_report FROM hosp_visits WHERE arrived>=? AND arrived<? ORDER BY arrived", (lo, hi)):
        done = lr < latest
        m = round(((lr if done else latest) - a) / 60000)
        out.append({"u": u, "h": h, "a": a, "m": m, "done": done, "stale": m > STALE_MIN, "st": C.unit_station(u)})
    return out

def _long(d):
    """v60: long stays = District 83 visits over LONG_MIN (1:30) at a hospital, newest first, with the unit's shift (tour of the arrival).
    The visits themselves are persisted in hosp_visits (merged by merge_db.py), so this log survives handoffs and box merges.
    A unit still listed counts as soon as it passes 1:30 (done = False, time so far)."""
    ls = []
    for v in d:
        if v["m"] <= LONG_MIN: continue
        sd, sh = C.shift_date_of(v["a"])
        ls.append({"u": v["u"], "h": v["h"], "a": v["a"], "m": v["m"], "sh": sh, "sd": sd, "done": v["done"]})
    ls.sort(key=lambda r: -r["a"])
    by = {x: 0 for x in "ABCD"}
    for r in ls: by[r["sh"]] = by.get(r["sh"], 0) + 1
    return {"min": LONG_MIN, "n": len(ls), "by": by, "list": ls[:LONG_KEEP]}

def _period(vs):
    """Per-unit and per-hospital summary for District 83 units (stale records excluded)."""
    d = [v for v in vs if v["st"] and not v["stale"]]
    units = {}; by = {x: {"n": 0, "_m": []} for x in "ABCD"}
    for v in d:
        r = units.setdefault(v["u"], {"u": v["u"], "st": v["st"], "n": 0, "A": 0, "B": 0, "C": 0, "D": 0, "days": {}, "_m": []})
        sd, sh = C.shift_date_of(v["a"])   # the tour (06:30-06:30 CT) the arrival falls in, and the shift that worked it
        r["n"] += 1; r[sh] = r.get(sh, 0) + 1; r["days"][sd] = r["days"].get(sd, 0) + 1
        b = by.setdefault(sh, {"n": 0, "_m": []}); b["n"] += 1
        if v["done"]: r["_m"].append(v["m"]); b["_m"].append(v["m"])
    for b in by.values(): b["med"] = _med(b.pop("_m"))
    for r in units.values():
        ms = r.pop("_m"); r["med"] = _med(ms); r["max"] = max(ms) if ms else None
    hosp = {}
    for v in d:
        r = hosp.setdefault(v["h"], {"h": v["h"], "n": 0, "_m": []}); r["n"] += 1
        if v["done"]: r["_m"].append(v["m"])
    for r in hosp.values(): r["med"] = _med(r.pop("_m"))
    return {"n": len(d), "long": _long(d), "med": _med([v["m"] for v in d if v["done"]]), "by": by, "units": sorted(units.values(), key=lambda r: C.unit_sortkey({"unit": r["u"], "station": r["st"]})), "hosp": sorted(hosp.values(), key=lambda r: (-r["n"], r["h"]))}

def _city(vs):
    hosp = {}
    for v in vs:
        if v["stale"] or not v["done"]: continue
        r = hosp.setdefault(v["h"], {"h": v["h"], "n": 0, "_m": []}); r["n"] += 1; r["_m"].append(v["m"])
    for r in hosp.values():
        ms = sorted(r.pop("_m")); r["med"] = _med(ms); r["p90"] = ms[min(len(ms) - 1, int(0.9 * len(ms)))]
    return sorted(hosp.values(), key=lambda r: (-r["n"], r["h"]))

def build(c, year):
    init(c)
    now = C.now_ms(); cur = datetime.fromtimestamp(now / 1000, C.TZ).year
    lo, hi = C.year_bounds(year)
    first = c.execute("SELECT MIN(report_ts) FROM hosp_reports").fetchone()[0]
    last = c.execute("SELECT report_ts, fetched_ts, declared, parsed FROM hosp_reports ORDER BY report_ts DESC LIMIT 1").fetchone()
    if not last: return None
    latest = last[0] if year == cur else min(last[0], hi + 86400000)
    since = max(lo, first)   # counts only cover what the logger saw
    vs = _visits(c, lo, min(hi, now + 1), latest)   # every visit the report showed us with arrival in the year
    out = {"generated": now, "generated_str": C.fmt(now), "year": year, "since": since, "since_str": C.fmt(since), "stale_min": STALE_MIN,
           "source": SRC_PAGE, "pdf": SRC_PDF, "periods": {"ytd": _period(vs)}, "city_year": _city(vs)}
    if year == cur:
        rts, fts, declared, parsed = last
        out.update(report_ts=rts, report_str=C.fmt(rts), fetched_str=C.fmt(fts), declared=declared, parsed=parsed)
        listed = [v for v in _visits(c, 0, now + 86400000, rts) if not v["done"]]
        units_seen = {}
        for v in sorted(listed, key=lambda v: -v["a"]): units_seen.setdefault(v["u"], v["a"])
        # drop records not cleared in CAD: listed > STALE_MIN, or the unit is listed again at a newer arrival
        out["dropped"] = sum(1 for v in listed if v["stale"] or units_seen[v["u"]] != v["a"])
        out["now"] = sorted([{"h": v["h"], "u": v["u"], "a": v["a"], "m": v["m"], "s": 0, "d": v["st"]} for v in listed
                             if not (v["stale"] or units_seen[v["u"]] != v["a"])], key=lambda r: (r["h"], r["a"]))
        sd, sh = C.shift_date_of(now)
        tour = [v for v in _visits(c, now - 2 * 86400000, now + 1, rts) if C.shift_date_of(v["a"])[0] == sd]
        mstart = int(datetime.fromtimestamp(now / 1000, C.TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        out["periods"]["tour"] = _period(tour); out["periods"]["month"] = _period([v for v in vs if v["a"] >= mstart])
        out["tour"] = {"sd": sd, "sh": sh}
        # This Week = the Monday..Sunday tours (Mon 06:30 CT to the next Mon 06:30 CT), same as Run Counts (collector.week_of)
        wk0, wk1, wks, wke = C.week_of(sd)
        out["periods"]["week"] = _period([v for v in _visits(c, wks - 86400000, now + 1, rts) if wk0 <= C.shift_date_of(v["a"])[0] < wk1])
        out["week"] = {"start_sd": wk0, "end_sd": wk1, "start": wks, "end": wke}
        out["city24"] = _city(_visits(c, now - 86400000, now + 1, rts))
        # citywide units at hospitals, hourly average of recent (<= 6 h) listings over the last 24 h
        hrs = {}
        for ts, rec in c.execute("SELECT report_ts, recent FROM hosp_reports WHERE report_ts>=?", (now - 24 * 3600000,)):
            hrs.setdefault(ts // 3600000 * 3600000, []).append(rec)
        out["trend"] = [[h, round(sum(v) / len(v), 1)] for h, v in sorted(hrs.items())]
        out["recent"] = [{"u": v["u"], "h": v["h"], "a": v["a"], "m": v["m"], "done": v["done"]} for v in
                         sorted([v for v in _visits(c, now - 7 * 86400000, now + 1, rts) if v["st"] and not v["stale"]], key=lambda v: -v["a"])[:25]]
    return out

def years(c):
    init(c)
    lo, hi = c.execute("SELECT MIN(report_ts), MAX(report_ts) FROM hosp_reports").fetchone()
    if lo is None: return []
    y = lambda ms: datetime.fromtimestamp(ms / 1000, C.TZ).year
    return list(range(y(lo), y(hi) + 1))

def export(c, cur_year):
    t = build(c, cur_year)
    if t: C.write_if_changed(OUT, json.dumps(t, separators=(",", ":")))
    done = []
    for y in years(c):
        if y >= cur_year: continue
        p = f"{C.ARCHIVE_DIR}/{y}/transport.json"; fresh = False
        try: fresh = json.load(open(p)).get("generated", 0) >= C.year_bounds(y)[1] + C.ARCHIVE_GRACE_MS
        except (OSError, ValueError): pass
        if not fresh:
            a = build(c, y)
            if a: C.write_if_changed(p, json.dumps(a, separators=(",", ":"))); C.log(f"archived transport {y}")
        done.append(y)
    return done
