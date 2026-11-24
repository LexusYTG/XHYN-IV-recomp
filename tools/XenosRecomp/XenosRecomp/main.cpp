#include <deque>
#include <fstream>
#include <mutex>
#include <thread>

#include "shader.h"
#include "shader_common.h"
#include "shader_recompiler.h"
#include "dxc_compiler.h"
#include "shader_source_preservation.h"

#ifdef XENOS_RECOMP_AIR
#include "air_compiler.h"
#endif

static std::unique_ptr<uint8_t[]> readAllBytes(const char* filePath, size_t& fileSize)
{
    FILE* file = fopen(filePath, "rb");
    fseek(file, 0, SEEK_END);
    fileSize = ftell(file);
    fseek(file, 0, SEEK_SET);
    auto data = std::make_unique<uint8_t[]>(fileSize);
    fread(data.get(), 1, fileSize, file);
    fclose(file);
    return data;
}

static void writeAllBytes(const char* filePath, const void* data, size_t dataSize)
{
    FILE* file = fopen(filePath, "wb");
    fwrite(data, 1, dataSize, file);
    fclose(file);
}

struct RecompiledShader
{
    uint8_t* data = nullptr;
    IDxcBlob* dxil = nullptr;
    std::vector<uint8_t> spirv;
    std::vector<uint8_t> lateSpirv;
    std::vector<uint8_t> air;
    uint32_t specConstantsMask = 0;
    uint32_t usedTextureMask = 0;
    bool recoveredPixelShader = false;
    std::string recoveredEarly;
    std::string recoveredLate;
};

void recompileShader(RecompiledShader& shader, const std::string_view include, std::atomic<uint32_t>& progress, uint32_t numShaders)
{
    static DxcCompiler dxcCompiler;
    static std::mutex dxcCompilerMutex;

    thread_local ShaderRecompiler recompiler;
    // XENOS_RECOMP_MERGE_PREDICATES=1: consecutive predicated instructions
    // share one block (see ShaderRecompiler::openPredicate).
    static const bool mergePredicates = [] {
        const char* value = std::getenv("XENOS_RECOMP_MERGE_PREDICATES");
        return value && value[0] == '1';
    }();
    recompiler = {};
    recompiler.mergePredicates = mergePredicates;
    const bool recovered = !shader.recoveredEarly.empty();
    if (!recovered)
    {
        recompiler.recompile(shader.data, include);
        // XENOS_RECOMP_DUMP_DIR: write each translated shader's HLSL there.
        if (const char* dumpDir = std::getenv("XENOS_RECOMP_DUMP_DIR"))
        {
            const auto name = fmt::format("{}/{:016x}.hlsl", dumpDir,
                XXH3_64bits(shader.data, 64));
            writeAllBytes(name.c_str(), recompiler.out.data(), recompiler.out.size());
        }
        shader.specConstantsMask = recompiler.specConstantsMask;
        shader.usedTextureMask = recompiler.usedTextureMask;
    }
    const std::string& earlySource = recovered ? shader.recoveredEarly : recompiler.out;
    const bool isPixelShader = recovered ? shader.recoveredPixelShader : recompiler.isPixelShader;

#ifdef XENOS_RECOMP_DXIL
    {
        std::lock_guard lock(dxcCompilerMutex);
        shader.dxil = dxcCompiler.compile(earlySource, isPixelShader, shader.specConstantsMask != 0, false);
    }
    assert(shader.dxil != nullptr);
    assert(*(reinterpret_cast<uint32_t *>(shader.dxil->GetBufferPointer()) + 1) != 0 && "DXIL was not signed properly!");
#endif

#ifdef XENOS_RECOMP_AIR
    shader.air = AirCompiler::compile(earlySource);
#endif

    IDxcBlob* spirv;
    {
        std::lock_guard lock(dxcCompilerMutex);
        spirv = dxcCompiler.compile(earlySource, isPixelShader, false, true);
    }
    assert(spirv != nullptr);

    bool result = smolv::Encode(spirv->GetBufferPointer(), spirv->GetBufferSize(), shader.spirv, smolv::kEncodeFlagStripDebugInfo);
    assert(result);

    spirv->Release();

    if (recovered ? !shader.recoveredLate.empty() :
        (isPixelShader && (shader.specConstantsMask & SPEC_CONSTANT_ALPHA_TEST_CAPABILITY_MASK) == SPEC_CONSTANT_ALPHA_TEST_CAPABILITY_MASK))
    {
        if (!recovered)
        {
            recompiler = {};
            recompiler.mergePredicates = mergePredicates;
            recompiler.recompile(shader.data, include, false);
            assert((recompiler.specConstantsMask & SPEC_CONSTANT_ALPHA_TEST_CAPABILITY_MASK) == SPEC_CONSTANT_ALPHA_TEST_CAPABILITY_MASK);
            assert(recompiler.usedTextureMask == shader.usedTextureMask);
        }

        {
            std::lock_guard lock(dxcCompilerMutex);
            spirv = dxcCompiler.compile(recovered ? shader.recoveredLate : recompiler.out, true, false, true);
        }
        assert(spirv != nullptr);

        result = smolv::Encode(spirv->GetBufferPointer(), spirv->GetBufferSize(), shader.lateSpirv, smolv::kEncodeFlagStripDebugInfo);
        assert(result);

        spirv->Release();
    }

    size_t currentProgress = ++progress;
    if ((currentProgress % 10) == 0 || (currentProgress == numShaders - 1))
        fmt::println("Recompiling shaders... {}%", currentProgress / float(numShaders) * 100.0f);
}

