"""Splice a purge object's layers into a main print at its colour changes (Bambu Studio G-code).

Both inputs are plate_1.gcode files sliced with identical settings, prime tower off and the
flush volumes set to 0 (on the P1S the AMS block then only pushes 2 mm into the chute). The purge object
is printed in chunks of whole layers at its own Z:
  visit 0  end of main layer 1   purge layer 1 (adhesion + gets the bed probed)
  visit k  right after the k-th filament change (after M621): purge layers flush it

Plans:
  max    change 1 -> purge up to the main print's next change height; change 2 -> purge to the
         top (relies on front/back separation from the X rail).
  tight  each change gets just enough purge layers for its flush volume (--flush, mm^3); the
         purge object never gets more than --lead mm above the main print; whatever is left is
         finished on a same-colour visit once the main print has caught up.

Usage: python splice.py MAIN.gcode PURGE.gcode OUT.gcode --plan max|tight --job job.json
       [--flush V1,V2,...] [--lead 25]
--job is make3mf.py's job.json: the machine and the flush volumes the project wanted (looked up
per change from the flush matrix). --flush overrides them.
Machines with hidden_change_moves (firmware does the cut/flush, e.g. H2S): every Z move inside a
filament-change block and in the end G-code is raised to the tallest printed object + 3 mm.
Writes OUT.gcode and OUT.plan.json (the visits, for check.py and the notes).
"""
import argparse, json, re, sys
import gcmodel as gc
import machine

ap = argparse.ArgumentParser()
ap.add_argument("main")
ap.add_argument("purge")
ap.add_argument("out")
ap.add_argument("--plan", choices=["max", "tight"], required=True)
ap.add_argument("--job", help="make3mf.py's job.json (machine, flush matrix)")
ap.add_argument("--machine", help="machine profile (default: from --job)")
ap.add_argument("--flush", help="flush volume per change, mm^3, comma separated")
ap.add_argument("--lead", type=float, default=25.0, help="tight: max purge height above main")
ap.add_argument("--zfeed", type=float, default=1200)
a = ap.parse_args()
job = json.load(open(a.job)) if a.job else {}
MID = a.machine or job.get("machine")
if not MID:
    sys.exit("[X] pass --job job.json or --machine")
MACH = machine.load(MID)

M = open(a.main, encoding="utf-8").read().split("\n")
P = open(a.purge, encoding="utf-8").read().split("\n")
mst, mh = gc.walk(M)
pst, ph = gc.walk(P)
mli, pli = gc.layer_index(M), gc.layer_index(P)
mz, pz = gc.layer_z(M), gc.layer_z(P)
m_ref = mst[gc.primed_ref(M, mli[1])].depth
p_ref = pst[gc.primed_ref(P, pli[1])].depth
P_END = gc.end_index(P)
M_END = gc.end_index(M)
NP = max(pli)

# filament changes in the main print (skip the start-of-print load, before layer 1)
pairs, changes = gc.filament_changes(M, mli)
TOOL = re.compile(r"^M62([01]) S(\d+)A")
if a.flush:
    flush = [float(v) for v in a.flush.split(",")]
elif job.get("flush_matrix"):
    fm = job["flush_matrix"]
    flush = [fm[f][t] for f, t in pairs]
else:
    sys.exit("[X] no flush volumes: pass --job job.json or --flush")
if len(flush) != len(changes):
    sys.exit(f"[X] {len(changes)} filament changes in main, {len(flush)} flush volumes given")
for (f, t), v in zip(pairs, flush):
    print(f"[OK] change filament {f + 1} -> {t + 1}: flush {v:.0f} mm3 into the purge object")


def purge_line(layer):           # chunk boundary: the purge object's layer comment
    return pli[layer] if layer <= NP else P_END


def vol(l0, l1):                 # filament volume extruded by purge layers l0..l1, mm^3
    e = 0.0
    for i in range(purge_line(l0), purge_line(l1 + 1)):
        c = gc.cmd(P[i])
        if c in ("G1", "G2", "G3"):
            p = gc.params(P[i])
            if p.get("E", 0) > 0 and ("X" in p or "Y" in p):
                e += p["E"]
    return e * gc.FIL_AREA


visits = [dict(why="layer 1 base", anchor=mli[2], main_layer=1, l0=1, l1=1)]
nxt = 2
if a.plan == "max":
    for k, (layer, at) in enumerate(changes):
        if k + 1 < len(changes):
            top = changes[k + 1][0] - 1          # main layers printed before the next change
            l1 = max(l for l in pli if pz[l] <= mz[top] + 1e-6)
        else:
            l1 = NP
        visits.append(dict(why=f"change {k+1}", anchor=at, main_layer=layer, l0=nxt, l1=l1))
        nxt = l1 + 1
