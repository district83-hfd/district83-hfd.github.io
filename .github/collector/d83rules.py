"""Shared District 83 rules (used by collector.py, alert_check.py, push_alerts.py; mirrored in dashboard_template.html).
Keep the JS copies (unitStation, isFireCall) in the template in sync with this file."""
import re

STATIONS = ("10", "69", "76", "83")
# Station units: these prefixes with a District 83 station number count as District 83.
STATION_PREFIXES = {"E", "L", "T", "TW", "SQ", "R", "RS", "RE", "A", "M", "AS", "MPE", "MPL"}   # RE010 = Station 10 rescue truck
# MPE/MPL = manpower engine/ladder: small unit staffed when the station's engine/ladder is out of service.
# MPE0xx stands in for E0xx and MPL0xx for L0xx (Station Stripped treats each pair as one slot).
MANPOWER = {"MPE": "E", "MPL": "L"}
# District chief: only D83 (District 83's own chief) counts; it is credited to Station 83.
CHIEF_UNITS = {("D", "83"): "83"}
UNIT_RE = re.compile(r"^([A-Z]+)0*(\d+)$")

def unit_station(u):
    """Station number ('10','69','76','83') a unit belongs to, or None. MD, SF, PG, CAT, SEMC, HM, FI, other D... -> None."""
    m = UNIT_RE.match((u or "").strip().upper())
    if not m: return None
    p, n = m.group(1), str(int(m.group(2)))
    if (p, n) in CHIEF_UNITS: return CHIEF_UNITS[(p, n)]
    return n if p in STATION_PREFIXES and n in STATIONS else None

# x11 codes: 111 FIRE ... 811 FIRE = additional alarms called (code shown as is)
MULTI_RE = re.compile(r"^\s*([1-8])11\s+FIRE\b")
STRUCT_RE = re.compile(r"\b(HOUSE|HOME|RESIDENTIAL|RESIDENCE|DWELLING|COMMERCIAL|BUSINESS|STORE|SHOP|RESTAURANT|APARTMENT|APTS?|BUILDING|BLDG|STRUCTURE|HIGH[ -]?RISE|TOWNHOMES?|TOWNHOUSE|CONDO|DUPLEX|WAREHOUSE|INDUSTRIAL|CHURCH|SCHOOL|HOSPITAL|HOTEL|MOTEL)\b")
NOT_BOX_RE = re.compile(r"ALARM|STAND-?BY|FIRE EVENT|INVESTIGAT|CHECK|WATCH|DETECTOR")
# A fire call needs a fire word; BURN alone (burn injury) is not one, BURNING is.
FIRE_WORD_RE = re.compile(r"\b(FIRE|FIRES|SMOKE|FLAMES?|EXPLOSION|EXPLOSIVE|BURNING|ON FIRE)\b")
# Not a fire call (unless an x11 code appears): alarms, detectors, water flow, stand-by, investigations, checks,
# watches, CO alarms, gas leaks/odors, burn injuries.
NON_FIRE_RE = re.compile(r"ALARM|DETECTOR|WATER ?FLOW|STAND-?BY|INVESTIGAT|\bCHECK\b|\bCK\b|WATCH|CARBON MONOXIDE|\bCO\b|"
                         r"GAS LEAK|GAS ODOR|ODOR OF GAS|NATURAL GAS|\bBURNS\b|BURN (INJ|PAT|VICT)")

def norm(t): return (t or "").upper().strip()
# v30: motor vehicle incidents (crashes, pedestrian/motorcycle struck). Vehicle fires stay Fire (any fire word wins).
MVI_RE = re.compile(r"MOTOR ?VEHICLE|\bMV[IA]\b|VEHICLE (ACCIDENT|INCIDENT|COLLISION|CRASH)|MAJOR ACCIDENT|AUTO[ -]?PED|PEDESTRIAN|MOTORCYCLE|\bCRASH\b|ROLLOVER|EXTRICAT")
def is_mvi(t): t = norm(t); return bool(MVI_RE.search(t)) and not FIRE_WORD_RE.search(t)
def is_multi(t): return bool(MULTI_RE.match(norm(t)))
def is_fire_box(t):
    t = norm(t)
    return is_multi(t) or ("FIRE" in t and bool(STRUCT_RE.search(t)) and not NOT_BOX_RE.search(t))
def is_fire_call(t):
    """Real fire call: x11 code, structure fire box, or any fire-worded type (vehicle, grass, trash, dumpster, FIRE EVENT...)
    that is not an alarm/check/stand-by style call. Alarm level is deliberately NOT used (every box is level 1)."""
    t = norm(t)
    if is_multi(t) or is_fire_box(t): return True
    return bool(FIRE_WORD_RE.search(t)) and not NON_FIRE_RE.search(t)
