# print-lan -- agent notes

Drives a Bambu printer (built on a P1S Combo with one AMS) over the LAN: status, chamber
camera, slicing with the Bambu Studio CLI, sending a job, pause/resume/stop. Read README.md
first; `experimental/purge_object/README.md` covers the purge-object prints.
Purge objects are sized to the job: scale ONE object (`run.py --purge-scale auto`,
or `fit.py` with any slicer) rather than adding copies.

## What works
- status, cam, slice (+ preview card to Downloads), verified FTPS upload, watch, checkpoint.
- Firmware 01.09 refuses unsigned commands (print start, pause; assume resume/stop too):
  `send` falls back to opening the job in Bambu Studio, which signs. Driving Studio's window
  (`screen.py`) to press Send is covered by the operator's go for that job.
- Tests: `python -m unittest discover -s tests` (offline, no printer).

## Rules
- Never send a job without the operator's explicit go for that job; check the plate with
  `cam` first.
- Never print, store or commit the printer's IP, serial or access code, or the printer's name.
  Credentials are read at run time from Bambu Studio's config; the IP lives in the ignored
  `printer.local.json`.
- No personal paths, names or machine-specific values in committed files; Bambu Studio's
  folder comes from `profiles.STUDIO_DIR` (env `BAMBU_STUDIO_DIR` overrides).
- ASCII only in all files. Commands handed to the operator: cmd.exe syntax.
- Reusable subcommands in `printer.py` over one-off scripts.
- Commit after a completed change, after scanning the staged files for local paths, emails
  and printer identifiers.
