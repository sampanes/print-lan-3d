"""Height-over-time chart (main print vs purge object) from check.py --timeline JSON files,
one panel per file. The dashed line is the main print + the machine's rod height (X rail).
Usage: python timeline.py out.png a.timeline.json [b.timeline.json ...] [--rod 34]"""
import argparse, json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MAIN, PURGE = "#2a78d6", "#eb6834"
ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("files", nargs="+")
ap.add_argument("--rod", type=float, default=34.0, help="extruder_clearance_height_to_rod")
a = ap.parse_args()
ROD, out, files = a.rod, a.out, a.files
fig, axes = plt.subplots(1, len(files), figsize=(6 * len(files), 4.6), sharey=True,
                         facecolor=SURF, squeeze=False)
axes = axes[0]
for ax, f in zip(axes, files):
    d = json.load(open(f))
    t = [r[0] for r in d]; m = [r[2] for r in d]; p = [r[3] for r in d]
    rail = [x + ROD for x in m]
    ax.set_facecolor(SURF)
    ax.plot(t, rail, color=INK2, lw=1.2, ls=(0, (4, 3)))
    ax.fill_between(t, rail, p, where=[pp > rr for pp, rr in zip(p, rail)], color="#d9d8d3", lw=0)
    ax.plot(t, m, color=MAIN, lw=2, solid_capstyle="round", label="Main print height")
    ax.plot(t, p, color=PURGE, lw=2, solid_capstyle="round", label="Purge object height")
    ax.text(t[-1], m[-1] - 3, "main", color=INK, ha="right", va="top", fontsize=10)
    ax.text(t[-1], p[-1] + 1.5, "purge", color=INK, ha="right", va="bottom", fontsize=10)
    ax.text(t[len(t) // 3], rail[len(t) // 3] + 1.5, f"main + {ROD:g} mm (X rail)", color=INK2, fontsize=9)
    name = os.path.basename(f).split(".")[0]
    over = any(pp > rr for pp, rr in zip(p, rail))
    ax.set_title(f"{name}: purge object " + ("above the rail line (shaded): front/back gap keeps it safe"
                                     if over else "always under the rail line"),
                 color=INK, fontsize=10, loc="left")
    ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    for s in ("left", "bottom"): ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.set_xlabel("print progress, rough minutes (motion only)", color=INK2, fontsize=9)
axes[0].set_ylabel("height, mm", color=INK2, fontsize=9)
axes[0].legend(frameon=False, loc="upper left", fontsize=9, labelcolor=INK)
fig.tight_layout()
fig.savefig(out, dpi=110, facecolor=SURF)
