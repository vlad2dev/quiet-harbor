"""Run N private matches in parallel on the play server and summarize them.

Usage: .venv/Scripts/python tools/batch.py N [label]
"""
import collections
import glob
import json
import os
import subprocess
import sys
import time

n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
label = sys.argv[2] if len(sys.argv) > 2 else "batch"
before = set(glob.glob("logs/2*"))
procs = []
for i in range(n):
    out = open(f"logs/{label}-{i}.out", "w")
    procs.append(subprocess.Popen([sys.executable, "tools/bridge.py", "--private", "--name", f"{label}-{i}"],
                                  stdout=out, stderr=subprocess.STDOUT))
    time.sleep(1.5)
for p in procs:
    try:
        p.wait(timeout=900)
    except subprocess.TimeoutExpired:
        p.kill()
dirs = sorted(set(glob.glob("logs/2*")) - before)
tot = collections.Counter()
places = []
for d in dirs:
    c = collections.Counter()
    end = None
    t_last = 0
    for line in open(os.path.join(d, "msgs.jsonl")):
        m = json.loads(line)
        if m.get("type") == "end":
            end = m
        if m.get("type") != "obs":
            continue
        t_last = m["t"]
        for e in m["events"]:
            if e["type"] == "hit":
                c["dealt_npc" if e["target"] >= 100 else "dealt"] += e["amount"]
            if e["type"] == "damaged":
                c["taken"] += e["amount"]
    place = end.get("place") if end else None
    kills = end.get("kills") if end else None
    places.append(place)
    tot.update(c)
    tot["kills"] += kills or 0
    print(f"{d}: place {place} kills {kills} died {t_last:.0f}s dealt {c['dealt']:.0f} (+npc {c['dealt_npc']:.0f}) taken {c['taken']:.0f}")
ok = [p for p in places if p]
if ok:
    print(f"{label}: matches {len(ok)} mean place {sum(ok) / len(ok):.2f} kills/match {tot['kills'] / len(ok):.2f} "
          f"dealt/taken {tot['dealt'] / max(1, tot['taken']):.2f}")
