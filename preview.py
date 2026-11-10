"""A preview card for a sliced plate, drawn from the G-code itself: a 3D view of the extrusion
paths in the tray's colour, the first layer on the full plate, and the numbers that matter."""
import math
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BED = 256.0
SKIP_3D = {"Custom", "Skirt", "Prime tower", "Wipe tower"}
DIM = {"Sparse infill": 0.55, "Internal solid infill": 0.6, "Inner wall": 0.8, "Bridge": 0.75,
       "Support": 0.5, "Support interface": 0.55, "Support transition": 0.5}


def parse(path):
    """Extrusion segments as an (N, 6) array x0 y0 z0 x1 y1 z1, a parallel feature list and
    tool list, plus the header block."""
    segs, feats, tools = [], [], []
    header = {}
    x = y = z = 0.0
    feat, tool, relative_e, in_header = "", 0, True, False
    num = re.compile(r"([XYZEIJ])(-?[\d.]+)")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith(";"):
                if line.startswith("; HEADER_BLOCK_START"):
                    in_header = True
                elif line.startswith("; HEADER_BLOCK_END"):
                    in_header = False
                elif in_header:
                    for part in line[1:].split(";"):
                        if ":" in part:
                            k, v = part.split(":", 1)
                            header[k.strip()] = v.strip()
                elif line.startswith("; FEATURE: "):
                    feat = line[11:].strip()
                continue
            c = line.split(";", 1)[0].strip()
            if not c:
                continue
            op = c.split(None, 1)[0]
            if op == "M83":
                relative_e = True
            elif op == "M82":
                relative_e = False
            elif op[0] == "T" and op[1:].isdigit():
                t = int(op[1:])
                if t < 255:
                    tool = t
            elif op in ("G0", "G1", "G2", "G3"):
                v = {k: float(n) for k, n in num.findall(c)}
                nx, ny, nz = v.get("X", x), v.get("Y", y), v.get("Z", z)
                e = v.get("E", 0.0)
                extruding = e > 0 and relative_e
                if extruding and op in ("G2", "G3") and ("I" in v or "J" in v):
                    cx, cy = x + v.get("I", 0.0), y + v.get("J", 0.0)
                    a0 = math.atan2(y - cy, x - cx)
                    a1 = math.atan2(ny - cy, nx - cx)
                    r = math.hypot(x - cx, y - cy)
                    if op == "G2" and a1 >= a0:
                        a1 -= 2 * math.pi
                    if op == "G3" and a1 <= a0:
                        a1 += 2 * math.pi
                    n = max(2, int(abs(a1 - a0) * r / 1.0))
                    px, py = x, y
                    for k in range(1, n + 1):
                        a = a0 + (a1 - a0) * k / n
                        qx, qy = cx + r * math.cos(a), cy + r * math.sin(a)
                        segs.append((px, py, nz, qx, qy, nz)); feats.append(feat); tools.append(tool)
                        px, py = qx, qy
                elif extruding and (nx != x or ny != y):
                    segs.append((x, y, nz, nx, ny, nz)); feats.append(feat); tools.append(tool)
                x, y, z = nx, ny, nz
    return np.array(segs, dtype=np.float32).reshape(-1, 6), feats, np.array(tools), header


