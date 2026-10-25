#!/usr/bin/env python3
"""District 83 collector running on GitHub Actions (v56). Same code and the same output as the old box logger:
polls the City feed every 60 s, renders the dashboard, and publishes it to this repo + Pages.

Run by .github/workflows/collector.yml as several "segments" (one step each) so that state can be check-pointed
between them. Usage: loop.py <segment_minutes>
  D83_BASE   work dir (code + private state, outside git); site/ under it is the repo checkout
  RUN_START  epoch seconds when the job started; HARD_MIN / NEXT_MIN = when to stop / start the next run
Publishing (same rules as the box publish.sh): push when the site fingerprint changes (at least MIN_GAP apart) or
every HEARTBEAT s; pull first (backup-logger commits), never force; then start the Pages deploy by workflow_dispatch
(pushes made with the built-in token do not trigger workflows) unless one is already queued/running.
Handoff: at NEXT_MIN the run starts its successor ("handoff" run). When this run sees a newer handoff run in
progress it stops, the job saves state, and the successor restores it."""
import calendar, json, os, shutil, subprocess, sys, time, traceback, glob, urllib.request
BASE = os.environ["D83_BASE"]; SITE = os.path.join(BASE, "site"); sys.path.insert(0, BASE)
REPO = os.environ["GITHUB_REPOSITORY"]; RUN_ID = int(os.environ["GITHUB_RUN_ID"])
START = float(os.environ.get("RUN_START") or time.time())
HARD_END = START + float(os.environ.get("HARD_MIN", "345")) * 60
NEXT_AT = START + float(os.environ.get("NEXT_MIN", "325")) * 60
DRY = os.environ.get("DRY_RUN") == "true"; HANDOFF = os.environ.get("HANDOFF") == "true"
MIN_GAP, HEARTBEAT, POLL, MAX_HOLD, STUCK = 120, 420, 60, 180, 300
ST_FILE = os.path.join(BASE, "loop_state.json")
os.environ.setdefault("TZ", "America/Chicago"); time.tzset()

def log(m): print(time.strftime("%F %T %Z ") + m, flush=True)
def sh(*a, timeout=120, **k):
    return subprocess.run(a, capture_output=True, text=True, timeout=timeout, **k)
def gh(path, *extra, timeout=30):
    r = sh("gh", "api", *extra, path, timeout=timeout)
    if r.returncode: raise RuntimeError(f"gh api {path.split('?')[0]}: {(r.stderr or r.stdout).strip()[:200]}")
    return json.loads(r.stdout) if r.stdout.strip() else None
def git(*a, timeout=120): return sh("git", "-C", SITE, *a, timeout=timeout)

S = {"last_push": 0, "last_fp": "", "next_started": False, "stop": "", "dispatched_sha": "", "hold_since": 0,
     "last_unstick": 0, "last_ctl": 0, "inputs_check": 0, "inputs_id": "", "pushes": 0, "deploys": 0, "polls": 0}
if os.path.exists(ST_FILE): S.update(json.load(open(ST_FILE)))
def save_state(): json.dump(S, open(ST_FILE, "w"))

# ---------- publishing (mirrors publish.sh) ----------
COPY = ["stats.json", "version.json", "points.json", "transport.json", "fdareas.json", "d83area.json", "neighbors.json"]
def stage():
    shutil.copyfile(f"{BASE}/District83_Dashboard.html", f"{SITE}/index.html")
    for f in COPY:
        if os.path.exists(f"{BASE}/{f}"): shutil.copyfile(f"{BASE}/{f}", f"{SITE}/{f}")
    os.makedirs(f"{SITE}/tours", exist_ok=True)
    for f in glob.glob(f"{BASE}/tours/*.json"): shutil.copyfile(f, f"{SITE}/tours/{os.path.basename(f)}")
    if os.path.isdir(f"{BASE}/archive"): shutil.copytree(f"{BASE}/archive", f"{SITE}/archive", dirs_exist_ok=True)
