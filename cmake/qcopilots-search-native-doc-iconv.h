// SPDX-License-Identifier: MIT
// Private compatibility surface used only by Calligra wv2's TextConverter.
// Prefer Qt Core's strict stateless decoder. Core5Compat supplies the legacy
// tables when the Qt build lacks a named decoder. This is not a general iconv API.
#pragma once

#include <QtCore5Compat/QTextCodec>
#include <QtCore/QByteArray>
#include <QtCore/QStringDecoder>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>

struct qcopilots_search_wv2_converter {
    QByteArray name;
    QTextCodec *codec;
    bool bigEndian;
};
using iconv_t = qcopilots_search_wv2_converter *;

inline iconv_t iconv_open(const char *destination, const char *source)
{
    const bool little = destination && std::strcmp(destination, "UCS-2LE") == 0;
    const bool big = destination && std::strcmp(destination, "UCS-2BE") == 0;
    // CP950 is a Qt built-in Big5 alias, but ICU's CP950 alias is Java-only.
    // Qt Core5Compat requires a MIME/IANA alias and rejects it on ICU builds.
    // Use the Windows-compatible Big5 name explicitly on every platform.
    const QByteArray name = source ? QByteArray(source) : QByteArray();
    QByteArray canonical = name;
    if (name.compare("CP950", Qt::CaseInsensitive) == 0) canonical = "Big5";
    // ICU's bare CP932/CP949 can select IBM variants. Preserve the Windows
    // Shift-JIS/UHC mappings expected by Word and Qt's built-in codec tables.
    else if (name.compare("CP949", Qt::CaseInsensitive) == 0) canonical = "windows-949";
    else if (name.compare("CP932", Qt::CaseInsensitive) == 0) canonical = "windows-932";
    QStringDecoder decoder(canonical, QStringConverter::Flag::Stateless);
    QTextCodec *codec = nullptr;
    if (!decoder.isValid() && !canonical.isEmpty()) {
        codec = QTextCodec::codecForName(canonical);
        if (!codec && canonical != name) codec = QTextCodec::codecForName(name);
    }
    if ((!little && !big) || canonical.isEmpty() || (!decoder.isValid() && !codec)) {
        errno = EINVAL;
        return reinterpret_cast<iconv_t>(-1);
    }
    auto *converter = new (std::nothrow) qcopilots_search_wv2_converter{canonical, codec, big};
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
    QString text;
    if (!converter->codec) {
        // A fresh decoder per complete run also clears Qt's error counter.
        // Qt 6.12's ICU resetState() only resets the ICU conversion state.
        QStringDecoder decoder(converter->name, QStringConverter::Flag::Stateless);
        if (!decoder.isValid()) { errno = EINVAL; return failure; }
        text = decoder(QByteArrayView(*input, static_cast<qsizetype>(*inputBytes)));
        // QIcuCodec in Core5Compat leaves trailing bytes in hidden ICU state
        // and does not count ToUnicode substitutions. QStringDecoder's
        // Stateless path checks pending bytes and counts invalid sequences.
        if (decoder.hasError()) { errno = EILSEQ; return failure; }
    } else {
        QTextCodec::ConverterState state(QTextCodec::IgnoreHeader);
        text = converter->codec->toUnicode(*input, static_cast<int>(*inputBytes), &state);
        if (state.invalidChars || state.remainingChars) {
            errno = state.remainingChars ? EINVAL : EILSEQ;
            return failure;
        }
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
