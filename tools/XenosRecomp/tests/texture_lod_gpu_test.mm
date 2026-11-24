// Run with a metallib containing lodTestVertex and lodTestFragment, plus the
// Python-generated test vectors. Exercises the actual shared sampling helpers.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <algorithm>
#include <array>
#include <cstdint>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <vector>

struct Params {
  float register_lod, sampler_bias, instruction_bias, uv_scale;
  uint32_t implicit_control;
};
struct TestVector { Params params; float expected; };
static_assert(sizeof(Params) == 20 && sizeof(TestVector) == 24,
              "GPU test layout must match Python's <4fIf vectors");

int main(int argc, const char** argv) {
  @autoreleasepool {
    if (argc != 3) { std::fprintf(stderr, "usage: texture_lod_gpu_test shader.metallib vectors.bin\n"); return 2; }
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device) { std::fprintf(stderr, "no Metal device\n"); return 2; }
    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithURL:[NSURL fileURLWithPath:@(argv[1])] error:&error];
    if (!library) { NSLog(@"library: %@", error); return 2; }
    id<MTLFunction> vertex = [library newFunctionWithName:@"lodTestVertex"];
    id<MTLFunction> fragment = [library newFunctionWithName:@"lodTestFragment"];
    MTLRenderPipelineDescriptor* pipelineDesc = [MTLRenderPipelineDescriptor new];
    pipelineDesc.vertexFunction = vertex;
    pipelineDesc.fragmentFunction = fragment;
    pipelineDesc.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA32Float;
    id<MTLRenderPipelineState> pipeline = [device newRenderPipelineStateWithDescriptor:pipelineDesc error:&error];
    if (!pipeline) { NSLog(@"pipeline: %@", error); return 2; }
    id<MTLCommandQueue> queue = [device newCommandQueue];
    MTLTextureDescriptor* inputDesc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float width:8 height:8 mipmapped:YES];
    inputDesc.storageMode = MTLStorageModeShared;
    inputDesc.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> input = [device newTextureWithDescriptor:inputDesc];
    if (!input) return 2;
    for (NSUInteger level = 0; level < input.mipmapLevelCount; ++level) {
      const NSUInteger extent = std::max(NSUInteger(1), input.width >> level);
      std::vector<std::array<float, 4>> texels(extent * extent, {float(level), float(level), float(level), 1.0f});
      [input replaceRegion:MTLRegionMake2D(0, 0, extent, extent) mipmapLevel:level withBytes:texels.data() bytesPerRow:extent * sizeof(texels[0])];
    }
    MTLSamplerDescriptor* samplerDesc = [MTLSamplerDescriptor new];
    samplerDesc.minFilter = MTLSamplerMinMagFilterLinear;
    samplerDesc.magFilter = MTLSamplerMinMagFilterLinear;
    samplerDesc.mipFilter = MTLSamplerMipFilterLinear;
    samplerDesc.sAddressMode = MTLSamplerAddressModeRepeat;
    samplerDesc.tAddressMode = MTLSamplerAddressModeRepeat;
    samplerDesc.supportArgumentBuffers = YES;
    id<MTLSamplerState> sampler = [device newSamplerStateWithDescriptor:samplerDesc];
    id<MTLArgumentEncoder> textures = [fragment newArgumentEncoderWithBufferIndex:0];
    id<MTLArgumentEncoder> samplers = [fragment newArgumentEncoderWithBufferIndex:1];
    id<MTLBuffer> textureArguments = [device newBufferWithLength:textures.encodedLength options:MTLResourceStorageModeShared];
    id<MTLBuffer> samplerArguments = [device newBufferWithLength:samplers.encodedLength options:MTLResourceStorageModeShared];
    [textures setArgumentBuffer:textureArguments offset:0];
    [textures setTexture:input atIndex:0];
    [samplers setArgumentBuffer:samplerArguments offset:0];
    [samplers setSamplerState:sampler atIndex:0];
    MTLTextureDescriptor* outputDesc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float width:16 height:16 mipmapped:NO];
    outputDesc.storageMode = MTLStorageModeShared;
    outputDesc.usage = MTLTextureUsageRenderTarget;
    id<MTLTexture> output = [device newTextureWithDescriptor:outputDesc];
    if (!output) return 2;
    std::ifstream file(argv[2], std::ios::binary);
    std::vector<TestVector> vectors;
    TestVector item{};
    for (; file.read(reinterpret_cast<char*>(&item), sizeof(item));) vectors.push_back(item);
    if (!file.eof() || file.gcount() != 0 || vectors.empty()) { std::fprintf(stderr, "invalid test vectors\n"); return 2; }
    size_t passed = 0, negativeControls = 0;
    for (const TestVector& test : vectors) {
      MTLRenderPassDescriptor* pass = [MTLRenderPassDescriptor renderPassDescriptor];
      pass.colorAttachments[0].texture = output;
      pass.colorAttachments[0].loadAction = MTLLoadActionClear;
      pass.colorAttachments[0].storeAction = MTLStoreActionStore;
      pass.colorAttachments[0].clearColor = MTLClearColorMake(-99, -99, -99, -99);
      id<MTLCommandBuffer> command = [queue commandBuffer];
      id<MTLRenderCommandEncoder> encoder = [command renderCommandEncoderWithDescriptor:pass];
      [encoder setRenderPipelineState:pipeline];
      [encoder setFragmentBuffer:textureArguments offset:0 atIndex:0];
      [encoder setFragmentBuffer:samplerArguments offset:0 atIndex:1];
      [encoder setFragmentBytes:&test.params length:sizeof(test.params) atIndex:2];
      [encoder useResource:input usage:MTLResourceUsageRead stages:MTLRenderStageFragment];
      [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
      [encoder endEncoding];
      [command commit];
      [command waitUntilCompleted];
      if (command.status == MTLCommandBufferStatusError) { NSLog(@"GPU: %@", command.error); return 2; }
      std::array<std::array<float, 4>, 256> pixels{};
      [output getBytes:pixels.data() bytesPerRow:16 * sizeof(pixels[0]) fromRegion:MTLRegionMake2D(0, 0, 16, 16) mipmapLevel:0];
      bool matches = true;
      for (const auto& pixel : pixels) {
        for (size_t channel = 0; channel < 3; ++channel) {
          matches &= std::isfinite(pixel[channel]) && std::abs(pixel[channel] - test.expected) <= 0.002f;
        }
        matches &= std::abs(pixel[3] - 1.0f) <= 0.002f;
      }
      if (test.params.implicit_control) {
        if (!matches) ++negativeControls;
      } else if (matches) {
        ++passed;
      } else {
        std::fprintf(stderr, "FAILED reg=%g sampler=%g instruction=%g scale=%g expected=%g actual=%g\n", test.params.register_lod, test.params.sampler_bias, test.params.instruction_bias, test.params.uv_scale, test.expected, pixels[0][0]);
        return 1;
      }
    }
    if (!negativeControls) { std::fprintf(stderr, "implicit negative control failed to reproduce the mismatch\n"); return 1; }
    std::printf("metal_lod_gpu=passed device=%s explicit_cases=%zu implicit_mismatch_controls=%zu\n", device.name.UTF8String, passed, negativeControls);
  }
  return 0;
}
