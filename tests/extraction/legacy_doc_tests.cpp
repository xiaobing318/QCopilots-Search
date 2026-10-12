#include "extraction/legacy_doc_extractor.h"
#include "../../cmake/qcs-native-doc-iconv.h"
#include "../support/test_environment.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QStandardPaths>
#include <QtTest>
#include <memory>

using namespace qcopilots_search;

class LegacyDocTests final : public QObject {
    Q_OBJECT
    std::unique_ptr<test::TestTemporaryDir> temporary_;
    QString copyFixture(const QString &name) {
        const QString path = QDir(temporary_->path()).filePath(QStringLiteral("中文 空格 ") + name);
        if (!QFile::copy(QStringLiteral(":/legacy-doc/") + name, path)) return {};
        return path;
    }
private slots:
    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void init() {
        QString root = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT");
        if (root.isEmpty()) root = QDir::tempPath();
        QVERIFY2(QDir().mkpath(root), qPrintable(QStringLiteral("Cannot create test root: ") + root));
        temporary_ = std::make_unique<test::TestTemporaryDir>(QDir(root).filePath(QStringLiteral("qcs-doc-XXXXXX")));
        QVERIFY2(temporary_->isValid(), qPrintable(temporary_->errorString()));
    }
    void cleanup() {
        QString cleanupError;
        if (temporary_ && temporary_->isValid()) {
            if (QTest::currentTestFailed()
                || qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") == 1) {
                temporary_->setAutoRemove(false);
                qInfo().noquote() << "Preserved DOC test artifacts:" << temporary_->path();
            } else {
                if (!temporary_->remove(&cleanupError)) temporary_->setAutoRemove(false);
            }
        }
        temporary_.reset();
        cleanupError += test::takeCleanupErrors();
        QVERIFY2(cleanupError.isEmpty(), qPrintable(cleanupError));
    }
    void fixtureIntegrity_data() {
        QTest::addColumn<QString>("name");
        QTest::addColumn<QByteArray>("digest");
        QTest::newRow("footnote") << QStringLiteral("footnote.doc") << QByteArray("81a18ac63010789ca08c28de107658a308c758a3cfc667a6edf11ffe888d3e13");
        QTest::newRow("table") << QStringLiteral("simple-table.doc") << QByteArray("67d7fe8e0555c16516354498a5935f24eeef90beb5efbf526f93a1d6c734da43");
        QTest::newRow("unicode") << QStringLiteral("HeaderFooterUnicode.doc") << QByteArray("8bd7fc9552bccb7b71a0dc34ffd801e7d3547d1317fd75d0c35fca3572f6fa4b");
        QTest::newRow("original-Chinese") << QStringLiteral("qcs-original.doc") << QByteArray("c7a3174bd536246bff63f6db75f596cc6e5aadf80fb01c48a36878581d35be76");
    }
    void fixtureIntegrity() {
        QFETCH(QString, name);
        QFETCH(QByteArray, digest);
        QFile fixture(QStringLiteral(":/legacy-doc/") + name);
        QVERIFY(fixture.open(QIODevice::ReadOnly));
        QCOMPARE(QCryptographicHash::hash(fixture.readAll(), QCryptographicHash::Sha256).toHex(), digest);
    }
    void tableAndBody() {
        const QString path = copyFixture(QStringLiteral("simple-table.doc"));
        QVERIFY(!path.isEmpty());
        QString error;
        const QString text = legacy_doc::extract(path, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(text.contains(QStringLiteral("Word 97")));
        for (int row = 1; row <= 2; ++row)
            for (int column = 1; column <= 3; ++column)
                QVERIFY2(text.contains(QStringLiteral("Cell %1,%2").arg(row).arg(column)), qPrintable(text));
        QVERIFY(text.contains(QStringLiteral("This text is below the table.")));
        QVERIFY(QFile::rename(path, path + QStringLiteral(".released")));
    }
    void footnotesEndnotesAndComments() {
        const QString path = copyFixture(QStringLiteral("footnote.doc"));
        QVERIFY(!path.isEmpty());
        QString error;
        const QString text = legacy_doc::extract(path, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(text.contains(QStringLiteral("TestFootnote")), qPrintable(text));
        QVERIFY2(text.contains(QStringLiteral("TestEndnote")), qPrintable(text));
        QVERIFY2(text.contains(QStringLiteral("TestComment")), qPrintable(text));
    }
    void unicodeHeadersAndFooters() {
        const QString path = copyFixture(QStringLiteral("HeaderFooterUnicode.doc"));
        QVERIFY(!path.isEmpty());
        QString error;
        const QString text = legacy_doc::extract(path, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(text.contains(QStringLiteral("This is a simple header, with a € euro symbol in it.")), qPrintable(text));
        QVERIFY2(text.contains(QStringLiteral("The footer, with Molière, has Unicode in it.")), qPrintable(text));
    }
    void originalChineseAndEnglishDocument() {
        const QString path = copyFixture(QStringLiteral("qcs-original.doc"));
        QVERIFY(!path.isEmpty());
        QString error;
        const QString text = legacy_doc::extract(path, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(text.contains(QStringLiteral("The community workshop repairs a wooden chair by tightening loose joints.")), qPrintable(text));
        QVERIFY2(text.contains(QStringLiteral("中文测试：社区维修活动修复木椅。")), qPrintable(text));
    }
    void cancellationAndOutputBound() {
        const QString path = copyFixture(QStringLiteral("simple-table.doc"));
        QVERIFY(!path.isEmpty());
        QString error;
        QVERIFY(legacy_doc::extract(path, &error, 10000, [] { return true; }).isEmpty());
        QVERIFY(error.contains(QStringLiteral("cancelled")));
        QVERIFY(legacy_doc::extract(path, &error, 10).isEmpty());
        QVERIFY2(error.contains(QStringLiteral("size limit")), qPrintable(error));
        int polls = 0;
        QVERIFY(legacy_doc::extract(path, &error, 10000, [&] { return ++polls > 3; }).isEmpty());
        QVERIFY(error.contains(QStringLiteral("cancelled")));
        QVERIFY(QFile::rename(path, path + QStringLiteral(".released")));
    }
    void malformedAndMissing() {
        QString error;
        const QString path = QDir(temporary_->path()).filePath(QStringLiteral("missing.doc"));
        QVERIFY(legacy_doc::extract(path, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly));
        QCOMPARE(file.write(QByteArray(512, '\0')), 512);
        file.close();
        QVERIFY(legacy_doc::extract(path, &error).isEmpty());
        QVERIFY(error.contains(QStringLiteral("signature")));
    }
    void legacyCodePages_data() {
        QTest::addColumn<QByteArray>("codePage");
        QTest::addColumn<QByteArray>("encoded");
        QTest::addColumn<QByteArray>("expected");
        QTest::addColumn<QByteArray>("destination");
        QTest::newRow("simplified-Chinese") << QByteArray("CP936") << QByteArray::fromHex("d6d0cec4")
            << QByteArray::fromHex("2d4e8765") << QByteArray("UCS-2LE");
        QTest::newRow("traditional-Chinese") << QByteArray("CP950") << QByteArray::fromHex("c163c5e9")
            << QByteArray::fromHex("417ed49a") << QByteArray("UCS-2LE");
        QTest::newRow("traditional-Big5-alias") << QByteArray("Big5") << QByteArray::fromHex("c163c5e9")
            << QByteArray::fromHex("417ed49a") << QByteArray("UCS-2LE");
        QTest::newRow("traditional-lowercase-alias") << QByteArray("cp950") << QByteArray::fromHex("c163c5e9")
            << QByteArray::fromHex("417ed49a") << QByteArray("UCS-2LE");
        QTest::newRow("Korean-Windows-UHC-extension") << QByteArray("CP949") << QByteArray::fromHex("8141")
            << QByteArray::fromHex("02ac") << QByteArray("UCS-2LE");
        QTest::newRow("Japanese-Windows-NEC-extension") << QByteArray("CP932") << QByteArray::fromHex("8740")
            << QByteArray::fromHex("6024") << QByteArray("UCS-2LE");
        // Microsoft CP932.TXT: NEC, IBM, Windows fullwidth tilde, ASCII and
        // fullwidth reverse solidus. Plain Shift_JIS has different mappings.
        QTest::newRow("Japanese-Windows-extensions-and-punctuation") << QByteArray("CP932")
            << QByteArray::fromHex("8740fa4081605c815f") << QByteArray::fromHex("602470215eff5c003cff") << QByteArray("UCS-2LE");
        QTest::newRow("Japanese-lowercase-big-endian") << QByteArray("cp932")
            << QByteArray::fromHex("8740fa4081605c815f") << QByteArray::fromHex("24602170ff5e005cff3c") << QByteArray("UCS-2BE");
        QTest::newRow("Windows-Latin") << QByteArray("CP1252") << QByteArray::fromHex("e9")
            << QByteArray::fromHex("e900") << QByteArray("UCS-2LE");
        QTest::newRow("big-endian") << QByteArray("CP936") << QByteArray::fromHex("d6d0cec4")
            << QByteArray::fromHex("4e2d6587") << QByteArray("UCS-2BE");
        QTest::newRow("literal-replacement-character") << QByteArray("UTF-8") << QByteArray::fromHex("efbfbd")
            << QByteArray::fromHex("fdff") << QByteArray("UCS-2LE");
    }
    void legacyCodePages() {
        QFETCH(QByteArray, codePage);
        QFETCH(QByteArray, encoded);
        QFETCH(QByteArray, expected);
        QFETCH(QByteArray, destination);
        std::unique_ptr<qcopilots_search_wv2_converter, decltype(&iconv_close)> converter(
            iconv_open(destination.constData(), codePage.constData()), iconv_close);
        QVERIFY(converter.get() != reinterpret_cast<iconv_t>(-1));
        const char *input = encoded.constData();
        size_t inputSize = size_t(encoded.size());
        QByteArray output(expected.size(), '\0');
        char *target = output.data();
        size_t targetSize = size_t(output.size());
        QCOMPARE(iconv(converter.get(), &input, &inputSize, &target, &targetSize), size_t(0));
        QCOMPARE(inputSize, size_t(0));
        QCOMPARE(targetSize, size_t(0));
        QCOMPARE(output, expected);
    }
    void codePageErrorsPreserveInput_data() {
        QTest::addColumn<QByteArray>("codePage");
        QTest::addColumn<QByteArray>("invalid");
        QTest::newRow("GBK-truncated") << QByteArray("CP936") << QByteArray::fromHex("81");
        QTest::newRow("GBK-invalid") << QByteArray("CP936") << QByteArray::fromHex("8120");
        QTest::newRow("GBK-valid-prefix-truncated-tail") << QByteArray("CP936") << QByteArray::fromHex("d6d081");
        QTest::newRow("Big5-truncated") << QByteArray("CP950") << QByteArray::fromHex("a4");
        QTest::newRow("Big5-invalid") << QByteArray("CP950") << QByteArray::fromHex("a420");
        QTest::newRow("CP932-truncated") << QByteArray("CP932") << QByteArray::fromHex("81");
        QTest::newRow("CP932-invalid") << QByteArray("CP932") << QByteArray::fromHex("8120");
        QTest::newRow("CP932-valid-prefix-truncated-tail") << QByteArray("CP932") << QByteArray::fromHex("874081");
        QTest::newRow("UTF8-truncated") << QByteArray("UTF-8") << QByteArray::fromHex("e4b8");
        QTest::newRow("UTF8-invalid") << QByteArray("UTF-8") << QByteArray::fromHex("e428a0");
    }
    void codePageErrorsPreserveInput() {
        QFETCH(QByteArray, codePage); QFETCH(QByteArray, invalid);
        std::unique_ptr<qcopilots_search_wv2_converter, decltype(&iconv_close)> converter(
            iconv_open("UCS-2LE", codePage.constData()), iconv_close);
        QVERIFY(converter.get() != reinterpret_cast<iconv_t>(-1));
        for (int attempt = 0; attempt < 2; ++attempt) {
            const char *input = invalid.constData();
            size_t inputSize = size_t(invalid.size());
            QByteArray output(16, char(0x5a));
            char *target = output.data();
            size_t targetSize = size_t(output.size());
            QCOMPARE(iconv(converter.get(), &input, &inputSize, &target, &targetSize), size_t(-1));
            QVERIFY(errno == EINVAL || errno == EILSEQ);
            QCOMPARE(input, invalid.constData());
            QCOMPARE(inputSize, size_t(invalid.size()));
            QCOMPARE(target, output.data());
            QCOMPARE(targetSize, size_t(16));
            QCOMPARE(output, QByteArray(16, char(0x5a)));

            // Each wv2 call supplies a complete independent run. Invalid input
            // cannot leave a lead byte or an error pending for the next call.
            const QByteArray ascii("AZ"); input = ascii.constData(); inputSize = size_t(ascii.size());
            target = output.data(); targetSize = size_t(output.size());
            QCOMPARE(iconv(converter.get(), &input, &inputSize, &target, &targetSize), size_t(0));
            QCOMPARE(inputSize, size_t(0)); QCOMPARE(targetSize, size_t(12));
            QCOMPARE(output.first(4), QByteArray::fromHex("41005a00"));
        }
    }
    void codePageOutputBoundsPreserveInput_data() {
        QTest::addColumn<QByteArray>("codePage");
        QTest::addColumn<QByteArray>("encoded");
        QTest::addColumn<QByteArray>("expected");
        QTest::newRow("Chinese") << QByteArray("CP936") << QByteArray::fromHex("d6d0cec4") << QByteArray::fromHex("2d4e8765");
        QTest::newRow("Japanese-Windows") << QByteArray("CP932") << QByteArray::fromHex("8740fa4081605c815f")
            << QByteArray::fromHex("602470215eff5c003cff");
    }
    void codePageOutputBoundsPreserveInput() {
        QFETCH(QByteArray, codePage); QFETCH(QByteArray, encoded); QFETCH(QByteArray, expected);
        std::unique_ptr<qcopilots_search_wv2_converter, decltype(&iconv_close)> converter(
            iconv_open("UCS-2LE", codePage.constData()), iconv_close);
        QVERIFY(converter.get() != reinterpret_cast<iconv_t>(-1));
        const char *input = encoded.constData();
        size_t inputSize = size_t(encoded.size());
        QByteArray output(expected.size() - 1, char(0x5a));
        char *target = output.data();
        size_t targetSize = size_t(output.size());
        QCOMPARE(iconv(converter.get(), &input, &inputSize, &target, &targetSize), size_t(-1));
        QCOMPARE(errno, E2BIG);
        QCOMPARE(input, encoded.constData());
        QCOMPARE(inputSize, size_t(encoded.size()));
        QCOMPARE(target, output.data());
        QCOMPARE(targetSize, size_t(output.size()));
        QCOMPARE(output, QByteArray(expected.size() - 1, char(0x5a)));
        QByteArray sufficient(expected.size(), '\0'); target = sufficient.data(); targetSize = size_t(sufficient.size());
        QCOMPARE(iconv(converter.get(), &input, &inputSize, &target, &targetSize), size_t(0));
        QCOMPARE(inputSize, size_t(0)); QCOMPARE(targetSize, size_t(0));
        QCOMPARE(sufficient, expected);
        QCOMPARE(iconv(converter.get(), nullptr, nullptr, nullptr, nullptr), size_t(0));
    }
};

QTEST_GUILESS_MAIN(LegacyDocTests)
#include "legacy_doc_tests.moc"
