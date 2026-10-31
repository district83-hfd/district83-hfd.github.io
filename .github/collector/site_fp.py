#!/usr/bin/env python3
"""Fingerprint of the published site that ignores fields that change on every render (render/poll times, poll
counters, running minutes/hours on active calls). publish.sh pushes when this changes, plus a heartbeat push so
the site's 'generated' time never goes stale. Usage: site_fp.py <site dir>  -> prints a hex digest."""
import hashlib, json, os, re, sys
VOLATILE = {"generated", "generated_str", "last_ok_poll", "last_ok_poll_str", "polls", "polls_ok", "fetched_str", "fetched",   # v66: when the weather / traffic feeds were last read (the readings themselves still count)
            
            "ls", "min", "cad", "hrs", "ov2", "m",
            # citywide-only numbers (not District 83 data): refreshed by the heartbeat push
            "total_incidents_logged", "city24", "city_year", "report_ts", "report_str", "declared", "parsed", "trend"}
def norm(x):
    if isinstance(x, dict): return {k: norm(v) for k, v in x.items() if k not in VOLATILE}
    if isinstance(x, list): return [norm(v) for v in x]
    return x
STATS_RE = re.compile(r"^const STATS=(.*);\s*$", re.M)
def main(d):
    h = hashlib.sha256()
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            p = os.path.join(root, f); rel = os.path.relpath(p, d); b = open(p, "rb").read()
            try:
                if f.endswith(".json"):
                    j = json.loads(b)
                    if f == "transport.json" and isinstance(j.get("now"), list):   # citywide list: only D83 units matter
                        j["now"] = [x for x in j["now"] if x.get("s")]
                    b = json.dumps(norm(j), sort_keys=True).encode()
                elif f.endswith(".html"):
                    t = b.decode("utf8")
                    t = STATS_RE.sub(lambda m: "const STATS=" + json.dumps(norm(json.loads(m.group(1))), sort_keys=True) + ";", t)
                    b = t.encode()
            except Exception: pass          # unparsable: hash raw bytes
            h.update(rel.encode() + b"\0" + hashlib.sha256(b).digest())
    print(h.hexdigest()[:16])
if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "site")
