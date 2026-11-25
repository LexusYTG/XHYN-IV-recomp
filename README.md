# XHYN-IV-recomp

GTA IV on Android, recompiled natively, tuned for low-end ARM Mali GPUs.

> [!WARNING]
> **Experimental.** Development and testing happen on a single phone: Redmi 13C (MediaTek Helio G85, Mali-G52 MC2, Android 15). Expect crashes, missing effects and rough edges. Save often.

## What this is

An Android-only fork of [Liberty Recompiled](https://github.com/OZORDI/LibertyRecomp), the static recompilation of the Xbox 360 version of Grand Theft Auto IV, built on the [ReXGlue SDK](https://github.com/rexglue/rexglue-sdk). The game runs natively on arm64 Android, with no emulator in between, through the phone's own Vulkan driver.

Its ancestors target Snapdragon (Adreno) phones and the Dimensity 8400. This fork aims at the bottom of the range, the Mali-G52 class, with one goal: **a stable 17 FPS on the Redmi 13C**, even if that means giving up effects such as dynamic lights and dynamic shadows. It is also stripped of desktop code and is built entirely on the phone, in Termux.

For Snapdragon phones use the upstream [LibertyRecompAndroid](https://github.com/vaduur/LibertyRecompAndroid) instead.

## Status

The game installs, boots and plays. Informal readings on one phone (360p, minimum settings): about 18 FPS indoors looking away from lights, 7 to 9 looking straight at a light, and roughly 3 to 12 FPS outdoors depending on how many lights are on screen. Visible lights are the biggest cost found so far. That is an observation, not yet a profile.

Memory use is about 1.3 GB at load and 2 to 2.5 GB in game. It has not been analysed yet.

## Requirements

- arm64 Android 10 (API 29) or newer.
- A Vulkan 1.2 driver with `shaderInt64`, `sampleRateShading`, `independentBlend`, `bufferDeviceAddress` and `dynamicRendering`.
- Roughly 7 GB for the installed game, plus room for the disc image while installing.
- A gamepad is recommended. Without one the game shows an on-screen controller (touch layout "Liberty" by default, or an Xbox layout).

## Game files

This project includes no game assets. You need your own legally obtained copy of the Xbox 360 game:

- The **Xbox 360 GTA IV disc** as an `.iso`. Only the USA (NTSC-U) release has been verified.
- **Title Update 8** for that release (version 0.0.8.5), as the STFS package or a raw `default.xexp`. The PAL update is rejected.

The files used during development:

| File | Size | MD5 | SHA-1 |
|---|---|---|---|
| `Grand Theft Auto IV (USA) (En,Fr,De,Es,It).iso` | 7,835,492,352 bytes | `f0a046aed1520a913b2125f0a69ee7d0` | `caa48dfb3b1b2fe3131e61fc66e35a22c6d0ba13` |
| `TU_1A581VI_000000K000000.0000000000205` | 3,715,072 bytes | `f041b5f6721d7ee578560bac21668826` | `88d1c438814298b6da987b3ad5edc7e48a769ac9` |

See the [dumping guide](docs/DUMPING-en.md) for how to get them from your own console and disc.

## Install

1. Install the APK and open it. The launcher shows the game status.
2. In the Game panel pick the disc image and the Title Update 8 file. The app asks once for All files access. Alternatively, copy both into `Android/data/com.libertyrecomp/files/install/`.
3. Press Install, wait for it to finish, then press Play.

## Settings

The launcher has a graphics card with: resolution, upscaler and FSR quality, anti-aliasing, frame limit, draw distance, population density, shadow quality, sun shadows, reflections, lazy resolves, vertex cache sort, motion blur, reflection anti-aliasing, run-ahead frames and CPU boost. Every option starts on Default, which keeps the value from `args.txt`. Press Save changes, then Play. Two presets (Performance and Maximum performance, 360p) set many options at once.

Note that "Sun shadows" sets how often the shadow map is redrawn, it does not turn shadows off. The "Sun shadows off" checkbox under Performance test settings does.

`args.txt` lives in `Android/data/com.libertyrecomp/files/`. It holds one argument per line, and `#` starts a comment. The launcher removes repeated options before starting the game, and the launcher's choice wins over `args.txt`.

A performance overlay (FPS, frame time, CPU usage and clock, memory) can be shown in the top-left corner during play. System counters such as Game Turbo count repeated frames, so use the overlay's FPS instead.

## What changed in this fork

Specific to this branch:

- **Android only.** Windows, macOS and iOS code paths are removed. Leftovers that cannot run on a phone (Metal and D3D12 backends, MoltenVK, desktop-only tools) are being deleted.
- **Built on the phone.** `mklibertyrecomp.sh` builds the APK in Termux: native libraries with the NDK, the APK with Gradle. No PC and no CI needed. It replaces the Windows-only `os/android/scripts/build_native.sh`.
- **No Vulkan driver proxy on Mali.** In the Mali build the runtime opens the system Vulkan driver directly. The proxy `libvulkan.so` (a libadrenotools layer for Turnip, which only exists for Adreno) is not loaded.
- **Internal resolution cap.** `gta4_output_height_cap` lowers the internal resolution of the deferred rendering phase. Values down to 144 are accepted.
- **Texture mip skipping.** `gta4_texture_skip_mips` (0 to 4) skips the largest mips of 2D color textures uploaded by the CPU. It never goes below 256 on the shorter side. It also lowers RAM use and CPU decode work.

Inherited from the Mali Edition:

- Shaders are rewritten at load so `shaderClipDistance`, which Mali lacks, is not required. User clip planes are skipped, so some reflections or cut-off geometry may look wrong.
- BC (DXT) textures are decoded to RGBA8 on the CPU, because Mali cannot sample BC. This uses 4 to 8 times more texture memory and can stutter while textures load.
- The launcher settings and the performance overlay.

## Roadmap

In order of work:

- [ ] Lower internal resolution and texture sizes (in progress)
- [ ] Find what the 1280x1280 depth clamp pass in the profiler really is
- [ ] Fixed, precomputed lighting: no dynamic local lights and no dynamic shadows, with a simple time-of-day function instead of per-frame lighting
- [ ] Study Render Clip Distance (CPU side) and whether it can be disabled or replaced
- [ ] Real multithreading and big.LITTLE placement (Helio G85: 2x A75 + 6x A55)
- [ ] Minimum LOD, with no streaming hitches
- [ ] Cache for frequently used textures (street lamps, lights)
- [ ] Find out why the shader and pipeline caches do not seem to help
- [ ] Break down the RAM use
- [ ] Finish removing non-Android code and review code quality

## Building

Everything below runs in Termux on the phone.

Prerequisites:

- Android SDK with platform 34 and build-tools 34.0.0 (`ANDROID_HOME`), and NDK 29.0.14206865 (`ANDROID_NDK`).
- JDK 17, Gradle (9.9.1 with AGP 8.7.2 is what is used), Termux `aapt2`, `adb`, cmake and ninja.
- `spirv-tools` (`pkg install spirv-tools`).
- An aarch64 `dxc`. It is installed by hand, so pass `-DGTA4_NATIVE_DXC=<path to dxc>` and `-DGTA4_NATIVE_DXC_LIBRARY_PATH=<directory with libdxcompiler.so>` when configuring.

Configure once with the `android-release` preset (see `CMakePresets.json`), then:

    cmake --build out/build/android-release -j6 --target LibertyRecomp liberty_install
    bash mklibertyrecomp.sh release

The script stages the native libraries and resources, builds and signs the APK, and installs it with `adb install -r`. Options: `debug|release`, `clean`, `cleanj`, `cleanl`, `noinstall`. Environment variables: `JOBS`, `GRADLE_DIR`, `LIBERTY_GPU` (default `mali`), `BDIR`, `REPO`.

## Reporting problems and profiling

Attach `Android/data/com.libertyrecomp/files/last_launch.txt` and, if the game starts, a log from Play (Debug Log), saved in `files/debug_logs/`.

To profile, add `--diagnostics` and `--diagnostics-categories=native-profiler` to `args.txt`. Profile captures are written to `files/LibertyRecomp/Diagnostics/`.

Much of the original Android port was written with AI assistance, as its author says, and so is much of this fork. Reviewing code quality is on the roadmap.

For background on the port, see [docs/android/PORTING.md](docs/android/PORTING.md), which describes the original Adreno build, and the [upstream README](README_UPSTREAM.md).

## Credits

- [Liberty Recompiled](https://github.com/OZORDI/LibertyRecomp) by OZORDI and contributors, and the [ReXGlue SDK](https://github.com/rexglue/rexglue-sdk) it is built on. Almost all of the hard work of recompiling GTA IV is theirs.
- [LibertyRecompAndroid](https://github.com/vaduur/LibertyRecompAndroid) by vaduur, the Android port this fork descends from.
- [LibertyRecompAndroidMaliVer](https://github.com/LexusYTG/XHYN-IV-recomp) by prollynotzjay, the Mali Edition: Mali support, launcher settings and overlay.
- [Mesa Turnip](https://docs.mesa3d.org/drivers/freedreno.html), the open-source Adreno Vulkan driver, and [libadrenotools](https://github.com/bylaws/libadrenotools) by bylaws.
- The Vulkan driver proxy comes from [skate3-android](https://github.com/andrewnakas/skate3-android) and [skate3-pocket](https://github.com/AlanConstantino/skate3-pocket).
- [SDL3](https://github.com/libsdl-org/SDL) for the Android activity, input and audio.
- [nfsmw-nx](https://github.com/StevensND/nfsmw-nx) by StevensND, a ReXGlue port of Need for Speed: Most Wanted to the Nintendo Switch. Our direct-calls tool (`gta4-recomp/tools/direct_calls.py`) is adapted from their `llamadas_directas.py`, and their work on busy-waits and thread placement informed this port's frame-wait hook and thread pinning experiments.

Grand Theft Auto IV is a trademark of Take-Two Interactive Software. This project is not affiliated with or endorsed by Rockstar Games or Take-Two Interactive.
