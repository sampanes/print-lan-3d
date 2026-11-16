# pinsplit

Cut a model into printable pieces that join with printed dowel pins, from a mesh that may
not be a clean solid (CAD tessellations with seams between surface patches, overlapping
bodies, mixed triangle orientation).

```
python -I pinsplit.py CONFIG.json OUTDIR
```

Writes one STL per piece, one STL per dowel size, and `pinsplit-report.json` (piece sizes,
volumes, watertight check, pin positions and counts). The config format is in the
docstring of `pinsplit.py`. Needs numpy, scipy, trimesh, scikit-image and libigl
(`pip install scikit-image libigl`); `fast-simplification` enables `"decimate"`.

## How it works
1. Solid: the outside air is flood-filled on a coarse grid from the border, through cells
   that are not within half a cell of a triangle. Seams narrower than a cell seal
   themselves and triangle orientation never matters. A fine grid per piece floods from
   that air and takes the exact distance to the triangles near the skin, so the rebuilt
   surface sits on the input surface, not on the voxels.
2. Pieces: each piece is the solid intersected with a region built from half-spaces
   (`and` / `or` / `not`, or another piece's name). Regions are exact signed distances, so
   cut faces are flat and exact.
3. Joints: for each cut plane, dowel spots are chosen where the shared face is thickest
   (at least `d/2 + clearance + wall` from its edge), `min_spacing` apart. Each spot gets a
   blind hole on both sides (pin + clearance all round, 0.4 mm lead-in).
4. Surface: marching cubes, folded duplicate faces and specks removed, optional decimation.

## Tips
- Dowels print standing on end. Hole clearance 0.15 mm all round is a starting point:
  print a fit coupon on the target printer first.
- Pick cut planes where the model is thick (for a plane: a body split down its symmetry
  plane prints flat on the cut face). A wing joined edge-on is usually too thin for more
  than one pin; a horizontal cut under a whole wing gives room for several.
- Memory: the fine grid is per piece; 0.3 mm on a 300 mm part uses about 1-2 GB.
