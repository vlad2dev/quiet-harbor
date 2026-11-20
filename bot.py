"""Survivor bot: placement wins, so hide in cover inside the next zone, stay silent,
face threats (rear hits deal x1.5), kite away when hurt, and shoot back with lead."""
import heapq
import json
import math
import os
import random
import sys
import time
from array import array

import mechfight as mf

CELL = 4.0  # navigation grid, meters
TICK_BUDGET = 0.022  # seconds of path search per decision


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class Grid:
    """Coarse walkability grid from map prisms and terrain slope."""

    def __init__(self, mapdir, cfg_mech):
        t0 = time.time()
        world = json.load(open(os.path.join(mapdir, "v2", "world.json")))
        ter = world["terrain"]
        self.tminx, self.tminy, self.tstep = ter["minX"], ter["minY"], ter["step"]
        self.tnx, self.tny = ter["nx"], ter["ny"]
        self.h = array("h")
        with open(os.path.join(mapdir, "v2", ter.get("file", "terrain.i16")), "rb") as f:
            self.h.frombytes(f.read())
        if sys.byteorder != "little":
            self.h.byteswap()
        b = world["bounds"]
        self.minx, self.miny = b["minX"], b["minY"]
        self.nx = int((b["maxX"] - b["minX"]) / CELL) + 1
        self.ny = int((b["maxY"] - b["minY"]) / CELL) + 1
        n = self.nx * self.ny
        ground = [0.0] * n
        for j in range(self.ny):
            for i in range(self.nx):
                x, y = self.center(i, j)
                ground[j * self.nx + i] = self.height(x, y)
        self.ground = ground
        blocked = bytearray(n)
        # border margin: the map edge is a wall
        for j in range(self.ny):
            for i in range(self.nx):
                if i < 2 or j < 2 or i >= self.nx - 2 or j >= self.ny - 2:
                    blocked[j * self.nx + i] = 1
        # steep terrain
        max_rise = CELL * math.tan(math.radians(cfg_mech.get("max_slope_deg", 50))) * 0.8
        for j in range(self.ny - 1):
            for i in range(self.nx - 1):
                k = j * self.nx + i
                g = ground[k]
                if abs(ground[k + 1] - g) > max_rise or abs(ground[k + self.nx] - g) > max_rise:
                    blocked[k] = 1
                    blocked[k + 1] = 1
                    blocked[k + self.nx] = 1
        # prisms, rasterized by scanline (even-odd covers holes), inflated by the mech radius
        for p in world["prisms"]:
            rings = [p["ring"]] + list(p.get("holes") or [])
            edges = []
            xs_all, ys_all = p["ring"][0::2], p["ring"][1::2]
            for r in rings:
                m = len(r) // 2
                for e in range(m):
                    x1, y1 = r[2 * e], r[2 * e + 1]
                    x2, y2 = r[2 * ((e + 1) % m)], r[2 * ((e + 1) % m) + 1]
                    if y1 != y2:
                        edges.append((x1, y1, x2, y2))
            j0 = max(0, int((min(ys_all) - self.miny) / CELL) - 1)
            j1 = min(self.ny - 1, int((max(ys_all) - self.miny) / CELL) + 1)
            for j in range(j0, j1 + 1):
                cells = set()
                # sample three scanlines per row so thin walls are not missed
                for fy in (0.1, 0.5, 0.9):
                    yc = self.miny + (j + fy) * CELL
                    xs = []
                    for x1, y1, x2, y2 in edges:
                        if (y1 <= yc) != (y2 <= yc):
                            xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
                    xs.sort()
                    for a in range(0, len(xs) - 1, 2):
                        i0 = max(0, int((xs[a] - self.minx) / CELL))
                        i1 = min(self.nx - 1, int((xs[a + 1] - self.minx) / CELL))
                        cells.update(range(i0, i1 + 1))
                for i in cells:
                    k = j * self.nx + i
                    g = ground[k]
                    if p["kind"] == "bridge" and p["base"] - g >= 10:
                        continue  # walk under
                    if p["top"] - g <= 1.0:
                        continue
                    blocked[k] = 1
        # inflate by one cell: the mech is 5 m wide
        infl = bytearray(blocked)
        nx = self.nx
        for k in range(n):
            if blocked[k]:
                continue
            i, j = k % nx, k // nx
            if 0 < i < nx - 1 and 0 < j < self.ny - 1:
                if blocked[k - 1] or blocked[k + 1] or blocked[k - nx] or blocked[k + nx]:
                    infl[k] = 2  # walkable but hugging a wall: costly, and good cover
        self.blocked = blocked
        self.soft = infl
        # cover: blocked cells within ~12 m
        cover = bytearray(n)
        R = 3
        for j in range(self.ny):
            for i in range(self.nx):
                k = j * nx + i
                if blocked[k]:
                    continue
                c = 0
                for dj in range(-R, R + 1, 1):
                    jj = j + dj
                    if jj < 0 or jj >= self.ny:
                        continue
                    row = jj * nx
                    for di in (-R, -1, 1, R):
                        ii = i + di
                        if 0 <= ii < nx and blocked[row + ii]:
                            c += 1
                cover[k] = min(c, 255)
        self.cover = cover
        log(f"grid {self.nx}x{self.ny} built in {time.time() - t0:.2f}s, blocked {sum(1 for v in blocked if v)}")

    def height(self, x, y):
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = min(max(int(fx), 0), self.tnx - 2)
        j = min(max(int(fy), 0), self.tny - 2)
        ax, ay = min(max(fx - i, 0.0), 1.0), min(max(fy - j, 0.0), 1.0)
        k = j * self.tnx + i
        h = self.h
        a = h[k] * (1 - ax) + h[k + 1] * ax
        b = h[k + self.tnx] * (1 - ax) + h[k + self.tnx + 1] * ax
        return (a * (1 - ay) + b * ay) * 0.01

    def center(self, i, j):
        return self.minx + (i + 0.5) * CELL, self.miny + (j + 0.5) * CELL

    def cell(self, x, y):
        i = min(max(int((x - self.minx) / CELL), 0), self.nx - 1)
        j = min(max(int((y - self.miny) / CELL), 0), self.ny - 1)
        return j * self.nx + i

    def free(self, k):
        return not self.blocked[k]

    def nearest_free(self, k, maxr=8):
        if self.free(k):
            return k
        i0, j0 = k % self.nx, k // self.nx
        for r in range(1, maxr + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.nx and 0 <= j < self.ny and self.free(j * self.nx + i):
                        return j * self.nx + i
        return k


class Search:
    """Incremental A* so a long search spreads over several decisions."""

    def __init__(self, grid, start, goal):
        self.g = grid
        self.start, self.goal = start, goal
        self.gx, self.gy = goal % grid.nx, goal // grid.nx
        self.open = [(0.0, start)]
        self.cost = {start: 0.0}
        self.prev = {start: -1}
        self.done = False
        self.path = None
        self.expanded = 0

    def h(self, k):
        i, j = k % self.g.nx, k // self.g.nx
        dx, dy = abs(i - self.gx), abs(j - self.gy)
        return (max(dx, dy) + 0.414 * min(dx, dy)) * CELL

    def step(self, deadline):
        g = self.g
        nx, ny = g.nx, g.ny
        blocked, soft = g.blocked, g.soft
        openl, cost, prev = self.open, self.cost, self.prev
        n = 0
        while openl:
            n += 1
            if (n & 255) == 0 and time.perf_counter() > deadline:
                return False
            f, k = heapq.heappop(openl)
            if k == self.goal:
                self.finish(k)
                return True
            ck = cost[k]
            self.expanded += 1
            if self.expanded > 60000:
                break
            i, j = k % nx, k // nx
            for di, dj, d in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                              (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)):
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= nx or jj >= ny:
                    continue
                kk = jj * nx + ii
                if blocked[kk]:
                    continue
                if di and dj and (blocked[j * nx + ii] or blocked[jj * nx + i]):
                    continue
                c = ck + d * CELL * (1.6 if soft[kk] == 2 else 1.0)
                if c < cost.get(kk, 1e18):
                    cost[kk] = c
                    prev[kk] = k
                    heapq.heappush(openl, (c + self.h(kk), kk))
        self.done = True
        self.path = []
        return True

    def finish(self, k):
        path = []
        while k != -1:
            path.append(k)
            k = self.prev[k]
        path.reverse()
        self.done = True
        self.path = path


