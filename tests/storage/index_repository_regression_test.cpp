#include "storage/index_repository.h"
#include "support/test_environment.h"

#include <QDir>
#include <QDirIterator>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QStandardPaths>
#include <QtTest>

#include <cmath>
#include <numbers>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

namespace {

Segment makeSegment(const QString &text, const QVector<float> &embedding = {})
{
    Segment segment;
    segment.kind = QStringLiteral("text");
    segment.modality = QStringLiteral("text");
    segment.text = text;
    segment.embedding = embedding;
    return segment;
}

QSet<QString> distinctPaths(const QList<SearchHit> &hits)
{
    QSet<QString> paths;
    for (const auto &hit : hits)
        paths.insert(hit.path);
    return paths;
}

qint64 directoryBytes(const QString &root)
{
    qint64 bytes = 0;
    QDirIterator files(root, QDir::Files | QDir::NoSymLinks, QDirIterator::Subdirectories);
    while (files.hasNext()) {
        files.next();
        bytes += files.fileInfo().size();
    }
    return bytes;
}

} // namespace

class IndexRepositoryRegressionTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
    }

    void keywordLimitCountsFilesInsteadOfDominantFileSegments()
    {
        TestEnvironment environment(QStringLiteral("fts-regression"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));

        const QString dominant = environment.path(QStringLiteral("a.txt"));
        const QString otherOne = environment.path(QStringLiteral("b.txt"));
        const QString otherTwo = environment.path(QStringLiteral("c.txt"));
        QVector<Segment> repeated(300, makeSegment(QStringLiteral("needle")));
        // The shorter dominant-file segments outrank the padded alternatives in
        // BM25. A fixed 256-segment cut would return only the dominant file.
        const QString padded = QStringLiteral("needle ") + QStringLiteral("padding ").repeated(400);
        QVERIFY2(repository.replaceFiles({
            {dominant, QStringLiteral("one"), false, repeated},
            {otherOne, QStringLiteral("one"), false, {makeSegment(padded)}},
            {otherTwo, QStringLiteral("one"), false, {makeSegment(padded)}}
        }, &error), qPrintable(error));

        SearchOptions options;
        options.query = QStringLiteral("needle");
        options.limit = 2;
        const auto twoFiles = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(twoFiles.size(), qsizetype(2));
        QCOMPARE(distinctPaths(twoFiles).size(), qsizetype(2));
        QCOMPARE(twoFiles.first().path, dominant);
        QVERIFY(twoFiles.last().path == otherOne || twoFiles.last().path == otherTwo);
        options.limit = 3;
        QCOMPARE(distinctPaths(repository.search(options, &error)),
                 (QSet<QString>{dominant, otherOne, otherTwo}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void scatteredCommonWordsDoNotDisplaceStrongSemanticImage()
    {
        TestEnvironment environment(QStringLiteral("conjunctive-hybrid"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));

        QVector<float> queryVector(embeddingDimensions, 0.0f);
        queryVector[0] = 1.0f;
        QVector<float> weakVector(embeddingDimensions, 0.0f);
        weakVector[0] = 0.1f;
        weakVector[1] = float(std::sqrt(0.99));
        Segment image = makeSegment({}, queryVector);
        image.kind = QStringLiteral("image");
        image.modality = QStringLiteral("image");
        const QString imagePath = environment.path(QStringLiteral("900.png"));
        QVector<IndexedFile> files{{imagePath, QStringLiteral("one"), false, {image}}};
        const QStringList fragments{QStringLiteral("where"), QStringLiteral("can"),
                                    QStringLiteral("a"), QStringLiteral("beside"),
                                    QStringLiteral("bicycle")};
        for (int index = 0; index < 15; ++index) {
            files.append({environment.path(QString::number(index) + QStringLiteral(".txt")),
                          QStringLiteral("one"), false,
                          {makeSegment(fragments[index % fragments.size()], weakVector)}});
        }
        QVERIFY2(repository.replaceFiles(files, &error), qPrintable(error));

        SearchOptions options;
        options.query = QStringLiteral("where can I find a bicycle beside a bridge");
        options.queryEmbedding = queryVector;
        options.limit = 10;
        const auto hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(10));
        // Each distractor matches part of the question but not its conjunction.
        // An OR keyword branch would give all 15 weak text files a second RRF
        // contribution and push the strongest image entirely out of the top 10.
        QCOMPARE(hits.first().path, imagePath);
        QCOMPARE(hits.first().matchSource, QStringLiteral("semantic"));
        for (const auto &hit : hits)
            QCOMPARE(hit.matchSource, QStringLiteral("semantic"));
    }

    void keywordSearchRequiresEveryWordRegardlessOfOrder()
    {
        TestEnvironment environment(QStringLiteral("conjunctive-keyword"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString complete = environment.path(QStringLiteral("100.txt"));
        const QString reordered = environment.path(QStringLiteral("101.txt"));
        QVERIFY2(repository.replaceFiles({
            {complete, QStringLiteral("one"), false, {makeSegment(QStringLiteral("cobalt bicycle"))}},
            {reordered, QStringLiteral("one"), false, {makeSegment(QStringLiteral("bicycle painted cobalt"))}},
            {environment.path(QStringLiteral("102.txt")), QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("cobalt paint"))}},
            {environment.path(QStringLiteral("103.txt")), QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("bicycle wheel"))}},
            {environment.path(QStringLiteral("104.txt")), QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("unrelated document"))}}
        }, &error), qPrintable(error));
        SearchOptions options;
        options.query = QStringLiteral("cobalt bicycle");
        const auto hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(distinctPaths(hits), (QSet<QString>{complete, reordered}));
        QCOMPARE(hits.size(), qsizetype(2));
        for (const auto &hit : hits)
            QCOMPARE(hit.matchSource, QStringLiteral("keyword"));
    }

    void mixedChineseAndLatinKeywordSearchRequiresEveryCondition()
    {
        TestEnvironment environment(QStringLiteral("conjunctive-mixed"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString complete = environment.path(QStringLiteral("200.txt"));
        const QString reordered = environment.path(QStringLiteral("201.txt"));
        QVERIFY2(repository.replaceFiles({
            {complete, QStringLiteral("one"), false, {makeSegment(QStringLiteral("北京 invoice 复核"))}},
            {reordered, QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("复核后的 invoice 位于北京"))}},
            {environment.path(QStringLiteral("202.txt")), QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("北京 invoice 已归档"))}},
            {environment.path(QStringLiteral("203.txt")), QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("北京 report 复核"))}},
            {environment.path(QStringLiteral("204.txt")), QStringLiteral("one"), false,
             {makeSegment(QStringLiteral("上海 invoice 复核"))}}
        }, &error), qPrintable(error));
        SearchOptions options;
        options.query = QStringLiteral("北京 invoice 复核");
        const auto hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(distinctPaths(hits), (QSet<QString>{complete, reordered}));
        QCOMPARE(hits.size(), qsizetype(2));
        for (const auto &hit : hits)
            QCOMPARE(hit.matchSource, QStringLiteral("keyword"));
    }

    void rootFilterRespectsCaseSensitivePlatformKeys()
    {
#ifdef Q_OS_WIN
        QSKIP("Non-Windows path keys are case-sensitive. Windows intentionally folds path keys.");
#else
        TestEnvironment environment(QStringLiteral("case-regression"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString upperRoot = environment.path(QStringLiteral("A"));
        const QString lowerRoot = environment.path(QStringLiteral("a"));
        const QString upper = QDir(upperRoot).filePath(QStringLiteral("upper.txt"));
        const QString lower = QDir(lowerRoot).filePath(QStringLiteral("lower.txt"));
        // These are indexed metadata paths only. Creating the source directories
        // would collapse them on case-insensitive macOS filesystems and obscure
        // the repository's explicit non-Windows path-key contract.
        QVERIFY2(repository.replaceFiles({
            {upper, QStringLiteral("one"), false, {makeSegment(QStringLiteral("sharedneedle"))}},
            {lower, QStringLiteral("one"), false, {makeSegment(QStringLiteral("sharedneedle"))}}
        }, &error), qPrintable(error));
        SearchOptions options;
        options.query = QStringLiteral("sharedneedle");
        QCOMPARE(repository.search(options, &error).size(), qsizetype(2));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        options.root = upperRoot;
        QCOMPARE(distinctPaths(repository.search(options, &error)), (QSet<QString>{upper}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        options.root = lowerRoot;
        QCOMPARE(distinctPaths(repository.search(options, &error)), (QSet<QString>{lower}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
#endif
    }

    void folderRulesApplyBeforeTheResultLimit_data()
    {
        QTest::addColumn<bool>("semantic");
        QTest::newRow("keyword") << false;
        QTest::newRow("exact-semantic") << true;
    }

    void folderRulesApplyBeforeTheResultLimit()
    {
        QFETCH(bool, semantic);
        TestEnvironment environment(QStringLiteral("folders-limit"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString selectedRoot = environment.path(QStringLiteral("selected"));
        const QString excludedRoot = QDir(selectedRoot).filePath(QStringLiteral("blocked"));
        const QString excludedFile = QDir(excludedRoot).filePath(QStringLiteral("a.txt"));
        const QString validFile = QDir(selectedRoot).filePath(QStringLiteral("kept/b.txt"));
        QVector<float> nearest(embeddingDimensions, 0.0f);
        nearest[0] = 1.0f;
        QVector<float> farther(embeddingDimensions, 0.0f);
        farther[0] = 0.8f;
        farther[1] = 0.6f;
        const QString padded = QStringLiteral("needle ") + QStringLiteral("padding ").repeated(400);
        QVERIFY2(repository.replaceFiles({
            {excludedFile, QStringLiteral("one"), false, {makeSegment(QStringLiteral("needle"), nearest)}},
            {validFile, QStringLiteral("one"), false, {makeSegment(padded, farther)}}
        }, &error), qPrintable(error));

        SearchOptions options;
        options.limit = 1;
        if (semantic)
            options.queryEmbedding = nearest;
        else
            options.query = QStringLiteral("needle");
        const auto withoutRules = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(withoutRules.size(), qsizetype(1));
        QCOMPARE(withoutRules.first().path, excludedFile);

        options.allowedRoots = {selectedRoot};
        options.excludedRoots = {excludedRoot};
        options.enforceFolderRules = true;
        const auto withRules = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(withRules.size(), qsizetype(1));
        QCOMPARE(withRules.first().path, validFile);
        options.allowedRoots.clear();
        QVERIFY(repository.search(options, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void explicitlySelectedNestedRootOverridesAncestorAndSelfExclusions()
    {
        TestEnvironment environment(QStringLiteral("folders-nested"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString outerRoot = environment.path(QStringLiteral("top"));
        const QString excludedAncestor = QDir(outerRoot).filePath(QStringLiteral("hidden"));
        const QString nestedRoot = QDir(excludedAncestor).filePath(QStringLiteral("selected"));
        const QString excludedChild = QDir(nestedRoot).filePath(QStringLiteral("blocked"));
        const QString outerVisible = QDir(outerRoot).filePath(QStringLiteral("normal.txt"));
        const QString nestedVisible = QDir(nestedRoot).filePath(QStringLiteral("keep.txt"));
        const QString prefixNeighbor = QDir(nestedRoot).filePath(QStringLiteral("blocked-extra/visible.txt"));
        const QString explicitlyExcludedFile = QDir(nestedRoot).filePath(QStringLiteral("denied.txt"));
        const QString childHidden = QDir(excludedChild).filePath(QStringLiteral("secret.txt"));
        const QString ancestorHidden = QDir(excludedAncestor).filePath(QStringLiteral("other.txt"));
        QVector<float> embedding(embeddingDimensions, 0.0f);
        embedding[0] = 1.0f;
        QVector<IndexedFile> files;
        for (const auto &path : {outerVisible, nestedVisible, prefixNeighbor, explicitlyExcludedFile, childHidden, ancestorHidden})
            files.append({path, QStringLiteral("one"), false, {makeSegment(QStringLiteral("needle"), embedding)}});
        QVERIFY2(repository.replaceFiles(files, &error), qPrintable(error));

        // Match FileCatalog::scan: each explicitly selected root ignores an
        // exclusion at or above itself, while strict descendants remain active.
        for (const bool semantic : {false, true}) {
            SearchOptions options;
            if (semantic)
                options.queryEmbedding = embedding;
            else
                options.query = QStringLiteral("needle");
            options.enforceFolderRules = true;
            options.allowedRoots = {outerRoot, nestedRoot};
            options.excludedRoots = {excludedAncestor, nestedRoot, excludedChild, explicitlyExcludedFile};
            QCOMPARE(distinctPaths(repository.search(options, &error)),
                     (QSet<QString>{outerVisible, nestedVisible, prefixNeighbor}));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            options.allowedRoots = {nestedRoot};
            QCOMPARE(distinctPaths(repository.search(options, &error)),
                     (QSet<QString>{nestedVisible, prefixNeighbor}));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            options.excludedRoots.append(nestedVisible);
            options.excludedRoots.append(prefixNeighbor);
            QVERIFY(repository.search(options, &error).isEmpty());
            QVERIFY2(error.isEmpty(), qPrintable(error));
        }
    }

    void annFileAggregationAndPendingTombstones()
    {
        TestEnvironment environment(QStringLiteral("ann-regression"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QElapsedTimer elapsed;
        elapsed.start();

        constexpr int dominantSegments = 300;
        // Keep this strictly above the repository's 20,000-vector exact cutoff
        // after deleting the dominant file, so both assertions exercise ANN.
        constexpr int liveFiles = 20001;
        constexpr int codebookSize = 257;
        constexpr int batchSize = 512;
        QVector<float> queryVector(embeddingDimensions, 0.0f);
        queryVector[0] = 1.0f;
        QVector<QVector<float>> codebook;
        codebook.reserve(codebookSize);
        for (int index = 0; index < codebookSize; ++index) {
            const double angle = 2.0 * std::numbers::pi * index / codebookSize;
            QVector<float> vector(embeddingDimensions, 0.0f);
            vector[0] = 0.96f;
            vector[1] = float(0.28 * std::cos(angle));
            vector[2] = float(0.28 * std::sin(angle));
            codebook.append(std::move(vector));
        }
        const QString dominant = environment.path(QStringLiteral("a"));
        const QString liveRoot = environment.path(QStringLiteral("v"));
        QVector<IndexedFile> batch;
        batch.reserve(batchSize);
        batch.append({dominant, QStringLiteral("one"), false,
                      QVector<Segment>(dominantSegments, makeSegment({}, queryVector))});
        for (int index = 0; index < liveFiles; ++index) {
            // QVector sharing keeps the fixture's input vectors bounded. The
            // 257-entry codebook avoids one giant identical-vector HNSW cluster
            // and has a different period from the 16-way shard assignment.
            batch.append({QDir(liveRoot).filePath(QString::number(index)),
                          QStringLiteral("one"), false,
                          {makeSegment({}, codebook[index % codebookSize])}});
            if (batch.size() >= batchSize) {
                QVERIFY2(repository.replaceFiles(batch, &error, true), qPrintable(error));
                batch.clear();
            }
        }
        if (!batch.isEmpty())
            QVERIFY2(repository.replaceFiles(batch, &error, true), qPrintable(error));
        batch.clear();
        QVERIFY2(repository.flush(&error), qPrintable(error));
        const auto ready = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(ready.value(QStringLiteral("files")).toInteger(), qint64(liveFiles + 1));
        QCOMPARE(ready.value(QStringLiteral("vectors")).toInteger(), qint64(liveFiles + dominantSegments));
        QCOMPARE(ready.value(QStringLiteral("annVectors")).toInteger(), qint64(liveFiles + dominantSegments));
        QVERIFY(!ready.value(QStringLiteral("deferredMaintenance")).toBool());

        SearchOptions options;
        options.queryEmbedding = queryVector;
        options.limit = 2;
        // No text, root or kind filter: FTS and allow-list construction must not
        // accidentally hide either regression in the unfiltered ANN path.
        const auto beforeDeletion = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(beforeDeletion.size(), qsizetype(2));
        QCOMPARE(distinctPaths(beforeDeletion).size(), qsizetype(2));
        QCOMPARE(beforeDeletion.first().path, dominant);
        QVERIFY(beforeDeletion.last().path.startsWith(liveRoot + u'/'));

        // Excluding the dominant file still leaves 20,001 vectors, so folder
        // enforcement must build the ANN allow-list instead of dropping the
        // original top result after its limit or falling into the exact path.
        SearchOptions folderFiltered = options;
        folderFiltered.limit = 1;
        folderFiltered.enforceFolderRules = true;
        folderFiltered.allowedRoots = {environment.root()};
        folderFiltered.excludedRoots = {dominant};
        const auto excludedDominant = repository.search(folderFiltered, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(excludedDominant.size(), qsizetype(1));
        QVERIFY(excludedDominant.first().path.startsWith(liveRoot + u'/'));
        folderFiltered.allowedRoots = {liveRoot};
        folderFiltered.excludedRoots.clear();
        const auto explicitlySelected = repository.search(folderFiltered, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(explicitlySelected.size(), qsizetype(1));
        QVERIFY(explicitlySelected.first().path.startsWith(liveRoot + u'/'));
        folderFiltered.allowedRoots.clear();
        QVERIFY(repository.search(folderFiltered, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));

        QVERIFY2(repository.removeFile(dominant, &error), qPrintable(error));
        const auto afterRemoval = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(afterRemoval.value(QStringLiteral("vectors")).toInteger(), qint64(liveFiles));
        // Do not flush or reopen here. The original near-neighbor IDs must still
        // exist in the immutable ANN graph and be excluded during traversal.
        QCOMPARE(afterRemoval.value(QStringLiteral("annVectors")).toInteger(), qint64(liveFiles + dominantSegments));
        const auto afterDeletion = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(afterDeletion.size(), qsizetype(2));
        QCOMPARE(distinctPaths(afterDeletion).size(), qsizetype(2));
        for (const auto &hit : afterDeletion) {
            QVERIFY(hit.path != dominant);
            QVERIFY(hit.path.startsWith(liveRoot + u'/'));
            QCOMPARE(hit.matchSource, QStringLiteral("semantic"));
            QCOMPARE(repository.fileVersion(hit.path, false, &error), QStringLiteral("one"));
            QVERIFY2(error.isEmpty(), qPrintable(error));
        }
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
        qInfo().noquote() << "ANN aggregation/tombstone fixture:" << liveFiles
                          << "live vectors," << dominantSegments << "pending deletions,"
                          << elapsed.elapsed() << "ms, observed logical file bytes"
                          << directoryBytes(environment.root());
    }
};

QTEST_GUILESS_MAIN(IndexRepositoryRegressionTest)
#include "index_repository_regression_test.moc"
