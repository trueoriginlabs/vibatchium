"""Measure a classify() implementation on recall + false positives.

usage: python -I run_eval.py <path/to/safety.py> <data_dir> [--label NAME] [-v]

The benign corpus is the twitter_persona tweet archive if present
(~/.local/share/twitter_persona/tl_archive.db); that part is skipped otherwise.
"""
import collections
import importlib.util
import json
import os
import sqlite3
import sys
import time

mod_path, data_dir = sys.argv[1], sys.argv[2]
label = sys.argv[4] if len(sys.argv) > 4 else mod_path
spec = importlib.util.spec_from_file_location("safety_under_test", mod_path)
safety = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safety)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handcrafted  # noqa: E402


def load(name, text_key, label_fn):
    out = []
    with open(os.path.join(data_dir, name)) as f:
        for line in f:
            r = json.loads(line)
            out.append((r[text_key], label_fn(r)))
    return out


sets = {
    "deepset": load("deepset_prompt-injections_train.jsonl", "text", lambda r: r["label"])
    + load("deepset_prompt-injections_test.jsonl", "text", lambda r: r["label"]),
    "gandalf": load("Lakera_gandalf_ignore_instructions_test.jsonl", "text", lambda r: 1),
    "jailbreak": load("jackhhao_jailbreak-classification_test.jsonl", "prompt",
                      lambda r: 1 if r["type"] == "jailbreak" else 0),
    "safeguard": load("xTRam1_safe-guard-prompt-injection_test.jsonl", "text", lambda r: r["label"]),
}

print(f"=== {label}")
for name, rows in sets.items():
    pos = [t for t, l in rows if l == 1]
    neg = [t for t, l in rows if l == 0]
    det = sum(safety.classify(t)["risk"] != "none" for t in pos)
    hi = sum(safety.classify(t)["risk"] == "high" for t in pos)
    fp = sum(safety.classify(t)["risk"] != "none" for t in neg)
    print(f"{name:10s} recall(any) {det}/{len(pos)} = {det/max(1,len(pos)):.1%}  "
          f"recall(high) {hi/max(1,len(pos)):.1%}  FP {fp}/{len(neg)} = {fp/max(1,len(neg)):.1%}")

fam = collections.defaultdict(lambda: [0, 0])
missed = []
for f, t in handcrafted.ATTACKS:
    r = safety.classify(t)["risk"]
    fam[f][1] += 1
    if r != "none":
        fam[f][0] += 1
    else:
        missed.append((f, t[:70]))
print("handcrafted:", " ".join(f"{k} {a}/{b}" for k, (a, b) in fam.items()),
      f"| total {sum(a for a,_ in fam.values())}/{len(handcrafted.ATTACKS)}")
hb = [(t, safety.classify(t)) for t in handcrafted.HARD_BENIGN]
hbfp = [(t[:50], c["signals"]) for t, c in hb if c["risk"] != "none"]
print(f"hard-benign FP {len(hbfp)}/{len(hb)}")
for t, s in hbfp:
    print("   FP", s, "|", t)
if "-v" in sys.argv:
    for f, t in missed:
        print("   MISS", f, "|", t)

# Tweets archive: the real benign corpus for twitter_persona.
db = os.path.expanduser("~/.local/share/twitter_persona/tl_archive.db")
if not os.path.exists(db):
    sys.exit(0)
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
texts = list({r[0] for r in con.execute("select text from tweets where text is not null")})
t0 = time.time()
sig = collections.Counter()
flag_hi = flag_any = 0
examples = collections.defaultdict(list)
for t in texts:
    c = safety.classify(t)
    if c["risk"] != "none":
        flag_any += 1
        flag_hi += c["risk"] == "high"
        for s in c["signals"]:
            sig[s] += 1
            if len(examples[s]) < 2:
                examples[s].append(t[:90].replace("\n", " "))
dt = time.time() - t0
print(f"tweets {len(texts)}: flagged any {flag_any} ({flag_any/len(texts):.3%}), high {flag_hi} "
      f"| {dt*1000/len(texts):.3f} ms/text")
for s, n in sig.most_common():
    print(f"   {s:28s} {n:5d}  e.g. {examples[s][0]!r}")
