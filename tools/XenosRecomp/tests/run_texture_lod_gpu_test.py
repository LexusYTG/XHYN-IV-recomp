#!/usr/bin/env python3
"""Run mip-selection readback checks on the local Mac GPU.

The test uses the production shared Metal helpers. Python generates expected
values; the GPU checks integer/fractional levels, signed bias, clamping, and
invariance to texture-coordinate footprint. Implicit sampling is a negative
control. This is a helper test, not a GTA IV visual acceptance test.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
METAL = r'''
#define __air__
#define GTA4_RECOMP
#define XENOS_RECOMP_PIXEL_SHADER
#include "COMMON_PATH"

struct LodTestParams {
    float register_lod, sampler_bias, instruction_bias, uv_scale;
    uint implicit_control;
};
struct LodTestVertexOutput { float4 position [[position]]; float2 uv; };
vertex LodTestVertexOutput lodTestVertex(uint index [[vertex_id]]) {
    const float2 positions[3] = {float2(-1, -1), float2(3, -1), float2(-1, 3)};
    LodTestVertexOutput output;
    output.position = float4(positions[index], 0.0, 1.0);
    output.uv = positions[index] * 0.5 + 0.5;
    return output;
}
fragment float4 lodTestFragment(
    LodTestVertexOutput input [[stage_in]],
    constant Texture2DDescriptorHeap* textures [[buffer(0)]],
    constant SamplerDescriptorHeap* samplers [[buffer(1)]],
    constant LodTestParams& params [[buffer(2)]]) {
    float lod = params.register_lod + params.sampler_bias + params.instruction_bias;
    float2 uv = input.uv * params.uv_scale;
    if (params.implicit_control)
        return tfetch2D(textures, samplers, 0, 0, uv, float2(0), lod);
    return tfetch2DLod(textures, samplers, 0, 0, uv, float2(0), lod);
}
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("This GPU test requires macOS and an installed Metal toolchain.")
    directory = args.output_directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "texture_lod_gpu_test.metal"
    source.write_text(METAL.replace("COMMON_PATH", str(
        ROOT / "tools/XenosRecomp/XenosRecomp/shader_common.h")))
    vectors = bytearray()
    for reg, sampler, instruction in ((0, 0, 0), (1, 0, 0), (2, 0, 0),
                                      (1, 0.5, 0), (1, 0.5, -0.25), (3, 1, 0), (0, -1, 0)):
        for footprint in (0.25, 1, 8, 64):
            expected = min(3.0, max(0.0, reg + sampler + instruction))
            vectors.extend(struct.pack("<4fIf", reg, sampler, instruction, footprint, 0, expected))
    for footprint in (0.25, 64):
        vectors.extend(struct.pack("<4fIf", 1, 0, 0, footprint, 1, 1))
    vector_path = directory / "texture_lod_vectors.bin"
    vector_path.write_bytes(vectors)
    ir = directory / "texture_lod_gpu_test.air"
    library = directory / "texture_lod_gpu_test.metallib"
    executable = directory / "texture_lod_gpu_test"
    commands = [
        ["xcrun", "metal", "-c", str(source), "-o", str(ir)],
        ["xcrun", "metallib", str(ir), "-o", str(library)],
        ["xcrun", "clang++", "-std=c++17", "-fobjc-arc",
         str(Path(__file__).with_name("texture_lod_gpu_test.mm")),
         "-framework", "Foundation", "-framework", "Metal", "-o", str(executable)],
        [str(executable), str(library), str(vector_path)],
    ]
    for command in commands:
        subprocess.run(command, check=True, timeout=120)


if __name__ == "__main__":
    main()
