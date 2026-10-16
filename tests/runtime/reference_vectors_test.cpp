// Golden inputs and test prompts adapted from DigUp, Copyright (c) 2026 Abdur Rahim.
// See fixtures/LICENSE-DigUp. These reference vectors must never be regenerated here.
#include "runtime/EmbeddingRuntime.h"
#include "runtime_test_paths.h"

#include <QCryptographicHash>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSet>
#include <QProcess>
#include <QXmlStreamReader>
#include <QtEndian>
#include <QtTest>
#include <atomic>
#include <cmath>

#ifndef QCOPILOTS_SEARCH_RUNTIME_FIXTURES
#error The build must provide QCOPILOTS_SEARCH_RUNTIME_FIXTURES as an absolute fixture directory.
#endif

using namespace qcopilots_search;

namespace {
QString fixturePath(const QString& name)
{
    return QDir(QString::fromUtf8(QCOPILOTS_SEARCH_RUNTIME_FIXTURES)).filePath(name);
}

QString vectorProblem(const QVector<float>& vector)
{
    if (vector.size() != EmbeddingRuntime::Dimension)
        return QStringLiteral("Expected %1 dimensions, received %2")
            .arg(EmbeddingRuntime::Dimension).arg(vector.size());
    double squaredNorm = 0;
    for (float value : vector) {
        if (!std::isfinite(value))
            return QStringLiteral("Vector contains a non-finite value");
        squaredNorm += double(value) * value;
    }
    if (std::abs(std::sqrt(squaredNorm) - 1.0) > 0.0001)
        return QStringLiteral("Vector is not normalized: norm=%1").arg(std::sqrt(squaredNorm), 0, 'g', 12);
    return {};
}

double cosine(const QVector<float>& a, const QVector<float>& b)
{
    if (a.size() != b.size() || a.isEmpty())
        return -1;
    double dot = 0, normA = 0, normB = 0;
    for (qsizetype i = 0; i < a.size(); ++i) {
        dot += double(a[i]) * b[i];
        normA += double(a[i]) * a[i];
        normB += double(b[i]) * b[i];
    }
    return dot / std::sqrt(normA * normB);
}
}

class ReferenceVectorsTest final : public QObject {
    Q_OBJECT
private:
    std::atomic<bool> cancellationRequested{false};
    EmbeddingRuntime runtime;
    QJsonObject reference;
    QString modelDir;

private slots:
    void cleanup()
    {
        const auto error = test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
        modelDir = qEnvironmentVariable("QCOPILOTS_SEARCH_MODEL_DIR");
        QVERIFY2(!modelDir.isEmpty(), "QCOPILOTS_SEARCH_MODEL_DIR is required. Missing real models fail this test, they do not skip it.");
        const QMap<QString, QByteArray> hashes = {
            {QStringLiteral("photo.jpg"), "243df6bca3dfce7888ba872d9a774b7d030f5f6d4c5f92bd11d0f3402c026133"},
            {QStringLiteral("screen.png"), "a8a0c831fe4746d00c95e3ab436d53e4cc9046a774fe4f0f6edb73d1a7d55f9b"},
            {QStringLiteral("speech.wav"), "bfbc029af6c0bacdbaccfa6c695d6772373453249386fa6a62a7b02a31d33e1f"},
            {QStringLiteral("vectors.json"), "0ec9336aa53fb0d5bafa074cd56122e2fd914cb73f080b4f98332872089f6dd8"},
        };
        for (auto it = hashes.cbegin(); it != hashes.cend(); ++it) {
            QFile fixture(fixturePath(it.key()));
            QVERIFY2(fixture.open(QIODevice::ReadOnly), qPrintable(fixture.errorString()));
            const auto content = fixture.readAll();
            QCOMPARE(QCryptographicHash::hash(content, QCryptographicHash::Sha256).toHex(), it.value());
            if (it.key() == QStringLiteral("vectors.json")) {
                QJsonParseError parseError;
                const auto document = QJsonDocument::fromJson(content, &parseError);
                QCOMPARE(parseError.error, QJsonParseError::NoError);
                QCOMPARE(document.object().value(QStringLiteral("vector_version")).toInt(), EmbeddingRuntime::VectorVersion);
                reference = document.object().value(QStringLiteral("vectors")).toObject();
            }
        }
        const QSet<QString> expectedNames = {QStringLiteral("query"), QStringLiteral("query-bn"),
            QStringLiteral("query-ar"), QStringLiteral("document"), QStringLiteral("document-long"),
            QStringLiteral("screen-280"), QStringLiteral("photo-140"), QStringLiteral("frame-70"), QStringLiteral("speech")};
        const auto names = reference.keys();
        QCOMPARE(QSet<QString>(names.cbegin(), names.cend()), expectedNames);
        QString error;
        runtime.setCancellationCallback([this] {
            return cancellationRequested.load(std::memory_order_relaxed);
        });
        QVERIFY2(runtime.load(modelDir, false, &error), qPrintable(error));
        QVERIFY(runtime.isLoaded());
        QVERIFY(!runtime.isTextOnly());
    }

