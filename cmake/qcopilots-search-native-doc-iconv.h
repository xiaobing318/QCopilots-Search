// SPDX-License-Identifier: MIT
// Private compatibility surface used only by Calligra wv2's TextConverter.
// Qt's pinned Core5Compat codecs supply the legacy Windows code pages on every
// supported platform. This is deliberately not a general-purpose iconv API.
#pragma once

#include <QtCore5Compat/QTextCodec>
#include <QtCore/QByteArray>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>

struct qcopilots_search_wv2_converter {
    QTextCodec *codec;
    bool bigEndian;
};
using iconv_t = qcopilots_search_wv2_converter *;

inline iconv_t iconv_open(const char *destination, const char *source)
{
    const bool little = destination && std::strcmp(destination, "UCS-2LE") == 0;
    const bool big = destination && std::strcmp(destination, "UCS-2BE") == 0;
    auto *codec = source ? QTextCodec::codecForName(source) : nullptr;
    if ((!little && !big) || !codec) {
        errno = EINVAL;
        return reinterpret_cast<iconv_t>(-1);
    }
    auto *converter = new (std::nothrow) qcopilots_search_wv2_converter{codec, big};
    if (!converter) {
        errno = ENOMEM;
        return reinterpret_cast<iconv_t>(-1);
    }
    return converter;
}

inline int iconv_close(iconv_t converter)
{
    if (!converter || converter == reinterpret_cast<iconv_t>(-1)) {
        errno = EBADF;
        return -1;
    }
    delete converter;
    return 0;
}

inline std::size_t iconv(iconv_t converter, const char **input,
                         std::size_t *inputBytes, char **output,
                         std::size_t *outputBytes)
{
    constexpr auto failure = static_cast<std::size_t>(-1);
    if (!converter || converter == reinterpret_cast<iconv_t>(-1)) {
        errno = EBADF;
        return failure;
    }
    // wv2 uses stateless code-page conversions. A reset has no pending output.
    if (!input || !*input)
        return 0;
    if (!inputBytes || !output || !*output || !outputBytes ||
        *inputBytes > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
        errno = EINVAL;
        return failure;
    }
    QTextCodec::ConverterState state(QTextCodec::IgnoreHeader);
    const QString text = converter->codec->toUnicode(
        *input, static_cast<int>(*inputBytes), &state);
    if (state.invalidChars || state.remainingChars) {
        errno = state.remainingChars ? EINVAL : EILSEQ;
        return failure;
    }
    const auto bytes = static_cast<std::size_t>(text.size()) * 2;
    if (bytes > *outputBytes) {
        errno = E2BIG;
        return failure;
    }
    for (qsizetype index = 0; index < text.size(); ++index) {
        const auto value = text.at(index).unicode();
        // UCS-2 does not represent Unicode supplementary characters.
        if (value >= 0xd800 && value <= 0xdfff) {
            errno = EILSEQ;
            return failure;
        }
    }
    for (qsizetype index = 0; index < text.size(); ++index) {
        const auto value = text.at(index).unicode();
        (*output)[index * 2] = static_cast<char>(converter->bigEndian ? value >> 8 : value & 0xff);
        (*output)[index * 2 + 1] = static_cast<char>(converter->bigEndian ? value & 0xff : value >> 8);
    }
    *input += *inputBytes;
    *inputBytes = 0;
    *output += bytes;
    *outputBytes -= bytes;
    return 0;
}
