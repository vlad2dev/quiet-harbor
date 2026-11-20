"""Print the Mech Royale leaderboard (parsed from the page's embedded data)."""
import json, re, sys, urllib.request

html = urllib.request.urlopen("https://www.joinbuilders.org/buildathon/leaderboard", timeout=15).read().decode()
html = html.replace('\\"', '"')
i = html.find("Placement points")
m = re.search(r'"standings":(\[.*?\])', html[i:])
rows = json.loads(m.group(1))
for r in rows[: int(sys.argv[1]) if len(sys.argv) > 1 else 40]:
    print(f'{r["rank"]:>3} {r["score"]:>6} {r["team"]}')
