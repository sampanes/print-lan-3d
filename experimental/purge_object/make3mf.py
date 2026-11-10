"""Build the two input projects for a purge-object print from any Bambu Studio project 3mf.

The source project holds the main print with its colour changes already set (layer tool
changes). Writes into OUTDIR:
  main.3mf   - every object shifted together to the back of the bed, prime tower off,
               chute flush volumes 0, colour changes kept
  purge.3mf  - same settings, one object whose mesh is the purge STL, in the machine's purge
               corner, no colour changes
  job.json   - machine, flush volumes Studio wanted (for splice.py), where things went

Usage: python make3mf.py SRC.3mf PURGE.stl OUTDIR [--machine p1s] [--main-xy X,Y]
       [--purge-xy X,Y] [--purge-scale A[,B]]
--main-xy / --purge-xy put that object's footprint centre at X,Y instead of the automatic spot.
--purge-scale scales the purge mesh by A in X and Y and by B (default A) in Z; fit.py picks it.
"""
import argparse, json, os, re, sys, zipfile
import numpy as np
import trimesh
import machine

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("purge_stl")
ap.add_argument("outdir")
ap.add_argument("--machine", help="machines/<name>.json (default: from the project's printer)")
ap.add_argument("--main-xy")
ap.add_argument("--purge-xy")
ap.add_argument("--purge-scale", default="1", help="A or A,B: XY and Z scale of the purge mesh")
a = ap.parse_args()
SCALE = [float(v) for v in (a.purge_scale.split(",") * 2)[:2]]
if not all(0.1 <= v <= 10 for v in SCALE):
    sys.exit(f"[X] --purge-scale {a.purge_scale}: out of range")
os.makedirs(a.outdir, exist_ok=True)

zin = zipfile.ZipFile(a.src)
files = {n: zin.read(n) for n in zin.namelist()}
proj = json.loads(files["Metadata/project_settings.config"])
mid = a.machine or machine.guess(proj.get("printer_model", ""))
if not mid:
    sys.exit(f"[X] no machine profile for printer '{proj.get('printer_model')}'; pass --machine")
M = machine.load(mid)
BX, BY = M["bed"]
area = proj.get("printable_area", [])
if area and area[2] != f"{BX}x{BY}":
    print(f"[!] project bed {area[2]} != machine profile bed {BX}x{BY}", file=sys.stderr)
if mid != machine.guess(proj.get("printer_model", "")):
    print(f"[!] project is for '{proj.get('printer_model')}', machine profile is {M['name']}",
          file=sys.stderr)


# ---- 3mf geometry ----------------------------------------------------------------------
def mat(s):
    """3MF transform string (12 numbers, row-vector convention) -> 4x4 matrix."""
    v = [float(t) for t in s.split()]
    m = np.eye(4)
    m[:3, :3] = np.array(v[:9]).reshape(3, 3)
    m[3, :3] = v[9:12]
    return m


def obj_block(xml, oid):
    m = re.search(rf'<object id="{oid}"[^>]*>.*?</object>', xml, re.S)
    if not m:
        sys.exit(f"[X] object {oid} not found")
    return m[0]


def mesh_verts(block):
    v = re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', block)
    return np.array(v, dtype=float)


root_name = "3D/3dmodel.model"
root = files[root_name].decode()
items = re.findall(r'<item objectid="(\d+)"[^>]*?transform="([^"]*)"', root)
if not items:
    sys.exit("[X] no build items with transforms in the project")


def components(oid):
    """[(file, objectid, transform matrix)] for an object in the root model (or itself)."""
    blk = obj_block(root, oid)
    comps = re.findall(r'<component p:path="([^"]+)" objectid="(\d+)"[^>]*?transform="([^"]*)"', blk)
    if comps:
        return [(p.lstrip("/"), c, mat(t)) for p, c, t in comps]
    return [(root_name, oid, np.eye(4))]


def item_points(oid, tr):
    pts = []
    for path, cid, ct in components(oid):
        v = mesh_verts(obj_block(files[path].decode(), cid))
        h = np.c_[v, np.ones(len(v))] @ ct @ mat(tr)
        pts.append(h[:, :3])
    return np.vstack(pts)


allp = np.vstack([item_points(o, t) for o, t in items])
mlo, mhi = allp.min(0), allp.max(0)
mw, md = mhi[0] - mlo[0], mhi[1] - mlo[1]

