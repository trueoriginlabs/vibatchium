#!/usr/bin/env bash
# Download the public labelled injection sets run_eval.py reads (Hugging Face
# datasets-server, no auth). Data is third-party: keep it out of git.
# usage: bench/injection_eval/fetch_datasets.sh <out_dir>
set -euo pipefail
out=${1:?out dir}; mkdir -p "$out"; cd "$out"
fetch(){ ds=$1; split=$2; max=$3; n=$(echo "$ds" | tr '/' '_')_$split; : > "$n.jsonl"; off=0
  while [ "$off" -lt "$max" ]; do
    curl -s -m 30 "https://datasets-server.huggingface.co/rows?dataset=$ds&config=default&split=$split&offset=$off&length=100" > tmp.json
    c=$(python3 -I -c "import json,sys;d=json.load(open('tmp.json'));rows=d.get('rows',[]);[print(json.dumps(r['row'])) for r in rows];sys.stderr.write(str(len(rows)))" 2>cnt >> "$n.jsonl"; cat cnt)
    [ "$(cat cnt)" = "100" ] || break; off=$((off+100)); done
  rm -f tmp.json cnt; echo "$n $(wc -l < "$n.jsonl")"; }
fetch deepset/prompt-injections train 700
fetch deepset/prompt-injections test 200
fetch Lakera/gandalf_ignore_instructions test 200
fetch jackhhao/jailbreak-classification test 300
fetch xTRam1/safe-guard-prompt-injection test 2100
