"""Decode a JT Tri-Strip Set Shape LOD element (topologically compressed) to triangles.

Python port of the mesh path of Open Cascade's TKJT (JT-Assistant,
Copyright (C) 2014-2015 OPEN CASCADE SAS, GPL version 2 or any later version; see COPYING):
JtDecode_Int32CDP*, JtDecode_ProbContext, JtDecode_MeshCoderDriver, JtDecode_VertexData*.
Normals and other vertex attributes are not decoded (STL needs positions only).
"""
import struct
import bisect
import numpy as np

DEBUG = False


def log(*a):
    if DEBUG:
        print(*a)


class Reader:
    def __init__(self, data, le=True, pos=0):
        self.d = data
        self.p = pos
        self.e = '<' if le else '>'

    def _u(self, fmt, n):
        v = struct.unpack_from(self.e + fmt, self.d, self.p)[0]
        self.p += n
        return v

    def u8(self): return self._u('B', 1)
    def i16(self): return self._u('h', 2)
    def i32(self): return self._u('i', 4)
    def u32(self): return self._u('I', 4)
    def u64(self): return self._u('Q', 8)
    def f32(self): return self._u('f', 4)

    def words(self, n):
        a = np.frombuffer(self.d, dtype=self.e + 'u4', count=n, offset=self.p).astype(np.uint32)
        self.p += 4 * n
        return a

    def bytes(self, n):
        b = self.d[self.p:self.p + n]
        self.p += n
        return b


# ---------------------------------------------------------------- bit readers

class WordBits:
    """MSB-first bits out of a sequence of 32-bit words (JtDecode_BitReader::U32)."""

    def __init__(self, words):
        # one big-endian byte string; read bits from it
        self.b = words.astype('>u4').tobytes() + b'\0' * 8
        self.pos = 0

    def u(self, n):
        if n == 0:
            return 0
        p = self.pos
        byte = p >> 3
        chunk = int.from_bytes(self.b[byte:byte + 9], 'big')
        v = (chunk >> (72 - (p & 7) - n)) & ((1 << n) - 1)
        self.pos = p + n
        return v

    def i(self, n):
        if n == 0:
            return 0
        v = self.u(n)
        if v >> (n - 1):
            v -= 1 << n
        return v

    def bit(self):
        return self.u(1)


class ByteBits:
    """MSB-first bits over the byte stream (JtDecode_BitReader::Raw); ends on a byte boundary."""

    def __init__(self, reader):
        self.r = reader
        self.start = reader.p
        self.pos = 0

    def u(self, n):
        if n == 0:
            return 0
        p = self.pos
        byte = self.start + (p >> 3)
        chunk = int.from_bytes(self.r.d[byte:byte + 9].ljust(9, b'\0'), 'big')
        v = (chunk >> (72 - (p & 7) - n)) & ((1 << n) - 1)
        self.pos = p + n
        return v

    def i(self, n):
        if n == 0:
            return 0
        v = self.u(n)
        if v >> (n - 1):
            v -= 1 << n
        return v

    def done(self):
        self.r.p = self.start + (self.pos + 7) // 8


# ---------------------------------------------------------------- unpackers

def wrap32(a):
    a = np.asarray(a, dtype=np.int64)
    return ((a + (1 << 31)) % (1 << 32) - (1 << 31)).astype(np.int64)


def unpack_lag1(v):
    v = np.array(v, dtype=np.int64)
    if len(v) > 4:
        v[3:] = np.cumsum(v[3:])
    return wrap32(v)


def unpack_null(v):
    return np.asarray(v, dtype=np.int64)


# ---------------------------------------------------------------- Int32 CDP Mk2

class CDP:
    """A loaded Int32 compressed data packet; .decode(unpack) returns int64 numpy array."""
    count = 0

    def decode(self, unpack=unpack_null):
        return unpack(self._decode())


class NullCDP(CDP):
    def __init__(self, words, count=None):
        self.words = words
        self.count = len(words) if count is None else count

    def _decode(self):
        return self.words.view(np.int32).astype(np.int64)[:self.count]


