"""Offline tests: no printer, no network. python -m unittest discover -s tests"""
import json
import os
import socket
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bambu_lan as bl  # noqa: E402
import printer  # noqa: E402
import profiles  # noqa: E402


class FakeSock:
    """recv() hands out the queued chunks one per call; a None chunk is a timeout."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = []

    def recv(self, n):
        if not self.chunks:
            raise socket.timeout()
        c = self.chunks.pop(0)
        if c is None:
            raise socket.timeout()
        return c

    def sendall(self, b):
        self.sent.append(b)

    def settimeout(self, t):
        pass

    def close(self):
        pass


def publish(obj, topic="device/x/report"):
    body = bl._s(topic) + json.dumps(obj).encode()
    return bytes([0x30]) + bl._len(len(body)) + body


def fake_printer(chunks=()):
    p = bl.Printer.__new__(bl.Printer)
    p.sock, p.buf, p.pid, p.seq = FakeSock(chunks), b"", 1, 100
    p.state, p.backlog, p.last_tx = {}, bl.deque(), bl.time.monotonic()
    p.request = "device/x/request"
    return p


class MqttFraming(unittest.TestCase):
    def test_timeout_mid_packet_keeps_stream_in_step(self):
        pkt = publish({"print": {"gcode_state": "RUNNING"}})
        p = fake_printer([pkt[:3], None, pkt[3:]])
        with self.assertRaises(socket.timeout):
            p._read()
        kind, body = p._read()
        self.assertEqual(kind, 0x30)
        self.assertEqual(p._decode(kind, body)["print"]["gcode_state"], "RUNNING")
        self.assertEqual(p.buf, b"")

    def test_two_packets_in_one_chunk(self):
        a, b = publish({"print": {"layer_num": 1}}), publish({"print": {"layer_num": 2}})
        p = fake_printer([a + b])
        for want in (1, 2):
            self.assertEqual(p._decode(*p._read())["print"]["layer_num"], want)

    def test_long_remaining_length(self):
        big = {"print": {"pad": "x" * 20000}}
        p = fake_printer([publish(big)])
        self.assertEqual(len(p._decode(*p._read())["print"]["pad"]), 20000)

    def test_malformed_and_oversize_lengths_refused(self):
        with self.assertRaises(ConnectionError):
            fake_printer([b"\x30\xff\xff\xff\xff\x01"])._read()
        with self.assertRaises(ConnectionError):
            fake_printer([b"\x30" + bl._len(bl.MAX_PACKET + 1)])._read()

    def test_command_keeps_other_reports(self):
        reply = {"print": {"command": "pause", "sequence_id": "101", "result": "success"}}
        p = fake_printer([publish({"print": {"gcode_state": "PAUSE"}}), publish(reply)])
        self.assertEqual(p.command("print", "pause", wait=1)["result"], "success")
        self.assertEqual(p.state["gcode_state"], "PAUSE")
        self.assertEqual([m["print"].get("gcode_state") for m in p.messages(0)], ["PAUSE"])

    def test_command_timeout_returns_none(self):
        self.assertIsNone(fake_printer([]).command("print", "stop", wait=0.3))

    def test_ping_when_quiet(self):
        p = fake_printer([])
        p.last_tx -= bl.KEEPALIVE
        list(p.messages(0.2))
        self.assertIn(b"\xc0\x00", p.sock.sent)

    def test_wait_for_sees_state_already_known(self):
        p = fake_printer([])
        p.state["gcode_state"] = "PAUSE"
        self.assertEqual(p.wait_for(lambda s: s.get("gcode_state") == "PAUSE", 0.1)["gcode_state"], "PAUSE")
        self.assertIsNone(p.wait_for(lambda s: s.get("gcode_state") == "RUNNING", 0.3))


def state(st="IDLE", tray_type="PLA", color="F74E02FF", **kw):
    return {"gcode_state": st, "print_error": 0,
            "ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": tray_type, "tray_color": color}]}]}, **kw}


META = {"tray": 1, "color": "#F74E02"}


class SendGuard(unittest.TestCase):
    def test_idle_matching_tray_ok(self):
        self.assertIsNone(printer.send_blocker(state(), META, "PLA"))

    def test_active_job_never_overridable(self):
        for st in ("RUNNING", "PAUSE", "PREPARE", "SLICING", None):
            self.assertIsNotNone(printer.send_blocker(state(st), META, "PLA", plate_clear=True, any_color=True), st)

    def test_finished_needs_plate_clear(self):
        for st in ("FINISH", "FAILED"):
            self.assertIn("--plate-clear", printer.send_blocker(state(st), META, "PLA"))
            self.assertIsNone(printer.send_blocker(state(st), META, "PLA", plate_clear=True))

    def test_material_mismatch_not_overridable(self):
        why = printer.send_blocker(state(tray_type="PETG"), META, "PLA", plate_clear=True, any_color=True)
        self.assertIn("re-slice", why)

    def test_colour_mismatch_needs_any_color(self):
        self.assertIn("--any-color", printer.send_blocker(state(color="00FF00FF"), META, "PLA"))
        self.assertIsNone(printer.send_blocker(state(color="00FF00FF"), META, "PLA", any_color=True))

    def test_empty_tray_and_error(self):
        self.assertIn("empty", printer.send_blocker(state(tray_type=""), META, "PLA"))
        self.assertIn("error", printer.send_blocker(state(print_error=123), META, "PLA"))


def ams(exist="f", bbl="d", st="IDLE"):
    return {"gcode_state": st, "ams": {"ams": [{"id": "0", "tray": []}], "tray_exist_bits": exist,
                                       "tray_is_bbl_bits": bbl}}


class TraySetting(unittest.TestCase):
    def test_untagged_loaded_tray_ok(self):
        self.assertIsNone(printer.tray_set_blocker(ams(), 2))

    def test_tagged_spool_refused(self):
        self.assertIn("tagged", printer.tray_set_blocker(ams(), 1))

    def test_unloaded_tray_and_out_of_range_refused(self):
        self.assertIn("no filament", printer.tray_set_blocker(ams(exist="d"), 2))
        self.assertIn("no tray", printer.tray_set_blocker(ams(), 5))
        self.assertIn("no tray", printer.tray_set_blocker(ams(), 0))

    def test_active_job_refused(self):
        self.assertIn("between jobs", printer.tray_set_blocker(ams(st="RUNNING"), 2))

    def test_command_body(self):
        fil = {"filament_id": "GFL99", "filament_type": ["PLA"], "nozzle_temperature_range_low": ["190"],
               "nozzle_temperature_range_high": ["240"]}
        b = printer.tray_setting(6, fil, "#abc123")
        self.assertEqual((b["ams_id"], b["tray_id"]), (1, 1))
        self.assertEqual((b["tray_info_idx"], b["tray_type"], b["tray_color"]), ("GFL99", "PLA", "ABC123FF"))
        self.assertEqual((b["nozzle_temp_min"], b["nozzle_temp_max"]), (190, 240))


class StubPrinter:
    """Printer stand-in: `after` is the state the printer reaches once a command is sent
    (None: it never changes)."""

    def __init__(self, before, after, reply=None):
        self.state, self.after, self.reply = {"gcode_state": before}, after, reply

    def status(self):
        return dict(self.state)

    def _cmd(self):
        if self.after:
            self.state["gcode_state"] = self.after
        return self.reply

    pause = resume = stop = _cmd

    def wait_for(self, pred, seconds):
        return dict(self.state) if pred(self.state) else None

    def close(self):
        pass


class Control(unittest.TestCase):
    def run_control(self, name, stub):
        with mock.patch.object(printer.bl, "Printer", lambda: stub):
            printer._control(name)(None)

    def test_stop_unconfirmed_fails_loudly(self):
        with self.assertRaises(SystemExit) as e:
            self.run_control("stop", StubPrinter("RUNNING", None))
        self.assertIn("NOT confirmed", str(e.exception))

    def test_stop_confirmed(self):
        self.run_control("stop", StubPrinter("RUNNING", "FAILED", {"result": "success"}))

    def test_pause_when_idle_refused(self):
        with self.assertRaises(SystemExit):
            self.run_control("pause", StubPrinter("IDLE", "PAUSE"))


class Checkpoint(unittest.TestCase):
    def run_follow(self, states):
        with mock.patch.object(printer.bl, "follow", lambda minutes: iter(states)), \
             mock.patch.object(printer.bl, "snapshot", lambda out: out), \
             mock.patch("builtins.print"):
            return printer.wait_for_layer(3, 1, "x.jpg")

    def test_stale_finish_ignored_then_photo_at_layer(self):
        s = self.run_follow([{"gcode_state": "FINISH", "layer_num": 80, "subtask_name": "old"},
                             {"gcode_state": "PREPARE", "layer_num": 0, "subtask_name": "old"},
                             {"gcode_state": "RUNNING", "layer_num": 1, "subtask_name": "new"},
                             {"gcode_state": "RUNNING", "layer_num": 3, "subtask_name": "new"}])
        self.assertEqual(s["layer_num"], 3)

    def test_other_job_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.run_follow([{"gcode_state": "RUNNING", "layer_num": 1, "subtask_name": "a"},
                             {"gcode_state": "RUNNING", "layer_num": 3, "subtask_name": "b"}])

    def test_job_ending_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.run_follow([{"gcode_state": "RUNNING", "layer_num": 1, "subtask_name": "a"},
                             {"gcode_state": "FAILED", "layer_num": 1, "subtask_name": "a"}])


class Identity(unittest.TestCase):
    def test_wrong_cn_refused(self):
        s = mock.Mock()
        s.getpeercert.return_value = {"subject": ((("commonName", "OTHER"),),)}
        with self.assertRaises(bl.IdentityError):
            bl._check_peer(s, "MINE")
        s.close.assert_called()

    def test_right_cn_accepted(self):
        s = mock.Mock()
        s.getpeercert.return_value = {"subject": ((("commonName", "MINE"),),)}
        bl._check_peer(s, "MINE")

    def test_verification_is_on(self):
        with mock.patch.object(bl.os.path, "exists", return_value=True), \
             mock.patch.object(bl.ssl, "create_default_context") as cdc:
            ctx = bl._ctx()
        cdc.assert_called_with(cafile=bl.STUDIO_CA)
        self.assertNotEqual(ctx.verify_mode, bl.ssl.CERT_NONE)


class Profiles(unittest.TestCase):
    def test_nested_include_and_loop(self):
        files = {"a": {"inherits": "b", "include": ["t"], "x": 1},
                 "b": {"y": 2}, "t": {"include": ["u"], "z": 3}, "u": {"w": 4},
                 "loop": {"inherits": "loop2"}, "loop2": {"include": ["loop"]}}
        with mock.patch.object(profiles, "_raw", lambda kind, name: dict(files[name])):
            d = profiles.resolve("filament", "a")
            self.assertEqual((d["x"], d["y"], d["z"], d["w"]), (1, 2, 3, 4))
            with self.assertRaises(SystemExit):
                profiles.resolve("filament", "loop")


if __name__ == "__main__":
    unittest.main()
