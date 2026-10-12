#include "document_extractor.h"
#include "legacy_doc_extractor.h"

#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSet>
#include <QStringConverter>
#include <QTextDocument>
#include <QTextBoundaryFinder>
#include <QtCore5Compat/QTextCodec>
#include <QXmlStreamReader>
#include <QtEndian>
#include <algorithm>
#include <limits>
#include <stdexcept>
#define MINIZ_NO_ZLIB_COMPATIBLE_NAMES
#include <miniz.h>

namespace qcopilots_search {
namespace {
const QSet<QString> &sourceExtensions() {
    static const auto extensions = [] {
        const auto words = QStringLiteral("py pyi pyx c h cc cpp cxx hpp hh hxx m mm swift ts tsx mts cts js jsx mjs cjs go rs java kt kts scala groovy rb php cs fs sh bash zsh fish ps1 lua pl pm r jl dart sql vue svelte astro html htm css scss sass less gradle cmake mk proto graphql gql tf hcl nix zig hs ex exs erl clj cljs elm ml mli vim el lisp asm s metal glsl hlsl wgsl cu cuh ino v sv tex qml").split(' ');
        return QSet<QString>(words.begin(), words.end());
    }();
    return extensions;
}
const QSet<QString> &codeTextExtensions() {
    static const auto extensions = [] {
        const auto words = QStringLiteral("md markdown rst adoc org txt yaml yml toml").split(' ');
        return QSet<QString>(words.begin(), words.end());
    }();
    return extensions;
}
class DocumentError : public std::runtime_error {
public:
    explicit DocumentError(const QString &s) : std::runtime_error(s.toStdString()) {}
};
void require(bool condition, const QString &error) { if (!condition) throw DocumentError(error); }
quint16 u16(const QByteArray &b, qsizetype p) {
    require(p >= 0 && p <= b.size() - 2, QStringLiteral("Truncated document structure"));
    return qFromLittleEndian<quint16>(reinterpret_cast<const uchar *>(b.constData() + p));
}
quint32 u32(const QByteArray &b, qsizetype p) {
    require(p >= 0 && p <= b.size() - 4, QStringLiteral("Truncated document structure"));
    return qFromLittleEndian<quint32>(reinterpret_cast<const uchar *>(b.constData() + p));
}

QString cp1252(const QByteArray &bytes) {
    static constexpr ushort extended[] = {0x20ac,0x0081,0x201a,0x0192,0x201e,0x2026,0x2020,0x2021,
        0x02c6,0x2030,0x0160,0x2039,0x0152,0x008d,0x017d,0x008f,0x0090,0x2018,0x2019,0x201c,
        0x201d,0x2022,0x2013,0x2014,0x02dc,0x2122,0x0161,0x203a,0x0153,0x009d,0x017e,0x0178};
    QString text;
    text.reserve(bytes.size());
    for (const uchar c : bytes) text += QChar(c >= 0x80 && c <= 0x9f ? extended[c - 0x80] : c);
    return text;
}
QString decode(const QByteArray &bytes) {
    auto encoding = QStringConverter::encodingForData(bytes);
    QStringDecoder decoder(encoding.value_or(QStringConverter::Utf8));
    QString text = decoder(bytes);
    if (decoder.hasError() && !encoding) text = cp1252(bytes);
    else require(!decoder.hasError(), QStringLiteral("Invalid text encoding"));
    require(!text.contains(QChar::Null), QStringLiteral("Binary content is not text"));
    return text;
}
QString normalized(QString text) {
    text.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    text.replace(QChar('\r'), QChar('\n'));
    text.replace(QChar(0x2029), QChar('\n'));
    text.replace(QChar(0x2028), QChar('\n'));
    return text;
}

// Only selected XML entries are read. No archive member is ever written to disk.
class ZipDocument {
    struct Entry { quint32 offset, packed, size, crc; quint16 method, flags; };
    QByteArray bytes;
    QHash<QString, Entry> entries;
    qint64 limit;
    qint64 expanded = 0;
public:
    ZipDocument(QByteArray data, qint64 maxBytes) : bytes(std::move(data)), limit(maxBytes) {
        qsizetype eocd = -1;
        for (qsizetype p = bytes.size() - 22; p >= std::max<qsizetype>(0, bytes.size() - 65557); --p) {
            if (u32(bytes, p) == 0x06054b50 && p + 22 + u16(bytes, p + 20) == bytes.size()) { eocd = p; break; }
        }
        require(eocd >= 0, QStringLiteral("Missing ZIP central directory"));
        require(u16(bytes, eocd + 4) == 0 && u16(bytes, eocd + 6) == 0,
                QStringLiteral("Multi-volume ZIP documents are unsupported"));
        const quint16 count = u16(bytes, eocd + 10);
        require(count != 0xffff && count == u16(bytes, eocd + 8), QStringLiteral("ZIP64 documents are unsupported"));
        qsizetype p = u32(bytes, eocd + 16);
        const quint32 directorySize = u32(bytes, eocd + 12);
        require(p <= eocd && directorySize <= quint64(eocd - p), QStringLiteral("Invalid ZIP directory extent"));
        const qsizetype directoryEnd = p + directorySize;
        for (quint16 i = 0; i < count; ++i) {
            require(p + 46 <= directoryEnd && u32(bytes, p) == 0x02014b50, QStringLiteral("Invalid ZIP directory entry"));
            const auto nameSize = u16(bytes, p + 28), extraSize = u16(bytes, p + 30), commentSize = u16(bytes, p + 32);
            require(p + 46 + nameSize + extraSize + commentSize <= directoryEnd, QStringLiteral("Truncated ZIP entry"));
            const QString name = QString::fromUtf8(bytes.mid(p + 46, nameSize));
            require(!entries.contains(name), QStringLiteral("Duplicate ZIP entry"));
            entries.insert(name, {u32(bytes, p + 42), u32(bytes, p + 20), u32(bytes, p + 24),
                                 u32(bytes, p + 16), u16(bytes, p + 10), u16(bytes, p + 8)});
            p += 46 + nameSize + extraSize + commentSize;
        }
    }
    bool has(const QString &name) const { return entries.contains(name); }
    QByteArray read(const QString &name) {
        require(entries.contains(name), QStringLiteral("Missing document entry: ") + name);
        const auto e = entries.value(name);
        require(!(e.flags & 1), QStringLiteral("Encrypted ZIP document is unsupported"));
        require(qint64(e.size) <= limit - expanded && e.size <= quint32(std::numeric_limits<int>::max()),
                QStringLiteral("Expanded document exceeds extraction size limit"));
        require(e.method == 0 || e.method == 8, QStringLiteral("Unsupported ZIP compression"));
        const qsizetype p = e.offset;
        require(p + 30 <= bytes.size() && u32(bytes, p) == 0x04034b50, QStringLiteral("Invalid ZIP local header"));
        require(u16(bytes, p + 8) == e.method && u16(bytes, p + 6) == e.flags, QStringLiteral("Inconsistent ZIP headers"));
        const qsizetype start = p + 30 + u16(bytes, p + 26) + u16(bytes, p + 28);
        require(start <= bytes.size() && quint64(e.packed) <= quint64(bytes.size() - start), QStringLiteral("Truncated ZIP payload"));
        QByteArray output(e.size, Qt::Uninitialized);
        if (e.method == 0) {
            require(e.packed == e.size, QStringLiteral("Invalid uncompressed ZIP entry size"));
            output = bytes.mid(start, e.size);
        } else {
            mz_stream stream{};
            stream.next_in = reinterpret_cast<unsigned char *>(bytes.data() + start);
            stream.avail_in = e.packed;
            // A spare byte permits zlib to reach MZ_STREAM_END for empty entries.
            output.resize(qsizetype(e.size) + 1);
            stream.next_out = reinterpret_cast<unsigned char *>(output.data());
            stream.avail_out = e.size + 1;
            require(mz_inflateInit2(&stream, -MZ_DEFAULT_WINDOW_BITS) == MZ_OK, QStringLiteral("Cannot initialize ZIP decompression"));
            const int result = mz_inflate(&stream, MZ_FINISH);
            mz_inflateEnd(&stream);
            require(result == MZ_STREAM_END && stream.total_out == e.size && stream.total_in == e.packed,
                    QStringLiteral("Invalid compressed ZIP payload"));
            output.resize(e.size);
        }
        require(mz_crc32(0, reinterpret_cast<const unsigned char *>(output.constData()), mz_uint(output.size())) == e.crc,
                QStringLiteral("ZIP checksum mismatch"));
        expanded += output.size();
        return output;
    }
};

QString xmlText(const QByteArray &bytes, bool odt, qint64 maxCharacters) {
    QXmlStreamReader xml(bytes);
    QString result;
    bool textRun = false;
    int invisible = 0;
    bool odtBody = false;
    constexpr auto wordNs = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    constexpr auto wordStrictNs = "http://purl.oclc.org/ooxml/wordprocessingml/main";
    constexpr auto textNs = "urn:oasis:names:tc:opendocument:xmlns:text:1.0";
    constexpr auto officeNs = "urn:oasis:names:tc:opendocument:xmlns:office:1.0";
    while (!xml.atEnd()) {
        xml.readNext();
        require(xml.tokenType() != QXmlStreamReader::DTD && xml.tokenType() != QXmlStreamReader::EntityReference,
                QStringLiteral("XML document type and external entities are unsupported"));
        const auto name = xml.name();
        const auto ns = xml.namespaceUri();
        const bool word = ns == QLatin1StringView(wordNs) || ns == QLatin1StringView(wordStrictNs);
        if (xml.isStartElement()) {
            if (odt && ns == QLatin1StringView(officeNs) && name == u"text") odtBody = true;
            if ((!odt && word && (name == u"del" || name == u"moveFrom")) || (odt && name == u"tracked-changes")) ++invisible;
            if (!odt && word && name == u"t") textRun = true;
            if (!invisible && ((!odt && word && name == u"tab") || (odt && ns == QLatin1StringView(textNs) && name == u"tab"))) result += '\t';
            if (!invisible && ((!odt && word && (name == u"br" || name == u"cr")) || (odt && name == u"line-break"))) result += '\n';
            if (!invisible && odt && ns == QLatin1StringView(textNs) && name == u"s") {
                bool ok = false;
                int count = xml.attributes().value(QLatin1StringView(textNs), u"c").toInt(&ok);
                if (!ok) count = 1;
                require(count >= 0 && count <= 100000 && result.size() + count <= maxCharacters, QStringLiteral("ODT space expansion exceeds limit"));
                result += QString(count, QChar(' '));
            }
        } else if (xml.isEndElement()) {
            if (!odt && word && name == u"t") textRun = false;
            if (!invisible && ((!odt && word && (name == u"p" || name == u"tr")) || (odt && (name == u"p" || name == u"h" || name == u"table-row")))) result += '\n';
            if (!invisible && ((!odt && word && name == u"tc") || (odt && name == u"table-cell"))) result += '\t';
            if ((!odt && word && (name == u"del" || name == u"moveFrom")) || (odt && name == u"tracked-changes")) --invisible;
            if (odt && ns == QLatin1StringView(officeNs) && name == u"text") odtBody = false;
        } else if (xml.isCharacters() && !invisible && ((odt && odtBody) || textRun)) result += xml.text();
        require(result.size() <= maxCharacters, QStringLiteral("Extracted document exceeds size limit"));
    }
    require(!xml.hasError(), QStringLiteral("Malformed document XML: ") + xml.errorString());
    return result;
}

QString rtfText(const QByteArray &bytes) {
    require(bytes.startsWith("{\\rtf"), QStringLiteral("Invalid RTF header"));
    struct State { bool skip = false; int uc = 1; int codepage = 1252; } state;
    QList<State> stack;
    QString out;
    QByteArray run;
    int fallback = 0;
    auto flush = [&] {
        if (!state.skip && !run.isEmpty()) {
            if (state.codepage == 1252 || state.codepage == 0) out += cp1252(run);
            else if (state.codepage == 65001) out += QString::fromUtf8(run);
            else {
                const QByteArray name = "CP" + QByteArray::number(state.codepage);
                auto *codec = QTextCodec::codecForName(name);
                require(codec != nullptr, QStringLiteral("Unsupported RTF codepage: ") + QString::number(state.codepage));
                QTextCodec::ConverterState conversion;
                QString decoded = codec->toUnicode(run.constData(), int(run.size()), &conversion);
                require(conversion.invalidChars == 0, QStringLiteral("Invalid RTF codepage sequence"));
                out += decoded;
            }
        }
        run.clear();
    };
    auto character = [&](char c) { if (fallback > 0) --fallback; else if (!state.skip) run += c; };
    const QSet<QByteArray> ignored{"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "objdata",
        "filetbl", "listtable", "listoverridetable", "revtbl", "generator", "fldinst", "datafield", "themedata", "colorschememapping"};
    for (qsizetype i = 0; i < bytes.size();) {
        const char c = bytes[i++];
        if (c == '{') { flush(); require(stack.size() < 512, QStringLiteral("RTF nesting exceeds limit")); stack.append(state); }
        else if (c == '}') { flush(); require(!stack.isEmpty(), QStringLiteral("Unbalanced RTF group")); state = stack.takeLast(); }
        else if (c == '\\') {
            require(i < bytes.size(), QStringLiteral("Truncated RTF escape"));
            const char symbol = bytes[i];
            if (symbol == '\\' || symbol == '{' || symbol == '}') { character(symbol); ++i; continue; }
            if (symbol == '\'') {
                require(i + 2 < bytes.size(), QStringLiteral("Truncated RTF byte escape"));
                bool ok = false;
                const uint value = bytes.mid(i + 1, 2).toUInt(&ok, 16);
                require(ok, QStringLiteral("Invalid RTF byte escape"));
                character(char(value)); i += 3; continue;
            }
            flush();
            if (symbol == '*') { state.skip = true; ++i; continue; }
            if (!((symbol >= 'a' && symbol <= 'z') || (symbol >= 'A' && symbol <= 'Z'))) {
                ++i;
                if (fallback > 0) { --fallback; continue; }
                if (!state.skip && symbol == '~') out += QChar(0xa0);
                if (!state.skip && symbol == '_') out += QChar(0x2011);
                continue;
            }
            const qsizetype start = i;
            while (i < bytes.size() && ((bytes[i] >= 'a' && bytes[i] <= 'z') || (bytes[i] >= 'A' && bytes[i] <= 'Z'))) ++i;
            const QByteArray word = bytes.mid(start, i - start);
            const qsizetype numberStart = i;
            if (i < bytes.size() && bytes[i] == '-') ++i;
            while (i < bytes.size() && bytes[i] >= '0' && bytes[i] <= '9') ++i;
            bool hasNumber = false;
            const int value = bytes.mid(numberStart, i - numberStart).toInt(&hasNumber);
            if (i < bytes.size() && bytes[i] == ' ') ++i;
            if (ignored.contains(word)) state.skip = true;
            if (word == "uc" && hasNumber) { require(value >= 0 && value <= 32, QStringLiteral("Invalid RTF Unicode fallback size")); state.uc = value; }
            else if (word == "ansicpg" && hasNumber) state.codepage = value;
            else if (word == "bin") {
                require(hasNumber && value >= 0 && value <= bytes.size() - i, QStringLiteral("Invalid RTF binary extent")); i += value;
            } else if (!state.skip && word == "u" && hasNumber) { out += QChar(ushort(value)); fallback = state.uc; }
            else if (!state.skip && (word == "par" || word == "line" || word == "row")) out += '\n';
            else if (!state.skip && (word == "tab" || word == "cell")) out += '\t';
            else if (!state.skip && word == "emdash") out += QChar(0x2014);
            else if (!state.skip && word == "endash") out += QChar(0x2013);
            else if (!state.skip && word == "bullet") out += QChar(0x2022);
        } else if (c != '\r' && c != '\n') character(c);
    }
    flush();
    require(stack.isEmpty(), QStringLiteral("Unterminated RTF group"));
    return out;
}

class OfflineHtmlDocument final : public QTextDocument {
    QVariant loadResource(int, const QUrl &) override { return {}; }
};

constexpr qsizetype documentSegmentLimit = 5000;
struct DocumentSegmentLimitReached {};

void appendBoundedSegment(QList<Segment> &segments, Segment segment) {
    segments.append(std::move(segment));
    // One sentinel distinguishes an exactly-full document from an incomplete
    // document. Never allocate a list proportional to content beyond the cap.
    if (segments.size() > documentSegmentLimit) throw DocumentSegmentLimitReached{};
}

qsizetype graphemeBefore(const QString &text, qsizetype offset) {
    if (offset <= 0 || offset >= text.size()) return offset;
    QTextBoundaryFinder boundary(QTextBoundaryFinder::Grapheme, text);
    boundary.setPosition(offset);
    return boundary.isAtBoundary() ? offset : boundary.toPreviousBoundary();
}

void checkCode(const QString &text, bool source) {
    require(text.toUtf8().size() <= 1024 * 1024, QStringLiteral("Code text exceeds 1 MiB extraction limit"));
    require(!text.contains(QStringLiteral("PRIVATE KEY-----")), QStringLiteral("Private key content excluded from code indexing"));
    if (!source) return;
    const auto header = text.left(1000);
    for (const QString marker : {QStringLiteral("DO NOT EDIT"), QStringLiteral("@generated"), QStringLiteral("Code generated by"),
                                QStringLiteral("Autogenerated by"), QStringLiteral("Generated by the protocol")})
        require(!header.contains(marker), QStringLiteral("Generated source excluded from code indexing"));
    const auto lines = text.split(QChar('\n'));
    if (text.size() / std::max<qsizetype>(1, lines.size()) > 300)
        for (const auto &line : lines) require(line.size() <= 3000, QStringLiteral("Minified source excluded from code indexing"));
}

void appendCodeChunks(QList<Segment> &segments, const QString &path, const QString &text, const ExtractionOptions &options, int cell) {
    auto lines = text.split(QChar('\n'));
    if (!lines.isEmpty() && lines.last().isEmpty()) lines.removeLast();
    QList<qsizetype> offsets{0};
    for (const auto &line : lines) offsets.append(offsets.last() + line.size() + 1);
    const qsizetype count = lines.size();
    const int size = std::max(32, options.chunkCharacters);
    const QSet<QString> closers{QStringLiteral("end"), QStringLiteral("fi"), QStringLiteral("done"), QStringLiteral("esac"),
        QStringLiteral("@end"), QStringLiteral("#endif"), QStringLiteral("end;"), QStringLiteral("end)")};
    auto blank = [&](qsizetype i) { return lines.at(i).trimmed().isEmpty(); };
    auto block = [&](qsizetype i) {
        const auto &line = lines.at(i);
        return !line.isEmpty() && !line.front().isSpace() && !QStringLiteral("})]").contains(line.front()) && !closers.contains(line.trimmed());
    };
    qsizetype start = 0;
    while (start < count) {
        if (options.cancelled && options.cancelled()) throw DocumentError(QStringLiteral("Extraction cancelled"));
        while (start < count && blank(start)) ++start;
        if (start == count) break;
        qsizetype end = start + 1;
        while (end < count && offsets[end + 1] - offsets[start] <= size) ++end;
        bool natural = end >= count;
        if (!natural) {
            const auto floor = offsets[start] + size * 2 / 5;
            for (int pass = 0; pass < 2 && !natural; ++pass) {
                for (auto line = end; line > start + 1 && offsets[line] >= floor; --line) {
                    if (pass == 0 ? (block(line) && blank(line - 1)) : (blank(line - 1) || block(line))) {
                        end = line; natural = true; break;
                    }
                }
            }
        }
        auto last = end;
        while (last > start + 1 && blank(last - 1)) --last;
        QString body = lines.sliced(start, last - start).join(QChar('\n'));
        if (body.size() > 4000) body.truncate(graphemeBefore(body, 4000));
        Segment segment;
        segment.path = path; segment.title = QFileInfo(path).fileName(); segment.kind = QStringLiteral("code");
        segment.modality = QStringLiteral("text"); segment.text = std::move(body); segment.cell = cell;
        segment.lineStart = int(start + 1); segment.lineEnd = int(last);
        appendBoundedSegment(segments, std::move(segment));
        start = natural || options.overlapCharacters == 0 ? end : std::max(end - 2, start + 1);
    }
}

void appendChunks(QList<Segment> &segments, const QString &path, QString text, const ExtractionOptions &options, int cell = 0) {
    text = normalized(std::move(text));
    if (options.code) { appendCodeChunks(segments, path, text, options, cell); return; }
    // The target is measured in UTF-16 code units. The actual split moves to a
    // grapheme boundary, so an indivisible grapheme can exceed the target.
    const int target = std::max(32, options.chunkCharacters);
    const int overlap = std::clamp(options.overlapCharacters, 0, target / 2);
    const QString title = QFileInfo(path).fileName();
    QTextBoundaryFinder graphemes(QTextBoundaryFinder::Grapheme, text);
    auto safeBefore = [&](qsizetype position) {
        if (position <= 0 || position >= text.size()) return position;
        graphemes.setPosition(position);
        return graphemes.isAtBoundary() ? position : graphemes.toPreviousBoundary();
    };
    qsizetype offset = 0;
    while (offset < text.size()) {
        if (options.cancelled && options.cancelled()) throw DocumentError(QStringLiteral("Extraction cancelled"));
        while (offset < text.size() && text[offset].isSpace()) ++offset;
        if (offset == text.size()) break;
        qsizetype end = std::min(text.size(), offset + target);
        if (end < text.size()) {
            // Keep combining marks and emoji ZWJ sequences with their grapheme.
            end = safeBefore(end);
            if (end <= offset) {
                graphemes.setPosition(offset);
                end = graphemes.toNextBoundary();
            }
            const QStringView window = QStringView(text).sliced(offset, end - offset);
            const auto newline = offset + window.lastIndexOf(QChar('\n'));
            if (newline > offset + target / 3) end = newline;
            else {
                const auto space = offset + window.lastIndexOf(QChar(' '));
                if (space > offset + target / 2) end = space;
            }
        }
        QString chunk = text.mid(offset, end - offset).trimmed();
        if (!chunk.isEmpty()) {
            Segment s;
            s.path = path; s.title = title; s.kind = QStringLiteral("text");
            s.modality = QStringLiteral("text"); s.text = std::move(chunk); s.cell = cell;
            appendBoundedSegment(segments, std::move(s));
        }
        qsizetype next = end;
        if (end < text.size() && overlap > 0) {
            next = std::max(offset + 1, end - overlap);
            next = safeBefore(next);
            if (next <= offset) next = end;
        }
        require(next > offset, QStringLiteral("Invalid document chunk progress"));
        offset = next;
    }
}
} // namespace

bool supportsDocument(QString suffix) {
    static const QSet<QString> extensions{QStringLiteral("txt"), QStringLiteral("text"), QStringLiteral("md"), QStringLiteral("markdown"),
        QStringLiteral("html"), QStringLiteral("htm"), QStringLiteral("rtf"), QStringLiteral("docx"), QStringLiteral("odt"), QStringLiteral("doc")};
    return extensions.contains(suffix.toLower());
}
bool supportsCode(QString suffix, QString filename) {
    suffix = suffix.toLower(); filename = filename.toLower();
    const QStringList excludedPrefixes{QStringLiteral("license"), QStringLiteral("licence"), QStringLiteral("copying"), QStringLiteral("notice"),
        QStringLiteral("id_rsa"), QStringLiteral("id_dsa"), QStringLiteral("id_ecdsa"), QStringLiteral("id_ed25519"), QStringLiteral(".env"),
        QStringLiteral("secret"), QStringLiteral("credential"), QStringLiteral(".netrc"), QStringLiteral(".npmrc"), QStringLiteral(".pypirc"),
        QStringLiteral("htpasswd"), QStringLiteral(".htpasswd")};
    for (const auto &prefix : excludedPrefixes) if (filename.startsWith(prefix)) return false;
    if (filename.endsWith(QStringLiteral(".min.js")) || filename.endsWith(QStringLiteral(".min.mjs")) || filename.endsWith(QStringLiteral(".min.css"))) return false;
    const QSet<QString> names{QStringLiteral("makefile"), QStringLiteral("gnumakefile"), QStringLiteral("dockerfile"), QStringLiteral("containerfile"),
        QStringLiteral("justfile"), QStringLiteral("rakefile"), QStringLiteral("gemfile"), QStringLiteral("podfile"), QStringLiteral("brewfile"),
        QStringLiteral("procfile"), QStringLiteral("vagrantfile"), QStringLiteral("jenkinsfile"), QStringLiteral("cmakelists.txt")};
    return sourceExtensions().contains(suffix) || codeTextExtensions().contains(suffix) || suffix == QStringLiteral("ipynb") ||
           names.contains(filename) || filename.startsWith(QStringLiteral("dockerfile"));
}
ExtractionResult extractDocument(QString path, const ExtractionOptions &options) {
    ExtractionResult result;
    try {
        require(options.maxInputBytes > 0 && options.maxInputBytes < std::numeric_limits<int>::max() && options.start >= 0 &&
                options.maxSegments > 0 && options.maxSegments <= documentSegmentLimit,
                QStringLiteral("Invalid document extraction limits"));
        QFile file(path);
        require(file.open(QIODevice::ReadOnly), QStringLiteral("Cannot read document: ") + file.errorString());
        require(file.size() <= options.maxInputBytes, QStringLiteral("Document exceeds input size limit"));
        const QString suffix = QFileInfo(path).suffix().toLower();
        if (options.code) require(file.size() <= (suffix == QStringLiteral("ipynb") ? 20 * 1024 * 1024 : 1024 * 1024),
                                  QStringLiteral("Source file exceeds code extraction size limit"));
        const QByteArray bytes = file.read(options.maxInputBytes + 1);
        require(file.error() == QFileDevice::NoError && bytes.size() <= options.maxInputBytes, QStringLiteral("Cannot read complete document within size limit"));
        if (options.cancelled && options.cancelled()) throw DocumentError(QStringLiteral("Extraction cancelled"));
        QList<Segment> all;
        bool segmentLimitReached = false;
        try {
            if (options.code && suffix == QStringLiteral("ipynb")) {
                QJsonParseError error;
                const QJsonDocument json = QJsonDocument::fromJson(bytes, &error);
                require(error.error == QJsonParseError::NoError && json.isObject(), QStringLiteral("Malformed Jupyter notebook"));
                auto cellValue = json.object().value(QStringLiteral("cells"));
                if (!cellValue.isArray()) {
                    const auto worksheets = json.object().value(QStringLiteral("worksheets"));
                    require(worksheets.isArray(), QStringLiteral("Missing notebook cells"));
                    QJsonArray flattened;
                    for (const auto worksheet : worksheets.toArray()) {
                        require(worksheet.isObject(), QStringLiteral("Malformed notebook worksheet"));
                        const auto worksheetCells = worksheet.toObject().value(QStringLiteral("cells"));
                        require(worksheetCells.isArray(), QStringLiteral("Missing notebook worksheet cells"));
                        for (const auto cell : worksheetCells.toArray()) flattened.append(cell);
                    }
                    // nbformat 3 permits several worksheets. Preserve their
                    // order and use one global cell number and resume cursor.
                    cellValue = flattened;
                }
                require(cellValue.isArray(), QStringLiteral("Missing notebook cells"));
                const auto cells = cellValue.toArray();
                int index = 0;
                qint64 sourceBytes = 0;
                for (const auto value : cells) {
                    ++index;
                    require(value.isObject(), QStringLiteral("Malformed notebook cell"));
                    auto source = value.toObject().value(QStringLiteral("source"));
                    if (source.isUndefined()) source = value.toObject().value(QStringLiteral("input"));
                    QString text;
                    if (source.isString()) text = source.toString();
                    else if (source.isArray()) { for (const auto line : source.toArray()) { require(line.isString(), QStringLiteral("Malformed notebook source")); text += line.toString(); } }
                    else require(source.isUndefined(), QStringLiteral("Malformed notebook source"));
                    sourceBytes += text.toUtf8().size();
                    require(sourceBytes <= 1024 * 1024, QStringLiteral("Notebook source exceeds 1 MiB extraction limit"));
                    checkCode(text, true);
                    appendChunks(all, path, std::move(text), options, index);
                }
            } else {
                QString text;
                if (suffix == QStringLiteral("docx") || suffix == QStringLiteral("odt")) {
                    ZipDocument zip(bytes, options.maxInputBytes);
                    if (suffix == QStringLiteral("odt")) text = xmlText(zip.read(QStringLiteral("content.xml")), true, options.maxInputBytes);
                    else {
                        text = xmlText(zip.read(QStringLiteral("word/document.xml")), false, options.maxInputBytes);
                        for (const QString entry : {QStringLiteral("word/footnotes.xml"), QStringLiteral("word/endnotes.xml")})
                            if (zip.has(entry)) text += QChar('\n') + xmlText(zip.read(entry), false, options.maxInputBytes - text.size());
                    }
                } else if (suffix == QStringLiteral("rtf") || (suffix == QStringLiteral("doc") && bytes.startsWith("{\\rtf"))) text = rtfText(bytes);
                else if (suffix == QStringLiteral("doc")) {
                    QString error;
                    text = legacy_doc::extract(path, &error, options.maxInputBytes, options.cancelled);
                    require(error.isEmpty(), error);
                }
                else if ((suffix == QStringLiteral("html") || suffix == QStringLiteral("htm")) && !options.code) {
                    OfflineHtmlDocument document;
                    document.setHtml(QTextCodec::codecForHtml(bytes, QTextCodec::codecForName("UTF-8"))->toUnicode(bytes));
                    text = document.toPlainText();
                } else text = decode(bytes);
                if (options.code) checkCode(text, sourceExtensions().contains(suffix));
                appendChunks(all, path, std::move(text), options);
            }
        } catch (const DocumentSegmentLimitReached &) {
            segmentLimitReached = true;
        }
        require(!segmentLimitReached || options.start < documentSegmentLimit,
                QStringLiteral("Document exceeds the 5000-segment extraction limit. Additional content remains unindexed."));
        const qsizetype available = std::min(all.size(), documentSegmentLimit);
        const qsizetype start = std::min<qsizetype>(options.start, available);
        const qsizetype end = std::min<qsizetype>(available, start + options.maxSegments);
        result.segments = all.sliced(start, end - start);
        result.next = int(end);
        result.complete = !segmentLimitReached && end == available;
        result.state = result.complete ? QStringLiteral("complete") : QStringLiteral("partial");
    } catch (const DocumentError &error) {
        result.error = QString::fromUtf8(error.what());
        result.state = result.error.contains(QStringLiteral("cancelled"), Qt::CaseInsensitive) ? QStringLiteral("cancelled") : QStringLiteral("failed");
        result.complete = false;
        result.next = options.start;
    }
    return result;
}
} // namespace qcopilots_search
