"""Bambu printer (P1S) over the LAN: status, camera, slice with Bambu Studio's CLI, send a sliced job.

  python printer.py status
  python printer.py cam [out.jpg]
  python printer.py tray N --color HEX [--type PLA] [--filament "Generic PLA @BBL X1C"]
  python printer.py slice MODEL.stl [MODEL2.stl ...] [--tray 1] [--copies 1] [--plate textured]
                      [--supports] [--name NAME] [--process "0.20mm Standard @BBL X1C"]
                      [--filament "Bambu PLA Basic @BBL X1C"]
  python printer.py send NAME [--no-level] [--plate-clear] [--any-color]
  python printer.py open NAME
  python printer.py watch [--until-start] [--minutes 20]
  python printer.py checkpoint [--layer 3] [--minutes 30] [--out photo.jpg]
  python printer.py pause | resume | stop

A slice lands in jobs/NAME/ (NAME.gcode.3mf, the flattened profiles, job.json) and its preview
card is copied to Downloads. `send` uploads that job and starts it on the tray it was sliced for.
pause / resume / stop exit non-zero unless the printer is seen to change state.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

import bambu_lan as bl
import preview
import profiles

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, "jobs")
DOWNLOADS = os.path.join(os.environ["USERPROFILE"], "Downloads")
STUDIO = profiles.STUDIO_EXE
SLICE_TIMEOUT = 900  # s; Studio can sit on a modal dialog forever
PLATES = {"textured": "Textured PEI Plate", "cool": "Cool Plate", "engineering": "Engineering Plate",
          "hot": "High Temp Plate"}
PLATE_TEMP = {"Textured PEI Plate": "textured_plate_temp", "Cool Plate": "cool_plate_temp",
              "Engineering Plate": "eng_plate_temp", "High Temp Plate": "hot_plate_temp"}
ENDED = {"FINISH", "FAILED"}  # idle, but the last part (or a failed one) may still be on the plate
ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
SIGNED_ONLY = 84033543  # 0x05024007, HMS "MQTT command verification failed"
# command: (states it applies to, states that confirm it)
CONTROL = {"pause": ({"RUNNING", "PREPARE"}, {"PAUSE"}),
           "resume": ({"PAUSE"}, {"RUNNING", "PREPARE"}),
           "stop": (ACTIVE, {"FAILED", "IDLE", "FINISH"})}


def open_in_studio(meta, job):
    """The cloud-mode fallback: open the sliced job in Bambu Studio, which signs the start
    command. The person picks the printer and clicks Print; filament 1 is coloured as the tray,
    so Studio's AMS mapping lands on it."""
    path = os.path.join(job, meta["file"])
    subprocess.Popen([STUDIO, path], close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    print(f"[OK] opened {meta['file']} in Bambu Studio: click Print plate, check filament 1 -> "
          f"AMS tray {meta['tray']}, then Send.")


def cmd_open(a):
    job = os.path.join(JOBS, a.name)
    open_in_studio(json.load(open(os.path.join(job, "job.json"))), job)


def _trays(state):
    out = {}
    for unit in state.get("ams", {}).get("ams", []):
        for t in unit.get("tray", []):
            out[int(unit["id"]) * 4 + int(t["id"]) + 1] = t
    return out


def _tray_label(t):
    if not t.get("tray_type"):
        return "empty"
    sub = t.get("tray_sub_brands") or t["tray_type"]
    return f"{sub} #{t.get('tray_color', '')[:6]}"


def cmd_status(a):
    p = bl.Printer()
    s = p.status()
    p.close()
    print(f"[OK] state {s.get('gcode_state')}  {s.get('mc_percent', 0)}%  "
          f"{s.get('mc_remaining_time', 0)} min left  layer {s.get('layer_num', '?')}/{s.get('total_layer_num', '?')}")
    print(f"     nozzle {s.get('nozzle_temper', 0):.0f}/{s.get('nozzle_target_temper', 0):.0f} C  "
          f"bed {s.get('bed_temper', 0):.0f}/{s.get('bed_target_temper', 0):.0f} C  "
          f"error {s.get('print_error', 0)}  wifi {s.get('wifi_signal')}")
    for n, t in _trays(s).items():
        print(f"     tray {n}: {_tray_label(t)}")


def tray_setting(n, fil, color):
    """The ams_filament_setting command body that labels AMS tray n (1-based) with a filament
    profile and colour: what the printer screen does for a spool without a Bambu tag."""
    return {"ams_id": (n - 1) // 4, "tray_id": (n - 1) % 4, "tray_info_idx": fil["filament_id"],
            "tray_type": fil.get("filament_type", ["PLA"])[0], "tray_color": color.lstrip("#").upper()[:6] + "FF",
            "nozzle_temp_min": int(fil.get("nozzle_temperature_range_low", ["190"])[0]),
            "nozzle_temp_max": int(fil.get("nozzle_temperature_range_high", ["230"])[0]),
            "setting_id": ""}


def tray_set_blocker(s, n):
    """Why tray n must not be relabelled now; None if it may. A tagged Bambu spool labels
    itself, and a running job may be feeding from the tray."""
    if s.get("gcode_state") in ACTIVE:
        return f"printer is {s.get('gcode_state')}; set trays between jobs"
    ams = s.get("ams", {})
    if not 1 <= n <= 4 * len(ams.get("ams", [])):
        return f"no tray {n}"
    if not int(ams.get("tray_exist_bits", "0") or "0", 16) >> (n - 1) & 1:
        return f"tray {n} has no filament loaded"
    if int(ams.get("tray_is_bbl_bits", "0") or "0", 16) >> (n - 1) & 1:
        return f"tray {n} holds a tagged Bambu spool; it labels itself"
    return None


def cmd_tray(a):
    fil_name = a.filament or profiles.for_tray({"tray_type": a.type})
    _, fil = profiles.filaments().get(fil_name, (None, None))
    if fil is None:
        raise SystemExit(f"[X] no P1S filament profile named {fil_name}")
    body = tray_setting(a.tray, fil, a.color)
    p = bl.Printer()
    try:
        why = tray_set_blocker(p.status(), a.tray)
        if why:
            raise SystemExit(f"[X] {why}")
        r = p.command("print", "ams_filament_setting", **body)
        if r is not None and r.get("err_code") == SIGNED_ONLY:
            raise SystemExit(f"[X] the firmware refuses unsigned AMS settings (err {SIGNED_ONLY}); "
                             f"set tray {a.tray} on the printer's screen or in Bambu Studio's Device tab")
        if r is not None and not _accepted(r):
            print(f"[!] printer reply: result {r.get('result')} err_code {r.get('err_code')}")
        want = lambda st: (_trays(st).get(a.tray, {}).get("tray_info_idx") == body["tray_info_idx"]
                           and _trays(st)[a.tray].get("tray_color", "")[:6].upper() == body["tray_color"][:6])
        p.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
        s = p.wait_for(want, 20)
    finally:
        p.close()
    if s is None:
        raise SystemExit(f"[X] tray {a.tray} NOT confirmed; set it on the printer's screen instead")
    print(f"[OK] tray {a.tray}: {_tray_label(_trays(s)[a.tray])} ({fil_name})")


def cmd_cam(a):
    out = a.out or os.path.join(DOWNLOADS, "p1s_cam.jpg")
    print("[OK] saved", bl.snapshot(out))


def _embedded_gcode(path3mf, plate=1):
    """The plate G-code as it will be printed: the copy inside the .gcode.3mf."""
    with zipfile.ZipFile(path3mf) as z:
        return z.read(f"Metadata/plate_{plate}.gcode").decode("utf-8", "replace")


def cmd_slice(a):
    name = a.name or os.path.splitext(os.path.basename(a.models[0]))[0]
    job = os.path.join(JOBS, name)
    os.makedirs(job, exist_ok=True)
    out3mf = f"{name}.gcode.3mf"
    # a failed re-slice must not leave the last slice's files looking current
    for f in ("job.json", "result.json", "plate_1.gcode", out3mf):
        if os.path.exists(os.path.join(job, f)):
            os.remove(os.path.join(job, f))
    tray = None
    try:
        p = bl.Printer()
        tray = _trays(p.status()).get(a.tray)
        p.close()
    except (OSError, ConnectionError) as e:
        if not a.filament:
            raise SystemExit(f"[X] printer not reachable ({e}); pass --filament to slice for a named profile")
        print(f"[!] printer not reachable ({e}); slicing for {a.filament}")
    if tray is not None and not tray.get("tray_type"):
        raise SystemExit(f"[X] tray {a.tray} is empty")
    fil_name = a.filament or profiles.for_tray(tray)
    color = "#" + (tray.get("tray_color", "CCCCCCFF")[:6] if tray else "CCCCCC")
    bed = PLATES[a.plate]
    mpath, ppath, fpath = (os.path.join(job, f) for f in ("machine.json", "process.json", "filament.json"))
    profiles.write_flat("machine", profiles.MACHINE, mpath)
    over = {"curr_bed_type": bed}
    if a.supports:
        over.update(enable_support="1", support_type="tree(auto)")
    profiles.write_flat("process", a.process, ppath, **over)
    fil = profiles.write_flat("filament", fil_name, fpath, filament_colour=[color])
    if tray is not None and fil.get("filament_type", ["?"])[0].upper() != tray["tray_type"].upper():
        raise SystemExit(f"[X] {fil_name} is {fil.get('filament_type')}, tray {a.tray} holds {tray['tray_type']}")
    models = [os.path.abspath(m) for m in a.models for _ in range(a.copies)]
    cmd = [STUDIO, "--slice", "0", "--arrange", "1", "--load-settings", f"{mpath};{ppath}",
           "--load-filaments", fpath, "--outputdir", job, "--export-3mf", out3mf, *models]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SLICE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[X] Bambu Studio did not finish slicing in {SLICE_TIMEOUT} s")
    res_path = os.path.join(job, "result.json")
    res = json.load(open(res_path)) if os.path.exists(res_path) else {}
    if r.returncode or res.get("return_code", 1):
        print(r.stdout[-2000:], r.stderr[-2000:])
        raise SystemExit(f"[X] slicing failed: {res.get('error_string', r.returncode)}")
    gcode = os.path.join(job, "plate_1.gcode")
    # sanity on what will actually be printed: the 3MF's own copy, which must also be the
    # loose file the preview is drawn from
    head = _embedded_gcode(os.path.join(job, out3mf))
    if head != open(gcode, encoding="utf-8", errors="replace").read():
        raise SystemExit("[X] the G-code inside the 3MF differs from plate_1.gcode; not safe to print")
    if ";===== machine: P1S" not in head[:400000]:
        raise SystemExit("[X] the G-code lacks the P1S start sequence; not safe to print")
    bed_t = fil.get(PLATE_TEMP[bed], ["?"])[0]
    if f"M190 S{bed_t}" not in head[:400000] and f"M140 S{bed_t}" not in head[:400000]:
        print(f"[!] expected a {bed_t} C bed for the {bed} and did not find it in the G-code")
    plate = res.get("sliced_plates", [{}])[0]
    warn = plate.get("warning_message", "")
    info = [("Tray", (color, f"{a.tray}: {_tray_label(tray) if tray else fil_name}")),
            ("Profile", fil_name.replace(" @BBL", " @")),
            ("Plate", f"{bed}, bed {bed_t} C"),
            ("Nozzle", f"{fil.get('nozzle_temperature', ['?'])[0]} C"),
            ("Process", a.process.replace(" @BBL X1C", "")),
            ("Parts", f"{len(models)}" + (", supports" if a.supports else ", no supports"))]
    if warn:
        info.append(("Warning", warn[:60]))
    png = os.path.join(job, f"{name}_preview.png")
    header = preview.card(gcode, png, name.replace("_", " "), info, {0: color})
    shutil.copy(png, os.path.join(DOWNLOADS, os.path.basename(png)))
    # written last: its presence means every file above is from this slice
    json.dump({"name": name, "file": out3mf, "tray": a.tray, "filament": fil_name, "color": color,
               "type": fil.get("filament_type", ["?"])[0], "filament_id": fil.get("filament_id"),
               "plate": bed, "models": models, "sliced": time.strftime("%Y-%m-%d %H:%M"),
               "time": header.get("total estimated time"), "grams": header.get("total filament weight [g]")},
              open(os.path.join(job, "job.json"), "w"), indent=1)
    print(f"[OK] sliced {name} in {time.time() - t0:.0f}s: {header.get('total estimated time')}, "
          f"{header.get('total filament weight [g]')} g, tray {a.tray} ({fil_name})")
    print(f"     preview: {os.path.join(DOWNLOADS, os.path.basename(png))}")
    if warn:
        print(f"[!] slicer warning: {warn}")


def _job_material(job, meta):
    """The material the job was sliced for (older job.json files lack it; the flattened
    filament profile next to it is the record)."""
    if meta.get("type"):
        return meta["type"]
    fil = json.load(open(os.path.join(job, "filament.json"), encoding="utf-8"))
    return fil.get("filament_type", ["?"])[0]


def send_blocker(s, meta, material, plate_clear=False, any_color=False):
    """Why this job must not start on a printer in state `s`; None if it may. Nothing
    overrides an active job, an error, an empty tray or a different material."""
    st = s.get("gcode_state")
    if st in ACTIVE or (st not in ENDED and st != "IDLE"):
        return f"printer is {st}; not starting another job"
    if st in ENDED and not plate_clear:
        return (f"printer is {st}: the last part may still be on the plate. Check the camera, "
                "then pass --plate-clear")
    if s.get("print_error"):
        return f"printer reports error {s['print_error']}; clear it on the printer first"
    tray = _trays(s).get(meta["tray"], {})
    if not tray.get("tray_type"):
        return f"tray {meta['tray']} is empty"
    if tray["tray_type"].upper() != str(material).upper():
        return f"tray {meta['tray']} holds {_tray_label(tray)} but the job was sliced for {material}; re-slice"
    now = "#" + tray.get("tray_color", "")[:6]
    if now.lower() != meta["color"].lower() and not any_color:
        return (f"tray {meta['tray']} now holds {_tray_label(tray)}, sliced for {meta['color']}; "
                "re-slice, or pass --any-color if the colour does not matter")
    return None


def _accepted(r):
    # a refusal carries err_code and no result (84033543 = 0x05024007: "MQTT command
    # verification failed", firmware that wants Bambu-signed print commands)
    return r is not None and not r.get("err_code") and str(r.get("result")).lower() in ("success", "ok")


def _started(name):
    return lambda s: s.get("gcode_state") in ("PREPARE", "RUNNING") and s.get("subtask_name") == name


def cmd_send(a):
    job = os.path.join(JOBS, a.name)
    meta = json.load(open(os.path.join(job, "job.json")))
    material = _job_material(job, meta)
    flags = dict(plate_clear=a.plate_clear, any_color=a.any_color)
    p = bl.Printer()
    why = send_blocker(p.status(), meta, material, **flags)
    p.close()
    if why:
        raise SystemExit(f"[X] {why}")
    cam = os.path.join(DOWNLOADS, f"{a.name}_plate_before.jpg")
    try:
        bl.snapshot(cam)
        print("[OK] plate photo before starting:", cam)
    except (OSError, ConnectionError) as e:
        print(f"[!] no camera frame ({e})")
    remote = meta["file"]
    size = bl.upload(os.path.join(job, remote), remote)
    print(f"[OK] uploaded {remote} ({size} bytes)")
    expect = os.path.splitext(os.path.splitext(remote)[0])[0]
    p = bl.Printer()  # fresh session: the upload can outlast an idle MQTT one
    try:
        reply = None
        for style in ("file", "ftp"):  # firmware differs on how it names a file on the card
            # re-check right before each start: the printer or the tray may have changed
            why = send_blocker(p.status(), meta, material, **flags)
            if why:
                raise SystemExit(f"[X] not starting: {why}")
            reply = p.start_print(remote, [meta["tray"] - 1], bed_leveling=not a.no_level, url_style=style)
            print(f"     printer reply ({style}):", {k: reply.get(k) for k in ("result", "reason", "err_code")} if reply else None)
            if reply is None or _accepted(reply) or reply.get("err_code") == SIGNED_ONLY:
                break  # only a definite refusal is safe to retry: no answer may still mean started
        if reply is not None and not _accepted(reply):
            if reply.get("err_code") == SIGNED_ONLY:
                print("[!] the printer only takes Bambu-signed print commands (Developer Mode is off).")
                open_in_studio(meta, job)
                return
            raise SystemExit("[X] the printer refused the job")
        s = p.wait_for(_started(expect), 90)
    finally:
        p.close()
    if s is None:
        what = "did not answer the start command" if reply is None else "accepted the job but has not started it"
        raise SystemExit(f"[X] the printer {what}; NOT retrying. Check its screen or the Handy app.")
    print(f"[OK] {a.name} started on tray {meta['tray']} (state {s['gcode_state']})")


def cmd_watch(a):
    """Print state changes; with --until-start, exit once a job is preparing or running."""
    seen = None
    for s in bl.follow(a.minutes):
        st = s.get("gcode_state")
        if st != seen:
            seen = st
            print("     state:", st, time.strftime("%H:%M:%S"), flush=True)
        if a.until_start and st in ("PREPARE", "RUNNING"):
            print(f"[OK] started: {s.get('subtask_name')}  layer {s.get('layer_num')}/{s.get('total_layer_num')}  "
                  f"{s.get('mc_remaining_time')} min left", flush=True)
            return
    if a.until_start:
        raise SystemExit("[X] no job started in time")


def wait_for_layer(layer, minutes, out):
    """Follow the job that is running (or the next one to start) until it prints `layer`, then
    save a camera frame to `out`. Every state/layer change is logged. Bound to that one job: a
    different job, or this one ending, is an error. Returns the state at the photo."""
    job, began, last = None, False, None
    for s in bl.follow(minutes):
        st, n = s.get("gcode_state"), int(s.get("layer_num") or 0)
        if (st, n) != last:
            print(time.strftime("%H:%M:%S"), f"state {st} layer {n}/{s.get('total_layer_num')}", flush=True)
            last = (st, n)
        # right after a send the printer still reports the last job's FINISH: only an end seen
        # after a job began counts
        began = began or st in ACTIVE
        if st == "RUNNING":
            name = s.get("subtask_name")
            if job is None:
                job = name
            elif name != job:
                raise SystemExit(f"[X] a different job is running now ({name}, was following {job})")
        if began and (st in ENDED or st == "IDLE" or s.get("print_error")):
            raise SystemExit(f"[X] print ended or errored: state {st} error {s.get('print_error')}")
        if st == "RUNNING" and n >= layer:
            bl.snapshot(out)
            print(f"[OK] {job}: layer {n}/{s.get('total_layer_num')}, {s.get('mc_remaining_time')} min left, "
                  f"tray_now {s.get('ams', {}).get('tray_now')}, photo {out}", flush=True)
            return s
    raise SystemExit(f"[X] timed out waiting for layer {layer}")


def cmd_checkpoint(a):
    wait_for_layer(a.layer, a.minutes, a.out or os.path.join(DOWNLOADS, "p1s_checkpoint.jpg"))


def _control(name):
    applies, confirms = CONTROL[name]

    def run(a):
        p = bl.Printer()
        try:
            before = p.status().get("gcode_state")
            if before not in applies:
                raise SystemExit(f"[X] printer is {before}; nothing to {name}")
            r = getattr(p, name)()
            if r is not None and not _accepted(r):
                print(f"[!] printer reply: result {r.get('result')} err_code {r.get('err_code')}")
            s = p.wait_for(lambda st: st.get("gcode_state") in confirms, 30)
            now = p.state.get("gcode_state")
        finally:
            p.close()
        if s is None:
            raise SystemExit(f"[X] {name} NOT confirmed: the printer is still {now}. "
                             "Use the printer's screen or the Handy app now.")
        print(f"[OK] {name} confirmed: printer is {s['gcode_state']}")
    return run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    c = sub.add_parser("cam"); c.add_argument("out", nargs="?"); c.set_defaults(fn=cmd_cam)
    t = sub.add_parser("tray", help="label an untagged spool: material profile and colour")
    t.add_argument("tray", type=int)
    t.add_argument("--type", default="PLA", help="material, for the Generic profile (default PLA)")
    t.add_argument("--filament", help="filament profile name instead of Generic TYPE")
    t.add_argument("--color", required=True, help="hex colour, e.g. D4AF37")
    t.set_defaults(fn=cmd_tray)
    s = sub.add_parser("slice")
    s.add_argument("models", nargs="+")
    s.add_argument("--tray", type=int, default=1)
    s.add_argument("--copies", type=int, default=1)
    s.add_argument("--plate", choices=sorted(PLATES), default="textured")
    s.add_argument("--supports", action="store_true")
    s.add_argument("--name")
    s.add_argument("--process", default=profiles.PROCESS)
    s.add_argument("--filament", help="filament profile name (default: the one matching the tray)")
    s.set_defaults(fn=cmd_slice)
    d = sub.add_parser("send")
    d.add_argument("name")
    d.add_argument("--no-level", action="store_true")
    d.add_argument("--plate-clear", action="store_true",
                   help="the printer shows FINISH/FAILED and the plate has been checked empty")
    d.add_argument("--any-color", action="store_true", help="same material, different colour is fine")
    d.set_defaults(fn=cmd_send)
    w = sub.add_parser("watch")
    w.add_argument("--until-start", action="store_true")
    w.add_argument("--minutes", type=float, default=20)
    w.set_defaults(fn=cmd_watch)
    k = sub.add_parser("checkpoint", help="photo once the running job reaches a layer")
    k.add_argument("--layer", type=int, default=3)
    k.add_argument("--minutes", type=float, default=30)
    k.add_argument("--out")
    k.set_defaults(fn=cmd_checkpoint)
    o = sub.add_parser("open")
    o.add_argument("name")
    o.set_defaults(fn=cmd_open)
    for n in CONTROL:
        sub.add_parser(n).set_defaults(fn=_control(n))
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
