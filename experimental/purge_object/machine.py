"""Machine profiles for the purge-object tools (machines/<name>.json).

Add a printer by copying one and filling the values from Bambu Studio's machine profile
(resources/profiles/BBL/machine/<printer> 0.4 nozzle*.json: printable_area, printable_height,
extruder_clearance_*) and its start / filament-change G-code templates. Mark it UNTESTED until
it has printed once.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "machines")


def names():
    return sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))


def load(name):
    path = os.path.join(DIR, name.lower() + ".json")
    if not os.path.exists(path):
        raise SystemExit(f"[X] no machine profile '{name}' (have: {', '.join(names())})")
    m = json.load(open(path, encoding="utf-8"))
    m["id"] = name.lower()
    return m


def guess(printer_model):
    """Machine id from a project's printer_model, e.g. 'Bambu Lab P1S' -> 'p1s'."""
    for n in names():
        if load(n)["name"] == printer_model:
            return n
    return None
