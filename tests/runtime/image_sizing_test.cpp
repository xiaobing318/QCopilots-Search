// Adapted from DigUp ImageSizingTests.swift, Copyright (c) 2026 Abdur Rahim.
// SPDX-License-Identifier: MIT
#include "runtime/ImageSizing.h"
#include "support/test_environment.h"

#include <QtTest>

#include <initializer_list>
#include <limits>

using qcopilots_search::ImageSizing;

namespace {
QByteArray bytes(std::initializer_list<unsigned char> values)
{
    return QByteArray(reinterpret_cast<const char*>(values.begin()),
                      static_cast<qsizetype>(values.size()));
}

const QByteArray source = bytes({
    0,115,230,3,118,233,6,121,236,9,124,239,12,127,242,15,130,245,18,133,248,
    77,192,51,80,195,54,83,198,57,86,201,60,89,204,63,92,207,66,95,210,69,
    154,13,128,157,16,131,160,19,134,163,22,137,166,25,140,169,28,143,172,31,146,
    231,90,205,234,93,208,237,96,211,240,99,214,243,102,217,246,105,220,249,108,223,
    52,167,26,55,170,29,58,173,32,61,176,35,64,179,38,67,182,41,70,185,44
});

QByteArray resize(int width, int height)
{
    return ImageSizing::resizeBicubic(reinterpret_cast<const unsigned char*>(source.constData()),
                                     7, 5, width, height);
}
} // namespace

