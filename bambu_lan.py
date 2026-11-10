"""Bambu P1S over the LAN with the standard library only:
- MQTT 3.1.1 over TLS on 8883 (status, commands), user `bblp`, password = LAN access code
- implicit FTPS on 990 (upload to the SD card)
- the chamber camera's JPEG stream on 6000

The serial and access code are read from Bambu Studio's own config at run time and never
printed or stored here. The printer's IP comes from printer.local.json (or a LAN scan).

Every TLS connection is verified against the Bambu CA that ships with Bambu Studio and must
present a certificate whose common name is this printer's serial. Nothing, least of all the
access code, goes to a host that fails that: a stale cached IP that now belongs to another
device, or anything else answering on the printer's ports."""
import ftplib
import json
import os
import secrets
import socket
import ssl
import struct
import time
from collections import deque

from profiles import STUDIO_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
STUDIO_CONF = os.path.join(os.environ["APPDATA"], "BambuStudio", "BambuStudio.conf")
STUDIO_CA = os.path.join(STUDIO_DIR, "resources", "cert", "printer.cer")
LOCAL = os.path.join(HERE, "printer.local.json")
KEEPALIVE = 60                 # seconds, as told to the broker in CONNECT
MAX_PACKET = 4 << 20           # a full status report is ~10 KB
MAX_FRAME = 8 << 20            # a camera JPEG is ~100 KB


class IdentityError(ConnectionError):
    """The host's certificate is not this printer's."""


def creds():
    """(serial, access code): from the environment (BAMBU_SERIAL, BAMBU_ACCESS_CODE) when both
    are set, for a printer Bambu Studio does not know; otherwise from Bambu Studio's config."""
    env = os.environ.get("BAMBU_SERIAL"), os.environ.get("BAMBU_ACCESS_CODE")
    if all(env):
        return env
    raw = open(STUDIO_CONF, encoding="utf-8").read().split("\n# MD5")[0]
    codes = json.loads(raw)["access_code"]
    if len(codes) != 1:
        raise SystemExit(f"[X] expected one printer in Bambu Studio, found {len(codes)}")
    (serial, code), = codes.items()
    return serial, code


def _ctx():
    if not os.path.exists(STUDIO_CA):
        raise SystemExit(f"[X] Bambu's CA certificate is missing ({STUDIO_CA}); is Bambu Studio installed?")
    ctx = ssl.create_default_context(cafile=STUDIO_CA)
    ctx.check_hostname = False  # reached by IP; the identity check is the CN in _check_peer
    return ctx


def _check_peer(s, serial):
    cert = s.getpeercert() or {}
    cn = dict(x[0] for x in cert.get("subject", ())).get("commonName")
    if cn != serial:
        s.close()
        raise IdentityError("[X] that host has a Bambu certificate for a different printer; not logging in")


def _verified(raw, serial):
    s = _ctx().wrap_socket(raw)
    _check_peer(s, serial)
    return s


def is_printer(ip, serial, timeout=2):
    """True if `ip` is this printer: a verified TLS handshake on 8883 naming its serial."""
    try:
        _verified(socket.create_connection((ip, 8883), timeout=timeout), serial).close()
        return True
    except (OSError, ConnectionError):
        return False


def scan(prefix, serial):
    """Hosts on prefix.1-254 that prove to be this printer."""
    from concurrent.futures import ThreadPoolExecutor

    def probe(i):
        ip = f"{prefix}.{i}"
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect((ip, 8883))
        except OSError:
            return None
        finally:
            s.close()
        return ip if is_printer(ip, serial) else None

    with ThreadPoolExecutor(64) as ex:
        return [ip for ip in ex.map(probe, range(1, 255)) if ip]


def printer_ip():
    """The saved IP if the printer still answers there, else scan this PC's /24 and save."""
    serial = creds()[0]
    ip = None
    if os.path.exists(LOCAL):
        ip = json.load(open(LOCAL)).get("ip")
    if ip:
        if is_printer(ip, serial):
            return ip
        print("[!] the printer is not at the saved address any more; scanning")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.0.2.1", 9))  # no packet sent; just picks the LAN interface
    mine = s.getsockname()[0]
    s.close()
    found = scan(mine.rsplit(".", 1)[0], serial)
    if len(found) != 1:
        raise SystemExit(f"[X] found the printer {len(found)} times on the LAN; set ip in {LOCAL}")
    json.dump({"ip": found[0]}, open(LOCAL, "w"), indent=1)
    return found[0]


def _s(b):
    b = b.encode() if isinstance(b, str) else b
    return struct.pack(">H", len(b)) + b


