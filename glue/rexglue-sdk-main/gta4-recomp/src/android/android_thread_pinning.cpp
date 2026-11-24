// Keeps the busiest threads of the process on dedicated big cores.
//
// Six threads carry the frame (guest render thread, recorder, command worker,
// and three lighter ones) and the scheduler moves them freely: the guest
// render thread was seen on a little core while the prime core idled, and the
// recorder spent ~30% of each frame off-CPU. Every two seconds this ranks the
// process's threads by CPU time over the last interval, gives the top ones a
// big core each (fastest first) and confines every other thread to the
// remaining cores. Threads are re-ranked continuously, so roles that move
// between threads (loading, cutscenes) follow.
#include <dirent.h>
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include <android/log.h>

#include <rex/cvar.h>

REXCVAR_DEFINE_UINT32(gta4_thread_pinning, 0, "GTA IV/Performance",
                      "Give the N busiest threads a big core each and keep the others off them "
                      "(0 = leave scheduling to Android)")
    .range(0, 4);

namespace {

std::string ThreadName(pid_t tid) {
  char path[64];
  std::snprintf(path, sizeof(path), "/proc/self/task/%d/comm", tid);
  FILE* file = std::fopen(path, "r");
  if (!file) return {};
  char buffer[32] = {};
  size_t length = std::fread(buffer, 1, sizeof(buffer) - 1, file);
  std::fclose(file);
  while (length && (buffer[length - 1] == '\n' || buffer[length - 1] == ' ')) --length;
  return std::string(buffer, length);
}

std::vector<int> CoresByMaxFrequency() {
  std::vector<std::pair<long, int>> cores;
  const long count = sysconf(_SC_NPROCESSORS_CONF);
  for (int cpu = 0; cpu < count && cpu < CPU_SETSIZE; ++cpu) {
    char path[96];
    std::snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq", cpu);
    long frequency = 0;
    if (FILE* file = std::fopen(path, "r")) {
      if (std::fscanf(file, "%ld", &frequency) != 1) frequency = 0;
      std::fclose(file);
    }
    cores.emplace_back(frequency, cpu);
  }
  std::stable_sort(cores.begin(), cores.end(),
                   [](const auto& a, const auto& b) { return a.first > b.first; });
  std::vector<int> result;
  for (const auto& [frequency, cpu] : cores) result.push_back(cpu);
  return result;
}

void SetAffinity(pid_t tid, const cpu_set_t& set) {
  syscall(__NR_sched_setaffinity, tid, sizeof(set), &set);
}

pid_t HottestThread(const char* prefix) {
  pid_t best = 0;
  unsigned long best_ticks = 0;
  if (DIR* directory = opendir("/proc/self/task")) {
    while (dirent* entry = readdir(directory)) {
      const pid_t tid = pid_t(std::atoi(entry->d_name));
      if (tid <= 0) continue;
      if (ThreadName(tid).rfind(prefix, 0) != 0) continue;
      char path[64];
      std::snprintf(path, sizeof(path), "/proc/self/task/%d/stat", tid);
      FILE* file = std::fopen(path, "r");
      if (!file) continue;
      char line[1024] = {};
      size_t length = std::fread(line, 1, sizeof(line) - 1, file);
      std::fclose(file);
      line[length] = 0;
      const char* close = std::strrchr(line, ')');
      if (!close) continue;
      unsigned long utime = 0, stime = 0;
      if (std::sscanf(close + 1, " %*c %*d %*d %*d %*d %*d %*u %*lu %*lu %*lu %*lu %lu %lu", &utime, &stime) != 2) continue;
      if (utime + stime > best_ticks) {
        best_ticks = utime + stime;
        best = tid;
      }
    }
    closedir(directory);
  }
  return best;
}

void LogHot(pid_t tid) {
  static pid_t prev_tid = 0;
  static unsigned long long pr = 0, pw = 0, ps = 0;
  static std::chrono::steady_clock::time_point pt;
  char path[64];
  std::snprintf(path, sizeof(path), "/proc/self/task/%d/schedstat", tid);
  unsigned long long r = 0, w = 0, sl = 0;
  FILE* f = std::fopen(path, "r");
  if (!f) return;
  if (std::fscanf(f, "%llu %llu %llu", &r, &w, &sl) != 3) {
    std::fclose(f);
    return;
  }
  std::fclose(f);
  const auto now = std::chrono::steady_clock::now();
  int cpu = -1;
  std::snprintf(path, sizeof(path), "/proc/self/task/%d/stat", tid);
  if (FILE* sf = std::fopen(path, "r")) {
    char line[1024] = {};
    size_t n = std::fread(line, 1, sizeof(line) - 1, sf);
    std::fclose(sf);
    line[n] = 0;
    if (const char* close = std::strrchr(line, ')')) {
      char* save = nullptr;
      char* copy = const_cast<char*>(close + 1);
      int idx = 0;
      for (char* tok = strtok_r(copy, " ", &save); tok; tok = strtok_r(nullptr, " ", &save)) {
        if (++idx == 37) {
          cpu = std::atoi(tok);
          break;
        }
      }
    }
  }
  cpu_set_t mask;
  CPU_ZERO(&mask);
  syscall(__NR_sched_getaffinity, tid, sizeof(mask), &mask);
  unsigned bits = 0;
  for (int i = 0; i < 8; ++i)
    if (CPU_ISSET(i, &mask)) bits |= 1u << i;
  if (tid == prev_tid) {
    const double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(now - pt).count();
    __android_log_print(ANDROID_LOG_WARN, "XTSTAT",
                        "tid=%d run=%.0f%% wait=%.0f%% slices=%llu cpu=%d mask=0x%02x",
                        tid, 100.0 * (r - pr) / ns, 100.0 * (w - pw) / ns, sl - ps, cpu, bits);
  }
  prev_tid = tid;
  pr = r;
  pw = w;
  ps = sl;
  pt = now;
}

void PinningLoop() {
  const std::vector<int> cores = CoresByMaxFrequency();
  if (cores.size() < 8) return;
  cpu_set_t main_set, recorder, audio, kernel, sdl, rest, render, xt;
  CPU_ZERO(&xt);
  CPU_SET(cores[1], &xt);
  CPU_ZERO(&render);
  CPU_SET(cores[0], &render);
  CPU_ZERO(&main_set);
  CPU_ZERO(&recorder);
  CPU_ZERO(&audio);
  CPU_ZERO(&kernel);
  CPU_ZERO(&sdl);
  CPU_ZERO(&rest);
  CPU_SET(cores[0], &main_set);
  CPU_SET(cores[0], &recorder);
  CPU_SET(cores[6], &audio);
  CPU_SET(cores[7], &kernel);
  CPU_SET(cores[5], &sdl);
  for (int i = 2; i <= 4; ++i) CPU_SET(cores[i], &rest);
  int last[6] = {-1, -1, -1, -1, -1, -1};
  for (;;) {
    std::this_thread::sleep_for(std::chrono::seconds(2));
    int count[6] = {0, 0, 0, 0, 0, 0};
    const pid_t hot = HottestThread("XT82849940");
    if (hot > 0) LogHot(hot);
    if (DIR* directory = opendir("/proc/self/task")) {
      while (dirent* entry = readdir(directory)) {
        const pid_t tid = pid_t(std::atoi(entry->d_name));
        if (tid <= 0) continue;
        const std::string name = ThreadName(tid);
        auto is = [&](const char* prefix) { return name.rfind(prefix, 0) == 0; };
        if (tid == hot) {
          SetAffinity(tid, xt);
          ++count[5];
        } else if (is("GtaRender")) {
          SetAffinity(tid, render);
          ++count[0];
        } else if (is("Main XThread")) {
          SetAffinity(tid, main_set);
          ++count[0];
        } else if (is("GtaRecorder")) {
          SetAffinity(tid, recorder);
          ++count[1];
        } else if (is("Audio Worker") || is("XMA Decoder") || is("SDLAudio")) {
          SetAffinity(tid, audio);
          ++count[2];
        } else if (is("Kernel Host") || is("Kernel File")) {
          SetAffinity(tid, kernel);
          ++count[3];
        } else if (is("SDL")) {
          SetAffinity(tid, sdl);
          ++count[4];
        } else {
          SetAffinity(tid, rest);
          ++count[5];
        }
      }
      closedir(directory);
    }
    bool changed = false;
    for (int i = 0; i < 6; ++i) {
      if (count[i] != last[i]) changed = true;
      last[i] = count[i];
    }
    if (changed) {
      __android_log_print(ANDROID_LOG_WARN, "LibertyRecomp",
                          "thread-pinning: main=%d(cpu%d) recorder=%d(cpu%d) audio=%d(cpu%d) kernel=%d(cpu%d) sdl=%d(cpu%d) rest=%d",
                          count[0], cores[0], count[1], cores[1], count[2], cores[6], count[3], cores[7],
                          count[4], cores[5], count[5]);
    }
  }
}

const bool g_pinning_started = [] {
  std::thread(PinningLoop).detach();
  return true;
}();

}  // namespace
