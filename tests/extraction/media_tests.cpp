#include "extraction/media_extractor.h"

#include <QDataStream>
#include <QDir>
#include <QFile>
#include <QImage>
#include <QStandardPaths>
#include "test_directory.h"
#include <QtTest>
#include <QtEndian>
#include <cmath>
#include <numbers>

using namespace qcopilots_search;

namespace {

QString testTemplate() {
    const QString root = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT", QDir::tempPath());
    if (!QDir().mkpath(root)) return root + QStringLiteral("/missing/qcopilots-search-media-XXXXXX");
    return QDir(root).filePath(QStringLiteral("qcopilots-search-media-XXXXXX"));
}

bool writeWave(const QString &path, int seconds, bool silent = false, int rate = 16000, int channels = 1) {
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly)) return false;
    QDataStream output(&file);
    output.setByteOrder(QDataStream::LittleEndian);
    const quint32 bytes = seconds * rate * channels * 2;
    output.writeRawData("RIFF", 4); output << quint32(bytes + 36);
    output.writeRawData("WAVEfmt ", 8); output << quint32(16) << quint16(1) << quint16(channels)
        << quint32(rate) << quint32(rate * channels * 2) << quint16(channels * 2) << quint16(16);
    output.writeRawData("data", 4); output << bytes;
    for (int i = 0; i < seconds * rate; ++i) {
        const qint16 sample = silent ? 0 : qint16(12000 * std::sin(2 * std::numbers::pi * 440.0 * i / rate));
        for (int channel = 0; channel < channels; ++channel) output << sample;
    }
    return output.status() == QDataStream::Ok;
}

QByteArray chunk(const char *id, const QByteArray &body) {
    QByteArray data;
    QDataStream output(&data, QIODevice::WriteOnly);
    output.setByteOrder(QDataStream::LittleEndian);
    output.writeRawData(id, 4); output << quint32(body.size()); output.writeRawData(body.constData(), body.size());
    if (body.size() % 2) output << quint8(0);
    return data;
}

// A self-contained uncompressed AVI fixture avoids installed codecs, network
// fixtures, external ffmpeg executables and platform-specific sample media.
bool writeVideo(const QString &path, bool oneShot = false, int frames = 90, bool soundtrack = false) {
    constexpr int width = 16, height = 16, frameBytes = width * height * 3;
    QByteArray aviHeader;
    QDataStream a(&aviHeader, QIODevice::WriteOnly); a.setByteOrder(QDataStream::LittleEndian);
    a << quint32(100000) << quint32(frameBytes * 10) << quint32(0) << quint32(0)
      << quint32(frames) << quint32(0) << quint32(soundtrack ? 2 : 1) << quint32(frameBytes) << quint32(width) << quint32(height);
    for (int i = 0; i < 4; ++i) a << quint32(0);
    QByteArray streamHeader;
    QDataStream s(&streamHeader, QIODevice::WriteOnly); s.setByteOrder(QDataStream::LittleEndian);
    s.writeRawData("vidsDIB ", 8); s << quint32(0) << quint16(0) << quint16(0) << quint32(0)
      << quint32(1) << quint32(10) << quint32(0) << quint32(frames) << quint32(frameBytes)
      << quint32(0xffffffffu) << quint32(0) << qint16(0) << qint16(0) << qint16(width) << qint16(height);
    QByteArray bitmapHeader;
    QDataStream b(&bitmapHeader, QIODevice::WriteOnly); b.setByteOrder(QDataStream::LittleEndian);
    b << quint32(40) << qint32(width) << qint32(height) << quint16(1) << quint16(24) << quint32(0)
      << quint32(frameBytes) << qint32(0) << qint32(0) << quint32(0) << quint32(0);
    QByteArray headers = "hdrl" + chunk("avih", aviHeader) + chunk("LIST", "strl" + chunk("strh", streamHeader) + chunk("strf", bitmapHeader));
    if (soundtrack) {
        QByteArray audioHeader;
        QDataStream h(&audioHeader, QIODevice::WriteOnly); h.setByteOrder(QDataStream::LittleEndian);
        h.writeRawData("auds", 4); h << quint32(0) << quint32(0) << quint16(0) << quint16(0) << quint32(0)
          << quint32(1) << quint32(16000) << quint32(0) << quint32(48000) << quint32(96000)
          << quint32(0xffffffffu) << quint32(2) << qint16(0) << qint16(0) << qint16(0) << qint16(0);
        QByteArray audioFormat;
        QDataStream f(&audioFormat, QIODevice::WriteOnly); f.setByteOrder(QDataStream::LittleEndian);
        f << quint16(1) << quint16(1) << quint32(16000) << quint32(32000) << quint16(2) << quint16(16);
        headers += chunk("LIST", "strl" + chunk("strh", audioHeader) + chunk("strf", audioFormat));
    }
    QByteArray movie("movi");
    for (int frame = 0; frame < frames; ++frame) {
        QByteArray pixels(frameBytes, Qt::Uninitialized);
        const int shot = oneShot ? 0 : frame / 30;
        for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) {
            const bool white = shot == 0 ? x < 8 : shot == 1 ? y < 8 : (x / 4 + y / 4) % 2 == 0;
            for (int component = 0; component < 3; ++component) pixels[(y * width + x) * 3 + component] = white ? char(255) : char(0);
        }
        movie += chunk("00db", pixels);
    }
    if (soundtrack) {
        QByteArray samples;
        QDataStream audio(&samples, QIODevice::WriteOnly); audio.setByteOrder(QDataStream::LittleEndian);
        for (int i = 0; i < 48000; ++i) audio << qint16(12000 * std::sin(2 * std::numbers::pi * 440.0 * i / 16000));
        movie += chunk("01wb", samples);
    }
    QFile file(path);
    return file.open(QIODevice::WriteOnly) && file.write(chunk("RIFF", "AVI " + chunk("LIST", headers) + chunk("LIST", movie))) > 0;
}

