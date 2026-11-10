# Purge object at its own Z (experimental)

Replace the AMS chute flush with printing a separate "purge object" (a Benchy, a cube,
anything single-colour) elsewhere on the bed. At each colour change the old colour is flushed
into the next few layers of the purge object instead of down the chute. The purge object is
printed in whole-layer chunks at ITS OWN height: the bed moves up and down between the two
objects.

Status:
- P1S: works. One TIGHT print (two colour changes, Benchy as the purge object): no
  collisions, both objects finished, the main part dimensionally fine.
- H2S: works. One TIGHT print sliced with OrcaSlicer (18 colour changes, two Benchies as the
  purge object, --lead 2): no collisions, both objects finished, colours clean on the main part.
  See "H2S notes".

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
  `--purge-xy X,Y` (footprint centres, to override the automatic placement),
  `--purge-scale auto|A|A,B` (size the purge object to the job; see "Sizing the purge object").

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
   It exits 3 when the purge object runs out before the last change (too small).
4. `check.py OUT.gcode` - collision check with the machine's clearances; must print
   `[OK] no collisions found`.
5. `when.py MAIN.gcode PURGE.gcode OUT.gcode.plan.json` - wall-clock of each purge visit.
6. `pack.py MAIN.gcode.3mf OUT.gcode PACKED.gcode.3mf EXTRA_SECONDS` - swaps the G-code, its
   MD5, time and grams into the main slice's 3mf.
7. Upload to the SD card and start it from the printer screen (or Studio's Device tab, SD
   list; untested). Do NOT open it in Studio's Prepare view: Studio re-slices and drops the
   splice.

`timeline.py chart.png a.timeline.json ...` draws height-over-time from `check.py --timeline`.

## Sizing the purge object (`fit.py`, `--purge-scale`)

The whole purge object is printed whatever the job flushes, so a fixed one is either too small
(splice.py exits 3) or wasted filament. Scale ONE object to the job instead of printing extra
copies: `run.py ... --purge-scale auto` slices it at scale 1, asks `fit.py` for the smallest
scale that takes every flush, rebuilds and re-slices it, and if splice.py still finds it short,
fits again on the real slice with 5 % more margin (up to 3 tries). `--purge-scale 1.2` or
`1.3,1.0` (XY, Z) scales by hand.

`python fit.py MAIN.gcode PURGE.gcode --job job.json --lead L [--at A,B]` on its own works with
any slicer's output (Bambu Studio, OrcaSlicer); `--at` is the scale PURGE.gcode was sliced at.
The last line it prints is the scale.

- What must fit (tight plan): each change's flush, on top of all earlier ones, in purge layers
  no higher than the main print + lead at that change; and the purge object's top no higher
  than the main print's last-but-one layer + lead, so the catch-up visit can finish it.
