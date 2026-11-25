#pragma once

#include <aaudio/AAudio.h>

#include <atomic>
#include <cstdint>
#include <mutex>

#include <rex/audio/audio_driver.h>
#include <rex/thread.h>

namespace rex::audio::sdl {

class AAudioDriver : public AudioDriver {
 public:
  AAudioDriver(memory::Memory* memory, rex::thread::Semaphore* semaphore);
  ~AAudioDriver() override;

  bool Initialize();
  void SubmitFrame(uint32_t frame_ptr) override;
  void Pause() override;
  void Resume() override;
  uint32_t RecommendedInitialCredits(uint32_t configured_maximum) const override;
  void Flush() override;
  void Shutdown();

 private:
  static aaudio_data_callback_result_t DataCallback(AAudioStream* stream, void* user, void* audio,
                                                    int32_t num_frames);
  static void ErrorCallback(AAudioStream* stream, void* user, aaudio_result_t error);
  bool OpenStream();
  void CloseStream();

  static constexpr uint32_t kFrameSamples = 256;
  static constexpr uint32_t kStereoFloats = kFrameSamples * 2;
  static constexpr uint32_t kRing = 64;

  rex::thread::Semaphore* semaphore_ = nullptr;
  AAudioStream* stream_ = nullptr;
  bool silent_fallback_ = false;
  std::atomic<bool> needs_reopen_{false};
  std::mutex mutex_;
  float ring_[kRing][kStereoFloats];
  uint32_t head_ = 0;
  uint32_t count_ = 0;
  float cur_[kStereoFloats];
  uint32_t cur_off_ = kFrameSamples;
  float last_[2] = {0.0f, 0.0f};
};

}  // namespace rex::audio::sdl
