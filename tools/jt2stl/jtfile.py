"""JT file container: header, TOC, segment headers. Python port of parts of TKJT (GPL-2.0)."""
import struct, zlib, lzma

class JtFile:
    def __init__(self, path):
        self.data = open(path, 'rb').read()
        d = self.data
        self.version = d[:80].decode('ascii', 'replace').strip()
        self.le = d[80] == 0
        self.e = '<' if self.le else '>'
        e = self.e
        self.major = int(self.version.split('Version')[1].split('.')[0])
        # JT 10: u64 TOC offset at 85 after reserved i32 at 81
        if self.major >= 10:
            (toc_off,) = struct.unpack_from(e + 'Q', d, 85)
        else:
            (toc_off,) = struct.unpack_from(e + 'i', d, 85)
        self.toc = []
        (n,) = struct.unpack_from(e + 'i', d, toc_off)
        p = toc_off + 4
        for _ in range(n):
            guid = d[p:p + 16]
            if self.major >= 10:
                off, ln, attr = struct.unpack_from(e + 'QII', d, p + 16); p += 32
            else:
                off, ln, attr = struct.unpack_from(e + 'iII', d, p + 16); p += 28
            self.toc.append((guid, off, ln, attr >> 24))

    def segment(self, off):
        """Return (guid, type, payload bytes) with segment-level compression removed."""
        e, d = self.e, self.data
        guid = d[off:off + 16]
        typ, size = struct.unpack_from(e + 'ii', d, off + 16)
        body = d[off + 24: off + size]
        if typ in (1, 2, 3, 4, 17, 18, 20, 24):
            flag, dlen, alg = struct.unpack_from(e + 'iiB', body, 0)
            if flag in (2, 3) and alg in (2, 3):
                raw = body[9: 9 + dlen - 1]
                if alg == 2:
                    body = zlib.decompress(raw)
                else:
                    body = lzma_raw(raw)
                return guid, typ, body
            elif flag in (2, 3):
                raise ValueError(f'segment {off}: unknown compression flag {flag} alg {alg}')
        return guid, typ, body

def lzma_raw(raw):
    # JT 10.x (DM 10.8) writes a plain .xz stream; older writers may use .lzma "alone" or raw LZMA1
    if raw[:6] == bytes.fromhex('fd377a585a00'):
        return lzma.LZMADecompressor(format=lzma.FORMAT_XZ).decompress(raw)
    try:
        return lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        pass
    props = raw[0]; dict_size = struct.unpack_from('<I', raw, 1)[0]
    lc = props % 9; props //= 9; lp = props % 5; pb = props // 5
    f = [{'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}]
    return lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=f).decompress(raw[5:])
