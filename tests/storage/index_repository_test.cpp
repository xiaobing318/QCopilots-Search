#include "storage/index_repository.h"
#include "support/test_environment.h"
#include "support/sqlite_database.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QScopeGuard>
#include <QtTest>
#include <sqlite3.h>

#include <atomic>
#include <cmath>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

namespace {

QVector<float> basis(int coordinate)
{
    QVector<float> result(embeddingDimensions, 0.0f);
    result[coordinate] = 1.0f;
    return result;
}

Segment segment(const QString &text, const QString &kind = QStringLiteral("document"),
                const QVector<float> &embedding = {})
{
    Segment result;
    result.text = text;
    result.title = QStringLiteral("Test fixture");
    result.kind = kind;
    result.modality = QStringLiteral("text");
    result.embedding = embedding;
    return result;
}

SearchOptions query(const QString &text)
{
    SearchOptions result;
    result.query = text;
    return result;
}

QSet<QString> hitPaths(const QList<SearchHit> &hits)
{
    QSet<QString> result;
    for (const SearchHit &hit : hits)
        result.insert(hit.path);
    return result;
}

QJsonArray segmentSnapshots(const QVector<Segment> &segments)
{
    QJsonArray result;
    for (const auto &segment : segments)
        result.append(segment.toJson());
    return result;
}

// Inspect the actual repository using raw SQL without a developer SDK or CLI.
bool executeSql(const QString &path, const char *statement, QString *error)
{
    sqlite3 *raw = nullptr;
    if (test::openSqliteDatabase(path, &raw,
                                SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE) != SQLITE_OK) {
        *error = raw ? QString::fromUtf8(sqlite3_errmsg(raw))
                     : QStringLiteral("SQLite did not return a database handle");
        if (raw)
            sqlite3_close(raw);
        return false;
    }
    std::unique_ptr<sqlite3, decltype(&sqlite3_close)> database(raw, sqlite3_close);
    char *message = nullptr;
    const int result = sqlite3_exec(database.get(), statement, nullptr, nullptr, &message);
    if (result != SQLITE_OK)
        *error = message ? QString::fromUtf8(message)
                         : QString::fromUtf8(sqlite3_errmsg(database.get()));
    sqlite3_free(message);
    return result == SQLITE_OK;
}

} // namespace

