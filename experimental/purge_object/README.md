# Purge object at its own Z (experimental)

Replace the AMS chute flush with printing a separate "purge object" (a Benchy, a cube,
anything single-colour) elsewhere on the bed. At each colour change the old colour is flushed
into the next few layers of the purge object instead of down the chute. The purge object is
printed in whole-layer chunks at ITS OWN height: the bed moves up and down between the two
objects.

Status:
- P1S: works. One TIGHT print (two colour changes, Benchy as the purge object): no
  collisions, both objects finished, the main part dimensionally fine.
- H2S: machine profile written from Bambu Studio's H2S profiles, never printed. See
  "H2S notes".

Not zero waste: with flush 0 the P1S still pushes about 2 mm of filament into the chute per
change, and the cutter still cuts. Everything else goes into the purge object.

## Quick start (cmd)

```
cd print-lan\experimental\purge_object
python run.py path\to\project.3mf path\to\purge.stl --name myjob
```

- `project.3mf`: a Bambu Studio project with the main print and its colour changes set
  (Preview > layer slider > add a filament change). Its printer decides the machine profile
  (`--machine` overrides).
- Needs Python 3.11 with numpy and trimesh, and Bambu Studio installed (`BAMBU_STUDIO_DIR`
  if not in Program Files).
- Output in `print-lan\jobs\purge_myjob\`: `myjob.gcode.3mf` plus every intermediate file.
  Nothing is sent to the printer.
- Options: `--plan tight|max` (default tight), `--lead 25`, `--main-xy X,Y`,
  `--purge-xy X,Y` (footprint centres, to override the automatic placement).

## Pipeline (what run.py does)

1. `make3mf.py SRC.3mf PURGE.stl OUTDIR [--machine NAME]` - writes `main.3mf` (all objects
   moved together to the back centre, prime tower off, flush_volumes_matrix all 0),
   `purge.3mf` (same settings, the purge mesh in the machine's purge corner, clear of its
   keep-outs, no colour changes) and `job.json` (machine, the flush matrix the project
   wanted, footprints).
2. Slice each with the Bambu Studio CLI using the project's embedded settings, no profile
   flattening: `bambu-studio.exe --slice 0 --outputdir DIR --export-3mf X.gcode.3mf IN.3mf`.
   The CLI process can linger after writing; poll for `result.json`.
3. `splice.py MAIN.gcode PURGE.gcode OUT.gcode --plan tight --job job.json` - flush volume per
   change is looked up from the matrix (from filament -> to filament, read from the G-code's
   M620 lines); `--flush V1,V2` overrides.
4. `check.py OUT.gcode` - collision check with the machine's clearances; must print
   `[OK] no collisions found`.
5. `when.py MAIN.gcode PURGE.gcode OUT.gcode.plan.json` - wall-clock of each purge visit.
6. `pack.py MAIN.gcode.3mf OUT.gcode PACKED.gcode.3mf EXTRA_SECONDS` - swaps the G-code, its
   MD5, time and grams into the main slice's 3mf.
7. Upload to the SD card and start it from the printer screen (or Studio's Device tab, SD
   list; untested). Do NOT open it in Studio's Prepare view: Studio re-slices and drops the
   splice.

`timeline.py chart.png a.timeline.json ...` draws height-over-time from `check.py --timeline`.

## Machine profiles (`machines/*.json`)

One file per printer model: bed size, z max, toolhead clearance radius, rod height, keep-out
rectangles (start purge line, excluded bed areas), purge corner, margin, and whether the
filament change happens in firmware (`hidden_change_moves`). To add a printer, copy one and
take the values from Bambu Studio's `resources\profiles\BBL\machine\<printer> 0.4 nozzle*.json`
(printable_area, printable_height, extruder_clearance_*) and its start / change G-code
templates. Keep `status` UNTESTED until it has printed once.

## Plans

- TIGHT (default): each change gets just enough purge layers for its flush volume; the last
  change goes up to main + lead (25 mm); a same-colour catch-up visit finishes the purge
  object once the main print is within the lead. Never relies on the Y gap.
- MAX: change 1 -> purge object printed up to the main item's next change height; change 2
  -> purge object finished. It can end up >34 mm above the nozzle while the main item is low;
  safe on the P1S only because the X rail never comes within reach of its Y range. Never
  printed.

## Facts that cost time (do not re-derive)

- P1S filament change: lifts to max_layer_z+3, flushes at the back chute (X 60-165,
  Y 255-265), CUTS at the FRONT-LEFT (G1 X20 Y50 then Y-3). End G-code visits both at
  max_layer_z+0.5. So: purge object front-RIGHT, main item at the back.
- P1S start purge line runs along the front edge X 18-240, Y 1, with a hook at X 239.5-240
  up to Y 11. Keep the purge object above Y ~15 there.
- With flush volume 0 the P1S change block still pushes 2 mm into the chute and leaves the
  filament retracted 2 mm; Studio then primes `G1 E2` at the next object point. splice.py
  folds that into the purge object's own first prime the same way.
- Clean chunk boundaries: each layer's `; layer num/total_layer_count` comment comes AFTER
  the layer-change wipe/retract, so cut there: the nozzle is retracted 0.8 at the wipe end.
  Returning to that exact XY/Z/retraction state makes the main print resume as if nothing
  happened. After-change visits anchor right after `M621 S<n>A`.
- check.py treats the toolhead as a disc of extruder_clearance_max_radius and the gantry as
  that deep in Y (conservative).
- Do not run these scripts with `python -I`: it drops the script dir from sys.path and a
  helper named `gc.py` silently loads the builtin gc module instead (hence gcmodel.py).

## Tray mapping

Started from the printer screen (SD file list), the P1S maps filament N straight to AMS tray
N - it does NOT match by colour. Before starting, either load the spools in the project's
filament order (filament 1 -> tray 1 ...) or pad the project's filament list so each colour
sits at the tray number it is loaded in, and re-slice. Check within the first minutes:
`ams.tray_now` (0-based) from a status pushall must be the tray of the first layer's colour;
it reads 255 during heating and leveling.

## Printer behaviour during a run

- Printer progress and countdown are wrong during purge visits (they only know the main
  print). `when.py` gives the real timing.
- Raw MQTT pause is refused on P1S firmware 01.09 (err_code 84033543, unsigned); pause from
  the printer screen, Bambu Handy, or Studio's Device tab.
- Fresh plastic edges glow orange in the chamber LED light: do not judge colour from a camera
  photo of the first few layers.

## H2S notes (from Studio's H2S 0.4 profiles; nothing printed on an H2S yet)

- Bed 340x320, height 340. extruder_clearance_max_radius 81 (P1S 68), height_to_rod 33.
- Start nozzle load line at the front RIGHT: X 250-290, Y -0.5.
- Filament change: `M620 S[n]A`, lift `G1 Z{max_layer_z + 3.0}`, then the cut and flush are
  done by FIRMWARE (`M620.10 ... L[flush_length]`, `T[n]`, `SYNC`), not by visible G-code
  moves, so check.py cannot see them. `hidden_change_moves: true` makes splice.py raise every
  Z move inside change blocks and the end G-code to the tallest printed object + 3 mm.
  `M621 S[n]A` still ends the block, so the splice anchor is the same.
- Bed leveling line is `G29 A1 O X.. Y.. I.. J..` (P1S: `G29 A X..`); splice.py handles both.
- To find out on the first H2S run: whether firmware honours flush_length 0 (it may enforce a
  minimum), and whether the slice's start/end G-code matches what these notes expect (read
  the sliced main G-code's change block before printing).
