#!/usr/bin/env python3
"""Exercise SM3 zero/denormal multiplication on Metal in strict and relaxed modes.

Uses the production helper and runtime operand bits, including genuine nonzero
NaNs which must remain NaN. Requires NumPy and the macOS Metal toolchain.
"""
from pathlib import Path
import argparse
import itertools
import json
import struct
import subprocess

import numpy as np

METAL = r'''
#define __air__
#define GTA4_RECOMP
#include "COMMON_PATH"
struct Operands { uint4 a, b, c; };
struct Results { uint4 vector4, scalar, mad, dots, smaller; };
kernel void testMul(device const Operands* inputs [[buffer(0)]],
                    device Results* outputs [[buffer(1)]], uint i [[thread_position_in_grid]]) {
    float4 a = as_type<float4>(inputs[i].a);
    float4 b = as_type<float4>(inputs[i].b);
    float4 c = as_type<float4>(inputs[i].c);
    Results result;
    result.vector4 = as_type<uint4>(xenosMul(a, b));
    result.scalar = as_type<uint4>(float4(xenosMul(a.x, b.x), xenosMul(a.y, b.y),
                                        xenosMul(a.z, b.z), xenosMul(a.w, b.w)));
    result.mad = as_type<uint4>(xenosMul(a, b) + c);
    result.dots = as_type<uint4>(float4(xenosDot(a.xy, b.xy), xenosDot(a.xyz, b.xyz),
                                       xenosDot(a, b), xenosDot(a.xy, b.xy) + c.x));
    result.smaller = as_type<uint4>(float4(xenosMul(a.xy, b.xy), xenosMul(a.xyz, b.xyz).yz));
    outputs[i] = result;
}
'''


def bits(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


def value(raw):
    return np.float32(struct.unpack('<f', struct.pack('<I', raw))[0])


def product(a, b):
    # Shader Model 3: either zero/denormal input forces positive zero.
    if not a & 0x7F800000 or not b & 0x7F800000:
        return np.float32(0)
    return np.float32(value(a) * value(b))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', required=True, type=Path)
    args = parser.parse_args()
    out = args.output_directory.resolve()
    out.mkdir(parents=True, exist_ok=True)
    common = Path(__file__).resolve().parents[1] / 'XenosRecomp/shader_common.h'
    source = out / 'test.metal'
    source.write_text(METAL.replace('COMMON_PATH', str(common)))
    candidates = [bits(x) for x in (0.0, -0.0, 1.0, -2.0, 0.5, float('inf'), -float('inf'))]
    candidates += [1, 0x80000001, 0x007FFFFF, 0x807FFFFF, 0x7FC12345]
    vectors, expected = bytearray(), []
    with np.errstate(all='ignore'):
        for a, b in itertools.product(candidates, repeat=2):
            av = [a, bits(2), bits(-1), b]
            bv = [b, bits(0.5), bits(3), a]
            cv = [bits(x) for x in (0.25, 2, -1, 0.5)]
            vectors.extend(struct.pack('<12I', *(av + bv + cv)))
            p = [product(x, y) for x, y in zip(av, bv)]
            d2 = np.float32(p[0] + p[1])
            d3 = np.float32(d2 + p[2])
            d4 = np.float32(d3 + p[3])
            expected.append(p + p + [np.float32(x + value(y)) for x, y in zip(p, cv)] +
                            [d2, d3, d4, np.float32(d2 + value(cv[0]))] + [p[0], p[1], p[1], p[2]])
    vector_path = out / 'operands.bin'
    vector_path.write_bytes(vectors)
    executable = out / 'gpu-test'
    subprocess.run(['xcrun', 'clang++', '-std=c++17', '-fobjc-arc',
                    str(Path(__file__).with_name('xenos_mul_gpu_test.mm')),
                    '-framework', 'Foundation', '-framework', 'Metal', '-o', str(executable)], check=True)
    reports = []
    for mode, flags in [('strict', ['-fno-fast-math']), ('relaxed', ['-fmetal-math-mode=relaxed'])]:
        air, library, output = (out / (mode + suffix) for suffix in ('.air', '.metallib', '.bin'))
        subprocess.run(['xcrun', 'metal', '-std=metal3.0', *flags, '-c', str(source), '-o', str(air)], check=True)
        subprocess.run(['xcrun', 'metallib', str(air), '-o', str(library)], check=True)
        size = len(expected) * len(expected[0]) * struct.calcsize('<I')
        subprocess.run([str(executable), str(library), str(vector_path), str(output), str(len(expected)), str(size)], check=True)
        actual = np.frombuffer(output.read_bytes(), dtype='<u4').reshape(len(expected), -1)
        for row, values in enumerate(expected):
            for column, want in enumerate(values):
                got = int(actual[row, column])
                matches = np.isnan(value(got)) if np.isnan(want) else got == bits(want)
                if not matches:
                    raise AssertionError(f'{mode} case={row} component={column}: got={got:08X} expected={bits(want):08X}')
        reports.append({'mode': mode, 'cases': len(expected), 'verified_components': actual.size, 'result': 'passed'})
    (out / 'result.json').write_text(json.dumps(reports, indent=2) + '\n')
    print(json.dumps(reports))


if __name__ == '__main__':
    main()