pm = trimesh.load(a.purge_stl, force="mesh")
pm.merge_vertices()
pm.apply_scale([SCALE[0], SCALE[0], SCALE[1]])
pm.apply_translation(-(pm.bounds[0] + pm.bounds[1]) / 2)
pw, pd, ph = pm.extents


# ---- placement ---------------------------------------------------------------------------
def hits(r, rects, gap=0.0):
    return any(r[0] < k[2] + gap and k[0] - gap < r[2] and r[1] < k[3] + gap and k[1] - gap < r[3]
               for k in rects)


mg = M["margin"]
if a.main_xy:
    cx, cy = (float(t) for t in a.main_xy.split(","))
else:
    cx, cy = BX / 2, BY - mg - md / 2                     # back centre
main_r = (cx - mw / 2, cy - md / 2, cx + mw / 2, cy + md / 2)

if a.purge_xy:
    px, py = (float(t) for t in a.purge_xy.split(","))
else:
    right = M["purge_corner"].endswith("right")
    px = BX - mg - pw / 2 if right else mg + pw / 2
    py = mg + pd / 2
    while hits((px - pw / 2, py - pd / 2, px + pw / 2, py + pd / 2), M["keepout"], mg) and py < BY:
        py += 0.5
purge_r = (px - pw / 2, py - pd / 2, px + pw / 2, py + pd / 2)

for name, r in (("main", main_r), ("purge", purge_r)):
    if r[0] < 0 or r[1] < 0 or r[2] > BX or r[3] > BY:
        sys.exit(f"[X] {name} footprint {np.round(r, 1).tolist()} is off the {BX}x{BY} bed")
    if hits(r, M["keepout"]):
        sys.exit(f"[X] {name} footprint {np.round(r, 1).tolist()} overlaps a keep-out "
                 f"({M['keepout_why']})")
if hits(main_r, [purge_r], mg):
    sys.exit(f"[X] main ({mw:.0f}x{md:.0f} mm) and purge ({pw:.0f}x{pd:.0f} mm) do not fit apart "
             f"on the {BX}x{BY} bed; use a smaller purge object or --main-xy/--purge-xy")
dx, dy = cx - (mlo[0] + mhi[0]) / 2, cy - (mlo[1] + mhi[1]) / 2


# ---- writing -------------------------------------------------------------------------------
def settings():
    d = dict(proj)
    d["enable_prime_tower"] = "0"
    d["flush_volumes_matrix"] = ["0"] * len(proj.get("flush_volumes_matrix", []))
    return json.dumps(d, indent=4).encode()