else:
    for k, (layer, at) in enumerate(changes):
        cap = mz[layer] + a.lead
        l1 = nxt
        while vol(nxt, l1) < flush[k] and l1 < NP:
            l1 += 1
        if nxt > NP or vol(nxt, l1) < flush[k]:
            left = vol(nxt, NP) if nxt <= NP else 0.0
            need = sum(flush[k:])
            print(f"[X] change {k+1}: the purge object is used up; changes {k+1}-{len(changes)} need "
                  f"{need:.0f} mm3, {left:.0f} mm3 left. Scale it up (fit.py, run.py --purge-scale auto).")
            sys.exit(3)
        if pz[l1] > cap:
            sys.exit(f"[X] change {k+1}: flush needs purge up to {pz[l1]} mm, over the {a.lead} mm lead")
        if k + 1 == len(changes):                # last change: go as high as the lead allows
            l1 = max(l for l in pli if pz[l] <= cap)
        visits.append(dict(why=f"change {k+1}", anchor=at, main_layer=layer, l0=nxt, l1=l1))
        nxt = l1 + 1
    if nxt <= NP:                                # one catch-up visit, same colour, no flush
        ok = [l for l in mli if l > visits[-1]["main_layer"] and l + 1 in mli
              and mz[l] >= pz[NP] - a.lead]
        if not ok:
            sys.exit("[X] main print never gets within --lead of the purge object's top")
        layer = min(ok)
        visits.append(dict(why="catch-up", anchor=mli[layer + 1], main_layer=layer, l0=nxt, l1=NP))
        nxt = NP + 1
if nxt <= NP:
    sys.exit(f"[X] plan leaves purge layers {nxt}..{NP} unprinted")

DROP = re.compile(r"^(M73 |M991 |M104 |M109 |M140 |M190 |M1003 |M981 |M624 |M625 )")


def chunk(l0, l1):
    out, skip = [], False
    for ln in P[purge_line(l0):purge_line(l1 + 1)]:
        if ln.startswith("; SKIPPABLE_START"):
            skip = True
        if skip:
            skip = not ln.startswith("; SKIPPABLE_END")
            continue
        if DROP.match(ln):
            continue
        if ln.startswith(("; CHANGE_LAYER", "; Z_HEIGHT", "; LAYER_HEIGHT", "; layer num")):
            ln = "; PURGE" + ln[1:]
        out.append(ln)
    return out


def state_lines(st):
    out = [st.fans[k] for k in sorted(st.fans)]
    if st.m204:
        out.append(st.m204)
    if st.f:
        out.append(f"G1 F{st.f:g}")
    return out


def hop(frm_xyz, frm_depth, to_xyz, to_depth, safe_z, defer=False):
    """Retract (at least 0.8), lift to safe_z, travel, descend, then restore the target's
    retraction depth. With defer, a needed unretract is returned instead of emitted, so the
    caller can fold it into the target's own first prime (like Studio's G1 E2 after a change)."""
    out = []
    d = frm_depth
    if d < 0.8:
        out.append(f"G1 E{-(0.8 - d):.3f} F1800")
        d = 0.8
    out += [f"G1 Z{safe_z:.3f} F{a.zfeed:g}",
            f"G1 X{to_xyz[0]:.3f} Y{to_xyz[1]:.3f} F30000",
            f"G1 Z{to_xyz[2]:.3f} F{a.zfeed:g}"]
    adj = d - to_depth
    if defer and adj > 0:
        return out, adj
    if abs(adj) > 0.01:
        out.append(f"G1 E{adj:.3f} F1800")
    return out, 0.0


def fold_prime(lines, adj):
    """Add adj mm to the first pure-E prime in lines (e.g. 'G1 E.8 F1800')."""
    if adj <= 0.01:
        return lines
    for i, ln in enumerate(lines):
        if gc.cmd(ln) == "G1":
            p = gc.params(ln)
            if p.get("E", 0) > 0 and not ("X" in p or "Y" in p or "Z" in p):
                f = f" F{p['F']:g}" if "F" in p else ""
                lines[i] = f"G1 E{p['E'] + adj:.3f}{f} ; prime + {adj:.3f} carried over from the main print"
                return lines
    raise SystemExit("[X] no prime line in the purge chunk to fold the unretract into")


def first_xy(lines):
    for ln in lines:
        if gc.cmd(ln) in ("G0", "G1"):
            p = gc.params(ln)
            if "X" in p and "Y" in p:
                return p["X"], p["Y"]
    raise SystemExit("[X] chunk has no XY move")


