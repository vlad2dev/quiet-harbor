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
TICK_BUDGET = 0.012  # seconds of path search per decision
FLAGS = set(f for f in os.environ.get("FR_FLAGS", "early,agg,aim2,laserseek,econ,drops2,ffa,gmove,opening").split(",") if f)  # behaviour switches; env overrides (dev only)


PARAMS = json.loads(os.environ.get("FR_PARAMS", "{}"))
AGG_CLOSE = PARAMS.get("agg_close", 60)
AGG_LOS = PARAMS.get("agg_los", 175)
ENGAGE_RADIUS = PARAMS.get("engage_radius", 90)
DISTANCE_WEIGHT = PARAMS.get("distance_weight", 6.0)
ATTACKER_BONUS = PARAMS.get("attacker_bonus", 300)
DROP_RADIUS = PARAMS.get("drop_radius", 230)
UPGRADE_FIRST = PARAMS.get("upgrade_first", [])
DRONE_RADIUS = PARAMS.get("drone_radius", 130)
CRATE_RADIUS = PARAMS.get("crate_radius", 160)
HEAL_BELOW_LASER = PARAMS.get("heal_below_laser", 0.7)
HEAL_BELOW = PARAMS.get("heal_below", 0.8)
HEAL_TO_EARLY = PARAMS.get("heal_to_early", 0.95)
HEAL_TO = PARAMS.get("heal_to", 0.9)
AGG_HEAL_BELOW = PARAMS.get("agg_heal_below", 0.5)
AGG_HEAL_TO = PARAMS.get("agg_heal_to", 0.75)
RECOVER_EXIT = PARAMS.get("recover_exit", 0.97)
LIGHTNING_RANGE = PARAMS.get("lightning_range", 32.0)
LASER_RANGE = PARAMS.get("laser_range", 110.0)
GUN_RANGE = PARAMS.get("gun_range", 70.0)
RETREAT_HP = PARAMS.get("retreat_hp", 0.35)
RETREAT_TWO_HP = PARAMS.get("retreat_two_hp", 0.55)
RETREAT_THREE_HP = PARAMS.get("retreat_three_hp", 0.8)
EARLY_RETREAT_HP = PARAMS.get("early_retreat_hp", 0.5)
AGG_RETREAT_HP = PARAMS.get("agg_retreat_hp", 0.4)
AGG_THREAT_COUNT = PARAMS.get("agg_threat_count", 6)
AGG_TWO_HP = PARAMS.get("agg_two_hp", 0.5)
AGG_OUTGUNNED_HP = PARAMS.get("agg_outgunned_hp", 0.8)
STRAFE_MIN = PARAMS.get("strafe_min", 0.3)
STRAFE_JITTER = PARAMS.get("strafe_jitter", 0.6)
CHARGE_ENERGY = PARAMS.get("charge_energy", 70)
CUBE_RADIUS = PARAMS.get("cube_radius", 90)
BOOST_FLOOR = PARAMS.get("boost_floor", 12)
LASER_WIDTH = PARAMS.get("laser_width", 2.0)
LASER_AIM_FRACTION = PARAMS.get("laser_aim_fraction", 0.0)
LASER_PITCH = PARAMS.get("laser_pitch", 0.05)


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
        self.htop = [0.0] * n  # tallest solid above ground in the cell (m)
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
                    if p["top"] - g > self.htop[k]:
                        self.htop[k] = p["top"] - g
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
        self.build_fire_raster(world)
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

    def los(self, ax, ay, bx, by, h=7.5):
        """Line of fire between two mechs: no building taller than h m on the segment (2.5 m steps)."""
        dx, dy = bx - ax, by - ay
        n = int(math.hypot(dx, dy) / 2.5)
        if n < 2:
            return True
        htop, nx, ny, minx, miny = self.htop, self.nx, self.ny, self.minx, self.miny
        inv = 1.0 / CELL
        sx, sy = dx / n, dy / n
        px, py = ax - minx, ay - miny
        for _ in range(1, n):
            px += sx
            py += sy
            i = int(px * inv)
            j = int(py * inv)
            if 0 <= i < nx and 0 <= j < ny and htop[j * nx + i] > h:
                return False
        return True

    def build_fire_raster(self, world, step=2.0):
        """Fine raster for line of fire: absolute top of the solid stack per 2 m cell and the ground under it."""
        b = world["bounds"]
        self.fs = step
        self.fnx = int((b["maxX"] - b["minX"]) / step) + 1
        self.fny = int((b["maxY"] - b["minY"]) / step) + 1
        n = self.fnx * self.fny
        top = array("f", [-1e9]) * n
        for p in world["prisms"]:
            if p["kind"] == "bridge":
                continue  # shots pass under bridges; their decks are thin
            rings = [p["ring"]] + list(p.get("holes") or [])
            edges = []
            for r in rings:
                m = len(r) // 2
                for e in range(m):
                    x1, y1 = r[2 * e], r[2 * e + 1]
                    x2, y2 = r[2 * ((e + 1) % m)], r[2 * ((e + 1) % m) + 1]
                    if y1 != y2:
                        edges.append((x1, y1, x2, y2))
            ys = p["ring"][1::2]
            j0 = max(0, int((min(ys) - self.miny) / step))
            j1 = min(self.fny - 1, int((max(ys) - self.miny) / step) + 1)
            ptop = p["top"]
            for j in range(j0, j1 + 1):
                yc = self.miny + (j + 0.5) * step
                xs = sorted(x1 + (yc - y1) * (x2 - x1) / (y2 - y1) for x1, y1, x2, y2 in edges if (y1 <= yc) != (y2 <= yc))
                row = j * self.fnx
                for a in range(0, len(xs) - 1, 2):
                    i0 = max(0, int((xs[a] - self.minx) / step + 0.5))
                    i1 = min(self.fnx - 1, int((xs[a + 1] - self.minx) / step - 0.5))
                    for i in range(i0, i1 + 1):
                        if top[row + i] < ptop:
                            top[row + i] = ptop
        gr = array("f", [0.0]) * n
        for j in range(self.fny):
            for i in range(self.fnx):
                gr[j * self.fnx + i] = self.height(self.minx + (i + 0.5) * step, self.miny + (j + 0.5) * step)
        self.ftop, self.fground = top, gr

    def fire_clear(self, ax, ay, az, bx, by, bz, skip_end=2.5):
        """True if a straight shot from (ax, ay, az) reaches (bx, by, bz) without hitting a building or the ground."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        dist = math.hypot(dx, dy)
        if dist < 3:
            return True
        n = int(dist / 1.5)
        top, gr, fnx, fny, inv = self.ftop, self.fground, self.fnx, self.fny, 1.0 / self.fs
        x0, y0 = ax - self.minx, ay - self.miny
        last = 1.0 - skip_end / dist
        for s in range(1, n):
            f = s / n
            if f > last:
                break
            i = int((x0 + dx * f) * inv)
            j = int((y0 + dy * f) * inv)
            if 0 <= i < fnx and 0 <= j < fny:
                k = j * fnx + i
                z = az + dz * f
                if top[k] > z or gr[k] > z + 0.2:
                    return False
        return True

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
        self.failed_goal = None
        self.threat = None  # (bearing, time)
        self.last_t = 0.0
        self.laser_chase = None
        self.skip_laser_until = {}
        self.reload_window = None
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
        if "jetescape" in FLAGS:
            self.planned_at = self.now
        start = g.nearest_free(g.cell(me["pos"][0], me["pos"][1]))
        goal = g.nearest_free(g.cell(self.goal[0], self.goal[1]))
        if self.search is None or self.search.goal != goal:
            if self.path and goal == self.path[-1]:
                return
            self.search = Search(g, start, goal)
        if not self.search.done:
            self.search.step(deadline)
        if self.search.done:
            if "jetescape" in FLAGS:
                self.failed_goal = self.goal_key if not self.search.path and not self.search.open else None
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
        mv = me.get("vel", [0, 0, 0])
        ox, oy, oz = sx + mv[0] * 0.1, sy + mv[1] * 0.1, sz + mh  # where our muzzle is when the shot leaves
        px, py, pz = t["pos"]
        vx, vy, vz = t.get("vel", [0, 0, 0])
        kind = t.get("kind", "crate")
        tz = pz + (4.5 if kind == "mech" else 1.75 if kind == "npc" else 0.8)
        if kind == "mech" and "aim2" in FLAGS:
            tz = pz + t.get("_aimh", 4.5 * (1 + 0.05 * (t.get("cubes") or 0)))
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

    def facing_error(self, enemy):
        aim = enemy.get("aim")
        if not aim:
            return None
        ex, ey = enemy["pos"][:2]
        return abs(wrap(aim[0] - math.atan2(self.mypos[1] - ey, self.mypos[0] - ex)))

    def opening_threat(self, enemy, hurt_by, now):
        if enemy["_d"] < 60 or now - hurt_by.get(enemy["id"], -99) < 4:
            return True
        if enemy["_d"] >= 120:
            return False
        if (enemy.get("hp") or 1400) < 350:
            return True
        error = self.facing_error(enemy)
        return enemy.get("charging") and error is not None and error < 0.22

    def duel_candidates(self, enemies, me, hp, t):
        """Keep one opponent through a duel, then require a quiet repair before reopening."""
        active = getattr(self, "duel_id", None)
        hit_by = getattr(self, "hurt_by", {})
        threats = {e["id"] for e in enemies if e["_d"] < 130}
        threats.update(i for i, when in hit_by.items() if isinstance(i, int) and i < 16 and t - when < 4)
        if hp < 0.5 or len(threats) > 1:
            self.duel_rest = True
        if getattr(self, "duel_rest", False):
            self.duel_id = None
            if hp < 0.98 or len(threats) > 1 or t - getattr(self, "last_hurt", -99) < 5:
                return []
            self.duel_rest = False
        for e in enemies:
            if e["id"] == active and e["_d"] <= 145:
                return [e]
        self.duel_id = None
        if hp < 0.9:
            self.duel_rest = True
            return []
        armed = any(w and w["kind"] == "laser" for w in me["weapons"])
        return [e for e in enemies if e["_d"] <= (115 if armed else 90) and
                (not threats or e["id"] in threats)]

    def patrol_point(self, me, zone, enemies):
        center, r = zone["next_center"], zone["next_radius"]
        if r <= 25.5:
            return center
        phase = math.atan2(me["pos"][1] - center[1], me["pos"][0] - center[0])
        best, cost = center, math.inf
        for step in (1, -1, 2, -2, 3, -3, 4, -4):
            yaw = phase + step * math.pi / 6
            pt = (center[0] + 0.5*r*math.cos(yaw), center[1] + 0.5*r*math.sin(yaw))
            if self.grid and not self.grid.free(self.grid.cell(*pt)):
                continue
            near = min((math.dist(pt, e["pos"][:2]) for e in enemies), default=115)
            score = abs(near - 115) + 0.25 * math.dist(pt, me["pos"][:2])
            if score < cost:
                best, cost = pt, score
        return best

    def hunt_target(self, e, me, t):
        d = e["_d"]
        if d > 240:
            return False
        if d < 60 or t - getattr(self, "hurt_by", {}).get(e["id"], -99) < 6:
            return True
        if d < 160 and (e.get("hp") or 1400) < 0.4 * (e.get("max_hp") or 1400):
            return True
        error = self.facing_error(e)
        if d < 190 and error is not None and error < 0.22:
            return True
        vx, vy = (e.get("vel") or (0, 0))[:2]
        mx, my = (me.get("vel") or (0, 0))[:2]
        ex, ey = e["pos"][:2]
        closing = ((vx - mx) * (self.mypos[0] - ex) + (vy - my) * (self.mypos[1] - ey)) / max(1, d)
        return d < 180 and closing > 3

    def showing_back(self, enemy):
        error = self.facing_error(enemy)
        return error is not None and error > math.pi / 2

    def choose_target(self, enemies):
        """Weakest effective target: low hp, close, or the one shooting us.

        Squad hunters come one at a time; shooting idle far-away ones only wakes more of them. Engage a mech
        only if it hurt us recently or is within 90 m."""
        best, bs = None, 1e18
        hurt_by = getattr(self, "hurt_by", {})
        now = getattr(self, "now", 0.0)
        if "opening" in FLAGS and now < 20:
            enemies = [e for e in enemies if self.opening_threat(e, hurt_by, now)]
        if "aim2" in FLAGS and self.grid is not None and getattr(self, "muzzle", None) is not None:
            return self.choose_target_lof(enemies, hurt_by, now)
        ffa = getattr(self, "ffa", False) and getattr(self, "alive", 16) > 4
        agg = "agg" in FLAGS and self.grid is not None and getattr(self, "mypos", None) is not None
        for e in enemies:
            if agg:
                # anyone in line of sight within 175 m, anyone within 60 m, or whoever hurt us
                if not (e["_d"] <= AGG_CLOSE or now - hurt_by.get(e["id"], -99) < 10 or
                        (e["_d"] <= AGG_LOS and self.grid.los(self.mypos[0], self.mypos[1], e["pos"][0], e["pos"][1]))):
                    continue
            elif e["_d"] > ENGAGE_RADIUS and now - hurt_by.get(e["id"], -99) > 10:
                continue
            if ffa:
                # free-for-all: others die without our help; fight only who hurts us, who is on top of us,
                # or a cheap kill (their cubes)
                frac = (e.get("hp") or 1400) / max(1.0, e.get("max_hp") or 1400)
                if not (now - hurt_by.get(e["id"], -99) < 5 or e["_d"] < 45 or (frac < 0.35 and e["_d"] < 120)):
                    continue
            hp = e.get("hp") or e.get("max_hp") or 1400
            s = hp + DISTANCE_WEIGHT * e["_d"]
            if "backstab" in FLAGS and self.showing_back(e):
                s -= 60 * DISTANCE_WEIGHT
            if self.threat and e["id"] == self.threat[2]:
                s -= ATTACKER_BONUS
            if s < bs:
                bs, best = s, e
        return best

    def lof_height(self, e):
        """Aim height on a mech we have a line of fire to: chest, else shoulders; None if neither is clear."""
        g, (mx, my, mz) = self.grid, self.muzzle
        sc = 1 + 0.05 * (e.get("cubes") or 0)
        ex, ey, ez = e["pos"]
        for h in (4.5 * sc, 7.7 * sc):
            if g.fire_clear(mx, my, mz, ex, ey, ez + h, skip_end=2.5 * sc):
                return h
        return None

    def choose_target_lof(self, enemies, hurt_by, now):
        """Target score in metres: distance + 0.18*HP, big bonuses for finishing, sticking and answering fire.
        Mechs without a line of fire stay selectable (to face them) but rank far behind."""
        best, bs = None, 1e18
        last_id, last_t = getattr(self, "tgt_id", None), getattr(self, "tgt_t", -99)
        ffa = getattr(self, "ffa", False) and getattr(self, "alive", 16) > 4
        mx, my = self.muzzle[0], self.muzzle[1]
        for e in enemies:
            hit_us = now - hurt_by.get(e["id"], -99)
            hp = e.get("hp") or e.get("max_hp") or 1400
            frac = hp / max(1.0, e.get("max_hp") or 1400)
            d = e["_d"]
            if "hunt" in FLAGS:
                # engage only mechs that matter now: close, shooting us, closing in, weak, aiming at us, or late game
                vx_, vy_ = (e.get("vel") or [0, 0, 0])[:2]
                closing_ = (vx_ * (mx - e["pos"][0]) + vy_ * (my - e["pos"][1])) / max(d, 1)
                eaim = (e.get("aim") or [9.0])[0]
                aimed = abs(wrap(eaim - math.atan2(my - e["pos"][1], mx - e["pos"][0]))) < 0.22
                late = getattr(self, "next_r", 999) <= 70 or getattr(self, "alive", 16) <= 3
                if d > 240 or not (d < 60 or hit_us < 6 or late or (closing_ > 3 and d < 180) or
                                   (frac < 0.4 and d < 160) or (aimed and d < 190)):
                    continue
            elif "agg" in FLAGS:
                if not (getattr(self, "hunt_on", False) or d <= 175 or hit_us < 10):
                    continue
            elif d > 90 and hit_us > 10:
                continue
            if ffa and not ("duel" in FLAGS and getattr(self, "ffa", False)) and not (hit_us < 5 or d < 45 or (frac < 0.35 and d < 120)):
                continue
            h = self.lof_height(e) if d < 260 else None
            e["_aimh"] = h if h is not None else 4.5 * (1 + 0.05 * (e.get("cubes") or 0))
            e["_lof"] = h is not None
            s = d + 0.18 * hp
            if "backstab" in FLAGS and self.showing_back(e):
                s -= 60
            if frac <= 0.3:
                s -= 200
            if e["id"] == last_id and now - last_t < 2:
                s -= 200
            if hit_us < 4:
                s -= 40
            vx, vy = (e.get("vel") or [0, 0, 0])[:2]
            if (vx * (mx - e["pos"][0]) + vy * (my - e["pos"][1])) / max(d, 1) > 3:
                s -= 15  # closing on us
            if h is None:
                s += 1000
            if s < bs:
                bs, best = s, e
        if best is not None:
            self.tgt_id, self.tgt_t = best["id"], now
        return best

    def steer(self, me, target, enemies, zone, t, want_los, goal_yaw, goal_w, rmin, rmax, perp_w):
        """Score 16 headings plus 'stand' each decision: goal alignment, walls, zone, strafe, peek
        (be in line of fire when a weapon is ready, out of it when not), exposure to other threats, smoothing.
        Returns a yaw or None (stand)."""
        g = self.grid
        x, y, z = me["pos"]
        sp = me.get("stats", {}).get("walk_speed", 11.7)
        tx, ty, tz = target["pos"]
        d = math.hypot(tx - x, ty - y)
        to_t = math.atan2(ty - y, tx - x)
        th = tz + 4.5 * (1 + 0.05 * (target.get("cubes") or 0))
        mh = me.get("stats", {}).get("muzzle_height", 7)
        others = [e for e in enemies if e is not target and e["_d"] < 200][:3]
        cc, cr = self.zone_at(zone, t + 1.0)
        last = getattr(self, "steer_last", None)
        if t >= getattr(self, "steer_flip", -1):
            self.steer_flip = t + 0.45 + 0.7 * random.random()
            self.steer_side = -getattr(self, "steer_side", 1)
        side = getattr(self, "steer_side", 1)
        perp = to_t + side * math.pi / 2
        best, bs = None, -1e9
        for k in range(17):
            if k == 16:
                yaw, step1, step2 = None, 0.0, 0.0
            else:
                yaw = k * math.pi / 8
                step1, step2 = sp * 0.45, sp * 1.0
            cx, cy = (math.cos(yaw), math.sin(yaw)) if yaw is not None else (0.0, 0.0)
            p1x, p1y = x + cx * step1, y + cy * step1
            p2x, p2y = x + cx * step2, y + cy * step2
            sc = 0.0
            if goal_yaw is not None and yaw is not None:
                sc += goal_w * math.cos(yaw - goal_yaw)
            if yaw is not None and g.blocked[g.cell(p1x, p1y)] == 1:
                sc -= 6.0
            if math.hypot(p2x - cc[0], p2y - cc[1]) > cr - 3:
                sc -= 2.5
            if yaw is not None:
                sc += perp_w * math.cos(yaw - perp)
                # keep the preferred band
                nd = math.hypot(tx - p2x, ty - p2y)
                if nd < rmin:
                    sc -= (rmin - nd) / 10.0
                elif nd > rmax:
                    sc -= (nd - rmax) / 10.0
            if want_los:
                l1 = g.fire_clear(p1x, p1y, g.height(p1x, p1y) + mh, tx, ty, th)
                l2 = g.fire_clear(p2x, p2y, g.height(p2x, p2y) + mh, tx, ty, th) if yaw is not None else l1
                sc += want_los * ((1.3 if l1 else -1.3) + (0.6 if l2 else -0.6))
            for e in others:
                if g.los(p1x, p1y, e["pos"][0], e["pos"][1]):
                    sc -= 0.45
            if last is not None and yaw is not None:
                sc += 0.15 * math.cos(yaw - last)
            if sc > bs:
                bs, best = sc, yaw
        self.steer_last = best
        return best

    def gmove(self, me, target, enemies, zone, t, ready, pref, near_cube):
        """Fight movement: score standing still and 16 steps of 6 m. Line of fire to the target is worth a lot
        when a weapon is ready (hiding when not), being seen by other mechs costs, keep the preferred range,
        stay off walls and inside the zone, strafe, and drift toward a nearby cube."""
        g = self.grid
        x, y, z = me["pos"]
        tx, ty, tz = target["pos"]
        th = tz + 4.5 * (1 + 0.05 * (target.get("cubes") or 0))
        mh = me.get("stats", {}).get("muzzle_height", 7)
        want_los = 1.0 if ready else -1.0
        others = [e for e in enemies if e is not target and e["_d"] < 170][:4]
        cc, cr = self.zone_at(zone, t + 1.0)
        if t >= getattr(self, "gm_flip", -1):
            self.gm_flip = t + 0.3 + 0.7 * random.random()
            if random.random() < 0.6:
                self.gm_side = -getattr(self, "gm_side", 1)
        side = getattr(self, "gm_side", 1)
        to_t = math.atan2(ty - y, tx - x)
        perp = to_t + side * math.pi / 2
        best, bs = None, -1e18
        for k in range(17):
            if k == 16:
                px, py, step, yaw = x, y, 0.0, None
            else:
                yaw = k * math.pi / 8
                px, py, step = x + math.cos(yaw) * 6.0, y + math.sin(yaw) * 6.0, 6.0
                if g.blocked[g.cell(px, py)] == 1 or g.blocked[g.cell(x + math.cos(yaw) * 3.0, y + math.sin(yaw) * 3.0)] == 1:
                    continue
            nd = math.hypot(tx - px, ty - py)
            sc = -1.1 * abs(nd - pref) - 0.4 * step
            los = g.fire_clear(px, py, g.height(px, py) + mh, tx, ty, th)
            if los:
                sc += 45 * want_los
            for e in others:
                ex, ey, ez = e["pos"]
                if g.fire_clear(ex, ey, ez + 8, px, py, g.height(px, py) + 5.4, skip_end=1.0):
                    sc -= 30
            dz = math.hypot(px - cc[0], py - cc[1])
            if dz > cr - 5:
                sc -= 10 + (dz - cr + 5) * 2
            if g.soft[g.cell(px, py)] == 2:
                sc -= 2.5 * 2  # hugging a wall: little room to move
            if yaw is not None and want_los > 0:
                sc += 9 * math.cos(yaw - perp)
            if near_cube is not None and near_cube[0] < 40 and yaw is not None:
                cp = near_cube[1]
                sc += 1.5 * (near_cube[0] - math.hypot(cp[0] - px, cp[1] - py))
            if yaw is None:
                sc -= 8
                if want_los < 0 and not los:
                    sc += 14
            if sc > bs:
                bs, best = sc, yaw
        return best

    def tactical(self, me, target, enemies, want, ready, hp_frac):
        """Score nearby cells (Killzone-style weighted sum) and return the best destination.

        Good cells see the target (when our weapons are ready), are hidden from every other
        hunter, sit near our preferred range, and are cheap to reach."""
        g = self.grid
        x, y = me["pos"][0], me["pos"][1]
        ci, cj = int((x - g.minx) / CELL), int((y - g.miny) / CELL)
        others = [e for e in enemies if e is not target and e["_d"] < 220][:4]
        tx, ty = target["pos"][0], target["pos"][1]
        w_fire = 3.0 if ready else -1.0          # want a shot when ready, want to be hidden when not
        w_other = 2.5 + (2.0 if hp_frac < 0.5 else 0.0)
        best, bs = None, -1e18
        R = 4
        for dj in range(-R, R + 1):
            for di in range(-R, R + 1):
                if di * di + dj * dj > R * R + 1:
                    continue
                i, j = ci + di, cj + dj
                if i < 1 or j < 1 or i >= g.nx - 1 or j >= g.ny - 1:
                    continue
                k = j * g.nx + i
                if g.blocked[k]:
                    continue
                px, py = (x, y) if (di == 0 and dj == 0) else g.center(i, j)
                dt = math.hypot(px - tx, py - ty)
                sees = dt < 230 and g.los(px, py, tx, ty)
                sc = w_fire * (1.0 if sees else 0.0)
                for e in others:
                    if g.los(px, py, e["pos"][0], e["pos"][1]):
                        sc -= w_other * (1.0 if e["_d"] < 120 else 0.5)
                sc -= abs(dt - want) / 40.0
                sc -= math.hypot(px - x, py - y) / 25.0
                if g.soft[k] == 2:
                    sc += 0.3  # hugging a wall: cover is a step away
                sc += random.random() * 0.25  # do not be predictable
                if sc > bs:
                    bs, best = sc, (px, py)
        return best

    def blocked_line(self, ax, ay, bx, by):
        """True if a building cell lies on the segment between two points (sampled every 2 m)."""
        g = self.grid
        n = int(math.hypot(bx - ax, by - ay) / 3.0)
        for i in range(1, n):
            f = i / n
            if g.blocked[g.cell(ax + (bx - ax) * f, ay + (by - ay) * f)] == 1:
                return True
        return False

    def find_cover(self, me, threats):
        """Nearest walkable cell within ~45 m that every listed threat's line of sight is blocked from."""
        g = self.grid
        x, y = me["pos"][0], me["pos"][1]
        ci, cj = int((x - g.minx) / CELL), int((y - g.miny) / CELL)
        best, bd = None, 1e9
        R = 10
        for dj in range(-R, R + 1):
            for di in range(-R, R + 1):
                i, j = ci + di, cj + dj
                if i < 1 or j < 1 or i >= g.nx - 1 or j >= g.ny - 1:
                    continue
                k = j * g.nx + i
                if g.blocked[k]:
                    continue
                px, py = g.center(i, j)
                d = math.hypot(px - x, py - y)
                if d >= bd or d > 46:
                    continue
                if all(self.blocked_line(e["pos"][0], e["pos"][1], px, py) for e in threats):
                    best, bd = (px, py), d
        return best

    def hear_weapons(self, obs, me, t):
        x, y = me["pos"][:2]
        for sound in sorted(obs.get("sounds", []), key=lambda s: s.get("age", 0), reverse=True):
            kind = sound["kind"]
            if kind not in ("gun", "laser", "laser_charge") or sound["dist"] not in ("near", "mid"):
                continue
            yaw = sound["bearing"]
            # Match both the noisy bearing and the distance band to visible possible sources.
            visible = any(abs(wrap(math.atan2(e["pos"][1] - y, e["pos"][0] - x) - yaw)) < 0.35 and
                          ((e["_d"] < 60) == (sound["dist"] == "near")) and e["_d"] < 160
                          for e in obs["mechs"])
            if visible:
                continue
            when = t - sound.get("age", 0)
            self.heard_weapon = (yaw, when)
            if kind == "laser_charge":
                self.heard_charge = (yaw, when)

    def escape_pocket(self, me, t):
        if not self.grid or getattr(self, "planned_at", None) != t or not self.goal:
            self.escape_watch = None
            return None
        x, y = me["pos"][:2]
        d = math.dist((x, y), self.goal)
        watch = getattr(self, "escape_watch", None)
        if d <= 20:
            self.escape_watch = None
            return None
        if watch is None or self.goal_key != watch[0] or d <= watch[1] - 2.5:
            self.escape_watch = (self.goal_key, d, t)
        elif t - watch[2] >= 3 and me["energy"] > 20:
            # An exhausted A* search plus a local wall distinguishes an enclosed route from a slow turn.
            yaw = math.atan2(self.goal[1] - y, self.goal[0] - x)
            if self.failed_goal == self.goal_key and self.blocked_line(x, y, x + 16*math.cos(yaw), y + 16*math.sin(yaw)):
                self.jet_yaw = yaw
                self.jet_until = t + 2
                self.escape_watch = (self.goal_key, d, t)
        if t < getattr(self, "jet_until", -1) and me["energy"] > 5:
            return self.jet_yaw
        return None

    def dodge(self, obs, me):
        """Sideways escape from bullets about to hit us and from mortar landing spots; (dx, dy) or None."""
        x, y, z = me["pos"]
        sc = 1 + 0.05 * me.get("cubes", 0)
        cz = z + 4.5 * sc
        grav = self.cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22)
        dx = dy = 0.0
        hit = False
        for p in obs.get("projectiles", []):
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            if p.get("kind") == "mortar":
                # when does it come down to our height, and where
                a, b, c = -0.5 * grav, vz, pz - z
                disc = b * b - 4 * a * c
                if disc < 0:
                    continue
                tl = (-b - math.sqrt(disc)) / (2 * a)
                if not 0 <= tl <= 3:
                    continue
                lx, ly = px + vx * tl, py + vy * tl
                if math.hypot(x + me["vel"][0] * tl - lx, y + me["vel"][1] * tl - ly) < 11:
                    l = max(0.5, math.hypot(x - lx, y - ly))
                    dx += (x - lx) / l
                    dy += (y - ly) / l
                    hit = True
                continue
            rx, ry, rz = x - px, y - py, cz - pz
            vv = vx * vx + vy * vy + vz * vz
            if vv < 1:
                continue
            tc = (rx * vx + ry * vy + rz * vz) / vv  # time of closest approach
            if not 0 <= tc <= 0.9:
                continue
            mx, my, mz = px + vx * tc - x, py + vy * tc - y, pz + vz * tc - cz
            if math.hypot(mx, my, mz * 0.5) > 3.6 * sc:
                continue
            hv = math.hypot(vx, vy) or 1.0
            nx, ny = -vy / hv, vx / hv
            if nx * mx + ny * my > 0:  # step to the side the bullet passes furthest from
                nx, ny = -nx, -ny
            dx += nx
            dy += ny
            hit = True
        if not hit or math.hypot(dx, dy) < 0.01:
            return None
        return dx, dy

    def route_to(self, me, pos, deadline):
        key = ("objective", *pos[:2])
        if key != self.goal_key:
            self.goal = pos[:2]
            self.goal_key = key
            self.path = []
            self.search = None
            if self.grid:
                g = self.grid
                k = g.cell(*self.goal)
                if not g.free(k):
                    k = g.nearest_free(k)
                    self.goal = g.center(k % g.nx, k // g.nx)
        self.plan(me, deadline)
        return self.follow(me)

    def seek_laser(self, mem, me, enemies, fighting, zone, t):
        here = me["pos"][:2]
        active = self.laser_chase[0] if self.laser_chase else None
        best = None
        for pid, (kind, pos) in mem.items():
            if kind != "laser" or self.skip_laser_until.get(pid, 0) > t:
                continue
            if math.dist(pos[:2], zone["center"]) > zone["radius"]:
                continue
            if self.grid and (not self.grid.free(self.grid.cell(*pos[:2])) or
                              abs(self.grid.height(*pos[:2]) - pos[2]) > 5):
                continue
            d = math.dist(here, pos[:2])
            if d > (120 if fighting and pid != active else 250):
                continue
            if any(math.dist(e["pos"][:2], pos[:2]) < max(20, d - 30) for e in enemies):
                continue
            if pid == active:
                _, last_d, since = self.laser_chase
                if d <= last_d - 2.5:
                    self.laser_chase = (pid, d, t)
                elif t - since >= 12:
                    self.skip_laser_until[pid] = t + 30
                    continue
                return pos
            if best is None or d < best[0]:
                best = (d, pid, pos)
        if best:
            d, pid, pos = best
            self.laser_chase = (pid, d, t)
            return pos
        self.laser_chase = None
        return None

    def top_up(self, gun, target, me, t):
        if target is None:
            window, delay, fraction = "idle", 1.5, 0.70
        elif self.grid and not self.grid.los(*me["pos"][:2], *target["pos"][:2]):
            window, delay, fraction = "blocked", 0.8, 0.34
        else:
            self.reload_window = None
            return False
        if self.reload_window is None or self.reload_window[0] != window:
            self.reload_window = (window, t)
        return (gun.get("reload", 0) == 0 and t - self.reload_window[1] >= delay and
                gun["ammo"] < fraction * gun["stats"]["mag"])

    def pick_drop(self, obs, me, zone, t, enemies=()):
        """A supply drop we can reach and hold alone (3 cubes, full HP, full energy), or None."""
        x, y = me["pos"][0], me["pos"][1]
        speed = me.get("stats", {}).get("walk_speed", 11.7)
        best, bd = None, 1e9
        station = "drops2" in FLAGS
        hp = me["hp"] / max(1, me["max_hp"])
        if station and hp >= 0.85 and me.get("cubes", 0) >= 10:
            return None
        for dr in obs.get("drops", []) or []:
            if dr.get("holder") is not None and dr.get("holder") != me["id"]:
                continue
            d = math.dist(dr["pos"][:2], (x, y))
            if station:
                if (d > (130 if hp < 0.7 else 85) or dr.get("lands_in", 0) > d / speed + 5 or
                        math.dist(dr["pos"][:2], zone["next_center"]) > zone["next_radius"] - 8 or
                        any(math.dist(e["pos"][:2], dr["pos"][:2]) < 55 for e in enemies)):
                    continue
            elif d > DROP_RADIUS or dr.get("lands_in", 0) > d / speed + 4:
                continue
            nc, nr = zone["next_center"], zone["next_radius"]
            if math.dist(dr["pos"][:2], nc) > nr and zone["shrink_start"] - t < 15:
                continue
            if d < bd:
                bd, best = d, dr
        return best

    def upgrade_pick(self, offer, has_laser=False):
        pref = ["plating", "armor", "nanites", "laser_damage", "laser_charge", "lightning_efficiency", "gun_damage",
                "gun_rate", "lightning_chain", "lightning_range", "gun_mag", "servos", "gyros", "capacitor",
                "reactor", "gun_velocity", "jets", "sensors"]
        pref = UPGRADE_FIRST + pref
        if "laserup" in FLAGS and has_laser:
            pref = ["laser_damage", "laser_charge"] + pref
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
            act["pick"] = self.upgrade_pick(obs["offer"], "laser" in kinds)

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.threat = (ev["bearing"], t, ev.get("by"))
                self.__dict__.setdefault("hurt_by", {})[ev.get("by")] = t
                self.last_hurt = t
            if ev.get("type") == "hit":
                self.last_dealt = t
            if ev.get("type") == "kill" and ev.get("target", 99) < 16:
                self.my_kills = getattr(self, "my_kills", 0) + 1
                if "duel" in FLAGS and getattr(self, "ffa", False):
                    self.kill_loot = getattr(self, "lastseen", {}).get(ev["target"], (None, t))[0]
                    self.kill_loot_until = t + 7
            if ev.get("type") == "kill" and "postkill" in FLAGS and ev.get("target", 99) < 16:
                self.hide_until = t + 5  # a kill wakes the next hunter and our gunfire drew others: reset first
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
        self.now = t
        self.mypos = (x, y)
        self.next_r = obs["zone"]["next_radius"]
        _mv = me.get("vel", [0, 0, 0])
        self.muzzle = (x + _mv[0] * 0.1, y + _mv[1] * 0.1, z + me.get("stats", {}).get("muzzle_height", 7))
        enemies, npcs = self.seen(obs, me)
        lastseen = self.__dict__.setdefault("lastseen", {})
        for e in enemies:
            lastseen[e["id"]] = (e["pos"], t)
        for k in list(lastseen):
            if t - lastseen[k][1] > 15:
                del lastseen[k]
        visible_ids = {m["id"] for m in obs["mechs"]}
        blind_hit = bool(self.threat and t - self.threat[1] < 1.5 and self.threat[2] is not None
                         and self.threat[2] < 100 and self.threat[2] not in visible_ids)
        cc, cr = self.zone_at(zone, t + 3)
        dz = math.hypot(x - cc[0], y - cc[1])
        urgent = dz > cr - 4 or not me["in_zone"]
        nc, nr = zone["next_center"], zone["next_radius"]
        has_lightning = "lightning" in kinds

        # ----- target -----
        candidates = enemies
        self.hunt_on = ("huntgate" in FLAGS and "aim2" in FLAGS and "agg" in FLAGS and
                        t >= 20 and not getattr(self, "ffa", False))
        if self.hunt_on:
            if nr <= 70 or obs.get("alive", 16) <= 3:
                candidates = [e for e in enemies if e["_d"] <= 240]
            else:
                candidates = [e for e in enemies if self.hunt_target(e, me, t)]
        duel = "duel" in FLAGS and getattr(self, "ffa", False)
        camp = "camp" in FLAGS and getattr(self, "ffa", False)
        if duel:
            candidates = self.duel_candidates(enemies, me, hp_frac, t)
        elif camp:
            candidates = [e for e in candidates if e["_d"] < 55 or
                          t - getattr(self, "hurt_by", {}).get(e["id"], -99) < 4 or
                          (e["_d"] < 100 and (e.get("hp") or 1400) < 0.3*(e.get("max_hp") or 1400))]
        target = self.choose_target(candidates)
        if duel and target is not None:
            self.duel_id = target["id"]
        attacker = None
        if self.threat and t - self.threat[1] < 3:
            for m in obs["mechs"]:
                if m["id"] == self.threat[2]:
                    attacker = m
        npc_target = None
        hunter_near = any(e["_d"] < 200 for e in enemies)
        # free-for-all detection: in practice only we kill house bots; mechs dying without us means FFA
        self.alive = obs.get("alive", 16)
        if "ffa" in FLAGS and not getattr(self, "ffa", False) and (16 - self.alive) - getattr(self, "my_kills", 0) >= 3:
            self.ffa = True
            log("ffa detected", t)
        ffa_on = getattr(self, "ffa", False) and self.alive > 4
        if ffa_on and any(e["_d"] < 150 for e in enemies):
            hunter_near = True
        if target is None and not (("ammo" in FLAGS or ffa_on) and hunter_near):
            for n in npcs:
                if attacker is n or (n["_d"] < DRONE_RADIUS and hp_frac > 0.55):
                    npc_target = n
                    break
            if npc_target is None and hp_frac > 0.3:
                best = None
                for c in obs.get("crates", []):
                    if abs(c["pos"][2] - z) > 3:
                        continue  # rooftop: cubes would land out of reach
                    d = math.dist(c["pos"][:2], (x, y))
                    if d < CRATE_RADIUS and (best is None or d < best[0]):
                        best = (d, c)
                if best is not None:
                    c = best[1]
                    npc_target = {"kind": "crate", "id": c["id"], "pos": c["pos"], "vel": [0, 0, 0], "_d": best[0]}

        # recover: when low and nobody is on top of us, stop shooting and get out of sight to regenerate
        near = min((e["_d"] for e in enemies), default=1e9)
        hurt_recently = t - getattr(self, "last_hurt", -99) < 2.5
        # a hunter with less hp than us is worth finishing; otherwise break off when low
        weak_target = target is not None and (target.get("hp") or 1400) < me["hp"] * 0.8
        # duel stats: hurt and without a laser we win 17% of fights, at full hp 56-72%. Heal first.
        heal_below = HEAL_BELOW_LASER if "laser" in kinds else HEAL_BELOW
        heal_to = HEAL_TO_EARLY if "early" in FLAGS else HEAL_TO
        if "agg" in FLAGS:
            heal_below, heal_to = AGG_HEAL_BELOW, AGG_HEAL_TO  # fight on while healthy; come back once three quarters healed
        if "burst" in FLAGS and not getattr(self, "ffa", False):
            heal_below, heal_to = 0.0, 0.0
        if hp_frac < heal_below and near > 60 and not weak_target:
            self.recovering = True
        if t < getattr(self, "hide_until", -1) and hp_frac < heal_to and near > 45:
            self.recovering = True
        if getattr(self, "recovering", False) and (hp_frac > (heal_to if "agg" in FLAGS else RECOVER_EXIT) or near < 40
                                                    or (weak_target and near < 80)):
            self.recovering = False
        recovering = getattr(self, "recovering", False) and not urgent
        king = "king" in FLAGS and nr <= 25.5
        drop = self.pick_drop(obs, me, zone, t, enemies)
        station = "drops2" in FLAGS and drop is not None and not urgent
        finish_close = ("finisher" in FLAGS and target is not None and hp_frac > 0.42 and
                        (target.get("hp") or 1400) < 0.38 * (target.get("max_hp") or 1400) and
                        sum(e["_d"] < 160 for e in enemies) <= 2)
        if king or station or finish_close:
            recovering = False
        resting = duel and getattr(self, "duel_rest", False)
        if duel:
            recovering = resting and not urgent
        if recovering or (camp and enemies):
            npc_target = None

        # ----- loot -----
        detour = None
        quiet = target is None or target["_d"] > 110
        cands = []
        for c in obs.get("cubes", []):
            d = math.dist(c["pos"][:2], (x, y))
            if abs(c["pos"][2] - z) < 5 and d < (70 if quiet else 25):
                cands.append((d, c["pos"]))
        # remember weapon pickups we have seen; forget ones that are gone when we look at their spot
        mem = self.__dict__.setdefault("pickmem", {})
        seen_ids = set()
        for p in obs.get("pickups", []):
            mem[p["id"]] = (p["kind"], p["pos"])
            seen_ids.add(p["id"])
        for pid in list(mem):
            forget_at = 30
            if "laserseek" in FLAGS and mem[pid][0] == "laser":
                forget_at = self.reach  # turning away can remove a real pickup from view
            if pid not in seen_ids and math.dist(mem[pid][1][:2], (x, y)) < forget_at:
                del mem[pid]

        def want_kind(k):
            if k == "laser":
                return kinds[1] != "laser"
            if k == "lightning":
                return kinds[1] in (None, "mortar")
            return False

        for pid, (k, pos) in mem.items():
            if "laserseek" in FLAGS and k == "laser":
                continue  # the pursuit timer also applies to quiet detours
            if abs(pos[2] - z) > 5 or not want_kind(k):
                continue
            d = math.dist(pos[:2], (x, y))
            bonus = 80 if k == "laser" else 40
            if d < ((260 if k == "laser" else 160) if quiet else 30):
                cands.append((d - bonus, pos))
        if cands and not urgent:
            cands.sort(key=lambda c: c[0])
            detour = cands[0][1]
        # cubes seen recently (each one: +4% max HP and damage up to 10)
        cmem = self.__dict__.setdefault("cubemem", {})
        for c in obs.get("cubes", []):
            if abs(c["pos"][2] - z) < 5:
                cmem[c["id"]] = (c["pos"], t)
        for cid in list(cmem):
            cp, ct = cmem[cid]
            if t - ct > 40 or (t - ct > 0.2 and math.dist(cp[:2], (x, y)) < 15):
                del cmem[cid]
        near_cube = min(((math.dist(cp[:2], (x, y)), cp) for cp, _ in cmem.values()), default=None,
                        key=lambda c: c[0])
        drop = self.pick_drop(obs, me, zone, t)
        # remembered crates and drones (cube sources) to farm when calm
        farm_goal = None
        if "econ" in FLAGS:
            fm = self.__dict__.setdefault("farmmem", {})
            seen_now = set()
            for c in obs.get("crates", []):
                if abs(c["pos"][2] - z) < 3:
                    fm[c["id"]] = (c["pos"], t)
                    seen_now.add(c["id"])
            for n in npcs:
                fm[n["id"]] = (n["pos"], t)
                seen_now.add(n["id"])
            for k in list(fm):
                p_, tt = fm[k]
                # gone: we are close enough to see it and it is not listed, or the memory is old
                if k not in seen_now and (math.dist(p_[:2], (x, y)) < 40 or t - tt > (60 if k >= 1000 else 20)):
                    del fm[k]
            last_mech = getattr(self, "last_mech_t", -99)
            if enemies:
                self.last_mech_t = t
                last_mech = t
            calm = t - last_mech > 6 and hp_frac >= 0.85 and me["in_zone"] and me.get("cubes", 0) < 14
            if "farm5" in FLAGS:
                # farm whenever nobody is fighting us and we are above half HP
                calm = (target is None and t - getattr(self, "last_hurt", -99) > 3 and hp_frac > 0.5 and
                        me["in_zone"] and me.get("cubes", 0) < 14)
            if calm and fm:
                best_f = min(fm.values(), key=lambda v: math.dist(v[0][:2], (x, y)))
                fd = math.dist(best_f[0][:2], (x, y))
                if fd < 250 and math.dist(best_f[0][:2], zone["next_center"]) < zone["next_radius"] + 40:
                    farm_goal = best_f[0]
        laser = None
        if "laserseek" in FLAGS and "laser" not in kinds and not urgent and not king:
            laser = self.seek_laser(mem, me, enemies, target is not None, zone, t)
        if "reload2" in FLAGS and g and self.top_up(g, target or npc_target, me, t):
            act["reload"] = True
        for p in obs.get("pickups", []):
            if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4 and want_kind(p["kind"]):
                if kinds[1] is None:
                    act["take"] = p["id"]
                elif not (weapons[1] and weapons[1].get("charge", 0) > 0):
                    act["take"] = p["id"]
                    act["drop"] = 1
                break

        # ----- movement -----
        move_yaw = None
        boost = False
        slow = False
        cover = None
        quiet_heal = "heal" in FLAGS and t - getattr(self, "last_hurt", -99) > 2.0
        if recovering and drop is not None and near > 80:
            dd = math.dist(drop["pos"][:2], (x, y))
            move_yaw = None if dd < drop.get("radius", 8) - 3 else math.atan2(drop["pos"][1] - y, drop["pos"][0] - x)
            boost = me["energy"] > 30 and dd > 40
        elif recovering and self.grid is not None and enemies:
            if t - getattr(self, "cover_t", -9) > 0.8 or getattr(self, "cover_pt", None) is None:
                self.cover_t = t
                self.cover_pt = self.find_cover(me, enemies[:3])
            cover = self.cover_pt
        if resting and not urgent and not station:
            if cover is not None:
                if math.dist((x, y), cover) > 3:
                    move_yaw = math.atan2(cover[1]-y, cover[0]-x)
            elif enemies:
                move_yaw = math.atan2(y-enemies[0]["pos"][1], x-enemies[0]["pos"][0])
        elif duel and not urgent and not resting and t < getattr(self, "kill_loot_until", -1) and near > 60 and self.kill_loot:
            cp = self.kill_loot
            nearby = [pos for pos, _ in cmem.values() if math.dist(pos[:2], cp[:2]) < 25]
            dest = min(nearby, key=lambda pos: math.dist(pos[:2], (x, y))) if nearby else cp
            move_yaw = self.route_to(me, dest, deadline)
            if not nearby and math.dist(dest[:2], (x, y)) < 4:
                self.kill_loot_until = t
        elif duel and not urgent and self.alive <= 4 and not resting:
            if t >= getattr(self, "patrol_at", 0) or math.dist(self.patrol, (x, y)) < 4:
                self.patrol = self.patrol_point(me, zone, enemies)
                self.patrol_at = t + 2
            move_yaw = self.route_to(me, self.patrol, deadline)
        elif camp and not urgent and target is None and (laser is None or near < 110):
            if t >= getattr(self, "camp_at", 0):
                hazards = [(e["pos"][0], e["pos"][1], 75) for e in enemies[:3]]
                self.camp_goal = self.pick_goal(me, zone, hazards)
                self.camp_at = t + 4
            move_yaw = self.route_to(me, self.camp_goal, deadline)
        elif king:
            move_yaw = self.route_to(me, nc, deadline)
            boost = math.dist((x, y), self.goal) > 30
        elif laser is not None and not resting and not (duel and target is not None):
            move_yaw = self.route_to(me, laser, deadline)
            boost = (bool(enemies) or hurt_recently) and math.dist((x, y), laser[:2]) > 20 and me["energy"] > 30
        elif station:
            if math.dist((x, y), drop["pos"][:2]) > drop.get("radius", 8) - 3:
                move_yaw = self.route_to(me, drop["pos"], deadline)
        elif recovering and drop is not None and near > 80:
            pass
        elif cover is not None and math.dist(cover, (x, y)) > 3:
            move_yaw = math.atan2(cover[1] - y, cover[0] - x)
            boost = me["energy"] > 15 and not quiet_heal
            slow = quiet_heal
        elif cover is not None:
            move_yaw = None  # in cover: stand still, silent, regenerate
        elif recovering and "heal" in FLAGS:
            # nobody in sight: regenerate silently (45 HP/s after 5 s without dealing or taking damage);
            # creep below step speed into cover from where hunters were last seen, else stand still
            ghosts = [{"pos": p} for p, _ in sorted(lastseen.values(), key=lambda v: -v[1])[:3]]
            if ghosts and self.grid is not None:
                if t - getattr(self, "hcover_t", -9) > 1.0:
                    self.hcover_t = t
                    exposed = any(self.grid.los(x, y, gh["pos"][0], gh["pos"][1]) for gh in ghosts)
                    self.hcover_pt = self.find_cover(me, ghosts) if exposed else None
                hp_ = getattr(self, "hcover_pt", None)
                if hp_ is not None and math.dist(hp_, (x, y)) > 3:
                    move_yaw = math.atan2(hp_[1] - y, hp_[0] - x)
                    slow = True
        elif recovering:
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
            has_laser = "laser" in kinds
            want = LIGHTNING_RANGE if has_lightning else ((155.0 if "lrange" in FLAGS else LASER_RANGE) if has_laser else GUN_RANGE)
            nthreat = len([e for e in enemies if e["_d"] < 160])
            efrac = ehp / max(1.0, target.get("max_hp") or 1400)
            finishing = efrac < 0.2 and hp_frac > 0.2 and nthreat <= 2
            losing = not finishing and ((hp_frac < RETREAT_HP and efrac > hp_frac + 0.1) or (nthreat >= 2 and hp_frac < RETREAT_TWO_HP)
                                        or (nthreat >= 3 and hp_frac < RETREAT_THREE_HP))
            if "iso" in FLAGS and not finishing and nthreat >= 2:
                losing = True  # only take one-on-ones
            if "early" in FLAGS and not finishing and hp_frac < EARLY_RETREAT_HP and efrac > hp_frac:
                losing = True
            if ffa_on and not finishing and nthreat >= 2 and hp_frac < 0.8:
                losing = True
            if "agg" in FLAGS:
                # break off only when clearly losing: six hunters on us, two of them below half HP, low HP,
                # or out-ranged by a laser
                mine = me["hp"]
                finishing = ehp < mine * 0.5 or (efrac < 0.55 and mine > ehp * 1.3)
                outgunned = "laser" in (target.get("weapons") or []) and not has_laser and d > 55
                losing = not finishing and (hp_frac < AGG_RETREAT_HP or nthreat >= AGG_THREAT_COUNT or (nthreat == 2 and hp_frac < AGG_TWO_HP)
                                            or (outgunned and hp_frac < AGG_OUTGUNNED_HP))
                if ffa_on and not finishing and nthreat >= 2 and hp_frac < 0.8:
                    losing = True
            if "burst" in FLAGS:
                # retreat only in short bursts after the last losing tick; no long heals
                near_n = len([e for e in enemies if e["_d"] < 160])
                lose_now = ((near_n >= 2 and hp_frac < 0.55) or (near_n >= 3 and hp_frac < 0.8) or
                            (hp_frac < 0.35 and efrac > hp_frac + 0.1))
                if efrac < 0.25 and hp_frac > 0.2 and near_n <= 2:
                    lose_now = False
                if ffa_on and nthreat >= 2 and hp_frac < 0.8:
                    lose_now = True
                if lose_now:
                    self.burst_until = t + 1.5
                losing = t < getattr(self, "burst_until", -1)
            if duel:
                losing = False  # duel_candidates handles the disengage and repair latch
                want = 32 if has_lightning else (95 if has_laser else 78)
                if self.alive <= 4 and has_laser:
                    want = 110
                if efrac < 0.3:
                    want = min(want, 45)
            if finish_close:
                losing = False
                want = 20
            outnumbered = False
            if losing and "burst" not in FLAGS:
                self.hide_until = t + 6
            if t >= getattr(self, "strafe_next", -1):
                self.strafe_next = t + STRAFE_MIN + STRAFE_JITTER * random.random()
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
                if self.grid is not None:
                    if t - getattr(self, "cover_t", -9) > 0.5 or getattr(self, "cover_pt", None) is None:
                        self.cover_t = t
                        self.cover_pt = self.find_cover(me, [e for e in enemies if e["_d"] < 160][:3] or [target])
                    if self.cover_pt is not None and math.dist(self.cover_pt, (x, y)) > 2:
                        away = math.atan2(self.cover_pt[1] - y, self.cover_pt[0] - x)
                move_yaw = away
                boost = me["energy"] > 10
            else:
                g0 = weapons[0]
                w1 = weapons[1]
                ready = bool(g0 and g0.get("reload", 0) == 0 and g0.get("ammo", 1) > 0) or                     bool(w1 and w1["kind"] == "laser" and w1["cooldown"] <= 0.3)
                if hp_frac < 0.35 and ehp > me["hp"]:
                    ready = False
                if self.grid is not None and (t - getattr(self, "tac_t", -9) > 0.3 or getattr(self, "tac_pt", None) is None):
                    self.tac_t = t
                    self.tac_pt = self.tactical(me, target, enemies, want, ready, hp_frac)
                dest = getattr(self, "tac_pt", None) if self.grid is not None else None
                if dest is not None and math.hypot(dest[0] - x, dest[1] - y) > 2.5:
                    dyaw = math.atan2(dest[1] - y, dest[0] - x)
                    # keep a sideways jitter so we are hard to lead
                    mx = math.cos(dyaw) + 0.5 * math.cos(perp)
                    my = math.sin(dyaw) + 0.5 * math.sin(perp)
                else:
                    radial = 0.0
                    if d > want + 15:
                        radial = 1.0
                    elif d < want - 12:
                        radial = -0.8
                    mx = math.cos(perp) + radial * math.cos(to_t)
                    my = math.sin(perp) + radial * math.sin(to_t)
                move_yaw = math.atan2(my, mx)
                if "gmove" in FLAGS and self.grid is not None:
                    lw_ = w1 if (w1 and w1["kind"] == "laser") else None
                    gun_ok = bool(g0 and g0.get("ammo", 1) > 0 and g0.get("reload", 0) <= 0.25 and
                                  d <= g0["stats"].get("range", 230) - 10)
                    # holding a laser or lightning always counts as ready while hunting
                    rdy = gun_ok or bool(lw_) or has_lightning
                    pref = 28.0 if (has_lightning and me["energy"] > 12) else (120.0 if has_laser else 85.0)
                    if not has_lightning and "lightning" in (target.get("weapons") or []):
                        pref = max(pref, 64.0)
                    if efrac < 0.38 and hp_frac > 0.42 and nthreat <= 2:
                        pref = 20.0  # close in to finish
                    gy_ = self.gmove(me, target, enemies, zone, t, rdy, pref, near_cube)
                    move_yaw = gy_
                if "steer" in FLAGS and self.grid is not None:
                    # weapon readiness decides whether we want to be seen (peek) or hidden (duck)
                    lw = w1 if (w1 and w1["kind"] == "laser") else None
                    gun_ready = bool(g0 and g0.get("ammo", 1) > 0 and g0.get("reload", 0) <= 0.25)
                    laser_ready = bool(lw and (lw.get("charge", 0) or 0) >= lw["stats"].get("charge", 0.75) - 0.3 and
                                       d < lw["stats"].get("range", 280))
                    light_ready = bool(w1 and w1["kind"] == "lightning" and d < w1["stats"].get("range", 45))
                    wl = 1.0 if (gun_ready or laser_ready or light_ready) else -1.0
                    taim_ = (target.get("aim") or [9.0])[0]
                    if target.get("charging") and abs(wrap(taim_ - math.atan2(y - ty, x - tx))) < 0.25 and not laser_ready:
                        wl = -1.6
                    if has_lightning:
                        rmin, rmax = 12.0, 36.0
                    elif has_laser:
                        rmin, rmax = 125.0, 185.0
                    else:
                        rmin, rmax = 45.0, 85.0
                    gy = to_t if d > rmax + 4 else (wrap(to_t + math.pi) if d < rmin - 4 else None)
                    move_yaw = self.steer(me, target, enemies, zone, t, wl, gy, 1.0, rmin, rmax,
                                          0.7 if target.get("_lof", True) else 0.2)
                # boosting is loud (90 m) and drains energy: only to close in with lightning
                if has_lightning and want + 15 < d < 90 and me["energy"] > CHARGE_ENERGY:
                    boost = True
                taim = (target.get("aim") or [9.0])[0]
                if target.get("charging") and abs(wrap(taim - math.atan2(y - ty, x - tx))) < 0.25:
                    move_yaw = perp  # its laser is lined up on us: break the line
                    boost = me["energy"] > (45 if has_lightning else 10)
                if finish_close and d > 23 and not target.get("charging"):
                    move_yaw = self.route_to(me, target["pos"], deadline) if not target.get("_lof", True) else to_t
                elif near_cube is not None and near_cube[0] < (70 if hp_frac > 0.5 else 40):
                    cp = near_cube[1]
                    move_yaw = math.atan2(cp[1] - y, cp[0] - x)
        elif drop is not None and not urgent and not recovering:
            dd = math.dist(drop["pos"][:2], (x, y))
            if dd > drop.get("radius", 8) - 3:
                move_yaw = math.atan2(drop["pos"][1] - y, drop["pos"][0] - x)
                boost = me["energy"] > 50 and dd > 60
        elif near_cube is not None and near_cube[0] < CUBE_RADIUS and not urgent and not recovering:
            cp = near_cube[1]
            move_yaw = math.atan2(cp[1] - y, cp[0] - x)
        elif npc_target is None and farm_goal is not None and not urgent and not recovering:
            move_yaw = math.atan2(farm_goal[1] - y, farm_goal[0] - x)
            if self.grid is not None:
                key = ("farm", round(farm_goal[0]), round(farm_goal[1]))
                if self.goal_key != key:
                    self.goal = [farm_goal[0], farm_goal[1]]
                    self.goal_key = key
                    self.path = []
                    self.search = None
                self.plan(me, deadline)
                fy = self.follow(me) if self.path else None
                if fy is not None:
                    move_yaw = fy
        elif npc_target is not None and not urgent:
            n = npc_target
            to_n = math.atan2(n["pos"][1] - y, n["pos"][0] - x)
            if n["kind"] == "crate":
                move_yaw = to_n if n["_d"] > 12 else None  # walk up to it: its cube drops right there
            elif n["_d"] > 45:
                move_yaw = to_n
            else:
                move_yaw = to_n + math.pi / 2 * (1 if int(t / 2) % 2 else -1)
        elif detour is not None:
            move_yaw = math.atan2(detour[1] - y, detour[0] - x)
        elif "opening" in FLAGS and t < 20 and enemies and self.grid and not urgent:
            if t >= getattr(self, "opening_cover_at", 0):
                self.opening_cover = self.find_cover(me, enemies[:2])
                self.opening_cover_at = t + 0.6
            if self.opening_cover and math.dist((x, y), self.opening_cover) > 3:
                move_yaw = math.atan2(self.opening_cover[1] - y, self.opening_cover[0] - x)
        else:
            key = (round(nc[0]), round(nc[1]), round(nr))
            if self.goal is None or self.goal_key != key:
                self.goal = self.pick_goal(me, zone, [(n["pos"][0], n["pos"][1], 40.0) for n in npcs])
                self.goal_key = key
                self.path = []
                self.search = None
            self.plan(me, deadline)
            move_yaw = self.follow(me)
        if urgent and not king:
            # outside (or about to be outside) the zone: follow the A* path in, not a straight line into a wall
            to_c = math.atan2(cc[1] - y, cc[0] - x)
            fy = None
            if self.grid is not None:
                key = (round(nc[0]), round(nc[1]), round(nr))
                if self.goal is None or self.goal_key != key:
                    self.goal = self.pick_goal(me, zone, [])
                    self.goal_key = key
                    self.path = []
                    self.search = None
                self.plan(me, deadline)
                fy = self.follow(me) if self.path else None
            pyaw = fy if fy is not None else to_c
            if move_yaw is None or abs(wrap(move_yaw - pyaw)) > 1.2:
                move_yaw = pyaw
            boost = True

        if "hearing" in FLAGS:
            self.hear_weapons(obs, me, t)
            charge_yaw, charge_t = getattr(self, "heard_charge", (0, -99))
            if t - charge_t < 0.65 and not urgent:
                side = 1 if move_yaw is None or math.sin(move_yaw - charge_yaw) >= 0 else -1
                move_yaw = charge_yaw + side * math.pi / 2
                slow = False
        dv = self.dodge(obs, me)
        if dv is not None:
            bx, by = (math.cos(move_yaw) * 0.3, math.sin(move_yaw) * 0.3) if move_yaw is not None else (0.0, 0.0)
            l = math.hypot(*dv)
            move_yaw = math.atan2(by + dv[1] / l, bx + dv[0] / l)
            if me["energy"] > (55 if has_lightning and target is not None else 25):
                boost = True
        # stuck handling
        if self.last_pos is not None and move_yaw is not None:
            moved = math.dist(self.last_pos[:2], (x, y))
            self.stuck = self.stuck + 1 if moved < 0.3 else 0
        self.last_pos = (x, y, z)
        jump = False
        jet = self.escape_pocket(me, t) if "jetescape" in FLAGS else None
        if jet is not None:
            move_yaw, jump, boost, slow = jet, me.get("grounded", False), True, False
        elif self.unstick > 0:
            self.unstick -= 1
            move_yaw = self.unstick_dir
            jump = me.get("grounded", False) and me["energy"] > 10  # an empty tank only wastes the jump
            boost = me["energy"] > 10
        elif self.stuck >= 5 and move_yaw is not None:
            self.unstick = 6
            self.unstick_dir = wrap(move_yaw + random.choice((-1.3, 1.3)))
            self.stuck = 0
            self.path = []
            self.search = None
            self.strafe_s = -getattr(self, "strafe_s", 1)
        if me["energy"] < BOOST_FLOOR and not urgent:
            boost = False
        if slow and not urgent and dv is None:
            sp = 4.4 / max(1.0, me.get("stats", {}).get("walk_speed", 11.7))  # below 5 m/s: no step sound
            boost = False
        else:
            sp = 1.0
        act["move"] = [math.cos(move_yaw) * sp, math.sin(move_yaw) * sp] if move_yaw is not None else [0.0, 0.0]
        act["boost"] = bool(boost and move_yaw is not None)
        act["jump"] = jump

        # ----- aim and fire -----
        fire = [False, False]
        aim = None
        shoot = target if target is not None else npc_target
        if resting or (recovering and target is None):
            shoot = None
        if "heal" in FLAGS and recovering and t - getattr(self, "last_hurt", -99) > 1.5 and                 (target is None or target["_d"] > 45):
            shoot = None  # any hit we deal restarts the 5 s regen timer  # retreating changes where we go, never whether we shoot back at a mech
        if blind_hit and (shoot is None or shoot.get("_d", 0) > 35):
            shoot = None  # turn to whoever is hitting us from out of sight
        if shoot is not None:
            aim, dh = self.lead(me, shoot, self.gun_speed)
            err_y = abs(wrap(me["aim"][0] - aim[0]))
            err_p = abs(me["aim"][1] - aim[1])
            tol = math.atan2(3.2, max(dh, 1.0)) + 0.03
            ptol = tol + 0.05
            clear = True
            if "aim2" in FLAGS:
                # error left after half a decision of torso slew; tolerance sized to the body
                st_ = me.get("stats", {})
                err_y = max(0.0, err_y - st_.get("turn_rate", 4) * 0.05)
                err_p = max(0.0, err_p - st_.get("pitch_rate", 3) * 0.05)
                sc_ = (1 + 0.05 * (shoot.get("cubes") or 0)) if shoot.get("kind") == "mech" else 0.6
                tol = max(0.018, math.atan2(2.0 * sc_, max(dh, 1.0)))
                ptol = max(0.02, math.atan2(3.0 * sc_, max(dh, 1.0)))
                if self.grid is not None and shoot.get("kind") == "mech":
                    mx_, my_, mz_ = self.muzzle
                    clear = self.grid.fire_clear(mx_, my_, mz_, mx_ + math.cos(aim[0]) * dh,
                                                 my_ + math.sin(aim[0]) * dh, mz_ + math.tan(aim[1]) * dh,
                                                 skip_end=3.0)
            g0 = weapons[0]
            if g0 and g0["kind"] == "gun":
                if g0.get("ammo", 1) == 0 and g0.get("reload", 0) == 0:
                    act["reload"] = True
                elif err_y < tol and err_p < ptol and clear and dh < g0["stats"].get("range", 230) * 0.95:
                    fire[0] = True
            w1 = weapons[1]
            if w1:
                if w1["kind"] == "laser" and shoot.get("kind") == "mech":
                    # hitscan, but it fires on the next tick: lead by one decision, relative to our own motion
                    sv = shoot.get("vel", [0, 0, 0])
                    mv = me.get("vel", [0, 0, 0])
                    lx = shoot["pos"][0] + (sv[0] - mv[0]) * 0.1
                    ly = shoot["pos"][1] + (sv[1] - mv[1]) * 0.1
                    direct = [math.atan2(ly - y, lx - x),
                              math.atan2(shoot["pos"][2] + (shoot.get("_aimh", 4.5) if "aim2" in FLAGS else 4.5) - (z + me.get("stats", {}).get("muzzle_height", 7)), max(dh, 1))]
                    ch = w1.get("charge", 0)
                    full = w1["stats"].get("charge", 0.75)
                    if w1["cooldown"] <= 0 and dh < w1["stats"].get("range", 280) * 0.95:
                        lined = abs(wrap(me["aim"][0] - direct[0])) < math.atan2(LASER_WIDTH, max(dh, 1)) and abs(me["aim"][1] - direct[1]) < LASER_PITCH
                        ready_ = ch >= full * 0.97
                        if "aim2" in FLAGS:
                            st_ = me.get("stats", {})
                            ey_ = max(0.0, abs(wrap(me["aim"][0] - direct[0])) - st_.get("turn_rate", 4) * 0.05)
                            ep_ = max(0.0, abs(me["aim"][1] - direct[1]) - st_.get("pitch_rate", 3) * 0.05)
                            sc_ = 1 + 0.05 * (shoot.get("cubes") or 0)
                            lined = (ey_ <= max(0.018, math.atan2(2.0 * sc_, max(dh, 1))) and
                                     ep_ <= max(0.02, math.atan2(3.0 * sc_, max(dh, 1))) and shoot.get("_lof", True))
                            ready_ = ch + 0.01 >= full  # the observed charge is rounded to 0.01
                            if not ready_ and ch >= 0.15:
                                # release early when the partial beam already kills (assume worst-case armor)
                                dmg = (w1["stats"].get("damage", 315) * (1 + 0.04 * min(10, me.get("cubes", 0))) *
                                       (0.35 + 0.65 * ch / full) * 0.76)
                                if "backstab" in FLAGS and self.showing_back(shoot):
                                    dmg *= 1.5
                                ready_ = dmg >= (shoot.get("hp") or 1400) + 15
                        fire[1] = not (ready_ and lined)
                    if ch > full * LASER_AIM_FRACTION:
                        aim = direct
                elif w1["kind"] == "lightning":
                    if dh < w1["stats"].get("range", 45) * 0.97 and err_y < 0.45 and me["energy"] > 6:
                        fire[1] = True
        elif self.threat and t - self.threat[1] < 4 and not (recovering and not blind_hit):
            aim = [self.threat[0], 0.0]
        if "hearing" in FLAGS and aim is None and target is None:
            yaw, when = getattr(self, "heard_weapon", (0, -99))
            if t - when < 0.8:
                aim = [yaw, 0.0]
        if aim is None and recovering and enemies:
            e = enemies[0]
            aim = [math.atan2(e["pos"][1] - y, e["pos"][0] - x), 0.0]  # keep our front toward them
        if aim is None:
            # sweep the 120 degree view across our path to spot loot and hunters early
            base = move_yaw if move_yaw is not None else me["aim"][0] + 0.6
            aim = [wrap(base + 0.9 * math.sin(t * 1.6)), 0.0]
        if "ammo" in FLAGS and shoot is None:
            g0 = weapons[0]
            if g0 and g0["kind"] == "gun" and g0.get("reload", 0) == 0 and g0.get("ammo", 12) < g0["stats"].get("mag", 12) * 0.5:
                act["reload"] = True  # top up between fights
        act["aim"] = aim
        act["fire"] = fire
        return act


if __name__ == "__main__":
    mf.run(Bot)