def fingerprint():
    r = sh(sys.executable, f"{BASE}/site_fp.py", SITE, timeout=60); return r.stdout.strip()
def pull():
    if git("fetch", "-q", "origin", "main", timeout=60).returncode: return False
    if git("rev-list", "--count", "HEAD..origin/main").stdout.strip() in ("", "0"): return True
    if git("rebase", "-q", "-X", "theirs", "origin/main").returncode == 0: return True
    git("rebase", "--abort")
    return git("merge", "-q", "--no-edit", "-X", "ours", "-m", "merge remote update (keep collector data)", "origin/main").returncode == 0
def stats_fresh():
    """Box rule: ping only while the feed itself is being read (last good poll under 10 min old)."""
    try: return time.time() - json.load(open(f"{BASE}/stats.json"))["last_ok_poll"] / 1000 < 600
    except Exception: return False
def hc_ping():
    u = os.environ.get("HC_PING_URL")
    if not u or not stats_fresh(): return
    try: urllib.request.urlopen(u, timeout=10).read()
    except Exception as e: log(f"!!! healthchecks ping failed: {type(e).__name__}")
def publish():
    now = time.time(); stage(); fp = fingerprint()
    why = "data changed" if fp and fp != S["last_fp"] else ("heartbeat" if now - S["last_push"] >= HEARTBEAT else "")
    if not why or now - S["last_push"] < MIN_GAP: return
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode:
        git("commit", "-qm", "update " + time.strftime("%F %H:%M %Z"))
    if DRY: S["last_push"], S["last_fp"] = now, fp; log(f"dry run: would push ({why})"); return
    for i in range(3):
        pull()
        r = git("push", "-q", "origin", "HEAD:main", timeout=120)
        if r.returncode == 0:
            S["last_push"], S["last_fp"] = time.time(), fp; S["pushes"] += 1
            log(f"pushed ({why}) {git('rev-parse', '--short', 'HEAD').stdout.strip()}"); hc_ping(); return
        log(f"!!! push failed (try {i + 1}): {r.stderr.strip()[:200]}"); time.sleep(5 * (i + 1))
    S["last_push"] = now - MIN_GAP + 60

def active(wf, per=15):
    runs = gh(f"repos/{REPO}/actions/workflows/{wf}/runs?per_page={per}")["workflow_runs"]
    return [r for r in runs if r["status"] != "completed"]
