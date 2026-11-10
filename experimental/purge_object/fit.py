"""Size the purge object to the purge it has to take: scale it up when the flushes do not fit,
down when it holds far more than they need (the whole object is printed either way, so spare
capacity is wasted filament).

Reads the main print's filament changes and a slice of the purge object (at any scale; say
which with --at), predicts the purge capacity at other scales and prints the smallest scale
that the TIGHT plan can use:
  - each change's flush fits in purge layers no higher than the main print + --lead at that
    change (cumulative: every earlier change has used the layers below)
  - the purge object's top is at most the main print's last-but-one layer + --lead, so the
    catch-up visit can finish it
Model: per layer, wall-like extrusion (outer/inner/overhang walls, gap infill) grows with the
XY scale a and the rest (infill, solid, top/bottom, bridges) with a^2; heights stretch with
the Z scale b. With W(h), A(h) the wall and area volume up to height h at scale 1, the volume
up to height h at (a, b) is  b * (a * W(h/b) + a^2 * A(h/b)).  Top/bottom shells do not
really stretch with b, so this is an estimate: --margin covers it, and splice.py has the
final word (it exits 3 when the purge object runs out).

Prefers one uniform scale. When the height limit rules that out (likely with a small lead,
e.g. 2 mm on the H2S), it widens more than it heightens: the least filament that fits,
never taller than proportional (b <= a) and never squashed past --max-squash (a <= 1.5 b by
default: squashing flattens overhangs, tan(angle) grows by a/b, and a drooping overhang on
the purge object is a collision risk).

Usage: python fit.py MAIN.gcode PURGE.gcode --job job.json [--flush V1,V2,...] [--lead 25]
       [--at A,B] [--margin 1.10] [--min 0.5] [--max 2.5] [--max-squash 1.5] [--json OUT.json]
The last line printed is the scale as 'A,B' (XY, Z) for make3mf.py / run.py --purge-scale.
Exit 0 = a scale was found, 2 = nothing in range fits.
"""
import argparse, bisect, json, sys
import gcmodel as gc


def profile(purge_lines, at=(1.0, 1.0)):
    """Cumulative (z, W, A) per purge layer, normalised to scale 1. Layer 1 is left out: the
    base visit prints it before any change, so it takes no flush."""
    a0, b0 = at
    rows, w_sum, a_sum = [], 0.0, 0.0
    for k, (z, w, ar) in sorted(gc.layer_volumes(purge_lines).items()):
        if k == 1 or z is None:
            continue
        w_sum += w / (a0 * b0)
        a_sum += ar / (a0 * a0 * b0)
        rows.append((z / b0, w_sum, a_sum))
    if not rows:
        raise SystemExit("[X] the purge slice has no layers above layer 1")
    return rows


class Model:
    def __init__(self, rows, change_z, flush, lead, top_limit, margin):
        self.z = [r[0] for r in rows]
        self.rows = rows
        self.change_z, self.flush = change_z, flush
        self.lead, self.top_limit, self.margin = lead, top_limit, margin
        self.top1 = rows[-1][0]

    def capacity(self, a, b, h):
        """Purge volume, mm^3, printed up to height h at scale (a, b) (layer 1 excluded)."""
        i = bisect.bisect_right(self.z, h / b + 1e-9) - 1
        if i < 0:
            return 0.0
        _, w, ar = self.rows[i]
        return b * (a * w + a * a * ar)

    def total(self, a, b):
        return self.capacity(a, b, self.top1 * b)

    def problems(self, a, b):
        """Why (a, b) does not fit; [] when it does."""
        out = []
        if self.top1 * b > self.top_limit + 1e-6:
            out.append(f"top {self.top1 * b:.1f} mm is over the main print's last layer + lead "
                       f"({self.top_limit:.1f} mm)")
        cum = 0.0
        for k, (zc, f) in enumerate(zip(self.change_z, self.flush)):
            cum += f
            have = self.capacity(a, b, zc + self.lead)
            if have < self.margin * cum:
                out.append(f"change {k + 1} (main at {zc:.2f} mm): {cum:.0f} mm3 flushed so far "
                           f"x {self.margin:g} margin, {have:.0f} mm3 below {zc + self.lead:.2f} mm")
                break
        return out

    def fits(self, a, b):
        return not self.problems(a, b)


