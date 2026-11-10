"""One command for a purge-object print: build, slice, splice, check, time and pack.

Usage: python run.py PROJECT.3mf PURGE.stl [--name NAME] [--machine p1s] [--plan tight|max]
       [--lead 25] [--main-xy X,Y] [--purge-xy X,Y]

PROJECT.3mf: a Bambu Studio project with the main print and its colour changes (layer tool
changes) set. PURGE.stl: any single-colour object to flush into (a Benchy works).
Everything goes to print-lan\\jobs\\purge_NAME\\; the result is NAME.gcode.3mf there. It
never sends anything to a printer.
"""
import argparse, json, os, re, subprocess, sys, time, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, TOOL)
import profiles  # noqa: E402
import machine  # noqa: E402

SLICE_TIMEOUT = 900

ap = argparse.ArgumentParser()
ap.add_argument("project")
ap.add_argument("purge_stl")
ap.add_argument("--name")
ap.add_argument("--machine")
ap.add_argument("--plan", choices=["tight", "max"], default="tight")
ap.add_argument("--lead", default="25")
ap.add_argument("--main-xy")
ap.add_argument("--purge-xy")
a = ap.parse_args()
name = a.name or re.sub(r"\W+", "_", os.path.splitext(os.path.basename(a.project))[0]).strip("_")
out = os.path.join(TOOL, "jobs", "purge_" + name)
os.makedirs(out, exist_ok=True)
py = sys.executable


def step(title, args):
    print(f"==== {title}")
    r = subprocess.run([py, *args], cwd=HERE, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stdout.write(r.stderr)
        sys.exit(f"[X] {title} failed")
    if r.stderr:
        sys.stdout.write(r.stderr)
    return r.stdout


def slice_(src, d, label):
    """Slice a project 3mf with its own embedded settings; returns the loose plate_1.gcode.
    The CLI can linger after writing result.json, so poll for it instead of waiting."""
    print(f"==== slice {label}")
    os.makedirs(d, exist_ok=True)
    res = os.path.join(d, "result.json")
    for f in (res, os.path.join(d, "plate_1.gcode")):
        if os.path.exists(f):
            os.remove(f)
    log = open(os.path.join(d, "log.txt"), "w")
    p = subprocess.Popen([profiles.STUDIO_EXE, "--slice", "0", "--outputdir", d,
                          "--export-3mf", label + ".gcode.3mf", src], stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    while not os.path.exists(res):
        if p.poll() is not None:
            time.sleep(1)
            break
        if time.time() - t0 > SLICE_TIMEOUT:
            p.kill()
            sys.exit(f"[X] {label}: Bambu Studio did not finish in {SLICE_TIMEOUT} s")
        time.sleep(2)
    time.sleep(2)
    if p.poll() is None:
        p.kill()
    r = json.load(open(res)) if os.path.exists(res) else {}
    if r.get("return_code", 1):
        sys.exit(f"[X] {label}: slicing failed: {r.get('error_string', 'no result.json')} (log {d}\\log.txt)")
    g = os.path.join(d, "plate_1.gcode")
    if not os.path.exists(g):
        with zipfile.ZipFile(os.path.join(d, label + ".gcode.3mf")) as z:
            open(g, "wb").write(z.read("Metadata/plate_1.gcode"))
    pl = r["sliced_plates"][0]
    print(f"[OK] {label}: {pl['total_predication'] / 60:.0f} min, "
          + ", ".join(f"filament {f['id']} {f['total_used_g']:.1f} g" for f in pl["filaments"]))
    return g


args = [a.project, a.purge_stl, out]
for k in ("machine", "main_xy", "purge_xy"):
    if getattr(a, k):
        args += ["--" + k.replace("_", "-"), getattr(a, k)]
step("make3mf", ["make3mf.py", *args])
job = os.path.join(out, "job.json")
mach = machine.load(json.load(open(job))["machine"])

mg = slice_(os.path.join(out, "main.3mf"), os.path.join(out, "sl_main"), "main")
pg = slice_(os.path.join(out, "purge.3mf"), os.path.join(out, "sl_purge"), "purge")
tag = ";===== machine: " + mach["name"].split()[-1]
for g in (mg, pg):
    if tag not in open(g, encoding="utf-8", errors="replace").read(400000):
        sys.exit(f"[X] {g} lacks '{tag}': sliced for another printer than the {mach['name']} profile")

spliced = os.path.join(out, name + ".gcode")
step("splice", ["splice.py", mg, pg, spliced, "--plan", a.plan, "--job", job, "--lead", a.lead])
step("check", ["check.py", spliced])
t = step("when", ["when.py", mg, pg, spliced + ".plan.json"])
m = re.search(r"total ~(\d+) min .*?: (\d+) min", t)
extra = (int(m[1]) - int(m[2])) * 60 if m else 0
packed = os.path.join(out, name + ".gcode.3mf")
step("pack", ["pack.py", os.path.join(out, "sl_main", "main.gcode.3mf"), spliced, packed, str(extra)])
print(f"[OK] ready: {packed}")
print(f"     machine profile: {mach['name']} ({mach['status']})")
print("     Not sent. Upload to the SD card and start it from the printer or Studio's Device tab;"
      " do NOT open it in Studio's Prepare view (it re-slices and loses the splice).")
