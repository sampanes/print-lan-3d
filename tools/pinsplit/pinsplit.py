"""pinsplit: cut a (possibly messy) triangle mesh into printable pieces joined by dowel pins.

Usage: python -I pinsplit.py CONFIG.json OUTDIR

The input mesh may be a CAD tessellation that is not a clean manifold: overlapping bodies,
seams between surface patches, mixed triangle orientation. It is rebuilt as a solid by
flood-filling the outside air on a voxel grid (see Body), with the exact distance to the
input triangles near the skin, then combined with exact signed-distance shapes (cut
half-spaces, pin holes) and extracted with marching cubes. Cut faces and pin holes come out
exact; the outer skin follows the input surface to within a small fraction of a voxel.

Config (JSON; lengths in output millimeters):
{
  "input": "model.stl",
  "scale": 2.0,                      # applied to the input first
  "voxel": 0.3,                      # fine step
  "coarse": 1.0,                     # coarse step for finding the outside air
  "pieces": {                        # name -> region (CSG of half-spaces)
    "wing-r": {"and": [["x", ">", 19], {"or": [["z", "<", 38], ["x", ">", 80]]}]},
    "body-r": {"and": [["x", ">", 0], {"not": "wing-r"}]}
  },
  "joints": [                        # dowels across a cut plane between two pieces
    {"axis": "x", "at": 0, "pieces": ["body-l", "body-r"], "d": 4, "depth": 6,
     "count": 4, "min_spacing": 30, "wall": 1.5, "clearance": 0.10}   # per-joint override
  ],
  "clearance": 0.15,                 # radial, default for every joint: hole = pin + clearance all round
  "pin_gap": 0.6                     # pin length = 2 * depth - pin_gap
}
A half-space is [axis, "<" or ">", value]. "and"/"or" take lists, "not" takes a region or a
piece name. Each piece is the solid intersected with its region. In a joint, "pieces" lists
the piece below the plane first. Dowel spots are picked automatically where the joint face
is thickest, at least min_spacing apart.
"""
import json, os, sys, math
import numpy as np
import trimesh
import igl
from scipy import ndimage
from skimage import measure

AX = {'x': 0, 'y': 1, 'z': 2}


def axes(lo, h, shape):
    return [lo[i] + h * np.arange(shape[i]) for i in range(3)]


# ---------------------------------------------------------------- body: a solid from a messy surface