- Model: per layer, wall-like extrusion (outer, inner and overhang walls, gap infill, from the
  slicer's `; FEATURE:` comments) grows with the XY scale a, everything else with a^2; heights
  stretch with the Z scale b. Volume up to height h at (a, b) = b (a W(h/b) + a^2 A(h/b)).
  Measured on an H2S job: within 1 % for a uniform 1.05, cautious (about 9 % low) when widening,
  optimistic (about 4 % high) when shrinking to 0.77, because walls and top shells do not shrink
  with the object. `--margin` (default 1.10) covers most of it; when splice.py still finds the
  object short, refit on the real slice (`--at`) with `--margin 1.15`, as run.py's retry does.
- It prefers one uniform scale. A small lead (2 mm on the H2S) caps the height, so then it
  widens more than it heightens (least filament, b <= a), but never squashes past
  `--max-squash` (a <= 1.5 b): squashing flattens overhangs (tan of the overhang angle grows by
  a/b), and a drooping overhang on the purge object is a collision risk.
- More copies of the main print do not need more purge: the flush is per filament change, and
  copies share their changes (two copies of an 18-change print still flush 18 times). Extra
  copies only add main-print time, so the same purge object usually fits (or can shrink).
- Early changes are often the binding ones: with a small lead, a change at 5 mm must fit in the
  purge object's first 7 mm, which favours a wide object.
- Example (H2S, lead 2, 18 changes, 8460 mm3 of flush, main print 49 mm tall): one Benchy at
  scale 1 runs out at change 17; `fit.py` picks 1.05 uniform (50.4 mm tall), which spliced and
  checked clean, estimated 116 min against 140 for two Benchies (2x the need; not printed).
  Two copies of the same main print, two Benchies as the purge object: `fit.py` predicted 0.77,
  the 0.77 slice came up 17 mm3 short at the last change (splice.py exit 3), 0.80 spliced and
  checked clean.
- make3mf.py reads Bambu Studio projects; projects saved by OrcaSlicer fail in it at any scale
  (object settings differ), so with Orca build the purge project yourself and use fit.py.

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

## H2S notes (from Studio's H2S 0.4 profiles and one print)

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
- Every H2S filament change moves the nozzle to Y 295 and back to Y 265 at its current X, at
  layer z + 3: once inside the change block (raised by hidden_change_moves) and again in the
  power-loss-resume block right after `M621` (not raised). A purge object at the back of the
  bed must never stand more than 3 mm above the main print at a change, so use `--lead 2`:
  the last change then stops the purge object at main + 2 mm and a catch-up visit finishes it.
  With the default lead 25 the back move runs into the purge object (check.py catches it).
  The second back move sits inside `M622 J1` after `M1002 judge_flag powerloss_resume_flag`,
  so it runs only when the printer resumes after a power loss; check.py counts it anyway.
- Better placement on the H2S: put the purge object behind AND to the side of the main print
  (no X overlap), so the back move, which runs at the nozzle's current X, never crosses it.
- The firmware moves no G-code shows (cut, flush, trash can `G150.x`, pause, jam recovery) are
  safe for the purge object whatever its position as long as it never stands taller than the
  main print: any move the firmware makes safely over the main print alone then clears it too.
  check.py prints how far the purge object ever gets above the main print; 0 is the strong
  case. It happens on its own when the purge object is shorter than the main print and the
  lead is small (two copies of a 49 mm print with two Benchies at 0.80: 0.00 mm; one copy with
  full-size Benchies: 2.00 mm, still safe for every move check.py can see).
- Clumping detection (`enable_wrapping_detection`, off by default in the slices here): at layers
  3, 10 and 19 its G-code runs firmware `G390` and then Y295/Y265 without lifting, and Studio
  keeps objects out of `wrapping_exclude_area` (X 172.3-232.5, Y 302-322) while it is on. The
  profile's `keepout_if` puts that box off-limits: make3mf.py always avoids it, check.py fails
  a footprint in it when the slice turns the setting on and warns otherwise. Back-center purge
  objects sit right in it; back-right does not.
- The start load line re-zeros Z with `G28.14` at Z 5, so the G-code model reads the line as
  5.8 mm tall; it is about 0.8 mm. check.py leaves the start line to the keep-out and the
  footprint check.

## Slicing with OrcaSlicer instead of Bambu Studio

The splice, check, when and pack steps also take OrcaSlicer 2.5 output (H2S profiles). Orca
has no `; MACHINE_END_GCODE_START`; `gcmodel.end_index` finds its end block at the first
`; filament end gcode` after the last layer. run.py and make3mf.py still drive Bambu Studio,
so with Orca build the two projects yourself and slice them with the same settings:
`orca-slicer.exe --load-settings "machine.json;process.json" --load-filaments "f1.json;f2.json;f3.json" --slice 0 --outputdir DIR --export-3mf X.gcode.3mf IN.3mf`.
Flattened profile JSONs (inherits and includes resolved, prime tower off, flush matrix all 0)
come from the BBL profiles in Orca's GitHub repo; the installed Orca keeps its bundled ones in
an unreadable `.opc` archive. Orca writes `L0` as the flush length for a zero-flush change.
