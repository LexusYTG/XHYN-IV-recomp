/*
 * Copyright (c) 2026 LibertyRecomp contributors
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#import <Metal/Metal.h>
#include <unordered_set>

// Owns the extra Metal dependencies introduced by conservative BDA enumeration.
// Encoding is single-writer, and completion cannot run until encoding has ended.
// This object must not own its command buffer, queue, or device.
class MVKMTLBufferRetention {
public:
	MVKMTLBufferRetention() = default;
	MVKMTLBufferRetention(const MVKMTLBufferRetention&) = delete;
	MVKMTLBufferRetention& operator=(const MVKMTLBufferRetention&) = delete;
	~MVKMTLBufferRetention();

	// Call while the device resource lock still protects the buffer lookup.
	void retainBuffer(id<MTLBuffer> buffer);
	void clear();

private:
	std::unordered_set<id<MTLBuffer>> _buffers;
};

// Borrowed state owned by this actual Metal command buffer, including prefilled
// buffers. Repeated Vulkan encoders/passes share the same deduplicated ownership.
// Returns nullptr for a failed (nil) command-buffer allocation.
MVKMTLBufferRetention* mvkGetMTLBufferRetention(id<MTLCommandBuffer> commandBuffer);
