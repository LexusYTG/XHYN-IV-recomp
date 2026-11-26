/*
 * Copyright (c) 2026 LibertyRecomp contributors
 * SPDX-License-Identifier: Apache-2.0
 */

#include "MVKMTLBufferRetention.h"
#import <objc/runtime.h>

MVKMTLBufferRetention::~MVKMTLBufferRetention() { clear(); }

void MVKMTLBufferRetention::retainBuffer(id<MTLBuffer> buffer) {
	if (buffer && _buffers.insert(buffer).second) { [buffer retain]; }
}

void MVKMTLBufferRetention::clear() {
	// Drop both resources and bookkeeping at completion, even if Metal keeps its
	// command-buffer wrapper alive. Swapping first also makes cleanup idempotent.
	decltype(_buffers) buffers;
	buffers.swap(_buffers);
	for (id<MTLBuffer> buffer : buffers) { [buffer release]; }
}

@interface MVKMTLBufferRetentionOwner : NSObject {
@public
	MVKMTLBufferRetention retention;
}
@end

@implementation MVKMTLBufferRetentionOwner
- (void)dealloc {
	// Also handles a command buffer discarded without invoking its handlers.
	retention.clear();
	[super dealloc];
}
@end

MVKMTLBufferRetention* mvkGetMTLBufferRetention(id<MTLCommandBuffer> commandBuffer) {
	if (!commandBuffer) { return nullptr; }
	static char associationKey;
	MVKMTLBufferRetentionOwner* owner =
		(MVKMTLBufferRetentionOwner*)objc_getAssociatedObject(commandBuffer, &associationKey);
	if (!owner) {
		owner = [MVKMTLBufferRetentionOwner new];
		objc_setAssociatedObject(commandBuffer, &associationKey, owner,
								 OBJC_ASSOCIATION_RETAIN_NONATOMIC);
		// Capture only the owner: capturing commandBuffer would form a retain
		// cycle. Error completions release resources just like successful ones.
		[commandBuffer addCompletedHandler: ^(id<MTLCommandBuffer>) {
			owner->retention.clear();
		}];
		[owner release];
	}
	return &owner->retention;
}
