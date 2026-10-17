#include "runtime/EmbeddingRuntime.h"
#include "runtime_test_paths.h"

#include <QFile>
#include <QFileInfo>
#include <QtTest>

using namespace qcopilots_search;

class RuntimeUnitTest final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
    }

    void cleanup()
    {
        const auto error = test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void promptContracts_data()
    {
        QTest::addColumn<QString>("text");
        QTest::addColumn<int>("kind");
        QTest::addColumn<QString>("title");
        QTest::addColumn<QString>("expected");
        QTest::newRow("search") << QStringLiteral("payment declined error")
            << int(PromptKind::Query) << QString{}
            << QStringLiteral("task: search result | query: payment declined error");
        QTest::newRow("code") << QStringLiteral("retry with backoff")
            << int(PromptKind::CodeQuery) << QString{}
            << QStringLiteral("task: code retrieval | query: retry with backoff");
        QTest::newRow("titled-document") << QStringLiteral("Train to Kamakura")
            << int(PromptKind::Document) << QStringLiteral("Tokyo itinerary")
            << QStringLiteral("title: Tokyo itinerary | text: Train to Kamakura");
        QTest::newRow("untitled-document") << QStringLiteral("A note")
            << int(PromptKind::Document) << QString{}
            << QStringLiteral("title: none | text: A note");
        QTest::newRow("raw") << QStringLiteral("<bos> do not rewrite this\n")
            << int(PromptKind::Raw) << QStringLiteral("ignored")
            << QStringLiteral("<bos> do not rewrite this\n");
        QTest::newRow("unicode") << QStringLiteral("查找北京旅行照片")
            << int(PromptKind::Query) << QString{}
            << QStringLiteral("task: search result | query: 查找北京旅行照片");
    }

    void promptContracts()
    {
        QFETCH(QString, text);
        QFETCH(int, kind);
        QFETCH(QString, title);
        QFETCH(QString, expected);
        QCOMPARE(EmbeddingRuntime::prompt(text, PromptKind(kind), title), expected);
    }

    void defaultModelIdentityBeforeLoad()
    {
        EmbeddingRuntime runtime;
        QCOMPARE(runtime.modelId(), QStringLiteral("embeddinggemma-2"));
        QCOMPARE(runtime.dimensions(), 768);
        QCOMPARE(runtime.modelFingerprint(), EmbeddingRuntime::fingerprint());
        QVERIFY(!runtime.modelFingerprint().isEmpty());
        QCOMPARE(runtime.capabilities(), (QJsonObject{{"text", true}, {"image", true}, {"videoFrames", true}, {"audio", true}}));
    }

    void unloadedRuntimeReportsErrors()
    {
        EmbeddingRuntime runtime;
        QVERIFY(!runtime.isLoaded());
        QString error;
        QVERIFY(runtime.embedText(QStringLiteral("hello"), PromptKind::Query, {}, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        QVERIFY(!runtime.lastError().isEmpty());
        error.clear();
        QVERIFY(runtime.embedImage(QString{}, 140, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(runtime.embedAudio(QString{}, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        runtime.close();
        runtime.close();
        QVERIFY(!runtime.isLoaded());
    }

    void backendModeAndDiagnosticsBeforeLoad()
    {
        const test::ScopedEnvironment mode("QCOPILOTS_SEARCH_BACKEND", QByteArrayLiteral("gpu"));
        EmbeddingRuntime runtime;
        auto diagnostic = runtime.diagnostics();
        QCOMPARE(diagnostic.value(QStringLiteral("requestedBackend")).toString(), QStringLiteral("gpu"));
        QVERIFY(!diagnostic.value(QStringLiteral("loaded")).toBool());
        QVERIFY(!diagnostic.value(QStringLiteral("gpuSelected")).toBool());
        QVERIFY(!diagnostic.value(QStringLiteral("gpuSelfTestPassed")).toBool());
        QVERIFY(!diagnostic.value(QStringLiteral("textWarmupCompleted")).toBool());
        QVERIFY(!diagnostic.value(QStringLiteral("packagedBackend")).toString().isEmpty());
        QString error;
        QVERIFY(runtime.setBackendMode(QStringLiteral(" CPU "), &error));
        QCOMPARE(runtime.diagnostics().value(QStringLiteral("requestedBackend")).toString(), QStringLiteral("cpu"));
        QVERIFY(!runtime.setBackendMode(QStringLiteral("guess"), &error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(runtime.diagnostics().value(QStringLiteral("requestedBackend")).toString(), QStringLiteral("cpu"));
    }

    void invalidBackendEnvironmentFailsExplicitly()
    {
        const test::ScopedEnvironment mode("QCOPILOTS_SEARCH_BACKEND", QByteArrayLiteral("cuad"));
        EmbeddingRuntime runtime;
        QString error;
        QVERIFY(!runtime.load(QString{}, true, &error));
        QVERIFY(error.contains(QStringLiteral("backend mode")));
        QCOMPARE(runtime.diagnostics().value(QStringLiteral("error")).toString(), error);
        QVERIFY(!runtime.diagnostics().value(QStringLiteral("loaded")).toBool());
    }

    void explicitDeviceSelectorIsValidatedBeforeLoad()
    {
        EmbeddingRuntime runtime;
        QString error;
        const QJsonObject requested{{"backend", "ROCm"}, {"deviceId", "0000:c1:00.0"}, {"name", "HIP1"}};
        QVERIFY2(runtime.setDevice(requested, &error), qPrintable(error));
        auto diagnostic = runtime.diagnostics();
        QVERIFY(diagnostic.value("deviceBindingExplicit").toBool());
        QCOMPARE(diagnostic.value("requestedDevice").toObject().value("backend").toString(), QStringLiteral("hip"));
        QCOMPARE(diagnostic.value("requestedDevice").toObject().value("deviceId").toString(), QStringLiteral("0000:c1:00.0"));
        QVERIFY(!diagnostic.value("loaded").toBool());
        const auto valid = diagnostic.value("requestedDevice");
        for (const auto& invalid : {QJsonObject{{"backend", "cuda"}}, QJsonObject{{"backend", "unknown"}, {"name", "GPU0"}},
            QJsonObject{{"backend", "cuda"}, {"deviceId", 2}}, QJsonObject{{"backend", "cuda"}, {"name", "GPU0"}, {"index", 0}}}) {
            QVERIFY(!runtime.setDevice(invalid, &error));
            QVERIFY(!error.isEmpty());
            QCOMPARE(runtime.diagnostics().value("requestedDevice"), valid);
        }
        QVERIFY(runtime.setDevice({}, &error));
        QVERIFY(!runtime.diagnostics().value("deviceBindingExplicit").toBool());
        QVERIFY(runtime.diagnostics().value("requestedDevice").toObject().isEmpty());
    }

    void missingModelsDoNotCountAsDeviceFailure()
    {
        EmbeddingRuntime runtime;
        QString error;
        QVERIFY(runtime.setDevice({{"backend", "cuda"}, {"deviceId", "0000:c1:00.0"}}, &error));
        QVERIFY(!runtime.load(QString{}, false, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!runtime.diagnostics().value("deviceFailure").toBool());
        QVERIFY(runtime.diagnostics().value("deviceBindingExplicit").toBool());
    }

    void missingAndCorruptModelsAreRejected()
    {
        QString error;
        auto directory = test::temporaryDirectory(&error);
        QVERIFY2(directory != nullptr, qPrintable(error));
        const auto models = directory->filePath(QStringLiteral("模型 空格/models"));
        QVERIFY(QDir().mkpath(models));
        QVERIFY(!EmbeddingRuntime::verifyModels(models, true, &error));
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(!EmbeddingRuntime::verifyModels(models, false, &error));
        QVERIFY(!error.isEmpty());

        // Names are runtime contracts, while these bytes deliberately are not GGUF files.
        for (const auto& name : {EmbeddingRuntime::textModelName(), EmbeddingRuntime::mediaModelName()}) {
            QFile file(QDir(models).filePath(name));
            QVERIFY(file.open(QIODevice::WriteOnly));
            QCOMPARE(file.write("not a model"), qint64(11));
        }
        error.clear();
        QVERIFY(!EmbeddingRuntime::verifyModels(models, false, &error));
        QVERIFY(!error.isEmpty());
        EmbeddingRuntime runtime;
        error.clear();
        QVERIFY(!runtime.load(models, false, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!runtime.isLoaded());
        runtime.close();
    }

    void independentTemporaryDirectories()
    {
        QString error;
        auto first = test::temporaryDirectory(&error);
        QVERIFY2(first != nullptr, qPrintable(error));
        auto second = test::temporaryDirectory(&error);
        QVERIFY2(second != nullptr, qPrintable(error));
        QVERIFY(first->path() != second->path());
        QVERIFY(QDir(first->path()).exists());
        QVERIFY(QDir(second->path()).exists());
        const auto firstPath = first->path();
        QVERIFY(first->remove());
        first.reset();
        QVERIFY(!QDir(firstPath).exists());
        QVERIFY(QDir(second->path()).exists());
    }

    void temporaryDirectoryUsesConfiguredRoot()
    {
        QString error;
        auto parent = test::temporaryDirectory(&error);
        QVERIFY2(parent != nullptr, qPrintable(error));
        const auto customRoot = parent->filePath(QStringLiteral("中文 临时根"));
        const test::ScopedEnvironment overrideRoot("QCOPILOTS_SEARCH_TEST_ROOT", customRoot.toUtf8());
        auto child = test::temporaryDirectory(&error);
        QVERIFY2(child != nullptr, qPrintable(error));
        QCOMPARE(QFileInfo(child->path()).absolutePath(), QDir(customRoot).absolutePath());
        const auto childPath = child->path();
        QVERIFY(child->remove());
        child.reset();
        QVERIFY(!QFileInfo::exists(childPath));
        QVERIFY(QFileInfo::exists(customRoot));
    }

    void temporaryDirectoryRejectsFileAsRoot()
    {
        QString error;
        auto parent = test::temporaryDirectory(&error);
        QVERIFY2(parent != nullptr, qPrintable(error));
        const auto occupiedRoot = parent->filePath(QStringLiteral("not a directory"));
        QFile file(occupiedRoot);
        QVERIFY(file.open(QIODevice::WriteOnly));
        file.close();
        const test::ScopedEnvironment overrideRoot("QCOPILOTS_SEARCH_TEST_ROOT", occupiedRoot.toUtf8());
        auto rejected = test::temporaryDirectory(&error);
        QVERIFY(rejected == nullptr);
        QVERIFY2(error.contains(occupiedRoot), qPrintable(error));
        QVERIFY(QFileInfo(occupiedRoot).isFile());
    }
};

QTEST_GUILESS_MAIN(RuntimeUnitTest)
#include "runtime_unit_test.moc"
