#include "extraction/extractor.h"
#include "runtime/EmbeddingRuntime.h"
#include "storage/index_repository.h"
#include "support/test_environment.h"
#include <QCommandLineParser>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QImage>
#include <QJsonDocument>
#include <QPainter>
#include <QPdfWriter>
#include <QSaveFile>
#include <QSet>
#include <QStandardPaths>
#include <QTextStream>
#include <QtEndian>
#include <algorithm>
#include <cmath>
#include <stdexcept>

using namespace qcopilots_search;
namespace {
constexpr double pi = 3.14159265358979323846;
struct Input {
    QString id, file, modality, en, zh;
    bool code = false;
};
void require(bool value, const QString &error) {
    if (!value) throw std::runtime_error(error.toStdString());
}
QByteArray read(const QString &path) {
    QFile file(path);
    require(file.open(QIODevice::ReadOnly), "Cannot read fixture: " + path + " " + file.errorString());
    return file.readAll();
}
void write(const QString &path, const QByteArray &bytes) {
    QSaveFile file(path);
    require(file.open(QIODevice::WriteOnly), "Cannot write: " + path);
    require(file.write(bytes) == bytes.size() && file.commit(), "Cannot commit: " + path);
}
QString sha256(const QString &path) {
    return QString::fromLatin1(QCryptographicHash::hash(read(path), QCryptographicHash::Sha256).toHex());
}
QString fixtureRoot() { return QStringLiteral(QCOPILOTS_SEARCH_FUNCTIONAL_FIXTURES); }
void validateAssets() {
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(read(fixtureRoot() + "/assets.json"), &error);
    require(error.error == QJsonParseError::NoError && document.isObject(), "Invalid fixture manifest");
    const auto manifest = document.object();
    require(manifest.value("schema").toInt() == 1, "Unsupported fixture schema");
    QSet<QString> names;
    for (const auto &entry : manifest.value("assets").toArray()) {
        const auto item = entry.toObject();
        const auto name = item.value("path").toString();
        require(name.startsWith("media/") && !name.contains("..") && !name.contains('\\')
                    && !names.contains(name), "Unsafe or duplicate fixture path: " + name);
        names.insert(name);
        const QFileInfo asset(fixtureRoot() + "/" + name);
        require(!asset.isSymLink() && asset.isFile() && asset.size() == item.value("bytes").toInteger(),
                "Wrong fixture size or file type: " + name);
        require(sha256(asset.absoluteFilePath()) == item.value("sha256").toString(),
                "Wrong fixture SHA-256: " + name);
        require(!item.value("source").toString().isEmpty() && !item.value("license").toString().isEmpty(),
                "Fixture attribution is missing: " + name);
    }
    for (const auto &name : {"photo-001.jpg", "photo-002.jpg", "speech-001.wav", "speech-002.wav",
                              "environment-001.ogg", "video-001.webm"})
        require(names.contains("media/" + QString::fromLatin1(name)), "Required fixture is missing");
}
void pdf(const QString &path, const QString &text) {
    QPdfWriter writer(path);
    writer.setResolution(144);
    writer.setPageSize(QPageSize(QPageSize::A4));
    QPainter painter(&writer);
    require(painter.isActive(), "Cannot paint PDF");
    painter.setFont(QFont("sans-serif", 18));
    painter.drawText(QRect(100, 100, 950, 1300), Qt::TextWordWrap, text);
    require(painter.end(), "Cannot finish PDF");
}
QImage illustration() {
    QImage image(640, 480, QImage::Format_RGB32);
    image.fill(Qt::white);
    QPainter painter(&image);
    painter.setRenderHint(QPainter::Antialiasing);
    painter.setPen(Qt::NoPen);
    painter.setBrush(QColor("#e52b25"));
    painter.drawEllipse(QRect(150, 70, 340, 340));
    painter.end();
    return image;
}
void ocrImage(const QString &path, const QString &text) {
    QImage image(1200, 700, QImage::Format_RGB32);
    image.fill(Qt::white);
    QPainter painter(&image);
    painter.setPen(Qt::black);
    painter.setFont(QFont("sans-serif", 34));
    painter.drawText(QRect(50, 50, 1100, 600), Qt::TextWordWrap, text);
    painter.end();
    require(image.save(path), "Cannot save OCR fixture");
}
void le(QByteArray &b, quint32 v) {
    char bytes[4];
    qToLittleEndian(v, reinterpret_cast<uchar *>(bytes));
    b.append(bytes, 4);
}
void le16(QByteArray &b, quint16 v) {
    char bytes[2];
    qToLittleEndian(v, reinterpret_cast<uchar *>(bytes));
    b.append(bytes, 2);
}
QByteArray chunk(const char *tag, QByteArray data) {
    QByteArray b(tag, 4);
    le(b, quint32(data.size()));
    b += data;
    if (data.size() % 2)
        b += '\0';
    return b;
}
QByteArray list(const char *tag, const QByteArray &data) { return chunk("LIST", QByteArray(tag, 4) + data); }
void video(const QString &path, int scene) {
    // Original six-second uncompressed AVI. The production FFmpeg decoder must
    // decode its real frames and timestamps, including the moving object.
    constexpr int width = 320, height = 240, fps = 4, frames = 24, size = width * height * 3;
    QByteArray avih;
    for (quint32 v : {250000u, quint32(size * fps), 0u, 0x10u, quint32(frames), 0u, 1u, quint32(size),
                      quint32(width), quint32(height), 0u, 0u, 0u, 0u})
        le(avih, v);
    QByteArray strh("vidsDIB ", 8);
    for (quint32 v : {0u, 0u, 0u, 1u, quint32(fps), 0u, quint32(frames), quint32(size), 0xffffffffu, 0u})
        le(strh, v);
    le16(strh, 0);
    le16(strh, 0);
    le16(strh, width);
    le16(strh, height);
    QByteArray strf;
    le(strf, 40);
    le(strf, width);
    le(strf, height);
    le16(strf, 1);
    le16(strf, 24);
    for (quint32 v : {0u, quint32(size), 0u, 0u, 0u, 0u})
        le(strf, v);
    QByteArray movi, index;
    quint32 offset = 4;
    for (int i = 0; i < frames; ++i) {
        QImage im(width, height, QImage::Format_RGB32);
        im.fill(Qt::white);
        QPainter p(&im);
        p.setPen(Qt::NoPen);
        p.setBrush(scene ? Qt::blue : Qt::red);
        int x = 10 + i * 10;
        int y = scene ? int(80 + 55 * sin(i * 0.5)) : 80;
        if (scene)
            p.drawRect(x, y, 50, 50);
        else
            p.drawEllipse(x, y, 50, 50);
        p.end();
        QByteArray raw;
        raw.reserve(size);
        for (int yy = height - 1; yy >= 0; --yy)
            for (int xx = 0; xx < width; ++xx) {
                QRgb c = im.pixel(xx, yy);
                raw += char(qBlue(c));
                raw += char(qGreen(c));
                raw += char(qRed(c));
            }
        movi += chunk("00db", raw);
        index += "00db";
        le(index, 0x10);
        le(index, offset);
        le(index, size);
        offset += size + 8;
    }
    auto data = QByteArray("AVI ", 4) +
                list("hdrl", chunk("avih", avih) + list("strl", chunk("strh", strh) + chunk("strf", strf))) +
                list("movi", movi) + chunk("idx1", index);
    write(path, chunk("RIFF", data));
}
void tone(const QString &path, bool alarm) {
    QByteArray pcm;
    constexpr int sampleRate = 16000;
    for (int i = 0; i < sampleRate * 6; ++i) {
        double t = double(i) / sampleRate;
        double amplitude = alarm ? (std::fmod(t, 0.7) < 0.35 ? 0.45 : 0.0) : 0.35;
        double hz = alarm ? 880 : 440;
        le16(pcm, quint16(qint16(std::sin(2 * pi * hz * t) * amplitude * 32767)));
    }
    QByteArray format;
    le16(format, 1);
    le16(format, 1);
    le(format, sampleRate);
    le(format, sampleRate * 2);
    le16(format, 2);
    le16(format, 16);
    write(path, chunk("RIFF", QByteArray("WAVE", 4) + chunk("fmt ", format) + chunk("data", pcm)));
}

QList<Input> prepare(const QString &root) {
    QList<Input> files;
    auto add = [&](const QString &id, const QString &extension, const QString &modality,
                   const QString &en, const QString &zh, bool code = false) {
        const QString directory = root + "/" + id;
        require(QDir().mkpath(directory), "Cannot create fixture directory");
        const QString path = directory + "/source." + extension;
        files.append({id, path, modality, en, zh, code});
        return path;
    };
    write(add("text", "txt", "text", "Instructions for storing garden seeds", "保存园艺种子的说明"),
          "Keep the garden seeds in a dry envelope. Label each envelope with the plant name and "
          "collection date. Store the envelopes in a cool cupboard until spring.\n");
    pdf(add("pdf", "pdf", "pdf", "A library notice about opening hours", "图书馆开放时间通知"),
        "The village library opens at nine on Tuesday. Readers may borrow three books for two weeks. "
        "Please return books at the service desk before closing time.");
    write(add("code", "py", "code", "Compute a SHA256 checksum while reading a file", "流式读取文件并计算校验和", true),
          "import hashlib\ndef digest_file(path):\n    digest = hashlib.sha256()\n"
          "    with open(path, 'rb') as stream:\n        while block := stream.read(65536):\n"
          "            digest.update(block)\n    return digest.hexdigest()\n");
    require(illustration().save(add("illustration", "png", "image", "A red circle on white paper", "白纸上的红色圆形")),
            "Cannot save illustration");
    write(add("photo", "jpg", "image", "The Earth seen from space", "从太空看到的地球"),
          read(fixtureRoot() + "/media/photo-001.jpg"));
    ocrImage(add("ocr-en", "png", "ocr", "A notice about a library closing day", "图书馆闭馆通知"),
             "LIBRARY NOTICE\nClosed on Monday\nPlease return books on Tuesday");
    ocrImage(add("ocr-zh", "png", "ocr", "A sign asking people to keep an exit clear", "保持安全出口畅通的标牌"),
             "安全出口\n请勿堆放物品\n保持通道畅通");
    write(add("speech", "wav", "audio", "An announcement about a delayed airport train", "机场列车延误广播"),
          read(fixtureRoot() + "/media/speech-001.wav"));
    write(add("environment", "ogg", "audio", "A dog barking", "狗叫声"),
          read(fixtureRoot() + "/media/environment-001.ogg"));
    tone(add("tone", "wav", "audio", "A steady continuous musical tone", "持续稳定的纯音"), false);
    video(add("video-generated", "avi", "video", "A red circle moving across the screen", "在画面中移动的红色圆形"), 0);
    write(add("video-natural", "webm", "video", "Heavy rain falling outdoors", "户外大雨"),
          read(fixtureRoot() + "/media/video-001.webm"));
    require(files.size() == 12, "The functional set must contain exactly twelve inputs");
    return files;
}
void validateVector(const QVector<float> &vector, int dimensions, const QString &context, double tolerance) {
    require(vector.size() == dimensions, context + QString(": expected %1 values").arg(dimensions));
    double squaredNorm = 0;
    for (const float value : vector) {
        require(std::isfinite(value), context + ": non-finite embedding");
        squaredNorm += double(value) * value;
    }
    require(std::abs(std::sqrt(squaredNorm) - 1.0) <= tolerance, context + ": embedding is not normalized");
}
void validateLocation(const Input &input, const Segment &segment) {
    require(segment.path == input.file, "Segment source path was lost: " + input.id);
    if (input.code)
        require(segment.lineStart > 0 && segment.lineEnd >= segment.lineStart,
                "Text/code line range was lost: " + input.id);
    else if (input.modality == "pdf")
        require(segment.page > 0, "PDF page was lost");
    else if (input.modality == "audio" || input.modality == "video")
        require(segment.startMs >= 0 && segment.endMs > segment.startMs,
                "Media time range was lost: " + input.id);
}
void validateHit(const Input &input, const SearchHit &hit, const QVector<Segment> &stored) {
    require(hit.path == input.file && hit.matchSource == "semantic", "Search did not return the actual semantic source");
    require(!hit.sourceVersion.isEmpty() && std::isfinite(hit.score), "Search metadata is incomplete");
    const auto position = std::find_if(stored.cbegin(), stored.cend(), [&](const Segment &segment) {
        return segment.id == hit.segmentId;
    });
    require(position != stored.cend(), "Search references a missing persisted segment");
    validateLocation(input, *position);
    require(hit.kind == position->kind && hit.text == position->text
                && hit.page == position->page && hit.lineStart == position->lineStart
                && hit.lineEnd == position->lineEnd && hit.cell == position->cell
                && hit.startMs == position->startMs && hit.endMs == position->endMs,
            "Search changed the persisted location fields: " + input.id);
}
} // namespace