class ChopperCDP(CDP):
    def __init__(self, count, msb, lsb, chop, span, bias):
        self.count, self.msb, self.lsb = count, msb, lsb
        self.shift, self.bias = span - chop, bias

    def _decode(self):
        m = self.msb.decode()
        l = self.lsb.decode()
        return wrap32(wrap32((m << self.shift) | l) + self.bias)


class BitlengthCDP(CDP):
    """JT 10 Bitlength codec (spec Annex B.2): nibbler-coded bounds, 4-bit nibbles."""

    def __init__(self, count, words, ctl):
        self.count, self.words, self.ctl = count, words, ctl

    @staticmethod
    def nibbler(b):
        v = 0
        n = 0
        while True:
            v |= b.u(4) << (4 * n)
            n += 1
            if not b.u(1):
                break
        sw = 4 * n
        if sw < 32 and v >> (sw - 1):
            v -= 1 << sw
        return v

    def _decode(self):
        b = WordBits(self.words)
        n = self.count
        if b.u(1) == 0:
            mn = self.nibbler(b)
            mx = self.nibbler(b)
            fw = (mx - mn).bit_length() if mx > mn else 0
            out = [b.u(fw) + mn for _ in range(n)]
        else:
            mean = self.nibbler(b)
            fw = 0
            out = []
            while len(out) < n:
                while True:
                    dw = b.i(4)
                    fw += dw
                    if dw not in (-8, 7):
                        break
                rl = b.u(4)
                out.extend(b.i(fw) + mean for _ in range(rl))
            out = out[:n]
        if b.pos != self.ctl:
            raise ValueError(f'bitlength: used {b.pos} bits, codetext has {self.ctl}')
        return np.array(out, dtype=np.int64)


class ArithCDP(CDP):
    """JT 10 Arithmetic codec (spec Annex B.3), one probability context, escape -> OOB."""

    def __init__(self, count, words, ctl, ctx, oob):
        self.count, self.words, self.ctl, self.ctx, self.oob = count, words, ctl, ctx, oob

    def _decode(self):
        oob = self.oob.decode().tolist() if self.oob is not None else []
        n = self.count
        if self.ctl == 0:
            assert len(oob) == n, 'all-OOB packet size mismatch'
            return np.array(oob, dtype=np.int64)
        esc, occ, val = self.ctx
        cum = []
        c = 0
        for o in occ:
            c += o
            cum.append(c)
        total = c
        b = WordBits(self.words)
        low, high = 0, 0xFFFF
        code = b.u(16)
        out = []
        oi = 0
        for _ in range(n):
            rescaled = ((code - low + 1) * total - 1) // (high - low + 1)
            k = bisect.bisect_right(cum, rescaled)
            if esc[k]:
                out.append(oob[oi]); oi += 1
            else:
                out.append(val[k])
            lo_ct = cum[k] - occ[k]
            hi_ct = cum[k]
            rng = high - low + 1
            high = low + (rng * hi_ct) // total - 1
            low = low + (rng * lo_ct) // total
            while True:
                if ((high ^ low) & 0x8000) == 0:
                    pass
                elif (low >> 14) == 1 and (high >> 14) == 2:
                    code ^= 0x4000
                    low &= 0x3FFF
                    high |= 0x4000
                else:
                    break
                low = (low << 1) & 0xFFFF
                high = ((high << 1) | 1) & 0xFFFF
                code = ((code << 1) & 0xFFFF) | b.u(1)
        if oi != len(oob):
            raise ValueError(f'arith: used {oi} of {len(oob)} OOB values')
        if not (self.ctl - 32 <= b.pos <= self.ctl + 32):
            raise ValueError(f'arith: used {b.pos} bits, codetext has {self.ctl}')
        return np.array(out, dtype=np.int64)


