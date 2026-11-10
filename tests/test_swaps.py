"""Offline tests for the swap report (experimental/purge_object/swaps.py)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "experimental", "purge_object"))
import swaps  # noqa: E402
import gcmodel as gc  # noqa: E402


def gcode(n_layers=30, band=(20, 22), flush_len=None, band2=None):
    """Filament 0 every layer; filament 1 only on layers band[0]..band[1] (and band2 if given),
    alternating order like a slicer (the last color of a layer prints first on the next).
    flush_len: the M620.10 L value to write (None: a zero-flush slice, 'L0')."""
    out = ["M620 S0A", "M621 S0A"]                       # start-of-print load
    L = f"L{flush_len}" if flush_len else "L0"
    cur = 0
    for k in range(1, n_layers + 1):
        out += [f"; Z_HEIGHT: {k * 0.2:.1f}", f"; layer num/total_layer_count: {k}/{n_layers}"]
        inband = band[0] <= k <= band[1] or (band2 and band2[0] <= k <= band2[1])
        order = [cur, 1 - cur] if inband else [0]     # outside a band: back to 0 first
        for f in order:
            if f != cur:
                out += [f"M620 S{f}A", f"M620.10 A0 F500 {L} H0.4", f"M621 S{f}A"]
                cur = f
            out += ["G1 X0 Y0", f"G1 X10 Y0 E{1 if f == 0 else 0.5}"]
    return out


MATRIX = [[0, 300], [100, 0]]


class SwapsTest(unittest.TestCase):
    def test_band_swaps_and_matrix_flush(self):
        r = swaps.analyze(gcode(), MATRIX)
        # 0->1 at 20, 1->0 at 21, 0->1 at 22, back to 0 at the start of 23
        self.assertEqual([s["layer"] for s in r["swaps"]], [20, 21, 22, 23])
        self.assertEqual([s["flush"] for s in r["swaps"]], [300, 100, 300, 100])
        self.assertEqual(r["total"], 800)
        self.assertEqual(len(r["bands"]), 1)
        self.assertEqual(r["bands"][0]["only_here"], [1])
        self.assertAlmostEqual(r["layer_height"], 0.2)

    def test_slice_flush_length_wins_over_matrix(self):
        r = swaps.analyze(gcode(flush_len=10), MATRIX)
        self.assertTrue(all(s["source"] == "slice" for s in r["swaps"]))
        self.assertAlmostEqual(r["swaps"][0]["flush"], 10 * gc.FIL_AREA)

    def test_filament_use_ranges(self):
        r = swaps.analyze(gcode(), MATRIX)
        f1 = r["filaments"][1]
        self.assertAlmostEqual(f1["z0"], 4.0)
        self.assertAlmostEqual(f1["z1"], 4.4)
        self.assertEqual(f1["layers"], 3)
        self.assertNotIn(0, r["bands"][0]["only_here"])    # filament 0 prints every layer

    def test_separate_bands(self):
        r = swaps.analyze(gcode(n_layers=200, band=(5, 6), band2=(180, 182)), MATRIX)
        self.assertEqual(len(r["bands"]), 2)
        self.assertEqual([len(b["swaps"]) for b in r["bands"]], [2, 4])
        self.assertEqual(r["bands"][0]["only_here"], [])   # filament 1 is in both bands
        self.assertEqual(r["bands"][1]["only_here"], [])

    def test_no_matrix_no_flush(self):
        r = swaps.analyze(gcode())
        self.assertTrue(all(s["source"] == "none" and s["flush"] == 0 for s in r["swaps"]))


if __name__ == "__main__":
    unittest.main()
