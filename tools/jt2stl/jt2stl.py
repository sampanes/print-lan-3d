"""jt2stl: JT 10.x tri-strip-set LOD -> binary STL.

Usage: python -I jt2stl.py IN.jt OUT.stl [--lod 7|9] [--covers]

  --lod     TOC segment type of the Shape LOD to read (7 = LOD0, the finest; 9 = coarser)
  --covers  also emit the encoder's "cover" polygons (dual vertices with group -1), which close
            the holes the topology coder needed closed; fan-triangulated

Mesh path ported from Open Cascade's TKJT (JT-Assistant, GPL-2.0-or-later, see COPYING) plus
the JT 10 changes from Siemens' public JT 10.6 format reference. Runs offline.
"""
import os, sys, struct, argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jtfile, jtmesh


def decode(path, lod_type=7, covers=False):
    j = jtfile.JtFile(path)
    segs = [t for t in j.toc if t[3] == lod_type]
    if not segs:
        raise SystemExit(f'no segment of type {lod_type}; TOC types: {sorted(set(t[3] for t in j.toc))}')
    tris_all, verts_all = [], []
    base = 0
    for _, off, _, _ in segs:
        _, size = struct.unpack_from(j.e + 'ii', j.data, off + 16)
        holder = {}
        orig = jtmesh.MeshDecoder.polygons

        def grab(self, holder=holder):
            holder['m'] = self.m
            return orig(self)
        jtmesh.MeshDecoder.polygons = grab
        try:
            coords, polys, left = jtmesh.read_tristrip_lod(j.data[off + 24:off + size], j.le, j.major)
        finally:
            jtmesh.MeshDecoder.polygons = orig
        if any(left):
            print(f'warning: segment @{off}: unconsumed symbols {left}')
        m = holder['m']
        tris = []
        for v in range(len(m.v_val)):
            if m.v_grp[v] < 0 and not covers:
                continue
            s = m.v_vfi[v]
            p = m.vf[s:s + m.v_val[v]]
            for k in range(1, len(p) - 1):
                tris.append((p[0], p[k], p[k + 1]))
        tris = np.array(tris, dtype=np.int64) + base
        base += len(coords)
        verts_all.append(coords)
        tris_all.append(tris)
    return j, np.vstack(verts_all), np.vstack(tris_all)


def write_stl(path, V, T):
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    rec = np.zeros(len(T), dtype=[('n', '<f4', 3), ('a', '<f4', 3), ('b', '<f4', 3), ('c', '<f4', 3), ('x', '<u2')])
    rec['n'], rec['a'], rec['b'], rec['c'] = n, a, b, c
    with open(path, 'wb') as f:
        f.write(b'jt2stl'.ljust(80, b' '))
        f.write(struct.pack('<I', len(T)))
        f.write(rec.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--lod', type=int, default=7)
    ap.add_argument('--covers', action='store_true')
    a = ap.parse_args()
    j, V, T = decode(a.src, a.lod, a.covers)
    write_stl(a.dst, V, T)
    print(f'{j.version}: {len(V)} vertices, {len(T)} triangles -> {a.dst}')
    print('bounds', V.min(0).round(3), V.max(0).round(3), 'size', (V.max(0) - V.min(0)).round(3))


if __name__ == '__main__':
    main()