int main(int argc, char** argv)
{
#ifndef XENOS_RECOMP_INPUT
    if (argc < 4)
    {
        printf("Usage: XenosRecomp [input path] [output path] [shader common header file path] "
               "[--preserve-sources baseline-sources.tsv] [--recovered-sources recovered-sources.tsv]\n");
        return 0;
    }
#endif

    const char* input =
#ifdef XENOS_RECOMP_INPUT 
        XENOS_RECOMP_INPUT
#else
        argv[1]
#endif
    ;

    const char* output =
#ifdef XENOS_RECOMP_OUTPUT 
        XENOS_RECOMP_OUTPUT
#else
        argv[2]
#endif
        ;
    
    const char* includeInput =
#ifdef XENOS_RECOMP_INCLUDE_INPUT
        XENOS_RECOMP_INCLUDE_INPUT
#else
        argv[3]
#endif
        ;

    PreferredShaderSources preferredSources;
    std::string preferredSourcePath;
    std::string recoveredSourcePath;
    std::map<uint64_t, RecoveredShaderSource> recoveredSources;
    for (int argument = 4; argument < argc; argument += 2)
    {
        const std::string_view flag(argv[argument]);
        std::string* destination = flag == "--preserve-sources" ? &preferredSourcePath :
            flag == "--recovered-sources" ? &recoveredSourcePath : nullptr;
        if (!destination || !destination->empty() || argument + 1 >= argc)
        {
            fmt::println(stderr, "Invalid/duplicate shader source option: {}", flag);
            return 1;
        }
        *destination = argv[argument + 1];
    }
    if (!preferredSourcePath.empty() || !recoveredSourcePath.empty())
    {
        if (!std::filesystem::is_directory(input))
        {
            fmt::println(stderr, "Shader preservation requires a complete input directory");
            return 1;
        }
        try
        {
            std::ifstream sourceMap(preferredSourcePath);
            preferredSources = ReadPreferredShaderSources(sourceMap);
            if (!recoveredSourcePath.empty())
            {
                std::ifstream recoveredMap(recoveredSourcePath);
                recoveredSources = ReadRecoveredShaderSources(recoveredMap, preferredSources);
            }
        }
        catch (const std::exception& error)
        {
            fmt::println(stderr, "Cannot load shader-preservation source map: {}", error.what());
            return 1;
        }
    }

    size_t includeSize = 0;
    auto includeData = readAllBytes(includeInput, includeSize);
    std::string_view include(reinterpret_cast<const char*>(includeData.get()), includeSize);

    if (std::filesystem::is_directory(input))
    {
        std::vector<std::unique_ptr<uint8_t[]>> files;
        std::map<XXH64_hash_t, RecompiledShader> shaders;
        std::map<XXH64_hash_t, std::string> shaderFilenames;

        for (auto& file : std::filesystem::recursive_directory_iterator(input))
        {
            if (std::filesystem::is_directory(file))
            {
                continue;
            }
            
            size_t fileSize = 0;
            auto fileData = readAllBytes(file.path().string().c_str(), fileSize);
            bool foundAny = false;
            const std::string sourceName = ShaderCacheSourceName(file.path().string());

            for (size_t i = 0; fileSize > sizeof(ShaderContainer) && i < fileSize - sizeof(ShaderContainer) - 1;)
            {
                auto shaderContainer = reinterpret_cast<const ShaderContainer*>(fileData.get() + i);
                size_t dataSize = shaderContainer->virtualSize + shaderContainer->physicalSize;

                if ((shaderContainer->flags & 0xFFFFFF00) == 0x102A1100 &&
                    dataSize <= (fileSize - i) &&
                    shaderContainer->field1C == 0 &&
                    shaderContainer->field20 == 0)
                {
                    XXH64_hash_t hash = XXH3_64bits(shaderContainer, dataSize);
                    // A baseline hash is selected only from its original source.
                    // Continue scanning every file/container, including duplicates;
                    // no corpus files are removed to influence traversal order.
                    if (IsPreferredShaderSource(preferredSources, hash, sourceName))
                    {
                        auto shader = shaders.try_emplace(hash);
                        if (shader.second)
                        {
                            shader.first->second.data = fileData.get() + i;
                            foundAny = true;
                            shaderFilenames[hash] = sourceName;
                        }
                    }

                    i += dataSize;
                }
                else
                {
                    // RAGE FXC fragments don't guarantee that embedded shader
                    // containers start on a 32-bit boundary.
                    ++i;
                }
            }

            if (foundAny)
                files.emplace_back(std::move(fileData));
        }

        try
        {
            const auto recoveryDirectory = std::filesystem::path(recoveredSourcePath).parent_path();
            const auto loadSource = [&](const std::string& filename, bool pixel)
            {
                std::ifstream stream(recoveryDirectory / filename, std::ios::binary);
                if (!stream)
                    throw std::runtime_error("cannot read recovered source: " + filename);
                std::string source((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
                if (stream.bad() || !HasCurrentRecoveredShaderPrefix(source, include, pixel))
                    throw std::runtime_error("recovered source has stale/missing stage preamble or common header: " + filename);
                return source;
            };
            for (const auto& [hash, source] : recoveredSources)
            {
                // An actual original container always takes precedence over recovery.
                if (shaders.find(hash) != shaders.end())
                    continue;
                auto& shader = shaders[hash];
                shader.recoveredEarly = loadSource(source.early, source.isPixelShader);
                if (!source.late.empty())
                    shader.recoveredLate = loadSource(source.late, source.isPixelShader);
                shader.recoveredPixelShader = source.isPixelShader;
                shader.specConstantsMask = source.specConstantsMask;
                shader.usedTextureMask = source.usedTextureMask;
                shaderFilenames[hash] = source.filename;
            }
        }
        catch (const std::exception& error)
        {
            fmt::println(stderr, "Cannot load recovered shader sources: {}", error.what());
            return 1;
        }

        bool missingPreferredSource = false;
        for (const auto& [hash, filename] : preferredSources)
        {
            if (shaders.find(hash) == shaders.end())
            {
                fmt::println(stderr, "Missing baseline shader {:016X} from preferred source {}", hash, filename);
                missingPreferredSource = true;
            }
        }
        if (missingPreferredSource)
            return 1;

        std::mutex shaderQueueMutex;
        std::deque<XXH64_hash_t> shaderQueue;
        for (const auto& [hash, _] : shaders)
        {
            shaderQueue.emplace_back(hash);
        }

        const uint32_t numThreads = std::max(std::thread::hardware_concurrency(), 1u);
        fmt::println("Recompiling shaders with {} threads", numThreads);

        std::atomic<uint32_t> progress = 0;
        std::vector<std::thread> threads;
        threads.reserve(numThreads);
        for (uint32_t i = 0; i < numThreads; i++)
        {
            threads.emplace_back([&]
            {
                while (true)
                {
                    XXH64_hash_t shaderHash;
                    {
                        std::lock_guard lock(shaderQueueMutex);
                        if (shaderQueue.empty()) {
                            return;
                        }
                        shaderHash = shaderQueue.front();
                        shaderQueue.pop_front();
                    }
                    recompileShader(shaders[shaderHash], include, progress, shaders.size());
                }
            });
        }
        for (auto& thread : threads)
        {
            thread.join();
        }

        fmt::println("Creating shader cache...");

        StringBuffer f;
        f.println("#include \"shader_cache.h\"");
        f.println("ShaderCacheEntry g_shaderCacheEntries[] = {{");

        std::vector<uint8_t> dxil;
        std::vector<uint8_t> spirv;
        std::vector<uint8_t> air;

        for (auto& [hash, shader] : shaders)
        {
            const std::string& filename = shaderFilenames.at(hash);
            const size_t dxilOffset = dxil.size();
            const size_t spirvOffset = spirv.size();
            const size_t airOffset = air.size();

            if (shader.dxil != nullptr)
            {
                dxil.insert(dxil.end(), reinterpret_cast<uint8_t *>(shader.dxil->GetBufferPointer()),
                    reinterpret_cast<uint8_t *>(shader.dxil->GetBufferPointer()) + shader.dxil->GetBufferSize());
            }

#ifdef XENOS_RECOMP_AIR
            air.insert(air.end(), shader.air.begin(), shader.air.end());
#endif

            spirv.insert(spirv.end(), shader.spirv.begin(), shader.spirv.end());

            const size_t lateSpirvOffset = shader.lateSpirv.empty() ? 0 : spirv.size();
            spirv.insert(spirv.end(), shader.lateSpirv.begin(), shader.lateSpirv.end());

#ifdef GTA4_RECOMP
            f.println("\t{{ 0x{:X}, {}, {}, {}, {}, {}, {}, {}, {}, {}, \"{}\", nullptr, {} }},",
                hash, dxilOffset, (shader.dxil != nullptr) ? shader.dxil->GetBufferSize() : 0,
                spirvOffset, shader.spirv.size(), lateSpirvOffset, shader.lateSpirv.size(),
                airOffset, shader.air.size(), shader.specConstantsMask, filename,
                shader.usedTextureMask);
#else
            f.println("\t{{ 0x{:X}, {}, {}, {}, {}, {}, {}, {}, {}, {}, \"{}\" }},",
                hash, dxilOffset, (shader.dxil != nullptr) ? shader.dxil->GetBufferSize() : 0,
                spirvOffset, shader.spirv.size(), lateSpirvOffset, shader.lateSpirv.size(),
                airOffset, shader.air.size(), shader.specConstantsMask, filename);
#endif
        }

        f.println("}};");

        fmt::println("Compressing DXIL cache...");

        int level = ZSTD_maxCLevel();

#ifdef XENOS_RECOMP_DXIL
        std::vector<uint8_t> dxilCompressed(ZSTD_compressBound(dxil.size()));
        dxilCompressed.resize(ZSTD_compress(dxilCompressed.data(), dxilCompressed.size(), dxil.data(), dxil.size(), level));

        f.print("const uint8_t g_compressedDxilCache[] = {{");

        for (auto data : dxilCompressed)
            f.print("{},", data);

        f.println("}};");
        f.println("const size_t g_dxilCacheCompressedSize = {};", dxilCompressed.size());
        f.println("const size_t g_dxilCacheDecompressedSize = {};", dxil.size());
#endif

#ifdef XENOS_RECOMP_AIR
        fmt::println("Compressing AIR cache...");

        std::vector<uint8_t> airCompressed(ZSTD_compressBound(air.size()));
        airCompressed.resize(ZSTD_compress(airCompressed.data(), airCompressed.size(), air.data(), air.size(), level));

        f.print("const uint8_t g_compressedAirCache[] = {{");

        for (auto data : airCompressed)
            f.print("{},", data);

        f.println("}};");
        f.println("const size_t g_airCacheCompressedSize = {};", airCompressed.size());
        f.println("const size_t g_airCacheDecompressedSize = {};", air.size());
#endif

        fmt::println("Compressing SPIRV cache...");

        std::vector<uint8_t> spirvCompressed(ZSTD_compressBound(spirv.size()));
        spirvCompressed.resize(ZSTD_compress(spirvCompressed.data(), spirvCompressed.size(), spirv.data(), spirv.size(), level));

        f.print("const uint8_t g_compressedSpirvCache[] = {{");

        for (auto data : spirvCompressed)
            f.print("{},", data);

        f.println("}};");

        f.println("const size_t g_spirvCacheCompressedSize = {};", spirvCompressed.size());
        f.println("const size_t g_spirvCacheDecompressedSize = {};", spirv.size());
        f.println("const size_t g_shaderCacheEntryCount = {};", shaders.size());

        writeAllBytes(output, f.out.data(), f.out.size());
    }
    else
    {
        ShaderRecompiler recompiler;
        size_t fileSize;
        recompiler.recompile(readAllBytes(input, fileSize).get(), include);
        writeAllBytes(output, recompiler.out.data(), recompiler.out.size());
    }

    return 0;
}