    void vectorsMatchReference_data()
    {
        QTest::addColumn<QString>("name");
        QTest::addColumn<QString>("input");
        QTest::addColumn<int>("inputKind"); // 0: query, 1: document, 2: image, 3: audio
        QTest::addColumn<QString>("title");
        QTest::addColumn<int>("budget");
        QTest::newRow("query") << QStringLiteral("query") << QStringLiteral("payment declined error") << 0 << QString{} << 0;
        QTest::newRow("query-bn") << QStringLiteral("query-bn") << QStringLiteral("ইলিশ মাছ রান্না") << 0 << QString{} << 0;
        QTest::newRow("query-ar") << QStringLiteral("query-ar") << QStringLiteral("متى تغلق المكتبة") << 0 << QString{} << 0;
        QTest::newRow("document") << QStringLiteral("document")
            << QStringLiteral("Flight to Tokyo Haneda on November 3. Then the train to Kamakura to see the Great Buddha, and back for dinner in Shibuya.")
            << 1 << QStringLiteral("Tokyo itinerary") << 0;
        QStringList notes;
        for (int i = 1; i <= 60; ++i)
            notes.append(QStringLiteral("Note %1: the review moved to room %2 because the projector in the main hall failed again, and the team agreed to send the slides the night before.").arg(i).arg(i % 7 + 2));
        QTest::newRow("document-long") << QStringLiteral("document-long") << notes.join(QLatin1Char(' ')) << 1 << QString{} << 0;
        QTest::newRow("screen-280") << QStringLiteral("screen-280") << QStringLiteral("screen.png") << 2 << QString{} << 280;
        QTest::newRow("photo-140") << QStringLiteral("photo-140") << QStringLiteral("photo.jpg") << 2 << QString{} << 140;
        QTest::newRow("frame-70") << QStringLiteral("frame-70") << QStringLiteral("photo.jpg") << 2 << QString{} << 70;
        QTest::newRow("speech") << QStringLiteral("speech") << QStringLiteral("speech.wav") << 3 << QString{} << 0;
    }

    void vectorsMatchReference()
    {
        QFETCH(QString, name);
        QFETCH(QString, input);
        QFETCH(int, inputKind);
        QFETCH(QString, title);
        QFETCH(int, budget);
        QString error;
        QVector<float> actual;
        if (inputKind == 2)
            actual = runtime.embedImage(fixturePath(input), budget, &error);
        else if (inputKind == 3)
            actual = runtime.embedAudio(fixturePath(input), &error);
        else
            actual = runtime.embedText(input, inputKind == 1 ? PromptKind::Document : PromptKind::Query, title, &error);
        QVERIFY2(!actual.isEmpty(), qPrintable(error));
        const auto problem = vectorProblem(actual);
        QVERIFY2(problem.isEmpty(), qPrintable(problem));
        QVector<float> expected;
        for (const auto& value : reference.value(name).toArray())
            expected.append(float(value.toDouble()));
        QCOMPARE(expected.size(), actual.size());
        const double agreement = cosine(actual, expected);
        qInfo().noquote() << name << "reference cosine" << QString::number(agreement, 'g', 12);
        QVERIFY2(agreement >= 0.999, qPrintable(QStringLiteral("%1: cosine %2 is below 0.999. Do not overwrite the version-1 reference vectors.").arg(name).arg(agreement, 0, 'g', 12)));
    }

