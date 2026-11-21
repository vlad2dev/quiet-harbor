"""Play the bot in a public match on play.joinbuilders.org, for logs and debugging.

Usage: .venv/Scripts/python tools/bridge.py [--room N] [--name NAME] [--matches K]
Writes logs/<stamp>/{msgs.jsonl,bot.stderr,summary.json}. Not part of the submission.
"""
import argparse
import asyncio
import json
import os
import random
import string
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent


async def pump_stderr(proc, path):
    with open(path, "ab") as f:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            f.write(line)
            f.flush()


async def play_one(args, outdir):
    sid = os.urandom(16).hex()
    code = ""
    if args.private:
        import urllib.request
        r = json.load(urllib.request.urlopen(f"https://play.joinbuilders.org/mm/code/new?sid={sid}", timeout=5))
        args.room, code = r["room"], r["code"]
    url = f"wss://play.joinbuilders.org/r/{args.room}/play?sid={sid}&name={args.name}" + (f"&code={code}" if code else "")
    print("connecting", url, file=sys.stderr)
    msgs = open(outdir / "msgs.jsonl", "a")
    summary = {"room": args.room, "start": time.time()}
    proc = None
    async with websockets.connect(url, max_size=2**24, ping_interval=20) as ws:
        async def from_bot():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if m.get("type") == "ready":
                    continue  # the lobby ready is sent separately
                await ws.send(json.dumps(m, separators=(",", ":")))

        reader = None
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            t = m.get("type")
            if t != "obs" or m.get("tick", 0) % 20 == 0 or m.get("events"):
                msgs.write(json.dumps(m)[:20000] + "\n")
            if t == "lobby":
                you = m.get("you", {})
                if not you.get("ready"):
                    await ws.send(json.dumps({"type": "ready", "name": args.name}))
                elif you.get("host") and m.get("phase") == "waiting" and not summary.get("started"):
                    summary["started"] = True
                    await ws.send(json.dumps({"type": "start"}))
                summary["lobby"] = {k: m.get(k) for k in ("phase", "next_at")}
            elif t == "hello":
                m["map"]["dir"] = str(ROOT / "arena")
                summary["slot"] = m.get("slot")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", args.bot,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                asyncio.create_task(pump_stderr(proc, outdir / "bot.stderr"))
                proc.stdin.write((json.dumps(m) + "\n").encode())
                await proc.stdin.drain()
                reader = asyncio.create_task(from_bot())
                await ws.send(json.dumps({"type": "loaded"}))
            elif t in ("obs", "end"):
                if proc:
                    proc.stdin.write((json.dumps(m) + "\n").encode())
                    await proc.stdin.drain()
                if t == "obs":
                    s = m["self"]
                    summary["last"] = {"t": m["t"], "hp": s["hp"], "alive": m["alive"], "pos": s["pos"]}
                if t == "end":
                    summary["end"] = m
                    print("END", m, file=sys.stderr)
            elif t == "results":
                summary["results"] = m
                break
    msgs.close()
    if proc:
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            proc.kill()
    return summary


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, default=0)
    ap.add_argument("--name", default="survivor-test")
    ap.add_argument("--matches", type=int, default=1)
    ap.add_argument("--private", action="store_true", help="own room: us against the house bots")
    ap.add_argument("--bot", default=str(ROOT / "bot.py"), help="bot script to run")
    ap.add_argument("--logdir", default=str(ROOT / "logs"))
    args = ap.parse_args()
    if not args.room and not args.private:
        import urllib.request
        args.room = json.load(urllib.request.urlopen("https://play.joinbuilders.org/mm/pick?lead=8", timeout=5))["room"]
    for _ in range(args.matches):
        outdir = Path(args.logdir) / (time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex())
        outdir.mkdir(parents=True, exist_ok=True)
        s = await play_one(args, outdir)
        (outdir / "summary.json").write_text(json.dumps(s, indent=1))
        print(json.dumps({k: s.get(k) for k in ("room", "slot", "end", "last")}))


if __name__ == "__main__":
    asyncio.run(main())
