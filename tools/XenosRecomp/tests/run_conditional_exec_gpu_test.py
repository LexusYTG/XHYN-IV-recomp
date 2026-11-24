#!/usr/bin/env python3
"""GPU comparison of original-container deck vertex translation, old versus fixed.

The generated shader arithmetic is unchanged. Only stage-interface annotations
are removed to allow the same function to be called from a test compute kernel.
Expected outputs come from the decoded original Boolean EXEC clause. This is
not a claim of full-game visual acceptance.
"""
from pathlib import Path
import argparse
import hashlib
import itertools
import json
import math
import re
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = r'''
kernel void probeRetailDeck(constant PushConstants* pushes [[buffer(0)]],
    const device float4* cases [[buffer(1)]], device float4* results [[buffer(2)]],
    uint id [[thread_position_in_grid]]) {
    VertexShaderInput input = {};
    input.iPosition0 = float4(0.25,0.5,0.75,1.0);
    input.iColor0 = float4(0.6,0.7,0.8,cases[id].x);
    input.iNormal0 = float4(0.0,0.0,cases[id].y,1.0);
    input.iTangent0 = float4(1.0,0.0,0.0,1.0);
    input.iTexCoord0 = float4(0.25,0.75,0.0,0.0);
    auto output = shaderMain(nullptr,nullptr,nullptr,nullptr,nullptr,pushes[id],input);
    results[id*2] = output.oColor0;
    results[id*2+1] = output.oPos;
}
'''


def run(command):
    return subprocess.run([str(x) for x in command], check=True, capture_output=True,
                          text=True, timeout=120)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before-translator',type=Path,required=True)
    parser.add_argument('--translator',type=Path,default=ROOT/'out/build/xenosrecomp-macos/XenosRecomp/XenosRecomp')
    parser.add_argument('--output-directory',type=Path,required=True)
    args=parser.parse_args()
    out=args.output_directory.resolve();out.mkdir(parents=True,exist_ok=True)
    source=ROOT/'LibertyRecompLib/shader/rage_shaders/gta_normal/gta_normal_vs4.bin'
    common=ROOT/'tools/XenosRecomp/XenosRecomp/shader_common.h'
    rows=[];vs=bytearray();shared=bytearray();inputs=bytearray()
    for alpha,normal,cosine,enabled in itertools.product((0.25,1.0),(-1.0,1.0),(0.0,0.125,0.5,1.0),(False,True)):
        constants=bytearray(4096)
        for first in (0,4,8):
            for row in range(4):
                struct.pack_into('<4f',constants,(first+row)*16,*[float(row==col) for col in range(4)])
        # Only the view-normal z computation uses this value. Position uses the
        # independent identity world/view/projection matrix, as in the shader.
        struct.pack_into('<f',constants,6*16+2*4,cosine)
        struct.pack_into('<4f',constants,39*16,1,0,1,0)
        struct.pack_into('<4f',constants,40*16,0,0,1,0)
        sc=bytearray(1024);struct.pack_into('<I',sc,528,int(enabled)<<1)
        vs.extend(constants);shared.extend(sc);inputs.extend(struct.pack('<4f',alpha,normal,0,0))
        rows.append(dict(vertex_alpha=alpha,normal_sign=normal,view_cosine=cosine,
                         boolean=enabled,expected_alpha=alpha if enabled else abs(cosine*normal)))
    (out/'vertex.bin').write_bytes(vs);(out/'shared.bin').write_bytes(shared);(out/'input.bin').write_bytes(inputs)
    exe=out/'conditional_exec_gpu_test'
    run(['xcrun','clang++','-std=c++17','-fobjc-arc',Path(__file__).with_name('conditional_exec_gpu_test.mm'),
         '-framework','Foundation','-framework','Metal','-o',exe])
    result={}
    for variant,tool in (('before',args.before_translator),('fixed',args.translator)):
        hlsl=out/(variant+'.hlsl')
        run([tool,source,hlsl,common])
        text=hlsl.read_text();start=text.index('struct VertexShaderInput')
        head,body=text[:start],text[start:]
        # Preserve every expression, constant access, register write and gate.
        body=re.sub(r'\[\[(?:attribute\([^)]*\)|buffer\([^)]*\)|user\([^)]*\)|position|invariant|clip_distance|vertex|stage_in)\]\]','',body)
        metal=out/(variant+'.metal');metal.write_text(head+body+'\n'+WRAPPER)
        air=out/(variant+'.air');lib=out/(variant+'.metallib')
        run(['xcrun','metal','-c','-D__air__','-DGTA4_RECOMP','-Wno-unused-variable',metal,'-o',air])
        run(['xcrun','metallib',air,'-o',lib])
        receipt=run([exe,lib,out/'vertex.bin',out/'shared.bin',out/'input.bin',out/(variant+'.bin'),len(rows)])
        print(variant,receipt.stdout.strip())
        data=(out/(variant+'.bin')).read_bytes();assert len(data)==len(rows)*32
        values=list(struct.iter_unpack('<8f',data));failures=[]
        for index,(row,value) in enumerate(zip(rows,values)):
            if not math.isfinite(value[3]) or abs(value[3]-row['expected_alpha'])>0.00001:
                failures.append(dict(index=index,**row,actual_alpha=value[3]))
        result[variant]=dict(invocations=len(values),failures=failures,source_sha256=hashlib.sha256(hlsl.read_bytes()).hexdigest(),output_sha256=hashlib.sha256(data).hexdigest())
    assert not result['fixed']['failures'],result['fixed']
    assert result['before']['failures'],'baseline failed to reproduce erroneous branch execution'
    assert all(row['boolean'] for row in result['before']['failures'])
    report=dict(status='passed',original_container_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),cases=rows,results=result)
    (out/'verified-gpu-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print('CONDITIONAL_EXEC_GPU=PASS','fixed_cases',len(rows),'baseline_mismatches',len(result['before']['failures']))

if __name__=='__main__':
    main()
