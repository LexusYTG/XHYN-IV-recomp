from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock


COMPILER_PATH = Path(__file__).resolve().parents[1] / "shader_override_compiler.py"
EXAMPLE_ROOT = Path(__file__).resolve().parent / "shader_overrides"
SPEC = importlib.util.spec_from_file_location("shader_override_compiler", COMPILER_PATH)
assert SPEC is not None and SPEC.loader is not None
COMPILER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPILER)


class ShaderOverrideCompilerTest(unittest.TestCase):
    def test_checked_in_example_is_sorted_by_stage_then_hash(self) -> None:
        entries = COMPILER.load_manifest(EXAMPLE_ROOT / "manifest.json", EXAMPLE_ROOT)
        self.assertEqual(
            [(entry["stage_name"], entry["hash"]) for entry in entries],
            [("pixel", 2), ("vertex", 1)],
        )

    def test_empty_manifest_generates_deterministic_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "overrides": []}), encoding="utf-8")
            entries = COMPILER.load_manifest(manifest, root)
            first = COMPILER.generate_cpp(entries)
            second = COMPILER.generate_cpp(entries)
            self.assertEqual(first, second)
            self.assertIn("g_shaderOverrideEntryCount = 0", first)

    def test_manifest_rejects_source_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "overrides": [
                            {
                                "stage": "pixel",
                                "hash": "0x0000000000000001",
                                "language": "hlsl",
                                "source": "../escape.hlsl",
                                "entry_point": "main",
                                "specialization_constants_mask": "0x00000000",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(COMPILER.ManifestError):
                COMPILER.load_manifest(manifest, root)

    def test_spirv_stage_and_entry_point_are_checked(self) -> None:
        name = b"main\0"
        name += bytes((-len(name)) % struct.calcsize("<I"))
        name_words = struct.unpack(f"<{len(name) // struct.calcsize('<I')}I", name)
        instruction_word_count = 3 + len(name_words)
        words = (
            COMPILER.SPIRV_MAGIC,
            0x00010000,
            0,
            2,
            0,
            (instruction_word_count << 16) | COMPILER.SPIRV_OP_ENTRY_POINT,
            COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
            1,
            *name_words,
        )
        blob = struct.pack(f"<{len(words)}I", *words)
        COMPILER.validate_spirv(blob, COMPILER.SPIRV_EXECUTION_MODELS["pixel"], "main")
        with self.assertRaises(COMPILER.ManifestError):
            COMPILER.validate_spirv(blob, COMPILER.SPIRV_EXECUTION_MODELS["vertex"], "main")

        early_fragment_tests = (
            (3 << 16) | COMPILER.SPIRV_OP_EXECUTION_MODE,
            1,
            COMPILER.SPIRV_EXECUTION_MODE_EARLY_FRAGMENT_TESTS,
        )
        early_blob = struct.pack(
            f"<{len(words) + len(early_fragment_tests)}I",
            *words,
            *early_fragment_tests,
        )
        with self.assertRaisesRegex(COMPILER.ManifestError, "EarlyFragmentTests"):
            COMPILER.validate_spirv(
                early_blob,
                COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
                "main",
                forbid_early_fragment_tests=True,
            )

    def test_alpha_capable_pixel_shader_requires_late_variant(self) -> None:
        self.assertTrue(
            COMPILER.requires_late_fragment_tests(
                {
                    "stage_name": "pixel",
                    "specialization_constants_mask": COMPILER.SPEC_CONSTANT_ALPHA_TEST,
                }
            )
        )
        self.assertFalse(
            COMPILER.requires_late_fragment_tests(
                {
                    "stage_name": "pixel",
                    "specialization_constants_mask": 0,
                }
            )
        )
        self.assertFalse(
            COMPILER.requires_late_fragment_tests(
                {
                    "stage_name": "vertex",
                    "specialization_constants_mask": COMPILER.SPEC_CONSTANT_ALPHA_TEST,
                }
            )
        )

    def test_hlsl_pixel_compilation_matches_xenos_stage_define(self) -> None:
        entry = {
            "language": "hlsl",
            "stage_name": "pixel",
            "entry_point": "shaderMain",
            "source": EXAMPLE_ROOT / "example_vertex.hlsl",
            "source_name": "example_vertex.hlsl",
            "defines": (),
        }
        with mock.patch.object(COMPILER, "_run_tool") as run_tool:
            COMPILER.compile_shader(entry, Path("output.spv"), EXAMPLE_ROOT, "dxc", None, None)
        command = run_tool.call_args.args[0]
        self.assertIn("-DXENOS_RECOMP_PIXEL_SHADER", command)

    def test_hlsl_late_compilation_adds_late_fragment_tests_define(self) -> None:
        entry = {
            "language": "hlsl",
            "stage_name": "pixel",
            "entry_point": "shaderMain",
            "source": EXAMPLE_ROOT / "example_vertex.hlsl",
            "source_name": "example_vertex.hlsl",
            "defines": (),
        }
        with mock.patch.object(COMPILER, "_run_tool") as run_tool:
            COMPILER.compile_shader(
                entry,
                Path("output.spv"),
                EXAMPLE_ROOT,
                "dxc",
                None,
                None,
                ("XENOS_RECOMP_LATE_FRAGMENT_TESTS",),
            )
        command = run_tool.call_args.args[0]
        self.assertIn("-DXENOS_RECOMP_LATE_FRAGMENT_TESTS", command)

    def test_hlsl_vertex_compilation_does_not_define_pixel_stage(self) -> None:
        entry = {
            "language": "hlsl",
            "stage_name": "vertex",
            "entry_point": "shaderMain",
            "source": EXAMPLE_ROOT / "example_vertex.hlsl",
            "source_name": "example_vertex.hlsl",
            "defines": (),
        }
        with mock.patch.object(COMPILER, "_run_tool") as run_tool:
            COMPILER.compile_shader(entry, Path("output.spv"), EXAMPLE_ROOT, "dxc", None, None)
        command = run_tool.call_args.args[0]
        self.assertNotIn("-DXENOS_RECOMP_PIXEL_SHADER", command)


    @staticmethod
    def _make_spirv(
        *,
        execution_model: int,
        entry_point: str = "main",
        interface_ids: tuple[int, ...] = (),
        builtins: tuple[tuple[int, int], ...] = (),
        early_fragment_tests: bool = False,
    ) -> bytes:
        name = entry_point.encode("utf-8") + b"\0"
        name += bytes((-len(name)) % struct.calcsize("<I"))
        name_words = struct.unpack(f"<{len(name) // struct.calcsize('<I')}I", name)
        entry_point_id = 1
        entry_words = (
            ((3 + len(name_words) + len(interface_ids)) << 16) | COMPILER.SPIRV_OP_ENTRY_POINT,
            execution_model,
            entry_point_id,
            *name_words,
            *interface_ids,
        )
        instructions: list[int] = list(entry_words)
        if early_fragment_tests:
            instructions.extend(
                (
                    (3 << 16) | COMPILER.SPIRV_OP_EXECUTION_MODE,
                    entry_point_id,
                    COMPILER.SPIRV_EXECUTION_MODE_EARLY_FRAGMENT_TESTS,
                )
            )
        for target_id, builtin in builtins:
            instructions.extend(
                (
                    (4 << 16) | COMPILER.SPIRV_OP_DECORATE,
                    target_id,
                    COMPILER.SPIRV_DECORATION_BUILT_IN,
                    builtin,
                )
            )
        bound = max((entry_point_id, *interface_ids, *(target for target, _ in builtins))) + 1
        words = (
            COMPILER.SPIRV_MAGIC,
            0x00010000,
            0,
            bound,
            0,
            *instructions,
        )
        return struct.pack(f"<{len(words)}I", *words)

    def test_spirv_contract_checks_sample_mask_and_early_tests(self) -> None:
        sample_mask_id = 2
        blob = self._make_spirv(
            execution_model=COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
            interface_ids=(sample_mask_id,),
            builtins=((sample_mask_id, COMPILER.SPIRV_BUILT_IN_SAMPLE_MASK),),
            early_fragment_tests=True,
        )
        COMPILER.validate_spirv(
            blob,
            COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
            "main",
            required_builtins=("SampleMask",),
            require_early_fragment_tests=True,
        )
        missing_mask = self._make_spirv(
            execution_model=COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
            early_fragment_tests=True,
        )
        with self.assertRaisesRegex(COMPILER.ManifestError, "SampleMask"):
            COMPILER.validate_spirv(
                missing_mask,
                COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
                "main",
                required_builtins=("SampleMask",),
            )
        missing_early = self._make_spirv(
            execution_model=COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
            interface_ids=(sample_mask_id,),
            builtins=((sample_mask_id, COMPILER.SPIRV_BUILT_IN_SAMPLE_MASK),),
        )
        with self.assertRaisesRegex(COMPILER.ManifestError, "EarlyFragmentTests"):
            COMPILER.validate_spirv(
                missing_early,
                COMPILER.SPIRV_EXECUTION_MODELS["pixel"],
                "main",
                require_early_fragment_tests=True,
            )

    def test_pipeline_pair_manifest_requires_reciprocal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "vertex.hlsl").write_text("void shaderMain() {}", encoding="utf-8")
            (root / "pixel.hlsl").write_text("void shaderMain() {}", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "overrides": [
                            {
                                "stage": "vertex",
                                "hash": "0x0000000000000001",
                                "language": "hlsl",
                                "source": "vertex.hlsl",
                                "entry_point": "shaderMain",
                                "specialization_constants_mask": "0x00000000",
                                "activation": "pipeline_pair",
                                "pipeline_pair_id": 7,
                                "counterpart_hashes": ["0x0000000000000002"],
                                "used_texture_mask": "0x00000000",
                                "support_radius_bits": "0x3F59999A",
                            },
                            {
                                "stage": "pixel",
                                "hash": "0x0000000000000002",
                                "language": "hlsl",
                                "source": "pixel.hlsl",
                                "entry_point": "shaderMain",
                                "specialization_constants_mask": "0x00000002",
                                "activation": "pipeline_pair",
                                "pipeline_pair_id": 7,
                                "counterpart_hashes": ["0x0000000000000001"],
                                "used_texture_mask": "0x00000001",
                                "support_radius_bits": "0x3F59999A",
                                "required_builtins": ["SampleMask"],
                                "require_early_fragment_tests": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            entries = COMPILER.load_manifest(manifest, root)
            self.assertEqual(len(entries), 2)
            self.assertTrue(all(entry["pipeline_pair_id"] == 7 for entry in entries))
            generated_entries = []
            for entry in entries:
                compiled = dict(entry)
                compiled["words"] = (COMPILER.SPIRV_MAGIC,)
                compiled["late_words"] = None
                generated_entries.append(compiled)
            generated = COMPILER.generate_cpp(generated_entries)
            self.assertIn("kShaderOverrideActivationPipelinePair", generated)
            self.assertIn("kShaderOverrideCounterparts", generated)
            self.assertIn("0x3F59999Au", generated)

            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["overrides"][1]["counterpart_hashes"] = ["0x0000000000000003"]
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(COMPILER.ManifestError, "counterpart"):
                COMPILER.load_manifest(manifest, root)

    def test_pipeline_pair_rejects_fragment_support_outside_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "vertex.hlsl").write_text("void shaderMain() {}", encoding="utf-8")
            (root / "pixel.hlsl").write_text("void shaderMain() {}", encoding="utf-8")
            entries = []
            for stage, hash_value, counterpart, source_name, radius in (
                ("vertex", "0x0000000000000001", "0x0000000000000002", "vertex.hlsl", "0x3F000000"),
                ("pixel", "0x0000000000000002", "0x0000000000000001", "pixel.hlsl", "0x3F59999A"),
            ):
                entries.append(
                    {
                        "stage": stage,
                        "hash": hash_value,
                        "language": "hlsl",
                        "source": source_name,
                        "entry_point": "shaderMain",
                        "specialization_constants_mask": "0x00000000",
                        "activation": "pipeline_pair",
                        "pipeline_pair_id": 9,
                        "counterpart_hashes": [counterpart],
                        "support_radius_bits": radius,
                    }
                )
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "overrides": entries}), encoding="utf-8")
            with self.assertRaisesRegex(COMPILER.ManifestError, "contain"):
                COMPILER.load_manifest(manifest, root)


if __name__ == "__main__":
    unittest.main()
