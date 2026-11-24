"""Regression coverage for Boolean EXEC clauses using retail shader containers.

Tests invoke the built translator, including mutations of instruction gates.
No fixed shader strings or asset-name exceptions are added to production code.
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
BINARY = Path(os.environ.get('XENOS_RECOMP_TEST_BINARY', str(
    ROOT/'out/build/xenosrecomp-macos/XenosRecomp/XenosRecomp')))
COMMON = ROOT/'tools/XenosRecomp/XenosRecomp/shader_common.h'


def shader(name: str) -> Path:
    return ROOT/'LibertyRecompLib/shader/rage_shaders'/name.rsplit('_', 1)[0]/(name+'.bin')


def mutate_cf(data: bytes, index: int, *, opcode: int | None = None,
              boolean: int | None = None, condition: int | None = None) -> bytes:
    h = struct.unpack_from('>9I', data)
    physical, size = struct.unpack_from('>II', data, h[6])
    offset = h[1] + physical + (index // 2) * 12
    if offset + 12 > h[1] + physical + size:
        raise ValueError('control-flow index outside shader')
    a, b, c = struct.unpack_from('>III', data, offset)
    pair = [a | ((b & 65535) << 32), (b >> 16) | (c << 16)]
    cf = pair[index % 2]
    for value, shift, bits in ((opcode, 44, 4), (boolean, 34, 8), (condition, 42, 1)):
        if value is not None:
            if not 0 <= value < 1 << bits:
                raise ValueError('invalid field')
            cf = (cf & ~(((1 << bits)-1) << shift)) | (value << shift)
    pair[index % 2] = cf
    result = bytearray(data)
    struct.pack_into('>III', result, offset, pair[0] & 0xffffffff,
                     (pair[0] >> 32) | ((pair[1] & 65535) << 16), pair[1] >> 16)
    return bytes(result)


def translate(name: str, data: bytes | None = None) -> str:
    with tempfile.TemporaryDirectory(prefix='liberty-cexec-test-') as directory:
        source, output = shader(name), Path(directory)/'output.hlsl'
        if data is not None:
            source = Path(directory)/'input.bin'
            source.write_bytes(data)
        p = subprocess.run([str(BINARY), str(source), str(output), str(COMMON)],
                           capture_output=True, text=True, timeout=30)
        if p.returncode:
            raise RuntimeError(p.stdout + p.stderr)
        return output.read_text()


def scope(source: str) -> tuple[str, str, str]:
    match = re.search(r'// bool-exec[^\n]*\n\s*(if[^\n]*)\n\s*\{', source)
    if not match:
        raise AssertionError('missing Boolean EXEC guard')
    end = source.find('} // bool-exec', match.end())
    if end < 0:
        raise AssertionError('missing Boolean EXEC scope end')
    return match.group(1), source[match.end():end], source[end:]


def control_flow(data: bytes) -> list[dict[str, int]]:
    header = struct.unpack_from('>9I', data)
    physical, size = struct.unpack_from('>II', data, header[6])
    words = struct.unpack_from('>' + str(size // 4) + 'I', data, header[1] + physical)
    limit = len(words)
    result = []
    for group in range(len(words) // 3):
        if group * 3 >= limit:
            break
        a, b, c = words[group * 3:group * 3 + 3]
        for raw in (a | ((b & 65535) << 32), (b >> 16) | (c << 16)):
            op, address = (raw >> 44) & 15, raw & 4095
            if op in (1, 2, 3, 4, 5, 6, 13, 14) and address:
                limit = min(limit, address * 3)
            result.append(dict(index=len(result), opcode=op, address=address,
                               boolean=(raw >> 34) & 255, condition=(raw >> 42) & 1))
    return result


def replace_with_jump(data: bytes, index: int, target: int, *,
                      boolean: int = 0, condition: int = 0,
                      unconditional: bool = False) -> bytes:
    header = struct.unpack_from('>9I', data)
    physical = struct.unpack_from('>I', data, header[6])[0]
    offset = header[1] + physical + (index // 2) * 12
    a, b, c = struct.unpack_from('>III', data, offset)
    pair = [a | ((b & 65535) << 32), (b >> 16) | (c << 16)]
    pair[index % 2] = (11 << 44) | (condition << 42) | (boolean << 34) | (int(unconditional) << 13) | target
    result = bytearray(data)
    struct.pack_into('>III', result, offset, pair[0] & 0xffffffff,
                     (pair[0] >> 32) | ((pair[1] & 65535) << 16), pair[1] >> 16)
    return bytes(result)


class ConditionalExecTranslationTest(unittest.TestCase):
    def test_vertex_deck_angle_only_runs_for_false_b1(self):
        guard, body, tail = scope(translate('gta_normal_vs4'))
        self.assertEqual(guard, 'if ((g_Booleans & 2u) == 0u)')
        self.assertIn('ps = xenosMul(gWorldView(2).z, r0.y);', body)
        self.assertIn('r3.z = ps;', body)
        self.assertNotIn('output.oColor0', body)
        self.assertIn('output.oColor0', tail)

    def test_pixel_deferred_alpha_is_guarded_by_absolute_b129(self):
        guard, body, tail = scope(translate('gta_normal_ps2'))
        self.assertEqual(guard, 'if ((g_Booleans & 131072u) != 0u)')
        self.assertIn('ps = abs(r0.w) >= 0.0;', body)
        self.assertIn('output.oC1.w', tail)

    def test_default_deferred_pair_also_receives_real_gates(self):
        self.assertIn('& 2u) == 0u', scope(translate('gta_default_vs4'))[0])
        self.assertIn('& 131072u) != 0u', scope(translate('gta_default_ps2'))[0])

    def test_both_non_end_boolean_opcodes_and_polarities(self):
        data = shader('gta_normal_vs4').read_bytes()
        for op in (3, 13):
            for condition in (0, 1):
                with self.subTest(opcode=op, condition=condition):
                    source = translate('gta_normal_vs4', mutate_cf(data, 4, opcode=op, condition=condition))
                    guard, body, tail = scope(source)
                    self.assertEqual(guard, f'if ((g_Booleans & 2u) {"!=" if condition else "=="} 0u)')
                    self.assertNotIn('return output;', body)
                    self.assertIn('return output;', tail)

    def test_conditional_end_returns_only_inside_taken_scope(self):
        data = shader('gta_normal_vs4').read_bytes()
        for op in (4, 14):
            with self.subTest(opcode=op):
                guard, body, tail = scope(translate('gta_normal_vs4', mutate_cf(data, 4, opcode=op)))
                self.assertIn('return output;', body)
                self.assertIn('output.oPos', tail)
                self.assertIn('return output;', tail)

    def test_stage_and_ctab_do_not_rewrite_absolute_boolean_address(self):
        data = shader('gta_normal_vs4').read_bytes()
        for absolute, bit in ((0, 0), (8, 8), (11, 11), (15, 15), (128, 16), (129, 17), (143, 31)):
            with self.subTest(boolean=absolute):
                guard, _, _ = scope(translate('gta_normal_vs4', mutate_cf(data, 4, boolean=absolute)))
                self.assertEqual(guard, f'if ((g_Booleans & {1 << bit}u) == 0u)')

    def test_unsupported_boolean_address_fails_instead_of_silent_false(self):
        data = shader('gta_normal_vs4').read_bytes()
        for address in (16, 127, 144, 255):
            with self.subTest(boolean=address):
                with self.assertRaisesRegex(RuntimeError, 'Boolean address outside native'):
                    translate('gta_normal_vs4', mutate_cf(data, 4, boolean=address))

    def test_boolean_jump_keeps_stripped_high_register_and_branch_polarity(self):
        data = shader('gta_normal_vs4').read_bytes()
        for boolean in (8, 11):
            for condition in (0, 1):
                with self.subTest(boolean=boolean, condition=condition):
                    # Replace the allocation hint, not a shader ALU instruction.
                    mutated = replace_with_jump(data, 5, 7, boolean=boolean, condition=condition)
                    result = translate('gta_normal_vs4', mutated)
                    inverse = '==' if condition else '!='
                    self.assertIn(f'if ((g_Booleans & {1 << boolean}u) {inverse} 0u)', result)

    def test_conditional_end_in_dispatch_mode_does_not_break_on_false(self):
        data = shader('gta_normal_vs4').read_bytes()
        data = replace_with_jump(data, 5, 6, unconditional=True)
        for opcode in (4, 14):
            with self.subTest(opcode=opcode):
                result = translate('gta_normal_vs4', mutate_cf(data, 4, opcode=opcode))
                self.assertIn('switch (pc)', result)
                _, body, tail = scope(result)
                self.assertIn('break;', body)
                self.assertRegex(tail, r'} // bool-exec\s+case 5:')
                self.assertIn('case 6:', tail)
                self.assertIn('return output;', tail)

    def test_all_retail_boolean_exec_clauses_keep_exact_gates(self):
        checked = 0
        corpus = ROOT/'LibertyRecompLib/shader/rage_shaders'
        for path in sorted(corpus.rglob('*.bin')):
            data = path.read_bytes()
            if len(data) < 36 or data[:3] != bytes.fromhex('102a11'):
                continue
            clauses = [cf for cf in control_flow(data) if cf['opcode'] in (3, 4, 13, 14)]
            if not clauses:
                continue
            # Supply bytes explicitly so nested corpus entries are supported.
            result = translate(path.stem, data)
            matches = re.findall(r'// bool-exec cf=(\d+) address=\d+ count=\d+ b(\d+) expected=(\d+)\n\s*if \(\(g_Booleans & (\d+)u\) (==|!=) 0u\)', result)
            actual = {int(index): (int(boolean), int(condition), int(mask), compare)
                      for index, boolean, condition, mask, compare in matches}
            with self.subTest(shader=str(path.relative_to(corpus))):
                self.assertEqual(len(actual), len(clauses))
                for cf in clauses:
                    address = cf['boolean']
                    bit = address if address < 16 else address - 128 + 16
                    self.assertEqual(actual[cf['index']],
                                     (address, cf['condition'], 1 << bit,
                                      '!=' if cf['condition'] else '=='))
                    checked += 1
        self.assertGreater(checked, 0)

    def test_non_deferred_controls_keep_their_sampling_modes(self):
        for name in ('water_ps0', 'mirror_ps0', 'gta_normal_ps3'):
            with self.subTest(shader=name):
                source = translate(name)
                self.assertNotIn('// bool-exec', source)
                self.assertIn('tfetch2D(', source)

    def test_native_upload_preserves_every_bit_in_both_boolean_banks(self):
        header = ROOT/'glue/rexglue-sdk-main/src/graphics/gta4_native/native_shader_booleans.h'
        renderer = ROOT/'glue/rexglue-sdk-main/src/graphics/gta4_native/graphics_system.cpp'
        self.assertIn('PackNativeShaderBooleans(vertex_booleans, pixel_booleans)', renderer.read_text())
        # The independent oracle constructs the ABI bytes, rather than repeating
        # the implementation's mask-and-shift expression.
        values = [0, 0xffff, 0xffff0000, 0xffffffff]
        values += [1 << bit for bit in range(16)]
        checks = []
        for vertex in values:
            for pixel in values:
                expected = int.from_bytes(vertex.to_bytes(4, 'little')[:2]
                                          + pixel.to_bytes(4, 'little')[:2], 'little')
                checks.append(f'static_assert(PackNativeShaderBooleans({vertex}u, {pixel}u) == {expected}u);')
        with tempfile.TemporaryDirectory(prefix='liberty-bool-upload-') as directory:
            source = Path(directory)/'boolean_upload.cpp'
            output = Path(directory)/'boolean_upload'
            source.write_text(f'#include "{header}"\nusing rex::graphics::gta4_native::PackNativeShaderBooleans;\n'
                              + '\n'.join(checks) + '\nint main() { return 0; }\n')
            subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', str(source), '-o', str(output)],
                           capture_output=True, text=True, check=True, timeout=30)
            subprocess.run([str(output)], check=True, timeout=10)

    def test_pixel_conditional_end_contains_coverage_epilogue(self):
        data = shader('gta_normal_ps2').read_bytes()
        for op in (4, 14):
            with self.subTest(opcode=op):
                _, body, tail = scope(translate('gta_normal_ps2', mutate_cf(data, 3, opcode=op)))
                self.assertIn('alphaTestPass', body)
                self.assertIn('return output;', body)
                self.assertIn('output.oC0.xyzw', tail)
                self.assertIn('return output;', tail)


if __name__ == '__main__':
    unittest.main()