QByteArray contents(const QString &path) {
    QFile file(path);
    return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray{};
}

QByteArray ebml(quint32 id, const QByteArray &body) {
    QByteArray result;
    bool begun = false;
    for (int shift = 24; shift >= 0; shift -= 8) {
        const auto byte = quint8(id >> shift);
        if (byte || begun || shift == 0) { result.append(char(byte)); begun = true; }
    }
    int width = 1;
    while (quint64(body.size()) >= (quint64(1) << (7 * width)) - 1) ++width;
    const quint64 size = quint64(body.size()) | (quint64(1) << (7 * width));
    for (int shift = (width - 1) * 8; shift >= 0; shift -= 8) result.append(char(size >> shift));
    return result + body;
}

QByteArray ebmlInteger(quint32 id, quint64 value) {
    QByteArray bytes;
    do { bytes.prepend(char(value)); value >>= 8; } while (value);
    return ebml(id, bytes);
}

QByteArray ebmlDouble(quint32 id, double value) {
    QByteArray bytes;
    QDataStream stream(&bytes, QIODevice::WriteOnly); stream.setByteOrder(QDataStream::BigEndian); stream << value;
    return ebml(id, bytes);
}

bool writeDiscontinuousAudio(const QString &path) {
    const QByteArray header = ebmlInteger(0x4286, 1) + ebmlInteger(0x42f7, 1) + ebmlInteger(0x42f2, 4)
        + ebmlInteger(0x42f3, 8) + ebml(0x4282, "matroska") + ebmlInteger(0x4287, 4) + ebmlInteger(0x4285, 2);
    const QByteArray info = ebmlInteger(0x2ad7b1, 1000000) + ebmlDouble(0x4489, 10000)
        + ebml(0x4d80, "qcopilots-search-tests") + ebml(0x5741, "qcopilots-search-tests");
    const QByteArray track = ebmlInteger(0xd7, 1) + ebmlInteger(0x73c5, 1) + ebmlInteger(0x83, 2)
        + ebml(0x86, "A_PCM/INT/LIT") + ebml(0xe1, ebmlDouble(0xb5, 16000) + ebmlInteger(0x9f, 1) + ebmlInteger(0x6264, 16));
    QByteArray blocks = ebmlInteger(0xe7, 0);
    // The first second fixes the container playback origin at zero. A long gap
    // then separates two three-second regions, without allocating silent PCM.
    for (const auto [startMs, seconds] : {std::pair{0, 1}, std::pair{2000, 3}, std::pair{7000, 3}}) {
        QByteArray block;
        QDataStream b(&block, QIODevice::WriteOnly); b.setByteOrder(QDataStream::BigEndian);
        b << quint8(0x81) << qint16(startMs) << quint8(0x80);
        b.setByteOrder(QDataStream::LittleEndian);
        for (int i = 0; i < seconds * 16000; ++i) b << qint16(12000 * std::sin(2 * std::numbers::pi * 440.0 * i / 16000));
        blocks += ebml(0xa3, block);
    }
    const QByteArray data = ebml(0x1a45dfa3, header) + ebml(0x18538067,
        ebml(0x1549a966, info) + ebml(0x1654ae6b, ebml(0xae, track)) + ebml(0x1f43b675, blocks));
    QFile file(path);
    return file.open(QIODevice::WriteOnly) && file.write(data) == data.size();
}

} // namespace

