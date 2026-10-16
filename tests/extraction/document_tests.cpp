#include "extraction/document_extractor.h"
#include "extraction/extractor.h"
#include "core/file_catalog.h"
#include "../support/test_environment.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>
#include <QTextBoundaryFinder>
#include <QUrl>
#include <QtEndian>
#include <QtTest>
#include <memory>
#define MINIZ_NO_ZLIB_COMPATIBLE_NAMES
#include <miniz.h>

using namespace qcopilots_search;

namespace {
void put16(QByteArray &bytes, qsizetype p, quint16 value) { qToLittleEndian(value, reinterpret_cast<uchar *>(bytes.data() + p)); }
void put32(QByteArray &bytes, qsizetype p, quint32 value) { qToLittleEndian(value, reinterpret_cast<uchar *>(bytes.data() + p)); }
QByteArray zip(const QList<QPair<QByteArray, QByteArray>> &entries, bool compressed = false) {
    QByteArray out, central;
    for (const auto &entry : entries) {
        const auto crc = mz_crc32(0, reinterpret_cast<const unsigned char *>(entry.second.constData()), mz_uint(entry.second.size()));
        QByteArray payload = entry.second;
        if (compressed) {
            mz_stream stream{};
            if (mz_deflateInit2(&stream, MZ_DEFAULT_COMPRESSION, MZ_DEFLATED, -MZ_DEFAULT_WINDOW_BITS, 8, MZ_DEFAULT_STRATEGY) != MZ_OK) return {};
            payload.resize(mz_deflateBound(&stream, mz_ulong(entry.second.size())));
            stream.next_in = reinterpret_cast<unsigned char *>(const_cast<char *>(entry.second.constData()));
            stream.avail_in = mz_uint(entry.second.size());
            stream.next_out = reinterpret_cast<unsigned char *>(payload.data());
            stream.avail_out = mz_uint(payload.size());
            const int result = mz_deflate(&stream, MZ_FINISH);
            const auto size = stream.total_out;
            mz_deflateEnd(&stream);
            if (result != MZ_STREAM_END) return {};
            payload.resize(size);
        }
        QByteArray local(30, '\0');
        put32(local, 0, 0x04034b50); put16(local, 4, 20); put16(local, 8, compressed ? 8 : 0);
        put32(local, 14, crc); put32(local, 18, quint32(payload.size())); put32(local, 22, quint32(entry.second.size())); put16(local, 26, quint16(entry.first.size()));
        QByteArray directory(46, '\0');
        put32(directory, 0, 0x02014b50); put16(directory, 4, 20); put16(directory, 6, 20); put16(directory, 10, compressed ? 8 : 0);
        put32(directory, 16, crc); put32(directory, 20, quint32(payload.size())); put32(directory, 24, quint32(entry.second.size()));
        put16(directory, 28, quint16(entry.first.size())); put32(directory, 42, quint32(out.size()));
        out += local + entry.first + payload;
        central += directory + entry.first;
    }
    QByteArray end(22, '\0');
    put32(end, 0, 0x06054b50); put16(end, 8, quint16(entries.size())); put16(end, 10, quint16(entries.size()));
    put32(end, 12, quint32(central.size())); put32(end, 16, quint32(out.size()));
    return out + central + end;
}
QString allText(const ExtractionResult &result) {
    QString text;
    for (const auto &segment : result.segments) text += segment.text + QChar('\n');
    return text;
}
}

