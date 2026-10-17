#include "core/app_settings.h"
#include "core/embedding_math.h"
#include "core/model_catalog.h"
#include "core/pipeline_manifest.h"
#include "core/runtime_paths.h"
#include "support/test_environment.h"
#include <QDir>
#include <QFileInfo>
#include <QSet>
#include <QtTest>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

class ModelCatalogTest : public QObject {
    Q_OBJECT
private slots:
    void catalogIdentity();
    void selectionAndMigration();
    void isolatedPaths();
    void dimensionsAtProtocolBoundary();
};

void ModelCatalogTest::catalogIdentity()
{
    QCOMPARE(modelCatalog().size(), 3);
    QCOMPARE(defaultModelId(), QStringLiteral("embeddinggemma-2"));
    QSet<QString> identities;
    for (const auto &model : modelCatalog()) {
        QVERIFY(model.dimensions == 768 || model.dimensions == 2048 || model.dimensions == 4096);
        QVERIFY(model.supportsVision);
        QCOMPARE(model.supportsAudio, model.id == defaultModelId());
        QCOMPARE(ModelFiles::manifest(model.id).size(), 2);
        const QString fingerprint = modelPipelineFingerprint(model.id);
        QVERIFY(!fingerprint.isEmpty());
        QVERIFY(!identities.contains(fingerprint));
        identities.insert(fingerprint);
        qint64 total = 0;
        for (const auto &artifact : model.artifacts) {
            QVERIFY(artifact.url.path().contains(model.revision));
            QCOMPARE(artifact.sha256.size(), 64);
            total += artifact.size;
        }
        QCOMPARE(model.toJson().value("totalBytes").toInteger(), total);
    }
    QCOMPARE(modelPipelineFingerprint(defaultModelId()),
             QStringLiteral("qcopilots-search-embedding-v1:e2eb31c724adc082a252eede5996a4d1edf435ad72538a2e9d9372a3b8f9560e"));
    QVERIFY(!findModel(QStringLiteral("../embeddinggemma-2")));
    QVERIFY(ModelFiles::manifest(QStringLiteral("unknown")).isEmpty());
    QVERIFY(modelPipelineFingerprint(QStringLiteral("unknown")).isEmpty());
}

void ModelCatalogTest::selectionAndMigration()
{
    AppSettings settings;
    const auto original = settings.toJson();
    for (int version : {1, 2}) {
        auto legacy = original;
        legacy.insert("version", version);
        legacy.remove("modelId");
        QString error;
        QVERIFY2(AppSettings::fromJson(legacy, &settings, &error), qPrintable(error));
        QCOMPARE(settings.modelId, defaultModelId());
        QVERIFY(settings.needsMigration());
        QCOMPARE(settings.toJson().value("version").toInt(), 3);
    }
    for (const auto &model : modelCatalog()) {
        auto json = original;
        json.insert("modelId", model.id);
        QString error;
        QVERIFY2(AppSettings::fromJson(json, &settings, &error), qPrintable(error));
        QCOMPARE(settings.modelId, model.id);
        QVERIFY(!settings.needsMigration());
    }
    const auto before = settings.toJson();
    auto invalid = before;
    invalid.insert("modelId", "unknown");
    QString error;
    QVERIFY(!AppSettings::fromJson(invalid, &settings, &error));
    QCOMPARE(settings.toJson(), before);
    invalid.insert("modelId", 7);
    QVERIFY(!AppSettings::fromJson(invalid, &settings, &error));
    QCOMPARE(settings.toJson(), before);
    const QString oldFingerprint = settings.fingerprint();
    settings.modelId = defaultModelId();
    QVERIFY(oldFingerprint != settings.fingerprint());
}

void ModelCatalogTest::isolatedPaths()
{
    TestEnvironment env(QStringLiteral("model-layout"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = env.runtimePaths();
    QCOMPARE(modelDirectory(paths.modelDir, defaultModelId()), QDir(paths.modelDir).absolutePath());
    const auto qwen = paths.forModelIndex(QStringLiteral("qwen3-vl-embedding-2b"));
    QCOMPARE(qwen.indexDir, QDir(paths.indexDir).filePath("models/qwen3-vl-embedding-2b"));
    QCOMPARE(qwen.profileDir, paths.profileDir);
    QCOMPARE(qwen.ipcName, paths.ipcName);
    QCOMPARE(qwen.lockFile, paths.lockFile);
    QCOMPARE(qwen.forModelIndex(defaultModelId()).indexDir, paths.indexDir);
    QCOMPARE(qwen.forModelIndex(QStringLiteral("qwen3-vl-embedding-2b")).indexDir, qwen.indexDir);
    QVERIFY(!QFileInfo::exists(qwen.indexDir));
    QVERIFY(!paths.forModelIndex(QStringLiteral("../../escape")).resolutionError.isEmpty());
    QVERIFY(modelDirectory(paths.modelDir, QStringLiteral("../../escape")).isEmpty());
    ModelFiles selected(paths.modelDir, QStringLiteral("qwen3-vl-embedding-8b"));
    QCOMPARE(selected.modelDirectory(), QDir(paths.modelDir).absoluteFilePath("qwen3-vl-embedding-8b"));
    QVERIFY(env.writeFile(QStringLiteral("profile/index/qcopilots-search.sqlite3"), QByteArrayLiteral("legacy")));
    QVERIFY(paths.forModelIndex(QStringLiteral("qwen3-vl-embedding-2b")).resolutionError.contains("legacy_profile_layout"));
}

void ModelCatalogTest::dimensionsAtProtocolBoundary()
{
    for (const auto &model : modelCatalog()) {
        Segment segment;
        segment.embedding = QVector<float>(model.dimensions, 0.0f);
        segment.embedding[0] = 1.0f;
        const auto decoded = Segment::fromJson(segment.toJson(true));
        QCOMPARE(decoded.embedding.size(), model.dimensions);
        QCOMPARE(embedding_math::normalized(decoded.embedding, model.dimensions), segment.embedding);
        QVERIFY_EXCEPTION_THROWN(embedding_math::normalized(decoded.embedding, model.dimensions + 1), std::runtime_error);
    }
    Segment oversized;
    oversized.embedding = QVector<float>(maximumEmbeddingDimensions + 1, 1.0f);
    QVERIFY(Segment::fromJson(oversized.toJson(true)).embedding.isEmpty());
}

QTEST_GUILESS_MAIN(ModelCatalogTest)
#include "model_catalog_test.moc"
