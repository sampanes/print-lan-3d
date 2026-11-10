"""Where a multi-color print's filament changes ("swaps") happen and what they cost, before any
purge object. Read this first: the cheapest flush is a swap that never happens.

The slicer swaps filament on every layer that holds more than one color, so a color that is
printed only in a thin band (a trim strip, a cap, a lettering layer) can cost many swaps for a
few grams of plastic. This script lists every swap with its flush volume, groups them into
height bands, and names each filament that is printed only inside one band: those are the
candidates to make a separate part and print by object (see README, "Fewer swaps first").

Flush per swap: the firmware flush length in the slice (`M620.10 ... L<mm>`, Bambu H2-series
and OrcaSlicer output) when it is there and not 0, else the flush matrix from --job or
--matrix (needed for slices made with the matrix at 0 for splice.py).

Usage: python swaps.py SLICED.gcode [--job job.json | --matrix v,v,...] [--names a,b,...]
       [--band-gap 2] [--json OUT.json]
Needs a by-layer slice (print-by-object restarts the layer count per object).
"""
import argparse, json, math, re, sys
import gcmodel as gc

_L = re.compile(r"^M620\.10 A0\b.*?\sL([\d.]+)")


def analyze(lines, matrix=None, band_gap=2.0):
    """Swaps, bands and per-filament use of a by-layer slice. matrix[from][to] in mm^3."""
    layers = gc.layer_index(lines)
    if not layers:
        raise SystemExit("[X] no '; layer num/total_layer_count' comments: not a Bambu/Orca slice")
    zs = gc.layer_z(lines)
    l1 = layers[1]
    pairs, changes = gc.filament_changes(lines, layers)

    # flush length written for each change, between its M620 and M621
    spans, start = [], None
    for i, ln in enumerate(lines):
        t = gc._TOOL.match(ln)
        if t and int(t[2]) < 255 and i > l1:
            if t[1] == "0":
                start = i
            else:
                spans.append((start, i))
    swaps = []
    for (f, t), (layer, _), (a, b) in zip(pairs, changes, spans):
        lens = [float(m[1]) for m in map(_L.match, lines[a:b]) if m]
        sliced = max(lens, default=0.0) * gc.FIL_AREA
        if sliced > 0:
            vol, src = sliced, "slice"
        elif matrix is not None and f is not None:
            vol, src = float(matrix[f][t]), "matrix"
        else:
            vol, src = 0.0, "none"
        swaps.append(dict(layer=layer, z=zs.get(layer), frm=f, to=t, flush=vol, source=src))

    # extrusion per filament per layer
    use, cur, layer, st = {}, None, 0, gc.State()
    starts = {v: k for k, v in layers.items()}
    for i, ln in enumerate(lines):
        if i in starts:
            layer = starts[i]
        t = gc._TOOL.match(ln)
        if t and t[1] == "1" and int(t[2]) < 255:
            cur = int(t[2])
        m = gc.step(st, ln)
        if layer and m and m[6] > 0 and cur is not None:
            use.setdefault(cur, {}).setdefault(layer, 0.0)
            use[cur][layer] += m[6] * gc.FIL_AREA
    fil = {f: dict(z0=zs[min(d)], z1=zs[max(d)], layers=len(d), volume=sum(d.values()))
           for f, d in use.items()}

    zl = sorted(z for z in zs.values() if z is not None)
    lh = sorted(b - a for a, b in zip(zl, zl[1:]) if b > a)
    lh = lh[len(lh) // 2] if lh else 0.2

    bands = []
    for s in sorted(swaps, key=lambda s: s["z"]):
        if bands and s["z"] - bands[-1]["z1"] <= band_gap:
            bands[-1]["swaps"].append(s)
            bands[-1]["z1"] = s["z"]
        else:
            bands.append(dict(z0=s["z"], z1=s["z"], swaps=[s]))
    total = sum(s["flush"] for s in swaps)
    for b in bands:
        b["flush"] = sum(s["flush"] for s in b["swaps"])
        b["share"] = b["flush"] / total if total else 0.0
        # filaments printed only inside this band (one layer of margin either side)
        b["only_here"] = [f for f, u in fil.items()
                          if u["z0"] >= b["z0"] - lh * 1.5 and u["z1"] <= b["z1"] + lh * 1.5]
    return dict(swaps=swaps, bands=bands, filaments=fil, layer_height=lh, total=total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode")
    ap.add_argument("--job", help="make3mf.py's job.json (flush_matrix)")
    ap.add_argument("--matrix", help="flush matrix, n*n values in mm^3, row = from")
    ap.add_argument("--names", help="filament names in order, comma separated")
    ap.add_argument("--band-gap", type=float, default=2.0, help="mm between swaps of one band")
    ap.add_argument("--json")
    a = ap.parse_args()
    L = open(a.gcode, encoding="utf-8").read().split("\n")
    if any(ln.startswith("; print_sequence = by object") for ln in L[:5000]) or \
            any(ln.startswith("; print_sequence = by object") for ln in L[-3000:]):
        sys.exit("[X] print-by-object slice: slice it by layer for this report")
    matrix = None
    if a.job:
        matrix = json.load(open(a.job)).get("flush_matrix")
    elif a.matrix:
        v = [float(x) for x in a.matrix.split(",")]
        n = int(round(math.sqrt(len(v))))
        matrix = [v[i * n:(i + 1) * n] for i in range(n)]
    r = analyze(L, matrix, a.band_gap)
    names = a.names.split(",") if a.names else []
    nm = lambda f: (names[f] if f is not None and f < len(names) else f"F{f + 1}" if f is not None else "?")

    sw, lh, total = r["swaps"], r["layer_height"], r["total"]
    if not sw:
        print("[OK] no filament changes after layer 1: nothing to purge")
        return 0
    print(f"     {len(sw)} swaps, {total:.0f} mm3 of flush (about {total * 1.24 / 1000:.1f} g of PLA); "
          f"layer height {lh:g} mm")
    if any(s["source"] == "none" for s in sw):
        print("[!]  some swaps have no flush volume (slice flushes 0 and no --job/--matrix given)")
    print("     layer      z   swap              flush mm3  solid area for a same-layer purge")
    for s in sw:
        area = s["flush"] / lh
        print(f"     {s['layer']:5d} {s['z']:6.2f}   {nm(s['frm']) + ' -> ' + nm(s['to']):<16} {s['flush']:9.0f}"
              f"   {area:7.0f} mm2 ({math.sqrt(area):.0f} mm square)")
    print("     filaments:")
    for f, u in sorted(r["filaments"].items()):
        print(f"       {nm(f):<10} z {u['z0']:.2f}-{u['z1']:.2f} ({u['layers']} layers), {u['volume']:.0f} mm3")
    print("     bands (swaps within --band-gap mm of each other):")
    for b in r["bands"]:
        only = ", ".join(nm(f) for f in b["only_here"]) or "-"
        print(f"       z {b['z0']:.2f}-{b['z1']:.2f}: {len(b['swaps'])} swaps, {b['flush']:.0f} mm3 "
              f"({b['share'] * 100:.0f} %); printed only here: {only}")
    hints = []
    for b in sorted(r["bands"], key=lambda b: -b["flush"]):
        for f in b["only_here"]:
            u = r["filaments"][f]
            hints.append(f"{nm(f)} is printed only at z {u['z0']:.2f}-{u['z1']:.2f} ({u['volume']:.0f} mm3) and "
                         f"this band costs {len(b['swaps'])} swaps, {b['flush']:.0f} mm3 of flush "
                         f"({b['share'] * 100:.0f} %): make it a separate part and print by object "
                         f"(README, 'Fewer swaps first').")
    if hints:
        print("     hints:")
        for h in hints:
            print("       " + h)
    if a.json:
        json.dump(r, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
