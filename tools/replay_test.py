"""Feed recorded observations (kit stream + logged live matches) through the bot; report errors and timing."""
import glob, gzip, json, sys, time
sys.path.insert(0, ".")
import bot

lines = [json.loads(l) for l in gzip.open("check/stream.jsonl.gz", "rt")]
hello = lines[0]
hello["map"]["dir"] = "arena"
b = bot.Bot(hello)
worst = 0.0
n = 0
for f in ["check"] + sorted(glob.glob("logs/*/msgs.jsonl")):
    src = lines if f == "check" else [json.loads(l) for l in open(f)]
    for o in src:
        if o.get("type") != "obs":
            continue
        t = time.perf_counter()
        b.act(o)
        worst = max(worst, time.perf_counter() - t)
        n += 1
print(f"ok: {n} observations, worst {worst * 1000:.1f} ms")