def _len(n):
    out = b""
    while True:
        d, n = n % 128, n // 128
        out += bytes([d | (0x80 if n else 0)])
        if not n:
            return out


class Printer:
    """One MQTT session. Every report read (by any call) is merged into `state`, and reports
    that a call was not looking for are kept in `backlog` for the next messages() call, so
    nothing is lost while waiting for a command's reply."""

    def __init__(self, ip=None, timeout=10):
        self.ip = ip or printer_ip()
        self.serial, self.code = creds()
        self.sock = _verified(socket.create_connection((self.ip, 8883), timeout=timeout), self.serial)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.pid = 1
        self.seq = int(time.time()) % 100000
        self.state = {}
        self.backlog = deque()
        self.last_tx = time.monotonic()
        var = _s("MQTT") + bytes([4, 0xC2]) + struct.pack(">H", KEEPALIVE)
        self._send(0x10, var + _s(f"print-lan-{secrets.token_hex(4)}") + _s("bblp") + _s(self.code))
        kind, body = self._read()
        if kind != 0x20 or body[1] != 0:
            raise ConnectionError(f"[X] printer refused the login (code {body[1] if len(body) > 1 else '?'})")
        self.request = f"device/{self.serial}/request"
        self._send(0x82, struct.pack(">H", self._pid()) + _s(f"device/{self.serial}/report") + b"\x00")

    # --- MQTT plumbing
    def _pid(self):
        self.pid = self.pid % 65535 + 1
        return self.pid

    def _send(self, kind, body):
        self.sock.sendall(bytes([kind]) + _len(len(body)) + body)
        self.last_tx = time.monotonic()

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("[X] printer closed the connection")
            self.buf += chunk

    def _read(self):
        """One whole packet. Nothing leaves the buffer until the packet is complete, so a
        timeout part-way through leaves the stream in step for the next call."""
        i, n, mul = 1, 0, 1
        while True:
            self._fill(i + 1)
            d = self.buf[i]
            n += (d & 127) * mul
            mul *= 128
            i += 1
            if not d & 128:
                break
            if i > 4:
                raise ConnectionError("[X] malformed MQTT packet length")
        if n > MAX_PACKET:
            raise ConnectionError(f"[X] MQTT packet of {n} bytes; refusing")
        self._fill(i + n)
        kind, body = self.buf[0] & 0xF0, self.buf[i:i + n]
        self.buf = self.buf[i + n:]
        return kind, body

    def _decode(self, kind, body):
        if kind != 0x30:
            return None  # SUBACK, PINGRESP
        tl = struct.unpack(">H", body[:2])[0]
        try:
            m = json.loads(body[2 + tl:])
        except ValueError:
            return None
        if isinstance(m, dict) and isinstance(m.get("print"), dict):
            self.state.update(m["print"])
        return m if isinstance(m, dict) else None

    def publish(self, payload):
        self._send(0x30, _s(self.request) + json.dumps(payload).encode())

    def messages(self, seconds):
        """Decoded JSON reports for up to `seconds` (held-back ones first). Pings the broker
        when quiet so it does not drop the session."""
        while self.backlog:
            yield self.backlog.popleft()
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            if time.monotonic() - self.last_tx > KEEPALIVE / 2:
                self._send(0xC0, b"")  # PINGREQ
            self.sock.settimeout(max(0.1, min(left, KEEPALIVE / 4)))
            try:
                kind, body = self._read()
            except (socket.timeout, TimeoutError):
                continue
            m = self._decode(kind, body)
            if m is not None:
                yield m

    def command(self, section, cmd, wait=8, **params):
        """Send {section: {command, sequence_id, ...}} and return the printer's reply to it
        (None if it did not answer within `wait` seconds)."""
        self.seq += 1
        seq = str(self.seq)
        self.publish({section: {"sequence_id": seq, "command": cmd, **params}})
        held = []
        try:
            for m in self.messages(wait):
                body = m.get(section)
                if isinstance(body, dict) and body.get("command") == cmd and str(body.get("sequence_id")) == seq:
                    return body
                held.append(m)
            return None
        finally:
            self.backlog.extend(held)

    def status(self, seconds=8):
        """The full status (the printer only sends deltas unless asked)."""
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
        for m in self.messages(seconds):
            p = m.get("print")
            if isinstance(p, dict) and p.get("command") == "push_status" and "ams" in p:
                break
        return dict(self.state)

    def wait_for(self, pred, seconds):
        """The state once pred(state) holds (checking what is known first), else None."""
        if pred(self.state):
            return dict(self.state)
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            for _ in self.messages(min(left, 5)):
                if pred(self.state):
                    return dict(self.state)
        return None

    def close(self):
        try:
            self._send(0xE0, b"")
        except OSError:
            pass
        self.sock.close()

    # --- print control
    def start_print(self, remote_name, ams_mapping, plate=1, bed_leveling=True, timelapse=False,
                    url_style="file"):
        """Start a sliced .gcode.3mf already on the SD card root. ams_mapping[i] is the AMS slot
        (0-3 on the first AMS) for project filament i."""
        url = f"file:///sdcard/{remote_name}" if url_style == "file" else f"ftp:///{remote_name}"
        return self.command(
            "print", "project_file", wait=15,
            param=f"Metadata/plate_{plate}.gcode", url=url,
            subtask_name=os.path.splitext(os.path.splitext(remote_name)[0])[0],
            project_id="0", profile_id="0", task_id="0", subtask_id="0", md5="",
            bed_type="auto", bed_leveling=bed_leveling, bed_levelling=bed_leveling,
            flow_cali=False, vibration_cali=False, layer_inspect=False, timelapse=timelapse,
            use_ams=True, ams_mapping=list(ams_mapping))

    def pause(self):
        return self.command("print", "pause")

    def resume(self):
        return self.command("print", "resume")

    def stop(self):
        return self.command("print", "stop")


