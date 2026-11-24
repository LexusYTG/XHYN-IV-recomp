#pragma once

#include "shader.h"
#include "shader_code.h"

struct StringBuffer
{
    std::string out;

    template<class... Args>
    void print(fmt::format_string<Args...> fmt, Args&&... args)
    {
        fmt::vformat_to(std::back_inserter(out), fmt.get(), fmt::make_format_args(args...));
    }

    template<class... Args>
    void println(fmt::format_string<Args...> fmt, Args&&... args)
    {
        fmt::vformat_to(std::back_inserter(out), fmt.get(), fmt::make_format_args(args...));
        out += '\n';
    }
};

struct ShaderRecompiler : StringBuffer
{
    uint32_t indentation = 0;
    bool isPixelShader = false;
    const uint8_t* constantTableData = nullptr;
    std::unordered_map<uint32_t, VertexElement> vertexElements;
    std::unordered_map<uint32_t, std::string> interpolators;
    std::unordered_map<uint32_t, const ConstantInfo*> float4Constants;
    std::unordered_map<uint32_t, const char*> boolConstants;
    std::unordered_map<uint32_t, const char*> samplers;
    std::unordered_map<uint32_t, uint32_t> ifEndLabels;
    uint32_t specConstantsMask = 0;
    uint32_t usedTextureMask = 0;

#ifdef UNLEASHED_RECOMP
    bool hasMtxProjection = false;
    bool hasMtxPrevInvViewProjection = false;
#endif

    void indent()
    {
        for (uint32_t i = 0; i < indentation; i++)
            out += '\t';
    }

    /*
     * Predicated instructions (ported from nfsmw-nx's XenosRecomp, StevensND).
     *
     * Each predicated instruction used to open its own `if (p0) { ... }`, so the
     * consecutive taps of one effect (a shadow filter's samples, for example)
     * sat in separate blocks and the GPU waited for each texture separately.
     * With mergePredicates, consecutive instructions under the same condition
     * share one block: a `}` just written is taken back when the next
     * instruction asks for the same condition and nothing was emitted in
     * between (the check is on the text, so control flow, labels or any other
     * statement in between prevent the merge). A block whose instructions wrote
     * p0 is closed for good: the next one must test the new value.
     */
    bool mergePredicates = false;
    int openPredicate_ = -1;                     // condition of the open block, -1 none
    size_t closeMark_ = std::string::npos;       // where the last `}` starts
    size_t closeEnd_ = std::string::npos;        // where it ends (valid while == out.size())
    int closeCondition_ = -1;

    void openPredicate(bool condition)
    {
        const int wanted = condition ? 1 : 0;
        if (mergePredicates)
        {
            if (openPredicate_ == wanted)
                return;
            if (openPredicate_ < 0 && closeCondition_ == wanted && closeEnd_ != std::string::npos &&
                closeEnd_ == out.size())
            {
                out.resize(closeMark_);
                ++indentation;
                closeMark_ = closeEnd_ = std::string::npos;
                openPredicate_ = wanted;
                return;
            }
            closePredicate();
        }
        indent();
        out += condition ? "if (p0)\n" : "if (!p0)\n";
        indent();
        out += "{\n";
        ++indentation;
        openPredicate_ = wanted;
    }

    void closePredicate()
    {
        if (openPredicate_ < 0)
            return;
        --indentation;
        const size_t mark = out.size();
        indent();
        out += "}\n";
        closeMark_ = mark;
        closeEnd_ = out.size();
        closeCondition_ = openPredicate_;
        openPredicate_ = -1;
    }

    // Without merging every predicated instruction closes its own block.
    void endInstructionPredicate()
    {
        if (!mergePredicates)
            closePredicate();
    }

    // Closes for good if the text emitted since `mark` writes p0.
    void closeIfWritesPredicate(size_t mark)
    {
        if (out.find("p0 = ", mark) == std::string::npos)
            return;
        closePredicate();
        closeMark_ = closeEnd_ = std::string::npos;
    }

    uint32_t printDstSwizzle(uint32_t dstSwizzle, bool operand);
    void printDstSwizzle01(uint32_t dstRegister, uint32_t dstSwizzle);

    void recompile(const VertexFetchInstruction& instr, uint32_t address);
    void recompile(const TextureFetchInstruction& instr, bool bicubic);
    void recompile(const AluInstruction& instr);

    void recompile(const uint8_t* shaderData, const std::string_view& include, bool useEarlyFragmentTests = true);
};