class Bot:
    name = "survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        random.seed(hello.get("seed", 0))
        self.grid = None
        try:
            self.grid = Grid(hello["map"]["dir"], self.cfg["mech"])
        except Exception as e:  # never die over the map
            log("grid failed", repr(e))
        self.reach = self.cfg["loot"]["pickup_radius"]
        self.gun_speed = self.cfg["weapons"]["gun"]["speed"] if "weapons" in self.cfg else 252
        self.goal = None
        self.goal_key = None
        self.search = None
        self.path = []
        self.path_i = 0
        self.last_pos = None
        self.stuck = 0
        self.unstick = 0
        self.unstick_dir = 0.0
        self.threat = None  # (bearing, time)
        self.last_t = 0.0
        self.laser_slot = None
        self.wanted = ("laser", "lightning")

    # ---------- planning ----------
    def zone_at(self, z, t):
        """Zone circle at time t (linear shrink)."""
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        a = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = [z["center"][0] + (z["next_center"][0] - z["center"][0]) * a,
             z["center"][1] + (z["next_center"][1] - z["center"][1]) * a]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * a

    def pick_goal(self, me, z, avoid):
        """A covered cell inside the next circle, close to us, away from known dangers."""
        g = self.grid
        nc, nr = z["next_center"], z["next_radius"]
        x, y = me["pos"][0], me["pos"][1]
        safe_r = max(nr * 0.6, min(nr, 6.0))
        if nr <= 1:
            return list(nc)
        if g is None:
            dx, dy = x - nc[0], y - nc[1]
            d = math.hypot(dx, dy) or 1
            r = min(d, safe_r * 0.8)
            return [nc[0] + dx / d * r, nc[1] + dy / d * r]
        best, best_s = None, 1e18
        rc = int(safe_r / CELL) + 1
        ci, cj = int((nc[0] - g.minx) / CELL), int((nc[1] - g.miny) / CELL)
        step = max(1, rc // 25)
        for dj in range(-rc, rc + 1, step):
            for di in range(-rc, rc + 1, step):
                i, j = ci + di, cj + dj
                if i < 0 or j < 0 or i >= g.nx or j >= g.ny:
                    continue
                k = j * g.nx + i
                if g.blocked[k]:
                    continue
                cx, cy = g.center(i, j)
                dc = math.hypot(cx - nc[0], cy - nc[1])
                if dc > safe_r:
                    continue
                s = math.hypot(cx - x, cy - y) - 6.0 * min(g.cover[k], 12) + 0.3 * dc
                for ax, ay, ar in avoid:
                    da = math.hypot(cx - ax, cy - ay)
                    if da < ar:
                        s += (ar - da) * 4
                if s < best_s:
                    best_s, best = s, (cx, cy)
        if best is None:
            return list(nc)
        return list(best)

    def plan(self, me, deadline):
        g = self.grid
        if g is None or self.goal is None:
            return
        start = g.nearest_free(g.cell(me["pos"][0], me["pos"][1]))
        goal = g.nearest_free(g.cell(self.goal[0], self.goal[1]))
        if self.search is None or self.search.goal != goal:
            if self.path and goal == self.path[-1]:
                return
            self.search = Search(g, start, goal)
        if not self.search.done:
            self.search.step(deadline)
        if self.search.done:
            self.path = self.search.path or []
            self.path_i = 0
            self.search_done_goal = goal
            self.search = None

    def follow(self, me):
        """Direction to move along the path, or straight at the goal."""
        x, y = me["pos"][0], me["pos"][1]
        if self.goal is None:
            return None
        if math.hypot(self.goal[0] - x, self.goal[1] - y) < 3.0:
            return None
        g = self.grid
        if g is not None and self.path:
            # advance to the furthest path node we are close to
            best = self.path_i
            for idx in range(self.path_i, min(len(self.path), self.path_i + 12)):
                cx, cy = g.center(self.path[idx] % g.nx, self.path[idx] // g.nx)
                if math.hypot(cx - x, cy - y) < CELL * 1.2:
                    best = idx + 1
            self.path_i = min(best, len(self.path) - 1)
            tx, ty = g.center(self.path[self.path_i] % g.nx, self.path[self.path_i] // g.nx)
            if self.path_i == len(self.path) - 1:
                tx, ty = self.goal[0], self.goal[1]
        else:
            tx, ty = self.goal[0], self.goal[1]
        return math.atan2(ty - y, tx - x)

    # ---------- shooting ----------
    def lead(self, me, t, speed):
        sx, sy, sz = me["pos"]
        mh = me.get("stats", {}).get("muzzle_height", 7)
        ox, oy, oz = sx, sy, sz + mh
        px, py, pz = t["pos"]
        vx, vy, vz = t.get("vel", [0, 0, 0])
        tz = pz + 4.5
        tt = 0.0
        for _ in range(3):
            ax, ay, az = px + vx * tt, py + vy * tt, tz + vz * tt
            d = math.sqrt((ax - ox) ** 2 + (ay - oy) ** 2 + (az - oz) ** 2)
            tt = d / speed + 0.1  # plus one decision of latency
        ax, ay, az = px + vx * tt, py + vy * tt, tz + vz * tt
        dh = math.hypot(ax - ox, ay - oy)
        return [math.atan2(ay - oy, ax - ox), math.atan2(az - oz, dh)], dh

    # ---------- helpers ----------
    def seen(self, obs, me):
        x, y = me["pos"][0], me["pos"][1]
        mechs, npcs = [], []
        for m in obs["mechs"]:
            m["_d"] = math.dist(m["pos"][:2], (x, y))
            (mechs if m["kind"] == "mech" else npcs).append(m)
        mechs.sort(key=lambda m: m["_d"])
        npcs.sort(key=lambda m: m["_d"])
        return mechs, npcs

    def choose_target(self, enemies):
        """Weakest effective target: low hp, close, or the one shooting us."""
        best, bs = None, 1e18
        for e in enemies:
            hp = e.get("hp") or e.get("max_hp") or 1400
            s = hp + 6.0 * e["_d"]
            if self.threat and e["id"] == self.threat[2]:
                s -= 300
            if s < bs:
                bs, best = s, e
        return best

    def upgrade_pick(self, offer):
        pref = ["armor", "plating", "gun_damage", "lightning_efficiency", "gun_rate", "laser_damage",
                "nanites", "lightning_chain", "laser_charge", "servos", "gun_mag", "lightning_range",
                "gun_velocity", "reactor", "capacitor", "sensors", "jets", "gyros"]
        ids = [c["id"] for c in offer["choices"]]
        for p in pref:
            if p in ids:
                return ids.index(p)
        return 0

    # ---------- main ----------
    def act(self, obs):
        t0 = time.perf_counter()
        deadline = t0 + TICK_BUDGET
        me = obs["self"]
        x, y, z = me["pos"]
        t = obs["t"]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        zone = obs["zone"]
        weapons = me["weapons"]
        kinds = [w["kind"] if w else None for w in weapons]
        if obs.get("offer"):
            act["pick"] = self.upgrade_pick(obs["offer"])

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.threat = (ev["bearing"], t, ev.get("by"))
                self.last_hurt = t
            if ev.get("type") == "hit":
                self.last_dealt = t
        # accuracy instrumentation (stderr only)
        g = weapons[0]
        st = self.__dict__.setdefault("stats", {"shots": 0, "gun_hits": 0, "laser_shots": 0, "laser_hits": 0})
        if g and g["kind"] == "gun":
            pa = getattr(self, "prev_ammo", None)
            if pa is not None and g.get("ammo", 0) < pa:
                st["shots"] += pa - g["ammo"]
            self.prev_ammo = g.get("ammo", 0)
        w1s = weapons[1]
        if w1s and w1s["kind"] == "laser":
            if getattr(self, "prev_lcd", 0) <= 0 and w1s["cooldown"] > 0.5:
                st["laser_shots"] += 1
            self.prev_lcd = w1s["cooldown"]
        for ev in obs.get("events", []):
            if ev.get("type") == "hit" and ev.get("target", 0) < 100:
                if ev.get("weapon") == "gun":
                    st["gun_hits"] += 1
                elif ev.get("weapon") == "laser":
                    st["laser_hits"] += 1
        if obs["tick"] % 200 == 0:
            log("stats", t, json.dumps(st))
        enemies, npcs = self.seen(obs, me)
        visible_ids = {m["id"] for m in obs["mechs"]}
        blind_hit = bool(self.threat and t - self.threat[1] < 1.5 and self.threat[2] is not None
                         and self.threat[2] < 100 and self.threat[2] not in visible_ids)
        cc, cr = self.zone_at(zone, t + 3)
        dz = math.hypot(x - cc[0], y - cc[1])
        urgent = dz > cr - 4 or not me["in_zone"]
        nc, nr = zone["next_center"], zone["next_radius"]
        has_lightning = "lightning" in kinds

        # ----- target -----
        target = self.choose_target(enemies)
        attacker = None
        if self.threat and t - self.threat[1] < 3:
            for m in obs["mechs"]:
                if m["id"] == self.threat[2]:
                    attacker = m
        npc_target = None
        if target is None:
            for n in npcs:
                if attacker is n or (n["_d"] < 70 and hp_frac > 0.6):
                    npc_target = n
                    break

        # recover: when low and nobody is on top of us, stop shooting and get out of sight to regenerate
        near = min((e["_d"] for e in enemies), default=1e9)
        if hp_frac < 0.4 and near > 55:
            self.recovering = True
        if getattr(self, "recovering", False) and (hp_frac > 0.85 or near < 40):
            self.recovering = False
        recovering = getattr(self, "recovering", False) and not urgent
        if recovering:
            npc_target = None

        # ----- loot -----
        detour = None
        quiet = target is None or target["_d"] > 110
        cands = []
        for c in obs.get("cubes", []):
            d = math.dist(c["pos"][:2], (x, y))
            if abs(c["pos"][2] - z) < 5 and d < (70 if quiet else 25):
                cands.append((d, c["pos"]))
        for p in obs.get("pickups", []):
            if abs(p["pos"][2] - z) > 5 or p["kind"] in kinds:
                continue
            if p["kind"] in ("lightning", "laser") and kinds[1] in (None, "mortar"):
                d = math.dist(p["pos"][:2], (x, y))
                bonus = 70 if p["kind"] == "laser" else 40
                if d < ((220 if p["kind"] == "laser" else 160) if quiet else 30):
                    cands.append((d - bonus, p["pos"]))
        if cands and not urgent:
            cands.sort(key=lambda c: c[0])
            detour = cands[0][1]
        for p in obs.get("pickups", []):
            if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4 and p["kind"] not in kinds:
                if p["kind"] in ("lightning", "laser"):
                    if kinds[1] is None:
                        act["take"] = p["id"]
                        break
                    if kinds[1] == "mortar":
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break

        # ----- movement -----
        move_yaw = None
        boost = False
        if recovering:
            rx = ry = 0.0
            for e in enemies + npcs:
                dd = max(e["_d"], 5.0)
                rx += (x - e["pos"][0]) / dd / dd
                ry += (y - e["pos"][1]) / dd / dd
            if self.threat and t - self.threat[1] < 6:
                rx -= math.cos(self.threat[0]) * 0.01
                ry -= math.sin(self.threat[0]) * 0.01
            if rx or ry:
                move_yaw = math.atan2(ry, rx)
                to_c = math.atan2(cc[1] - y, cc[0] - x)
                if dz > cr * 0.6 and abs(wrap(move_yaw - to_c)) > 1.4:
                    move_yaw = wrap(to_c + (1.4 if wrap(move_yaw - to_c) > 0 else -1.4))
                boost = me["energy"] > 25
        elif target is not None and not urgent:
            tx, ty = target["pos"][0], target["pos"][1]
            to_t = math.atan2(ty - y, tx - x)
            d = target["_d"]
            ehp = target.get("hp") or 1400
            want = 32.0 if has_lightning else 70.0
            outnumbered = len([e for e in enemies if e["_d"] < 130]) >= 3
            losing = hp_frac < 0.3 and ehp > me["hp"] * 1.2
            if t >= getattr(self, "strafe_next", -1):
                self.strafe_next = t + 0.3 + 0.6 * random.random()
                self.strafe_s = -getattr(self, "strafe_s", 1) if random.random() < 0.75 else getattr(self, "strafe_s", 1)
            perp = to_t + self.strafe_s * math.pi / 2
            if losing or (outnumbered and hp_frac < 0.6):
                rx = ry = 0.0
                for e in enemies:
                    dd = max(e["_d"], 5.0)
                    rx += (x - e["pos"][0]) / dd / dd
                    ry += (y - e["pos"][1]) / dd / dd
                away = math.atan2(ry, rx)
                to_c = math.atan2(cc[1] - y, cc[0] - x)
                if dz > cr * 0.6 and abs(wrap(away - to_c)) > 1.4:
                    away = wrap(to_c + (1.4 if wrap(away - to_c) > 0 else -1.4))
                move_yaw = away
                boost = me["energy"] > 10
            else:
                radial = 0.0
                if d > want + 15:
                    radial = 1.0
                elif d < want - 12:
                    radial = -0.8
                mx = math.cos(perp) + radial * math.cos(to_t)
                my = math.sin(perp) + radial * math.sin(to_t)
                move_yaw = math.atan2(my, mx)
                if d < 150 and me["energy"] > 25:
                    boost = True
                if target.get("charging"):
                    move_yaw = perp
                    boost = True
        elif npc_target is not None and not urgent:
            n = npc_target
            to_n = math.atan2(n["pos"][1] - y, n["pos"][0] - x)
            if n["_d"] > 45:
                move_yaw = to_n
            else:
                move_yaw = to_n + math.pi / 2 * (1 if int(t / 2) % 2 else -1)
        elif detour is not None:
            move_yaw = math.atan2(detour[1] - y, detour[0] - x)
        else:
            key = (round(nc[0]), round(nc[1]), round(nr))
            if self.goal is None or self.goal_key != key:
                self.goal = self.pick_goal(me, zone, [(n["pos"][0], n["pos"][1], 40.0) for n in npcs])
                self.goal_key = key
                self.path = []
                self.search = None
            self.plan(me, deadline)
            move_yaw = self.follow(me)
        if urgent:
            to_c = math.atan2(cc[1] - y, cc[0] - x)
            if move_yaw is None or abs(wrap(move_yaw - to_c)) > 1.2:
                move_yaw = to_c
            boost = True

        # stuck handling
        if self.last_pos is not None and move_yaw is not None:
            moved = math.dist(self.last_pos[:2], (x, y))
            self.stuck = self.stuck + 1 if moved < 0.3 else 0
        self.last_pos = (x, y, z)
        jump = False
        if self.unstick > 0:
            self.unstick -= 1
            move_yaw = self.unstick_dir
            jump = me.get("grounded", False)
            boost = True
        elif self.stuck >= 5 and move_yaw is not None:
            self.unstick = 6
            self.unstick_dir = wrap(move_yaw + random.choice((-1.3, 1.3)))
            self.stuck = 0
            self.path = []
            self.search = None
            self.strafe_s = -getattr(self, "strafe_s", 1)
        if me["energy"] < 12 and not urgent:
            boost = False
        act["move"] = [math.cos(move_yaw), math.sin(move_yaw)] if move_yaw is not None else [0.0, 0.0]
        act["boost"] = bool(boost and move_yaw is not None)
        act["jump"] = jump

        # ----- aim and fire -----
        fire = [False, False]
        aim = None
        shoot = target if target is not None else npc_target
        if recovering and (target is None or target["_d"] > 40):
            shoot = None
        if blind_hit and (shoot is None or shoot.get("_d", 0) > 35):
            shoot = None  # turn to whoever is hitting us from out of sight
        if shoot is not None:
            aim, dh = self.lead(me, shoot, self.gun_speed)
            err_y = abs(wrap(me["aim"][0] - aim[0]))
            err_p = abs(me["aim"][1] - aim[1])
            tol = math.atan2(3.2, max(dh, 1.0)) + 0.03
            g0 = weapons[0]
            if g0 and g0["kind"] == "gun":
                if g0.get("ammo", 1) == 0 and g0.get("reload", 0) == 0:
                    act["reload"] = True
                elif err_y < tol and err_p < tol + 0.05 and dh < g0["stats"].get("range", 230) * 0.95:
                    fire[0] = True
            w1 = weapons[1]
            if w1:
                if w1["kind"] == "laser":
                    direct = [math.atan2(shoot["pos"][1] - y, shoot["pos"][0] - x),
                              math.atan2(shoot["pos"][2] + 4.5 - (z + 7), max(dh, 1))]
                    ch = w1.get("charge", 0)
                    full = w1["stats"].get("charge", 0.75)
                    if w1["cooldown"] <= 0 and dh < w1["stats"].get("range", 280) * 0.95:
                        lined = abs(wrap(me["aim"][0] - direct[0])) < math.atan2(2.0, max(dh, 1)) and abs(me["aim"][1] - direct[1]) < 0.05
                        fire[1] = not (ch >= full * 0.97 and lined)
                    if ch > 0:
                        aim = direct
                elif w1["kind"] == "lightning":
                    if dh < w1["stats"].get("range", 45) * 0.97 and err_y < 0.45 and me["energy"] > 6:
                        fire[1] = True
        elif self.threat and t - self.threat[1] < 4 and not (recovering and not blind_hit):
            aim = [self.threat[0], 0.0]
        if aim is None and recovering and enemies:
            e = enemies[0]
            aim = [math.atan2(e["pos"][1] - y, e["pos"][0] - x), 0.0]  # keep our front toward them
        if aim is None:
            aim = [move_yaw, 0.0] if move_yaw is not None else [wrap(me["aim"][0] + 0.6), 0.0]
        act["aim"] = aim
        act["fire"] = fire
        return act


if __name__ == "__main__":
    mf.run(Bot)