class Body:
    """Inside/outside for a triangle soup with seams, overlapping shells and mixed orientation.

    Outside = air reachable from the grid border without crossing a "wall" of cells lying
    within about half a cell of any triangle. A coarse pass over the whole model finds that
    air; fine passes flood-fill from it, so seams narrower than a cell seal themselves and
    triangle orientation never matters. Near the surface the field is the exact distance to
    the triangles, so the rebuilt skin sits on the original surface, not on the voxel grid.
    """

    def __init__(self, V, F, hc):
        self.V = np.ascontiguousarray(V, dtype=np.float64)
        self.F = np.ascontiguousarray(F, dtype=np.int64)
        self.hc = hc
        lo = self.V.min(0) - 4 * hc
        hi = self.V.max(0) + 4 * hc
        shape = tuple(int(math.ceil((hi[i] - lo[i]) / hc)) + 1 for i in range(3))
        g = axes(lo, hc, shape)
        P = np.stack(np.meshgrid(*g, indexing='ij'), -1).reshape(-1, 3)
        d = self.udf(P)[0].reshape(shape).astype(np.float32)
        wall = d < 0.75 * hc
        lab, _ = ndimage.label(~wall)
        border = np.unique(np.concatenate([lab[0].ravel(), lab[-1].ravel(), lab[:, 0].ravel(),
                                           lab[:, -1].ravel(), lab[:, :, 0].ravel(), lab[:, :, -1].ravel()]))
        self.cext = np.isin(lab, border[border > 0])
        self.cd = d
        self.clo, self.cshape = lo, shape

    def udf(self, P):
        if len(P) == 0:
            return np.zeros(0), np.zeros((0, 3))
        out_d, out_c = [], []
        step = 2_000_000
        for k in range(0, len(P), step):
            d2, _, c = igl.point_mesh_squared_distance(np.ascontiguousarray(P[k:k + step]), self.V, self.F)
            out_d.append(np.sqrt(d2))
            out_c.append(c)
        return np.concatenate(out_d), np.concatenate(out_c)

    def coarse_index(self, g):
        return [np.clip(np.rint((g[i] - self.clo[i]) / self.hc).astype(np.int64), 0, self.cshape[i] - 1)
                for i in range(3)]

    def field(self, lo, hi, h):
        """Signed distance (+ inside, mm) on a fine grid covering [lo, hi]; returns (f, lo, shape)."""
        lo = np.asarray(lo, float)
        shape = tuple(int(math.ceil((hi[i] - lo[i]) / h)) + 1 for i in range(3))
        N = int(np.prod(shape))
        g = axes(lo, h, shape)
        ci = self.coarse_index(g)
        cd = self.cd[np.ix_(*ci)]
        cext = self.cext[np.ix_(*ci)]
        bidx = np.flatnonzero(cd < 0.9 * self.hc + 3 * h)
        ii = np.unravel_index(bidx, shape)
        P = np.stack([g[k][ii[k]] for k in range(3)], -1)
        del ii
        d, C = self.udf(P)
        dist = np.full(N, 3 * h, np.float32)
        dist[bidx] = np.minimum(d, 3 * h)
        wsel = d < 0.6 * h
        wall = np.zeros(N, bool)
        wall[bidx[wsel]] = True
        seeds = cext & (cd > 2 * self.hc)
        del cd, cext
        lab, _ = ndimage.label(~wall.reshape(shape))
        del wall
        keep = np.unique(lab[seeds])
        ext = np.isin(lab, keep[keep > 0]).ravel()
        del lab, seeds
        # wall cells: outside if a step away from the nearest surface point lands in air
        wi = bidx[wsel]
        Pw, Cw = P[wsel], C[wsel]
        del P, C
        v = Pw - Cw
        n = np.linalg.norm(v, axis=1)
        ok = n > 1e-9
        q = Pw[ok] + v[ok] / n[ok, None] * (1.5 * h)
        qi = [np.clip(np.rint((q[:, k] - lo[k]) / h).astype(np.int64), 0, shape[k] - 1) for k in range(3)]
        out = ext[np.ravel_multi_index(qi, shape)]
        ext[wi[ok][out]] = True
        f = np.where(ext, -dist, dist).reshape(shape)
        return f, lo, shape

    def solid_coarse(self):
        return self.clo, self.hc, ~self.cext


# ---------------------------------------------------------------- CSG regions (exact SDF)

def region_field(reg, P, pieces):
    """Signed distance (+ inside) of a region at points P (dict of axis -> broadcast arrays)."""
    if isinstance(reg, str):
        return region_field(pieces[reg], P, pieces)
    if isinstance(reg, list):
        ax, op, val = reg
        d = P[ax] - val
        return d if op == '>' else -d
    if 'and' in reg:
        return _fold(np.minimum, [region_field(r, P, pieces) for r in reg['and']])
    if 'or' in reg:
        return _fold(np.maximum, [region_field(r, P, pieces) for r in reg['or']])
    if 'not' in reg:
        return -region_field(reg['not'], P, pieces)
    raise ValueError(reg)


def _fold(fn, xs):
    out = xs[0]
    for x in xs[1:]:
        out = fn(out, x)
    return out


def hole_field(P, axis, at, center, r, depth, chamfer=0.4):
    """+ outside a blind hole of radius r reaching depth on both sides of the plane axis=at,
    with a lead-in chamfer where the pin enters."""
    o = [k for k in 'xyz' if k != axis]
    rad = np.sqrt((P[o[0]] - center[0]) ** 2 + (P[o[1]] - center[1]) ** 2)
    t = np.abs(P[axis] - at)
    rr = r + np.maximum(0.0, chamfer - t)
    return np.maximum(rad - rr, t - depth)


