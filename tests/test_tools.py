"""Offline tests for tools/jt2stl (codec pieces) and tools/pinsplit (solid rebuild + pins)."""
import math, os, sys, unittest
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "jt2stl"))
sys.path.insert(0, os.path.join(ROOT, "tools", "pinsplit"))
import jtmesh  # noqa: E402


def words_from_bits(bits):
    bits = bits + "0" * (-len(bits) % 32)
    return np.array([int(bits[i:i + 32], 2) for i in range(0, len(bits), 32)], dtype=np.uint32)


class JtCodecs(unittest.TestCase):
    def test_bitlength_fixed_width(self):
        # fixed-width flag, nibbler(2), nibbler(5), then 2-bit codes for 2, 5, 3
        bits = "0" + "0010" + "0" + "0101" + "0" + "00" + "11" + "01"
        cdp = jtmesh.BitlengthCDP(3, words_from_bits(bits), len(bits))
        self.assertEqual(cdp.decode().tolist(), [2, 5, 3])

    def test_bitlength_checks_bit_count(self):
        bits = "0" + "0010" + "0" + "0101" + "0" + "00" + "11" + "01"
        cdp = jtmesh.BitlengthCDP(3, words_from_bits(bits), len(bits) + 1)
        with self.assertRaises(ValueError):
            cdp.decode()

    def test_move_to_front(self):
        vals = jtmesh.NullCDP(np.array([7, 9], dtype=np.uint32))
        offs = jtmesh.NullCDP(np.array([-1, 0, -1, 1, 0], dtype=np.int64).astype(np.uint32))
        self.assertEqual(jtmesh.MTFCDP(5, vals, offs).decode().tolist(), [7, 7, 9, 7, 7])

    def test_lag1_starts_after_four_primers(self):
        self.assertEqual(jtmesh.unpack_lag1([1, 2, 3, 4, 1, 1]).tolist(), [1, 2, 3, 4, 5, 6])


try:
    import trimesh, igl, skimage  # noqa: F401
    import pinsplit
    HAVE_PINSPLIT = True
except ImportError:
    HAVE_PINSPLIT = False


@unittest.skipUnless(HAVE_PINSPLIT, "pinsplit needs trimesh, libigl and scikit-image")
class PinSplit(unittest.TestCase):
    def test_overlapping_boxes_mixed_orientation(self):
        a = trimesh.creation.box(bounds=[[-10, -10, 0], [10, 10, 10]])
        b = trimesh.creation.box(bounds=[[5, -5, 0], [20, 5, 10]])
        b.invert()  # inside-out shell overlapping the first: the rebuild must not care
        m = trimesh.util.concatenate([a, b])
        cfg = {"pieces": {"left": ["x", "<", 0], "right": ["x", ">", 0]}, "clearance": 0.15}
        body = pinsplit.Body(m.vertices, m.faces, 2.0)
        joint = {"axis": "x", "at": 0, "pieces": ["left", "right"], "d": 3, "depth": 4,
                 "count": 1, "min_spacing": 5, "wall": 1.0}
        pts, _ = pinsplit.place_pins(body, joint, cfg["pieces"], 0.5)
        self.assertEqual(len(pts), 1)
        joints = [dict(joint, pins=pts)]
        left = pinsplit.build_piece("left", body, cfg, joints, 0.5)
        right = pinsplit.build_piece("right", body, cfg, joints, 0.5)
        self.assertTrue(left.is_watertight and right.is_watertight)
        hole = math.pi * 1.65 ** 2 * 4
        self.assertAlmostEqual(left.volume, 2000 - hole, delta=40)
        self.assertAlmostEqual(right.volume, 3000 - hole, delta=60)


if __name__ == "__main__":
    unittest.main()