def alarm_rank(t):
    """(level, coded): level from the x11 code (111=1 .. 811=8), floor 1 for any fire box, else 0.
    coded=1 when an x11 code is present, so HOUSE FIRE (1,0) -> 111 FIRE (1,1) still counts as going up."""
    t = norm(t); m = MULTI_RE.match(t)
    if m: return (int(m.group(1)), 1)
    return (1, 0) if is_fire_box(t) else (0, 0)

# Working fire, rule 3 (added 2026-10-21): any fire call with 2+ units on it at the same time for more than 60 minutes.
# The feed has no on-scene status, so "on it" = the unit is listed on the incident in the CAD feed; each unit's span is
# first_seen -> last_seen from our polls. We count the total time with 2+ unit spans overlapping (not time since call),
# so a long single-unit watch with a brief second unit does not qualify.
MULTI_UNIT_WF_MIN = 60
WF_MIN = 50   # rule 1: fire box (structure types + x11 codes) on CAD 50+ min from call time (v44, 2026-10-23: was 45; tuned on the 5" hose list)

def overlap2_ms(spans):
    """Total ms during which at least two of the [start, end] spans overlap."""
    ev = []
    for s, e in spans:
        if s is not None and e is not None and e > s: ev += [(s, 1), (e, -1)]
    ev.sort(key=lambda x: (x[0], x[1]))   # ends before starts at the same instant
    tot, n, last = 0, 0, None
    for t, d in ev:
        if n >= 2 and last is not None: tot += t - last
        n += d; last = t
    return tot

def is_multi_unit_wf(t, spans):
    # v44 (2026-10-23): structure-fire boxes only (is_fire_box); no hose-list working fire was ever a non-structure call
    return is_fire_box(t) and overlap2_ms(spans) > MULTI_UNIT_WF_MIN * 60000

# Working fire, rule 4 (added 2026-10-21, "the full box that got dispatched"): a District Chief (D or DC unit,
# any district) AND the initial box assignment are still on CAD 20 minutes after the call time AND 20 minutes after we
# first saw the call, whichever is later (v21, 2026-10-22: the City feed can list a call 15+ min late, and a box that is
# only "still on" a few minutes after we first see it proves nothing - Clarewood Dr 10/22 06:23 was flagged that way).
#  - initial box = units listed when we first see the call, or within BOX_WINDOW_MIN after (our polls are 2 min apart;
#    the City feed shows calls ~5-10 min after call time)
#  - it needs BOX_MIN_UNITS+ fire apparatus (E, L, T, TW, SQ, R, RS, RE); ambulances, medics, EMS supervisors and other
#    units are not part of the "still on" check because they leave to transport or are support units
#  - at the mark, all of those fire units are still listed (at most BOX_MAX_DROPPED released) and a chief is listed
DC_WF_MIN = 25   # v44 (2026-10-23): was 20; tuned on the 5" hose list
BOX_WINDOW_MIN = 5
BOX_MIN_UNITS = 3
BOX_MAX_DROPPED = 1
CHIEF_RE = re.compile(r"^(D|DC)0*\d+$")
BOX_FIRE_RE = re.compile(r"^(E|L|T|TW|SQ|R|RS|RE|MPE|MPL)0*\d+$")
def is_chief(u): return bool(CHIEF_RE.match((u or "").strip().upper()))

def box_wf_detail(call_time, inc_first_seen, uspans):
    """(qualifies, box fire units, still on at the mark, chief on at the mark). uspans: [(unit, first_seen, last_seen)]."""
    fs = inc_first_seen
    if fs is None: return False, 0, 0, False
    t0 = call_time if call_time and call_time <= fs else fs
    if t0 + DC_WF_MIN * 60000 < fs: return False, 0, 0, False   # first seen more than DC_WF_MIN after the call: no initial box to judge
    mark = max(t0, fs) + DC_WF_MIN * 60000                  # DC_WF_MIN after the call AND after first sighting (feed lag can't trigger it)
    box = [(u, f, l) for u, f, l in uspans if f is not None and f <= fs + BOX_WINDOW_MIN * 60000 and BOX_FIRE_RE.match((u or "").upper())]
    on = sum(1 for u, f, l in box if l is not None and l >= mark)
    chief = any(is_chief(u) and f <= mark + 2 * 60000 and l is not None and l >= mark for u, f, l in uspans)
    ok = chief and len(box) >= BOX_MIN_UNITS and len(box) - on <= BOX_MAX_DROPPED
    return ok, len(box), on, chief