# ---------------------------------------------------------------- pin placement

def place_pins(body, joint, pieces, h):
    axis, at = joint['axis'], joint['at']
    a = AX[axis]
    o = [i for i in range(3) if i != a]
    lo = body.V.min(0) - 2 * h
    hi = body.V.max(0) + 2 * h
    lo[a], hi[a] = at - 2 * h, at + 2 * h
    f, lo, shape = body.field(lo, hi, h)
    sec = np.take(f, shape[a] // 2, axis=a)
    g = axes(lo, h, shape)
    P = {}
    gi = np.meshgrid(g[o[0]], g[o[1]], indexing='ij')
    P['xyz'[o[0]]], P['xyz'[o[1]]] = gi
    eps = 1.5 * h
    pa, pb = joint['pieces']
    P[axis] = np.full_like(gi[0], at - eps)
    ra = region_field(pieces[pa], P, pieces)
    P[axis] = np.full_like(gi[0], at + eps)
    rb = region_field(pieces[pb], P, pieces)
    # the joint face: solid on the plane, piece a just before it and piece b just after it
    face = (sec > 0) & (ra > 0) & (rb > 0)
    dist = ndimage.distance_transform_edt(face) * h
    need = joint['d'] / 2 + joint.get('clearance', 0.15) + joint.get('wall', 1.5)
    cand = np.argwhere(dist >= need)
    if len(cand) == 0:
        raise SystemExit('joint %s/%s: no spot thick enough (max inscribed radius %.2f mm)'
                         % (pa, pb, dist.max()))
    order = np.argsort(-dist[tuple(cand.T)])
    sp = joint.get('min_spacing', 10)
    pts = []
    for k in order:
        c = cand[k]
        p = (float(g[o[0]][c[0]]), float(g[o[1]][c[1]]))
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) >= sp for q in pts):
            pts.append(p)
        if len(pts) >= joint['count']:
            break
    return pts, float(dist.max())


# ---------------------------------------------------------------- pieces

def build_piece(name, body, cfg, joints, h):
    pieces = cfg['pieces']
    clo, ch, solid = body.solid_coarse()
    g = axes(clo, ch, solid.shape)
    P = dict(zip('xyz', np.meshgrid(*g, indexing='ij', sparse=True)))
    m = (solid | (body.cd < ch)) & (region_field(pieces[name], P, pieces) > -ch)
    idx = np.argwhere(m)
    if len(idx) == 0:
        raise SystemExit('piece %s is empty' % name)
    plo = np.array([g[i][idx[:, i].min()] for i in range(3)]) - 2 * ch
    phi = np.array([g[i][idx[:, i].max()] for i in range(3)]) + 2 * ch
    # an irrational offset keeps exact planes (cuts, hole floors) off the grid nodes,
    # where marching cubes would see exact zeros and leave non-manifold edges
    f, plo, shape = body.field(plo + 0.1234567 * h, phi, h)
    g = axes(plo, h, shape)
    P = dict(zip('xyz', np.meshgrid(*g, indexing='ij', sparse=True)))
    f = np.minimum(f, region_field(pieces[name], P, pieces).astype(np.float32))
    for j in joints:
        if name not in j['pieces']:
            continue
        for c in j['pins']:
            f = np.minimum(f, hole_field(P, j['axis'], j['at'], c, j['d'] / 2 + j.get('clearance', cfg.get('clearance', 0.15)),
                                         j['depth']).astype(np.float32))
    f[f == 0] = -1e-6
    f = np.pad(f, 1, constant_values=-1.0)
    verts, faces, _, _ = measure.marching_cubes(f, 0.0, spacing=(h, h, h))
    del f
    verts += plo - h
    mesh = cleanup(trimesh.Trimesh(verts, faces, process=True))
    keep = cfg.get('decimate')
    if keep:
        try:
            import fast_simplification
        except ImportError:
            print('decimate: fast_simplification not installed, keeping full resolution')
        else:
            # decimation can occasionally pinch an edge; try nearby targets, else keep full res
            for k in (keep, keep * 1.1, keep * 1.25, keep * 1.5):
                if k >= 1:
                    break
                v, fc = fast_simplification.simplify(mesh.vertices, mesh.faces, target_reduction=1 - k)
                dm = cleanup(trimesh.Trimesh(v, fc, process=True))
                if dm.is_watertight and len(dm.split(only_watertight=False)) == 1:
                    return dm
            print('decimate: %s stays at full resolution (decimated mesh was not watertight)' % name)
    return mesh


