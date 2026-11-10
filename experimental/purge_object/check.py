"""Collision check for a spliced purge-object G-code (machine geometry, deliberately conservative).

Objects: "main" (extrusions outside purge visits, after the first CHANGE_LAYER), "purge"
(extrusions between PURGE_VISIT_START/END) and "startup" (purge line etc. before layer 1).
Each object is its XY bounding box with a height that grows as it is printed.

For every move, with the nozzle tip at height z:
  head  an object taller than z must stay more than HEAD_R mm (XY) from the move
  rail  an object taller than z + ROD_SAFE must stay more than RAIL_HALF mm (in Y) from the
        nozzle: the X gantry spans the whole bed width at the toolhead's Y
Also: every purge layer printed exactly once, main layers in order, Z within the lid limit.

Clearances come from the machine profile named in OUT.gcode.plan.json (or --machine).
On machines with hidden_change_moves the firmware's cut/flush moves are not in the file and
cannot be checked here; splice.py lifts those blocks above both objects instead.

Usage: python check.py OUT.gcode [--machine p1s] [--timeline out.json]
Exit 0 = pass. Prints the closest calls either way.
"""
import argparse, json, math, os, sys
import gcmodel as gc
import machine

ap = argparse.ArgumentParser()
ap.add_argument("gcode")
ap.add_argument("--machine")
ap.add_argument("--timeline")
a = ap.parse_args()
plan = a.gcode + ".plan.json"
mid = a.machine or (json.load(open(plan)).get("machine") if os.path.exists(plan) else None)
if not mid:
    sys.exit("[X] no machine: pass --machine or keep OUT.gcode.plan.json next to the G-code")
MACH = machine.load(mid)
HEAD_R = float(MACH["head_radius"])      # extruder_clearance_max_radius
ROD = float(MACH["rod_height"])          # extruder_clearance_height_to_rod
ROD_SAFE = ROD - 4.0
RAIL_HALF = float(MACH["rail_half"])     # assume the gantry is as deep as the toolhead envelope
Z_MAX = float(MACH["z_max"])
print(f"     machine {mid}: head radius {HEAD_R:g}, rod {ROD:g}, z max {Z_MAX:g}")
if MACH.get("hidden_change_moves"):
    print("[!]  cut/flush moves happen inside firmware and are NOT checked (Z raised by splice.py)")
L = open(a.gcode, encoding="utf-8").read().split("\n")


def seg_box_dist(x0, y0, x1, y1, b):
    """Distance from segment to axis-aligned box b=(xa,ya,xb,yb), sampled (moves are short or
    straight; 1 mm steps is plenty for a 68 mm clearance)."""
    n = max(1, int(math.hypot(x1 - x0, y1 - y0)))
    best = 1e9
    for k in range(n + 1):
        t = k / n
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        dx = max(b[0] - x, 0, x - b[2])
        dy = max(b[1] - y, 0, y - b[3])
        best = min(best, math.hypot(dx, dy))
    return best


# pass 1: who printed what (bounding boxes)
st = gc.State()
owner = "startup"
boxes = {}
for ln in L:
    if ln.startswith("; CHANGE_LAYER") and owner == "startup":
        owner = "main"
    if ln.startswith("; PURGE_VISIT_START"):
        owner = "purge"
    elif ln.startswith("; PURGE_VISIT_END"):
        owner = "main"
    m = gc.step(st, ln)
    if m and m[6] > 0:
        b = boxes.setdefault(owner, [1e9, 1e9, -1e9, -1e9])
        for x, y in ((m[1], m[2]), (m[3], m[4])):
            b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)

# startup extrusions (the purge line) must stay clear of every object footprint
st = gc.State()
startup_segs = []
for ln in L:
    if ln.startswith("; CHANGE_LAYER"):
        break
    m = gc.step(st, ln)
    if m and m[6] > 0:
        startup_segs.append(m)
startup_fail = []
for k, b in boxes.items():
    if k == "startup":
        continue
    d = min((seg_box_dist(m[1], m[2], m[3], m[4], b) for m in startup_segs), default=1e9)
    print(f"     start purge line to {k} footprint: {d:.1f} mm")
    if d < 2.0:
        startup_fail.append(f"start purge line {d:.1f} mm from the {k} footprint (need 2)")