def follow(minutes, ip=None, full_every=60):
    """Yield the printer's merged state each time a report arrives, for `minutes`. A lost
    connection is reopened and the state starts again from a fresh full report, so nothing
    from the old session leaks into the new one."""
    end = time.monotonic() + 60 * minutes
    p = None
    try:
        while time.monotonic() < end:
            try:
                if p is None:
                    p = Printer(ip)
                    p.status()
                    full = time.monotonic()
                    yield dict(p.state)
                if time.monotonic() - full > full_every:  # in case a delta was lost
                    p.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
                    full = time.monotonic()
                for _ in p.messages(5):
                    yield dict(p.state)
            except (OSError, ConnectionError) as e:
                print("[!] connection lost:", e, flush=True)
                if p is not None:
                    p.close()
                p = None
                time.sleep(5)
    finally:
        if p is not None:
            p.close()


class _FTPS(ftplib.FTP_TLS):
    """Implicit TLS on 990; data connections reuse the control session."""

    serial = None

    def connect(self, host, port=990, timeout=20):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = socket.create_connection((host, port), timeout)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock)
        _check_peer(self.sock, self.serial)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        # the printer never answers a TLS close_notify on the data connection, so close it
        # without the unwrap() ftplib does (which waits until the timeout)
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        finally:
            conn.close()
        return self.voidresp()


def ftp(ip=None):
    serial, code = creds()
    f = _FTPS(context=_ctx())
    f.serial = serial
    f.connect(ip or printer_ip())
    f.login("bblp", code)
    f.prot_p()
    return f


def upload(local_path, remote_name, ip=None):
    """Upload to the SD card root; raises unless the card then holds every byte."""
    want = os.path.getsize(local_path)
    f = ftp(ip)
    try:
        with open(local_path, "rb") as fh:
            f.storbinary(f"STOR {remote_name}", fh, blocksize=64 * 1024)
        size = f.size(remote_name)
    finally:
        f.quit()
    if size != want:
        raise ConnectionError(f"[X] upload incomplete: {size} of {want} bytes on the card")
    return size


def snapshot(path, ip=None, timeout=15):
    """Save one JPEG from the chamber camera (port 6000)."""
    serial, code = creds()
    s = _verified(socket.create_connection((ip or printer_ip(), 6000), timeout=timeout), serial)
    s.settimeout(timeout)
    end = time.monotonic() + 2 * timeout
    s.sendall(struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.encode().ljust(32, b"\0"))

    def take(n):
        out = b""
        while len(out) < n:
            if time.monotonic() > end:
                raise TimeoutError("[X] camera too slow")
            chunk = s.recv(n - len(out))
            if not chunk:
                raise ConnectionError("[X] camera closed the stream")
            out += chunk
        return out

    try:
        for i in range(5):  # the first frame can be stale; take the second good one
            size = struct.unpack("<I", take(16)[:4])[0]
            if size > MAX_FRAME:
                raise ConnectionError(f"[X] camera frame of {size} bytes; refusing")
            img = take(size)
            if img[:2] == b"\xff\xd8" and i > 0:
                with open(path, "wb") as f:
                    f.write(img)
                return path
    finally:
        s.close()
    raise ConnectionError("[X] no JPEG frame from the camera")