class MediaTests final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() {
        QStandardPaths::setTestModeEnabled(true);
        QVERIFY2(mediaBackendError().isEmpty(), qPrintable(mediaBackendError()));
    }
    void extensions() {
        QVERIFY(supportsAudio(QStringLiteral("/samples/中文 speech.WAV")));
        QVERIFY(supportsAudio(QStringLiteral("opus")));
        QVERIFY(supportsVideo(QStringLiteral("C:/samples/recording.MP4")));
        QVERIFY(supportsVideo(QStringLiteral("m2ts")));
        QVERIFY(!supportsAudio(QStringLiteral("picture.png")));
        QVERIFY(!supportsVideo(QStringLiteral("document.txt")));
    }
    void audioWindowsAndResume() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        const QString input = root.filePath(QStringLiteral("中文 recording.wav"));
        QVERIFY(writeWave(input, 62));
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("结果 artifacts")); options.maxSegments = 1;
        const auto first = extractMedia(input, options, false);
        QCOMPARE(first.state, QStringLiteral("partial")); QCOMPARE(first.next, 1); QCOMPARE(first.segments.size(), 1);
        QCOMPARE(first.segments[0].startMs, 0); QCOMPARE(first.segments[0].endMs, 30000);
        const auto bytes = contents(first.segments[0].mediaPath);
        QCOMPARE(bytes.left(4), QByteArray("RIFF")); QCOMPARE(bytes.mid(8, 4), QByteArray("WAVE"));
        QCOMPARE(bytes.size(), 44 + 30 * 16000 * 2);
        QCOMPARE(qFromLittleEndian<quint32>(bytes.constData() + 24), quint32(16000));
        options.start = first.next; options.maxSegments = 40;
        const auto remaining = extractMedia(input, options, false);
        QVERIFY2(remaining.complete, qPrintable(remaining.error)); QCOMPARE(remaining.segments.size(), 2); QCOMPARE(remaining.next, 3);
        QCOMPARE(remaining.segments[0].startMs, 25000); QCOMPARE(remaining.segments[0].endMs, 55000);
        QCOMPARE(remaining.segments[1].startMs, 50000); QCOMPARE(remaining.segments[1].endMs, 62000);
        options.start = 0;
        const auto all = extractMedia(input, options, false);
        QCOMPARE(all.segments.size(), 3);
        QCOMPARE(contents(all.segments[1].mediaPath), contents(remaining.segments[0].mediaPath));
    }
    void silenceTailAndResample() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("output"));
        const QString silence = root.filePath(QStringLiteral("silence.wav")); QVERIFY(writeWave(silence, 35, true));
        const auto empty = extractMedia(silence, options, false);
        QVERIFY2(empty.complete, qPrintable(empty.error)); QVERIFY(empty.segments.isEmpty());
        const QString shortInput = root.filePath(QStringLiteral("short.wav")); QVERIFY(writeWave(shortInput, 1));
        QVERIFY(extractMedia(shortInput, options, false).segments.isEmpty());
        const QString stereo = root.filePath(QStringLiteral("stereo.wav")); QVERIFY(writeWave(stereo, 3, false, 44100, 2));
        const auto converted = extractMedia(stereo, options, false);
        QVERIFY2(converted.complete, qPrintable(converted.error)); QCOMPARE(converted.segments.size(), 1);
        QCOMPARE(converted.segments[0].endMs, 3000);
        QCOMPARE(contents(converted.segments[0].mediaPath).size(), 44 + 3 * 16000 * 2);
    }
    void tailDoesNotDuplicateOverlap() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("output"));
        for (const int seconds : {30, 31, 32}) {
            const QString source = root.filePath(QString::number(seconds) + QStringLiteral(".wav")); QVERIFY(writeWave(source, seconds));
            const auto result = extractMedia(source, options, false);
            QVERIFY2(result.complete, qPrintable(result.error)); QCOMPARE(result.segments.size(), seconds == 32 ? 2 : 1);
        }
    }
    void videoShotsAndPagination() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        const QString input = root.filePath(QStringLiteral("三个 shots.avi")); QVERIFY(writeVideo(input));
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("frames")); options.maxSegments = 1;
        const auto first = extractMedia(input, options, true);
        QCOMPARE(first.state, QStringLiteral("partial")); QCOMPARE(first.segments.size(), 1); QCOMPARE(first.next, 1);
        QCOMPARE(first.segments[0].kind, QStringLiteral("video")); QCOMPARE(first.segments[0].modality, QStringLiteral("image"));
        QCOMPARE(first.segments[0].startMs, 500); QCOMPARE(first.segments[0].endMs, 3500);
        QVERIFY(!QImage(first.segments[0].mediaPath).isNull());
        options.start = first.next; options.maxSegments = 40;
        const auto remaining = extractMedia(input, options, true);
        QVERIFY2(remaining.complete, qPrintable(remaining.error)); QCOMPARE(remaining.segments.size(), 2);
        QCOMPARE(remaining.segments[0].startMs, 3500); QCOMPARE(remaining.segments[0].endMs, 6500);
        QCOMPARE(remaining.segments[1].endMs, 9000);
        const QString repeated = root.filePath(QStringLiteral("same.avi")); QVERIFY(writeVideo(repeated, true));
        options.start = 0;
        const auto dedup = extractMedia(repeated, options, true);
        QVERIFY2(dedup.complete, qPrintable(dedup.error)); QCOMPARE(dedup.segments.size(), 1); QCOMPARE(dedup.segments[0].endMs, 9000);
    }
    void singleFrameVideo() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        const QString input = root.filePath(QStringLiteral("single.avi")); QVERIFY(writeVideo(input, true, 1));
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("frames"));
        const auto result = extractMedia(input, options, true);
        QVERIFY2(result.complete, qPrintable(result.error)); QCOMPARE(result.segments.size(), 1);
        QCOMPARE(result.segments[0].startMs, 0); QCOMPARE(result.segments[0].endMs, 100);
        QVERIFY(!QImage(result.segments[0].mediaPath).isNull());
    }
    void audioTimestampDiscontinuities() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        const QString input = root.filePath(QStringLiteral("gaps.mka")); QVERIFY(writeDiscontinuousAudio(input));
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("pieces"));
        const auto result = extractMedia(input, options, false);
        QVERIFY2(result.complete, qPrintable(result.error)); QCOMPARE(result.segments.size(), 2);
        QCOMPARE(result.segments[0].startMs, 2000); QCOMPARE(result.segments[0].endMs, 5000);
        QCOMPARE(result.segments[1].startMs, 7000); QCOMPARE(result.segments[1].endMs, 10000);
        QCOMPARE(contents(result.segments[0].mediaPath).size(), 44 + 3 * 16000 * 2);
    }
    void videoSoundtrackPagination() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        const QString input = root.filePath(QStringLiteral("soundtrack.avi")); QVERIFY(writeVideo(input, false, 90, true));
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("pieces")); options.maxSegments = 3;
        const auto first = extractMedia(input, options, true);
        QCOMPARE(first.state, QStringLiteral("partial")); QCOMPARE(first.next, 3); QCOMPARE(first.segments.size(), 3);
        options.start = first.next;
        const auto last = extractMedia(input, options, true);
        QVERIFY2(last.complete, qPrintable(last.error)); QCOMPARE(last.segments.size(), 1);
        QCOMPARE(last.segments[0].kind, QStringLiteral("video")); QCOMPARE(last.segments[0].modality, QStringLiteral("audio"));
        QCOMPARE(last.segments[0].startMs, 0); QCOMPARE(last.segments[0].endMs, 3000); QCOMPARE(last.next, 4);
    }
    void failuresAndCancellation() {
        qcopilots_search::test::ExtractionDirectory root(testTemplate()); QVERIFY(root.isValid());
        ExtractionOptions options; options.tempDir = root.filePath(QStringLiteral("output"));
        const QString broken = root.filePath(QStringLiteral("broken.wav"));
        QFile file(broken); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("this is not audio"); file.close();
        const auto invalid = extractMedia(broken, options, false);
        QCOMPARE(invalid.state, QStringLiteral("failed")); QVERIFY(!invalid.error.isEmpty()); QVERIFY(!invalid.complete);
        const QString source = root.filePath(QStringLiteral("valid.wav")); QVERIFY(writeWave(source, 3));
        options.cancelled = [] { return true; };
        QCOMPARE(extractMedia(source, options, false).state, QStringLiteral("cancelled"));
        options.cancelled = {}; options.start = -1;
        QCOMPARE(extractMedia(source, options, false).state, QStringLiteral("failed"));
        options.start = 0; options.tempDir = broken;
        QCOMPARE(extractMedia(source, options, false).state, QStringLiteral("failed"));
        options.tempDir = QStringLiteral("relative-output-is-forbidden");
        QCOMPARE(extractMedia(source, options, false).state, QStringLiteral("failed"));
    }
};

QTEST_GUILESS_MAIN(MediaTests)
#include "media_tests.moc"