class ImageSizingTest : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void targetSizeFillsBudget_data()
    {
        QTest::addColumn<int>("height");
        QTest::addColumn<int>("width");
        QTest::addColumn<int>("budget");
        QTest::addColumn<QSize>("expected");
        QTest::newRow("screenshot-280") << 900 << 1440 << 280 << QSize(1008, 624);
        QTest::newRow("screenshot-140") << 900 << 1440 << 140 << QSize(672, 432);
        QTest::newRow("screenshot-70") << 900 << 1440 << 70 << QSize(480, 288);
        QTest::newRow("photo-280") << 768 << 768 << 280 << QSize(768, 768);
        QTest::newRow("photo-140") << 768 << 768 << 140 << QSize(528, 528);
        QTest::newRow("small-photo-upscale") << 300 << 300 << 140 << QSize(528, 528);
        QTest::newRow("thin-horizontal") << 10 << 4000 << 140 << QSize(6720, 48);
        QTest::newRow("thin-vertical") << 4000 << 10 << 140 << QSize(48, 6720);
        QTest::newRow("single-pixel") << 1 << 1 << 1 << QSize(48, 48);
    }

    void targetSizeFillsBudget()
    {
        QFETCH(int, height);
        QFETCH(int, width);
        QFETCH(int, budget);
        QFETCH(QSize, expected);
        const auto actual = ImageSizing::targetSize(height, width, budget);
        QVERIFY(actual.has_value());
        QCOMPARE(*actual, expected);
    }

    void invalidTargetSize()
    {
        QVERIFY(!ImageSizing::targetSize(0, 100, 140));
        QVERIFY(!ImageSizing::targetSize(100, -1, 140));
        QVERIFY(!ImageSizing::targetSize(100, 100, 0));
        QVERIFY(!ImageSizing::targetSize(100, 100, -1));
        QVERIFY(!ImageSizing::targetSize(1, std::numeric_limits<int>::max(),
                                         std::numeric_limits<int>::max()));
    }

    void qwenBudgetMapping()
    {
        QCOMPARE(ImageSizing::qwenTokenBudget(70), 160);
        QCOMPARE(ImageSizing::qwenTokenBudget(140), 320);
        QCOMPARE(ImageSizing::qwenTokenBudget(280), 640);
        QCOMPARE(ImageSizing::qwenTokenBudget(560), 1280);
        QCOMPARE(ImageSizing::qwenTokenBudget(1120), 1280);
        for (int budget : {-1, 0, 1, 160, 320, 1280, std::numeric_limits<int>::max()})
            QCOMPARE(ImageSizing::qwenTokenBudget(budget), 0);
    }

    void qwenTargetSizeMatchesSmartResize_data()
    {
        QTest::addColumn<int>("height");
        QTest::addColumn<int>("width");
        QTest::addColumn<int>("budget");
        QTest::addColumn<QSize>("expected");
        QTest::newRow("landscape-70") << 900 << 1440 << 70 << QSize(512, 320);
        QTest::newRow("landscape-140") << 900 << 1440 << 140 << QSize(704, 448);
        QTest::newRow("landscape-280") << 900 << 1440 << 280 << QSize(1024, 640);
        QTest::newRow("landscape-560") << 900 << 1440 << 560 << QSize(1440, 896);
        QTest::newRow("landscape-1120") << 900 << 1440 << 1120 << QSize(1440, 896);
        QTest::newRow("portrait") << 1440 << 900 << 140 << QSize(448, 704);
        // Qwen retains a small image near its original size instead of filling the budget.
        QTest::newRow("small-photo") << 300 << 300 << 140 << QSize(288, 288);
        QTest::newRow("single-pixel") << 1 << 1 << 70 << QSize(64, 64);
        QTest::newRow("minimum-upscale") << 16 << 32 << 70 << QSize(96, 64);
        // Python round() resolves both even and odd half-grid cases to an even grid count.
        QTest::newRow("half-grid-round-down") << 80 << 144 << 70 << QSize(128, 64);
        QTest::newRow("half-grid-round-up") << 112 << 176 << 70 << QSize(192, 128);
        QTest::newRow("maximum-aspect-ratio") << 32 << 6400 << 280 << QSize(6400, 32);
        QTest::newRow("large-input-no-overflow") << std::numeric_limits<int>::max()
            << std::numeric_limits<int>::max() << 1120 << QSize(1120, 1120);
    }

    void qwenTargetSizeMatchesSmartResize()
    {
        QFETCH(int, height);
        QFETCH(int, width);
        QFETCH(int, budget);
        QFETCH(QSize, expected);
        const auto actual = ImageSizing::qwenTargetSize(height, width, budget);
        QVERIFY(actual.has_value());
        QCOMPARE(*actual, expected);
        QCOMPARE(actual->width() % 32, 0);
        QCOMPARE(actual->height() % 32, 0);
        const qint64 pixels = qint64(actual->width()) * actual->height();
        QVERIFY(pixels >= 4096);
        QVERIFY(pixels <= qint64(ImageSizing::qwenTokenBudget(budget)) * 1024);
    }

    void invalidQwenTargetSize()
    {
        QVERIFY(!ImageSizing::qwenTargetSize(0, 100, 140));
        QVERIFY(!ImageSizing::qwenTargetSize(100, 0, 140));
        QVERIFY(!ImageSizing::qwenTargetSize(-1, 100, 140));
        QVERIFY(!ImageSizing::qwenTargetSize(100, -1, 140));
        for (int budget : {-1, 0, 1, 160, 1280, std::numeric_limits<int>::max()})
            QVERIFY(!ImageSizing::qwenTargetSize(100, 100, budget));
        QVERIFY(!ImageSizing::qwenTargetSize(32, 6401, 280));
        QVERIFY(!ImageSizing::qwenTargetSize(6401, 32, 280));
        QVERIFY(!ImageSizing::qwenTargetSize(1, std::numeric_limits<int>::max(), 1120));
        // A valid aspect ratio can still collapse the short side at a small token budget.
        QVERIFY(!ImageSizing::qwenTargetSize(32, 6400, 70));
    }

    void shrinkMatchesPillow()
    {
        const auto expected = bytes({28,151,152,33,156,157,39,162,163,44,167,168,
            165,68,129,170,73,134,176,79,140,181,84,145,132,133,106,137,138,111,
            143,144,117,148,149,122});
        QCOMPARE(resize(4, 3), expected);
    }

    void growMatchesPillow()
    {
        const auto expected = bytes({0,112,237,0,113,238,1,116,241,3,118,243,5,120,245,
            7,122,247,9,124,249,11,126,251,14,129,254,15,130,255,55,187,85,56,188,86,
            59,191,89,61,193,91,63,195,93,65,197,95,67,199,97,69,201,99,72,204,102,
            73,205,103,122,82,83,123,83,84,126,86,87,128,88,89,130,90,91,132,92,93,
            134,94,95,136,96,97,139,99,100,140,100,101,199,27,173,200,28,174,203,31,177,
            205,33,179,207,35,181,209,37,183,211,39,185,213,41,187,216,44,190,217,45,191,
            197,112,171,198,113,172,201,116,175,203,118,177,205,120,179,207,122,181,
            209,124,183,211,126,185,214,129,188,215,130,189,45,170,19,46,171,20,49,174,23,
            51,176,25,53,178,27,55,180,29,57,182,31,59,184,33,62,187,36,63,188,37});
        QCOMPARE(resize(10, 6), expected);
    }

    void verticalOnlyMatchesPillow()
    {
        const auto expected = bytes({74,120,137,77,123,140,80,126,143,83,129,146,
            86,132,149,89,135,152,92,138,155,153,100,119,156,103,122,159,106,125,
            162,109,128,165,112,131,168,115,134,171,118,137});
        QCOMPARE(resize(7, 2), expected);
    }

    void horizontalOnlyMatchesPillow()
    {
        const auto expected = bytes({1,116,231,6,121,236,12,127,242,17,132,247,
            78,193,52,83,198,57,89,204,63,94,209,68,
            155,14,129,160,19,134,166,25,140,171,30,145,
            232,91,206,237,96,211,243,102,217,248,107,222,
            53,168,27,58,173,32,64,179,38,69,184,43});
        QCOMPARE(resize(4, 5), expected);
    }

    void noResizeCopiesPixels()
    {
        auto copy = resize(7, 5);
        QCOMPARE(copy, source);
        copy[0] = 42;
        QCOMPARE(static_cast<unsigned char>(source[0]), static_cast<unsigned char>(0));
    }

    void constantImageAndSinglePixel()
    {
        const auto pixel = bytes({0, 128, 255});
        const auto output = ImageSizing::resizeBicubic(
            reinterpret_cast<const unsigned char*>(pixel.constData()), 1, 1, 19, 11);
        QCOMPARE(output, pixel.repeated(19 * 11));
        const auto solid = pixel.repeated(19 * 11);
        QCOMPARE(ImageSizing::resizeBicubic(
            reinterpret_cast<const unsigned char*>(solid.constData()), 19, 11, 1, 1), pixel);
    }

    void invalidResize()
    {
        const auto* data = reinterpret_cast<const unsigned char*>(source.constData());
        QVERIFY(ImageSizing::resizeBicubic(nullptr, 7, 5, 4, 3).isEmpty());
        QVERIFY(ImageSizing::resizeBicubic(data, 0, 5, 4, 3).isEmpty());
        QVERIFY(ImageSizing::resizeBicubic(data, 7, -5, 4, 3).isEmpty());
        QVERIFY(ImageSizing::resizeBicubic(data, 7, 5, 0, 3).isEmpty());
        QVERIFY(ImageSizing::resizeBicubic(data, 7, 5, 4, -3).isEmpty());
        QVERIFY(ImageSizing::resizeBicubic(data, std::numeric_limits<int>::max(),
            std::numeric_limits<int>::max(), 4, 3).isEmpty());
    }
};

QTEST_APPLESS_MAIN(ImageSizingTest)
#include "image_sizing_test.moc"
