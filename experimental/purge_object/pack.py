"""Put a spliced G-code into a copy of the main print's sliced .gcode.3mf: replaces
Metadata/plate_1.gcode, its .md5, and the time / weight / per-filament grams in slice_info.
Usage: python pack.py MAIN.gcode.3mf SPLICED.gcode OUT.gcode.3mf EXTRA_SECONDS"""
import hashlib, re, sys, zipfile
import gcmodel as gc

src, gpath, out, extra_s = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
g = open(gpath, "rb").read()
# grams per tool from the spliced file itself (T commands switch tools; density 1.24)
tool, mm = 0, {}
for ln in g.decode().split("\n"):
    c = gc.cmd(ln)
    if c[:1] == "T" and c[1:].isdigit() and int(c[1:]) < 255:
        tool = int(c[1:])
    elif c in ("G1", "G2", "G3"):
        p = gc.params(ln)
        if p.get("E", 0) > 0 and ("X" in p or "Y" in p):
            mm[tool] = mm.get(tool, 0) + p["E"]
grams = {t: v * gc.FIL_AREA * 1.24 / 1000 for t, v in mm.items()}
zin = zipfile.ZipFile(src)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for n in zin.namelist():
        d = zin.read(n)
        if n == "Metadata/plate_1.gcode":
            d = g
        elif n == "Metadata/plate_1.gcode.md5":
            d = hashlib.md5(g).hexdigest().upper().encode()
        elif n == "Metadata/slice_info.config":
            s = d.decode()
            pred = float(re.search(r'key="prediction" value="([\d.]+)"', s)[1]) + extra_s
            s = re.sub(r'(key="prediction" value=")[\d.]+', lambda m: m[1] + f"{pred:.0f}", s)
            s = re.sub(r'(key="weight" value=")[\d.]+', lambda m: m[1] + f"{sum(grams.values()):.2f}", s)
            for t, gr in grams.items():
                s = re.sub(rf'(<filament id="{t+1}"[^>]*used_m=")[\d.]+(" used_g=")[\d.]+',
                           lambda m: f"{m[1]}{mm[t]/1000:.2f}{m[2]}{gr:.2f}", s)
            d = s.encode()
        z.writestr(n, d)
print(f"[OK] {out}: " + ", ".join(f"filament {t+1} {gr:.2f} g" for t, gr in sorted(grams.items()))
      + f", est {(pred)/60:.0f} min")