def deploy():
    """Start a Pages deploy for the newest pushed commit (deploy.yml always deploys the newest main)."""
    if DRY: return
    now = time.time()
    if now - S["last_unstick"] >= 60:   # a run stuck "queued" > 5 min blocks the pages group (box v30 rule)
        S["last_unstick"] = now
        try:
            for r in active("deploy.yml"):
                t = calendar.timegm(time.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
                if r["status"] == "queued" and now - t > STUCK:
                    sh("gh", "api", "-X", "POST", f"repos/{REPO}/actions/runs/{r['id']}/cancel", timeout=30)
                    log(f"cancelled deploy run {r['id']} stuck queued > 5 min")
        except Exception as e: log(f"unstick check: {e}")
    head = git("rev-parse", "origin/main").stdout.strip()
    if not head or head == S["dispatched_sha"]: return
    try: busy = len(active("deploy.yml")) > 0
    except Exception as e: log(f"deploy check: {e}"); busy = False
    if busy and (not S["hold_since"] or now - S["hold_since"] < MAX_HOLD):
        S["hold_since"] = S["hold_since"] or now; return
    r = sh("gh", "workflow", "run", "deploy.yml", "--ref", "main", "-R", REPO, timeout=30)
    if r.returncode == 0: S["dispatched_sha"], S["hold_since"] = head, 0; S["deploys"] += 1
    else: log(f"!!! deploy dispatch failed: {r.stderr.strip()[:200]}")

# ---------- run control ----------
def control():
    """Every 2 min: stop if a newer handoff run is in progress; start the successor at NEXT_AT;
    keep the Backup Logger chain alive (it is the fallback when this chain is down)."""
    now = time.time()
    if DRY or (now - S["last_ctl"] < 120 and now < NEXT_AT): return
    S["last_ctl"] = now
    try:
        for r in active("collector.yml", 20):
            if r["id"] == RUN_ID or r["status"] != "in_progress" or "dry run" in (r.get("display_title") or ""): continue
            if r["id"] > RUN_ID and "handoff" in (r.get("display_title") or ""):
                S["stop"] = f"newer run {r['id']} took over"
            elif r["id"] < RUN_ID and not HANDOFF:   # two restarts raced: the older run keeps going
                S["stop"] = f"older run {r['id']} is already collecting"
            if S["stop"]: open(f"{BASE}/STOPPED", "w").write(S["stop"]); return
    except Exception as e: log(f"run check: {e}")
    if now >= NEXT_AT and not S["next_started"] and not DRY:
        r = sh("gh", "workflow", "run", "collector.yml", "--ref", "main", "-R", REPO, "-f", "handoff=true", timeout=30)
        if r.returncode == 0: S["next_started"] = True; log("started the next run (handoff)")
        else: log(f"!!! could not start the next run: {r.stderr.strip()[:200]}")
    if not DRY and now - S.get("last_bk", 0) >= 600:
        S["last_bk"] = now
        try:
            if not active("backup.yml", 10):
                sh("gh", "workflow", "run", "backup.yml", "--ref", "main", "-R", REPO, timeout=30); log("restarted the Backup Logger chain")
        except Exception as e: log(f"backup check: {e}")

def inputs_changed():
    """Every 10 min: has the box uploaded new private inputs (draft release asset inputs.enc)? -> reload."""
    if time.time() - S["inputs_check"] < 600: return False
    S["inputs_check"] = time.time()
    r = sh("bash", f"{BASE}/state.sh", "inputs", timeout=180)
    if r.returncode == 0 and "updated" in r.stdout: log("private inputs updated by the box; reloading"); return True
    return False

def main():
    seg_end = min(time.time() + float(sys.argv[1]) * 60, HARD_END)
    if not S["stop"] and time.time() >= HARD_END - 30: S["stop"] = "run time limit reached"
    if S["stop"]:
        open(f"{BASE}/STOPPED", "w").write(S["stop"]); save_state(); log(f"not running: {S['stop']}"); return
    import collector as C
    c = C.db()
    try: C.reclassify(c)
    except Exception: log("reclassify error: " + traceback.format_exc())
    log(f"segment start; until {time.strftime('%H:%M', time.localtime(seg_end))}")
    while time.time() < seg_end and not S["stop"]:
        t0 = time.time()
        try:
            if inputs_changed():
                c.close(); importlib_reload(); c = C.db()
        except Exception: log("inputs reload error: " + traceback.format_exc())
        try: pull()
        except Exception: pass
        try: C.cycle(c); S["polls"] += 1
        except Exception:
            log("cycle error: " + traceback.format_exc())
            try: c.close()
            except Exception: pass
            c = C.db()
        for f in (publish, deploy, control):
            try: f()
            except Exception: log(f"{f.__name__} error: " + traceback.format_exc())
        save_state()
        if S["stop"] or time.time() >= HARD_END: break
        time.sleep(max(5, POLL - (time.time() - t0)))
    c.close()
    save_state()
    try:   # collector.py's own log (feed errors, aliases, merges) for this segment
        lines = open(f"{BASE}/collector.log").read().splitlines()[S.get("log_lines", 0):]
        S["log_lines"] = S.get("log_lines", 0) + len(lines); save_state()
        for l in lines[-200:]: print("  collector: " + l)
    except OSError: pass
    log(f"segment end: polls {S['polls']}, pushes {S['pushes']}, deploys {S['deploys']}" + (f"; {S['stop']}" if S["stop"] else ""))

def importlib_reload():
    import importlib, collector as C, d83rules as R
    importlib.reload(R); importlib.reload(C)

if __name__ == "__main__": main()
