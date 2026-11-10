"""Flatten Bambu Studio's system profiles (inherits + include templates) into complete JSON files
the Bambu Studio CLI can slice with, and pick the filament profile for an AMS tray.

The CLI resolves neither `inherits` chains nor `include` templates fully: given the stock files
it slices with a generic start G-code (no AMS load, no bed leveling, no purge) and density 0.
"""
import json
import os

# Bambu Studio install folder; set BAMBU_STUDIO_DIR when it is not in the default place
STUDIO_DIR = os.environ.get("BAMBU_STUDIO_DIR") or os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"), "Bambu Studio")
STUDIO_EXE = os.path.join(STUDIO_DIR, "bambu-studio.exe")
BBL = os.path.join(STUDIO_DIR, "resources", "profiles", "BBL")
MACHINE = "Bambu Lab P1S 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL X1C"
SKIP = {"inherits", "include", "instantiation"}


_INDEX = None


def _raw(kind, name):
    """One profile file, found through BBL.json (names may hold characters a path cannot)."""
    global _INDEX
    if _INDEX is None:
        with open(BBL + ".json", encoding="utf-8") as f:
            idx = json.load(f)
        _INDEX = {(k.split("_list")[0], e["name"]): e["sub_path"]
                  for k, v in idx.items() if k.endswith("_list") for e in v}
    with open(os.path.join(BBL, _INDEX[(kind, name)]), encoding="utf-8") as f:
        return json.load(f)


def resolve(kind, name, _path=()):
    """The full settings of one system profile: parents first, then included templates (each
    resolved the same way), then its own keys. A loop in either chain is an error."""
    if name in _path:
        raise SystemExit(f"[X] profile loop: {' -> '.join(_path + (name,))}")
    path = _path + (name,)
    d = _raw(kind, name)
    out = resolve(kind, d["inherits"], path) if d.get("inherits") else {}
    for inc in d.get("include", []):
        out.update({k: v for k, v in resolve(kind, inc, path).items() if k not in SKIP | {"name"}})
    out.update({k: v for k, v in d.items() if k not in SKIP})
    return out


def write_flat(kind, name, path, **overrides):
    d = resolve(kind, name)
    d.update(overrides)
    d["inherits"] = ""
    d["from"] = "system"
    d["instantiation"] = "true"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    return d


_FILAMENTS = None


def filaments():
    """Every instantiable filament profile that lists the P1S 0.4 as compatible:
    {name: (filament_id, settings)}."""
    global _FILAMENTS
    if _FILAMENTS is None:
        _FILAMENTS = {}
        _raw("filament", "fdm_filament_common")
        for kind, name in list(_INDEX):
            if kind != "filament":
                continue
            try:
                raw = _raw("filament", name)
            except (OSError, ValueError):
                continue
            if raw.get("instantiation") != "true":
                continue
            d = resolve("filament", name)
            if MACHINE in d.get("compatible_printers", []):
                _FILAMENTS[name] = (d.get("filament_id"), d)
    return _FILAMENTS


def for_tray(tray):
    """The filament profile for an AMS tray report (its tray_info_idx is Bambu's filament_id).
    Prefers a P1S-specific profile, then the X1C one (P1S shares it)."""
    fid = tray.get("tray_info_idx")
    hits = [n for n, (i, _) in filaments().items() if i == fid]
    if not hits:
        kind = tray.get("tray_type") or "PLA"
        hits = [n for n, (_, d) in filaments().items() if n.startswith(f"Generic {kind} @") or n == f"Generic {kind}"]
    if not hits:
        raise SystemExit(f"[X] no P1S filament profile for tray filament {fid} / {tray.get('tray_type')}")
    hits.sort(key=lambda n: (" P1S " not in n + " ", "X1C" not in n, len(n)))
    return hits[0]
