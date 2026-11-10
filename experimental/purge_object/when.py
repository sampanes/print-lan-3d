"""Rough clock for a spliced print: when each purge visit starts and how long it lasts.

Uses Studio's own M73 remaining-time lines from the two unspliced slices, so it is as good
as Studio's estimate (it ignores the hops, a few seconds each). Times count from the end of
the start G-code (first layer), not from pressing print.

Usage: python when.py MAIN.gcode PURGE.gcode OUT.gcode.plan.json
"""
import json, re, sys
import gcmodel as gc

if len(sys.argv) != 4:
    sys.exit(__doc__)
M = open(sys.argv[1], encoding="utf-8").read().split("\n")
P = open(sys.argv[2], encoding="utf-8").read().split("\n")
plan = json.load(open(sys.argv[3]))


def remaining(lines):            # minutes left per line, from the last M73 P.. R.. seen
    r, out = None, []
    for ln in lines:
        m = re.match(r"M73 P\d+ R(\d+)", ln)
        if m:
            r = int(m[1])
        out.append(r)
    return out


mr, pr = remaining(M), remaining(P)
mtot = max(x for x in mr if x is not None)
ptot = max(x for x in pr if x is not None)
pli = {}
for i, ln in enumerate(P):
    m = re.match(r"; layer num/total_layer_count: (\d+)/", ln)
    if m:
        pli[int(m[1])] = i
pend = gc.end_index(P)

extra = 0
for v in plan["visits"]:
    t_main = mtot - (mr[v["anchor"]] or mtot)
    a, b = pli[v["l0"]], pli.get(v["l1"] + 1, pend)
    dur = (pr[a] or ptot) - (pr[b] or 0)
    start = t_main + extra
    print(f"{v['why']:<13} after main layer {v['main_layer']:>3}: starts ~{start:3d} min, "
          f"lasts ~{dur:2d} min, ends ~{start + dur:3d}")
    extra += dur
print(f"total ~{mtot + extra} min (the printer's own countdown only knows the main print: "
      f"{mtot} min)")