# pass 2: heights over time + clearance checks
st = gc.State()
h = {k: 0.0 for k in boxes}
owner = "startup"
fails, worst_head, worst_rail = list(startup_fail), {}, {}
t = 0.0
timeline = []
purge_layers, main_layers = [], []
for i, ln in enumerate(L):
    if ln.startswith("; CHANGE_LAYER") and owner == "startup":
        owner = "main"
    if ln.startswith("; PURGE_VISIT_START"):
        owner = "purge"
    elif ln.startswith("; PURGE_VISIT_END"):
        owner = "main"
    if ln.startswith("; PURGE layer num"):
        purge_layers.append(int(ln.split(":")[1].split("/")[0]))
    elif ln.startswith("; layer num"):
        main_layers.append(int(ln.split(":")[1].split("/")[0]))
    z0 = st.z
    m = gc.step(st, ln)
    if not m:
        continue
    _, x0, y0, x1, y1, z1, e = m
    if st.f:
        t += math.hypot(x1 - x0, y1 - y0, z1 - z0) / st.f   # minutes, no accel: rough
    zt = min(z0, z1)
    if zt > Z_MAX + 1e-6 or max(z0, z1) > Z_MAX + 1e-6:
        fails.append(f"line {i+1}: Z {max(z0, z1)} over {Z_MAX}")
    if owner != "startup" or i > 0:
        for k, b in boxes.items():
            # the start line is a flat line on the bed edge, covered by the keep-out and the
            # footprint check above; its tracked height is not real on the H2S (the load line
            # re-zeros Z with G28.14 at Z5, so the model reads 5.8 for a line ~0.8 mm high)
            if k == "startup":
                continue
            if h[k] > 0 and h[k] > zt + 0.05:
                d = seg_box_dist(x0, y0, x1, y1, b)
                if d < worst_head.get(k, (1e9,))[0]:
                    worst_head[k] = (d, i + 1, round(zt, 2), round(h[k], 2))
                if d < HEAD_R:
                    fails.append(f"line {i+1}: toolhead {d:.1f} mm from {k} "
                                 f"({k} {h[k]:.2f} mm tall, nozzle at {zt:.2f})")
            if h[k] > 0 and h[k] > zt + ROD_SAFE:
                dy = min(max(b[1] - y, 0, y - b[3]) for y in (y0, y1))
                if dy < worst_rail.get(k, (1e9,))[0]:
                    worst_rail[k] = (dy, i + 1, round(zt, 2), round(h[k], 2))
                if dy < RAIL_HALF:
                    fails.append(f"line {i+1}: X rail over {k}: {k} {h[k]:.2f} mm tall, rail at "
                                 f"~{zt + ROD:.2f}, Y gap {dy:.1f} mm")
    if e > 0:
        h[owner] = max(h[owner], z1)
    if a.timeline and (not timeline or t - timeline[-1][0] > 0.25 or e > 0 and len(timeline) % 50 == 0):
        timeline.append((round(t, 2), round(z1, 2), round(h.get("main", 0), 2), round(h.get("purge", 0), 2)))

expect = list(range(1, max(purge_layers) + 1)) if purge_layers else []
if sorted(purge_layers) != expect:
    fails.append(f"purge layers not printed exactly once: {len(purge_layers)} comments")
if main_layers != sorted(main_layers):
    fails.append("main layers out of order")

for k, b in boxes.items():
    print(f"     {k:<8} box X {b[0]:.1f}..{b[2]:.1f}  Y {b[1]:.1f}..{b[3]:.1f}  final height {h[k]:.2f}")
for k, (d, ln, z, hh) in worst_head.items():
    print(f"     closest toolhead pass to taller {k}: {d:.1f} mm (limit {HEAD_R}) at line {ln}, "
          f"nozzle {z}, {k} {hh}")
for k, (d, ln, z, hh) in worst_rail.items():
    print(f"     closest rail pass over {k} >{ROD_SAFE:.0f} mm above nozzle: Y gap {d:.1f} mm "
          f"(limit {RAIL_HALF}) at line {ln}, nozzle {z}, {k} {hh}")
if not worst_rail:
    print(f"     rail: no object ever more than {ROD_SAFE:.0f} mm above the nozzle")
print(f"     rough motion time {t:.0f} min (no acceleration; Studio's estimate is better)")
if a.timeline:
    json.dump(timeline, open(a.timeline, "w"))
if fails:
    print(f"[X] {len(fails)} problems, first ones:")
    for f in fails[:15]:
        print("    ", f)
    sys.exit(1)
print("[OK] no collisions found")