def _rgb(hexcol):
    h = hexcol.lstrip("#")[:6]
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _font(size, bold=False):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _view3d(segs, feats, tools, colors, size, ss=2):
    """Painter's-order render from the front right, above."""
    W, H = size[0] * ss, size[1] * ss
    img = Image.new("RGB", (W, H), (24, 26, 30))
    d = ImageDraw.Draw(img)
    keep = np.array([f not in SKIP_3D for f in feats])
    s, fl, tl = segs[keep], [f for f, k in zip(feats, keep) if k], tools[keep]
    if not len(s):
        return img.resize(size, Image.LANCZOS)
    phi, el = math.radians(-60), math.radians(32)
    right = np.array([-math.sin(phi), math.cos(phi), 0.0])
    up = np.array([-math.cos(phi) * math.sin(el), -math.sin(phi) * math.sin(el), math.cos(el)])
    toward = np.array([math.cos(phi) * math.cos(el), math.sin(phi) * math.cos(el), math.sin(el)])
    p0, p1 = s[:, :3], s[:, 3:]
    sx0, sy0, sx1, sy1 = p0 @ right, p0 @ up, p1 @ right, p1 @ up
    xs, ys = np.concatenate([sx0, sx1]), np.concatenate([sy0, sy1])
    pad = 0.08
    span = max(xs.max() - xs.min(), (ys.max() - ys.min()) * W / H) * (1 + 2 * pad)
    scale = W / span
    cx, cy = (xs.max() + xs.min()) / 2, (ys.max() + ys.min()) / 2

    def to_px(a, b):
        return W / 2 + (a - cx) * scale, H / 2 - (b - cy) * scale

    # plate grid under the parts
    zmin = float(min(p0[:, 2].min(), p1[:, 2].min()))
    lo = np.floor(np.minimum(p0[:, :2].min(0), p1[:, :2].min(0)) / 10) * 10 - 20
    hi = np.ceil(np.maximum(p0[:, :2].max(0), p1[:, :2].max(0)) / 10) * 10 + 20
    lo, hi = np.clip(lo, 0, BED), np.clip(hi, 0, BED)
    for gx in np.arange(lo[0], hi[0] + 0.1, 10):
        a, b = np.array([gx, lo[1], zmin - 0.2]), np.array([gx, hi[1], zmin - 0.2])
        d.line([to_px(a @ right, a @ up), to_px(b @ right, b @ up)], fill=(44, 47, 54), width=ss)
    for gy in np.arange(lo[1], hi[1] + 0.1, 10):
        a, b = np.array([lo[0], gy, zmin - 0.2]), np.array([hi[0], gy, zmin - 0.2])
        d.line([to_px(a @ right, a @ up), to_px(b @ right, b @ up)], fill=(44, 47, 54), width=ss)

    depth = ((p0 + p1) / 2) @ toward
    order = np.argsort(depth)
    zmax = max(float(p1[:, 2].max()), 0.1)
    light = np.array([0.45, -0.75, 0.0])
    light /= np.linalg.norm(light)
    width = max(ss, int(round(0.45 * scale)))
    base = {t: np.array(_rgb(c), dtype=float) for t, c in colors.items()}
    for i in order:
        x0, y0 = to_px(sx0[i], sy0[i])
        x1, y1 = to_px(sx1[i], sy1[i])
        dx, dy = s[i, 3] - s[i, 0], s[i, 4] - s[i, 1]
        n = math.hypot(dx, dy) or 1.0
        facing = abs(-dy / n * light[0] + dx / n * light[1])  # wall normal against the light
        k = DIM.get(fl[i], 1.0) * (0.55 + 0.3 * facing + 0.25 * s[i, 2] / zmax)
        col = base.get(int(tl[i]), base.get(0, np.array([200.0, 200, 200])))
        c = tuple(int(min(255, v)) for v in col * k)
        d.line([(x0, y0), (x1, y1)], fill=c, width=width)
    return img.resize(size, Image.LANCZOS)


def _first_layer(segs, tools, colors, size):
    img = Image.new("RGB", (size, size), (34, 37, 42))
    d = ImageDraw.Draw(img)
    k = (size - 20) / BED
    o = 10
    d.rectangle([o, o, o + BED * k, o + BED * k], outline=(90, 94, 102), width=2)
    for g in range(0, 257, 32):
        d.line([o + g * k, o, o + g * k, o + BED * k], fill=(48, 52, 58))
        d.line([o, o + g * k, o + BED * k, o + g * k], fill=(48, 52, 58))
    if len(segs):
        z1 = segs[:, 2].min()
        first = segs[:, 2] <= z1 + 1e-3
        for (x0, y0, _, x1, y1, _), t in zip(segs[first], tools[first]):
            d.line([o + x0 * k, o + (BED - y0) * k, o + x1 * k, o + (BED - y1) * k],
                   fill=_rgb(colors.get(int(t), "#cccccc")), width=2)
    return img


def card(gcode_path, out_png, title, info_lines, colors):
    """info_lines: [(label, value)]; colors: {tool index: '#rrggbb'}."""
    segs, feats, tools, header = parse(gcode_path)
    W, H = 1600, 1000
    img = Image.new("RGB", (W, H), (24, 26, 30))
    img.paste(_view3d(segs, feats, tools, colors, (1000, 1000)), (0, 0))
    d = ImageDraw.Draw(img)
    x = 1040
    d.text((x, 34), title, font=_font(40, True), fill=(240, 240, 240))
    y = 100
    stats = [("Print time", header.get("total estimated time", "?")),
             ("Filament", f"{header.get('total filament weight [g]', '?')} g, "
                          f"{float(header.get('total filament length [mm]', 0)) / 1000:.2f} m"),
             ("Layers", f"{header.get('total layer number', '?')}, {header.get('max_z_height', '?')} mm tall")]
    for label, value in stats + list(info_lines):
        d.text((x, y), label, font=_font(22), fill=(150, 156, 166))
        if isinstance(value, tuple):  # (swatch colour, text)
            d.rectangle([x + 190, y + 4, x + 214, y + 28], fill=_rgb(value[0]), outline=(200, 200, 200))
            d.text((x + 226, y), value[1], font=_font(24, True), fill=(235, 235, 235))
        else:
            d.text((x + 190, y), str(value), font=_font(24, True), fill=(235, 235, 235))
        y += 40
    y += 16
    d.text((x, y), "First layer on the plate (front at the bottom)", font=_font(20), fill=(150, 156, 166))
    side = min(H - y - 50, W - x - 40)
    img.paste(_first_layer(segs, tools, colors, side), (x, y + 32))
    img.save(out_png)
    return header
