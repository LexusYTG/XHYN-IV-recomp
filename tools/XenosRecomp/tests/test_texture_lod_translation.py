"""Exercise the built GTA IV translator using original and mutated containers.

Run after building XenosRecomp:
    python3 -m unittest discover -s tools/XenosRecomp/tests -p test_texture_lod_translation.py
Set XENOS_RECOMP_TEST_BINARY to test a different translator executable.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "LibertyRecompLib/shader/rage_shaders"
COMMON = ROOT / "tools/XenosRecomp/XenosRecomp/shader_common.h"
BINARY = Path(os.environ.get("XENOS_RECOMP_TEST_BINARY", str(
    ROOT / "out/build/xenosrecomp-macos/XenosRecomp/XenosRecomp")))


def shader_path(name: str) -> Path:
    return CORPUS / name.rsplit("_", 1)[0] / (name + ".bin")


def instruction_offset(data: bytes, instruction: int, word: int = 0) -> int:
    header = struct.unpack_from(">9I", data)
    physical_offset, size = struct.unpack_from(">2I", data, header[6])
    offset = instruction * 12 + word * 4
    if not (0 <= word < 3 and 0 <= offset and offset + 4 <= size):
        raise ValueError("instruction outside shader code")
    return header[1] + physical_offset + offset


def replace_bits(data: bytes, instruction: int, word: int,
                 shift: int, width: int, value: int) -> bytes:
    offset = instruction_offset(data, instruction, word)
    original = struct.unpack_from(">I", data, offset)[0]
    mask = ((1 << width) - 1) << shift
    changed = bytearray(data)
    struct.pack_into(">I", changed, offset,
                     (original & ~mask) | ((value << shift) & mask))
    return bytes(changed)


class TextureLodTranslationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BINARY.is_file():
            raise RuntimeError(f"Build the GTA IV XenosRecomp test binary first: {BINARY}")

    def translate(self, name: str, data: bytes | None = None) -> str:
        with tempfile.TemporaryDirectory(prefix="liberty-lod-test-") as temporary:
            destination = Path(temporary) / "output.hlsl"
            source = shader_path(name)
            if data is not None:
                source = Path(temporary) / "input.bin"
                source.write_bytes(data)
            subprocess.run([str(BINARY), str(source), str(destination), str(COMMON)],
                           check=True, capture_output=True, text=True, timeout=30)
            output = destination.read_text()
        # Only inspect executable shader code, excluding shared helper definitions.
        return output[output.index("\tint a0 = 0;"):]

    def test_original_deferred_reflections_keep_both_predicated_lod_updates(self) -> None:
        source = self.translate("deferred_lighting_ps2")
        self.assertRegex(source, r"if \(p0\)\s*\{\s*textureLod = r1\.x;\s*\}")
        self.assertRegex(source, r"if \(!p0\)\s*\{\s*textureLod = r1\.z;\s*\}")
        self.assertEqual(source.count("tfetch2DLod("), 2)
        self.assertEqual(source.count("textureLod + GetTextureLodBias(6)"), 2)
        self.assertLess(source.index("textureLod = r1.x;"), source.index("tfetch2DLod("))

    def test_original_vehicle_glass_keeps_the_unpredicated_snapshot(self) -> None:
        source = self.translate("gta_vehicle_vehglass_ps0")
        self.assertIn("\n\ttextureLod = r4.w;\n", source)
        self.assertEqual(source.count("tfetch2DLod("), 1)
        self.assertIn("textureLod + GetTextureLodBias(4)", source)
        self.assertLess(source.index("textureLod = r4.w;"), source.index("tfetch2DLod("))

    def test_all_original_register_lod_families_use_explicit_sampling(self) -> None:
        expected = {
            **{f"deferred_lighting_ps{i}": 2 for i in (0, 1, 2, 3, 14, 15)},
            "deferred_lighting_ps27": 1,
            **{f"{family}_ps{i}": 1 for family in
               ("gta_vehicle_badges", "gta_vehicle_lightsemissive", "gta_vehicle_vehglass")
               for i in range(3)},
        }
        for name, count in expected.items():
            with self.subTest(shader=name):
                source = self.translate(name)
                self.assertEqual(source.count("tfetch2DLod("), count)
                self.assertEqual(len(re.findall(r"textureLod = r\d+\.[xyzw];", source)), count)
                self.assertEqual(source.count("textureLod + GetTextureLodBias("), count)

    def test_pixel_base_level_fetches_do_not_gain_implicit_derivatives(self) -> None:
        for name in ("gta_trees_ps2", "shadowsmartblit_ps4", "shadowsmartblit_ps5"):
            with self.subTest(shader=name):
                source = self.translate(name)
                self.assertIn("tfetch2DLod(", source)
                self.assertNotIn("textureLod + GetTextureLodBias", source)

    def test_existing_implicit_material_fetches_remain_implicit(self) -> None:
        for name in ("mirror_ps0", "water_ps0", "water_ps1", "gta_cutout_fence_ps2",
                     "gta_cutout_fence_ps3", "gta_reflect_ps2", "gta_cubemap_reflect_ps2"):
            with self.subTest(shader=name):
                source = self.translate(name)
                self.assertRegex(source, r"tfetch(?:2D|Cube)\(")
                self.assertNotRegex(source, r"tfetch\w+Lod\(")
                self.assertNotRegex(source, r"textureLod = r\d+")

    def test_vertex_fetches_still_sample_explicit_base_level(self) -> None:
        source = self.translate("water_vs0")
        self.assertEqual(source.count("tfetch2DLod("), 5)
        self.assertNotIn("textureLod +", source)
        self.assertNotIn("tfetch2D(", source)

    def test_lod_state_is_initialized_per_invocation(self) -> None:
        source = self.translate("deferred_lighting_ps2")
        self.assertEqual(source.count("float textureLod = 0.0;"), 1)
        self.assertLess(source.index("float textureLod = 0.0;"), source.index("textureLod = r1.x;"))

    def test_set_lod_uses_encoded_source_component(self) -> None:
        original = shader_path("deferred_lighting_ps2").read_bytes()
        for component, suffix in enumerate("xyzw"):
            with self.subTest(component=suffix):
                changed = replace_bits(original, 42, 0, 26, 2, component)
                source = self.translate("deferred_lighting_ps2", changed)
                self.assertRegex(source, rf"if \(p0\)\s*\{{\s*textureLod = r1\.{suffix};\s*\}}")

    def test_set_lod_keeps_encoded_predicate_polarity(self) -> None:
        original = shader_path("deferred_lighting_ps2").read_bytes()
        changed = replace_bits(original, 42, 2, 31, 1, 0)
        source = self.translate("deferred_lighting_ps2", changed)
        self.assertRegex(source, r"if \(!p0\)\s*\{\s*textureLod = r1\.x;\s*\}")

    def test_computed_lod_bit_selects_mode_independently_of_register_bias(self) -> None:
        original = shader_path("deferred_lighting_ps2").read_bytes()
        for computed in (0, 1):
            for register in (0, 1):
                with self.subTest(computed=computed, register=register):
                    changed = replace_bits(original, 43, 1, 28, 1, computed)
                    changed = replace_bits(changed, 43, 1, 29, 1, register)
                    source = self.translate("deferred_lighting_ps2", changed)
                    call = re.search(r"r5\.xyz = (tfetch2D(?:Lod)?)\((.*?)\)\.xyz;",
                                     source, re.DOTALL)
                    self.assertIsNotNone(call)
                    self.assertEqual(call.group(1), "tfetch2D" if computed else "tfetch2DLod")
                    self.assertEqual("textureLod +" in call.group(2), bool(register))
                    self.assertEqual(call.group(2).count("GetTextureLodBias(6)"), 1)

    def test_instruction_bias_is_signed_and_applied_once_in_both_modes(self) -> None:
        original = shader_path("deferred_lighting_ps2").read_bytes()
        for computed in (0, 1):
            for encoded, text in ((-64, "-4"), (-8, "-0.5"), (8, "0.5"), (63, "3.9375")):
                with self.subTest(computed=computed, bias=encoded):
                    changed = replace_bits(original, 43, 1, 28, 1, computed)
                    changed = replace_bits(changed, 43, 2, 2, 7, encoded)
                    source = self.translate("deferred_lighting_ps2", changed)
                    self.assertIn(f"textureLod + GetTextureLodBias(6) + ({text})", source)

    def test_zero_instruction_bias_does_not_add_an_extra_term(self) -> None:
        source = self.translate("deferred_lighting_ps2")
        self.assertNotIn("GetTextureLodBias(6) + (", source)

    def test_explicit_helpers_use_level_in_hlsl_and_metal(self) -> None:
        source = COMMON.read_text()
        for dimension in ("2D", "3D", "Cube"):
            helpers = re.findall(rf"float4 tfetch{dimension}Lod\([^{{]+\{{(.*?)\n\}}",
                                 source, re.DOTALL)
            self.assertEqual(len(helpers), 2)
            self.assertTrue(any(".SampleLevel(" in helper for helper in helpers))
            self.assertTrue(any("level(lod)" in helper for helper in helpers))
            for helper in helpers:
                self.assertNotIn(".SampleBias(", helper)
                self.assertNotIn("bias(lod)", helper)


if __name__ == "__main__":
    unittest.main()