def write(name, overrides):
    with zipfile.ZipFile(os.path.join(a.outdir, name), "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            if n in overrides:
                data = overrides[n]
                if data is None:
                    continue
            z.writestr(n, data)


def num(x):
    return f"{x:.6f}".rstrip("0").rstrip(".")


def shift(m):
    """Move a build item by (dx, dy); every other number is kept as written."""
    v = m[2].split()
    v[9], v[10] = num(float(v[9]) + dx), num(float(v[10]) + dy)
    return m[1] + " ".join(v) + '"'


main_root = re.sub(r'(<item objectid="\d+"[^>]*?transform=")([^"]*)"', shift, root)
write("main.3mf", {root_name: main_root.encode(),
                   "Metadata/project_settings.config": settings()})

# purge: the first build item carries the purge mesh; everything else is removed
oid = items[0][0]
comps = components(oid)
if len(comps) != 1:
    sys.exit(f"[X] the first object has {len(comps)} parts; the purge copy needs a single-part "
             "first object (reorder the objects in Studio)")
cpath, cid, _ = comps[0]
others = [o for o, _ in items if o != oid]
vx = "\n".join(f'     <vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in pm.vertices)
tr = "\n".join(f'     <triangle v1="{i}" v2="{j}" v3="{k}"/>' for i, j, k in pm.faces)
mesh = f"<mesh>\n    <vertices>\n{vx}\n    </vertices>\n    <triangles>\n{tr}\n    </triangles>\n   </mesh>"
cfile = files[cpath].decode()
cblk = obj_block(cfile, cid)
cfile = cfile.replace(cblk, re.sub(r"<mesh>.*</mesh>", lambda _: mesh, cblk, flags=re.S))
if cpath == root_name:
    root_with_mesh = cfile
else:
    root_with_mesh = root
drop = {p: None for o in others for p, _, _ in components(o) if p not in (cpath, root_name)}

proot = root_with_mesh
for o in others:
    proot = proot.replace(obj_block(proot, o), "")
proot = re.sub(r'\s*<item objectid="(?!' + oid + r'")\d+"[^>]*/>', "", proot)
proot = re.sub(rf'(<item objectid="{oid}"[^>]*?transform=")[^"]*"',
               lambda m: m[1] + f'1 0 0 0 1 0 0 0 1 {px:.4f} {py:.4f} {ph / 2:.6f}"', proot)
proot = re.sub(rf'(<object id="{oid}"[^>]*>\s*<components>\s*<component [^>]*?transform=")[^"]*"',
               lambda m: m[1] + '1 0 0 0 1 0 0 0 1 0 0 0"', proot)
rels_name = "3D/_rels/3dmodel.model.rels"
rels = files.get(rels_name, b"").decode()
for p in drop:
    rels = re.sub(rf'\s*<Relationship Target="/{re.escape(p)}"[^>]*/>', "", rels)

cfg = files["Metadata/model_settings.config"].decode()
for o in others:
    cfg = re.sub(rf'\s*<object id="{o}">.*?</object>', "", cfg, flags=re.S)
    cfg = re.sub(rf'\s*<model_instance>\s*<metadata key="object_id" value="{o}"/>.*?</model_instance>',
                 "", cfg, flags=re.S)
    cfg = re.sub(rf'\s*<assemble_item object_id="{o}"[^>]*/>', "", cfg)
pname = os.path.basename(a.purge_stl)
oblk = re.search(rf'<object id="{oid}">.*?</object>', cfg, re.S)[0]
nblk = re.sub(r'(key="name" value=")[^"]*"', lambda m: m[1] + pname + '"', oblk)
nblk = re.sub(r'face_count="\d+"', f'face_count="{len(pm.faces)}"', nblk)
nblk = re.sub(r'(key="source_offset_z" value=")[^"]*"', lambda m: m[1] + f'{ph / 2:.6f}"', nblk)
cfg = cfg.replace(oblk, nblk)
cfg = re.sub(rf'(<assemble_item object_id="{oid}"[^>]*?transform=")[^"]*"',
             lambda m: m[1] + f'1 0 0 0 1 0 0 0 1 0 0 {ph / 2:.6f}"', cfg)

over = dict(drop)
over[root_name] = proot.encode()
if cpath != root_name:
    over[cpath] = cfile.encode()
if rels:
    over[rels_name] = rels.encode()
over["Metadata/model_settings.config"] = cfg.encode()
over["Metadata/custom_gcode_per_layer.xml"] = None
over["Metadata/project_settings.config"] = settings()
write("purge.3mf", over)

fm = [float(v) for v in proj.get("flush_volumes_matrix", [])]
n = int(round(len(fm) ** 0.5))
mult = float((proj.get("flush_multiplier") or ["1"])[0])
job = dict(machine=mid, source=os.path.basename(a.src), purge=pname,
           flush_matrix=[[fm[i * n + j] * mult for j in range(n)] for i in range(n)],
           filament_colour=proj.get("filament_colour"),
           main_rect=[round(v, 2) for v in main_r], purge_rect=[round(v, 2) for v in purge_r],
           purge_height=round(float(ph), 2), purge_scale=SCALE)
json.dump(job, open(os.path.join(a.outdir, "job.json"), "w"), indent=1)
print(f"[OK] machine {mid} ({M['status']})")
print(f"[OK] main  {mw:.1f} x {md:.1f} mm -> X {main_r[0]:.1f}-{main_r[2]:.1f}, "
      f"Y {main_r[1]:.1f}-{main_r[3]:.1f}")
print(f"[OK] purge {pw:.1f} x {pd:.1f} x {ph:.1f} mm (scale {SCALE[0]:g},{SCALE[1]:g}), {len(pm.faces)} faces, watertight "
      f"{pm.is_watertight} -> X {purge_r[0]:.1f}-{purge_r[2]:.1f}, Y {purge_r[1]:.1f}-{purge_r[3]:.1f}")