class DocumentTests final : public QObject {
    Q_OBJECT
    std::unique_ptr<test::TestTemporaryDir> directory;
    QString write(const QString &name, const QByteArray &bytes) {
        const auto path = directory->filePath(name);
        QFile file(path);
        if (!file.open(QIODevice::WriteOnly) || file.write(bytes) != bytes.size()) return {};
        return path;
    }
private slots:
    void initTestCase() {
        QStandardPaths::setTestModeEnabled(true);
    }
    void init() {
        auto root = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT");
        if (root.isEmpty()) root = QDir::tempPath();
        QVERIFY2(QDir().mkpath(root), qPrintable(root));
        directory = std::make_unique<test::TestTemporaryDir>(QDir(root).filePath(QStringLiteral("qcs-document-XXXXXX")));
        QVERIFY2(directory->isValid(), qPrintable(directory->errorString()));
        if (qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") == 1) directory->setAutoRemove(false);
        qInfo().noquote() << "Document test root:" << directory->path();
    }
    void cleanup() {
        QString cleanupError;
        if (directory && directory->isValid()) {
            if (QTest::currentTestFailed()
                || qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") == 1) {
                directory->setAutoRemove(false);
                qInfo().noquote() << "Preserved document test artifacts:" << directory->path();
            } else {
                if (!directory->remove(&cleanupError)) directory->setAutoRemove(false);
            }
        }
        directory.reset();
        cleanupError += test::takeCleanupErrors();
        QVERIFY2(cleanupError.isEmpty(), qPrintable(cleanupError));
    }
    void textUnicodePathsAndEncodings() {
        const auto path = write(QStringLiteral("中文 sample.txt"), QStringLiteral("中文检索\r\nSecond paragraph").toUtf8());
        QVERIFY(!path.isEmpty());
        auto result = extractDocument(path, {});
        QCOMPARE(result.state, QStringLiteral("complete"));
        QVERIFY(allText(result).contains(QStringLiteral("中文检索\nSecond paragraph")));
        QCOMPARE(result.segments.first().path, path);
        const auto utf16 = QByteArray::fromHex("fffe2d4e87650a004100");
        result = extractDocument(write(QStringLiteral("utf16.txt"), utf16), {});
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("中文\nA")));
        result = extractDocument(write(QStringLiteral("latin.txt"), QByteArray::fromHex("436166e9208094")), {});
        QVERIFY(allText(result).contains(QStringLiteral("Café €”")));
    }
    void longUnicodePaths_data() {
        QTest::addColumn<int>("requestedLength");
        QTest::newRow("258") << 258;
        QTest::newRow("259") << 259;
        QTest::newRow("260") << 260;
        QTest::newRow("300") << 300;
    }
    void longUnicodePaths() {
        QFETCH(int, requestedLength);
        // Account for the caller's temporary root instead of assuming a drive,
        // username or checkout length. Keep individual components portable.
        const QString basename = QStringLiteral("中文 文档.txt");
        const auto targetLength = qMax(qsizetype(258), directory->path().size() + 48)
            + requestedLength - 258;
        QString parent = directory->path();
        while (targetLength - parent.size() - 1 - basename.size() > 60)
            parent = QDir(parent).filePath(QString(32, QLatin1Char('d')));
        QVERIFY2(QDir().mkpath(parent), qPrintable(parent));
        const QString filename = QString(targetLength - parent.size() - 1 - basename.size(), QLatin1Char('p')) + basename;
        const QString path = QDir(parent).filePath(filename);
        QCOMPARE(path.size(), targetLength);
        const QByteArray body = QStringLiteral("中文长路径检索\nportable document extraction").toUtf8();
        {
            QFile file(path);
            QVERIFY2(file.open(QIODevice::WriteOnly), qPrintable(file.errorString()));
            QCOMPARE(file.write(body), body.size());
        }
        const auto result = extractDocument(path, {});
        QCOMPARE(result.state, QStringLiteral("complete"));
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("中文长路径检索")));
        QCOMPARE(result.segments.first().path, path);
    }
    void limitsAndCancellation() {
        auto options = ExtractionOptions{};
        const auto path = write(QStringLiteral("limits.txt"), "hello there");
        options.maxInputBytes = 4;
        QCOMPARE(extractDocument(path, options).state, QStringLiteral("failed"));
        options.maxInputBytes = 100;
        options.cancelled = [] { return true; };
        QCOMPARE(extractDocument(path, options).state, QStringLiteral("cancelled"));
        options.cancelled = {};
        options.maxSegments = 0;
        QCOMPARE(extractDocument(path, options).state, QStringLiteral("failed"));
        QCOMPARE(extractDocument(directory->filePath(QStringLiteral("missing.txt")), {}).state, QStringLiteral("failed"));
        QCOMPARE(extractDocument(write(QStringLiteral("binary.txt"), QByteArray("x\0y", 3)), {}).state, QStringLiteral("failed"));
    }
    void largeMarkdownUsesDocumentLimitsOutsideCodeScope() {
        QByteArray content("# Large document\nunique-document-start\n");
        const QByteArray paragraph = QStringLiteral("This document explains building a desktop application. 中文编译说明。\n").toUtf8();
        while (content.size() <= 1024 * 1024) content += paragraph;
        content += "\nunique-document-tail\n";
        const QString path = write(QStringLiteral("large.md"), content);
        QVERIFY(!path.isEmpty());
        QVERIFY(QFileInfo(path).size() > 1024 * 1024);

        ExtractionOptions options;
        options.maxSegments = 5000;
        const auto document = Extractor::extract(path, options);
        QVERIFY2(document.error.isEmpty(), qPrintable(document.error));
        QCOMPARE(document.state, QStringLiteral("complete"));
        QVERIFY(document.complete);
        QVERIFY(document.segments.size() > 40);
        QVERIFY(allText(document).contains(QStringLiteral("unique-document-start")));
        QVERIFY(allText(document).contains(QStringLiteral("unique-document-tail")));
        for (const auto &segment : document.segments) QCOMPARE(segment.kind, QStringLiteral("text"));

        options.code = true;
        const auto code = Extractor::extract(path, options);
        QCOMPARE(code.state, QStringLiteral("failed"));
        QVERIFY(!code.complete);
        QVERIFY(code.segments.isEmpty());
        QCOMPARE(code.error, QStringLiteral("Source file exceeds code extraction size limit"));
    }
    void longDocumentResumesWithoutLosingTail() {
        QString content;
        for (int i = 0; i < 100; ++i) content += QStringLiteral("paragraph %1 body 中文\n").arg(i);
        content += QStringLiteral("unique-tail-marker");
        const auto path = write(QStringLiteral("long.md"), content.toUtf8());
        ExtractionOptions options;
        options.chunkCharacters = 100; options.overlapCharacters = 20; options.maxSegments = 5000;
        const auto expected = extractDocument(path, options);
        QVERIFY(expected.complete);
        QVERIFY(expected.segments.size() > 10);
        options.maxSegments = 3;
        QList<Segment> segments;
        for (int pass = 0; pass < 100; ++pass) {
            const auto part = extractDocument(path, options);
            QVERIFY2(part.error.isEmpty(), qPrintable(part.error));
            segments.append(part.segments);
            if (part.complete) break;
            QVERIFY(part.next > options.start);
            options.start = part.next;
        }
        QCOMPARE(segments.size(), expected.segments.size());
        for (int i = 0; i < segments.size(); ++i) QCOMPARE(segments[i].text, expected.segments[i].text);
        QVERIFY(segments.last().text.contains(QStringLiteral("unique-tail-marker")));
    }
    void htmlNeverReadsLocalResources() {
        const auto secret = write(QStringLiteral("resource.txt"), "THIS_MUST_NOT_BE_INDEXED");
        const auto html = QStringLiteral("<html><head><script>hiddenScript</script><style>hiddenStyle</style></head><body><h1>Title 中文</h1><p>Visible &amp; readable</p><img src=\"%1\"></body></html>").arg(QUrl::fromLocalFile(secret).toString());
        const auto result = extractDocument(write(QStringLiteral("offline.html"), html.toUtf8()), {});
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("Visible & readable")));
        QVERIFY(!allText(result).contains(QStringLiteral("hiddenScript")));
        QVERIFY(!allText(result).contains(QStringLiteral("THIS_MUST_NOT_BE_INDEXED")));
    }
    void rtfUnicodeCodepageAndHiddenGroups() {
        const auto rtf = QByteArray("{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}Hello \\'e9 \\u20013?\\u25991?\\par {\\*\\unknown hidden}{\\pict 00ff}Next\\tab cell}");
        const auto result = extractDocument(write(QStringLiteral("sample.rtf"), rtf), {});
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("Hello é 中文\nNext\tcell")));
        QVERIFY(!allText(result).contains(QStringLiteral("hidden")));
        QCOMPARE(extractDocument(write(QStringLiteral("bad.rtf"), "{\\rtf1 bad"), {}).state, QStringLiteral("failed"));
        const auto chinese = extractDocument(write(QStringLiteral("gbk.rtf"), "{\\rtf1\\ansi\\ansicpg936 \\'d6\\'d0\\'ce\\'c4}"), {});
        QVERIFY2(chinese.error.isEmpty(), qPrintable(chinese.error));
        QVERIFY(allText(chinese).contains(QStringLiteral("中文")));
    }
    void docxParagraphTableAndFootnote_data() { QTest::addColumn<bool>("compressed"); QTest::newRow("stored") << false; QTest::newRow("deflated") << true; }
    void docxParagraphTableAndFootnote() {
        QFETCH(bool, compressed);
        const QByteArray ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
        const auto body = "<w:document xmlns:w=\"" + ns + "\"><w:body><w:p><w:r><w:t>Body 中文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:del><w:r><w:t>deleted</w:t></w:r></w:del></w:body></w:document>";
        const auto notes = "<w:footnotes xmlns:w=\"" + ns + "\"><w:footnote><w:p><w:r><w:t>Footnote</w:t></w:r></w:p></w:footnote></w:footnotes>";
        const auto result = extractDocument(write(QStringLiteral("sample.docx"), zip({{"word/document.xml", body}, {"word/footnotes.xml", notes}}, compressed)), {});
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("Body 中文")));
        QVERIFY(allText(result).contains(QStringLiteral("Cell")));
        QVERIFY(allText(result).contains(QStringLiteral("Footnote")));
        QVERIFY(!allText(result).contains(QStringLiteral("deleted")));
    }
    void archiveCorruptionAndExpansionLimit() {
        const auto malformed = zip({{"word/document.xml", "<bad>"}});
        QCOMPARE(extractDocument(write(QStringLiteral("malformed.docx"), malformed), {}).state, QStringLiteral("failed"));
        auto corrupt = zip({{"content.xml", "<text>hello</text>"}});
        corrupt[30 + 11] = '!';
        QCOMPARE(extractDocument(write(QStringLiteral("corrupt.odt"), corrupt), {}).state, QStringLiteral("failed"));
        const auto expansion = zip({{"content.xml", QByteArray(200000, 'a')}}, true);
        QVERIFY(expansion.size() < 4000);
        ExtractionOptions options; options.maxInputBytes = 4000;
        QCOMPARE(extractDocument(write(QStringLiteral("expansion.odt"), expansion), options).state, QStringLiteral("failed"));
        const auto dtd = zip({{"content.xml", "<!DOCTYPE x [<!ENTITY bomb 'hello'>]><x>&bomb;</x>"}});
        QCOMPARE(extractDocument(write(QStringLiteral("entity.odt"), dtd), {}).state, QStringLiteral("failed"));
    }
    void odtSpacesAndUnicode() {
        const QByteArray xml = "<office:document-content xmlns:office=\"urn:oasis:names:tc:opendocument:xmlns:office:1.0\" xmlns:text=\"urn:oasis:names:tc:opendocument:xmlns:text:1.0\"><office:body><office:text><text:p>ODT 中文<text:s text:c=\"3\"/>spaces<text:tab/>tab</text:p><text:p>Second</text:p></office:text></office:body></office:document-content>";
        const auto result = extractDocument(write(QStringLiteral("sample.odt"), zip({{"content.xml", xml}})), {});
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("ODT 中文   spaces\ttab\nSecond")));
    }
    void codeLocationsAndNotebookSources() {
        ExtractionOptions options; options.code = true; options.chunkCharacters = 100; options.overlapCharacters = 0;
        auto result = extractDocument(write(QStringLiteral("sample.cpp"), "\r\n\r\nint main() {\r\n  return 0;\r\n}\r\n"), options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QCOMPARE(result.segments.size(), 1);
        QCOMPARE(result.segments.first().lineStart, 3);
        QCOMPARE(result.segments.first().lineEnd, 5);
        const QJsonArray cells{QJsonObject{{"cell_type", "markdown"}, {"source", QJsonArray{"# Heading\n"}}},
            QJsonObject{{"cell_type", "code"}, {"source", QJsonArray{"answer = 42\n"}}, {"outputs", QJsonArray{QJsonObject{{"text", "UNWANTED_OUTPUT"}}}}}};
        result = extractDocument(write(QStringLiteral("notebook.ipynb"), QJsonDocument(QJsonObject{{"cells", cells}, {"nbformat", 4}}).toJson()), options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QCOMPARE(result.segments.size(), 2);
        QCOMPARE(result.segments[0].cell, 1); QCOMPARE(result.segments[1].cell, 2);
        QVERIFY(!allText(result).contains(QStringLiteral("UNWANTED_OUTPUT")));
        QCOMPARE(extractDocument(write(QStringLiteral("bad.ipynb"), "{}"), options).state, QStringLiteral("failed"));
        const QJsonArray oldCells{QJsonObject{{"cell_type", "code"}, {"input", QJsonArray{"old_answer = 42\n"}}}};
        const QJsonObject oldBook{{"nbformat", 3}, {"worksheets", QJsonArray{QJsonObject{{"cells", oldCells}}}}};
        result = extractDocument(write(QStringLiteral("v3.ipynb"), QJsonDocument(oldBook).toJson()), options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(allText(result).contains(QStringLiteral("old_answer = 42")));
        const QJsonObject secretBook{{"cells", QJsonArray{QJsonObject{{"source", "-----BEGIN PRIVATE KEY-----"}}}}};
        QCOMPARE(extractDocument(write(QStringLiteral("secret.ipynb"), QJsonDocument(secretBook).toJson()), options).state, QStringLiteral("failed"));
    }
    void codeLineBoundariesAndOverlap() {
        QString source = QStringLiteral("void function() {\n");
        for (int i = 0; i < 40; ++i) source += QStringLiteral("    call(%1);\n").arg(i);
        source += QStringLiteral("}\n");
        ExtractionOptions options; options.code = true; options.chunkCharacters = 100;
        const auto result = extractDocument(write(QStringLiteral("lines.cpp"), source.toUtf8()), options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(result.segments.size() > 4);
        const auto lines = source.split(QChar('\n'));
        for (int i = 0; i < result.segments.size(); ++i) {
            const auto &segment = result.segments[i];
            QCOMPARE(segment.text, lines.sliced(segment.lineStart - 1, segment.lineEnd - segment.lineStart + 1).join(QChar('\n')));
            if (i + 1 < result.segments.size()) QCOMPARE(result.segments[i + 1].lineStart, segment.lineEnd - 1);
        }
        QCOMPARE(result.segments.last().lineEnd, 42);
    }
    void notebookV3AllWorksheetsAndResume() {
        const QJsonArray firstCells{
            QJsonObject{{"cell_type", "code"}, {"input", QJsonArray{"FIRST_WORKSHEET_TOKEN = 1\n"}}},
            QJsonObject{{"cell_type", "code"}, {"input", QJsonArray{}}}};
        const QJsonArray secondCells{
            QJsonObject{{"cell_type", "code"}, {"input", QJsonArray{"SECOND_WORKSHEET_TOKEN = 2\n"}}}};
        const QJsonObject notebook{{"nbformat", 3}, {"worksheets", QJsonArray{
            QJsonObject{{"cells", firstCells}}, QJsonObject{{"cells", secondCells}}}}};
        const auto path = write(QStringLiteral("multiple-worksheets.ipynb"), QJsonDocument(notebook).toJson());
        ExtractionOptions options;
        options.code = true;
        const auto all = extractDocument(path, options);
        QVERIFY2(all.error.isEmpty(), qPrintable(all.error));
        QVERIFY(all.complete);
        QCOMPARE(all.segments.size(), 2);
        QVERIFY(all.segments[0].text.contains(QStringLiteral("FIRST_WORKSHEET_TOKEN")));
        QVERIFY(all.segments[1].text.contains(QStringLiteral("SECOND_WORKSHEET_TOKEN")));
        QCOMPARE(all.segments[0].cell, 1);
        QCOMPARE(all.segments[1].cell, 3);

        options.maxSegments = 1;
        const auto first = extractDocument(path, options);
        QCOMPARE(first.state, QStringLiteral("partial"));
        QCOMPARE(first.next, 1);
        QCOMPARE(first.segments.size(), 1);
        QCOMPARE(first.segments[0].text, all.segments[0].text);
        options.start = first.next;
        const auto second = extractDocument(path, options);
        QCOMPARE(second.state, QStringLiteral("complete"));
        QCOMPARE(second.next, 2);
        QCOMPARE(second.segments.size(), 1);
        QCOMPARE(second.segments[0].text, all.segments[1].text);
        QCOMPARE(second.segments[0].cell, 3);
    }
    void unicodeGraphemesStayWhole() {
        const QString grapheme = QString::fromUtf8("👨‍👩‍👧‍👦") + QStringLiteral("a\u0301");
        QString body;
        for (int i = 0; i < 30; ++i) body += grapheme;
        ExtractionOptions options; options.chunkCharacters = 32; options.overlapCharacters = 7;
        const auto result = extractDocument(write(QStringLiteral("emoji.txt"), body.toUtf8()), options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QVERIFY(result.segments.size() > 2);
        for (const auto &segment : result.segments) {
            QVERIFY(!segment.text.front().isLowSurrogate());
            QVERIFY(!segment.text.back().isHighSurrogate());
            QVERIFY(segment.text.front().unicode() != 0x200d);
            QVERIFY(segment.text.back().unicode() != 0x200d);
            QVERIFY(segment.text.front().unicode() != 0x0301);
        }
    }
    void documentSegmentCapPreservesPartialState() {
        ExtractionOptions options;
        options.chunkCharacters = 32;
        options.overlapCharacters = 0;
        options.maxSegments = 40;
        const auto path = write(QStringLiteral("over-cap.txt"), QByteArray(32 * 5002, 'x'));
        auto result = extractDocument(path, options);
        QCOMPARE(result.state, QStringLiteral("partial"));
        QCOMPARE(result.segments.size(), 40);
        QCOMPARE(result.next, 40);
        QVERIFY(!result.complete);
        options.maxSegments = 5000;
        result = extractDocument(path, options);
        QCOMPARE(result.segments.size(), 5000);
        QCOMPARE(result.next, 5000);
        QCOMPARE(result.state, QStringLiteral("partial"));
        QVERIFY(!result.complete);
        options.start = 4999;
        options.maxSegments = 2;
        result = extractDocument(path, options);
        QCOMPARE(result.segments.size(), 1);
        QCOMPARE(result.next, 5000);
        QVERIFY(!result.complete);
        options.start = 5000;
        result = extractDocument(path, options);
        QCOMPARE(result.state, QStringLiteral("failed"));
        QVERIFY(!result.complete);
        QVERIFY(result.segments.isEmpty());
        QVERIFY(result.error.contains(QStringLiteral("5000")));
        QCOMPARE(result.next, 5000);
        // An exactly-full file is complete, and a resumed EOF is not an error.
        const auto exact = write(QStringLiteral("exact-cap.txt"), QByteArray(32 * 5000, 'x'));
        options.start = 0;
        options.maxSegments = 5000;
        result = extractDocument(exact, options);
        QCOMPARE(result.state, QStringLiteral("complete"));
        QCOMPARE(result.segments.size(), 5000);
        options.start = 5000;
        result = extractDocument(exact, options);
        QVERIFY(result.complete);
        QVERIFY(result.segments.isEmpty());
    }
    void codeAndNotebookShareGlobalSegmentCap() {
        ExtractionOptions options;
        options.code = true;
        options.chunkCharacters = 32;
        options.overlapCharacters = 0;
        options.maxSegments = 5000;
        QByteArray code;
        QJsonArray cells;
        for (int i = 0; i < 5002; ++i) {
            const auto line = QStringLiteral("void function_%1() {}\n").arg(i, 5, 10, QChar('0'));
            code += line.toUtf8();
            cells.append(QJsonObject{{"cell_type", "code"}, {"source", line}});
        }
        const auto codePath = write(QStringLiteral("code-cap.cpp"), code);
        const auto notebookPath = write(QStringLiteral("notebook-cap.ipynb"), QJsonDocument(QJsonObject{{"nbformat", 4}, {"cells", cells}}).toJson());
        for (const auto &path : {codePath, notebookPath}) {
            options.start = 0;
            auto result = extractDocument(path, options);
            QCOMPARE(result.state, QStringLiteral("partial"));
            QCOMPARE(result.segments.size(), 5000);
            QCOMPARE(result.next, 5000);
            QVERIFY(!result.complete);
            options.start = 5000;
            result = extractDocument(path, options);
            QCOMPARE(result.state, QStringLiteral("failed"));
            QVERIFY(!result.complete);
            QVERIFY(result.error.contains(QStringLiteral("5000")));
        }
    }
    void codeClassificationAndExclusions() {
        QVERIFY(supportsCode(QStringLiteral("cpp"), QStringLiteral("main.cpp")));
        QVERIFY(supportsCode({}, QStringLiteral("Dockerfile.dev")));
        QVERIFY(!supportsCode(QStringLiteral("json"), QStringLiteral("package.json")));
        QVERIFY(!supportsCode(QStringLiteral("md"), QStringLiteral("LICENSE.md")));
        QVERIFY(!supportsCode(QStringLiteral("js"), QStringLiteral("bundle.min.js")));
        QVERIFY(!supportsCode(QStringLiteral("yaml"), QStringLiteral("credentials.yaml")));
        ExtractionOptions options; options.code = true;
        QCOMPARE(extractDocument(write(QStringLiteral("generated.cpp"), "// Code generated. DO NOT EDIT.\nvoid f() {}"), options).state, QStringLiteral("failed"));
        QCOMPARE(extractDocument(write(QStringLiteral("secret.py"), "key = '''-----BEGIN RSA PRIVATE KEY-----xxx'''"), options).state, QStringLiteral("failed"));
        QVERIFY(supportsDocument(QStringLiteral("DOCX")));
    }
    void cataloguedFormatsReachExtractor_data() {
        QTest::addColumn<QString>("filename");
        QTest::addColumn<QString>("kind");
        for (const QString &suffix : {QStringLiteral("oga"), QStringLiteral("aifc"), QStringLiteral("caf"),
                QStringLiteral("mka"), QStringLiteral("m4b")})
            QTest::newRow(qPrintable(suffix)) << QStringLiteral("sample.") + suffix.toUpper() << QStringLiteral("audio");
        for (const QString &suffix : {QStringLiteral("3gp"), QStringLiteral("flv")})
            QTest::newRow(qPrintable(suffix)) << QStringLiteral("sample.") + suffix.toUpper() << QStringLiteral("video");
        for (const QString &suffix : {QStringLiteral("sr2"), QStringLiteral("srf"), QStringLiteral("srw"),
                QStringLiteral("kdc"), QStringLiteral("mrw"), QStringLiteral("erf"), QStringLiteral("mos"), QStringLiteral("iiq")})
            QTest::newRow(qPrintable(suffix)) << QStringLiteral("sample.") + suffix.toUpper() << QStringLiteral("image");
        QTest::newRow("qml") << QStringLiteral("Sample.QML") << QStringLiteral("code");
        QTest::newRow("dockerfile-exact") << QStringLiteral("DOCKERFILE") << QStringLiteral("code");
        QTest::newRow("dockerfile-suffix") << QStringLiteral("dOcKeRfIlE.dev") << QStringLiteral("code");
        QTest::newRow("containerfile") << QStringLiteral("containerfile") << QStringLiteral("code");
        QTest::newRow("cmakelists") << QStringLiteral("CMAKELISTS.TXT") << QStringLiteral("code");
    }
    void cataloguedFormatsReachExtractor() {
        QFETCH(QString, filename);
        QFETCH(QString, kind);
        const auto &root = *directory;
        const QString path = root.filePath(filename);
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly));
        const QByteArray content("import QtQuick\nItem { property string label: \"Search result\" }\n");
        QCOMPARE(file.write(content), content.size());
        file.close();
        const bool code = kind == QStringLiteral("code");
        const auto scanned = FileCatalog::scan({{root.path()}, {}, false,
            code ? FolderRules::Scope::Code : FolderRules::Scope::General});
        QVERIFY2(scanned.errors.isEmpty(), qPrintable(scanned.errors.join('\n')));
        QCOMPARE(scanned.files.size(), 1);
        QCOMPARE(scanned.files.first().kind, kind);
        QCOMPARE(Extractor::kindForPath(scanned.files.first().path, code), kind);
        FileRecord described;
        QString error;
        QVERIFY2(FileCatalog::describe(path, &described, &error), qPrintable(error));
        QCOMPARE(described.kind, kind);
        // Only source text is decoded here. Media and RAW rows verify routing,
        // without treating arbitrary bytes as valid files for those decoders.
        if (code) {
            ExtractionOptions options;
            options.code = true;
            const auto extracted = Extractor::extract(path, options);
            QVERIFY2(extracted.complete, qPrintable(extracted.error));
            QVERIFY(!extracted.segments.isEmpty());
            QCOMPARE(extracted.segments.first().kind, QStringLiteral("code"));
            QVERIFY(allText(extracted).contains(QStringLiteral("Search result")));
        }
    }
    void codeFilenameMatchingKeepsExclusions() {
        for (const QString &name : {QStringLiteral("DockerfileBackup"), QStringLiteral("Dockerfile.pem"),
                QStringLiteral("credentials.yaml"), QStringLiteral("SecretPicture.png"), QStringLiteral("SecretNotes.txt"),
                QStringLiteral("bundle.MIN.JS")}) {
            const QString path = write(name, "safe fixture content\n");
            QVERIFY(!path.isEmpty());
            QVERIFY(!supportsCode(QFileInfo(path).suffix(), name));
            QVERIFY(Extractor::kindForPath(path, true).isEmpty());
            ExtractionOptions codeOptions;
            codeOptions.code = true;
            const auto code = Extractor::extract(path, codeOptions);
            QCOMPARE(code.state, QStringLiteral("unsupported"));
            QVERIFY(code.segments.isEmpty());
            FileRecord record;
            QString error;
            if (name == QStringLiteral("SecretPicture.png") || name == QStringLiteral("SecretNotes.txt")) {
                // General search accepts supported formats independently of
                // code-only filename exclusions. describe serves both spaces.
                QVERIFY2(FileCatalog::describe(path, &record, &error), qPrintable(error));
                QVERIFY(error.isEmpty());
                QCOMPARE(record.kind, name.endsWith(QStringLiteral(".png")) ? QStringLiteral("image") : QStringLiteral("text"));
                QCOMPARE(Extractor::kindForPath(path, false), record.kind);
                if (record.kind == QStringLiteral("text")) {
                    const auto general = Extractor::extract(path);
                    QVERIFY2(general.complete, qPrintable(general.error));
                    QVERIFY(allText(general).contains(QStringLiteral("safe fixture content")));
                }
            } else {
                QVERIFY(!FileCatalog::describe(path, &record, &error));
                QVERIFY(!error.isEmpty());
            }
        }
        const auto code = FileCatalog::scan({{directory->path()}, {}, false, FolderRules::Scope::Code});
        QVERIFY2(code.errors.isEmpty(), qPrintable(code.errors.join('\n')));
        QVERIFY(code.files.isEmpty());
        const auto general = FileCatalog::scan({{directory->path()}, {}, false});
        QVERIFY2(general.errors.isEmpty(), qPrintable(general.errors.join('\n')));
        QCOMPARE(general.files.size(), 2);
    }
    void transportClassificationMatchesExtraction() {
        const auto &root = *directory;
        int count = 0;
        for (const QString &suffix : {QStringLiteral("ts"), QStringLiteral("mts"), QStringLiteral("m2ts")}) {
            for (const int stride : {188, 192}) {
                for (int offset = 0; offset <= 4; ++offset) {
                    const QString path = root.filePath(QStringLiteral("transport-%1-%2.%3").arg(stride).arg(offset).arg(suffix));
                    QByteArray bytes(offset + 2 * stride + 1, '\0');
                    bytes[offset] = bytes[offset + stride] = bytes[offset + 2 * stride] = 0x47;
                    QFile file(path);
                    QVERIFY(file.open(QIODevice::WriteOnly));
                    QCOMPARE(file.write(bytes), bytes.size());
                    file.close();
                    FileRecord record;
                    QString error;
                    QVERIFY2(FileCatalog::describe(path, &record, &error), qPrintable(error));
                    QCOMPARE(record.kind, QStringLiteral("video"));
                    QCOMPARE(Extractor::kindForPath(path, false), record.kind);
                    QVERIFY(Extractor::kindForPath(path, true).isEmpty());
                    ++count;
                }
            }
        }
        const auto general = FileCatalog::scan({{root.path()}, {}, false, FolderRules::Scope::General});
        QVERIFY2(general.errors.isEmpty(), qPrintable(general.errors.join('\n')));
        QCOMPARE(general.files.size(), count);
        const auto code = FileCatalog::scan({{root.path()}, {}, false, FolderRules::Scope::Code});
        QVERIFY2(code.errors.isEmpty(), qPrintable(code.errors.join('\n')));
        QVERIFY(code.files.isEmpty());
    }
    void sourceAndTruncatedTransportAreNotVideo() {
        const auto &root = *directory;
        for (const QString &suffix : {QStringLiteral("ts"), QStringLiteral("mts"), QStringLiteral("m2ts")}) {
            const QString source = write(QStringLiteral("source.") + suffix, "export const answer = 42;\n");
            QVERIFY(!source.isEmpty());
            QByteArray truncated(376, '\0');
            truncated[0] = truncated[188] = 0x47;
            const QString partial = write(QStringLiteral("truncated.") + suffix, truncated);
            QVERIFY(!partial.isEmpty());
            const bool code = suffix != QStringLiteral("m2ts");
            for (const auto &path : {source, partial}) {
                QVERIFY(Extractor::kindForPath(path, false).isEmpty());
                QCOMPARE(Extractor::kindForPath(path, true), code ? QStringLiteral("code") : QString{});
                FileRecord record;
                QString error;
                QCOMPARE(FileCatalog::describe(path, &record, &error), code);
                if (code) QCOMPARE(record.kind, QStringLiteral("code"));
            }
            if (code) {
                ExtractionOptions options;
                options.code = true;
                const auto extracted = Extractor::extract(source, options);
                QVERIFY2(extracted.complete, qPrintable(extracted.error));
                QVERIFY(allText(extracted).contains(QStringLiteral("answer")));
            }
        }
        const auto general = FileCatalog::scan({{root.path()}, {}, false, FolderRules::Scope::General});
        QVERIFY2(general.errors.isEmpty(), qPrintable(general.errors.join('\n')));
        QVERIFY(general.files.isEmpty());
        const auto code = FileCatalog::scan({{root.path()}, {}, false, FolderRules::Scope::Code});
        QVERIFY2(code.errors.isEmpty(), qPrintable(code.errors.join('\n')));
        QCOMPARE(code.files.size(), 4);
    }
};

QTEST_MAIN(DocumentTests)
#include "document_tests.moc"