class IndexRepositoryTest : public QObject
{
    Q_OBJECT

private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void emptyRepository()
    {
        TestEnvironment environment(QStringLiteral("storage-empty"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QCOMPARE(repository.databasePath(), environment.runtimePaths().databaseFile());
        QVERIFY(QFileInfo::exists(repository.databasePath()));
        const auto hits = repository.search(query(QStringLiteral("missing")), &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(hits.isEmpty());
        const QJsonObject status = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(status.value(QStringLiteral("files")).toInteger(), qint64(0));
        QCOMPARE(status.value(QStringLiteral("segments")).toInteger(), qint64(0));
        QCOMPARE(status.value(QStringLiteral("vectors")).toInteger(), qint64(0));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void keywordSearch_data()
    {
        QTest::addColumn<QString>("contents");
        QTest::addColumn<QString>("needle");
        QTest::newRow("english") << QStringLiteral("An orchid blooms beside the window")
                                 << QStringLiteral("orchid");
        QTest::newRow("english-case") << QStringLiteral("A DISTINCTIVE Orchid")
                                      << QStringLiteral("ORCHID");
        QTest::newRow("simplified-chinese") << QString::fromUtf8("北京春天公园的照片")
                                            << QString::fromUtf8("北京");
        QTest::newRow("traditional-chinese") << QString::fromUtf8("臺北音樂會的錄音")
                                             << QString::fromUtf8("音樂");
        QTest::newRow("single-han") << QString::fromUtf8("猫在窗边睡觉")
                                    << QString::fromUtf8("猫");
        QTest::newRow("mixed-languages") << QString::fromUtf8("会议记录关于 Vector 数据库")
                                         << QStringLiteral("vector");
    }

    void keywordSearch()
    {
        QFETCH(QString, contents);
        QFETCH(QString, needle);
        TestEnvironment environment(QStringLiteral("storage-fts"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QString::fromUtf8("资料 空格/文档.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"), {segment(contents)}, false, &error),
                 qPrintable(error));
        const auto hits = repository.search(query(needle), &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().path, path);
        QCOMPARE(hits.first().text, contents);
        QVERIFY(hits.first().segmentId > 0);
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("v1"));
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void filtersRespectKindRootAndCode()
    {
        TestEnvironment environment(QStringLiteral("storage-filters"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString root = environment.path(QString::fromUtf8("资料%_ 空格"));
        const QString document = QDir(root).filePath(QStringLiteral("article.txt"));
        const QString image = QDir(root).filePath(QStringLiteral("image.png"));
        const QString prefixNeighbour = environment.path(QString::fromUtf8("资料%_ 空格-more/other.txt"));
        const QString code = QDir(root).filePath(QStringLiteral("source.cpp"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFiles({
            {document, QStringLiteral("v1"), false, {segment(QStringLiteral("sharedtoken document"))}},
            {image, QStringLiteral("v1"), false, {segment(QStringLiteral("sharedtoken image"), QStringLiteral("image"))}},
            {prefixNeighbour, QStringLiteral("v1"), false, {segment(QStringLiteral("sharedtoken neighbour"))}},
            {code, QStringLiteral("v1"), true, {segment(QStringLiteral("sharedtoken source"), QStringLiteral("code"))}}
        }, &error), qPrintable(error));

        SearchOptions options = query(QStringLiteral("sharedtoken"));
        QCOMPARE(hitPaths(repository.search(options, &error)), (QSet<QString>{document, image, prefixNeighbour}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        options.root = root;
        QCOMPARE(hitPaths(repository.search(options, &error)), (QSet<QString>{document, image}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        options.kind = QStringLiteral("image");
        QCOMPARE(hitPaths(repository.search(options, &error)), (QSet<QString>{image}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        options.kind.clear();
        options.code = true;
        QCOMPARE(hitPaths(repository.search(options, &error)), (QSet<QString>{code}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(repository.fileVersion(code, true, &error), QStringLiteral("v1"));
        QVERIFY(repository.fileVersion(code, false, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void replacementAndRemovalRetireOldSegments()
    {
        TestEnvironment environment(QStringLiteral("storage-replace"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("files/article.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("old"),
                    {segment(QStringLiteral("retiredword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        QVERIFY2(repository.flush(&error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("new"),
                    {segment(QStringLiteral("currentword"), QStringLiteral("document"), basis(1))}, false, &error),
                 qPrintable(error));
        QVERIFY(repository.search(query(QStringLiteral("retiredword")), &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const auto current = repository.search(query(QStringLiteral("currentword")), &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(current.size(), qsizetype(1));
        QCOMPARE(current.first().text, QStringLiteral("currentword"));
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("new"));
        QCOMPARE(repository.status(&error).value(QStringLiteral("segments")).toInteger(), qint64(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.removeFile(path, &error), qPrintable(error));
        QVERIFY(repository.search(query(QStringLiteral("currentword")), &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(1);
        QVERIFY(repository.search(semantic, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(repository.fileVersion(path, false, &error).isEmpty());
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
        QVERIFY2(repository.removeFile(path, &error), qPrintable(error));
    }

    void pruneOnlyAffectsMissingFilesInsideRoot()
    {
        TestEnvironment environment(QStringLiteral("storage-prune"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString root = environment.path(QStringLiteral("root"));
        const QString keep = QDir(root).filePath(QStringLiteral("keep.txt"));
        const QString removed = QDir(root).filePath(QStringLiteral("nested/removed.txt"));
        const QString outside = environment.path(QStringLiteral("root-extra/keep.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        for (const QString &path : {keep, removed, outside}) {
            QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"),
                        {segment(QStringLiteral("pruneword"))}, false, &error), qPrintable(error));
        }
        QVERIFY2(repository.prune(root, {keep}, &error), qPrintable(error));
        QCOMPARE(hitPaths(repository.search(query(QStringLiteral("pruneword")), &error)),
                 (QSet<QString>{keep, outside}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(repository.fileVersion(removed, false, &error).isEmpty());
        QCOMPARE(repository.fileVersion(outside, false, &error), QStringLiteral("v1"));
        QVERIFY2(repository.prune(root, {}, &error), qPrintable(error));
        QCOMPARE(hitPaths(repository.search(query(QStringLiteral("pruneword")), &error)),
                 (QSet<QString>{outside}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void locationAndFileAggregation()
    {
        TestEnvironment environment(QStringLiteral("storage-locations"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("book.pdf"));
        Segment first = segment(QStringLiteral("locatable passage"), QStringLiteral("pdf"));
        first.page = 3;
        first.lineStart = 8;
        first.lineEnd = 12;
        first.cell = 2;
        first.startMs = 1500;
        first.endMs = 2700;
        Segment second = first;
        second.page = 4;
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"), {first, second}, false, &error), qPrintable(error));
        const auto hits = repository.search(query(QStringLiteral("locatable")), &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        const SearchHit &hit = hits.first();
        QVERIFY(hit.page == 3 || hit.page == 4);
        QCOMPARE(hit.lineStart, 8);
        QCOMPARE(hit.lineEnd, 12);
        QCOMPARE(hit.cell, 2);
        QCOMPARE(hit.startMs, qint64(1500));
        QCOMPARE(hit.endMs, qint64(2700));
        QCOMPARE(hit.kind, QStringLiteral("pdf"));
    }

    void semanticSearchAppliesFiltersBeforeLimit()
    {
        TestEnvironment environment(QStringLiteral("storage-semantic"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString image = environment.path(QStringLiteral("images/target.png"));
        const QString text = environment.path(QStringLiteral("documents/nearest.txt"));
        QVector<float> near = basis(0);
        near[0] = 0.8f;
        near[1] = 0.6f;
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFiles({
            {text, QStringLiteral("v1"), false, {segment(QStringLiteral("nearest"), QStringLiteral("document"), basis(0))}},
            {image, QStringLiteral("v1"), false, {segment(QStringLiteral("target"), QStringLiteral("image"), near)}}
        }, &error), qPrintable(error));
        SearchOptions options;
        options.queryEmbedding = basis(0);
        options.limit = 1;
        const auto all = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(all.size(), qsizetype(1));
        QCOMPARE(all.first().path, text);
        options.kind = QStringLiteral("image");
        for (int phase = 0; phase < 2; ++phase) {
            const auto filtered = repository.search(options, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(filtered.size(), qsizetype(1));
            QCOMPARE(filtered.first().path, image);
            if (phase == 0)
                QVERIFY2(repository.flush(&error), qPrintable(error));
        }
    }

    void pipelineChangeInvalidatesVectorsButPreservesKeywordSearch()
    {
        TestEnvironment environment(QStringLiteral("storage-fingerprint"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("retained.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("pipeline-one"), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"),
                    {segment(QStringLiteral("retainedword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        QVERIFY2(repository.flush(&error), qPrintable(error));
        QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("pipeline-one"), &error), qPrintable(error));
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("v1"));
        QCOMPARE(repository.status(&error).value(QStringLiteral("vectors")).toInteger(), qint64(1));
        QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("pipeline-two"), &error), qPrintable(error));
        QCOMPARE(repository.pipelineFingerprint(&error), QStringLiteral("pipeline-two"));
        QVERIFY(repository.fileVersion(path, false, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const QJsonObject status = repository.status(&error);
        QCOMPARE(status.value(QStringLiteral("files")).toInteger(), qint64(1));
        QCOMPARE(status.value(QStringLiteral("vectors")).toInteger(), qint64(0));
        QCOMPARE(repository.search(query(QStringLiteral("retainedword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(0);
        QVERIFY(repository.search(semantic, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void pipelineCommitFailureRollsBackAllAuthoritativeState()
    {
        TestEnvironment environment(QStringLiteral("storage-pipeline-rollback"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        bool failCommit = false;
        StorageOptions options;
        options.faultHook = [&](const QString &point) {
            if (failCommit && point == QStringLiteral("before-pipeline-commit"))
                throw std::runtime_error("Injected pipeline commit failure");
        };
        IndexRepository repository(options);
        QString error;
        const QString path = environment.path(QStringLiteral("stable.txt"));
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("stable-pipeline"), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("source|stable-pipeline"),
                    {segment(QStringLiteral("survivorword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        QVERIFY2(repository.flush(&error), qPrintable(error));
        const auto before = repository.status(&error);
        failCommit = true;
        QVERIFY(!repository.setPipelineFingerprint(QStringLiteral("new-pipeline"), &error));
        QVERIFY(error.contains(QStringLiteral("Injected pipeline commit failure")));
        QCOMPARE(repository.pipelineFingerprint(&error), QStringLiteral("stable-pipeline"));
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("source|stable-pipeline"));
        const auto after = repository.status(&error);
        QCOMPARE(after.value(QStringLiteral("generation")), before.value(QStringLiteral("generation")));
        QCOMPARE(after.value(QStringLiteral("vectors")), before.value(QStringLiteral("vectors")));
        QCOMPARE(after.value(QStringLiteral("pendingShards")), before.value(QStringLiteral("pendingShards")));
        auto search = query(QStringLiteral("survivorword"));
        search.queryEmbedding = basis(0);
        const auto hits = repository.search(search, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().sourceVersion, QStringLiteral("source|stable-pipeline"));
        QCOMPARE(repository.fileSegments(path, false, &error).first().embedding.size(), embeddingDimensions);
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
        failCommit = false;
        QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("new-pipeline"), &error), qPrintable(error));
        QCOMPARE(repository.pipelineFingerprint(&error), QStringLiteral("new-pipeline"));
        QCOMPARE(repository.status(&error).value(QStringLiteral("vectors")).toInteger(), qint64(0));
        QVERIFY(repository.fileVersion(path, false, &error).isEmpty());
        QCOMPARE(repository.search(query(QStringLiteral("survivorword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void invalidEmbedding_data()
    {
        QTest::addColumn<QVector<float>>("embedding");
        QTest::newRow("short-dimension") << QVector<float>{1.0f, 0.0f};
        QTest::newRow("long-dimension") << QVector<float>(embeddingDimensions + 1, 1.0f);
        QTest::newRow("zero") << QVector<float>(embeddingDimensions, 0.0f);
        auto value = basis(0);
        value[5] = std::numeric_limits<float>::quiet_NaN();
        QTest::newRow("nan") << value;
        value[5] = std::numeric_limits<float>::infinity();
        QTest::newRow("infinity") << value;
    }

    void invalidEmbedding()
    {
        QFETCH(QVector<float>, embedding);
        TestEnvironment environment(QStringLiteral("storage-invalid-vector"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("stable.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"),
                    {segment(QStringLiteral("stableword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        QVERIFY(!repository.replaceFile(path, QStringLiteral("v2"),
                    {segment(QStringLiteral("invalidword"), QStringLiteral("document"), embedding)}, false, &error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("v1"));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(repository.search(query(QStringLiteral("stableword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(repository.search(query(QStringLiteral("invalidword")), &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

#ifdef Q_OS_WIN
    void longUnicodeProfile_data()
    {
        QTest::addColumn<int>("minimumUtf16Units");
        QTest::addColumn<int>("minimumUtf8Bytes");
        QTest::newRow("beyond-win32-max-path") << 360 << 0;
        // SQLite's Windows VFS byte limit is independent of Windows UTF-16
        // path lengths. Other platforms, including macOS, have lower limits.
        QTest::newRow("beyond-default-sqlite-byte-budget") << 360 << 1300;
    }

    void longUnicodeProfile()
    {
        QFETCH(int, minimumUtf16Units);
        QFETCH(int, minimumUtf8Bytes);
        TestEnvironment environment(QStringLiteral("storage-long-profile"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QString profile = environment.path(QStringLiteral("中文 空格 profile"));
        // Every component remains below filesystem component limits. Grow the
        // actual profile rather than assuming a particular developer root.
        const QString component = QStringLiteral("中文 空格 ") + QString(36, u'a');
        while (profile.size() < minimumUtf16Units || profile.toUtf8().size() < minimumUtf8Bytes)
            profile = QDir(profile).filePath(component);
        const auto paths = RuntimePaths::resolve(profile, environment.path(QStringLiteral("models")));
        QVERIFY(paths.profileDir.size() >= minimumUtf16Units);
        QVERIFY(paths.profileDir.toUtf8().size() >= minimumUtf8Bytes);
        const QString source = environment.path(QStringLiteral("资料 中文.txt"));
        QString error;
        QVERIFY2(environment.writeFile(QStringLiteral("资料 中文.txt"), "longprofilecanary", &error),
                 qPrintable(error));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QCOMPARE(repository.databasePath(), paths.databaseFile());
            QVERIFY2(repository.replaceFile(source, QStringLiteral("revision"),
                         {segment(QStringLiteral("长路径 longprofilecanary"), QStringLiteral("document"), basis(0))},
                         false, &error), qPrintable(error));
            QCOMPARE(repository.search(query(QStringLiteral("longprofilecanary")), &error).size(), qsizetype(1));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QVERIFY2(repository.flush(&error), qPrintable(error));
            QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
        }
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        QCOMPARE(reopened.fileVersion(source, false, &error), QStringLiteral("revision"));
        QCOMPARE(reopened.search(query(QStringLiteral("longprofilecanary")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(0);
        const auto hits = reopened.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().path, source);
        QVERIFY2(reopened.checkIntegrity(&error), qPrintable(error));
    }
#endif

    void reopenPreservesIndexAndIntegrity()
    {
        TestEnvironment environment(QStringLiteral("storage-reopen"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = RuntimePaths::resolve(environment.path(QString::fromUtf8("配置 空格/索引实例")),
                                                 environment.path(QStringLiteral("models")));
        const QString path = environment.path(QString::fromUtf8("资料/中文 document.txt"));
        QString error;
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("persisted-pipeline"), &error), qPrintable(error));
            QVERIFY2(repository.replaceFile(path, QStringLiteral("revision"),
                        {segment(QString::fromUtf8("持久化 orchid"), QStringLiteral("document"), basis(0))}, false, &error),
                     qPrintable(error));
            QVERIFY2(repository.flush(&error), qPrintable(error));
        }
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        QCOMPARE(reopened.pipelineFingerprint(&error), QStringLiteral("persisted-pipeline"));
        QCOMPARE(reopened.fileVersion(path, false, &error), QStringLiteral("revision"));
        QCOMPARE(reopened.search(query(QStringLiteral("orchid")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(0);
        const auto hits = reopened.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().path, path);
        QVERIFY2(reopened.checkIntegrity(&error), qPrintable(error));
    }

    void diskReserveFailureLeavesCommittedFileUntouched()
    {
        TestEnvironment environment(QStringLiteral("storage-disk-reserve"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        const QString path = environment.path(QStringLiteral("stable.txt"));
        qint64 available = std::numeric_limits<qint64>::max() / 2;
        QStringList inspectedPaths;
        StorageOptions options;
        options.diskReserveBytes = 1024;
        options.availableBytes = [&](const QString &volumePath) {
            inspectedPaths.append(volumePath);
            return available;
        };
        IndexRepository repository(options);
        QString error;
        QVERIFY2(repository.open(paths, &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"),
                    {segment(QStringLiteral("stableword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        const auto before = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));

        // The reserve itself fits, but the pending write does not. No real disk
        // needs to be filled to verify this error and recovery path.
        available = options.diskReserveBytes;
        QVERIFY(!repository.replaceFile(path, QStringLiteral("v2"),
                    {segment(QStringLiteral("uncommittedword"), QStringLiteral("document"), basis(1))}, false, &error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("v1"));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const auto after = repository.status(&error);
        QCOMPARE(after.value(QStringLiteral("generation")), before.value(QStringLiteral("generation")));
        QCOMPARE(after.value(QStringLiteral("segments")), before.value(QStringLiteral("segments")));
        QCOMPARE(after.value(QStringLiteral("vectors")), before.value(QStringLiteral("vectors")));
        QCOMPARE(repository.search(query(QStringLiteral("stableword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(repository.search(query(QStringLiteral("uncommittedword")), &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(!inspectedPaths.isEmpty());
        for (const QString &checked : inspectedPaths)
            QCOMPARE(checked, paths.indexDir);

        available = std::numeric_limits<qint64>::max() / 2;
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v2"),
                    {segment(QStringLiteral("recoveredword"), QStringLiteral("document"), basis(1))}, false, &error),
                 qPrintable(error));
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("v2"));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void automaticMaintenanceFailureDoesNotReportCommittedWriteAsFailure()
    {
        TestEnvironment environment(QStringLiteral("storage-maintenance-failure"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString oldPath = environment.path(QStringLiteral("existing.txt"));
        const QString newPath = environment.path(QStringLiteral("committed.txt"));
        bool failMaintenance = false;
        int diskChecks = 0;
        StorageOptions options;
        options.diskReserveBytes = 1024;
        options.availableBytes = [&](const QString &) -> qint64 {
            ++diskChecks;
            return failMaintenance && diskChecks > 1 ? 0
                        : std::numeric_limits<qint64>::max() / 2;
        };
        IndexRepository repository(options);
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(oldPath, QStringLiteral("v1"),
                    {segment(QStringLiteral("existingword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));

        // Seed a pending event backlog directly. This is fault-injection setup,
        // not an ANN scale benchmark, so it need not create thousands of files.
        QVERIFY2(executeSql(repository.databasePath(),
                           "WITH RECURSIVE backlog(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM backlog WHERE n<4096) "
                           "INSERT INTO ann_events(generation,shard,segment_id,operation) "
                           "SELECT s.generation,s.shard,s.id,'upsert' FROM segments s CROSS JOIN backlog;",
                           &error), qPrintable(error));
        diskChecks = 0;
        failMaintenance = true;
        // The first disk check permits the transaction. Automatic derived-index
        // maintenance then runs out of space after the authoritative commit.
        QVERIFY2(repository.replaceFile(newPath, QStringLiteral("committed-v1"),
                    {segment(QStringLiteral("committedword"), QStringLiteral("document"), basis(1))}, false, &error),
                 qPrintable(error));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(diskChecks >= 2, "The post-commit maintenance disk fault was not exercised");
        QCOMPARE(repository.fileVersion(newPath, false, &error), QStringLiteral("committed-v1"));
        QCOMPARE(repository.search(query(QStringLiteral("committedword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(1);
        semantic.limit = 1;
        const auto hits = repository.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().path, newPath);
        QVERIFY(!repository.flush(&error));
        QVERIFY(!error.isEmpty());
        QCOMPARE(repository.fileVersion(newPath, false, &error), QStringLiteral("committed-v1"));
        failMaintenance = false;
        QVERIFY2(repository.flush(&error), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void offlineImportKeepsKeywordsAndRequiresFlushForSemanticQueries()
    {
        TestEnvironment environment(QStringLiteral("storage-offline-import"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        const QString path = environment.path(QStringLiteral("imported.txt"));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(0);
        QString error;
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY2(repository.replaceFiles({{path, QStringLiteral("import-v1"), false,
                        {segment(QStringLiteral("importedword"), QStringLiteral("document"), basis(0))}}},
                        &error, true), qPrintable(error));
            QVERIFY(repository.status(&error).value(QStringLiteral("deferredMaintenance")).toBool());
            QVERIFY(repository.search(semantic, &error).isEmpty());
            QVERIFY(!error.isEmpty());
            QCOMPARE(repository.search(query(QStringLiteral("importedword")), &error).size(), qsizetype(1));
            QVERIFY2(error.isEmpty(), qPrintable(error));
        }
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        QVERIFY(reopened.status(&error).value(QStringLiteral("deferredMaintenance")).toBool());
        QVERIFY(reopened.search(semantic, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        QVERIFY2(reopened.flushOneShard(&error), qPrintable(error));
        // One-shard maintenance is not the explicit end of an offline import.
        QVERIFY(reopened.search(semantic, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        QVERIFY2(reopened.flush(&error), qPrintable(error));
        QVERIFY(!reopened.status(&error).value(QStringLiteral("deferredMaintenance")).toBool());
        const auto hits = reopened.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().path, path);
        QCOMPARE(reopened.fileVersion(path, false, &error), QStringLiteral("import-v1"));
    }

    void fileSegmentsRoundTripAndResumeAcrossReopen()
    {
        TestEnvironment environment(QStringLiteral("storage-segment-resume"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        const QString path = environment.path(QString::fromUtf8("资料/续读 文件.pdf"));
        const QString codePath = environment.path(QStringLiteral("source.cpp"));
        Segment first = segment(QString::fromUtf8("第一页 持久化片段"), QStringLiteral("pdf"), basis(0));
        first.title = QString::fromUtf8("自定义标题");
        first.modality = QStringLiteral("image");
        first.mediaPath = environment.path(QString::fromUtf8("预览 缓存/page 3.png"));
        first.page = 3;
        first.lineStart = 9;
        first.lineEnd = 14;
        first.cell = 5;
        first.startMs = 1300;
        first.endMs = 4800;
        first.embedding[0] = 0.6f;
        first.embedding[1] = 0.8f;
        Segment second = segment(QStringLiteral("Second persisted passage"));
        second.page = 4;
        QString error;
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY2(repository.replaceFile(path, QStringLiteral("partial-v1"), {first, second}, false, &error),
                     qPrintable(error));
            QVERIFY2(repository.replaceFile(codePath, QStringLiteral("code-v1"),
                        {segment(QStringLiteral("Separate code record"), QStringLiteral("code"))}, true, &error),
                     qPrintable(error));
        }
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        auto restored = reopened.fileSegments(path, false, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(restored.size(), qsizetype(2));
        const Segment actual = restored.first();
        QVERIFY(actual.id > 0);
        QVERIFY(restored.last().id > actual.id);
        QCOMPARE(actual.path, path);
        QCOMPARE(actual.title, first.title);
        QCOMPARE(actual.kind, first.kind);
        QCOMPARE(actual.modality, first.modality);
        QCOMPARE(actual.text, first.text);
        QCOMPARE(actual.mediaPath, first.mediaPath);
        QCOMPARE(actual.page, first.page);
        QCOMPARE(actual.lineStart, first.lineStart);
        QCOMPARE(actual.lineEnd, first.lineEnd);
        QCOMPARE(actual.cell, first.cell);
        QCOMPARE(actual.startMs, first.startMs);
        QCOMPARE(actual.endMs, first.endMs);
        QCOMPARE(actual.embedding.size(), qsizetype(embeddingDimensions));
        for (qsizetype i = 0; i < first.embedding.size(); ++i)
            QVERIFY(std::abs(actual.embedding.at(i) - first.embedding.at(i)) < 0.0005f);
        QVERIFY(restored.last().embedding.isEmpty());
        QCOMPARE(restored.last().text, second.text);
        QCOMPARE(restored.last().page, second.page);
        const auto code = reopened.fileSegments(codePath, true, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(code.size(), qsizetype(1));
        QCOMPARE(code.first().text, QStringLiteral("Separate code record"));
        QVERIFY(reopened.fileSegments(path, true, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(reopened.fileSegments(codePath, false, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(reopened.fileSegments(environment.path(QStringLiteral("missing.txt")), false, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));

        Segment tail = segment(QStringLiteral("Resumed tail"), QStringLiteral("pdf"), basis(2));
        tail.page = 5;
        restored.append(tail);
        QVERIFY2(reopened.replaceFile(path, QStringLiteral("complete-v2"), restored, false, &error), qPrintable(error));
        const auto resumed = reopened.fileSegments(path, false, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(resumed.size(), qsizetype(3));
        QCOMPARE(resumed.first().text, first.text);
        QCOMPARE(resumed.first().embedding.size(), actual.embedding.size());
        for (qsizetype i = 0; i < actual.embedding.size(); ++i)
            QVERIFY(std::abs(resumed.first().embedding.at(i) - actual.embedding.at(i)) < 0.0005f);
        QCOMPARE(resumed.last().page, 5);
        QCOMPARE(resumed.last().embedding, basis(2));
        QCOMPARE(reopened.fileVersion(path, false, &error), QStringLiteral("complete-v2"));
        QCOMPARE(reopened.fileVersion(codePath, true, &error), QStringLiteral("code-v1"));
        QVERIFY2(reopened.checkIntegrity(&error), qPrintable(error));
    }

    void samePathSpacesKeepIndependentKeywordsAndVectors()
    {
        TestEnvironment environment(QStringLiteral("storage-space-isolation"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("repository/README.md"));
        struct Phase {
            bool code;
            QString version;
            QString text;
            QVector<float> embedding;
        };
        const QList<Phase> phases{
            {true, QStringLiteral("code-v1"), QStringLiteral("codefirstword"), basis(0)},
            {false, QStringLiteral("main-v2"), QStringLiteral("mainmiddleword"), basis(1)},
            {true, QStringLiteral("code-v3"), QStringLiteral("codefinalword"), basis(2)}
        };
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QHash<bool, Phase> currentBySpace;
        for (qsizetype i = 0; i < phases.size(); ++i) {
            const auto &phase = phases.at(i);
            QVERIFY2(repository.replaceFile(path, phase.version,
                        {segment(phase.text, phase.code ? QStringLiteral("code") : QStringLiteral("document"),
                                 phase.embedding)}, phase.code, &error), qPrintable(error));
            currentBySpace.insert(phase.code, phase);
            // Exercise the live delta/tombstone path and then the persisted ANN.
            for (int publication = 0; publication < 2; ++publication) {
                const auto status = repository.status(&error);
                QVERIFY2(error.isEmpty(), qPrintable(error));
                QCOMPARE(status.value(QStringLiteral("files")).toInteger(), qint64(currentBySpace.size()));
                QCOMPARE(status.value(QStringLiteral("segments")).toInteger(), qint64(currentBySpace.size()));
                QCOMPARE(status.value(QStringLiteral("vectors")).toInteger(), qint64(currentBySpace.size()));
                for (bool code : {false, true}) {
                    if (!currentBySpace.contains(code)) {
                        QVERIFY(repository.fileVersion(path, code, &error).isEmpty());
                        QVERIFY(repository.fileSegments(path, code, &error).isEmpty());
                        QVERIFY2(error.isEmpty(), qPrintable(error));
                        continue;
                    }
                    const auto &current = currentBySpace[code];
                    QCOMPARE(repository.fileVersion(path, code, &error), current.version);
                    const auto stored = repository.fileSegments(path, code, &error);
                    QVERIFY2(error.isEmpty(), qPrintable(error));
                    QCOMPARE(stored.size(), qsizetype(1));
                    QCOMPARE(stored.first().text, current.text);

                    auto keyword = query(current.text);
                    keyword.code = code;
                    const auto currentHits = repository.search(keyword, &error);
                    QVERIFY2(error.isEmpty(), qPrintable(error));
                    QCOMPARE(currentHits.size(), qsizetype(1));
                    QCOMPARE(currentHits.first().text, current.text);
                    keyword.code = !code;
                    QVERIFY(repository.search(keyword, &error).isEmpty());
                    QVERIFY2(error.isEmpty(), qPrintable(error));

                    SearchOptions semantic;
                    semantic.code = code;
                    semantic.queryEmbedding = current.embedding;
                    const auto semanticHits = repository.search(semantic, &error);
                    QVERIFY2(error.isEmpty(), qPrintable(error));
                    QCOMPARE(semanticHits.size(), qsizetype(1));
                    QCOMPARE(semanticHits.first().text, current.text);
                }
                for (qsizetype old = 0; old < i; ++old) {
                    const auto &prior = phases.at(old);
                    if (currentBySpace[prior.code].version == prior.version)
                        continue;
                    auto retired = query(prior.text);
                    retired.code = prior.code;
                    QVERIFY(repository.search(retired, &error).isEmpty());
                    QVERIFY2(error.isEmpty(), qPrintable(error));
                }
                if (publication == 0)
                    QVERIFY2(repository.flush(&error), qPrintable(error));
            }
        }

        // The compatibility overload targets ordinary files only.
        QVERIFY2(repository.removeFile(path, &error), qPrintable(error));
        QVERIFY(repository.fileVersion(path, false, &error).isEmpty());
        QCOMPARE(repository.fileVersion(path, true, &error), QStringLiteral("code-v3"));
        QVERIFY2(repository.flush(&error), qPrintable(error));
        SearchOptions codeSearch = query(QStringLiteral("codefinalword"));
        codeSearch.code = true;
        QCOMPARE(repository.search(codeSearch, &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        SearchOptions semantic;
        semantic.code = true;
        semantic.queryEmbedding = basis(2);
        const auto remainingCode = repository.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(remainingCode.size(), qsizetype(1));
        QCOMPARE(remainingCode.first().text, QStringLiteral("codefinalword"));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("main-v4"),
                    {segment(QStringLiteral("mainremainingword"), QStringLiteral("document"), basis(1))},
                    false, &error), qPrintable(error));
        QVERIFY2(repository.removeFile(path, true, &error), qPrintable(error));
        QVERIFY(repository.fileVersion(path, true, &error).isEmpty());
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("main-v4"));
        QVERIFY(repository.search(codeSearch, &error).isEmpty());
        QCOMPARE(repository.search(query(QStringLiteral("mainremainingword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.flush(&error), qPrintable(error));
        QVERIFY(repository.search(semantic, &error).isEmpty());
        semantic.code = false;
        semantic.queryEmbedding = basis(1);
        const auto remainingOrdinary = repository.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(remainingOrdinary.size(), qsizetype(1));
        QCOMPARE(remainingOrdinary.first().text, QStringLiteral("mainremainingword"));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void pruningIsIndependentForOrdinaryAndCodeSpaces()
    {
        TestEnvironment environment(QStringLiteral("storage-space-pruning"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString root = environment.path(QStringLiteral("root"));
        const QString shared = QDir(root).filePath(QStringLiteral("shared.md"));
        const QString ordinaryOnly = QDir(root).filePath(QStringLiteral("ordinary.txt"));
        const QString codeOnly = QDir(root).filePath(QStringLiteral("source.cpp"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFiles({
            {shared, QStringLiteral("main-v1"), false, {segment(QStringLiteral("ordinarytoken"))}},
            {ordinaryOnly, QStringLiteral("main-v1"), false, {segment(QStringLiteral("ordinarytoken"))}},
            {shared, QStringLiteral("code-v1"), true, {segment(QStringLiteral("codetoken"))}},
            {codeOnly, QStringLiteral("code-v1"), true, {segment(QStringLiteral("codetoken"))}}
        }, &error), qPrintable(error));
        auto codeSearch = query(QStringLiteral("codetoken"));
        codeSearch.code = true;
        QVERIFY2(repository.prune(root, {shared}, &error), qPrintable(error));
        QCOMPARE(hitPaths(repository.search(query(QStringLiteral("ordinarytoken")), &error)),
                 (QSet<QString>{shared}));
        QCOMPARE(hitPaths(repository.search(codeSearch, &error)), (QSet<QString>{shared, codeOnly}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.prune(root, {codeOnly}, true, &error), qPrintable(error));
        QCOMPARE(hitPaths(repository.search(codeSearch, &error)), (QSet<QString>{codeOnly}));
        QCOMPARE(repository.fileVersion(shared, false, &error), QStringLiteral("main-v1"));
        QVERIFY(repository.fileVersion(shared, true, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.prune(root, {}, false, &error), qPrintable(error));
        QVERIFY(repository.search(query(QStringLiteral("ordinarytoken")), &error).isEmpty());
        QCOMPARE(hitPaths(repository.search(codeSearch, &error)), (QSet<QString>{codeOnly}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void publicationGateRejectsObsoleteWorkAndReleasesLeaseBeforeMaintenance()
    {
        TestEnvironment environment(QStringLiteral("storage-publication-gate"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("guarded.txt"));
        std::weak_ptr<void> activeLease;
        bool observeMaintenance = false;
        bool maintenanceReached = false;
        bool leaseWasAliveDuringMaintenance = false;
        StorageOptions options;
        options.faultHook = [&](const QString &point) {
            if (observeMaintenance
                && point == QStringLiteral("ann-manifest-published-before-outbox-cleanup")) {
                maintenanceReached = true;
                leaseWasAliveDuringMaintenance = leaseWasAliveDuringMaintenance || !activeLease.expired();
            }
        };
        IndexRepository repository(options);
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("lease-v1"),
                    {segment(QStringLiteral("acceptedword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        const auto before = repository.status(&error);
        int rejectedGateCalls = 0;
        const PublicationGate reject = [&]() -> std::shared_ptr<void> {
            ++rejectedGateCalls;
            return {};
        };
        QVERIFY(!repository.replaceFile(path, QStringLiteral("obsolete-v2"),
                    {segment(QStringLiteral("obsoleteword"), QStringLiteral("document"), basis(1))},
                    false, &error, reject));
        QVERIFY(!error.isEmpty());
        QCOMPARE(rejectedGateCalls, 1);
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("lease-v1"));
        QCOMPARE(repository.status(&error).value(QStringLiteral("generation")),
                 before.value(QStringLiteral("generation")));
        QVERIFY(repository.search(query(QStringLiteral("obsoleteword")), &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(repository.search(query(QStringLiteral("acceptedword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));

        // Force automatic post-commit maintenance without a scale-test corpus.
        const QString databasePath = repository.databasePath();
        QVERIFY2(executeSql(databasePath,
                           "WITH RECURSIVE backlog(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM backlog WHERE n<4096) "
                           "INSERT INTO ann_events(generation,shard,segment_id,operation) "
                           "SELECT s.generation,s.shard,s.id,'upsert' FROM segments s CROSS JOIN backlog;",
                           &error), qPrintable(error));
        bool leaseReleased = false;
        bool commitVisibleWhenLeaseReleased = false;
        QString leaseReleaseError;
        int acceptedGateCalls = 0;
        const PublicationGate accept = [&]() -> std::shared_ptr<void> {
            ++acceptedGateCalls;
            std::shared_ptr<void> lease(new int(1), [&](void *value) {
                delete static_cast<int *>(value);
                leaseReleased = true;
                // A separate SQLite connection must already see the commit
                // when the service's publication lease is dropped.
                commitVisibleWhenLeaseReleased = executeSql(databasePath,
                    "CREATE TEMP TABLE committed(row_count INT CHECK(row_count=1));"
                    "INSERT INTO committed SELECT count(*) FROM files WHERE version='lease-v2';",
                    &leaseReleaseError);
            });
            activeLease = lease;
            return lease;
        };
        observeMaintenance = true;
        QVERIFY2(repository.replaceFile(path, QStringLiteral("lease-v2"),
                    {segment(QStringLiteral("replacementword"), QStringLiteral("document"), basis(1))},
                    false, &error, accept), qPrintable(error));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(acceptedGateCalls, 1);
        QVERIFY(leaseReleased);
        QVERIFY2(commitVisibleWhenLeaseReleased, qPrintable(leaseReleaseError));
        QVERIFY2(maintenanceReached, "The post-commit maintenance hook was not exercised");
        QVERIFY(!leaseWasAliveDuringMaintenance);
        QVERIFY(activeLease.expired());
        QCOMPARE(repository.fileVersion(path, false, &error), QStringLiteral("lease-v2"));
        QCOMPARE(repository.search(query(QStringLiteral("replacementword")), &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(repository.search(query(QStringLiteral("acceptedword")), &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void pipelineChangePreservesSourceVersionForKeywordHits()
    {
        TestEnvironment environment(QStringLiteral("storage-source-version"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        const QString path = environment.path(QStringLiteral("versioned.txt"));
        const QString sourceVersion = QStringLiteral("content-digest-original-42");
        QString error;
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("model-one"), &error), qPrintable(error));
            QVERIFY2(repository.replaceFile(path, sourceVersion,
                        {segment(QStringLiteral("versionedword"), QStringLiteral("document"), basis(0))}, false, &error),
                     qPrintable(error));
            const auto original = repository.search(query(QStringLiteral("versionedword")), &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(original.size(), qsizetype(1));
            QCOMPARE(original.first().sourceVersion, sourceVersion);
            QVERIFY2(repository.setPipelineFingerprint(QStringLiteral("model-two"), &error), qPrintable(error));
            QVERIFY(repository.fileVersion(path, false, &error).isEmpty());
            QVERIFY2(error.isEmpty(), qPrintable(error));
            const auto retained = repository.search(query(QStringLiteral("versionedword")), &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(retained.size(), qsizetype(1));
            QCOMPARE(retained.first().sourceVersion, sourceVersion);
            QCOMPARE(retained.first().path, path);
        }
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        QVERIFY(reopened.fileVersion(path, false, &error).isEmpty());
        const auto retained = reopened.search(query(QStringLiteral("versionedword")), &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(retained.size(), qsizetype(1));
        QCOMPARE(retained.first().sourceVersion, sourceVersion);
        QVERIFY2(reopened.checkIntegrity(&error), qPrintable(error));
    }

    void cancelledSearchReturnsExplicitErrorAndCanBeRetried()
    {
        TestEnvironment environment(QStringLiteral("storage-query-cancellation"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("searchable.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("v1"),
                    {segment(QStringLiteral("cancellableword"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        auto cancelled = std::make_shared<std::atomic_bool>(true);
        auto keyword = query(QStringLiteral("cancellableword"));
        keyword.cancelled = cancelled;
        SearchOptions semantic;
        semantic.queryEmbedding = basis(0);
        semantic.cancelled = cancelled;
        auto hybrid = keyword;
        hybrid.queryEmbedding = basis(0);
        for (const auto &options : {keyword, semantic, hybrid}) {
            cancelled->store(true, std::memory_order_relaxed);
            QVERIFY(repository.search(options, &error).isEmpty());
            QCOMPARE(error, QStringLiteral("Search cancelled"));
            cancelled->store(false, std::memory_order_relaxed);
            const auto hits = repository.search(options, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(hits.size(), qsizetype(1));
            QCOMPARE(hits.first().path, path);
            QCOMPARE(hits.first().sourceVersion, QStringLiteral("v1"));
        }
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void flushOneShardPublishesBoundedProgressAndDeletion()
    {
        TestEnvironment environment(QStringLiteral("storage-one-shard"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVector<IndexedFile> files;
        for (int i = 0; i < 32; ++i) {
            files.append({environment.path(QStringLiteral("files/%1.txt").arg(i)), QStringLiteral("v1"), false,
                          {segment(QStringLiteral("shardword %1").arg(i), QStringLiteral("document"), basis(i % 2))}});
        }
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFiles(files, &error), qPrintable(error));
        QCOMPARE(repository.status(&error).value(QStringLiteral("annVectors")).toInteger(), qint64(0));
        QVERIFY2(repository.flushOneShard(&error), qPrintable(error));
        qint64 published = repository.status(&error).value(QStringLiteral("annVectors")).toInteger();
        QVERIFY(published > 0);
        QVERIFY(published < files.size());
        for (int remaining = 0; remaining < 16 && published < files.size(); ++remaining) {
            QVERIFY2(repository.flushOneShard(&error), qPrintable(error));
            const auto next = repository.status(&error).value(QStringLiteral("annVectors")).toInteger();
            QVERIFY(next > published);
            published = next;
        }
        QCOMPARE(published, qint64(files.size()));
        QVERIFY2(repository.flushOneShard(&error), qPrintable(error));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const QString removed = files.first().path;
        QVERIFY2(repository.removeFile(removed, &error), qPrintable(error));
        QVERIFY2(repository.flushOneShard(&error), qPrintable(error));
        QCOMPARE(repository.status(&error).value(QStringLiteral("annVectors")).toInteger(), qint64(files.size() - 1));
        SearchOptions semantic;
        semantic.queryEmbedding = basis(0);
        const auto hits = repository.search(semantic, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(files.size() - 1));
        QVERIFY(!hitPaths(hits).contains(removed));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void fileMetadataBackfillPreservesIndexedContentAndVersion()
    {
        TestEnvironment environment(QStringLiteral("storage-metadata-backfill"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QString::fromUtf8("资料/报告.pdf"));
        const QString version = QStringLiteral("source-v1|partial:2");
        Segment first = segment(QStringLiteral("first searchable passage"), QStringLiteral("pdf"), basis(0));
        first.page = 3;
        Segment second = segment(QStringLiteral("second searchable passage"), QStringLiteral("pdf"));
        second.page = 4;
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, version, {first, second}, false, &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("code-v1"),
                    {segment(QStringLiteral("separate code passage"), QStringLiteral("code"))},
                    true, &error, {}, 800, QStringLiteral("code")), qPrintable(error));
        const auto before = segmentSnapshots(repository.fileSegments(path, false, &error));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const auto pending = repository.listFiles({});
        QVERIFY(pending.metadataPending);
        QCOMPARE(pending.errorCode, QStringLiteral("metadata_pending"));
        QVERIFY(pending.files.isEmpty());

        // Metadata repair must not re-extract, change source versions or create
        // records for files that have not been indexed yet.
        QVERIFY2(repository.updateFileMetadata({
            {path, false, 1700, QStringLiteral("pdf"), QStringLiteral("source-v1")},
            {environment.path(QStringLiteral("missing.txt")), false, 1900, QStringLiteral("text"), {}}
        }, &error), qPrintable(error));
        const auto page = repository.listFiles({});
        QVERIFY2(page.error.isEmpty(), qPrintable(page.error));
        QVERIFY(!page.metadataPending);
        QCOMPARE(page.files.size(), qsizetype(1));
        const auto entry = page.files.first();
        QCOMPARE(entry.path, path);
        QCOMPARE(entry.title, QFileInfo(path).fileName());
        QCOMPARE(entry.kind, QStringLiteral("pdf"));
        QCOMPARE(entry.modifiedMs, qint64(1700));
        QCOMPARE(entry.sourceVersion, version);
        QCOMPARE(repository.fileVersion(path, false, &error), version);
        QCOMPARE(segmentSnapshots(repository.fileSegments(path, false, &error)), before);
        QCOMPARE(repository.status(&error).value(QStringLiteral("files")).toInteger(), qint64(2));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        FileListOptions codeOptions;
        codeOptions.code = true;
        const auto codePage = repository.listFiles(codeOptions);
        QVERIFY2(codePage.error.isEmpty(), qPrintable(codePage.error));
        QCOMPARE(codePage.files.size(), qsizetype(1));
        QCOMPARE(codePage.files.first().modifiedMs, qint64(800));
        QCOMPARE(codePage.files.first().kind, QStringLiteral("code"));
        QCOMPARE(codePage.files.first().sourceVersion, QStringLiteral("code-v1"));

        // Unchanged repair scans and stale fingerprints must leave cursor
        // generations alone, even if the stale scan proposes other metadata.
        QVERIFY2(repository.updateFileMetadata({
            {path, false, 1700, QStringLiteral("pdf"), QStringLiteral("source-v1")}
        }, &error), qPrintable(error));
        QCOMPARE(repository.listFiles({}).browseGeneration, page.browseGeneration);
        QVERIFY2(repository.updateFileMetadata({
            {path, false, 9999, QStringLiteral("image"), QStringLiteral("source-v2")},
            {path, false, 9999, QStringLiteral("image"), QStringLiteral("source")}
        }, &error), qPrintable(error));
        const auto unchanged = repository.listFiles({});
        QVERIFY2(unchanged.error.isEmpty(), qPrintable(unchanged.error));
        QCOMPARE(unchanged.browseGeneration, page.browseGeneration);
        QCOMPARE(unchanged.files.first().modifiedMs, qint64(1700));
        QCOMPARE(unchanged.files.first().kind, QStringLiteral("pdf"));
        QCOMPARE(segmentSnapshots(repository.fileSegments(path, false, &error)), before);
        QVERIFY2(error.isEmpty(), qPrintable(error));

        QVERIFY(!repository.updateFileMetadata({
            {path, false, 1800, QStringLiteral("pdf"), QStringLiteral("source-v1")}
        }, &error, [] { return std::shared_ptr<void>{}; }));
        QVERIFY(!error.isEmpty());
        const auto rejected = repository.listFiles({});
        QVERIFY2(rejected.error.isEmpty(), qPrintable(rejected.error));
        QCOMPARE(rejected.browseGeneration, page.browseGeneration);
        QCOMPARE(rejected.files.first().modifiedMs, qint64(1700));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void metadataSnapshotCanRepairChangedSourcesWithoutReextracting()
    {
        TestEnvironment environment(QStringLiteral("storage-metadata-snapshot"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString changed = environment.path(QStringLiteral("changed.txt"));
        const QString healthy = environment.path(QStringLiteral("healthy.txt"));
        const QString oldVersion = QStringLiteral("old-source|pipeline");
        const QString healthyVersion = QStringLiteral("healthy-source|pipeline");
        QVERIFY2(repository.replaceFile(changed, oldVersion,
            {segment(QStringLiteral("old indexed content"))}, false, &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(healthy, healthyVersion,
            {segment(QStringLiteral("healthy content"))}, false, &error), qPrintable(error));
        const auto before = segmentSnapshots(repository.fileSegments(changed, false, &error));
        QCOMPARE(repository.listFiles({}).errorCode, QStringLiteral("metadata_pending"));
        // A paused service can repair both metadata rows. It later hides the
        // changed source by checking its retained sourceVersion against disk.
        QVERIFY2(repository.updateFileMetadata({
            {changed, false, 2000, QStringLiteral("text"), QStringLiteral("new-source"), oldVersion},
            {healthy, false, 1000, QStringLiteral("text"), QStringLiteral("healthy-source"), healthyVersion}
        }, &error), qPrintable(error));
        const auto page = repository.listFiles({});
        QVERIFY2(page.error.isEmpty(), qPrintable(page.error));
        QCOMPARE(page.files.size(), qsizetype(2));
        QCOMPARE(page.files.first().path, changed);
        QCOMPARE(page.files.first().sourceVersion, oldVersion);
        QCOMPARE(segmentSnapshots(repository.fileSegments(changed, false, &error)), before);
        QCOMPARE(repository.fileVersion(changed, false, &error), oldVersion);

        const QString publishedVersion = QStringLiteral("published-source|pipeline");
        QVERIFY2(repository.replaceFile(changed, publishedVersion,
            {segment(QStringLiteral("newly indexed content"))}, false, &error, {},
            3000, QStringLiteral("text")), qPrintable(error));
        const auto publishedGeneration = repository.browseGeneration(&error);
        QVERIFY2(repository.updateFileMetadata({
            {changed, false, 5, QStringLiteral("pdf"), QStringLiteral("published-source"), oldVersion}
        }, &error), qPrintable(error));
        const auto stillCurrent = repository.listFiles({});
        QVERIFY2(stillCurrent.error.isEmpty(), qPrintable(stillCurrent.error));
        QCOMPARE(stillCurrent.files.first().modifiedMs, qint64(3000));
        QCOMPARE(stillCurrent.files.first().kind, QStringLiteral("text"));
        QCOMPARE(stillCurrent.files.first().sourceVersion, publishedVersion);
        QCOMPARE(stillCurrent.browseGeneration, publishedGeneration);
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void schemaTwoMigrationRequiresMetadataWithoutReextractingFiles()
    {
        TestEnvironment environment(QStringLiteral("storage-schema-two-browse-migration"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        const QString path = environment.path(QStringLiteral("old/report.pdf"));
        const QString version = QStringLiteral("legacy-source|partial:2");
        QJsonArray originalSegments;
        QString error;
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            Segment first = segment(QStringLiteral("legacy searchable passage"), QStringLiteral("pdf"), basis(0));
            first.page = 7;
            Segment second = segment(QStringLiteral("legacy second passage"), QStringLiteral("pdf"));
            second.page = 8;
            QVERIFY2(repository.replaceFile(path, version, {first, second}, false, &error,
                        {}, 600, QStringLiteral("pdf")), qPrintable(error));
            originalSegments = segmentSnapshots(repository.fileSegments(path, false, &error));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QVERIFY2(repository.flush(&error), qPrintable(error));
        }
        // Materialize the preceding schema, including its already indexed
        // segments. Closing the repository first also exercises real reopening.
        QVERIFY2(executeSql(paths.databaseFile(),
            "DROP INDEX files_browse;"
            "DROP INDEX files_browse_kind;"
            "DROP INDEX files_pending_metadata;"
            "ALTER TABLE files DROP COLUMN modified_ms;"
            "ALTER TABLE files DROP COLUMN kind;"
            "DELETE FROM meta WHERE key='browse_generation';"
            "PRAGMA user_version=2;", &error), qPrintable(error));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            const auto status = repository.status(&error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(status.value(QStringLiteral("schemaVersion")).toInt(), 3);
            QCOMPARE(status.value(QStringLiteral("metadataPendingFiles")).toInteger(), qint64(1));
            const auto pending = repository.listFiles({});
            QVERIFY(pending.metadataPending);
            QCOMPARE(pending.errorCode, QStringLiteral("metadata_pending"));
            QVERIFY(pending.files.isEmpty());
            QVERIFY(pending.nextCursor.isEmpty());
            QVERIFY(!pending.hasMore);
            QCOMPARE(repository.fileVersion(path, false, &error), version);
            QCOMPARE(segmentSnapshots(repository.fileSegments(path, false, &error)), originalSegments);
            const auto hits = repository.search(query(QStringLiteral("searchable")), &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(hits.size(), qsizetype(1));
            QCOMPARE(hits.first().sourceVersion, version);

            // An unchanged source version still needs this metadata update.
            QVERIFY2(repository.updateFileMetadata({
                {path, false, 900, QStringLiteral("pdf"), QStringLiteral("legacy-source")}
            }, &error), qPrintable(error));
            const auto completed = repository.listFiles({});
            QVERIFY2(completed.error.isEmpty(), qPrintable(completed.error));
            QVERIFY(!completed.metadataPending);
            QCOMPARE(completed.files.size(), qsizetype(1));
            QCOMPARE(completed.files.first().modifiedMs, qint64(900));
            QCOMPARE(completed.files.first().sourceVersion, version);
            QCOMPARE(segmentSnapshots(repository.fileSegments(path, false, &error)), originalSegments);
            QCOMPARE(repository.fileVersion(path, false, &error), version);
            QCOMPARE(repository.status(&error).value(QStringLiteral("metadataPendingFiles")).toInteger(), qint64(0));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
        }
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        const auto page = reopened.listFiles({});
        QVERIFY2(page.error.isEmpty(), qPrintable(page.error));
        QCOMPARE(page.files.size(), qsizetype(1));
        QCOMPARE(page.files.first().modifiedMs, qint64(900));
        QCOMPARE(page.files.first().sourceVersion, version);
        QCOMPARE(segmentSnapshots(reopened.fileSegments(path, false, &error)), originalSegments);
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void pageLayoutIsSelectedOnlyForNewDatabases()
    {
        TestEnvironment environment(QStringLiteral("storage-page-layout"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QString error;
        const auto fresh = RuntimePaths::resolve(environment.path(QStringLiteral("fresh")));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(fresh, &error), qPrintable(error));
        }
        QVERIFY2(executeSql(fresh.databaseFile(),
            "CREATE TEMP TABLE require_pages(value INTEGER CHECK(value=16384));"
            "INSERT INTO require_pages SELECT page_size FROM pragma_page_size;", &error), qPrintable(error));
        const auto existing = RuntimePaths::resolve(environment.path(QStringLiteral("existing")));
        QVERIFY2(existing.ensure(&error), qPrintable(error));
        QVERIFY2(executeSql(existing.databaseFile(),
            "PRAGMA page_size=4096;CREATE TABLE preserved_layout(value TEXT);"
            "INSERT INTO preserved_layout VALUES('retained');", &error), qPrintable(error));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(existing, &error), qPrintable(error));
        }
        QVERIFY2(executeSql(existing.databaseFile(),
            "CREATE TEMP TABLE require_pages(value INTEGER CHECK(value=4096));"
            "INSERT INTO require_pages SELECT page_size FROM pragma_page_size;"
            "CREATE TEMP TABLE require_preserved(value TEXT CHECK(value='retained'));"
            "INSERT INTO require_preserved SELECT value FROM preserved_layout;", &error), qPrintable(error));
    }

    void futureSchemaIsRejectedWithoutChangingDatabase()
    {
        TestEnvironment environment(QStringLiteral("storage-future-schema"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        QVERIFY2(executeSql(paths.databaseFile(),
                           "PRAGMA user_version=2147483647; CREATE TABLE future_data(value TEXT);"
                           "INSERT INTO future_data VALUES('preserve me');", &error), qPrintable(error));
        IndexRepository repository;
        QVERIFY(!repository.open(paths, &error));
        QVERIFY(!error.isEmpty());
        // A failing reopen must not silently migrate or remove unknown data.
        error.clear();
        QVERIFY2(executeSql(paths.databaseFile(),
                           "CREATE TEMP TABLE require_preserved_data(value TEXT CHECK(value='preserve me'));"
                           "INSERT INTO require_preserved_data VALUES(COALESCE((SELECT value FROM future_data LIMIT 1),''));"
                           "CREATE TEMP TABLE require_future_version(value INT CHECK(value=2147483647));"
                           "INSERT INTO require_future_version SELECT user_version FROM pragma_user_version;",
                           &error), qPrintable(error));
    }

    void malformedDatabaseIsRejected()
    {
        TestEnvironment environment(QStringLiteral("storage-corrupt"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        QFile database(paths.databaseFile());
        QVERIFY2(database.open(QIODevice::WriteOnly), qPrintable(database.errorString()));
        const QByteArray garbage(1024, 'x');
        QCOMPARE(database.write(garbage), qint64(garbage.size()));
        database.close();
        IndexRepository repository;
        QVERIFY(!repository.open(paths, &error));
        QVERIFY(!error.isEmpty());
    }

    void concurrentReadersSeeCommittedReplacements()
    {
        TestEnvironment environment(QStringLiteral("storage-concurrency"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString path = environment.path(QStringLiteral("changing.txt"));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("0"),
                    {segment(QStringLiteral("generation 0"), QStringLiteral("document"), basis(0))}, false, &error),
                 qPrintable(error));
        std::atomic<int> completedReads{0};
        std::mutex failureMutex;
        QString readerFailure;
        std::atomic_bool stopReader{false};
        std::thread reader([&] {
            while (!stopReader.load(std::memory_order_relaxed)) {
                QString readError;
                const auto hits = repository.search(query(QStringLiteral("generation")), &readError);
                if (!readError.isEmpty() || hits.size() != 1 || hits.first().path != path
                    || !hits.first().text.startsWith(QStringLiteral("generation "))) {
                    std::lock_guard lock(failureMutex);
                    readerFailure = QStringLiteral("Reader saw %1 hits, error: %2").arg(hits.size()).arg(readError);
                    return;
                }
                completedReads.fetch_add(1, std::memory_order_relaxed);
                std::this_thread::yield();
            }
        });
        // Join before repository or TestEnvironment are destroyed, including
        // when a writer-side QVERIFY exits this test early. std::thread also
        // supports Apple libc++ versions that do not provide std::jthread.
        const auto joinReader = qScopeGuard([&] {
            stopReader.store(true, std::memory_order_relaxed);
            if (reader.joinable())
                reader.join();
        });
        QTRY_VERIFY_WITH_TIMEOUT(completedReads.load(std::memory_order_relaxed) > 0, 10000);
        for (int generation = 1; generation <= 40; ++generation) {
            const QString revision = QString::number(generation);
            const QString text = QStringLiteral("generation %1").arg(generation);
            QVERIFY2(repository.replaceFile(path, revision,
                        {segment(text, QStringLiteral("document"), basis(generation % 2))}, false, &error),
                     qPrintable(error));
            const auto hits = repository.search(query(QStringLiteral("generation")), &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(hits.size(), qsizetype(1));
            QCOMPARE(hits.first().text, text);
            if (generation % 10 == 0)
                QVERIFY2(repository.flush(&error), qPrintable(error));
        }
        stopReader.store(true, std::memory_order_relaxed);
        reader.join();
        QVERIFY2(readerFailure.isEmpty(), qPrintable(readerFailure));
        QVERIFY(completedReads.load(std::memory_order_relaxed) > 0);
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }
};

QTEST_GUILESS_MAIN(IndexRepositoryTest)
#include "index_repository_test.moc"
