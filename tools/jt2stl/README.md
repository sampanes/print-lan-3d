# jt2stl

JT (Siemens visualization format, ISO 14306) to binary STL, for printing parts that only
exist as JT. Reads the tessellated Tri-Strip Set LOD of JT 10.x files (tested on JT 10.6
written by JT DM 10.8). JT 9.5 and older use different packet codecs and are not
supported (Open Cascade's JT-Assistant reads those). Offline; numpy only.

```
python -I jt2stl.py IN.jt OUT.stl              # finest LOD (TOC segment type 7)
python -I jt2stl.py IN.jt OUT.stl --lod 9      # a coarser LOD
python -I jt2stl.py IN.jt OUT.stl --covers     # also emit the coder's hole-closing cover polygons
```

The output is the CAD tessellation as-is: usually one surface patch per B-rep face, with
seams between patches and mixed triangle orientation. Fine to view; for slicing, rebuild a
solid with `../pinsplit` (which also cuts it into pinned pieces).

## Where it comes from
- The mesh path is a Python port of Open Cascade's TKJT reader (JT-Assistant,
  Copyright (C) 2014-2015 OPEN CASCADE SAS), so `jtmesh.py` is GPL-2.0-or-later; see COPYING.
  TKJT covers JT 8 to 9.5.
- The JT 10 changes follow Siemens' public "JT V10.6 Format Description" (free from the
  JT Open Program page), with the gaps below found by decoding real files and checking
  every packet's bit count.

## JT 10.x differences from 9.5 (as implemented)
- Header: TOC offset is a u64 at byte 85; TOC entries are 32 bytes (GUID, u64 offset,
  u32 length, u32 attributes; segment type = attributes >> 24).
- Whole-segment compression (LSG, metadata): flag 3 / algorithm 3 is a plain .xz stream.
  Shape LOD segments are not compressed as a whole.
- Tri-Strip Set LOD element: U8 base-shape version, U8 vertex-shape version, U64 bindings,
  then a nested element header the spec does not show (I32 length, GUID
  f830a5ad-be4c-4fbc-9b5f-b9269278d2e1, U8 base type 9, I32 object id), then U8 version,
  U32 vertex-records id, U8 version, then the topology packets.
- Topology packets: face degrees x8, valences, groups, flags (Lag1), attribute masks x8,
  ONE "mask 8 MSBs" packet (9.5 had two), the high-degree masks as a plain VecU32,
  split faces (Lag1), split positions, U32 hash.
- Int32 packet codecs: 0 null, 1 bitlength, 3 arithmetic, 4 chopper, 5 move-to-front.
  - Bitlength: 1 flag bit; fixed width = nibbler(min), nibbler(max) (4-bit nibbles, each
    followed by a more-bit, sign-extended), width = bit length of max - min; variable width =
    nibbler(mean), 4-bit signed width changes (repeat on -8 or 7), 4-bit run lengths. The
    bits used match the packet's CodeText Length exactly, which makes a good self-check.
  - Arithmetic: probability table = U16 count, U6 occurrence bits, U7 value bits, I32 min,
    then per entry 1 escape bit, occurrence, value - min; byte-aligned after. An out-of-band
    packet follows only if some entry is the escape. CodeText Length 0 = all values out of band.
  - Move-to-front: a packet of values, then a packet of offsets; offset -1 takes the next
    value, otherwise it indexes a 16-entry most-recent window (move to front).
- Lossless vertex coordinates (quantization bits 0): one packet per component holding the
  IEEE float bits through a Lag1 predictor (9.5 used exponent/mantissa pairs).
- In the decoded dual mesh, dual vertices are the polygons. Group -1 marks cover polygons
  the encoder added to close holes; they are interior and skipped unless `--covers`.

Not read: normals, colors, texture coordinates, PMI, the B-rep segments, LSG transforms
(a multi-part assembly comes out in each part's own coordinates).
