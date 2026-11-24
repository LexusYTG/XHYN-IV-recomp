// Differential test for the original deferred pixel shader's Boolean EXEC.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <vector>

struct Push { uint64_t vertex, pixel, shared; };
struct Case { float alpha, scalar; uint32_t booleans; float expected_color_alpha, expected_data_alpha; };
static_assert(sizeof(Push) == 24 && sizeof(Case) == 20);

int main(int argc, const char** argv) {
  @autoreleasepool {
    if (argc != 4) { std::fprintf(stderr,"usage: deferred_alpha_gpu_test library vectors.bin output.bin\n"); return 2; }
    std::ifstream casesFile(argv[2],std::ios::binary);
    std::vector<Case> cases; Case item{};
    for (; casesFile.read(reinterpret_cast<char*>(&item),sizeof(item));) cases.push_back(item);
    if (!casesFile.eof() || casesFile.gcount()!=0 || cases.empty()) return 2;
    id<MTLDevice> device=MTLCreateSystemDefaultDevice(); if (!device) return 2;
    NSError* error=nil;
    id<MTLLibrary> library=[device newLibraryWithURL:[NSURL fileURLWithPath:@(argv[1])] error:&error];
    if (!library) { NSLog(@"library: %@",error); return 2; }
    id<MTLFunction> vertex=[library newFunctionWithName:@"deferredAlphaTestVertex"];
    // The retail pixel entry point requires the same specialization constant
    // that the application supplies. Zero disables optional alpha testing here.
    MTLFunctionConstantValues* specialization=[MTLFunctionConstantValues new];
    const uint32_t specBits=0;
    [specialization setConstantValue:&specBits type:MTLDataTypeUInt atIndex:0];
    id<MTLFunction> pixel=[library newFunctionWithName:@"shaderMain" constantValues:specialization error:&error];
    if (!pixel) { NSLog(@"pixel specialization: %@",error); return 2; }
    MTLRenderPipelineDescriptor* desc=[MTLRenderPipelineDescriptor new];
    desc.vertexFunction=vertex; desc.fragmentFunction=pixel;
    for (NSUInteger target=0;target<3;++target) desc.colorAttachments[target].pixelFormat=MTLPixelFormatRGBA32Float;
    id<MTLRenderPipelineState> pipeline=[device newRenderPipelineStateWithDescriptor:desc error:&error];
    if (!pipeline) { NSLog(@"pipeline: %@",error); return 2; }
    id<MTLCommandQueue> queue=[device newCommandQueue];
    MTLTextureDescriptor* textureDesc=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float width:1 height:1 mipmapped:NO];
    textureDesc.storageMode=MTLStorageModeShared; textureDesc.usage=MTLTextureUsageShaderRead;
    id<MTLTexture> diffuse=[device newTextureWithDescriptor:textureDesc];
    id<MTLTexture> normal=[device newTextureWithDescriptor:textureDesc];
    const std::array<float,4> color={0.25f,0.5f,0.75f,1.0f}, bump={0.5f,0.5f,1.0f,0.5f};
    [diffuse replaceRegion:MTLRegionMake2D(0,0,1,1) mipmapLevel:0 withBytes:color.data() bytesPerRow:sizeof(color)];
    [normal replaceRegion:MTLRegionMake2D(0,0,1,1) mipmapLevel:0 withBytes:bump.data() bytesPerRow:sizeof(bump)];
    MTLSamplerDescriptor* samplerDesc=[MTLSamplerDescriptor new];
    samplerDesc.supportArgumentBuffers=YES; samplerDesc.minFilter=MTLSamplerMinMagFilterLinear;
    samplerDesc.magFilter=MTLSamplerMinMagFilterLinear; samplerDesc.sAddressMode=MTLSamplerAddressModeRepeat;
    samplerDesc.tAddressMode=MTLSamplerAddressModeRepeat;
    id<MTLSamplerState> sampler=[device newSamplerStateWithDescriptor:samplerDesc];
    id<MTLArgumentEncoder> textureEncoder=[pixel newArgumentEncoderWithBufferIndex:0];
    id<MTLArgumentEncoder> samplerEncoder=[pixel newArgumentEncoderWithBufferIndex:4];
    const NSUInteger textureStride=textureEncoder.encodedLength;
    id<MTLBuffer> textures=[device newBufferWithLength:textureStride*2 options:MTLResourceStorageModeShared];
    id<MTLBuffer> samplers=[device newBufferWithLength:samplerEncoder.encodedLength options:MTLResourceStorageModeShared];
    [textureEncoder setArgumentBuffer:textures offset:0]; [textureEncoder setTexture:diffuse atIndex:0];
    [textureEncoder setArgumentBuffer:textures offset:textureStride]; [textureEncoder setTexture:normal atIndex:0];
    [samplerEncoder setArgumentBuffer:samplers offset:0]; [samplerEncoder setSamplerState:sampler atIndex:0];
    id<MTLBuffer> constants=[device newBufferWithLength:4096 options:MTLResourceStorageModeShared];
    id<MTLBuffer> shared=[device newBufferWithLength:1024 options:MTLResourceStorageModeShared];
    id<MTLTexture> outputs[3];
    MTLTextureDescriptor* outDesc=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float width:16 height:16 mipmapped:NO];
    outDesc.storageMode=MTLStorageModeShared; outDesc.usage=MTLTextureUsageRenderTarget;
    for (NSUInteger target=0;target<3;++target) outputs[target]=[device newTextureWithDescriptor:outDesc];
    if (!diffuse || !normal || !sampler || !textures || !samplers || !constants || !shared || !outputs[2]) return 2;
    std::ofstream output(argv[3],std::ios::binary); if (!output) return 2;
    for (const Case& test:cases) {
      std::memset(constants.contents,0,constants.length); std::memset(shared.contents,0,shared.length);
      auto floats=static_cast<float*>(constants.contents); floats[39*4]=test.scalar; floats[210*4]=0.9f;
      auto sharedWords=static_cast<uint32_t*>(shared.contents); sharedWords[1]=1; sharedWords[528/4]=test.booleans;
      auto sharedFloats=static_cast<float*>(shared.contents); sharedFloats[744/4]=1; sharedFloats[748/4]=1;
      Push push={constants.gpuAddress,constants.gpuAddress,shared.gpuAddress};
      MTLRenderPassDescriptor* pass=[MTLRenderPassDescriptor renderPassDescriptor];
      for (NSUInteger target=0;target<3;++target) {
        pass.colorAttachments[target].texture=outputs[target]; pass.colorAttachments[target].loadAction=MTLLoadActionClear;
        pass.colorAttachments[target].storeAction=MTLStoreActionStore; pass.colorAttachments[target].clearColor=MTLClearColorMake(-99,-99,-99,-99);
      }
      id<MTLCommandBuffer> command=[queue commandBuffer]; id<MTLRenderCommandEncoder> encoder=[command renderCommandEncoderWithDescriptor:pass];
      [encoder setRenderPipelineState:pipeline]; [encoder setVertexBytes:&test.alpha length:sizeof(test.alpha) atIndex:9];
      [encoder setFragmentBuffer:textures offset:0 atIndex:0]; [encoder setFragmentBuffer:samplers offset:0 atIndex:4];
      [encoder setFragmentBytes:&push length:sizeof(push) atIndex:8];
      for (id<MTLResource> resource in @[diffuse,normal,constants,shared]) [encoder useResource:resource usage:MTLResourceUsageRead stages:MTLRenderStageFragment];
      [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [encoder endEncoding];
      [command commit]; [command waitUntilCompleted];
      if (command.status==MTLCommandBufferStatusError) { NSLog(@"GPU: %@",command.error); return 2; }
      std::array<std::array<float,4>,256> pixels{};
      for (NSUInteger target=0;target<3;++target) {
        [outputs[target] getBytes:pixels.data() bytesPerRow:16*sizeof(pixels[0]) fromRegion:MTLRegionMake2D(0,0,16,16) mipmapLevel:0];
        output.write(reinterpret_cast<const char*>(pixels.data()),sizeof(pixels));
      }
    }
    if (!output) return 2;
    std::printf("deferred_pixel_gpu device=%s cases=%zu completed=true\n",device.name.UTF8String,cases.size());
  }
  return 0;
}