    void chineseInputsAreNormalizedAndDistinct()
    {
        QString error;
        const auto chinese = runtime.embedText(QStringLiteral("北京旅行时拍摄的长城照片"), PromptKind::Query, {}, &error);
        QVERIFY2(!chinese.isEmpty(), qPrintable(error));
        const auto chineseProblem = vectorProblem(chinese);
        QVERIFY2(chineseProblem.isEmpty(), qPrintable(chineseProblem));
        const auto unrelated = runtime.embedText(QStringLiteral("How to repair a broken database transaction"), PromptKind::Query, {}, &error);
        QVERIFY2(!unrelated.isEmpty(), qPrintable(error));
        const auto unrelatedProblem = vectorProblem(unrelated);
        QVERIFY2(unrelatedProblem.isEmpty(), qPrintable(unrelatedProblem));
        QVERIFY2(cosine(chinese, unrelated) < 0.98, "Different input must not produce a constant or placeholder vector.");
    }

    void cancellationDoesNotPoisonNextRequest()
    {
        QString error;
        cancellationRequested.store(true, std::memory_order_relaxed);
        const auto cancelled = runtime.embedText(QStringLiteral("Cancel this query"), PromptKind::Query, {}, &error);
        // Restore before assertions so even a failing assertion cannot contaminate later cases.
        cancellationRequested.store(false, std::memory_order_relaxed);
        QVERIFY(cancelled.isEmpty());
        QVERIFY2(error.contains(QStringLiteral("cancelled"), Qt::CaseInsensitive), qPrintable(error));
        error.clear();
        const auto recovered = runtime.embedText(QStringLiteral("恢复正常检索"), PromptKind::Query, {}, &error);
        QVERIFY2(!recovered.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(recovered).isEmpty(), qPrintable(vectorProblem(recovered)));
        QVERIFY(error.isEmpty());
    }

    void unicodeAndSpacedImagePathMatchesReference()
    {
        QString error;
        auto directory = test::temporaryDirectory(&error);
        QVERIFY2(directory != nullptr, qPrintable(error));
        const auto folder = directory->filePath(QStringLiteral("用户 照片/旅行"));
        QVERIFY(QDir().mkpath(folder));
        const auto copiedPath = QDir(folder).filePath(QStringLiteral("照片 café 中文.jpg"));
        QVERIFY(QFile::copy(fixturePath(QStringLiteral("photo.jpg")), copiedPath));
        const auto actual = runtime.embedImage(copiedPath, 140, &error);
        QVERIFY2(!actual.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(actual).isEmpty(), qPrintable(vectorProblem(actual)));
        QVector<float> expected;
        for (const auto& value : reference.value(QStringLiteral("photo-140")).toArray())
            expected.append(float(value.toDouble()));
        const double agreement = cosine(actual, expected);
        QVERIFY2(agreement >= 0.999, qPrintable(QStringLiteral("Unicode/spaced path changed image embedding: cosine %1").arg(agreement, 0, 'g', 12)));
    }

