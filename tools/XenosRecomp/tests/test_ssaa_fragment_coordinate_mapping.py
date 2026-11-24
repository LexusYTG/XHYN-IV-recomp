from __future__ import annotations

from pathlib import Path
import unittest


TRANSLATOR_PATH = Path(__file__).resolve().parents[1] / "XenosRecomp" / "shader_recompiler.cpp"
CENTER_PRESERVING_EXPRESSION = (
    "float2 guestPixelPosition = (input.iPos.xy - 0.5) * "
    "g_FragmentCoordinateScale + 0.5;"
)
OLD_ORIGIN_SCALED_EXPRESSION = (
    "float2 guestPixelPosition = input.iPos.xy * g_FragmentCoordinateScale;"
)


def map_physical_center_to_logical(position: float, scale: float) -> float:
    return (position - 0.5) * scale + 0.5


class SsaaFragmentCoordinateMappingTest(unittest.TestCase):
    def test_first_pixel_center_is_preserved(self) -> None:
        self.assertEqual(map_physical_center_to_logical(0.5, 0.5), 0.5)
        self.assertEqual(map_physical_center_to_logical(0.5, 1.0), 0.5)

    def test_identity_mapping_is_unchanged(self) -> None:
        for position in (0.5, 1.5, 63.5, 1919.5):
            self.assertEqual(map_physical_center_to_logical(position, 1.0), position)

    def test_translator_uses_center_preserving_mapping(self) -> None:
        source = TRANSLATOR_PATH.read_text(encoding="utf-8")
        self.assertIn(CENTER_PRESERVING_EXPRESSION, source)
        self.assertNotIn(OLD_ORIGIN_SCALED_EXPRESSION, source)


if __name__ == "__main__":
    unittest.main()
