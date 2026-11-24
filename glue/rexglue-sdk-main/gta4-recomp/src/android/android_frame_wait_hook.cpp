#include <cstdio>
// sub_82A46D70(device r3, out r4) writes {0, submitted frame, submitted -
// completed} for the device. Its only caller, sub_828BF420, polls it in a
// loop without sleeping while two frames are pending, i.e. while the title
// waits for the renderer to submit a frame: a busy-wait that held a big core
// at ~7% of the main game thread's samples. Sleep briefly when the title is
// about to spin; the game's own loop still decides when to stop waiting.
//
// The same two-frame limit caps throughput: one frame passes the command
// worker, the recorder and the GPU in sequence (~75 ms at the bridge), so two
// in flight give ~38 ms per frame while every stage has slack. Reporting
// fewer pending frames lets the title run that many frames further ahead; the
// renderer's own queue still bounds the total.
#include <time.h>

#include <android/log.h>

#include "android/android_performance_hint.h"

#include <atomic>

#include <rex/cvar.h>

#include "gta4_init.h"

// Off by default: it gained nothing measurable, and the game-code crash in
// sub_8296E480 (stale object pointer) first appeared in builds that had it;
// changing the main thread's timing can expose game-side races.
REXCVAR_DEFINE_BOOL(gta4_frame_wait_sleep, false, "GTA IV/Performance",
                    "Sleep instead of spinning while the title waits for a frame slot");
REXCVAR_DEFINE_UINT32(gta4_extra_frames_in_flight, 0, "GTA IV/Performance",
                      "Frames the title may run ahead beyond its own limit of two pending "
                      "frames (more throughput, one frame of input latency each)")
    .range(0, 2);

// Latest game update rate, shown by the in-game "Show FPS" overlay.
std::atomic<float> g_liberty_title_fps{0.0f};

extern "C" void sub_82A46D70(PPCContext& ctx, uint8_t* base) {
  const uint32_t out = ctx.r4.u32;
  __imp__sub_82A46D70(ctx, base);
  uint32_t pending = REX_LOAD_U32(out + 8);
  if (pending >= 0x80000000u) return;
  // Title frame rate: how often the game submits a new frame (world update),
  // as opposed to how often frames are presented. Logged every 2 seconds.
  {
    static uint32_t last_submitted = 0;
    static uint32_t submitted_count = 0;
    static timespec window_start{};
    const uint32_t submitted = REX_LOAD_U32(out + 4);
    if (submitted != last_submitted) {
      last_submitted = submitted;
      ++submitted_count;
      gta4::android::NoteTitleFrame();
    }
    timespec now{};
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (window_start.tv_sec == 0) window_start = now;
    const double elapsed = double(now.tv_sec - window_start.tv_sec) +
                           double(now.tv_nsec - window_start.tv_nsec) / 1e9;
    if (elapsed >= 0.5) {
      g_liberty_title_fps.store(float(submitted_count / elapsed), std::memory_order_relaxed);
    }
    if (elapsed >= 2.0) {
      __android_log_print(ANDROID_LOG_WARN, "LibertyRecomp",
                          "title-fps: %.1f (game frames submitted per second)",
                          submitted_count / elapsed);
      submitted_count = 0;
      window_start = now;
    }
  }
  const uint32_t extra = REXCVAR_GET(gta4_extra_frames_in_flight);
  if (extra) {
    pending = pending > extra ? pending - extra : 0;
    REX_STORE_U32(out + 8, pending);
  }
  if (pending >= 2 && REXCVAR_GET(gta4_frame_wait_sleep)) {
    const timespec delay{0, 250 * 1000};
    nanosleep(&delay, nullptr);
  }
}

namespace {
struct ProbeAcc {
  std::atomic<int64_t> ns{0};
  std::atomic<uint32_t> calls{0};
};
ProbeAcc g_loop, g_handler;
std::atomic<int64_t> g_window{0};
int64_t NowNs() {
  timespec t{};
  clock_gettime(CLOCK_MONOTONIC, &t);
  return int64_t(t.tv_sec) * 1000000000LL + t.tv_nsec;
}
void ProbeReport(int64_t now) {
  int64_t start = g_window.load();
  if (start == 0) {
    g_window.compare_exchange_strong(start, now);
    return;
  }
  if (now - start < 2000000000LL || !g_window.compare_exchange_strong(start, now)) return;
  const int64_t ln = g_loop.ns.exchange(0), hn = g_handler.ns.exchange(0);
  const uint32_t lc = g_loop.calls.exchange(0), hc = g_handler.calls.exchange(0);
  __android_log_print(ANDROID_LOG_WARN, "BRANCHPROBE",
                      "loop calls=%u total=%.1fms | handler calls=%u total=%.1fms | window=%.0fms",
                      lc, ln / 1e6, hc, hn / 1e6, (now - start) / 1e6);
}
}  // namespace

extern "C" void sub_821BB6D8(PPCContext& ctx, uint8_t* base) {
  const int64_t a = NowNs();
  __imp__sub_821BB6D8(ctx, base);
  const int64_t b = NowNs();
  g_handler.ns.fetch_add(b - a);
  g_handler.calls.fetch_add(1);
  ProbeReport(b);
}

namespace {
ProbeAcc g_fn[7];
std::atomic<int64_t> g_fn_window{0};
const char* const kFnNames[7] = {"2728","42F0","D41A8","C6620","E7000","BFC00","3E348"};
void FnReport(int64_t now) {
  int64_t start = g_fn_window.load();
  if (start == 0) {
    g_fn_window.compare_exchange_strong(start, now);
    return;
  }
  if (now - start < 2000000000LL || !g_fn_window.compare_exchange_strong(start, now)) return;
  char line[512];
  int n = 0;
  for (int i = 0; i < 7; ++i) {
    const int64_t ns = g_fn[i].ns.exchange(0);
    const uint32_t c = g_fn[i].calls.exchange(0);
    n += std::snprintf(line + n, sizeof(line) - n, "%s=%.0fms/%u ", kFnNames[i], ns / 1e6, c);
  }
  __android_log_print(ANDROID_LOG_WARN, "FNPROBE", "%s window=%.0fms", line, (now - start) / 1e6);
}
}  // namespace

#define PROBE_FN(NAME, IDX) \
  extern "C" void NAME(PPCContext& ctx, uint8_t* base) { \
    const int64_t a = NowNs(); \
    __imp__##NAME(ctx, base); \
    const int64_t b = NowNs(); \
    g_fn[IDX].ns.fetch_add(b - a); \
    g_fn[IDX].calls.fetch_add(1); \
    FnReport(b); \
  }
PROBE_FN(sub_82902728, 0)
PROBE_FN(sub_828C42F0, 1)