    void audioAfterThirtySecondsIsDiscarded()
    {
        QFile source(fixturePath(QStringLiteral("speech.wav")));
        QVERIFY2(source.open(QIODevice::ReadOnly), qPrintable(source.errorString()));
        const auto original = source.readAll();
        source.close();
        // This immutable fixture is canonical mono PCM16 at 16 kHz with a 44-byte header.
        QCOMPARE(original.first(4), QByteArray("RIFF"));
        QCOMPARE(original.mid(8, 4), QByteArray("WAVE"));
        QCOMPARE(original.mid(36, 4), QByteArray("data"));
        QCOMPARE(qFromLittleEndian<quint16>(original.constData() + 20), quint16(1));
        QCOMPARE(qFromLittleEndian<quint16>(original.constData() + 22), quint16(1));
        QCOMPARE(qFromLittleEndian<quint32>(original.constData() + 24), quint32(16000));
        QCOMPARE(qFromLittleEndian<quint16>(original.constData() + 34), quint16(16));
        const auto thirtySeconds = original.sliced(44).repeated(8).first(30 * 16000 * 2);
        QCOMPARE(thirtySeconds.size(), qsizetype(30 * 16000 * 2));
        QString error;
        auto directory = test::temporaryDirectory(&error);
        QVERIFY2(directory != nullptr, qPrintable(error));
        const auto writeWav = [&](const QString& path, const QByteArray& pcm) {
            auto header = original.first(44);
            qToLittleEndian<quint32>(quint32(pcm.size() + 36), header.data() + 4);
            qToLittleEndian<quint32>(quint32(pcm.size()), header.data() + 40);
            QFile output(path);
            return output.open(QIODevice::WriteOnly) && output.write(header) == header.size()
                && output.write(pcm) == pcm.size();
        };
        const auto exactPath = directory->filePath(QStringLiteral("30 seconds.wav"));
        const auto extendedPath = directory->filePath(QStringLiteral("34 秒 带静音尾部.wav"));
        QVERIFY(writeWav(exactPath, thirtySeconds));
        QVERIFY(writeWav(extendedPath, thirtySeconds + QByteArray(4 * 16000 * 2, '\0')));
        const auto exact = runtime.embedAudio(exactPath, &error);
        QVERIFY2(!exact.isEmpty(), qPrintable(error));
        const auto extended = runtime.embedAudio(extendedPath, &error);
        QVERIFY2(!extended.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(exact).isEmpty(), qPrintable(vectorProblem(exact)));
        QVERIFY2(vectorProblem(extended).isEmpty(), qPrintable(vectorProblem(extended)));
        QVERIFY2(cosine(exact, extended) >= 0.999999, "Audio samples after the first 30 seconds changed the embedding.");
    }

    void truncationPreservesThePrefix()
    {
        // Far beyond the full runtime's 2048-token limit. Changing only a discarded
        // suffix must not change the embedding, while replacing the prefix must.
        const auto prefix = QStringLiteral("The projector failed in the meeting room. ").repeated(900);
        QString error;
        const auto first = runtime.embedText(prefix + QStringLiteral("Tail about Tokyo."), PromptKind::Document, {}, &error);
        QVERIFY2(!first.isEmpty(), qPrintable(error));
        const auto second = runtime.embedText(prefix + QStringLiteral("Tail about quantum chemistry."), PromptKind::Document, {}, &error);
        QVERIFY2(!second.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(first).isEmpty(), qPrintable(vectorProblem(first)));
        QVERIFY2(vectorProblem(second).isEmpty(), qPrintable(vectorProblem(second)));
        QVERIFY2(cosine(first, second) >= 0.999999, "Text truncation must preserve the same token prefix and terminal EOS.");
        const auto changed = runtime.embedText(QStringLiteral("Beijing trains travel to the Great Wall. ").repeated(900), PromptKind::Document, {}, &error);
        QVERIFY2(!changed.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(changed).isEmpty(), qPrintable(vectorProblem(changed)));
        QVERIFY(cosine(first, changed) < 0.98);
    }

