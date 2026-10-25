#!/bin/bash
# Private collector state for GitHub Actions (v56). Nothing private is ever committed: the citywide incident DB,
# the report-history roll-up DB and the hand-edited inputs travel only AES-256 encrypted (key = secret D83_STATE_KEY):
#   state  -> Actions artifact "d83-state" (state.enc; overwritten at each checkpoint, kept 3 days)
#   seed   -> draft release "collector-inputs", asset seed.enc   (box snapshot: used when it is newer than any artifact)
#   inputs -> same draft release, asset inputs.enc              (backfill roll-up, manual units, WF exclusions, schedule)
# Usage: state.sh restore | snapshot | inputs     (env: D83_BASE, D83_STATE_KEY, GH_TOKEN, GITHUB_REPOSITORY)
set -euo pipefail
B=$D83_BASE; R=$GITHUB_REPOSITORY; TAG=collector-inputs
[ -n "${D83_STATE_KEY:-}" ] || { echo "D83_STATE_KEY secret missing"; exit 1; }
enc(){ openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:D83_STATE_KEY; }
dec(){ openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:D83_STATE_KEY; }
asset(){ gh api "repos/$R/releases?per_page=50" --jq "[.[] | select(.draft and .tag_name==\"$TAG\") | .assets[] | select(.name==\"$1\")] | sort_by(.updated_at) | last | if . then \"\(.id) \(.updated_at)\" else \"\" end"; }
get(){ gh api -H "Accept: application/octet-stream" "repos/$R/releases/assets/$1" > "$2"; }
inputs(){   # $1=force -> always apply
  local a id; a=$(asset inputs.enc); id=${a%% *}
  [ -n "$id" ] || { echo "no inputs asset"; return 1; }
  [ "${1:-}" != force ] && [ "$a" = "$(cat "$B/.inputs_id" 2>/dev/null)" ] && { echo "inputs unchanged"; return 0; }
  rm -rf "$B/.in" && mkdir -p "$B/.in"; get "$id" "$B/.in/i.enc"; dec < "$B/.in/i.enc" | tar xz -C "$B/.in"
  for f in backfill.db manual_units.json wf_exclude.json hfd_schedule.csv ntfy_topics.json; do
    [ -f "$B/.in/$f" ] && mv -f "$B/.in/$f" "$B/$f"; done
  rm -rf "$B/.in"; echo "$a" > "$B/.inputs_id"; echo "inputs updated ($a)"
}
case "$1" in
  restore)
    art=$(gh api "repos/$R/actions/artifacts?name=d83-state&per_page=30" --jq '[.artifacts[] | select(.expired|not)] | sort_by(.created_at) | last | if . then "\(.id) \(.created_at) \(.workflow_run.id)" else "" end')
    seed=$(asset seed.enc); read -r aid at arun <<< "${art:-x 0 0}"; read -r sid st <<< "${seed:-x 0}"
    if [ "$aid" != x ] && [[ "$at" > "$st" ]]; then
      gh api "repos/$R/actions/artifacts/$aid/zip" > "$B/s.zip"; unzip -p "$B/s.zip" state.enc | dec | tar xz -C "$B"; rm -f "$B/s.zip"
      echo "restored state from artifact $aid (run $arun, saved $at)"
    elif [ "$sid" != x ]; then
      get "$sid" "$B/s.enc"; dec < "$B/s.enc" | tar xz -C "$B"; rm -f "$B/s.enc"; echo "restored state from the box seed (uploaded $st)"
    else echo "no state found"; exit 1; fi
    inputs force ;;
  snapshot)   # consistent copy of the DB (sqlite backup API) + box map caches -> $B/out/state.enc
    rm -rf "$B/.snap" && mkdir -p "$B/.snap" "$B/out"
    python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" "$B/incidents.db" "$B/.snap/incidents.db"
    cp "$B/boxes.json" "$B/districts.json" "$B/.snap/" 2>/dev/null || true
    tar cz -C "$B/.snap" . | enc > "$B/out/state.enc"; rm -rf "$B/.snap"
    echo "snapshot $(du -h "$B/out/state.enc" | cut -f1)" ;;
  inputs) inputs ;;
  *) echo "usage: state.sh restore|snapshot|inputs"; exit 2 ;;
esac