class MTFCDP(CDP):
    """JT 10 Move-to-Front pseudo codec (undocumented in the 10.6 spec annex; inferred)."""

    def __init__(self, count, values, offsets):
        self.count, self.values, self.offsets = count, values, offsets

    def _decode(self):
        vals = self.values.decode().tolist()
        offs = self.offsets.decode().tolist()
        win = []
        out = []
        vi = 0
        for o in offs:
            if o < 0 or o >= len(win):
                v = vals[vi]; vi += 1
                win.insert(0, v)
                del win[16:]
            else:
                v = win.pop(o)
                win.insert(0, v)
            out.append(v)
        if vi != len(vals) or len(out) != self.count:
            raise ValueError(f'mtf: used {vi} of {len(vals)} values, {len(out)} of {self.count} out')
        return np.array(out, dtype=np.int64)


def read_prob_context_jt10(r):
    bb = ByteBits(r)
    n = bb.u(16)
    occ_bits = bb.u(6)
    val_bits = bb.u(7)
    min_val = bb.i(32)
    esc, occ, val = [], [], []
    for _ in range(n):
        esc.append(bb.u(1))
        occ.append(bb.u(occ_bits))
        val.append(bb.u(val_bits) + min_val)
    bb.done()
    return esc, occ, val


def load_cdp2(r, depth=0):
    start = r.p
    count = r.i32()
    if count == 0:
        return NullCDP(np.zeros(0, np.uint32), 0)
    codec = r.u8()
    log('  ' * depth + f'cdp @{start} count={count} codec={codec}')
    if codec == 4:
        chop = r.u8()
        if chop == 0:
            return load_cdp2(r, depth + 1)
        bias = r.i32()
        span = r.u8()
        msb = load_cdp2(r, depth + 1)
        lsb = load_cdp2(r, depth + 1)
        assert msb.count == count and lsb.count == count, 'chopper size mismatch'
        return ChopperCDP(count, msb, lsb, chop, span, bias)
    if codec == 5:
        values = load_cdp2(r, depth + 1)
        offsets = load_cdp2(r, depth + 1)
        return MTFCDP(count, values, offsets)
    ctl = r.i32()
    nwords = (ctl + 31) // 32
    words = r.words(nwords)
    if codec == 0:
        return NullCDP(words, count)
    if codec == 1:
        return BitlengthCDP(count, words, ctl)
    if codec == 3:
        ctx = read_prob_context_jt10(r)
        # an OOB packet follows only when the table has an escape entry
        oob = load_cdp2(r, depth + 1) if any(ctx[0]) else None
        return ArithCDP(count, words, ctl, ctx, oob)
    raise ValueError(f'unknown CDP codec {codec} at byte {start}')


# ---------------------------------------------------------------- mesh coder

class DualVFMesh:
    def __init__(self):
        self.v_val = []     # valence
        self.v_grp = []
        self.v_vfi = []     # start into vf
        self.vf = []        # vertex -> face slots
        self.f_deg = []
        self.f_empty = []
        self.f_fvi = []
        self.fv = []        # face -> vertex slots

    def new_vtx(self, val, grp):
        i = len(self.v_val)
        self.v_val.append(val)
        self.v_grp.append(grp)
        self.v_vfi.append(len(self.vf))
        self.vf.extend([-1] * val)
        return i

    def new_face(self, deg):
        i = len(self.f_deg)
        self.f_deg.append(deg)
        self.f_empty.append(deg)
        self.f_fvi.append(len(self.fv))
        self.fv.extend([-1] * deg)
        return i

    def valid_face(self, f):
        return 0 <= f < len(self.f_deg) and self.f_deg[f] != 0

    def face(self, v, slot):
        return self.vf[self.v_vfi[v] + slot]

    def vtx(self, f, slot):
        return self.fv[self.f_fvi[f] + slot]

    def set_vtx_face(self, v, slot, f):
        self.vf[self.v_vfi[v] + slot] = f

    def set_face_vtx(self, f, slot, v):
        k = self.f_fvi[f] + slot
        if self.fv[k] != v:
            self.f_empty[f] -= 1
        self.fv[k] = v

    def find_vtx_slot(self, f, target):
        s = self.f_fvi[f]
        try:
            return self.fv.index(target, s, s + self.f_deg[f]) - s
        except ValueError:
            return -1

    def find_face_slot(self, v, target):
        s = self.v_vfi[v]
        try:
            return self.vf.index(target, s, s + self.v_val[v]) - s
        except ValueError:
            return -1


