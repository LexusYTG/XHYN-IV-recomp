#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
#include <string>
#include <rex/assert.h>
#include <rex/audio/conversion.h>
#include <rex/audio/flags.h>
#include <rex/audio/sdl/sdl_audio_driver.h>
#include <rex/cvar.h>
#include <rex/dbg.h>
#include <rex/logging.h>
#include <rex/perf/counter.h>
#include <SDL3/SDL.h>
#include <aaudio/AAudio.h>
#include <algorithm>
#include <chrono>
#include <cstring>
#include <mutex>

#include <rex/audio/sdl/aaudio_driver.h>

namespace rex::audio::sdl {

AAudioDriver::AAudioDriver(memory::Memory* memory, rex::thread::Semaphore* semaphore)
    : AudioDriver(memory), semaphore_(semaphore) {
  std::memset(ring_, 0, sizeof(ring_));
  std::memset(cur_, 0, sizeof(cur_));
}

AAudioDriver::~AAudioDriver() {
  Shutdown();
}

aaudio_data_callback_result_t AAudioDriver::DataCallback(AAudioStream*, void* user, void* audio,
                                                         int32_t num_frames) {
  auto* d = static_cast<AAudioDriver*>(user);
  float* out = static_cast<float*>(audio);
  uint32_t popped = 0;
  int32_t done = 0;
  while (done < num_frames) {
    if (d->cur_off_ >= kFrameSamples) {
      std::lock_guard<std::mutex> guard(d->mutex_);
      if (d->count_ == 0) break;
      std::memcpy(d->cur_, d->ring_[d->head_], sizeof(d->cur_));
      d->head_ = (d->head_ + 1) % kRing;
      --d->count_;
      d->cur_off_ = 0;
      ++popped;
    }
    const int32_t take =
        std::min<int32_t>(num_frames - done, int32_t(kFrameSamples - d->cur_off_));
    std::memcpy(out + size_t(done) * 2, d->cur_ + size_t(d->cur_off_) * 2,
                size_t(take) * 2 * sizeof(float));
    done += take;
    d->cur_off_ += uint32_t(take);
  }
  if (done > 0) {
    d->last_[0] = out[size_t(done - 1) * 2];
    d->last_[1] = out[size_t(done - 1) * 2 + 1];
  }
  for (int32_t i = done; i < num_frames; ++i) {
    d->last_[0] *= 0.97f;
    d->last_[1] *= 0.97f;
    out[size_t(i) * 2] = d->last_[0];
    out[size_t(i) * 2 + 1] = d->last_[1];
  }
  if (popped) d->semaphore_->Release(popped, nullptr);
  return AAUDIO_CALLBACK_RESULT_CONTINUE;
}

void AAudioDriver::ErrorCallback(AAudioStream*, void* user, aaudio_result_t) {
  static_cast<AAudioDriver*>(user)->needs_reopen_.store(true);
}

bool AAudioDriver::OpenStream() {
  AAudioStreamBuilder* builder = nullptr;
  if (AAudio_createStreamBuilder(&builder) != AAUDIO_OK || !builder) return false;
  AAudioStreamBuilder_setDirection(builder, AAUDIO_DIRECTION_OUTPUT);
  AAudioStreamBuilder_setSharingMode(builder, AAUDIO_SHARING_MODE_SHARED);
  AAudioStreamBuilder_setFormat(builder, AAUDIO_FORMAT_PCM_FLOAT);
  AAudioStreamBuilder_setChannelCount(builder, 2);
  AAudioStreamBuilder_setSampleRate(builder, 48000);
  AAudioStreamBuilder_setPerformanceMode(builder, AAUDIO_PERFORMANCE_MODE_NONE);
  AAudioStreamBuilder_setDataCallback(builder, DataCallback, this);
  AAudioStreamBuilder_setErrorCallback(builder, ErrorCallback, this);
  AAudioStream* stream = nullptr;
  const aaudio_result_t r = AAudioStreamBuilder_openStream(builder, &stream);
  AAudioStreamBuilder_delete(builder);
  if (r != AAUDIO_OK || !stream) return false;
  const int32_t burst = AAudioStream_getFramesPerBurst(stream);
  AAudioStream_setBufferSizeInFrames(stream, burst * 4);
  if (AAudioStream_requestStart(stream) != AAUDIO_OK) {
    AAudioStream_close(stream);
    return false;
  }
  stream_ = stream;
  REXAPU_WARN("gta4-aaudio: opened rate={} burst={}", AAudioStream_getSampleRate(stream), burst);
  return true;
}

void AAudioDriver::CloseStream() {
  if (stream_) {
    AAudioStream_requestStop(stream_);
    AAudioStream_close(stream_);
    stream_ = nullptr;
  }
}

bool AAudioDriver::Initialize() {
  if (!OpenStream()) {
    REXAPU_WARN("gta4-aaudio: open failed, using paced silent fallback");
    silent_fallback_ = true;
  }
  return true;
}

void AAudioDriver::SubmitFrame(uint32_t frame_ptr) {
  if (needs_reopen_.exchange(false)) {
    CloseStream();
    silent_fallback_ = !OpenStream();
  }
  if (silent_fallback_ || !stream_) {
    rex::thread::Sleep(std::chrono::microseconds(5333));
    semaphore_->Release(1, nullptr);
    return;
  }
  const auto in = memory_->TranslateVirtual<float*>(frame_ptr);
  float tmp[kStereoFloats];
  conversion::sequential_6_BE_to_interleaved_2_LE(tmp, in, kFrameSamples);
  bool dropped = false;
  {
    std::lock_guard<std::mutex> guard(mutex_);
    if (count_ >= kRing) {
      dropped = true;
    } else {
      std::memcpy(ring_[(head_ + count_) % kRing], tmp, sizeof(tmp));
      ++count_;
    }
  }
  if (dropped) semaphore_->Release(1, nullptr);
}

void AAudioDriver::Pause() {
  if (stream_) AAudioStream_requestPause(stream_);
}

void AAudioDriver::Resume() {
  if (stream_) AAudioStream_requestStart(stream_);
}

uint32_t AAudioDriver::RecommendedInitialCredits(uint32_t configured_maximum) const {
  return configured_maximum;
}

void AAudioDriver::Flush() {
  std::lock_guard<std::mutex> guard(mutex_);
  head_ = 0;
  count_ = 0;
}

void AAudioDriver::Shutdown() {
  CloseStream();
}

}  // namespace rex::audio::sdl
