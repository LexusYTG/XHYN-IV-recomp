#!/usr/bin/env python3
"""Render the original GTA IV deferred pixel shader before and after translation.

This test leaves fragment shader arithmetic and its stage interface unchanged.
It verifies all pixels of three float render targets for independent vertex and
pixel Boolean-bank values. A small test vertex shader supplies controlled data;
this is not a complete-game visual acceptance test.
"""
from pathlib import Path
import argparse
import hashlib
import itertools
import json
import math
import struct
import subprocess

ROOT=Path(__file__).resolve().parents[3]
VERTEX=r'''
vertex Interpolators deferredAlphaTestVertex(uint index [[vertex_id]],
    constant float& alpha [[buffer(9)]]) {
    const float2 positions[3]={float2(-1,-1),float2(3,-1),float2(-1,3)};
    Interpolators value={};
    value.iPos=float4(positions[index],0,1);
    value.iTexCoord0=float4(0.5,0.5,0,0);
    value.iTexCoord1=float4(0,0,1,1);
    value.iTexCoord4=float4(1,0,0,0);
    value.iTexCoord5=float4(0,1,0,0);
    value.iColor0=float4(1,1,1,alpha);
    return value;
}
'''

def run(command):
    result = subprocess.run([str(x) for x in command], capture_output=True,
                            text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {command}\n'
                           + result.stdout + result.stderr)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before-translator',type=Path,required=True)
    parser.add_argument('--translator',type=Path,default=ROOT/'out/build/xenosrecomp-macos/XenosRecomp/XenosRecomp')
    parser.add_argument('--output-directory',type=Path,required=True)
    args=parser.parse_args()
    out=args.output_directory.resolve();out.mkdir(parents=True,exist_ok=True)
    rows=[];vectors=bytearray()
    for alpha,scalar,vertex_bool,pixel_bool in itertools.product((0,0.125,0.5,1),(0.5,1),(False,True),(False,True)):
        booleans=(int(vertex_bool)<<1)|(int(pixel_bool)<<17)
        expected0=alpha*scalar;expected12=expected0*int(pixel_bool)
        vectors.extend(struct.pack('<ffIff',alpha,scalar,booleans,expected0,expected12))
        rows.append(dict(alpha=alpha,scalar=scalar,vertex_bool=vertex_bool,pixel_bool=pixel_bool,packed_booleans=booleans,expected=[expected0,expected12,expected12]))
    (out/'cases.bin').write_bytes(vectors)
    executable=out/'deferred_alpha_gpu_test'
    run(['xcrun','clang++','-std=c++17','-fobjc-arc',Path(__file__).with_name('deferred_alpha_gpu_test.mm'),'-framework','Foundation','-framework','Metal','-o',executable])
    source=ROOT/'LibertyRecompLib/shader/rage_shaders/gta_normal/gta_normal_ps2.bin'
    common=ROOT/'tools/XenosRecomp/XenosRecomp/shader_common.h'
    variants={}
    for variant,translator in [('before',args.before_translator),('fixed',args.translator)]:
        hlsl=out/(variant+'.hlsl');run([translator,source,hlsl,common])
        metal=out/(variant+'.metal');metal.write_text(hlsl.read_text()+'\n'+VERTEX)
        air=out/(variant+'.air');library=out/(variant+'.metallib')
        run(['xcrun','metal','-c','-D__air__','-DGTA4_RECOMP','-Wno-unused-variable',metal,'-o',air])
        run(['xcrun','metallib',air,'-o',library])
        binary=out/(variant+'.bin')
        receipt=run([executable,library,out/'cases.bin',binary]);print(variant,receipt.stdout.strip())
        data=binary.read_bytes();pixels=list(struct.iter_unpack('<4f',data))
        assert len(pixels)==len(rows)*3*256
        failures=[]
        for index,row in enumerate(rows):
            for target,expected in enumerate(row['expected']):
                actual=pixels[(index*3+target)*256:(index*3+target+1)*256]
                assert all(all(math.isfinite(x) for x in p) for p in actual)
                mismatch=sum(abs(p[3]-expected)>0.00001 for p in actual)
                if mismatch: failures.append(dict(case=index,target=target,mismatching_pixels=mismatch,target_expected_alpha=expected,actual_alpha=actual[0][3],**row))
        variants[variant]=dict(cases=len(rows),failures=failures,completed=True,source_sha256=hashlib.sha256(hlsl.read_bytes()).hexdigest(),output_sha256=hashlib.sha256(data).hexdigest())
    assert not variants['fixed']['failures'],variants['fixed']['failures']
    assert variants['before']['failures'],'baseline did not expose the ignored pixel Boolean'
    assert all(f['target'] in (1,2) and not f['pixel_bool'] for f in variants['before']['failures'])
    report=dict(status='passed',cases=rows,results=variants,original_container_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    (out/'verified-pixel-gpu-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print('DEFERRED_ALPHA_GPU=PASS','fixed_cases',len(rows),'baseline_case_target_mismatches',len(variants['before']['failures']))

if __name__=='__main__':
    main()