def inc(v, n):
    return 0 if v == n - 1 else v + 1


def dec(v, n):
    return n - 1 if v == 0 else v - 1


class MeshDecoder:
    def __init__(self, s):
        # s: dict of decoded symbol lists
        self.deg = s['deg']          # list of 8 lists
        self.val = s['val']
        self.grp = s['grp']
        self.split_face = s['split_face']
        self.split_pos = s['split_pos']
        self.rp_deg = [0] * 8
        self.rp_val = self.rp_grp = self.rp_sf = self.rp_sp = 0
        self.m = DualVFMesh()
        self.active = []
        self.removed = set()

    # ---- symbol streams
    def next_val(self):
        if self.rp_val < len(self.val):
            self.rp_val += 1
            return self.val[self.rp_val - 1]
        return -1

    def next_grp(self):
        if self.rp_grp < len(self.grp):
            self.rp_grp += 1
            return self.grp[self.rp_grp - 1]
        return -1

    def next_deg(self, c):
        if self.rp_deg[c] < len(self.deg[c]):
            self.rp_deg[c] += 1
            return self.deg[c][self.rp_deg[c] - 1]
        return -1

    def next_sf(self):
        if self.rp_sf < len(self.split_face):
            self.rp_sf += 1
            return self.split_face[self.rp_sf - 1]
        return -1

    def next_sp(self):
        if self.rp_sp < len(self.split_pos):
            self.rp_sp += 1
            return self.split_pos[self.rp_sp - 1]
        return -1

    # ---- io
    def io_vtx(self):
        e = self.next_val()
        if e > -1:
            return self.m.new_vtx(e, self.next_grp())
        return -1

    def face_cntxt(self, v):
        m = self.m
        cval = m.v_val[v]
        nk = tot = 0
        for i in range(cval):
            f = m.face(v, i)
            if not m.valid_face(f):
                continue
            nk += 1
            tot += m.f_deg[f]
        if cval == 3:
            return 0 if tot < nk * 6 else (1 if tot == nk * 6 else 2)
        if cval == 4:
            return 3 if tot < nk * 4 else (4 if tot == nk * 4 else 5)
        if cval == 5:
            return 6
        return 7

    def io_face(self, v):
        e = self.next_deg(self.face_cntxt(v))
        if e != 0 and e != -1:
            return self.m.new_face(e)
        if e == -1:
            raise RuntimeError('degree symbols exhausted')
        return -1

    def io_split_face(self):
        e = self.next_sf()
        if e > -1:
            n = len(self.active)
            assert 0 < e <= n, f'bad split offset {e} of {n}'
            return self.active[n - e]
        raise RuntimeError('split face symbols exhausted')

    def io_split_pos(self):
        e = self.next_sp()
        if e < 0:
            raise RuntimeError('split pos symbols exhausted')
        return e

    # ---- codec
    def run(self):
        while True:
            v = self.io_vtx()
            if v == -1:
                break
            for i in range(self.m.v_val[v]):
                self.activate_f(v, i)
            while True:
                f = self.next_active_face()
                if f == -1:
                    break
                self.complete_v(f)
                self.removed.add(f)

    def complete_v(self, f):
        m = self.m
        while True:
            j = m.find_vtx_slot(f, -1)
            if j == -1:
                break
            v = self.activate_v(f, j)
            self.complete_f(v, j)

    def activate_f(self, v, vslot):
        m = self.m
        f = self.io_face(v)
        if f >= 0:
            m.set_vtx_face(v, vslot, f)
            m.set_face_vtx(f, 0, v)
            self.active.append(f)
        else:
            f = self.io_split_face()
            j = self.io_split_pos()
            m.set_vtx_face(v, vslot, f)
            self.add_vtx_to_face(v, vslot, f, j)
        return f

    def activate_v(self, f, vslot):
        v = self.io_vtx()
        if v < 0:
            raise RuntimeError('valence symbols exhausted')
        self.m.set_vtx_face(v, 0, f)
        self.add_vtx_to_face(v, 0, f, vslot)
        return v

    def complete_f(self, v, vslot):
        m = self.m
        cval = m.v_val[v]
        vp = m.face(v, 0)
        jp = vslot
        i = 1
        while True:
            vn = m.face(v, i)
            if vn == -1:
                break
            jp = dec(jp, m.f_deg[vp])
            v2 = m.vtx(vp, jp)
            if v2 == -1:
                break
            jn = m.find_vtx_slot(vn, v2)
            assert jn > -1
            jn = dec(jn, m.f_deg[vn])
            self.add_vtx_to_face(v, i, vn, jn)
            vp, jp = vn, jn
            i += 1
            if i >= cval:
                return
        ilast = i
        vp = m.face(v, 0)
        jp = vslot
        i = cval - 1
        while True:
            vn = m.face(v, i)
            if vn == -1:
                break
            jp = inc(jp, m.f_deg[vp])
            v2 = m.vtx(vp, jp)
            if v2 == -1:
                break
            jn = m.find_vtx_slot(vn, v2)
            assert jn > -1
            jn = inc(jn, m.f_deg[vn])
            self.add_vtx_to_face(v, i, vn, jn)
            vp, jp = vn, jn
            i -= 1
            if i < ilast:
                return
        while ilast <= i:
            self.activate_f(v, ilast)
            ilast += 1

    def add_vtx_to_face(self, v, fslot, f, vslot):
        m = self.m
        deg = m.f_deg[f]
        ccw = inc(vslot, deg)
        cw = dec(vslot, deg)
        m.set_face_vtx(f, vslot, v)
        fp = m.vtx(f, cw)
        if fp != -1:
            ip = m.find_face_slot(fp, f)
            s = inc(fslot, m.v_val[v])
            if m.face(v, s) == -1:
                ip = dec(ip, m.v_val[fp])
                m.set_vtx_face(v, s, m.face(fp, ip))
        fn = m.vtx(f, ccw)
        if fn != -1:
            i_n = m.find_face_slot(fn, f)
            s = dec(fslot, m.v_val[v])
            if m.face(v, s) == -1:
                i_n = inc(i_n, m.v_val[fn])
                m.set_vtx_face(v, s, m.face(fn, i_n))

    def next_active_face(self):
        a = self.active
        rem = self.removed
        while a and a[-1] in rem:
            a.pop()
        best = -1
        lowest = 9999999
        f_empty = self.m.f_empty
        i = len(a) - 1
        while i >= max(0, len(a) - 16):
            f0 = a[i]
            if f0 in rem:
                del a[i]
                i -= 1
                continue
            e = f_empty[f0]
            if e < lowest:
                lowest = e
                best = f0
            i -= 1
        return best

    def polygons(self):
        """Dual vertices are the polygons; their faces are the real vertex indices."""
        m = self.m
        out = []
        for v in range(len(m.v_val)):
            if m.v_grp[v] >= 0:
                s = m.v_vfi[v]
                out.append(m.vf[s:s + m.v_val[v]])
        return out