def choose(m, lo, hi, squash=1.5, step=0.01):
    """Smallest uniform scale that fits, else the least-filament (a, b) with b <= a; None if
    nothing in [lo, hi] fits; a <= squash * b. Returns (a, b, how)."""
    grid = [round(lo + i * step, 4) for i in range(int(round((hi - lo) / step)) + 1)]
    for s in grid:
        if m.fits(s, s):
            return s, s, "uniform"
    best = None
    for b in grid:
        if m.top1 * b > m.top_limit + 1e-6:
            break
        cand = [a for a in grid if b <= a <= squash * b + 1e-9]
        if not cand or not m.fits(cand[-1], b):
            continue
        i, j = 0, len(cand) - 1                  # capacity grows with a: bisect the first fit
        while i < j:
            k = (i + j) // 2
            if m.fits(cand[k], b):
                j = k
            else:
                i = k + 1
        t = m.total(cand[i], b)
        if best is None or t < best[0]:
            best = (t, cand[i], b)
    if best:
        return best[1], best[2], "wider than tall (the height limit rules out a uniform scale)"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("main")
    ap.add_argument("purge")
    ap.add_argument("--job", help="make3mf.py's job.json (flush matrix, purge footprint)")
    ap.add_argument("--flush", help="flush volume per change, mm^3, comma separated")
    ap.add_argument("--lead", type=float, default=25.0, help="as splice.py --lead")
    ap.add_argument("--at", default="1,1", help="XY,Z scale the purge slice was made at")
    ap.add_argument("--margin", type=float, default=1.10)
    ap.add_argument("--min", type=float, default=0.5)
    ap.add_argument("--max", type=float, default=2.5)
    ap.add_argument("--max-squash", type=float, default=1.5, help="largest XY/Z scale ratio")
    ap.add_argument("--json")
    a = ap.parse_args()
    job = json.load(open(a.job)) if a.job else {}

    M = open(a.main, encoding="utf-8").read().split("\n")
    P = open(a.purge, encoding="utf-8").read().split("\n")
    mz = gc.layer_z(M)
    pairs, changes = gc.filament_changes(M)
    if not changes:
        print("[OK] the main print has no filament changes after layer 1: no purge object needed")
        return 0
    if a.flush:
        flush = [float(v) for v in a.flush.split(",")]
    elif job.get("flush_matrix"):
        flush = [job["flush_matrix"][f][t] for f, t in pairs]
    else:
        sys.exit("[X] no flush volumes: pass --job job.json or --flush")
    if len(flush) != len(changes):
        sys.exit(f"[X] {len(changes)} filament changes in main, {len(flush)} flush volumes given")

    at = tuple(float(v) for v in (a.at.split(",") * 2)[:2])
    rows = profile(P, at)
    last = max(mz)
    m = Model(rows, [mz[l] for l, _ in changes], flush, a.lead, mz[last - 1] + a.lead, a.margin)
    need = sum(flush)
    print(f"     {len(changes)} changes, {need:.0f} mm3 to flush; lead {a.lead:g} mm; "
          f"main print {mz[last]:.2f} mm tall")
    have = m.total(*at)
    print(f"     purge object as sliced (scale {at[0]:g},{at[1]:g}): {have:.0f} mm3 above layer 1, "
          f"{m.top1 * at[1]:.1f} mm tall")
    for p in m.problems(*at):
        print(f"     does not fit as sliced: {p}")

    pick = choose(m, a.min, a.max, a.max_squash)
    if not pick:
        print(f"[X] no scale between {a.min:g} and {a.max:g} fits:")
        b = min(a.max, m.top_limit / m.top1)
        for p in m.problems(min(a.max, a.max_squash * b), b):
            print(f"     at the largest allowed: {p}")
        print("     Use a purge object with more volume low down (a wider, squatter shape), a bigger "
              "--lead where the machine allows it, or --max-squash above 1.5 at your own risk.")
        return 2
    sa, sb, how = pick
    tot = m.total(sa, sb)
    print(f"[OK] scale XY {sa:.2f}, Z {sb:.2f} ({how}): about {tot:.0f} mm3, "
          f"{tot / need:.2f}x the flush; {m.top1 * sb:.1f} mm tall")
    rect = job.get("purge_rect")
    if rect:
        w, d = (rect[2] - rect[0]) / at[0] * sa, (rect[3] - rect[1]) / at[0] * sa
        print(f"     footprint about {w:.1f} x {d:.1f} mm (make3mf.py checks it fits the bed)")
    if a.json:
        json.dump(dict(xy=sa, z=sb, how=how, volume_mm3=round(tot), flush_mm3=round(need),
                       top_mm=round(m.top1 * sb, 2), lead=a.lead, margin=a.margin),
                  open(a.json, "w"), indent=1)
    print(f"{sa:.2f},{sb:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
