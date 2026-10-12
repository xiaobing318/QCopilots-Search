#include "extraction/extractor.h"
#include "extraction/image_extractor.h"
#include <QDir>
#include <QFile>
#include <QImage>
#include <QPainter>
#include <QPdfWriter>
#include <QStandardPaths>
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
