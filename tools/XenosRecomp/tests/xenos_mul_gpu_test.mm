#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <cstdio>

// The Python driver supplies raw operand bits and checks the GPU output bits.
int main(int argc, const char** argv) {
  @autoreleasepool {
    if (argc != 6) return 2;
    auto device = MTLCreateSystemDefaultDevice();
    if (!device) return 2;
    NSError* error = nil;
    auto library = [device newLibraryWithURL:[NSURL fileURLWithPath:@(argv[1])] error:&error];
    if (!library) { NSLog(@"%@", error); return 2; }
    auto pipeline = [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"testMul"] error:&error];
    if (!pipeline) { NSLog(@"%@", error); return 2; }
    auto data = [NSData dataWithContentsOfFile:@(argv[2])];
    if (!data.length) return 2;
    auto input = [device newBufferWithBytes:data.bytes length:data.length options:MTLResourceStorageModeShared];
    const NSUInteger count = [@(argv[4]) integerValue];
    const NSUInteger outputSize = [@(argv[5]) integerValue];
    auto output = [device newBufferWithLength:outputSize options:MTLResourceStorageModeShared];
    if (!input || !output || !count) return 2;
    auto queue = [device newCommandQueue];
    auto command = [queue commandBuffer];
    auto encoder = [command computeCommandEncoder];
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:input offset:0 atIndex:0];
    [encoder setBuffer:output offset:0 atIndex:1];
    [encoder dispatchThreads:MTLSizeMake(count, 1, 1) threadsPerThreadgroup:MTLSizeMake(1, 1, 1)];
    [encoder endEncoding];
    [command commit];
    [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted) { NSLog(@"%@", command.error); return 2; }
    auto result = [NSData dataWithBytes:output.contents length:outputSize];
    if (![result writeToFile:@(argv[3]) atomically:YES]) return 2;
    std::printf("device=%s cases=%lu\n", device.name.UTF8String, (unsigned long)count);
  }
  return 0;
}
