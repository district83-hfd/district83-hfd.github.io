#!/usr/bin/env python3
"""Generated site data travels as a tarball on a draft release ("site-data"), not as git commits (storage plan, staged).
The collector uploads one bundle per publish; the Pages deploy downloads the newest good one and lays it over the
checkout of main (which then holds only source and static files). Nothing private is ever in a bundle: it is exactly
the files the site already serves (index.html, stats.json, ooa.json, version.json, points.json, transport.json, the
first-due map files, tours/, archive/).

  site_bundle.py build <build dir> <out.tgz>       tar the staged site files + bundle.json (generated, build, sha256s)
  site_bundle.py upload <out.tgz>                  upload as site-<ms>-<run>.tgz, keep the newest KEEP, delete older
  site_bundle.py fetch <dest dir> [--repo-stats F] newest valid bundle -> dest, only if newer than F (prints decision)
Env: GITHUB_REPOSITORY, GH_TOKEN (gh CLI); D83_BUNDLE_TAG (default site-data)."""
import hashlib, io, json, os, subprocess, sys, tarfile, time
TAG = os.environ.get("D83_BUNDLE_TAG", "site-data"); KEEP = 4
FILES = ["index.html", "stats.json", "ooa.json", "version.json", "points.json", "transport.json", "fdareas.json", "d83area.json", "neighbors.json"]
DIRS = ["tours", "archive"]

def gh(*a, data=None, raw=False, timeout=120):
    r = subprocess.run(["gh", "api", *a], input=data, capture_output=True, timeout=timeout)
    if r.returncode: raise RuntimeError(f"gh api {' '.join(x for x in a if not x.startswith('-'))[:80]}: {r.stderr.decode()[:200]}")
    return r.stdout if raw else (json.loads(r.stdout) if r.stdout.strip() else None)

def repo(): return os.environ["GITHUB_REPOSITORY"]
def release(create=False):
    for r in gh(f"repos/{repo()}/releases?per_page=100") or []:
        if r.get("draft") and r.get("tag_name") == TAG: return r
    if not create: return None
    return gh("-X", "POST", f"repos/{repo()}/releases", "-f", f"tag_name={TAG}", "-F", "draft=true",
              "-f", "name=Site data (generated; draft, never publish)", "-f", "body=Uploaded by the collector; read by Deploy Pages.")
def assets(rel):
    a = [x for x in rel.get("assets", []) if x["name"].startswith("site-") and x["name"].endswith(".tgz")]
    return sorted(a, key=lambda x: x["name"], reverse=True)   # site-<13-digit generated ms>-... newest data first

def gen_of(stats_bytes):
    try: return int(json.loads(stats_bytes).get("generated") or 0)
    except Exception: return 0

def build(src, out):
    buf = io.BytesIO(); meta = {"files": {}}
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as t:
        names = [f for f in FILES if os.path.isfile(os.path.join(src, f))]
        for d in DIRS:
            for root, _, fs in os.walk(os.path.join(src, d)):
                names += sorted(os.path.relpath(os.path.join(root, f), src) for f in fs if f.endswith(".json"))
        for n in names:
            b = open(os.path.join(src, n), "rb").read(); meta["files"][n] = hashlib.sha256(b).hexdigest()
            ti = tarfile.TarInfo(n); ti.size = len(b); ti.mtime = 0; ti.mode = 0o644; t.addfile(ti, io.BytesIO(b))
        meta["generated"] = gen_of(open(os.path.join(src, "stats.json"), "rb").read())
        meta["build"] = json.load(open(os.path.join(src, "version.json"))).get("build")
        meta["created"] = int(time.time() * 1000); meta["run"] = os.environ.get("GITHUB_RUN_ID", "local")
        mb = json.dumps(meta, sort_keys=True).encode(); ti = tarfile.TarInfo("bundle.json"); ti.size = len(mb); ti.mtime = 0; t.addfile(ti, io.BytesIO(mb))
    open(out, "wb").write(buf.getvalue()); return meta

def upload(path):
    rel = release(create=True)
    # site-<stats generated ms>-<run>-<upload ms>.tgz: sorting by name = newest data first, even when two runs overlap
    # at a handoff (the old run's last upload can land after the new run's first). backup.py reads the generated ms too.
    gen = check(open(path, "rb").read())[0]["generated"]
    name = f"site-{gen:013d}-{os.environ.get('GITHUB_RUN_ID', 'local')}-{int(time.time() * 1000)}.tgz"
    up = f"https://uploads.github.com/repos/{repo()}/releases/{rel['id']}/assets?name={name}"
    a = gh("-X", "POST", "-H", "Content-Type: application/gzip", up, "--input", path)
    rel = release() or rel
    for old in assets(rel)[KEEP:]:   # newest KEEP stay, so a deploy that is mid-download never loses its file
        try: gh("-X", "DELETE", f"repos/{repo()}/releases/assets/{old['id']}")
        except RuntimeError as e: print(f"prune {old['name']}: {e}")
    return a["id"], name

def check(blob):
    """Open a bundle, verify every file against bundle.json and the key files parse. Returns (meta, {name: bytes})."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        for m in t.getmembers():
            if not m.isfile() or m.name.startswith("/") or ".." in m.name.split("/"): raise ValueError(f"bad member {m.name}")
            out[m.name] = t.extractfile(m).read()
    meta = json.loads(out.pop("bundle.json"))
    for n, h in meta["files"].items():
        if hashlib.sha256(out[n]).hexdigest() != h: raise ValueError(f"checksum {n}")
    if set(out) != set(meta["files"]): raise ValueError("file list mismatch")
    if b"District 83" not in out["index.html"] or not gen_of(out["stats.json"]): raise ValueError("index/stats invalid")
    json.loads(out["version.json"])
    return meta, out

def fetch(dest, repo_stats=None):
    """Lay the newest valid bundle over dest when its stats are newer than the repo's own stats.json (newer wins:
    the box fallback collector still publishes by commit, so whichever logger wrote last is what the site shows)."""
    rel = release()
    if not rel: print("no site-data release: deploying the repo as committed"); return None
    rg = gen_of(open(repo_stats, "rb").read()) if repo_stats and os.path.exists(repo_stats) else 0
    for a in assets(rel)[:KEEP]:
        try:
            blob = gh("-H", "Accept: application/octet-stream", f"repos/{repo()}/releases/assets/{a['id']}", raw=True)
            meta, files = check(blob)
        except Exception as e: print(f"skip {a['name']}: {e}"); continue
        if meta["generated"] <= rg:
            print(f"repo stats.json is newer ({rg} >= {meta['generated']}): deploying the repo as committed"); return None
        for n, b in files.items():
            p = os.path.join(dest, n); os.makedirs(os.path.dirname(p) or ".", exist_ok=True); open(p, "wb").write(b)
        print(f"overlaid {a['name']}: {len(files)} files, generated {time.strftime('%F %T', time.localtime(meta['generated'] / 1000))}, build {meta['build']}")
        return a["name"]
    print("no valid bundle: deploying the repo as committed"); return None

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build": print(json.dumps({k: v for k, v in build(sys.argv[2], sys.argv[3]).items() if k != "files"}))
    elif cmd == "upload": print(upload(sys.argv[2]))
    elif cmd == "fetch":
        rs = sys.argv[sys.argv.index("--repo-stats") + 1] if "--repo-stats" in sys.argv else None
        fetch(sys.argv[2], rs)
    else: sys.exit(__doc__)
