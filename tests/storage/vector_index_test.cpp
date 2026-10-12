#include "storage/vector_index.h"

#include <QCryptographicHash>
#include <QDataStream>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <QtTest>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <random>
#include <stdexcept>

using namespace qcopilots_search;

Q_DECLARE_METATYPE(qcopilots_search::VectorRecord)

namespace {

QString temporaryTemplate()
{
    const QString configured = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT");
    const QString parent = configured.isEmpty() ? QDir::tempPath() : configured;
    if (!QDir().mkpath(parent))
        return QDir(parent).filePath("unavailable-parent/qcopilots-search-vector-tests-XXXXXX");
    return QDir(parent).filePath(QString::fromUtf8("qcopilots-search-vector-tests 中文 空格-XXXXXX"));
}

QVector<float> basis(int coordinate)
{
    QVector<float> result(4, 0.0f);
    result[coordinate] = 1.0f;
    return result;
}

QSet<QString> filesBelow(const QString &root)
{
    QSet<QString> result;
    const QDir directory(root);
    for (const auto &name : directory.entryList(QDir::Files | QDir::NoDotAndDotDot))
        result.insert(directory.filePath(name));
    for (const auto &name : directory.entryList(QDir::Dirs | QDir::NoDotAndDotDot))
        result.unite(filesBelow(directory.filePath(name)));
    return result;
}

QVector<VectorRecord> sphericalRecords(int count, quint32 seed = 20261011)
{
    std::mt19937 random(seed);
    QVector<VectorRecord> records;
    records.reserve(count);
    for (int row = 0; row < count; ++row) {
        QVector<float> vector;
        double norm = 0;
        for (int coordinate = 0; coordinate < 4; ++coordinate) {
            const float value = float(double(random()) / double(std::mt19937::max()) * 2.0 - 1.0);
            vector.append(value);
            norm += double(value) * value;
        }
        for (float &value : vector)
            value = float(value / std::sqrt(norm));
        records.append({qint64(row + 1) * VectorIndex::shardCount, vector});
    }
    return records;
}

} // namespace

class VectorIndexTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
    }

    void emptyIndex()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.loadManifest(&error), qPrintable(error));
        const auto snapshot = index.snapshot();
        QVERIFY(snapshot);
        QCOMPARE(snapshot->size(), qsizetype(0));
        QVERIFY(index.search(snapshot, basis(0), 10, nullptr, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void shardsMergeInDistanceOrder()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}, {32, basis(1)}}, &error), qPrintable(error));
        QVERIFY2(index.rebuildShard(1, 2, {{17, {0.8f, 0.6f, 0.0f, 0.0f}}}, &error), qPrintable(error));
        const auto snapshot = index.snapshot();
        QCOMPARE(snapshot->size(), qsizetype(3));
        QCOMPARE(snapshot->shardGeneration(0), quint64(1));
        QCOMPARE(snapshot->shardGeneration(1), quint64(2));
        QCOMPARE(snapshot->generation(), quint64(2));
        const auto matches = index.search(snapshot, basis(0), 10, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(3));
        QCOMPARE(matches.at(0).id, qint64(16));
        QCOMPARE(matches.at(1).id, qint64(17));
        QCOMPARE(matches.at(2).id, qint64(32));
        QVERIFY(matches.at(0).distance <= matches.at(1).distance);
        QVERIFY(matches.at(1).distance <= matches.at(2).distance);
        QCOMPARE(index.search(snapshot, basis(0), 1).size(), qsizetype(1));
    }

    void allowedIdsRestrictCandidates()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}, {32, basis(1)}, {48, basis(2)}}, &error), qPrintable(error));
        QVERIFY2(index.rebuildShard(1, 2, {{17, {0.8f, 0.6f, 0.0f, 0.0f}}}, &error), qPrintable(error));
        const QSet<qint64> allowed{17, 32, 999999};
        const auto matches = index.search(index.snapshot(), basis(0), 10, &allowed, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(2));
        QCOMPARE(matches.at(0).id, qint64(17));
        QCOMPARE(matches.at(1).id, qint64(32));
        const QSet<qint64> empty;
        QVERIFY(index.search(index.snapshot(), basis(0), 10, &empty, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void reloadFromUnicodeAndSpacePath()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        const QString root = QDir(temporary.path()).filePath(QString::fromUtf8("索引 文件夹"));
        QString error;
        {
            VectorIndex index(root, 4);
            QVERIFY2(index.rebuildShard(0, 11, {{16, basis(0)}}, &error), qPrintable(error));
            QVERIFY2(index.rebuildShard(1, 12, {{17, basis(1)}}, &error), qPrintable(error));
        }
        VectorIndex reloaded(root, 4);
        QVERIFY2(reloaded.loadManifest(&error), qPrintable(error));
        QCOMPARE(reloaded.snapshot()->size(), qsizetype(2));
        QCOMPARE(reloaded.snapshot()->generation(), quint64(12));
        QCOMPARE(reloaded.snapshot()->shardGeneration(0), quint64(11));
        const auto matches = reloaded.search(reloaded.snapshot(), basis(1), 2, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(2));
        QCOMPARE(matches.first().id, qint64(17));
    }

    void tombstonesParticipateInTraversalAndExactFiltering()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QVector<VectorRecord> records;
        QSet<qint64> excluded;
        for (int i = 1; i <= 1024; ++i) {
            records.append({qint64(i) * 16, {1.0f, float(i) * 0.001f, 0.0f, 0.0f}});
            if (i <= 512)
                excluded.insert(qint64(i) * 16);
        }
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, records, &error), qPrintable(error));
        const auto snapshot = index.snapshot();
        const auto ann = index.search(snapshot, basis(0), 1, nullptr, &error, &excluded);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(ann.size(), qsizetype(1));
        QCOMPARE(ann.first().id, qint64(513 * 16));
        const QSet<qint64> allowed{16, 512 * 16, 514 * 16, 515 * 16};
        const auto exact = index.search(snapshot, basis(0), 10, &allowed, &error, &excluded);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(exact.size(), qsizetype(2));
        QCOMPARE(exact.first().id, qint64(514 * 16));
        QCOMPARE(exact.last().id, qint64(515 * 16));
        QSet<qint64> largeAllowed = allowed;
        for (qint64 id = 100000; id < 109000; ++id)
            largeAllowed.insert(id);
        const auto filteredAnn = index.search(snapshot, basis(0), 10, &largeAllowed, &error, &excluded);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(filteredAnn.size(), qsizetype(2));
        QCOMPARE(filteredAnn.first().id, qint64(514 * 16));
        for (const auto &record : records)
            excluded.insert(record.id);
        QVERIFY(index.search(snapshot, basis(0), 10, nullptr, &error, &excluded).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void cancellationDoesNotChangeSnapshotOrUncancelledResults()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QVector<VectorRecord> records;
        QSet<qint64> allowed;
        for (int i = 1; i <= 1024; ++i) {
            records.append({qint64(i) * 16, {1.0f, float(i) * 0.001f, 0.0f, 0.0f}});
            allowed.insert(qint64(i) * 16);
        }
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, records, &error), qPrintable(error));
        const auto snapshot = index.snapshot();
        const QSet<qint64> excluded{16, 32, 48};
        const auto expected = index.search(snapshot, basis(0), 8, nullptr, &error, &excluded);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(expected.size(), qsizetype(8));
        std::atomic_bool cancelled{true};
        QVERIFY(index.search(snapshot, basis(0), 8, nullptr, &error, &excluded, &cancelled).isEmpty());
        QCOMPARE(error, QStringLiteral("Search cancelled"));
        QVERIFY(index.search(snapshot, basis(0), 8, &allowed, &error, &excluded, &cancelled).isEmpty());
        QCOMPARE(error, QStringLiteral("Search cancelled"));
        cancelled.store(false);
        const auto actual = index.search(snapshot, basis(0), 8, nullptr, &error, &excluded, &cancelled);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(actual.size(), expected.size());
        for (qsizetype i = 0; i < actual.size(); ++i) {
            QCOMPARE(actual[i].id, expected[i].id);
            QCOMPARE(actual[i].distance, expected[i].distance);
        }
        const auto exact = index.search(snapshot, basis(0), 8, &allowed, &error, &excluded, &cancelled);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(exact.size(), expected.size());
        for (qsizetype i = 0; i < exact.size(); ++i)
            QCOMPARE(exact[i].id, expected[i].id);
        const auto noFilter = index.search(snapshot, basis(0), 1, nullptr, &error, nullptr, &cancelled);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(noFilter.size(), qsizetype(1));
        QCOMPARE(noFilter.first().id, qint64(16));
        QCOMPARE(index.snapshot()->generation(), quint64(1));
        QCOMPARE(index.snapshot()->size(), qsizetype(1024));
    }

    void runningTraversalCanBeCancelledDeterministically()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        std::mutex gateMutex;
        std::condition_variable gate;
        bool observeTraversal = true;
        bool enteredTraversal = false;
        bool releaseTraversal = false;
        bool cancellationWasAlreadySet = false;
        bool hookTimedOut = false;
        std::atomic_bool cancelled{false};
        VectorIndex index(temporary.path(), 4, [&](const QString &point) {
            if (point != QStringLiteral("ann-search-traversal-started"))
                return;
            std::unique_lock lock(gateMutex);
            if (!observeTraversal)
                return;
            // This callback is entered by hnswlib's active base-layer traversal,
            // after its visited-list acquisition. It cannot run at query entry.
            cancellationWasAlreadySet = cancelled.load();
            enteredTraversal = true;
            gate.notify_all();
            hookTimedOut = !gate.wait_for(lock, std::chrono::seconds(10), [&] { return releaseTraversal; });
        });
        QVector<VectorRecord> records;
        for (int i = 1; i <= 1024; ++i)
            records.append({qint64(i) * 16, {1.0f, float(i) * 0.001f, 0.0f, 0.0f}});
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, records, &error), qPrintable(error));
        const auto snapshot = index.snapshot();
        auto running = std::async(std::launch::async, [&] {
            QString searchError;
            auto hits = index.search(snapshot, basis(0), 8, nullptr, &searchError, nullptr, &cancelled);
            return std::pair{std::move(hits), std::move(searchError)};
        });
        bool observed;
        {
            std::unique_lock lock(gateMutex);
            observed = gate.wait_for(lock, std::chrono::seconds(10), [&] { return enteredTraversal; });
            // Always release and join before asserting, including timeout paths.
            // The flag is set only after the callback proved traversal began.
            cancelled.store(true);
            releaseTraversal = true;
        }
        gate.notify_all();
        const auto outcome = running.get();
        QVERIFY2(observed, "The query did not reach the HNSW traversal checkpoint");
        QVERIFY(!hookTimedOut);
        QVERIFY(!cancellationWasAlreadySet);
        QVERIFY(outcome.first.isEmpty());
        QCOMPARE(outcome.second, QStringLiteral("Search cancelled"));
        {
            std::lock_guard lock(gateMutex);
            observeTraversal = false;
        }
        cancelled.store(false);
        const auto retry = index.search(snapshot, basis(0), 1, nullptr, &error, nullptr, &cancelled);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(retry.size(), qsizetype(1));
        QCOMPARE(retry.first().id, qint64(16));
        QCOMPARE(index.snapshot()->size(), qsizetype(1024));
        QCOMPARE(index.snapshot()->generation(), quint64(1));
    }

    void traversalCheckpointExceptionsReturnAfterHnswCleanup()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        int failureKind = 0;
        VectorIndex index(temporary.path(), 4, [&](const QString &point) {
            if (point != QStringLiteral("ann-search-traversal-started"))
                return;
            if (failureKind == 1)
                throw std::runtime_error("Injected traversal failure");
            if (failureKind == 2)
                throw 42;
        });
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}, {32, basis(1)}}, &error), qPrintable(error));
        std::atomic_bool cancelled{false};
        for (int kind = 1; kind <= 2; ++kind) {
            failureKind = kind;
            QVERIFY(index.search(index.snapshot(), basis(0), 1, nullptr, &error, nullptr, &cancelled).isEmpty());
            QVERIFY(error.contains(QStringLiteral("ANN traversal checkpoint failed")));
            failureKind = 0;
            const auto retry = index.search(index.snapshot(), basis(0), 1, nullptr, &error, nullptr, &cancelled);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(retry.size(), qsizetype(1));
            QCOMPARE(retry.first().id, qint64(16));
        }
    }

    void legacy512SearchBudgetLoadsWithoutRewritingStoredGraph()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        QString error;
        QVector<VectorNeighbor> expected;
        {
            VectorIndex original(temporary.path(), 4);
            QVERIFY2(original.rebuildShard(0, 1,
                {{16, basis(0)}, {32, {0.8f, 0.6f, 0.0f, 0.0f}}, {48, basis(1)}},
                &error), qPrintable(error));
            expected = original.search(original.snapshot(), basis(0), 3, nullptr, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(expected.size(), qsizetype(3));
        }

        QFile manifest(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-ann-manifest.json")));
        QVERIFY2(manifest.open(QIODevice::ReadOnly), qPrintable(manifest.errorString()));
        QJsonParseError parseError;
        auto document = QJsonDocument::fromJson(manifest.readAll(), &parseError);
        manifest.close();
        QCOMPARE(parseError.error, QJsonParseError::NoError);
        QVERIFY(document.isObject());
        auto manifestObject = document.object();
        auto shards = manifestObject.value(QStringLiteral("shards")).toArray();
        QCOMPARE(shards.size(), qsizetype(VectorIndex::shardCount));
        auto shard = shards.at(0).toObject();
        const QString graphPath = QDir(temporary.path()).filePath(shard.value(QStringLiteral("file")).toString());
        QFile graph(graphPath);
        QVERIFY2(graph.open(QIODevice::ReadOnly), qPrintable(graph.errorString()));
        QByteArray legacyGraph = graph.readAll();
        graph.close();
        const qsizetype originalBytes = legacyGraph.size();
        QCOMPARE(shard.value(QStringLiteral("bytes")).toString().toLongLong(), qint64(originalBytes));

        {
            // Consume the v1 field sequence to locate efQuery, rather than
            // hard-coding an offset. The magic includes its terminating NUL.
            // efQuery records search effort and does not change graph topology.
            constexpr char magic[] = "qcopilots-search-hnsw-v1";
            QDataStream header(&legacyGraph, QIODevice::ReadWrite);
            header.setByteOrder(QDataStream::LittleEndian);
            QByteArray actualMagic(sizeof(magic), '\0');
            QCOMPARE(header.readRawData(actualMagic.data(), int(sizeof(magic))), int(sizeof(magic)));
            QCOMPARE(actualMagic, QByteArray(magic, sizeof(magic)));
            quint32 format = 0, dimensions = 0, shardNumber = 0, graphM = 0, efBuild = 0;
            quint64 generation = 0, count = 0;
            header >> format >> dimensions >> shardNumber >> generation >> count >> graphM >> efBuild;
            QCOMPARE(header.status(), QDataStream::Ok);
            QCOMPARE(format, quint32(1));
            QCOMPARE(dimensions, quint32(4));
            QCOMPARE(shardNumber, quint32(0));
            QCOMPARE(generation, quint64(1));
            QCOMPARE(count, quint64(3));
            QCOMPARE(graphM, quint32(24));
            QCOMPARE(efBuild, quint32(200));
            const qint64 searchBudgetOffset = header.device()->pos();
            quint32 savedSearchBudget = 0;
            header >> savedSearchBudget;
            QCOMPARE(savedSearchBudget, quint32(VectorIndex::searchEf));
            QVERIFY(savedSearchBudget != 512);
            QVERIFY(header.device()->seek(searchBudgetOffset));
            header << quint32(512);
            QCOMPARE(header.status(), QDataStream::Ok);
        }
        QCOMPARE(legacyGraph.size(), originalBytes);
        QVERIFY2(graph.open(QIODevice::WriteOnly | QIODevice::Truncate), qPrintable(graph.errorString()));
        QCOMPARE(graph.write(legacyGraph), qint64(legacyGraph.size()));
        graph.close();
        shard.insert(QStringLiteral("sha256"), QString::fromLatin1(
            QCryptographicHash::hash(legacyGraph, QCryptographicHash::Sha256).toHex()));
        shards[0] = shard;
        manifestObject.insert(QStringLiteral("shards"), shards);
        const QByteArray legacyManifest = QJsonDocument(manifestObject).toJson(QJsonDocument::Compact);
        QVERIFY2(manifest.open(QIODevice::WriteOnly | QIODevice::Truncate), qPrintable(manifest.errorString()));
        QCOMPARE(manifest.write(legacyManifest), qint64(legacyManifest.size()));
        manifest.close();

        const auto beforeFiles = filesBelow(temporary.path());
        {
            VectorIndex restored(temporary.path(), 4);
            QVERIFY2(restored.loadManifest(&error), qPrintable(error));
            QCOMPARE(restored.snapshot()->size(), qsizetype(3));
            QCOMPARE(restored.snapshot()->generation(), quint64(1));
            const auto actual = restored.search(restored.snapshot(), basis(0), 3, nullptr, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(actual.size(), expected.size());
            for (qsizetype index = 0; index < actual.size(); ++index) {
                QCOMPARE(actual[index].id, expected[index].id);
                QCOMPARE(actual[index].distance, expected[index].distance);
            }
        }
        QVERIFY(QFileInfo::exists(graphPath));
        QCOMPARE(filesBelow(temporary.path()), beforeFiles);
        QVERIFY2(graph.open(QIODevice::ReadOnly), qPrintable(graph.errorString()));
        QCOMPARE(graph.readAll(), legacyGraph);
        graph.close();
        QVERIFY2(manifest.open(QIODevice::ReadOnly), qPrintable(manifest.errorString()));
        QCOMPARE(manifest.readAll(), legacyManifest);
        manifest.close();
    }

    void oldSnapshotSurvivesRebuild()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        const auto oldSnapshot = index.snapshot();
        QVERIFY2(index.rebuildShard(0, 2, {{32, basis(1)}}, &error), qPrintable(error));
        const auto newSnapshot = index.snapshot();
        QCOMPARE(oldSnapshot->shardGeneration(0), quint64(1));
        QCOMPARE(newSnapshot->shardGeneration(0), quint64(2));
        const auto oldMatches = index.search(oldSnapshot, basis(0), 10, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(oldMatches.size(), qsizetype(1));
        QCOMPARE(oldMatches.first().id, qint64(16));
        const auto newMatches = index.search(newSnapshot, basis(0), 10, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(newMatches.size(), qsizetype(1));
        QCOMPARE(newMatches.first().id, qint64(32));
    }

    void retiredFilesWaitForLastSnapshot()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        QString error;
        QSet<QString> currentFiles;
        {
            VectorIndex index(temporary.path(), 4);
            QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
            auto oldSnapshot = index.snapshot();
            auto anotherReference = oldSnapshot;
            const auto firstFiles = filesBelow(temporary.path());
            QVERIFY2(index.rebuildShard(0, 2, {{32, basis(1)}}, &error), qPrintable(error));
            const auto secondFiles = filesBelow(temporary.path());
            QSet<QString> oldShardFiles;
            for (const QString &path : firstFiles) {
                if (QFileInfo(path).fileName().startsWith(QStringLiteral("qcopilots-search-ann-shard-"))
                    && path.endsWith(QStringLiteral(".bin")))
                    oldShardFiles.insert(path);
            }
            QVERIFY(!oldShardFiles.isEmpty());
            for (const QString &path : oldShardFiles)
                QVERIFY2(secondFiles.contains(path), qPrintable(path));
            oldSnapshot.reset();
            for (const QString &path : oldShardFiles)
                QVERIFY2(QFileInfo::exists(path), qPrintable(path));
            anotherReference.reset();
            for (const QString &path : oldShardFiles)
                QVERIFY2(!QFileInfo::exists(path), qPrintable(path));
            currentFiles = filesBelow(temporary.path());
            QVERIFY(!currentFiles.isEmpty());
        }
        // Shutting down a live index must keep its committed generation loadable.
        QCOMPARE(filesBelow(temporary.path()), currentFiles);
        VectorIndex reloaded(temporary.path(), 4);
        QVERIFY2(reloaded.loadManifest(&error), qPrintable(error));
        const auto matches = reloaded.search(reloaded.snapshot(), basis(1), 1, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(32));
    }

    void emptyRebuildRemovesOnlyChosenShard()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        QVERIFY2(index.rebuildShard(1, 2, {{17, basis(1)}}, &error), qPrintable(error));
        QVERIFY2(index.rebuildShard(0, 3, {}, &error), qPrintable(error));
        QCOMPARE(index.snapshot()->size(), qsizetype(1));
        QCOMPARE(index.snapshot()->shardGeneration(0), quint64(3));
        VectorIndex reloaded(temporary.path(), 4);
        QVERIFY2(reloaded.loadManifest(&error), qPrintable(error));
        QCOMPARE(reloaded.snapshot()->size(), qsizetype(1));
        QCOMPARE(reloaded.snapshot()->shardGeneration(0), quint64(3));
        const auto matches = reloaded.search(reloaded.snapshot(), basis(1), 10, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(17));
    }

    void validLoadCollectsOnlyUnreferencedOwnedFiles()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        auto oldSnapshot = index.snapshot();
        const QDir directory(temporary.path());
        const auto oldNames = directory.entryList({QStringLiteral("qcopilots-search-ann-shard-*.bin")}, QDir::Files);
        QCOMPARE(oldNames.size(), qsizetype(1));
        const QString oldFile = directory.filePath(oldNames.first());
        const QString orphan = directory.filePath(QStringLiteral(
            "qcopilots-search-ann-shard-0-1-12345678-1234-1234-1234-123456789abc.bin"));
        const QString unrelated = directory.filePath(QStringLiteral("qcopilots-search-ann-shard-personal.bin"));
        QVERIFY(QFile::copy(oldFile, orphan));
        QVERIFY(QFile::copy(oldFile, unrelated));
        QVERIFY2(index.rebuildShard(0, 2, {{32, basis(1)}}, &error), qPrintable(error));
        QVERIFY2(index.loadManifest(&error), qPrintable(error));
        QVERIFY(!QFileInfo::exists(orphan));
        QVERIFY(QFileInfo::exists(unrelated));
        QVERIFY(QFileInfo::exists(oldFile));
        QCOMPARE(index.search(oldSnapshot, basis(0), 1).first().id, qint64(16));
        oldSnapshot.reset();
        QVERIFY(!QFileInfo::exists(oldFile));
    }

    void exceptionBeforeManifestDoesNotPublishOrLeak()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        bool inject = false;
        QString faultPoint;
        VectorIndex index(temporary.path(), 4, [&](const QString &point) {
            if (inject) {
                faultPoint = point;
                throw std::runtime_error("Injected pre-manifest failure");
            }
        });
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        const auto originalFiles = filesBelow(temporary.path());
        inject = true;
        QVERIFY(!index.rebuildShard(0, 2, {{32, basis(1)}}, &error));
        QVERIFY(error.contains(QStringLiteral("Injected pre-manifest failure")));
        QCOMPARE(faultPoint, QStringLiteral("ann-file-saved-before-manifest"));
        QCOMPARE(filesBelow(temporary.path()), originalFiles);
        QCOMPARE(index.snapshot()->generation(), quint64(1));
        VectorIndex reopened(temporary.path(), 4);
        QVERIFY2(reopened.loadManifest(&error), qPrintable(error));
        QCOMPARE(reopened.snapshot()->generation(), quint64(1));
        QCOMPARE(reopened.search(reopened.snapshot(), basis(0), 1).first().id, qint64(16));
    }

    void corruptManifestPreservesLiveSnapshot()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 7, {{16, basis(0)}}, &error), qPrintable(error));
        const auto previous = index.snapshot();
        QFile manifest(QDir(temporary.path()).filePath(QStringLiteral("qcopilots-search-ann-manifest.json")));
        QVERIFY2(manifest.open(QIODevice::WriteOnly | QIODevice::Truncate), qPrintable(manifest.errorString()));
        QCOMPARE(manifest.write("{ invalid manifest"), qint64(18));
        manifest.close();
        QVERIFY(!index.loadManifest(&error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(index.snapshot()->generation(), previous->generation());
        QCOMPARE(index.snapshot()->size(), previous->size());
        error.clear();
        const auto matches = index.search(index.snapshot(), basis(0), 1, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(16));
        VectorIndex fresh(temporary.path(), 4);
        QVERIFY(!fresh.loadManifest(&error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(fresh.snapshot()->size(), qsizetype(0));
    }

    void multilevelGraphReloadPreservesNearestNeighbors()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        const auto records = sphericalRecords(2048);
        QVector<QVector<VectorNeighbor>> expected;
        QString error;
        {
            VectorIndex index(temporary.path(), 4);
            QVERIFY2(index.rebuildShard(0, 1, records, &error), qPrintable(error));
            for (int row = 0; row < records.size(); row += 137) {
                const auto matches = index.search(index.snapshot(), records[row].vector, 10, nullptr, &error);
                QVERIFY2(error.isEmpty(), qPrintable(error));
                QCOMPARE(matches.size(), qsizetype(10));
                QCOMPARE(matches.first().id, records[row].id);
                expected.append(matches);
            }
        }
        VectorIndex reloaded(temporary.path(), 4);
        QVERIFY2(reloaded.loadManifest(&error), qPrintable(error));
        QCOMPARE(reloaded.snapshot()->size(), records.size());
        for (int row = 0, query = 0; row < records.size(); row += 137, ++query) {
            const auto matches = reloaded.search(reloaded.snapshot(), records[row].vector, 10, nullptr, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(matches.size(), expected[query].size());
            for (qsizetype neighbor = 0; neighbor < matches.size(); ++neighbor) {
                QCOMPARE(matches[neighbor].id, expected[query][neighbor].id);
                QCOMPARE(matches[neighbor].distance, expected[query][neighbor].distance);
            }
        }
    }

    void corruptShardChecksumPreservesLiveSnapshot()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 3, {{16, basis(0)}}, &error), qPrintable(error));
        const auto previous = index.snapshot();
        const QDir directory(temporary.path());
        const auto shardFiles = directory.entryList({QStringLiteral("qcopilots-search-ann-shard-*.bin")}, QDir::Files);
        QCOMPARE(shardFiles.size(), qsizetype(1));
        QFile shard(directory.filePath(shardFiles.first()));
        QVERIFY2(shard.open(QIODevice::ReadWrite), qPrintable(shard.errorString()));
        const qint64 bytes = shard.size();
        QVERIFY(bytes > 0);
        QVERIFY(shard.seek(bytes - 1));
        char last;
        QCOMPARE(shard.read(&last, 1), qint64(1));
        last = char(static_cast<unsigned char>(last) ^ 1U);
        QVERIFY(shard.seek(bytes - 1));
        QCOMPARE(shard.write(&last, 1), qint64(1));
        shard.close();
        QCOMPARE(QFileInfo(shard.fileName()).size(), bytes);
        QVERIFY(!index.loadManifest(&error));
        QVERIFY2(error.contains(QStringLiteral("SHA-256")), qPrintable(error));
        QCOMPARE(index.snapshot()->generation(), previous->generation());
        QCOMPARE(index.snapshot()->size(), previous->size());
        const auto matches = index.search(index.snapshot(), basis(0), 1, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(16));
        VectorIndex reloaded(temporary.path(), 4);
        QVERIFY(!reloaded.loadManifest(&error));
        QVERIFY2(error.contains(QStringLiteral("SHA-256")), qPrintable(error));
        QCOMPARE(reloaded.snapshot()->size(), qsizetype(0));
    }

    void manifestPublicationFailureRemovesUnpublishedGraph()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        const QDir directory(temporary.path());
        const QString name = QStringLiteral("qcopilots-search-ann-manifest.json");
        QVERIFY(directory.mkdir(name));
        VectorIndex index(temporary.path(), 4);
        const auto before = filesBelow(temporary.path());
        QString error;
        QVERIFY(!index.rebuildShard(0, 1, {{16, basis(0)}}, &error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(index.snapshot()->generation(), quint64(0));
        QCOMPARE(index.snapshot()->size(), qsizetype(0));
        QCOMPARE(filesBelow(temporary.path()), before);
        QVERIFY(directory.rmdir(name));
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        VectorIndex reloaded(temporary.path(), 4);
        QVERIFY2(reloaded.loadManifest(&error), qPrintable(error));
        QCOMPARE(reloaded.snapshot()->size(), qsizetype(1));
    }

    void staleGenerationCannotReplacePublishedShard()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 12, {{16, basis(0)}}, &error), qPrintable(error));
        const auto before = filesBelow(temporary.path());
        for (const quint64 stale : {quint64(12), quint64(11), quint64(0)}) {
            QVERIFY(!index.rebuildShard(0, stale, {{32, basis(1)}}, &error));
            QVERIFY(!error.isEmpty());
            QCOMPARE(index.snapshot()->generation(), quint64(12));
            QCOMPARE(index.snapshot()->shardGeneration(0), quint64(12));
            QCOMPARE(filesBelow(temporary.path()), before);
        }
        const auto matches = index.search(index.snapshot(), basis(0), 1, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(16));
    }

    void concurrentReadersKeepOldSnapshotDuringRebuild()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        const auto records = sphericalRecords(1024);
        const auto replacement = sphericalRecords(1024, 20261012);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, records, &error), qPrintable(error));
        const auto oldSnapshot = index.snapshot();
        struct ReaderResult {
            QString failure;
            int completed = 0;
        };
        std::promise<void> startPromise;
        const auto start = startPromise.get_future().share();
        std::atomic<bool> rebuildComplete = false;
        std::array<std::future<ReaderResult>, 4> readers;
        for (size_t thread = 0; thread < readers.size(); ++thread) {
            readers[thread] = std::async(std::launch::async, [&, start, oldSnapshot, thread] {
                start.wait();
                ReaderResult result;
                do {
                    const auto row = (result.completed * 73 + int(thread) * 151) % records.size();
                    QString queryError;
                    const auto matches = index.search(oldSnapshot, records[row].vector, 5, nullptr, &queryError);
                    if (!queryError.isEmpty()) {
                        result.failure = queryError;
                        break;
                    }
                    if (matches.size() != 5 || matches.first().id != records[row].id) {
                        result.failure = QStringLiteral("Old-snapshot result changed during publication");
                        break;
                    }
                    ++result.completed;
                } while (!rebuildComplete.load(std::memory_order_acquire) || result.completed < 64);
                return result;
            });
        }
        startPromise.set_value();
        // Join every reader before checking assertions or releasing shared state.
        const bool rebuilt = index.rebuildShard(0, 2, replacement, &error);
        rebuildComplete.store(true, std::memory_order_release);
        std::array<ReaderResult, 4> results;
        for (size_t thread = 0; thread < readers.size(); ++thread)
            results[thread] = readers[thread].get();
        QVERIFY2(rebuilt, qPrintable(error));
        for (const auto &result : results) {
            QVERIFY2(result.failure.isEmpty(), qPrintable(result.failure));
            QVERIFY(result.completed >= 64);
        }
        QCOMPARE(oldSnapshot->generation(), quint64(1));
        QCOMPARE(index.snapshot()->generation(), quint64(2));
        const auto fresh = index.search(index.snapshot(), replacement.first().vector, 1, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(fresh.size(), qsizetype(1));
        QCOMPARE(fresh.first().id, replacement.first().id);
    }

    void invalidRecords_data()
    {
        QTest::addColumn<QVector<VectorRecord>>("records");
        QTest::newRow("dimension-mismatch") << QVector<VectorRecord>{{16, {1.0f, 0.0f, 0.0f}}};
        QTest::newRow("nan") << QVector<VectorRecord>{{16, {std::numeric_limits<float>::quiet_NaN(), 0.0f, 0.0f, 0.0f}}};
        QTest::newRow("infinity") << QVector<VectorRecord>{{16, {std::numeric_limits<float>::infinity(), 0.0f, 0.0f, 0.0f}}};
        QTest::newRow("zero-vector") << QVector<VectorRecord>{{16, QVector<float>(4, 0.0f)}};
        QTest::newRow("negative-id") << QVector<VectorRecord>{{-16, basis(0)}};
        QTest::newRow("zero-id") << QVector<VectorRecord>{{0, basis(0)}};
        QTest::newRow("duplicate-id") << QVector<VectorRecord>{{16, basis(0)}, {16, basis(1)}};
        QTest::newRow("wrong-shard") << QVector<VectorRecord>{{17, basis(0)}};
    }

    void invalidRecords()
    {
        QFETCH(QVector<VectorRecord>, records);
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        QVERIFY(!index.rebuildShard(0, 2, records, &error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(index.snapshot()->shardGeneration(0), quint64(1));
        QCOMPARE(index.snapshot()->size(), qsizetype(1));
        error.clear();
        const auto matches = index.search(index.snapshot(), basis(0), 10, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(16));
        VectorIndex reloaded(temporary.path(), 4);
        QVERIFY2(reloaded.loadManifest(&error), qPrintable(error));
        QCOMPARE(reloaded.snapshot()->shardGeneration(0), quint64(1));
    }

    void invalidQueries_data()
    {
        QTest::addColumn<QVector<float>>("query");
        QTest::newRow("dimension-mismatch") << QVector<float>{1.0f, 0.0f};
        QTest::newRow("nan") << QVector<float>{std::numeric_limits<float>::quiet_NaN(), 0.0f, 0.0f, 0.0f};
        QTest::newRow("infinity") << QVector<float>{std::numeric_limits<float>::infinity(), 0.0f, 0.0f, 0.0f};
        QTest::newRow("zero") << QVector<float>(4, 0.0f);
    }

    void invalidQueries()
    {
        QFETCH(QVector<float>, query);
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, basis(0)}}, &error), qPrintable(error));
        QVERIFY(index.search(index.snapshot(), query, 10, nullptr, &error).isEmpty());
        QVERIFY(!error.isEmpty());
    }

    void dimensions768()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path());
        QVector<float> vector(768, 0.0f);
        vector[767] = 1.0f;
        QString error;
        QVERIFY2(index.rebuildShard(0, 1, {{16, vector}}, &error), qPrintable(error));
        const auto matches = index.search(index.snapshot(), vector, 1, nullptr, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(matches.size(), qsizetype(1));
        QCOMPARE(matches.first().id, qint64(16));
        QVERIFY(std::abs(matches.first().distance) < 1.0e-5f);
    }

    void invalidShardDoesNotPublish()
    {
        QTemporaryDir temporary(temporaryTemplate());
        QVERIFY2(temporary.isValid(), qPrintable(temporary.errorString()));
        VectorIndex index(temporary.path(), 4);
        QString error;
        QVERIFY(!index.rebuildShard(-1, 1, {}, &error));
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(!index.rebuildShard(VectorIndex::shardCount, 1, {}, &error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(index.snapshot()->size(), qsizetype(0));
        for (qint64 id = 1; id <= 1024; ++id)
            QCOMPARE(VectorIndex::shardForId(id), int(id % VectorIndex::shardCount));
    }

    void float16RoundTrip()
    {
        const QVector<float> original{0.0f, 1.0f, -1.0f, 0.125f, 0.3333f, -0.8191f, 65504.0f};
        const QByteArray encoded = encodeFloat16(original);
        QCOMPARE(encoded.size(), original.size() * 2);
        QCOMPARE(encodeFloat16({1.0f, -1.0f}), QByteArray::fromHex("003c00bc"));
        QString error;
        const QVector<float> restored = decodeFloat16(encoded, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(restored.size(), original.size());
        for (qsizetype i = 0; i < original.size(); ++i)
            QVERIFY2(std::abs(restored.at(i) - original.at(i)) <= std::max(0.0005f, std::abs(original.at(i)) * 0.001f),
                     qPrintable(QStringLiteral("FP16 value differs at %1").arg(i)));
        QVERIFY(encodeFloat16({}).isEmpty());
        QVERIFY(decodeFloat16({}, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(decodeFloat16(QByteArray(1, '\0'), &error).isEmpty());
        QVERIFY(!error.isEmpty());
        error.clear();
        QVERIFY(decodeFloat16(QByteArray::fromHex("007e"), &error).isEmpty());
        QVERIFY(!error.isEmpty());
        QVERIFY_EXCEPTION_THROWN(encodeFloat16({std::numeric_limits<float>::quiet_NaN()}), std::invalid_argument);
        QVERIFY_EXCEPTION_THROWN(encodeFloat16({std::numeric_limits<float>::infinity()}), std::invalid_argument);
        QVERIFY_EXCEPTION_THROWN(encodeFloat16({65536.0f}), std::invalid_argument);
    }
};

QTEST_GUILESS_MAIN(VectorIndexTest)
#include "vector_index_test.moc"