def is_box_wf(t, call_time, inc_first_seen, uspans):
    return is_fire_call(t) and box_wf_detail(call_time, inc_first_seen, uspans)[0]

# Working fire, rule 5 (added 2026-10-23, v38): a Rehab (RH) or Cascade/air (CC) unit assigned to a fire call is sent only
# to working fires, so it tags the call immediately. Exact prefixes only (RH008, CC015...); HR, R, RE, SC etc. never match.
# Fire calls only (is_fire_call): EMS, alarms, checks and other non-fire calls with RH/CC on them are not tagged.
SUPPORT_WF_RE = re.compile(r"^(RH|CC)0*\d+$")
def is_support_unit(u): return bool(SUPPORT_WF_RE.match((u or "").strip().upper()))
def is_support_wf(t, units):
    """units: unit ids, or (unit, first_seen, last_seen) tuples."""
    return is_fire_call(t) and any(is_support_unit(u[0] if isinstance(u, (tuple, list)) else u) for u in (units or ()))

# v41 citywide fire ticker (owner, 2026-10-23): STRUCTURE fires only (structure-fire box types + x11 FIRE codes, i.e.
# is_fire_box) that have had 3+ HEAVY apparatus assigned. Heavy = engines (E), ladders/trucks (L), towers (T, TW),
# quints (Q) and MPE/MPL manpower units. Never squads, ambulances/medics, chiefs, RE, SF, RH/CC or any other unit.
# Units count once each, ever assigned (CAD listing), so a box stays on the ticker once it has reached 3.
TICKER_HEAVY_RE = re.compile(r"^(E|L|T|TW|Q|MPE|MPL)0*\d+$")
TICKER_MIN_HEAVY = 3
def is_structure_fire(t): return is_fire_box(t)
def is_heavy(u): return bool(TICKER_HEAVY_RE.match((u or "").strip().upper()))
def heavy_count(units):
    """units: unit ids, or (unit, ...) tuples."""
    return len({(u[0] if isinstance(u, (tuple, list)) else u or "").strip().upper() for u in (units or ()) if is_heavy(u[0] if isinstance(u, (tuple, list)) else u)})
def is_ticker_fire(t, units): return is_structure_fire(t) and heavy_count(units) >= TICKER_MIN_HEAVY

# Manual exclusions: calls removed from Working Fires by hand (wf_exclude.json next to this file).
# Each entry: {"ikey": "<incident key>", "note": "..."} ; an "ikey" may also be the feed's CALL_TIME|Address|CrossStreet.
import json as _json, os as _os
WF_EXCLUDE_FILE = _os.path.join(_os.environ.get("D83_BASE", _os.path.dirname(_os.path.abspath(__file__))), "wf_exclude.json")
_wfx = {"mt": None, "keys": frozenset()}
def wf_excluded_keys():
    try: mt = _os.path.getmtime(WF_EXCLUDE_FILE)
    except OSError: return frozenset()
    if mt != _wfx["mt"]:
        try: _wfx["keys"] = frozenset(str(e["ikey"] if isinstance(e, dict) else e) for e in _json.load(open(WF_EXCLUDE_FILE)).get("exclude", []))
        except (OSError, ValueError, KeyError, TypeError, AttributeError): _wfx["keys"] = frozenset()
        _wfx["mt"] = mt
    return _wfx["keys"]
def is_wf_excluded(ikey): return ikey is not None and str(ikey) in wf_excluded_keys()

def wf_reason(t, cad_min, spans, uspans=None, call_time=None, inc_first_seen=None, ikey=None):
    """Why a call is a working fire, or None. Rule 1 checked first so existing working fires keep their reason.
    spans: [(first_seen, last_seen)] per unit; rule 4 also needs uspans [(unit, first_seen, last_seen)], the call time
    and when the incident was first seen; rule 5 (RH/CC unit on a fire call) needs uspans. A call listed in wf_exclude.json (by ikey) is never a working fire."""
    if is_wf_excluded(ikey): return None
    if is_fire_box(t) and cad_min >= WF_MIN: return f"fire box on CAD {WF_MIN}+ min"
    if uspans and is_support_wf(t, uspans): return "Rehab or Cascade unit assigned"
    if uspans and is_box_wf(t, call_time, inc_first_seen, uspans): return f"District Chief + full box on CAD {DC_WF_MIN}+ min"
    if is_multi_unit_wf(t, spans): return f"2+ units on CAD {MULTI_UNIT_WF_MIN}+ min"
    return None
