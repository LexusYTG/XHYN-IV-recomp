from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[3] / "LibertyRecompLib" / "shader_overrides"
TFETCH = re.compile(r"\btfetch(?:2DBicubic|2DArray|2D|3D|Cube)\(")
SAMPLER_SLOT = re.compile(
    r"#define (\w+)_SamplerDescriptorIndex vk::RawBufferLoad<uint>"
    r"\(g_PushConstants.SharedConstants \+ (\d+)\)"
)


def arguments_at(source: str, opening: int) -> str:
    depth = 1
    position = opening + 1
    while depth:
        if source[position] == "(":
            depth += 1
        elif source[position] == ")":
            depth -= 1
        position += 1
    return source[opening + 1 : position - 1]


class OverrideSamplerLodBiasTest(unittest.TestCase):
    def setUp(self) -> None:
        manifest = json.loads((SOURCE_ROOT / "manifest.json").read_text())
        self.sources = {
            entry["source"]: (SOURCE_ROOT / entry["source"]).read_text()
            for entry in manifest["overrides"]
        }

    def test_every_texture_fetch_uses_its_guest_slot_not_its_descriptor_index(self) -> None:
        checked = 0
        for name, source in self.sources.items():
            slots = {
                sampler: (int(offset) - 416) // 4
                for sampler, offset in SAMPLER_SLOT.findall(source)
            }
            for match in TFETCH.finditer(source):
                arguments = arguments_at(source, match.end() - 1)
                if "uint resourceDescriptorIndex" in arguments:
                    continue
                with self.subTest(source=name, call=arguments):
                    sampler = re.search(r"\b(\w+)_SamplerDescriptorIndex\b", arguments)
                    self.assertIsNotNone(sampler)
                    expected_slot = slots[sampler.group(1)]
                    self.assertGreaterEqual(expected_slot, 0)
                    self.assertLess(expected_slot, 26)
                    bias_slots = re.findall(r"GetTextureLodBias\((\d+)\)", arguments)
                    self.assertEqual(bias_slots, [str(expected_slot)])
                    checked += 1
        self.assertGreater(checked, 0)

    def test_implicit_sampling_carries_bias_through_every_embedded_common_copy(self) -> None:
        sources = dict(self.sources)
        sources["xenos_shader_common.h"] = (SOURCE_ROOT / "xenos_shader_common.h").read_text()
        for name, source in sources.items():
            with self.subTest(source=name):
                self.assertNotRegex(source, r"\.Sample\(")
                for match in re.finditer(r"\.SampleBias\(", source):
                    arguments = arguments_at(source, match.end() - 1)
                    if name.startswith("bloom/"):
                        self.assertIn(
                            "const float lod_bias = vk::RawBufferLoad<float>"
                            "(g_PushConstants.SharedConstants + 752);", source
                        )
                        self.assertTrue(arguments.endswith(", lod_bias"), arguments)
                    else:
                        self.assertTrue(arguments.endswith(", lodBias"), arguments)

    def test_bias_payload_matches_the_appended_shared_constant_registers(self) -> None:
        header = (SOURCE_ROOT / "xenos_shader_common.h").read_text()
        self.assertIn("g_PushConstants.SharedConstants + 752 + (SLOT) * 4", header)
        self.assertIn("float4 g_TextureLodBias[7] : packoffset(c47);", header)
        self.assertEqual(47 * 16, 752)
        self.assertEqual((26 + 2) * 4, 7 * 16)

    def test_explicit_helpers_add_bias_without_changing_sampling_mode(self) -> None:
        sources = dict(self.sources)
        sources["xenos_shader_common.h"] = (SOURCE_ROOT / "xenos_shader_common.h").read_text()
        for name, source in sources.items():
            if "float lodBias = 0.0" not in source:
                continue
            with self.subTest(source=name):
                helpers = 0
                slots = {
                    sampler: (int(offset) - 416) // 4
                    for sampler, offset in SAMPLER_SLOT.findall(source)
                }
                for match in re.finditer(r"\.SampleLevel\(", source):
                    arguments = arguments_at(source, match.end() - 1)
                    if "g_SamplerDescriptorHeap[samplerDescriptorIndex]" in arguments:
                        self.assertTrue(arguments.endswith(", 0.0 + lodBias"), arguments)
                        helpers += 1
                    else:
                        sampler = re.search(r"\b(\w+)_SamplerDescriptorIndex\b", arguments)
                        self.assertIsNotNone(sampler)
                        expected_slot = slots[sampler.group(1)]
                        self.assertTrue(
                            arguments.endswith(f", 0.0 + GetTextureLodBias({expected_slot})"),
                            arguments,
                        )
                self.assertEqual(helpers, 4)

    def test_fog_explicit_sampling_adds_the_corresponding_guest_slot_bias(self) -> None:
        source = self.sources["fog/rage_postfx_exponential_height_fog.hlsl"]
        for sampler in ("hdr", "depth"):
            descriptor = re.search(
                rf"const uint {sampler}_sampler_index =\s*"
                r"vk::RawBufferLoad<uint>\(g_PushConstants.SharedConstants \+ (0x[0-9A-F]+)\)",
                source,
            )
            bias = re.search(
                rf"const float {sampler}_sampler_lod_bias =\s*"
                r"vk::RawBufferLoad<float>\(g_PushConstants.SharedConstants \+ (\d+)\)",
                source,
            )
            self.assertIsNotNone(descriptor)
            self.assertIsNotNone(bias)
            slot = (int(descriptor.group(1), 16) - 416) // 4
            self.assertEqual(int(bias.group(1)), 752 + slot * 4)
            self.assertIn(
                f"{sampler}_texture.SampleLevel({sampler}_sampler, r0.xy, "
                f"0.0 + {sampler}_sampler_lod_bias)", source
            )
        self.assertEqual(source.count(".SampleLevel("), 2)
        self.assertNotIn(".SampleBias(", source)


if __name__ == "__main__":
    unittest.main()