def cleanup(mesh):
    """Drop folded duplicate faces (both copies) and zero-volume specks; orient outward."""
    key = np.sort(mesh.faces, 1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    mesh.update_faces(cnt[inv.ravel()] == 1)
    mesh.remove_unreferenced_vertices()
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        big = max(abs(p.volume) for p in parts)
        mesh = trimesh.util.concatenate([p for p in parts if abs(p.volume) > 1e-3 * big])
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def make_pin(d, length, chamfer=0.4):
    """Dowel along z, centered at the origin, chamfered both ends."""
    r = d / 2
    pts = [(0, -length / 2), (r - chamfer, -length / 2), (r, -length / 2 + chamfer),
           (r, length / 2 - chamfer), (r - chamfer, length / 2), (0, length / 2)]
    return trimesh.creation.revolve(np.array(pts), sections=48)


def main():
    cfg_path, out = sys.argv[1], sys.argv[2]
    cfg = json.load(open(cfg_path))
    os.makedirs(out, exist_ok=True)
    src = cfg['input']
    if not os.path.isabs(src):
        src = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), src)
    mesh = trimesh.load(src, process=False)
    V = np.asarray(mesh.vertices, float) * cfg.get('scale', 1.0)
    F = np.asarray(mesh.faces)
    h = cfg.get('voxel', 0.3)
    body = Body(V, F, cfg.get('coarse', 1.0))
    joints = []
    for j in cfg.get('joints', []):
        j = dict(j, clearance=j.get('clearance', cfg.get('clearance', 0.15)))
        pts, rmax = place_pins(body, j, cfg['pieces'], h)
        j = dict(j, pins=pts)
        joints.append(j)
        print('joint %s @%s=%g: %d pins d%g (max inscribed r %.1f mm) at %s'
              % ('/'.join(j['pieces']), j['axis'], j['at'], len(pts), j['d'], rmax,
                 [(round(p, 1), round(q, 1)) for p, q in pts]))
    only = cfg.get('only')
    report = {'pieces': {}, 'joints': joints}
    for name in cfg['pieces']:
        if name.startswith('_') or (only and name not in only):
            continue
        pm = build_piece(name, body, cfg, joints, h)
        pm.export(os.path.join(out, name + '.stl'))
        report['pieces'][name] = {'faces': len(pm.faces), 'watertight': bool(pm.is_watertight),
                                  'volume_cm3': round(pm.volume / 1000, 2),
                                  'size_mm': [round(float(v), 1) for v in pm.extents]}
        print(name, report['pieces'][name])
    gap = cfg.get('pin_gap', 0.6)
    pins = {}
    for j in joints:
        L = 2 * j['depth'] - gap
        tag = 'pin-d%gx%g' % (j['d'], L)
        make_pin(j['d'], L).export(os.path.join(out, tag + '.stl'))
        pins[tag] = pins.get(tag, 0) + len(j['pins'])
    report['pins'] = pins
    json.dump(report, open(os.path.join(out, 'pinsplit-report.json'), 'w'), indent=1)
    print('pins:', pins)


if __name__ == '__main__':
    main()
