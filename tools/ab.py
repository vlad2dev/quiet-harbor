"""A/B test bot versions in private matches on the play server, interleaved and concurrent.

Usage: .venv/Scripts/python tools/ab.py N name=git-ref-or-path [name=...] [--par 8]
  name=HEAD~1         snapshot bot.py at that git ref
  name=./bot.py       use a file as-is
Each variant plays N matches. Prints per-variant mean place, kills, damage ratio, survival.
"""
import collections
import glob
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
argv = sys.argv[1:]
par = 8
if "--par" in argv:
    i = argv.index("--par")
    par = int(argv[i + 1])
    del argv[i:i + 2]
args = argv
n = int(args[0])
variants = {}
for spec in args[1:]:
    name, ref = spec.split("=", 1)
    d = os.path.join(ROOT, "variants", name)
    os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "mechfight.py"), d)
    if os.path.exists(ref):
        shutil.copy(ref, os.path.join(d, "bot.py"))
    else:
        src = subprocess.check_output(["git", "show", f"{ref}:bot.py"], cwd=ROOT)
        open(os.path.join(d, "bot.py"), "wb").write(src)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    variants[name] = d

queue = [name for _ in range(n) for name in variants]
running = []
while queue or running:
    while queue and len(running) < par:
        name = queue.pop(0)
        d = variants[name]
        out = open(os.path.join(d, "logs", f"run-{time.time():.0f}-{len(queue)}.out"), "w")
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "bridge.py"), "--private",
                              "--name", f"ab-{name}", "--bot", os.path.join(d, "bot.py"),
                              "--logdir", os.path.join(d, "logs")], stdout=out, stderr=subprocess.STDOUT, cwd=ROOT)
        running.append((p, time.time()))
        time.sleep(1.0)
    still = []
    for p, t0 in running:
        if p.poll() is None and time.time() - t0 < 900:
            still.append((p, t0))
        elif p.poll() is None:
            p.kill()
    running = still
    time.sleep(2)


def stats(d):
    rows = []
    for md in sorted(glob.glob(os.path.join(d, "logs", "2*"))):
        f = os.path.join(md, "msgs.jsonl")
        if not os.path.exists(f):
            continue
        end, t_last, dealt, taken = None, 0, 0.0, 0.0
        for line in open(f):
            m = json.loads(line)
            if m.get("type") == "end":
                end = m
            if m.get("type") != "obs":
                continue
            t_last = m["t"]
            for e in m["events"]:
                if e["type"] == "hit" and e["target"] < 100:
                    dealt += e["amount"]
                if e["type"] == "damaged":
                    taken += e["amount"]
        if end:
            rows.append((end["place"], end["kills"], dealt, taken, t_last))
    return rows


for name, d in variants.items():
    rows = stats(d)
    if not rows:
        print(name, "no completed matches")
        continue
    k = len(rows)
    mp = sum(r[0] for r in rows) / k
    sd = (sum((r[0] - mp) ** 2 for r in rows) / max(1, k - 1)) ** 0.5
    print(f"{name}: matches {k} place {mp:.2f} (+-{sd / k ** 0.5:.2f}) kills {sum(r[1] for r in rows) / k:.2f} "
          f"dealt/taken {sum(r[2] for r in rows) / max(1, sum(r[3] for r in rows)):.2f} "
          f"survive {sum(r[4] for r in rows) / k:.0f}s")
