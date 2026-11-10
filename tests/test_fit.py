"""Offline tests for the purge-object sizing (experimental/purge_object/fit.py, gcmodel.py)."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "experimental", "purge_object"))
import fit  # noqa: E402
import gcmodel as gc  # noqa: E402


def box_rows(height=40.0, lh=0.2, wall=2.0, area=8.0):
    """A straight prism: every layer above layer 1 extrudes `wall` + `area` mm^3."""
    rows, w, a = [], 0.0, 0.0
    n = int(round(height / lh))
    for k in range(2, n + 1):
        w += wall
        a += area
        rows.append((round(k * lh, 4), w, a))
    return rows


class ModelTest(unittest.TestCase):
    def test_capacity_scales_walls_by_a_and_area_by_a2(self):
        m = fit.Model(box_rows(), [10.0], [100.0], 25.0, 100.0, 1.0)
        w, a = 2.0 * 49, 8.0 * 49                 # layers 2..50 = up to 10 mm
        self.assertAlmostEqual(m.capacity(1, 1, 10.0), w + a)
        self.assertAlmostEqual(m.capacity(2, 1, 10.0), 2 * w + 4 * a)
        # Z x2: twice the layers below 20 mm, each like the scale-1 layer at half the height
        self.assertAlmostEqual(m.capacity(1, 2, 20.0), 2 * (w + a))

    def test_uniform_scale_is_the_smallest_that_fits(self):
        rows = box_rows()
        total1 = rows[-1][1] + rows[-1][2]
        m = fit.Model(rows, [30.0], [0.5 * total1], 25.0, 100.0, 1.0)
        a, b, how = fit.choose(m, 0.5, 2.5)
        self.assertEqual((a, b - a, how), (a, 0, "uniform"))
        self.assertLess(a, 1.0)                    # half the volume needed: it shrinks
        self.assertTrue(m.fits(a, b))
        self.assertFalse(m.fits(round(a - 0.01, 2), round(b - 0.01, 2)))

    def test_height_limit_makes_it_wider_not_taller(self):
        rows = box_rows()
        total1 = rows[-1][1] + rows[-1][2]
        # needs 1.5x the volume but may not grow taller than 42 mm (main 40 + lead 2)
        m = fit.Model(rows, [39.0], [1.5 * total1], 2.0, 42.0, 1.0)
        a, b, how = fit.choose(m, 0.5, 2.5)
        self.assertNotEqual(how, "uniform")
        self.assertLessEqual(b * m.top1, 42.0 + 1e-6)
        self.assertGreater(a, b)
        self.assertLessEqual(a, 1.5 * b + 1e-9)
        self.assertTrue(m.fits(a, b))

    def test_early_changes_bind_low_down(self):
        rows = box_rows()
        # an early change at 4 mm with lead 2 must fit below 6 mm: 29 layers of 10 mm^3 = 290
        m = fit.Model(rows, [4.0], [400.0], 2.0, 42.0, 1.0)
        self.assertFalse(m.fits(1, 1))
        self.assertTrue(m.problems(1, 1)[0].startswith("change 1"))

    def test_nothing_fits(self):
        m = fit.Model(box_rows(), [4.0], [1e6], 2.0, 42.0, 1.0)
        self.assertIsNone(fit.choose(m, 0.5, 2.5))


GCODE = """; layer num/total_layer_count: 1/3
; FEATURE: Outer wall
G1 X1 Y0 E1
; layer num/total_layer_count: 2/3
; FEATURE: Inner wall
G1 X2 Y0 E2
; FEATURE: Sparse infill
G1 X3 Y0 E3
G1 E-0.8
M620 S1A
M621 S1A
; layer num/total_layer_count: 3/3
;TYPE:Gap infill
G1 X4 Y0 E4
M620 S0A
M621 S0A
"""


class GcodeTest(unittest.TestCase):
    def test_layer_volumes_split_walls_and_area(self):
        lines = GCODE.replace("; layer num", "; Z_HEIGHT: 0.2\n; layer num", 1).split("\n")
        v = gc.layer_volumes(lines)
        self.assertAlmostEqual(v[1][1], 1 * gc.FIL_AREA)
        self.assertAlmostEqual(v[2][1], 2 * gc.FIL_AREA)      # inner wall
        self.assertAlmostEqual(v[2][2], 3 * gc.FIL_AREA)      # infill; the retract is not counted
        self.assertAlmostEqual(v[3][1], 4 * gc.FIL_AREA)      # gap infill counts as wall

    def test_filament_changes(self):
        pairs, changes = gc.filament_changes(GCODE.split("\n"))
        self.assertEqual(pairs, [(None, 1), (1, 0)])
        self.assertEqual([c[0] for c in changes], [2, 3])


if __name__ == "__main__":
    unittest.main()
