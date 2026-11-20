"""Summarize logged matches: place, kills, death time, damage, and alive count over time.

Usage: python tools/analyze.py [logs/<stamp> ...]   (default: every logged match)
"""
import glob
import json
import sys


def summarize(d):
    end = None
    rows = []
    dealt = taken = 0.0
    for line in open(f"{d}/msgs.jsonl"):
        m = json.loads(line)
        if m.get("type") == "end":
            end = m
        if m.get("type") != "obs":
            continue
        for e in m.get("events", []):
            if e["type"] == "hit":
                dealt += e.get("amount", 0)
            if e["type"] == "damaged":
                taken += e.get("amount", 0)
        s = m["self"]
        rows.append((m["t"], round(s["hp"]), m["alive"], s["cubes"], [w and w["kind"] for w in s["weapons"]]))
    if not rows:
        return f"{d}: no observations"
    t_last = rows[-1][0]
    alive_drops = [(r[0], r[2]) for i, r in enumerate(rows) if i and r[2] != rows[i - 1][2]]
    return (f"{d}: place {end and end.get('place')} kills {end and end.get('kills')} died~{t_last:.0f}s "
            f"cubes {rows[-1][3]} weapons {rows[-1][4]} sampled dealt {dealt:.0f} taken {taken:.0f}\n"
            f"  alive changes: {alive_drops}")


dirs = sys.argv[1:] or sorted(glob.glob("logs/2*"))
for d in dirs:
    try:
        print(summarize(d.rstrip("/")))
    except FileNotFoundError:
        pass