# ---------------------------------------------------------------- element

def read_tristrip_lod(data, le, major):
    """data: bytes of the LOD segment payload starting at the element length."""
    r = Reader(data, le)
    elen = r.i32()
    estart = r.p
    guid = r.bytes(16)
    base_type = r.u8()
    obj_id = r.i32() if major > 8 else None
    log('element len', elen, 'guid', guid.hex(), 'base', base_type, 'id', obj_id)
    if major >= 10:
        # JT 10.x: I8 Base Shape LOD version, I8 Vertex Shape LOD version, U64 bindings
        v1 = r.u8()
        v2 = r.u8()
        binding = r.u64()
        # Not in the 10.6 spec: the TopoMesh data is wrapped in its own element header
        # (I32 length, GUID f830a5ad-be4c-4fbc-9b5f-b9269278d2e1, U8 base type 9, I32 object id)
        nlen = r.i32()
        nguid = r.bytes(16)
        nbase = r.u8()
        nid = r.i32()
        log('nested element', nlen, nguid.hex(), nbase, nid)
        # TopoMesh LOD Data: U8 version, U32 vertex records object id;
        # TopoMesh Topologically Compressed LOD Data: U8 version
        mesh_ver = r.u8()
        tri_id = r.u32()
        v4 = r.u8()
    else:
        v1 = r.i16()
        v2 = r.i16()
        binding = r.u64()
        mesh_ver = r.i16()
        tri_id = r.i32()
        v4 = r.i16()
    log('versions', v1, v2, 'binding', hex(binding), 'mesh_ver', mesh_ver, 'tri', tri_id, 'v4', v4, 'at', r.p)
    names = ([f'deg{i}' for i in range(8)] + ['val', 'grp', 'fflags'] + [f'am{i}' for i in range(8)]
             + ['am8_msb', 'amlrg', 'split_vtx', 'split_pos'])
    pk = {}
    for nm in names:
        log('--', nm, '@', r.p)
        if nm == 'amlrg':
            n = r.i32()          # VecU32: High-Degree Face Attribute Masks
            r.words(n)
            pk[nm] = None
            continue
        pk[nm] = load_cdp2(r)
    h = r.u32()
    log('mesh hash', hex(h), 'at', r.p)

    # Topologically Compressed Vertex Records
    vb = r.u64()
    qp = r.bytes(4)
    nvert = r.i32()
    log('vertex records binding', hex(vb), 'quant', list(qp), 'nvert', nvert)
    coords = None
    if nvert > 0:
        nattr = r.i32()
        cnt = r.i32()
        ncomp = r.u8()
        q = [(r.f32(), r.f32(), r.u8()) for _ in range(3)]
        log('nattr', nattr, 'coord count', cnt, 'ncomp', ncomp, 'quant', q)
        if q[0][2]:
            pk_c = [load_cdp2(r) for _ in range(ncomp)]
            comps = []
            for j in range(ncomp):
                codes = pk_c[j].decode(unpack_lag1).astype(np.int64) & 0xFFFFFFFF
                mn, mx, bits = q[j]
                maxcode = float(1 << bits) if bits < 32 else float(0xFFFFFFFF)
                comps.append(mn + (codes - 0.5) * ((mx - mn) / maxcode))
            coords = np.stack(comps, axis=1)
        elif major >= 10:
            # JT 10: one packet per component, IEEE float bits through a Lag1 predictor
            pk_c = [load_cdp2(r) for _ in range(ncomp)]
            comps = []
            for j in range(ncomp):
                u = (pk_c[j].decode(unpack_lag1) & 0xFFFFFFFF).astype(np.uint32)
                comps.append(u.view(np.float32).astype(np.float64))
            coords = np.stack(comps, axis=1)
        else:
            pk_c = [load_cdp2(r) for _ in range(ncomp * 2)]
            comps = []
            for j in range(ncomp):
                ex = pk_c[2 * j].decode(unpack_lag1).astype(np.int64) & 0xFFFFFFFF
                ma = pk_c[2 * j + 1].decode(unpack_lag1).astype(np.int64) & 0xFFFFFFFF
                u = ((ex << 23) | ma).astype(np.uint32)
                comps.append(u.view(np.float32).astype(np.float64))
            coords = np.stack(comps, axis=1)
        log('coords', coords.shape, coords.min(0), coords.max(0))

    # topology
    syms = {
        'deg': [pk[f'deg{i}'].decode().tolist() for i in range(8)],
        'val': pk['val'].decode().tolist(),
        'grp': pk['grp'].decode().tolist(),
        'split_face': pk['split_vtx'].decode(unpack_lag1).tolist(),
        'split_pos': pk['split_pos'].decode().tolist(),
    }
    log('symbols: deg', [len(x) for x in syms['deg']], 'val', len(syms['val']),
        'split', len(syms['split_face']))
    md = MeshDecoder(syms)
    md.run()
    left = (sum(len(x) for x in syms['deg']) - sum(md.rp_deg), len(syms['val']) - md.rp_val,
            len(syms['split_face']) - md.rp_sf, len(syms['split_pos']) - md.rp_sp)
    log('unconsumed symbols (deg, val, splitface, splitpos):', left)
    return coords, md.polygons(), left