    void malformedAndMismatchedMediaAreRejected()
    {
        QString error;
        auto directory = test::temporaryDirectory(&error);
        QVERIFY2(directory != nullptr, qPrintable(error));
        QFile corrupt(directory->filePath(QStringLiteral("损坏 图片.png")));
        QVERIFY(corrupt.open(QIODevice::WriteOnly));
        QCOMPARE(corrupt.write("not an image or audio file"), qint64(26));
        corrupt.close();
        QVERIFY(runtime.embedImage(corrupt.fileName(), 140, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedAudio(corrupt.fileName(), &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedImage(directory->filePath(QStringLiteral("missing.png")), 140, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedImage(fixturePath(QStringLiteral("photo.jpg")), 141, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedImage(fixturePath(QStringLiteral("speech.wav")), 140, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedAudio(fixturePath(QStringLiteral("photo.jpg")), &error).isEmpty());
        QVERIFY(!error.isEmpty());
        // Failure must not poison the next request on the same worker.
        error.clear();
        const auto recovered = runtime.embedText(QStringLiteral("Recovered after malformed media"), PromptKind::Query, {}, &error);
        QVERIFY2(!recovered.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(recovered).isEmpty(), qPrintable(vectorProblem(recovered)));
    }

    void textOnlyRejectsMedia()
    {
        runtime.close();
        QVERIFY(!runtime.isLoaded());
        QString error;
        QVERIFY2(runtime.load(modelDir, true, &error), qPrintable(error));
        QVERIFY(runtime.isTextOnly());
        QVERIFY(runtime.embedImage(fixturePath(QStringLiteral("photo.jpg")), 140, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedAudio(fixturePath(QStringLiteral("speech.wav")), &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        const auto query = runtime.embedText(QStringLiteral("中文检索仍可使用"), PromptKind::Query, {}, &error);
        QVERIFY2(!query.isEmpty(), qPrintable(error));
        QVERIFY2(vectorProblem(query).isEmpty(), qPrintable(vectorProblem(query)));
    }

#ifdef Q_OS_WIN
    void relocatedInstallationLoadsCpuBackend()
    {
        QString error;
        auto directory = test::temporaryDirectory(&error);
        QVERIFY2(directory != nullptr, qPrintable(error));
        const QString installed = directory->filePath(QStringLiteral("relocated-install/bin"));
        QVERIFY(QDir().mkpath(installed));
        const QDir source(QCoreApplication::applicationDirPath());
        const QString executable = QFileInfo(QCoreApplication::applicationFilePath()).fileName();
        QVERIFY(QFile::copy(QCoreApplication::applicationFilePath(), QDir(installed).filePath(executable)));
        // Native CPU modules must come from the relocated installation. Qt itself
        // may be supplied by the test runner's SDK environment in a developer build.
        const auto libraries = source.entryList({QStringLiteral("*.dll")}, QDir::Files);
        QVERIFY(!libraries.isEmpty());
        for (const auto& name : libraries)
            QVERIFY2(QFile::copy(source.filePath(name), QDir(installed).filePath(name)), qPrintable(name));
        const QString report = directory->filePath(QStringLiteral("relocated-results.xml"));
        QProcess process;
        process.setProgram(QDir(installed).filePath(executable));
        process.setWorkingDirectory(directory->path());
        process.setArguments({QStringLiteral("vectorsMatchReference:query"),
                              QStringLiteral("vectorsMatchReference:photo-140"),
                              QStringLiteral("vectorsMatchReference:speech"),
                              // Keep QtTest's C stdio log filename relative to the
                              // explicitly selected working directory on Windows.
                              QStringLiteral("-o"), QStringLiteral("relocated-results.xml,junitxml")});
        process.start();
        QVERIFY2(process.waitForStarted(10000), qPrintable(process.errorString()));
        const bool finished = process.waitForFinished(180000);
        if (!finished) { process.kill(); process.waitForFinished(10000); }
        QVERIFY2(finished, "Relocated real-model test exceeded its deadline");
        const auto output = process.readAllStandardError() + process.readAllStandardOutput();
        QCOMPARE(process.exitStatus(), QProcess::NormalExit);
        QVERIFY2(process.exitCode() == 0, output.constData());
        QFile result(report);
        QVERIFY2(result.open(QIODevice::ReadOnly), qPrintable(result.errorString()));
        QXmlStreamReader xml(result.readAll());
        QSet<QString> observed;
        while (!xml.atEnd()) {
            if (xml.readNext() == QXmlStreamReader::StartElement && xml.name() == QStringLiteral("testcase"))
                observed.insert(xml.attributes().value(QStringLiteral("name")).toString());
        }
        QVERIFY2(!xml.hasError(), qPrintable(xml.errorString()));
        QVERIFY(observed.contains(QStringLiteral("vectorsMatchReference(query)")));
        QVERIFY(observed.contains(QStringLiteral("vectorsMatchReference(photo-140)")));
        QVERIFY(observed.contains(QStringLiteral("vectorsMatchReference(speech)")));
    }
#endif

    void cleanupTestCase()
    {
        runtime.close();
        QVERIFY(!runtime.isLoaded());
    }
};

QTEST_GUILESS_MAIN(ReferenceVectorsTest)
#include "reference_vectors_test.moc"
