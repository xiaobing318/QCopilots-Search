#include "core/embedding_math.h"
#include "core/model_catalog.h"
#include "core/pipeline_manifest.h"
#include "support/test_environment.h"
#include <QDir>
#include <QtTest>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

class ModelCatalogTest : public QObject {
    Q_OBJECT
private slots:
    void catalogIdentity();
    void fixedModelPaths();
    void dimensionsAtProtocolBoundary();
};

void ModelCatalogTest::catalogIdentity()
{
    QCOMPARE(modelCatalog().size(), 1);
    QCOMPARE(defaultModelId(), QStringLiteral("embeddinggemma-2"));
    const auto &model = modelCatalog().first();
    QCOMPARE(model.id, defaultModelId());
    QCOMPARE(model.adapter, defaultModelId());
    QCOMPARE(model.dimensions, 768);
    QVERIFY(model.supportsVision);
    QVERIFY(model.supportsAudio);
    QCOMPARE(ModelFiles::manifest().size(), 2);
    qint64 total = 0;
    for (const auto &artifact : model.artifacts) {
        QVERIFY(artifact.url.path().contains(model.revision));
        QCOMPARE(artifact.sha256.size(), 64);
        total += artifact.size;
    }
    QCOMPARE(model.toJson().value("totalBytes").toInteger(), total);
    QCOMPARE(modelPipelineContract(defaultModelId()), embeddingPipelineContract());
    QCOMPARE(modelPipelineFingerprint(defaultModelId()),
             QStringLiteral("qcopilots-search-embedding-v1:e2eb31c724adc082a252eede5996a4d1edf435ad72538a2e9d9372a3b8f9560e"));
    for (const auto &id : {QString{}, QStringLiteral("unknown"), QStringLiteral("../embeddinggemma-2")}) {
        QVERIFY(!findModel(id));
        QVERIFY(modelPipelineContract(id).isEmpty());
        QVERIFY(modelPipelineFingerprint(id).isEmpty());
    }
}

void ModelCatalogTest::fixedModelPaths()
{
    TestEnvironment env(QStringLiteral("model-layout"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = env.runtimePaths();
    QCOMPARE(modelDirectory(paths.modelDir, defaultModelId()), QDir(paths.modelDir).absolutePath());
    ModelFiles files(paths.modelDir);
    QCOMPARE(files.modelDirectory(), QDir(paths.modelDir).absolutePath());
    QVERIFY(modelDirectory(paths.modelDir, QStringLiteral("unknown")).isEmpty());
    QVERIFY(modelDirectory(paths.modelDir, QStringLiteral("../../escape")).isEmpty());
    QVERIFY(modelDirectory(QString{}, defaultModelId()).isEmpty());
}

void ModelCatalogTest::dimensionsAtProtocolBoundary()
{
    Segment segment;
    segment.embedding = QVector<float>(embeddingDimensions, 0.0f);
    segment.embedding[0] = 1.0f;
    const auto decoded = Segment::fromJson(segment.toJson(true));
    QCOMPARE(decoded.embedding.size(), 768);
    QCOMPARE(embedding_math::normalized(decoded.embedding), segment.embedding);
    for (const int dimensions : {769, 2048, 4096}) {
        Segment oversized;
        oversized.embedding = QVector<float>(dimensions, 1.0f);
        QVERIFY(Segment::fromJson(oversized.toJson(true)).embedding.isEmpty());
        QVERIFY_EXCEPTION_THROWN(embedding_math::normalized(oversized.embedding), std::runtime_error);
    }
}

QTEST_GUILESS_MAIN(ModelCatalogTest)
#include "model_catalog_test.moc"
