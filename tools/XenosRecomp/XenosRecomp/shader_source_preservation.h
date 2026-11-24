#pragma once

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <istream>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>

using PreferredShaderSources = std::map<uint64_t, std::string>;

inline std::string ShaderCacheSourceName(std::string filename)
{
    const size_t shaderPos = filename.find("shader");
    if (shaderPos != std::string::npos)
    {
        filename = filename.substr(shaderPos);
        std::replace(filename.begin(), filename.end(), '\\', '/');
    }
    return filename;
}

inline PreferredShaderSources ReadPreferredShaderSources(std::istream& input)
{
    constexpr std::string_view prefix = "liberty-shader-sources-v1\t";
    std::string line;
    if (!std::getline(input, line) || line.compare(0, prefix.size(), prefix) != 0)
        throw std::runtime_error("invalid shader-preservation source-map header");
    size_t expectedCount = 0;
    const auto count = std::from_chars(line.data() + prefix.size(), line.data() + line.size(), expectedCount);
    if (count.ec != std::errc{} || count.ptr != line.data() + line.size() || !expectedCount)
        throw std::runtime_error("invalid shader-preservation source-map count");

    PreferredShaderSources sources;
    while (std::getline(input, line))
    {
        const size_t tab = line.find('\t');
        if (tab == std::string::npos || tab == 0 || tab == line.size() - 1 ||
            line.find_first_of("\t\r", tab + 1) != std::string::npos)
            throw std::runtime_error("malformed shader-preservation source-map row");
        uint64_t hash = 0;
        const auto parsed = std::from_chars(line.data(), line.data() + tab, hash, 16);
        if (parsed.ec != std::errc{} || parsed.ptr != line.data() + tab)
            throw std::runtime_error("invalid hash in shader-preservation source map");
        if (!sources.emplace(hash, line.substr(tab + 1)).second)
            throw std::runtime_error("duplicate hash in shader-preservation source map");
    }
    if (input.bad() || sources.size() != expectedCount)
        throw std::runtime_error("shader-preservation source-map count mismatch");
    return sources;
}

inline bool IsPreferredShaderSource(const PreferredShaderSources& sources,
                                    uint64_t hash, const std::string& filename)
{
    const auto preferred = sources.find(hash);
    return preferred == sources.end() || preferred->second == filename;
}

struct RecoveredShaderSource
{
    bool isPixelShader = false;
    uint32_t specConstantsMask = 0;
    uint32_t usedTextureMask = 0;
    std::string filename;
    std::string early;
    std::string late;
};

inline bool HasCurrentRecoveredShaderPrefix(std::string_view source,
                                           std::string_view common, bool pixel)
{
    constexpr std::string_view pixelPreamble =
        "#if defined(__air__) && !defined(XENOS_RECOMP_PIXEL_SHADER)\n"
        "#define XENOS_RECOMP_PIXEL_SHADER\n#endif\n";
    if (pixel)
    {
        if (source.substr(0, pixelPreamble.size()) != pixelPreamble)
            return false;
        source.remove_prefix(pixelPreamble.size());
    }
    return source.size() > common.size() && source.substr(0, common.size()) == common;
}

inline std::map<uint64_t, RecoveredShaderSource> ReadRecoveredShaderSources(
    std::istream& input, const PreferredShaderSources& preferred)
{
    constexpr std::string_view prefix = "liberty-recovered-sources-v1\t";
    std::string line;
    if (!std::getline(input, line) || line.compare(0, prefix.size(), prefix) != 0)
        throw std::runtime_error("invalid recovered-source header");
    size_t expectedCount = 0;
    const auto count = std::from_chars(line.data() + prefix.size(), line.data() + line.size(), expectedCount);
    if (count.ec != std::errc{} || count.ptr != line.data() + line.size() || !expectedCount)
        throw std::runtime_error("invalid recovered-source count");
    std::map<uint64_t, RecoveredShaderSource> sources;
    while (std::getline(input, line))
    {
        std::array<std::string_view, 7> fields;
        std::string_view remaining(line);
        for (size_t i = 0; i < fields.size(); ++i)
        {
            const size_t tab = remaining.find('\t');
            if ((i == fields.size() - 1) != (tab == std::string_view::npos))
                throw std::runtime_error("invalid recovered-source column count");
            fields[i] = remaining.substr(0, tab);
            if (fields[i].empty() || fields[i].find('\r') != std::string_view::npos)
                throw std::runtime_error("empty/invalid recovered-source field");
            if (tab != std::string_view::npos)
                remaining.remove_prefix(tab + 1);
        }
        const auto parse = [](std::string_view field, auto& value, int base)
        {
            const auto result = std::from_chars(field.data(), field.data() + field.size(), value, base);
            if (result.ec != std::errc{} || result.ptr != field.data() + field.size())
                throw std::runtime_error("invalid recovered-source integer");
        };
        uint64_t hash = 0;
        parse(fields[0], hash, 16);
        RecoveredShaderSource source;
        if (fields[1] != "pixel" && fields[1] != "vertex")
            throw std::runtime_error("invalid recovered-source stage");
        source.isPixelShader = fields[1] == "pixel";
        parse(fields[2], source.specConstantsMask, 10);
        parse(fields[3], source.usedTextureMask, 10);
        source.filename = fields[4];
        const auto baseline = preferred.find(hash);
        if (baseline == preferred.end() || baseline->second != source.filename)
            throw std::runtime_error("recovered source is not an exact baseline hash/filename");
        for (const auto field : {fields[5], fields[6]})
            if (field.find_first_of("/\\:") != std::string_view::npos || field == "." || field == "..")
                throw std::runtime_error("recovered-source paths must be local basenames");
        source.early = fields[5];
        if (source.early == "-")
            throw std::runtime_error("missing recovered early source");
        if (fields[6] != "-")
        {
            if (!source.isPixelShader || fields[5] == fields[6])
                throw std::runtime_error("invalid recovered late variant");
            source.late = fields[6];
        }
        if (!sources.emplace(hash, std::move(source)).second)
            throw std::runtime_error("duplicate recovered shader hash");
    }
    if (input.bad() || sources.size() != expectedCount)
        throw std::runtime_error("recovered-source count mismatch");
    return sources;
}
