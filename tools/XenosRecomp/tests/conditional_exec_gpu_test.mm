// Executes the translated retail deck vertex shader as a compute-callable
// function. The Python driver removes stage-interface attributes only.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <vector>

static std::vector<uint8_t> Read(const char* path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return {};
  return std::vector<uint8_t>(std::istreambuf_iterator<char>(f), {});
}
struct Push { uint64_t vertex, pixel, shared; };
static_assert(sizeof(Push) == 24);

int main(int argc, const char** argv) {
  @autoreleasepool {
    if (argc != 7) { std::fprintf(stderr, "usage: probe library vertex.bin shared.bin input.bin output.bin count\n"); return 2; }
    const size_t count = std::stoul(argv[6]);
    const auto vertexData = Read(argv[2]), sharedData = Read(argv[3]), inputData = Read(argv[4]);
    if (!count || vertexData.size() != count*4096 || sharedData.size() != count*1024 || inputData.size()!=count*16) return 2;
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device) return 2;
    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithURL:[NSURL fileURLWithPath:@(argv[1])] error:&error];
    if (!library) { NSLog(@"library error: %@",error); return 2; }
    id<MTLFunction> function = [library newFunctionWithName:@"probeRetailDeck"];
    id<MTLComputePipelineState> pipeline = [device newComputePipelineStateWithFunction:function error:&error];
    if (!pipeline) { NSLog(@"pipeline error: %@",error); return 2; }
    id<MTLBuffer> vertex = [device newBufferWithBytes:vertexData.data() length:vertexData.size() options:MTLResourceStorageModeShared];
    id<MTLBuffer> shared = [device newBufferWithBytes:sharedData.data() length:sharedData.size() options:MTLResourceStorageModeShared];
    id<MTLBuffer> input = [device newBufferWithBytes:inputData.data() length:inputData.size() options:MTLResourceStorageModeShared];
    id<MTLBuffer> output = [device newBufferWithLength:count*32 options:MTLResourceStorageModeShared];
    if (!vertex || !shared || !input || !output) return 2;
    std::vector<Push> addresses(count);
    for (size_t i=0;i<count;++i) addresses[i] = {vertex.gpuAddress+i*4096, vertex.gpuAddress+i*4096, shared.gpuAddress+i*1024};
    id<MTLBuffer> pushes = [device newBufferWithBytes:addresses.data() length:addresses.size()*sizeof(Push) options:MTLResourceStorageModeShared];
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLCommandBuffer> command = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:pushes offset:0 atIndex:0];
    [encoder setBuffer:input offset:0 atIndex:1];
    [encoder setBuffer:output offset:0 atIndex:2];
    [encoder useResource:vertex usage:MTLResourceUsageRead];
    [encoder useResource:shared usage:MTLResourceUsageRead];
    [encoder dispatchThreads:MTLSizeMake(count,1,1) threadsPerThreadgroup:MTLSizeMake(1,1,1)];
    [encoder endEncoding];
    [command commit];
    [command waitUntilCompleted];
    if (command.status == MTLCommandBufferStatusError) { NSLog(@"execution error: %@",command.error); return 2; }
    std::ofstream result(argv[5],std::ios::binary);
    result.write(static_cast<const char*>(output.contents),output.length);
    if (!result) return 2;
    std::printf("device=%s retail_shader_invocations=%zu completed=true\n",device.name.UTF8String,count);
  }
  return 0;
}