out = []
pos = 0
purge_h = 0.0
log = []
for v in sorted(visits, key=lambda v: v["anchor"]):
    at = v["anchor"]
    out += M[pos:at]
    pos = at
    ms = mst[at]
    s0, s1 = purge_line(v["l0"]), purge_line(v["l1"] + 1)
    ps0, ps1 = pst[s0], pst[s1]
    main_h = mh[at]
    top = max(ph[s1], purge_h)
    vol_mm3 = vol(v["l0"], v["l1"])
    out.append(f"; PURGE_VISIT_START {v['why']}: purge layers {v['l0']}-{v['l1']} "
               f"(z {pz[v['l0']]:.2f}-{pz[v['l1']]:.2f}), {vol_mm3:.0f} mm3")
    body = chunk(v["l0"], v["l1"])
    tx, ty, tz = ps0.x, ps0.y, ps0.z
    if v["l0"] == 1:                     # nothing printed yet: go straight to its first point
        (tx, ty), tz = first_xy(body), pz[1] + 0.2
    lines, adj = hop((ms.x, ms.y, ms.z), ms.depth - m_ref, (tx, ty, tz), ps0.depth - p_ref,
                     max(ms.z, main_h, purge_h, tz) + 2.0, defer=True)
    out += lines
    out += state_lines(ps0)
    out += fold_prime(body, adj)
    out += hop((ps1.x, ps1.y, ps1.z), ps1.depth - p_ref, (ms.x, ms.y, ms.z), ms.depth - m_ref,
               max(ps1.z, top, main_h, ms.z) + 2.0)[0]
    out += state_lines(ms)
    out.append(f"; PURGE_VISIT_END top={top:.3f}")
    purge_h = top
    log.append(dict(v, main_z=mz[v["main_layer"]], purge_z0=pz[v["l0"]], purge_z1=pz[v["l1"]],
                    volume_mm3=round(vol_mm3)))
if pos > M_END:
    sys.exit("[X] a purge visit lands inside the main print's end G-code")
end_out = len(out) + (M_END - pos)       # where the end G-code starts in the spliced file
out += M[pos:]

# probe the purge object's footprint too (P1S: "G29 A X.. Y.. I.. J..", H2S: "G29 A1 O X..")
g29 = re.compile(r"^(\s*G29 A\d*(?: O)?) X(\S+) Y(\S+) I(\S+) J(\S+)")
pm = next((g29.match(l) for l in P if g29.match(l)), None)
n29 = 0
for i, l in enumerate(out):
    m = g29.match(l)
    if m and pm:
        x0 = min(float(m[2]), float(pm[2])); y0 = min(float(m[3]), float(pm[3]))
        x1 = max(float(m[2]) + float(m[4]), float(pm[2]) + float(pm[4]))
        y1 = max(float(m[3]) + float(m[5]), float(pm[3]) + float(pm[5]))
        out[i] = f"{m[1]} X{x0:.3f} Y{y0:.3f} I{x1-x0:.3f} J{y1-y0:.3f}"
        n29 += 1
if not n29:
    sys.exit("[X] no G29 A (bed leveling area) line in main and purge")

# hidden firmware moves: keep every Z inside change blocks and the end G-code above both objects
raised = 0
if MACH.get("hidden_change_moves"):
    top, inside, absolute = 0.0, False, True
    for i, l in enumerate(out):
        if l.startswith("; PURGE_VISIT_END top="):
            top = max(top, float(l.split("=")[1]))
        t = TOOL.match(l)
        if t and int(t[2]) < 255:
            inside = t[1] == "0"
        if i >= end_out:
            inside = True
        c = gc.cmd(l)
        if c in ("G90", "G91"):
            absolute = c == "G90"
        if inside and absolute and c in ("G0", "G1") and top > 0:
            p = gc.params(l)
            if "Z" in p and p["Z"] < top + 3.0:
                out[i] = re.sub(r"Z-?\d*\.?\d+", f"Z{top + 3.0:.3f}", l.split(";", 1)[0], count=1) \
                    + f" ; raised from Z{p['Z']:g} (hidden firmware moves)"
                raised += 1
    print(f"[OK] {raised} Z moves raised above the purge object (hidden_change_moves)")

open(a.out, "w", encoding="utf-8", newline="\n").write("\n".join(out))
json.dump(dict(plan=a.plan, machine=MID, flush=flush, lead=a.lead, visits=log, raised_z=raised,
               main_bbox=None), open(a.out + ".plan.json", "w"), indent=1)
for v in log:
    print(f"[OK] {v['why']:<13} after main layer {v['main_layer']:>3} (z {v['main_z']:5.2f}): "
          f"purge layers {v['l0']:>3}-{v['l1']:>3} (z {v['purge_z0']:5.2f}-{v['purge_z1']:5.2f}), "
          f"{v['volume_mm3']} mm3")
