# Mech Royale bot (Builders Buildathon 2026)

A survival-first bot for the 16-mech battle royale. Placement scores, not kills, so it:

- builds a 4 m walkability grid from the map's prisms and terrain slope, and paths with incremental A*,
- picks a covered spot inside the next zone circle, away from NPCs, drops and enemies, and stands still there (no step sounds),
- faces whoever is shooting it (rear hits deal 1.5x), shoots back with lead, and backs away when hurt,
- picks up lasers and lightning guns and cubes when nobody is near, and takes survival upgrades first.

Check: `python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech-bot`

MIT licensed.
