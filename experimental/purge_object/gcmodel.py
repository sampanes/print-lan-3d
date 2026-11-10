"""Small G-code model shared by splice.py and check.py (Bambu Studio output, M83 relative E).

state_walk() steps through the lines and returns, for every line index, the machine state just
BEFORE that line: position, retraction depth (mm of filament pulled back from the point where
the print is "primed"), last fan / acceleration / feedrate commands, and the printed height.
"""
import re

FIL_AREA = 3.14159265 * 0.875 ** 2      # mm^2, 1.75 mm filament
_P = re.compile(r"([A-Z])(-?\d*\.?\d+)")


def params(line):
    code = line.split(";", 1)[0]
    return {k: float(v) for k, v in _P.findall(code[code.find(" "):] if " " in code else "")}


def cmd(line):
    s = line.split(";", 1)[0].strip()
    return s.split()[0] if s else ""


class State:
    __slots__ = ("x", "y", "z", "depth", "fans", "m204", "f", "absolute")

    def __init__(self):
        self.x = self.y = self.z = 0.0
        self.depth = 0.0
        self.fans = {}
        self.m204 = None
        self.f = None
        self.absolute = True

    def copy(self):
        s = State()
        s.x, s.y, s.z, s.depth = self.x, self.y, self.z, self.depth
        s.fans, s.m204, s.f, s.absolute = dict(self.fans), self.m204, self.f, self.absolute
        return s


def step(st, line):
    """Apply one line to state st. Returns (kind, x0, y0, x1, y1, z, e) for motion, else None."""
    c = cmd(line)
    if c in ("G0", "G1", "G2", "G3"):
        p = params(line)
        x0, y0 = st.x, st.y
        if st.absolute:
            st.x = p.get("X", st.x)
            st.y = p.get("Y", st.y)
            st.z = p.get("Z", st.z)
        else:
            st.x += p.get("X", 0.0)
            st.y += p.get("Y", 0.0)
            st.z += p.get("Z", 0.0)
        if "F" in p:
            st.f = p["F"]
        e = p.get("E", 0.0)
        xy = "X" in p or "Y" in p
        if e < 0 or (e > 0 and not xy):
            st.depth -= e          # retract (wipe or plain) / unretract
        return ("move", x0, y0, st.x, st.y, st.z, e if xy else 0.0)
    if c == "G90":
        st.absolute = True
    elif c == "G91":
        st.absolute = False
    elif c == "M106":
        p = params(line)
        st.fans[int(p.get("P", 1))] = line.split(";", 1)[0].strip()
    elif c == "M204":
        st.m204 = line.split(";", 1)[0].strip()
    return None


def walk(lines):
    """States before each line (list, len(lines)+1) and the printed height before each line."""
    st = State()
    states, heights = [], []
    h = 0.0
    for ln in lines:
        states.append(st.copy())
        heights.append(h)
        m = step(st, ln)
        if m and m[6] > 0:
            h = max(h, m[5])
    states.append(st.copy())
    heights.append(h)
    return states, heights


def layer_index(lines):
    """{layer number: line index of its '; layer num/total_layer_count: N/T' comment}."""
    out = {}
    for i, ln in enumerate(lines):
        if ln.startswith("; layer num/total_layer_count:"):
            out[int(ln.split(":")[1].split("/")[0])] = i
    return out


def layer_z(lines):
    """{layer number: Z_HEIGHT} from the '; Z_HEIGHT:' comment of each CHANGE_LAYER."""
    out, z = {}, None
    for ln in lines:
        if ln.startswith("; Z_HEIGHT:"):
            z = float(ln.split(":")[1])
        elif ln.startswith("; layer num/total_layer_count:"):
            out[int(ln.split(":")[1].split("/")[0])] = z
    return out


def end_index(lines):
    """Line index where the end G-code starts. Bambu Studio marks it
    '; MACHINE_END_GCODE_START'; OrcaSlicer has no such marker, its end block starts at the
    first '; filament end gcode' after the last layer comment."""
    for i, ln in enumerate(lines):
        if ln.startswith("; MACHINE_END_GCODE_START"):
            return i
    last = max(layer_index(lines).values())
    for i in range(last, len(lines)):
        if lines[i].startswith("; filament end gcode"):
            return i
    raise SystemExit("[X] no end G-code marker (; MACHINE_END_GCODE_START or ; filament end gcode)")


def primed_ref(lines, layer1):
    """Index of the first extrusion of layer 1: the retraction-depth zero point."""
    for i in range(layer1, len(lines)):
        m = cmd(lines[i])
        if m in ("G1", "G2", "G3"):
            p = params(lines[i])
            if p.get("E", 0) > 0 and ("X" in p or "Y" in p):
                return i
    raise SystemExit("[X] no extrusion after layer 1")


_TOOL = re.compile(r"^M62([01]) S(\d+)A")


def filament_changes(lines, layers=None):
    """Filament changes after layer 1 (the start-of-print load is skipped).
    Returns (pairs, changes): pairs[k] = (from filament, to filament), 0-based;
    changes[k] = (main layer it happens in, line index right after its M621)."""
    layers = layers or layer_index(lines)
    l1 = layers[1]
    pairs, changes, cur = [], [], None
    for i, ln in enumerate(lines):
        t = _TOOL.match(ln)
        if not t or int(t[2]) >= 255:
            continue
        n = int(t[2])
        if t[1] == "0":                          # M620: change starts
            if i > l1:
                pairs.append((cur, n))
            cur = n
        elif i > l1:                             # M621: change done
            layer = max(k for k, v in layers.items() if v < i)
            changes.append((layer, i + 1))
    if len(pairs) != len(changes):
        raise SystemExit(f"[X] {len(pairs)} M620 vs {len(changes)} M621 filament-change lines after layer 1")
    return pairs, changes


# Extrusion that grows with an object's outline (scale s -> s) vs with its area (s -> s^2).
WALL_FEATURES = ("outer wall", "inner wall", "overhang wall", "gap infill")
_FEATURE = re.compile(r"^;\s*(?:FEATURE:|TYPE:)\s*(.+)$")


def layer_volumes(lines):
    """{layer: (z, wall mm^3, area mm^3)}: filament extruded per layer, split by the slicer's
    feature comments ('; FEATURE: Outer wall' in Bambu Studio / OrcaSlicer, ';TYPE:' in others)
    into wall-like and area-like extrusion. Layers without feature comments count as area."""
    zs = layer_z(lines)
    out, layer, feat = {}, None, ""
    for ln in lines:
        if ln.startswith("; layer num/total_layer_count:"):
            layer = int(ln.split(":")[1].split("/")[0])
            out[layer] = [zs.get(layer), 0.0, 0.0]
            continue
        m = _FEATURE.match(ln)
        if m:
            feat = m[1].strip().lower()
            continue
        if layer is None or cmd(ln) not in ("G1", "G2", "G3"):
            continue
        p = params(ln)
        if p.get("E", 0) > 0 and ("X" in p or "Y" in p):
            out[layer][1 if feat in WALL_FEATURES else 2] += p["E"] * FIL_AREA
    return {k: tuple(v) for k, v in out.items()}