int main(int argc, char **argv) {
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication app(argc, argv);
    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption({"validate-only", "Validate the fixed assets and generated functional inputs without inference."});
    parser.addOption({"model-dir", "Read-only verified model directory.", "path",
                      qEnvironmentVariable("QCOPILOTS_SEARCH_MODEL_DIR")});
    parser.addOption({"tessdata-dir", "OCR trained data directory.", "path",
                      qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR",
                                           QStringLiteral(QCOPILOTS_SEARCH_SEMANTIC_TESSDATA))});
    parser.addOption({"report", "Write JSON report to this explicit location.", "path"});
    parser.addOption({"preserve", "Retain the isolated inputs and index for diagnosis."});
    // Avoid QCommandLineParser's Windows native dialog when streams are redirected.
    if (!parser.parse(app.arguments())) {
        QTextStream(stderr) << parser.errorText() << '\n' << parser.helpText();
        return 2;
    }
    if (parser.isSet("help")) {
        QTextStream(stdout) << parser.helpText();
        return 0;
    }
    const bool validateOnly = parser.isSet("validate-only");
    QJsonObject report{{"schema", 2}, {"test", "multimodal-functional"}, {"passed", false},
                       {"realModel", false}, {"persisted", false}, {"reloaded", false},
                       {"startedAt", QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
                       {"mode", validateOnly ? "validate-only" : "functional"},
                       {"completedFiles", 0}, {"completedQueries", 0}, {"actualEmbeddedSegments", 0}};
    int result = 1;
    std::unique_ptr<test::TestEnvironment> environment;
    try {
        validateAssets();
        environment = std::make_unique<test::TestEnvironment>("semantic 中文 space");
        require(environment->isValid(), environment->error());
        const QString corpus = environment->path("inputs");
        const auto inputs = prepare(corpus);
        report["fixtures"] = inputs.size();
        report["queryCount"] = inputs.size() * 2;
        report["fixtureManifestSha256"] = sha256(fixtureRoot() + "/assets.json");
        report["generatorSha256"] = sha256(QStringLiteral(QCOPILOTS_SEARCH_SEMANTIC_SOURCE));
        if (validateOnly) {
            report["status"] = "manifest-passed";
            result = 0;
        } else {
            require(!parser.value("model-dir").isEmpty(), "A verified model directory is required for the full functional test");
            EmbeddingRuntime runtime;
            runtime.setThreadCount(4);
            QString error;
            require(runtime.load(parser.value("model-dir"), false, &error), error);
            require(runtime.modelId() == QStringLiteral("embeddinggemma-2")
                        && runtime.dimensions() == embeddingDimensions,
                    "The full multimodal regression requires the default EmbeddingGemma 2 contract");
            report["realModel"] = true;
            report["modelId"] = runtime.modelId();
            report["embeddingDimensions"] = runtime.dimensions();
            report["modelFingerprint"] = runtime.modelFingerprint();
            report["extractionFingerprint"] = Extractor::pipelineFingerprint();
            const auto paths = environment->runtimePaths();
            require(paths.ensure(&error), error);
            QJsonArray files;
            int embedded = 0;
            {
                IndexRepository index;
                require(index.open(paths, &error), error);
                require(index.setPipelineFingerprint(runtime.modelFingerprint() + "|" + Extractor::pipelineFingerprint(), &error), error);
                for (const auto &input : inputs) {
                    ExtractionOptions options;
                    options.tempDir = environment->path("media/" + input.id);
                    require(QDir().mkpath(options.tempDir), "Cannot create extracted media directory");
                    options.tessdataDir = parser.value("tessdata-dir");
                    options.code = input.code;
                    options.maxSegments = 100;
                    options.enableOcr = true;
                    auto extraction = Extractor::extract(input.file, options);
                    require(extraction.complete && extraction.state == "complete" && !extraction.segments.isEmpty(),
                            "Extraction failed for " + input.id + ": " + extraction.state + " " + extraction.error);
                    QVector<Segment> segments;
                    QJsonObject modalities;
                    qsizetype textCharacters = 0;
                    bool containsHan = false;
                    for (auto segment : extraction.segments) {
                        validateLocation(input, segment);
                        modalities[segment.modality] = modalities.value(segment.modality).toInt() + 1;
                        textCharacters += segment.text.size();
                        for (const auto character : segment.text)
                            containsHan |= character.script() == QChar::Script_Han;
                        if (segment.modality == "image" || segment.modality == "video")
                            segment.embedding = runtime.embedImage(segment.mediaPath, segment.imageBudget, &error);
                        else if (segment.modality == "audio")
                            segment.embedding = runtime.embedAudio(segment.mediaPath, &error);
                        else
                            segment.embedding = runtime.embedText(segment.text, PromptKind::Document, segment.title, &error);
                        require(error.isEmpty(), "Encoding failed for " + input.id + ": " + error);
                        validateVector(segment.embedding, runtime.dimensions(), input.id, .001);
                        segments.append(segment);
                        ++embedded;
                    }
                    if (input.modality == "ocr")
                        require(textCharacters > 0 && (input.id != "ocr-zh" || containsHan),
                                "OCR did not produce the expected script: " + input.id);
                    const QString expectedModality = input.code || input.modality == "text" || input.modality == "pdf"
                        ? "text" : input.modality == "ocr" || input.modality == "video" ? "image" : input.modality;
                    require(modalities.value(expectedModality).toInt() > 0, "Missing extraction channel: " + input.id);
                    require(index.replaceFiles({IndexedFile{input.file, extraction.sourceVersion, input.code, segments}}, &error, true), error);
                    const auto stored = index.fileSegments(input.file, input.code, &error);
                    require(error.isEmpty() && stored.size() == segments.size(), "Cannot read persisted segments: " + error);
                    for (const auto &segment : stored)
                        validateVector(segment.embedding, runtime.dimensions(), input.id + " persisted", .002);
                    files.append(QJsonObject{{"id", input.id}, {"file", QDir(corpus).relativeFilePath(input.file)},
                        {"modality", input.modality}, {"code", input.code}, {"segments", segments.size()},
                        {"extractedModalities", modalities}, {"extractedTextCharacters", textCharacters},
                        {"sha256", sha256(input.file)}});
                    report["files"] = files;
                    report["completedFiles"] = files.size();
                    report["actualEmbeddedSegments"] = embedded;
                    QTextStream(stdout) << "Encoded " << input.id << " (" << segments.size() << " segments)\n";
                }
                require(index.flush(&error) && index.checkIntegrity(&error), error);
                report["persisted"] = true;
            }
            // A fresh repository must load authoritative persisted vectors and the
            // derived index. Queries use the same text-only runtime mode as the service.
            runtime.close();
            require(runtime.load(parser.value("model-dir"), true, &error), error);
            IndexRepository reloaded;
            require(reloaded.open(paths, &error) && reloaded.checkIntegrity(&error), error);
            report["reloaded"] = true;
            QJsonArray queries;
            for (const auto &input : inputs) {
                const auto stored = reloaded.fileSegments(input.file, input.code, &error);
                require(error.isEmpty() && !stored.isEmpty(), "Reload lost file segments: " + error);
                for (const auto &segment : stored)
                    validateVector(segment.embedding, runtime.dimensions(), input.id + " reloaded", .002);
                for (const auto &language : {QStringLiteral("en"), QStringLiteral("zh")}) {
                    const QString query = language == "en" ? input.en : input.zh;
                    const auto embedding = runtime.embedText(query, input.code ? PromptKind::CodeQuery : PromptKind::Query, {}, &error);
                    require(error.isEmpty(), "Query encoding failed: " + error);
                    validateVector(embedding, runtime.dimensions(), input.id + " query", .001);
                    SearchOptions options;
                    // Empty query disables keyword fallback. Each directory contains
                    // one source, exercising the ordinary production semantic path
                    // without imposing a ranking or acceptance threshold.
                    options.queryEmbedding = embedding;
                    options.root = QFileInfo(input.file).absolutePath();
                    options.code = input.code;
                    options.limit = 10;
                    const auto hits = reloaded.search(options, &error);
                    require(error.isEmpty() && hits.size() == 1, "Isolated semantic search failed for " + input.id + ": " + error);
                    validateHit(input, hits.front(), stored);
                    require(hits.front().sourceVersion == reloaded.fileVersion(input.file, input.code, &error)
                                && error.isEmpty(), "Search changed the persisted source version: " + input.id);
                    queries.append(QJsonObject{{"id", input.id + "-" + language}, {"fixtureId", input.id},
                        {"language", language}, {"query", query}, {"source", hits.front().matchSource},
                        {"returnedFile", QDir(corpus).relativeFilePath(hits.front().path)},
                        {"location", QJsonObject{{"page", hits.front().page}, {"lineStart", hits.front().lineStart},
                            {"lineEnd", hits.front().lineEnd}, {"cell", hits.front().cell},
                            {"startMs", hits.front().startMs}, {"endMs", hits.front().endMs}}},
                        {"locationPassed", true}, {"passed", true}});
                    report["queries"] = queries;
                    report["completedQueries"] = queries.size();
                    QTextStream(stdout) << "Verified semantic source and location: " << input.id << " " << language << '\n';
                }
            }
            report["passed"] = true;
            report["status"] = "passed";
            result = 0;
        }
    } catch (const std::exception &exception) {
        report["status"] = "failed";
        report["error"] = QString::fromUtf8(exception.what());
        QTextStream(stderr) << exception.what() << '\n';
    }
    report["finishedAt"] = QDateTime::currentDateTimeUtc().toString(Qt::ISODate);
    QString reportPath = parser.value("report");
    if (reportPath.isEmpty() && !validateOnly)
        reportPath = qEnvironmentVariable("QCOPILOTS_SEARCH_SEMANTIC_REPORT");
    if (reportPath.isEmpty() && environment && !validateOnly)
        reportPath = environment->path("qcs-semantic-functional.json");
    if (environment && (parser.isSet("preserve") || result != 0
        || (!reportPath.isEmpty() && QDir::cleanPath(reportPath).startsWith(environment->root() + "/")))) {
        environment->preserve();
        report["preservedRoot"] = environment->root();
    }
    environment.reset();
    const auto cleanupError = test::takeCleanupErrors();
    if (!cleanupError.isEmpty()) {
        result = 1;
        report["status"] = "cleanup-failed";
        report["passed"] = false;
        report["cleanupError"] = cleanupError;
        QTextStream(stderr) << cleanupError << '\n';
    }
    if (!reportPath.isEmpty()) {
        report["reportPath"] = reportPath;
        try { write(reportPath, QJsonDocument(report).toJson()); }
        catch (const std::exception &exception) {
            QTextStream(stderr) << exception.what() << '\n';
            return 1;
        }
    }
    QTextStream(stdout) << QJsonDocument(QJsonObject{{"status", report.value("status")},
        {"passed", report.value("passed")}, {"reportPath", reportPath},
        {"completedFiles", report.value("completedFiles")}, {"completedQueries", report.value("completedQueries")},
        {"error", report.value("error")}}).toJson(QJsonDocument::Compact) << '\n';
    return result;
}
