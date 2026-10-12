#include "extraction/extractor.h"
#include "extraction/image_extractor.h"
#include <QDir>
#include <QCryptographicHash>
#include <QFile>
#include <QFontDatabase>
#include <QFontMetrics>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPainter>
#include <QPdfWriter>
#include <QPdfDocument>
#include <QPdfSelection>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStandardPaths>
#include <QThread>
#include <memory>
#include "test_directory.h"
#include <QtTest>

using namespace qcopilots_search;


class ImageExtractionTests : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void classification_data()
    {
        QTest::addColumn<QString>("suffix"); QTest::addColumn<bool>("image");
        for (const QString &value : {QStringLiteral("png"), QStringLiteral("HEIC"), QStringLiteral("dng"), QStringLiteral("nef"), QStringLiteral("webp")})
            QTest::newRow(qPrintable(value)) << value << true;
        QTest::newRow("source") << QStringLiteral("cpp") << false;
        QTest::newRow("unknown") << QStringLiteral("unrecognizable") << false;
    }
    void classification() { QFETCH(QString, suffix); QFETCH(bool, image); QCOMPARE(supportsImage(suffix), image); }
    void imagePreparation()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("中文 picture.png"));
        QImage source(2400, 1200, QImage::Format_RGB32); source.fill(Qt::red); QVERIFY(source.save(path));
        ExtractionOptions options; options.enableOcr = false;
        options.tempDir = QDir(temporary.path()).filePath(QStringLiteral("独立 media cache"));
        auto result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("complete")); QVERIFY2(result.error.isEmpty(), qPrintable(result.error));
        QCOMPARE(result.segments.size(), 1); QCOMPARE(result.next, 1); QVERIFY(result.complete);
        const auto segment = result.segments.first();
        QCOMPARE(segment.path, path); QCOMPARE(segment.modality, QStringLiteral("image"));
        QVERIFY(segment.mediaPath.startsWith(options.tempDir + QDir::separator()) || segment.mediaPath.startsWith(options.tempDir + '/'));
        const QImage prepared(segment.mediaPath); QCOMPARE(prepared.size(), QSize(1024, 512));
        QCOMPARE(prepared.pixelColor(4, 4), QColor(Qt::red)); QVERIFY(!result.sourceVersion.isEmpty());
        options.start = result.next;
        result = Extractor::extract(path, options); QVERIFY(result.complete); QVERIFY(result.segments.isEmpty());
    }
    void screenshotBudgetAndScale()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("截图 2026.png"));
        QImage source(1920,1080,QImage::Format_RGB32); source.fill(Qt::gray); QVERIFY(source.save(path));
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        const auto result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("complete")); QCOMPARE(result.segments.size(), 1);
        QCOMPARE(result.segments.first().imageBudget, 280);
        QCOMPARE(QImage(result.segments.first().mediaPath).size(), QSize(1536,864));
        QString error; QCOMPARE(loadPreviewImage(path, 320, &error).size(), QSize(320,180)); QVERIFY(error.isEmpty());
        QVERIFY(loadPreviewImage(path, 0, &error).isNull()); QVERIFY(!error.isEmpty());
        QVERIFY(loadPreviewImage(path, 320, &error, [] { return true; }).isNull());
        QCOMPARE(error, QStringLiteral("Preview cancelled"));
    }
    void transparentImagePreservesVisibleContentForModelAndOcr()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("透明 text and shape.png"));
        QImage source(1000, 300, QImage::Format_ARGB32_Premultiplied); source.fill(Qt::transparent);
        {
            QPainter painter(&source); QFont font(QStringLiteral("Arial")); font.setPixelSize(64); painter.setFont(font);
            painter.setPen(Qt::black); painter.drawText(50, 120, QStringLiteral("4815162342"));
            painter.fillRect(QRect(50, 200, 100, 50), Qt::black);
            painter.fillRect(QRect(200, 200, 100, 50), QColor(0, 0, 0, 128));
        }
        QVERIFY(source.save(path));
        ExtractionOptions options; options.tempDir = QDir(temporary.path()).filePath("media");
        options.tessdataDir = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
        QVERIFY2(!options.tessdataDir.isEmpty(), "The pinned OCR data directory is required");
        QString error;
        const QString directText = recognizeText(source, options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error)); QVERIFY2(directText.contains(QStringLiteral("4815162342")), qPrintable(directText));
        const auto result = Extractor::extract(path, options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QCOMPARE(result.segments.size(), 1);
        const auto &segment = result.segments.first();
        QVERIFY2(segment.text.contains(QStringLiteral("4815162342")), qPrintable(segment.text));
        const QImage prepared(segment.mediaPath); QVERIFY(!prepared.isNull()); QVERIFY(!prepared.hasAlphaChannel());
        QCOMPARE(prepared.pixelColor(0, 0), QColor(Qt::white));
        QCOMPARE(prepared.pixelColor(75, 225), QColor(Qt::black));
        const QColor blended = prepared.pixelColor(225, 225);
        QVERIFY(blended.red() >= 126 && blended.red() <= 128);
        QCOMPARE(blended.green(), blended.red()); QCOMPARE(blended.blue(), blended.red());
    }
    void transportStreamIsNotSourceCode()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("stream.ts"));
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
        QByteArray packets(188 * 3, '\0'); packets[0] = packets[188] = packets[376] = '\x47';
        QCOMPARE(file.write(packets), qint64(packets.size())); file.close();
        QCOMPARE(Extractor::kindForPath(path), QStringLiteral("video")); QVERIFY(Extractor::kindForPath(path, true).isEmpty());
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Truncate)); file.write("export function search() { return 'text' }\n"); file.close();
        QVERIFY(Extractor::kindForPath(path).isEmpty()); QCOMPARE(Extractor::kindForPath(path, true), QStringLiteral("code"));
    }
    void realCameraFormats_data()
    {
        QTest::addColumn<QString>("name"); QTest::addColumn<QSize>("size");
        QTest::newRow("HEIC") << QStringLiteral("generated-gradient.heic") << QSize(256,128);
        QTest::newRow("DNG-uncompressed") << QStringLiteral("generated-uncompressed.dng") << QSize(32,32);
        QTest::newRow("DNG-float-deflate") << QStringLiteral("generated-deflate.dng") << QSize(32,32);
        QTest::newRow("DNG-lossy-jpeg") << QStringLiteral("generated-jpeg.dng") << QSize(64,64);
    }
    void jpegDngCodecContract()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const auto path = QDir(temporary.path()).filePath(QStringLiteral("中文 diagnostic generated-jpeg.dng"));
        QVERIFY(QFile::copy(QStringLiteral(":/images/generated-jpeg.dng"), path));
        QProcess helper;
        helper.setWorkingDirectory(temporary.path());
        helper.start(QString::fromUtf8(QCOPILOTS_SEARCH_TEST_JPEG_PROBE_PATH), {path});
        const bool started = helper.waitForStarted(10'000);
        if (!started) { helper.kill(); helper.waitForFinished(5'000); }
        QVERIFY2(started, qPrintable(helper.errorString()));
        const bool finished = helper.waitForFinished(30'000);
        if (!finished) { helper.kill(); helper.waitForFinished(5'000); }
        const auto json = helper.readAllStandardOutput();
        const auto errors = helper.readAllStandardError();
        QFile evidence(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-jpeg-dng-codec.json")));
        QVERIFY(evidence.open(QIODevice::WriteOnly)); QCOMPARE(evidence.write(json), qint64(json.size())); evidence.close();
        QFile stderrFile(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-jpeg-dng-codec.stderr.txt")));
        QVERIFY(stderrFile.open(QIODevice::WriteOnly)); QCOMPARE(stderrFile.write(errors), qint64(errors.size())); stderrFile.close();
        qInfo().noquote() << json << errors;
        QVERIFY2(finished, "JPEG codec probe timed out");
        QVERIFY2(helper.exitStatus() == QProcess::NormalExit, errors.constData());
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(json, &parse);
        QVERIFY2(parse.error == QJsonParseError::NoError && document.isObject(), json.constData());
        const auto report = document.object();
        QCOMPARE(report.value("fixtureSha256").toString(), QStringLiteral("65eb7a5050b3f87172b9a424cfa9b7652adc0c30a260b1d4b971e14c2a1fb28b"));
        const auto jpeg = report.value("jpeg").toObject();
        const auto buffer = report.value("rawBuffer").toObject();
        const auto traced = report.value("rawTracedBuffer").toObject();
        QVERIFY2(jpeg.value("complete").toBool(), json.constData());
        QVERIFY2(buffer.value("complete").toBool(), json.constData());
        QVERIFY2(traced.value("complete").toBool(), json.constData());
        QCOMPARE(buffer.value("width").toInt(), 64); QCOMPARE(buffer.value("height").toInt(), 64);
        QCOMPARE(traced.value("width").toInt(), 64); QCOMPARE(traced.value("height").toInt(), 64);
        QCOMPARE(helper.exitCode(), 0);
    }
    void jpegDngWorkerContract()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const auto path = QDir(temporary.path()).filePath(QStringLiteral("中文 worker generated-jpeg.dng"));
        QVERIFY(QFile::copy(QStringLiteral(":/images/generated-jpeg.dng"), path));
        QProcess worker;
        worker.setWorkingDirectory(temporary.path());
        worker.start(QString::fromUtf8(QCOPILOTS_SEARCH_TEST_IMAGE_WORKER_PATH),
            {QStringLiteral("--mode"), QStringLiteral("extract"), QStringLiteral("--profile-dir"),
             QDir(temporary.path()).filePath(QStringLiteral("独立 worker profile")), QStringLiteral("--model-dir"),
             QDir(temporary.path()).filePath(QStringLiteral("unused models")), QStringLiteral("--threads"), QStringLiteral("1")});
        const bool started = worker.waitForStarted(10'000);
        if (!started) { worker.kill(); worker.waitForFinished(5'000); }
        QVERIFY2(started, qPrintable(worker.errorString()));
        const QJsonObject request{{"id", "jpeg-dng"}, {"operation", "extract"}, {"path", path},
            {"embed", false}, {"enableOcr", false}};
        const auto frame = QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n';
        const auto accepted = worker.write(frame);
        // Closing stdin drains this single real extraction before worker exit.
        worker.closeWriteChannel();
        const bool finished = worker.waitForFinished(30'000);
        if (!finished) { worker.kill(); worker.waitForFinished(5'000); }
        const auto json = worker.readAllStandardOutput();
        const auto errors = worker.readAllStandardError();
        QFile evidence(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-jpeg-dng-worker.json")));
        QVERIFY(evidence.open(QIODevice::WriteOnly)); QCOMPARE(evidence.write(json), qint64(json.size())); evidence.close();
        QFile stderrFile(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-jpeg-dng-worker.stderr.txt")));
        QVERIFY(stderrFile.open(QIODevice::WriteOnly)); QCOMPARE(stderrFile.write(errors), qint64(errors.size())); stderrFile.close();
        qInfo().noquote() << json << errors;
        QCOMPARE(accepted, qint64(frame.size()));
        QVERIFY2(finished, "DNG extraction worker timed out");
        QVERIFY2(worker.exitStatus() == QProcess::NormalExit, errors.constData());
        QCOMPARE(worker.exitCode(), 0);
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(json, &parse);
        QVERIFY2(parse.error == QJsonParseError::NoError && document.isObject(), json.constData());
        const auto reply = document.object();
        QCOMPARE(reply.value("id").toString(), QStringLiteral("jpeg-dng"));
        QVERIFY2(reply.value("ok").toBool(), json.constData());
        const auto result = reply.value("result").toObject();
        QVERIFY2(result.value("error").toString().isEmpty(), json.constData());
        QCOMPARE(result.value("state").toString(), QStringLiteral("complete"));
        QVERIFY(result.value("complete").toBool()); QVERIFY(!result.value("embedded").toBool());
        QCOMPARE(result.value("next").toInt(), 1);
        QCOMPARE(result.value("sourceVersion").toString(), Extractor::sourceVersion(path));
        const auto segments = result.value("segments").toArray(); QCOMPARE(segments.size(), 1);
        const auto segment = segments.first().toObject();
        QCOMPARE(segment.value("path").toString(), path);
        QCOMPARE(segment.value("kind").toString(), QStringLiteral("image"));
        QCOMPARE(segment.value("modality").toString(), QStringLiteral("image"));
        QCOMPARE(segment.value("imageBudget").toInt(), 140);
        QVERIFY(segment.value("embedding").toArray().isEmpty());
        // Worker deliberately removes its transient media before replying, so
        // 64 x 64 pixel dimensions remain asserted by realCameraFormats.
        QVERIFY(segment.value("mediaPath").toString().isEmpty());
    }
    void realCameraFormats()
    {
        QFETCH(QString, name); QFETCH(QSize, size);
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("中文 camera ") + name);
        QVERIFY(QFile::copy(QStringLiteral(":/images/") + name, path));
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        const auto result = Extractor::extract(path, options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QCOMPARE(result.state, QStringLiteral("complete"));
        QCOMPARE(result.segments.size(), 1); QCOMPARE(result.segments.first().modality, QStringLiteral("image"));
        const QImage prepared(result.segments.first().mediaPath); QVERIFY(!prepared.isNull()); QCOMPARE(prepared.size(), size);
        QString error; const auto preview = loadPreviewImage(path, 512, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error)); QCOMPARE(preview.size(), size);
        QVERIFY(preview.pixelColor(1, 1) != preview.pixelColor(preview.width() - 2, preview.height() - 2));
    }
    void imageDecodeFitsSmallWorkerStack()
    {
        // Isolate a native stack overflow so it is reported as a failing case,
        // not a crash that erases the remainder of this extraction suite.
        if (qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_IMAGE_STACK_CHILD") != 1) {
            QProcess child;
            auto environment = QProcessEnvironment::systemEnvironment();
            environment.insert(QStringLiteral("QCOPILOTS_SEARCH_IMAGE_STACK_CHILD"), QStringLiteral("1"));
            child.setProcessEnvironment(environment);
            child.start(QCoreApplication::applicationFilePath(), {QStringLiteral("imageDecodeFitsSmallWorkerStack"),
                QStringLiteral("-o"), QStringLiteral("-,txt")});
            QVERIFY2(child.waitForStarted(), qPrintable(child.errorString()));
            if (!child.waitForFinished(30'000)) { child.kill(); child.waitForFinished(); QFAIL("Small-stack decode child timed out"); }
            const QByteArray output = child.readAllStandardOutput() + child.readAllStandardError();
            QVERIFY2(child.exitStatus() == QProcess::NormalExit, output.constData());
            QVERIFY2(child.exitCode() == 0, output.constData());
            return;
        }
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        QStringList paths;
        for (const QString &name : {QStringLiteral("qcopilots-search-ocr-simplified-40px.png"),
                QStringLiteral("generated-uncompressed.dng"), QStringLiteral("generated-deflate.dng"),
                QStringLiteral("generated-jpeg.dng")}) {
            const auto path = QDir(temporary.path()).filePath(name);
            QVERIFY(QFile::copy(QStringLiteral(":/images/") + name, path)); paths.append(path);
        }
        QString failure;
        int decoded = 0;
        std::unique_ptr<QThread> thread(QThread::create([&] {
            for (const auto &path : paths) {
                QString error;
                if (loadPreviewImage(path, 512, &error).isNull() || !error.isEmpty()) {
                    failure = QStringLiteral("%1: %2").arg(path, error); return;
                }
                ++decoded;
            }
        }));
        thread->setStackSize(256 * 1024); thread->start();
        QVERIFY(thread->wait(20'000)); QVERIFY2(failure.isEmpty(), qPrintable(failure));
        QCOMPARE(decoded, paths.size());
    }
    void invalidImageAndTemp()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("bad.heic"));
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("invalid heif"); file.close();
        ExtractionOptions options; options.enableOcr = false;
        auto result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("failed")); QVERIFY(!result.error.isEmpty());
        options.tempDir = temporary.path();
        result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("failed")); QVERIFY(result.segments.isEmpty());
        const QString blocker = QDir(temporary.path()).filePath(QStringLiteral("file instead of directory"));
        file.setFileName(blocker); QVERIFY(file.open(QIODevice::WriteOnly)); file.close();
        options.tempDir = blocker;
        result = Extractor::extract(path, options); QCOMPARE(result.state, QStringLiteral("failed"));
    }
    void cancelBeforeDecode()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("cancel.png"));
        QImage source(8,8,QImage::Format_RGB32); source.fill(Qt::white); QVERIFY(source.save(path));
        ExtractionOptions options; options.cancelled = [] { return true; };
        const auto result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("cancelled")); QVERIFY(result.segments.isEmpty()); QVERIFY(!result.complete);
    }
    void changedSourceCannotCommit()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("source changes.png"));
        QImage source(256,256,QImage::Format_RGB32); source.fill(Qt::white); QVERIFY(source.save(path));
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        int checks = 0;
        options.cancelled = [&] {
            if (++checks == 2) { QFile file(path); if (file.open(QIODevice::Append)) file.write("changed after decode"); }
            return false;
        };
        const auto result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("failed")); QVERIFY(result.error.contains(QStringLiteral("changed")));
        QVERIFY(result.segments.isEmpty()); QCOMPARE(result.next, 0);
    }
    void pdfPaginationAndPosition()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("中文 multipage.pdf"));
        {
            QPdfWriter writer(path); writer.setTitle(QStringLiteral("Page positioning"));
            QPainter painter(&writer); QVERIFY(painter.isActive());
            for (int page = 1; page <= 3; ++page) {
                if (page > 1) QVERIFY(writer.newPage());
                painter.drawText(QRect(100,100,5000,5000), Qt::TextWordWrap,
                    QStringLiteral("Unique page %1. Portable local document extraction. ").arg(page).repeated(20));
            }
        }
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media"); options.maxSegments = 1;
        auto result = Extractor::extract(path, options);
        QCOMPARE(result.state, QStringLiteral("partial")); QCOMPARE(result.next, 1); QCOMPARE(result.segments.size(), 1);
        QCOMPARE(result.segments.first().page, 1); QVERIFY(result.segments.first().text.contains(QStringLiteral("Unique page 1")));
        options.start = result.next; options.maxSegments = 2;
        result = Extractor::extract(path, options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QVERIFY(result.complete); QCOMPARE(result.segments.size(), 2);
        QCOMPARE(result.segments.first().page, 2); QCOMPARE(result.segments.last().page, 3); QCOMPARE(result.next, 3);
    }
    void scannedPdfRendersImage()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("scan.pdf"));
        { QPdfWriter writer(path); QPainter painter(&writer); painter.fillRect(QRect(100,100,1000,1000), Qt::blue); }
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        const auto result = Extractor::extract(path, options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QCOMPARE(result.segments.size(), 1);
        QCOMPARE(result.segments.first().modality, QStringLiteral("image"));
        QVERIFY(QFileInfo::exists(result.segments.first().mediaPath)); QCOMPARE(result.segments.first().page, 1);
    }
    void shortNativePdfTextUsesTextAndKeepsPageCursor()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("短文本 three pages.pdf"));
        const QStringList paragraphs{
            QStringLiteral("A volunteer repairs a wooden chair by tightening loose joints in the community workshop."),
            QStringLiteral("The astronomy club observes Jupiter and Saturn through a telescope on a clear night."),
            QStringLiteral("社区维修活动修复木椅。")};
        {
            QPdfWriter writer(path); writer.setTitle(QStringLiteral("Short native paragraphs"));
            QPainter painter(&writer); QVERIFY(painter.isActive());
            QFont font; font.setPointSize(16); painter.setFont(font);
            for (int page = 0; page < paragraphs.size(); ++page) {
                if (page) QVERIFY(writer.newPage());
                painter.drawText(QRect(200, 200, 6000, 2500), Qt::TextWordWrap, paragraphs[page]);
            }
        }
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        options.maxSegments = 1;
        for (int page = 0; page < paragraphs.size(); ++page) {
            const auto result = Extractor::extract(path, options);
            QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QCOMPARE(result.segments.size(), 1);
            QCOMPARE(result.next, page + 1); QCOMPARE(result.complete, page == paragraphs.size() - 1);
            const auto &segment = result.segments.first();
            QCOMPARE(segment.page, page + 1); QCOMPARE(segment.modality, QStringLiteral("text"));
            QVERIFY(segment.text.size() < 200); QVERIFY(!segment.text.isEmpty()); QVERIFY(segment.mediaPath.isEmpty());
            QVERIFY2(segment.text.simplified().contains(paragraphs[page]), qPrintable(segment.text));
            options.start = result.next;
        }
        const auto finished = Extractor::extract(path, options);
        QVERIFY(finished.complete); QVERIFY(finished.segments.isEmpty()); QCOMPARE(finished.next, 3);
    }
    void transparentPdfRenderIsFlattenedOnWhite()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString path = QDir(temporary.path()).filePath(QStringLiteral("transparent paper.pdf"));
        {
            QPdfWriter writer(path); QPainter painter(&writer); QVERIFY(painter.isActive());
            painter.fillRect(QRect(800, 800, 1800, 1800), Qt::black);
            painter.setPen(Qt::black); painter.drawText(QPoint(300, 400), QStringLiteral("7"));
        }
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        const auto result = Extractor::extract(path, options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QCOMPARE(result.segments.size(), 1);
        const auto &segment = result.segments.first(); QCOMPARE(segment.modality, QStringLiteral("image"));
        const QImage prepared(segment.mediaPath); QVERIFY(!prepared.isNull()); QVERIFY(!prepared.hasAlphaChannel());
        QCOMPARE(prepared.pixelColor(0, 0), QColor(Qt::white));
        QCOMPARE(prepared.pixelColor(prepared.width() - 1, prepared.height() - 1), QColor(Qt::white));
        qint64 white = 0, dark = 0;
        for (int y = 0; y < prepared.height(); ++y) for (int x = 0; x < prepared.width(); ++x) {
            const QColor pixel = prepared.pixelColor(x, y);
            if (pixel.red() > 240 && pixel.green() > 240 && pixel.blue() > 240) ++white;
            if (pixel.red() < 20 && pixel.green() < 20 && pixel.blue() < 20) ++dark;
        }
        QVERIFY(white > qint64(prepared.width()) * prepared.height() / 2); QVERIFY(dark > 1000);
        QCOMPARE(result.next, 1); QVERIFY(result.complete);
    }
    void nativePdfCompatibilityCharactersAreNormalized()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const auto path = QDir(temporary.path()).filePath(QStringLiteral("font unicode compatibility.pdf"));
        // An explicit PDF ToUnicode map makes this input independent of installed
        // fonts. The visible base-font glyph maps to the same Kangxi radical
        // observed in macOS PDFs, while another maps to a Latin ligature.
        const QByteArray stream("BT /F1 18 Tf 50 750 Td (A wooden chair is repaired in the community workshop: XY) Tj ET\n");
        const QByteArray map("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Compatibility def /CMapType 2 def\n1 begincodespacerange <00> <FF> endcodespacerange\n"
            "2 beginbfchar <58> <2F4A> <59> <FB01> endbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end\n");
        const QList<QByteArray> objects{
            "<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
            "<< /Length " + QByteArray::number(stream.size()) + " >>\nstream\n" + stream + "endstream",
            "<< /Length " + QByteArray::number(map.size()) + " >>\nstream\n" + map + "endstream"};
        QByteArray pdf("%PDF-1.4\n"); QList<qsizetype> offsets;
        for (qsizetype index = 0; index < objects.size(); ++index) {
            offsets.append(pdf.size()); pdf += QByteArray::number(index + 1) + " 0 obj\n" + objects[index] + "\nendobj\n";
        }
        const auto xref = pdf.size(); pdf += "xref\n0 7\n0000000000 65535 f \n";
        for (const auto offset : offsets) pdf += QByteArray::number(offset).rightJustified(10, '0') + " 00000 n \n";
        pdf += "trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n" + QByteArray::number(xref) + "\n%%EOF\n";
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly)); QCOMPARE(file.write(pdf), pdf.size()); file.close();
        QPdfDocument raw; QCOMPARE(raw.load(path), QPdfDocument::Error::None);
        const auto nativeText = raw.getAllText(0).text();
        // PDFium may itself expand the Latin ligature, but the compatibility
        // CJK radical must reach the production boundary unchanged in this test.
        QVERIFY2(nativeText.contains(QChar(0x2f4a)), qPrintable(nativeText));
        ExtractionOptions options; options.enableOcr = false; options.tempDir = QDir(temporary.path()).filePath("media");
        const auto result = Extractor::extract(path, options);
        QVERIFY2(result.error.isEmpty(), qPrintable(result.error)); QCOMPARE(result.segments.size(), 1);
        const auto &segment = result.segments.first(); QCOMPARE(segment.modality, QStringLiteral("text"));
        QCOMPARE(segment.page, 1); QVERIFY(segment.mediaPath.isEmpty());
        QVERIFY2(segment.text.contains(QStringLiteral("木fi")), qPrintable(segment.text));
        QVERIFY(!segment.text.contains(QChar(0x2f4a))); QVERIFY(!segment.text.contains(QChar(0xfb01)));
    }
    void missingOcrIsExplicitFailure()
    {
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        ExtractionOptions options; options.tessdataDir = QDir(temporary.path()).filePath("missing");
        QImage image(100,100,QImage::Format_RGB32); image.fill(Qt::white); QString error;
        QVERIFY(recognizeText(image, options, &error).isEmpty()); QVERIFY(error.contains(QStringLiteral("language data missing")));
    }
    void realOcrWithUnicodeDataDirectory()
    {
        const QString sourceData = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
        QVERIFY2(!sourceData.isEmpty(), "QCOPILOTS_SEARCH_TESSDATA_DIR must name the pinned OCR data directory");
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        const QString data = QDir(temporary.path()).filePath(QStringLiteral("中文 OCR data"));
        QVERIFY(QDir().mkpath(data));
        for (const QString &language : {QStringLiteral("eng"), QStringLiteral("chi_sim"), QStringLiteral("chi_tra")})
            QVERIFY2(QFile::copy(QDir(sourceData).filePath(language + ".traineddata"), QDir(data).filePath(language + ".traineddata")), qPrintable(language));
        QImage image(1800, 300, QImage::Format_RGB32); image.fill(Qt::white);
        { QPainter painter(&image); QFont font(QStringLiteral("Arial")); font.setPixelSize(64); painter.setFont(font);
          painter.setPen(Qt::black); painter.drawText(60, 150, QStringLiteral("Portable search 4815162342")); }
        ExtractionOptions options; options.tessdataDir = data; QString error;
        const QString text = recognizeText(image, options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error)); QVERIFY2(text.contains(QStringLiteral("4815162342")), qPrintable(text));
    }
    void realChineseOcr_data()
    {
        QTest::addColumn<QString>("fixture");
        QTest::addColumn<QString>("expected");
        QTest::addColumn<QByteArray>("sha256");
        QTest::newRow("simplified-40px") << QStringLiteral("qcopilots-search-ocr-simplified-40px.png") << QStringLiteral("安全出口禁止吸烟")
            << QByteArray("ae4b3d8ee1005c86284094fb4ff35e819b1e0b56dc19954b67d6e36e25bfb020");
        QTest::newRow("traditional-40px") << QStringLiteral("qcopilots-search-ocr-traditional-40px.png") << QStringLiteral("社區會議歡迎參加")
            << QByteArray("d27525c909c530515762aa4d2261c7b4aef7aeb8c43205151a2a27bb1e4e73b4");
        QTest::newRow("simplified-80px") << QStringLiteral("qcopilots-search-ocr-simplified-80px.png") << QStringLiteral("安全出口禁止吸烟")
            << QByteArray("ded33620494a47bb0222bf0400cdd5bd38bbcaf1cee9c7256fc6cf928d06b299");
        QTest::newRow("traditional-80px") << QStringLiteral("qcopilots-search-ocr-traditional-80px.png") << QStringLiteral("社區會議歡迎參加")
            << QByteArray("3fe4e737f53fd2e9766971f137b163b3d9c8d49cba4f506045b6dd475fdef25f");
    }
    void ocrKeepsBlankImagesEmptyAndHonorsCancellation()
    {
        QImage image(64, 64, QImage::Format_RGB32); image.fill(Qt::white);
        ExtractionOptions options; options.tessdataDir = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
        QVERIFY2(!options.tessdataDir.isEmpty(), "The pinned OCR data directory is required");
        QString error;
        QVERIFY(recognizeText(image, options, &error).isEmpty()); QVERIFY2(error.isEmpty(), qPrintable(error));
        bool initialCheck = true;
        options.cancelled = [&] { if (initialCheck) { initialCheck = false; return false; } return true; };
        QVERIFY(recognizeText(image, options, &error).isEmpty()); QCOMPARE(error, QStringLiteral("OCR cancelled"));
    }
    void ocrPreservesEnglishColumns()
    {
        QImage image(1800, 750, QImage::Format_RGB32); image.fill(Qt::white);
        const QStringList left{QStringLiteral("Alpha planning report"), QStringLiteral("Volunteers repair wooden chairs"),
            QStringLiteral("The workshop opens on Monday"), QStringLiteral("Tools are stored on the shelf")};
        const QStringList right{QStringLiteral("Omega delivery schedule"), QStringLiteral("Fresh apples arrive on Tuesday"),
            QStringLiteral("The market serves local families"), QStringLiteral("Drivers return before sunset")};
        { QPainter painter(&image); QFont font = QFontDatabase::systemFont(QFontDatabase::GeneralFont);
          font.setPixelSize(38); painter.setFont(font); painter.setPen(Qt::black);
          for (int line = 0; line < left.size(); ++line) {
              painter.drawText(50, 100 + 100 * line, left[line]); painter.drawText(950, 100 + 100 * line, right[line]);
          } }
        ExtractionOptions options; options.tessdataDir = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
        QVERIFY2(!options.tessdataDir.isEmpty(), "The pinned OCR data directory is required");
        QString error; const QString text = recognizeText(image, options, &error).simplified();
        QVERIFY2(error.isEmpty(), qPrintable(error));
        for (const QString &line : left + right) QVERIFY2(text.contains(line), qPrintable(text));
    }
    void realChineseOcr()
    {
        QFETCH(QString, fixture); QFETCH(QString, expected); QFETCH(QByteArray, sha256);
        qcopilots_search::test::ExtractionDirectory temporary(testTemplate()); QVERIFY(temporary.isValid());
        QFile source(QStringLiteral(":/images/") + fixture); QVERIFY(source.open(QIODevice::ReadOnly));
        const QByteArray bytes = source.readAll();
        QCOMPARE(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex(), sha256);
        const QImage image = QImage::fromData(bytes, "PNG"); QVERIFY(!image.isNull());
        QFile saved(QDir(temporary.path()).filePath(fixture)); QVERIFY(saved.open(QIODevice::WriteOnly));
        QCOMPARE(saved.write(bytes), bytes.size()); saved.close();
        ExtractionOptions options; options.tessdataDir = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
        QVERIFY2(!options.tessdataDir.isEmpty(), "The pinned English, Simplified Chinese and Traditional Chinese OCR data is required");
        QString error;
        const QString text = recognizeText(image, options, &error);
        QString normalized;
        for (const QChar character : text) if (!character.isSpace()) normalized += character;
        QFile diagnostic(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-ocr-result.json")));
        QVERIFY(diagnostic.open(QIODevice::WriteOnly));
        const QJsonObject record{{"fixture", fixture}, {"sha256", QString::fromLatin1(sha256)},
            {"expected", expected}, {"actual", text}, {"whitespaceRemoved", normalized}, {"error", error}};
        QVERIFY(diagnostic.write(QJsonDocument(record).toJson()) > 0); diagnostic.close();
        QVERIFY2(error.isEmpty(), qPrintable(error));
        qInfo().noquote() << QStringLiteral("Direct OCR fixture=%1 expected=%2 actual=%3").arg(fixture, expected, normalized);
        QVERIFY2(normalized.contains(expected), qPrintable(QStringLiteral("Direct OCR failed for %1: '%2' (%3)")
            .arg(expected, text, fixture)));
    }
    void ocrPreservesLargeMultilineColumns()
    {
        QImage image(2600, 600, QImage::Format_RGB32); image.fill(Qt::white);
        const QStringList left{QStringLiteral("Alpha planning report"), QStringLiteral("Workshop opens Monday")};
        const QStringList right{QStringLiteral("Omega delivery schedule"), QStringLiteral("Fresh apples arrive Tuesday")};
        { QPainter painter(&image); QFont font(QStringLiteral("Arial")); font.setPixelSize(80);
          painter.setFont(font); painter.setPen(Qt::black);
          for (int line = 0; line < left.size(); ++line) {
              painter.drawText(60, 150 + line * 220, left[line]); painter.drawText(1360, 150 + line * 220, right[line]);
          } }
        ExtractionOptions options; options.tessdataDir = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
        QVERIFY2(!options.tessdataDir.isEmpty(), "The pinned OCR data directory is required");
        QString error; const auto text = recognizeText(image, options, &error).simplified();
        QVERIFY2(error.isEmpty(), qPrintable(error));
        for (const auto &line : left + right) QVERIFY2(text.contains(line), qPrintable(text));
    }
private:
    static QString testTemplate()
    {
        QString root = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT");
        if (root.isEmpty()) root = QDir::tempPath();
        if (!QDir().mkpath(root)) return QDir(root).filePath("invalid/qcopilots-search-image-XXXXXX");
        return QDir(root).filePath("qcopilots-search-image-XXXXXX");
    }
};

QTEST_MAIN(ImageExtractionTests)
#include "image_tests.moc"
