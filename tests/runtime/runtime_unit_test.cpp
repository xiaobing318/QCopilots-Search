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
