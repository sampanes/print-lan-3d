# print-lan

Drive a Bambu printer (built and tested on a P1S) over the home LAN with no cloud and no
slicer pairing: status, chamber camera, slicing with Bambu Studio's command line, and sending
a job to an AMS tray. `bambu_lan.py` is the Bambu protocol; another printer brand would be a
module beside it.
Python 3.11 standard library plus numpy and Pillow (for the preview card).

## Commands (cmd)

```
cd print-lan
set BAMBU_STUDIO_DIR=D:\Apps\Bambu Studio & rem only if Studio is not in Program Files
python printer.py status
python printer.py cam
python printer.py slice path\to\model.stl --tray 1 --copies 2
python printer.py send model
python printer.py checkpoint --layer 3 --minutes 60
python printer.py pause & rem or resume / stop
python -m unittest discover -s tests & rem offline tests, no printer needed
```

- `slice` writes `jobs\NAME\` (the `.gcode.3mf`, flattened profiles, `job.json`) and puts
  `NAME_preview.png` in Downloads: the extrusion paths in the tray's colour, the first layer on
  the plate, time, grams, tray, profile, plate and temperatures.
  Options: `--plate textured|cool|engineering|hot` (default textured), `--supports`,
  `--name`, `--process "0.12mm Fine @BBL X1C"`.
- `send NAME` refuses unless the printer is idle and the tray holds the material the job was
  sliced for (checked before the upload and again right before the start). A FINISH/FAILED
  printer needs `--plate-clear` (after a camera check), a colour change `--any-color`; nothing
  overrides an active job, a printer error, or a different material. It saves a plate photo
  to Downloads, uploads over FTPS (size verified) and starts the job with bed leveling on. A
  start command that gets no answer is never resent.
- `pause` / `resume` / `stop` exit non-zero, saying "NOT confirmed", unless the printer is seen
  to change state within 30 s.
- `checkpoint` follows the running job (bound to its name) and saves a camera photo to
  Downloads at the given layer.
- `slice` without a reachable printer needs `--filament "Bambu PLA Basic @BBL X1C"`.
- `tray N --color HEX [--type PLA]` labels a loaded spool that has no Bambu tag (the AMS
  reports it as empty until labelled). It refuses a tagged spool, an unloaded tray, or a
  running job. Firmware 01.09 refuses it unsigned (err 84033543), like a print start: then
  label the tray on the printer's screen.

## How it works

- Control: MQTT over TLS on 8883 (user `bblp`, password = the printer's LAN access code).
- Files: implicit FTPS on 990 to the SD card root.
- Camera: the JPEG stream on 6000.
- TLS on all three is verified against the Bambu CA shipped with Bambu Studio
  (`resources\cert\printer.cer`), and the certificate's common name must be this printer's
  serial before the access code is sent. The LAN scan uses the same check.
- Credentials are read at run time from Bambu Studio's config (`%APPDATA%\BambuStudio`),
  never copied here. The printer's address is cached in `printer.local.json`; if it stops
  answering there, the tool scans this PC's /24 and saves the new one.
- Slicing: the Bambu Studio CLI does not resolve profile `inherits` chains or `include`
  templates. Given the stock P1S profiles it slices with a generic start G-code (no AMS
  load, no bed leveling, no purge) and density 0. `profiles.py` flattens the machine, process
  and filament profiles first, and `slice` refuses a G-code without the P1S start sequence.
- Tray to profile: the AMS reports each tray's Bambu filament ID; `profiles.for_tray` picks
  the P1S-compatible profile with that ID (P1S-specific first, then the X1C one).
