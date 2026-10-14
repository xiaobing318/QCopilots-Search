#include "storage/index_repository.h"
#include "core/embedding_math.h"
#include "support/test_environment.h"
#include "support/sqlite_database.h"

#include <QDir>
#include <QDirIterator>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QJsonDocument>
#include <QStandardPaths>
#include <QtTest>

#include <sqlite3.h>
#include <cmath>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <exception>
#include <functional>
#include <limits>
#include <future>
#include <mutex>
#include <stdexcept>
#include <utility>
#include <memory>
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

using TestDatabase = std::unique_ptr<sqlite3, decltype(&sqlite3_close_v2)>;

TestDatabase openTestDatabase(const QString &path, int flags, QString *error)
{
    sqlite3 *raw = nullptr;
    const int rc = test::openSqliteDatabase(path, &raw, flags);
    TestDatabase database(raw, sqlite3_close_v2);
    if (rc != SQLITE_OK) {
        *error = raw ? QString::fromUtf8(sqlite3_errmsg(raw)) : QStringLiteral("Cannot open SQLite fixture");
        return TestDatabase(nullptr, sqlite3_close_v2);
    }
    error->clear();
    return database;
}

bool executeTestSql(sqlite3 *database, const char *sql, QString *error)
{
    const int rc = sqlite3_exec(database, sql, nullptr, nullptr, nullptr);
    *error = rc == SQLITE_OK ? QString{} : QString::fromUtf8(sqlite3_errmsg(database));
    return rc == SQLITE_OK;
}

qint64 testFileCount(sqlite3 *database)
{
    sqlite3_stmt *raw = nullptr;
    const int rc = sqlite3_prepare_v2(database, "SELECT count(*) FROM files", -1, &raw, nullptr);
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(raw, sqlite3_finalize);
    return rc == SQLITE_OK && sqlite3_step(statement.get()) == SQLITE_ROW
        ? sqlite3_column_int64(statement.get(), 0) : -1;
}


QStringList hitPaths(const QList<SearchHit> &hits)
{
    QStringList paths;
    for (const auto &hit : hits)
        paths.append(hit.path);
    return paths;
}

QJsonArray hitIdentities(const QList<SearchHit> &hits)
{
    QJsonArray identities;
    for (const auto &hit : hits)
        identities.append(QJsonObject{{QStringLiteral("id"), hit.segmentId},
                                     {QStringLiteral("path"), hit.path},
                                     {QStringLiteral("version"), hit.sourceVersion}});
    return identities;
}

QVector<float> sentinelVector(float score)
{
    QVector<float> vector(embeddingDimensions, 0.0f);
    vector[0] = score;
    vector[1] = std::sqrt(1.0f - score * score);
    return vector;
}

qint64 pendingEventsForShard(const QString &path, int shard, QString *error)
{
    auto database = openTestDatabase(path, SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, error);
    if (!database)
        return -1;
    sqlite3_stmt *raw = nullptr;
    const int prepared = sqlite3_prepare_v2(database.get(),
        "SELECT count(*) FROM ann_events WHERE shard=?", -1, &raw, nullptr);
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(raw, sqlite3_finalize);
    if (prepared != SQLITE_OK || sqlite3_bind_int(raw, 1, shard) != SQLITE_OK
            || sqlite3_step(raw) != SQLITE_ROW) {
        *error = QString::fromUtf8(sqlite3_errmsg(database.get()));
        return -1;
    }
    error->clear();
    return sqlite3_column_int64(raw, 0);
}


class PairedSnapshotProbe {
public:
    struct State {
        std::mutex mutex;
        std::condition_variable changed;
        std::atomic_bool armed{false};
        bool entered = false;
        bool released = false;
        bool timedOut = false;
    };

    struct Result {
        QList<SearchHit> hits;
        QString error;
    };

    static std::function<void(const QString &)> observer(const std::shared_ptr<State> &state)
    {
        return [state](const QString &point) {
            if (point != QStringLiteral("sqlite-search-progress") ||
                !state->armed.exchange(false, std::memory_order_acq_rel))
                return;
            std::unique_lock lock(state->mutex);
            state->entered = true;
            state->changed.notify_all();
            // This is a fail-safe, never a successful automatic release.
            // One flushOneShard checkpoint may wait SQLite's 5-second busy
            // timeout while this reader pins WAL. Do not use full flush here.
            if (!state->changed.wait_for(lock, std::chrono::seconds(60),
                                         [&] { return state->released; }))
                state->timedOut = true;
        };
    }

    explicit PairedSnapshotProbe(std::shared_ptr<State> state)
        : state_(std::move(state)), cancelled_(std::make_shared<std::atomic_bool>(false))
    {
    }

    PairedSnapshotProbe(std::shared_ptr<State> state, const IndexRepository &repository,
                        SearchOptions options)
        : PairedSnapshotProbe(std::move(state))
    {
        start(repository, std::move(options));
    }

    PairedSnapshotProbe(const PairedSnapshotProbe &) = delete;
    PairedSnapshotProbe &operator=(const PairedSnapshotProbe &) = delete;

    ~PairedSnapshotProbe()
    {
        // QVERIFY/QCOMPARE may return from the enclosing test at any point.
        // The only thread owned here is this query. Never leave its barrier
        // waiting on a fixture or repository whose lifetime has already ended.
        state_->armed.store(false, std::memory_order_release);
        cancelled_->store(true, std::memory_order_relaxed);
        release();
        if (future_.valid())
            future_.wait();
    }

    void start(const IndexRepository &repository, SearchOptions options)
    {
        if (started_)
            throw std::logic_error("A paired snapshot probe is single-use");
        started_ = true;
        // Query options are captured by value. Cleanup owns its cancellation
        // token and never changes a caller's options or shared token.
        options.cancelled = cancelled_;
        state_->armed.store(true, std::memory_order_release);
        future_ = std::async(std::launch::async, [&repository, options = std::move(options)] {
            Result result;
            result.hits = repository.search(options, &result.error);
            return result;
        });
    }

    bool waitEntered(std::chrono::seconds timeout = std::chrono::seconds(10))
    {
        std::unique_lock lock(state_->mutex);
        return state_->changed.wait_for(lock, timeout, [&] { return state_->entered; });
    }

    bool waitUntilEntered() { return waitEntered(); }

    bool remainsBlocked() const
    {
        std::lock_guard lock(state_->mutex);
        return state_->entered && !state_->released && !state_->timedOut;
    }

    void release()
    {
        {
            std::lock_guard lock(state_->mutex);
            state_->released = true;
        }
        state_->changed.notify_all();
    }

    bool releaseAndJoin(std::chrono::seconds timeout = std::chrono::seconds(10))
    {
        release();
        if (!future_.valid() || future_.wait_for(timeout) != std::future_status::ready)
            return false;
        try {
            result_ = future_.get();
        } catch (const std::exception &exception) {
            result_.error = QStringLiteral("Snapshot query worker exception: %1")
                                .arg(QString::fromUtf8(exception.what()));
        } catch (...) {
            result_.error = QStringLiteral("Unknown snapshot query worker exception");
        }
        joined_ = true;
        return true;
    }

    bool hookTimedOut() const
    {
        std::lock_guard lock(state_->mutex);
        return state_->timedOut;
    }

    bool finish()
    {
        const bool writerFinishedWhilePinned = remainsBlocked();
        if (!releaseAndJoin()) {
            result_.error = QStringLiteral("Snapshot query did not finish within 10 seconds after explicit release");
            return false;
        }
        if (hookTimedOut()) {
            result_.error = QStringLiteral("Snapshot barrier expired after 60 seconds while writer was still active");
            return false;
        }
        if (!writerFinishedWhilePinned) {
            result_.error = QStringLiteral("Snapshot reader was not pinned when writer completed");
            return false;
        }
        return result_.error.isEmpty();
    }

    const QString &error() const { return result_.error; }
    const QList<SearchHit> &hits() const { return result().hits; }

    const Result &result() const
    {
        if (!joined_)
            throw std::logic_error("Snapshot query result requires successful join");
        return result_;
    }

private:
    std::shared_ptr<State> state_;
    std::shared_ptr<std::atomic_bool> cancelled_;
    std::future<Result> future_;
    Result result_;
    bool started_ = false;
    bool joined_ = false;
};

// The barrier/state precedes IndexRepository. The returned paused query is
// declared after IndexRepository, and therefore is torn down before it.
class SnapshotBarrier {
public:
    SnapshotBarrier()
        : state_(std::make_shared<PairedSnapshotProbe::State>()),
          observer_(PairedSnapshotProbe::observer(state_))
    {
    }

    void observe(const QString &point) { observer_(point); }

    PairedSnapshotProbe start(const IndexRepository &repository, SearchOptions options)
    {
        return PairedSnapshotProbe(state_, repository, std::move(options));
    }

private:
    std::shared_ptr<PairedSnapshotProbe::State> state_;
    std::function<void(const QString &)> observer_;
};


} // namespace

class IndexRepositoryRegressionTest : public QObject
{
    Q_OBJECT

private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
    }

    void walBudgetMustBePositive()
    {
        for (const qint64 budget : {qint64(0), qint64(-1)}) {
            TestEnvironment environment(QStringLiteral("invalid-wal-budget"));
            QVERIFY2(environment.isValid(), qPrintable(environment.error()));
            StorageOptions options;
            options.maxWalBytes = budget;
            IndexRepository repository(options);
            QString error;
            QVERIFY(!repository.open(environment.runtimePaths(), &error));
            QVERIFY2(error.contains(QStringLiteral("WAL byte budget must be positive")), qPrintable(error));
            QVERIFY(!QFileInfo::exists(environment.runtimePaths().databaseFile()));
        }
    }

    void oversizedWalResumesAfterReadersRelease()
    {
        TestEnvironment environment(QStringLiteral("wal-reader-recovery"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        StorageOptions options;
        options.maxWalBytes = 256 * 1024;
        IndexRepository repository(options);
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString original = environment.path(QStringLiteral("original.txt"));
        QVERIFY2(repository.replaceFile(original, QStringLiteral("original-v1"),
                    {makeSegment(QStringLiteral("waloriginaltoken"))}, false, &error), qPrintable(error));

        // This connection performs only explicit checkpoints. All fixture
        // contents and WAL frames are generated by normal repository writes.
        auto checkpointConnection = openTestDatabase(repository.databasePath(), SQLITE_OPEN_READWRITE, &error);
        QVERIFY2(checkpointConnection != nullptr, qPrintable(error));
        // Load the schema/WAL state before checkpointing on a fresh connection.
        QCOMPARE(testFileCount(checkpointConnection.get()), qint64(1));
        QCOMPARE(sqlite3_wal_checkpoint_v2(checkpointConnection.get(), "main",
                     SQLITE_CHECKPOINT_TRUNCATE, nullptr, nullptr), SQLITE_OK);
        const QString walPath = repository.databasePath() + QStringLiteral("-wal");
        QCOMPARE(QFileInfo(walPath).size(), qint64(0));

        auto oldReader = openTestDatabase(repository.databasePath(), SQLITE_OPEN_READONLY, &error);
        QVERIFY2(oldReader != nullptr, qPrintable(error));
        QVERIFY2(executeTestSql(oldReader.get(), "BEGIN", &error), qPrintable(error));
        QCOMPARE(testFileCount(oldReader.get()), qint64(1)); // Pin an actual read snapshot.
        QVector<float> embedding(embeddingDimensions, 0.0f);
        embedding[700] = 1.0f;
        const QString large = environment.path(QStringLiteral("committed.txt"));
        const QVector<Segment> segments(256, makeSegment(QStringLiteral("walcommittedtoken"), embedding));
        QVERIFY2(repository.replaceFile(large, QStringLiteral("committed-v1"), segments,
                                        false, &error), qPrintable(error));
        QVERIFY(QFileInfo(walPath).size() > options.maxWalBytes);
        QCOMPARE(testFileCount(oldReader.get()), qint64(1));
        const auto committedGeneration = repository.status(&error).value(QStringLiteral("generation"));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const QString rejected = environment.path(QStringLiteral("rejected.txt"));
        const QVector<Segment> rejectedSegments{makeSegment(QStringLiteral("walrejectedtoken"))};
        QVERIFY(!repository.replaceFile(rejected, QStringLiteral("never-committed"), rejectedSegments,
                                        false, &error));
        QVERIFY2(error.contains(QStringLiteral("WAL")), qPrintable(error));
        QCOMPARE(repository.status(&error).value(QStringLiteral("generation")), committedGeneration);
        QVERIFY2(error.isEmpty(), qPrintable(error));

        // A newer reader lets PASSIVE checkpoint every frame, but still holds
        // a WAL read mark that prevents TRUNCATE. This covers the path where
        // accidentally retaining the 5-second busy timeout blocks publication.
        auto currentReader = openTestDatabase(repository.databasePath(), SQLITE_OPEN_READONLY, &error);
        QVERIFY2(currentReader != nullptr, qPrintable(error));
        QVERIFY2(executeTestSql(currentReader.get(), "BEGIN", &error), qPrintable(error));
        QCOMPARE(testFileCount(currentReader.get()), qint64(2));
        QVERIFY2(executeTestSql(oldReader.get(), "COMMIT", &error), qPrintable(error));
        oldReader.reset();
        int logFrames = -1, checkpointedFrames = -1;
        QCOMPARE(sqlite3_wal_checkpoint_v2(checkpointConnection.get(), "main", SQLITE_CHECKPOINT_PASSIVE,
                     &logFrames, &checkpointedFrames), SQLITE_OK);
        QVERIFY(logFrames > 0);
        QCOMPARE(checkpointedFrames, logFrames);
        QVERIFY(QFileInfo(walPath).size() > options.maxWalBytes);
        QElapsedTimer elapsed;
        elapsed.start();
        QVERIFY(!repository.replaceFile(rejected, QStringLiteral("never-committed"), rejectedSegments,
                                        false, &error));
        const qint64 pausedMilliseconds = elapsed.elapsed();
        QVERIFY2(error.contains(QStringLiteral("WAL")), qPrintable(error));
        // This generous ceiling detects the fixed busy-timeout regression,
        // rather than imposing the separate search performance target on CI.
        QVERIFY2(pausedMilliseconds < 4000,
                 qPrintable(QStringLiteral("WAL pause waited %1 ms for a reader").arg(pausedMilliseconds)));
        QCOMPARE(repository.status(&error).value(QStringLiteral("generation")), committedGeneration);
        QVERIFY2(error.isEmpty(), qPrintable(error));

        QVERIFY2(executeTestSql(currentReader.get(), "COMMIT", &error), qPrintable(error));
        currentReader.reset();
        const QString resumed = environment.path(QStringLiteral("resumed.txt"));
        QVERIFY2(repository.replaceFile(resumed, QStringLiteral("resumed-v1"),
                    {makeSegment(QStringLiteral("walresumedtoken"))}, false, &error), qPrintable(error));
        QVERIFY(QFileInfo(walPath).size() <= options.maxWalBytes);
        QCOMPARE(repository.fileVersion(rejected, false, &error), QString{});
        QVERIFY2(error.isEmpty(), qPrintable(error));
        for (const auto &fixture : {
                 QStringList{original, QStringLiteral("waloriginaltoken"), QStringLiteral("original-v1")},
                 QStringList{large, QStringLiteral("walcommittedtoken"), QStringLiteral("committed-v1")},
                 QStringList{resumed, QStringLiteral("walresumedtoken"), QStringLiteral("resumed-v1")}}) {
            SearchOptions query;
            query.query = fixture[1];
            const auto hits = repository.search(query, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(hits.size(), qsizetype(1));
            QCOMPARE(hits.first().path, fixture[0]);
            QCOMPARE(hits.first().sourceVersion, fixture[2]);
        }
        SearchOptions rejectedQuery;
        rejectedQuery.query = QStringLiteral("walrejectedtoken");
        QVERIFY(repository.search(rejectedQuery, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
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

    void recentFilesLimitCountsFilesInsteadOfNewestFileSegments()
    {
        TestEnvironment environment(QStringLiteral("recent-files-regression"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString oldest = environment.path(QStringLiteral("oldest.txt"));
        const QString middle = environment.path(QStringLiteral("middle.txt"));
        const QString newest = environment.path(QStringLiteral("newest.txt"));
        auto image = makeSegment(QStringLiteral("image"));
        image.kind = QStringLiteral("image");
        QVector<Segment> longFile(400, makeSegment(QStringLiteral("newest text")));
        longFile.append(image);
        QVERIFY2(repository.replaceFiles({
            {oldest, QStringLiteral("one"), false, {makeSegment(QStringLiteral("oldest text"))}},
            {middle, QStringLiteral("one"), false, {makeSegment(QStringLiteral("middle text"))}},
            {newest, QStringLiteral("one"), false, longFile}
        }, &error), qPrintable(error));
        SearchOptions options;
        options.limit = 2;
        auto hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(2));
        QCOMPARE(hits[0].path, newest);
        QCOMPARE(hits[1].path, middle);
        QCOMPARE(hits[0].kind, QStringLiteral("image"));
        options.kind = QStringLiteral("text");
        hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(2));
        QCOMPARE(hits[0].path, newest);
        QCOMPARE(hits[0].kind, QStringLiteral("text"));
        QCOMPARE(hits[1].path, middle);
        options.enforceFolderRules = true;
        options.allowedRoots = {environment.root()};
        options.excludedRoots = {newest};
        hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(2));
        QCOMPARE(hits[0].path, middle);
        QCOMPARE(hits[1].path, oldest);
    }

    void fileListPagesEveryFileOnceWithStableModificationTies()
    {
        TestEnvironment environment(QStringLiteral("file-list-pages"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVector<IndexedFile> fixtures;
        QStringList insertedPaths;
        for (int index = 0; index < 205; ++index) {
            const QString path = environment.path(QStringLiteral("files/%1.txt").arg(index));
            insertedPaths.append(path);
            fixtures.append({path, QStringLiteral("one"), false,
                             {makeSegment(QStringLiteral("body must not be a list preview"))},
                             1000, QStringLiteral("text")});
        }
        QVERIFY2(repository.replaceFiles(fixtures, &error), qPrintable(error));

        FileListOptions options;
        options.limit = 100;
        QSet<qint64> identities;
        QSet<QString> paths;
        qint64 generation = -1;
        qint64 previousId = 0;
        qsizetype seen = 0;
        for (const int expectedCount : {100, 100, 5}) {
            const auto page = repository.listFiles(options);
            QVERIFY2(page.error.isEmpty(), qPrintable(page.error));
            QVERIFY(page.errorCode.isEmpty());
            QVERIFY(!page.metadataPending);
            QCOMPARE(page.files.size(), qsizetype(expectedCount));
            if (generation < 0)
                generation = page.browseGeneration;
            QCOMPARE(page.browseGeneration, generation);
            for (const auto &file : page.files) {
                QVERIFY(file.fileId > 0);
                if (seen > 0)
                    QVERIFY(file.fileId < previousId);
                QVERIFY(!identities.contains(file.fileId));
                QVERIFY(!paths.contains(file.path));
                QCOMPARE(file.path, insertedPaths.at(204 - seen));
                QCOMPARE(file.modifiedMs, qint64(1000));
                QCOMPARE(file.kind, QStringLiteral("text"));
                QCOMPARE(file.title, QFileInfo(file.path).fileName());
                QCOMPARE(file.sourceVersion, QStringLiteral("one"));
                QVERIFY(!file.code);
                identities.insert(file.fileId);
                paths.insert(file.path);
                previousId = file.fileId;
                ++seen;
            }
            QCOMPARE(page.hasMore, seen < 205);
            QCOMPARE(page.nextCursor.isEmpty(), seen == 205);
            options.cursor = page.nextCursor;
        }
        QCOMPARE(seen, qsizetype(205));
        QCOMPARE(identities.size(), qsizetype(205));
        QCOMPARE(paths, QSet<QString>(insertedPaths.cbegin(), insertedPaths.cend()));
    }

    void fileListCursorRejectsChangedVisibilityContext()
    {
        TestEnvironment environment(QStringLiteral("file-list-context"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString root = environment.path(QStringLiteral("selected"));
        const QString first = QDir(root).filePath(QStringLiteral("first.txt"));
        const QString second = QDir(root).filePath(QStringLiteral("second.txt"));
        const QString otherRoot = environment.path(QStringLiteral("other"));
        QVERIFY2(repository.replaceFiles({
            {first, QStringLiteral("one"), false, {makeSegment(QStringLiteral("first"))},
             1000, QStringLiteral("text")},
            {second, QStringLiteral("one"), false, {makeSegment(QStringLiteral("second"))},
             2000, QStringLiteral("text")},
            {QDir(root).filePath(QStringLiteral("source.cpp")), QStringLiteral("one"), true,
             {makeSegment(QStringLiteral("code"))}, 3000, QStringLiteral("code")}
        }, &error), qPrintable(error));
        FileListOptions original;
        original.limit = 1;
        original.root = root;
        original.kind = QStringLiteral("text");
        original.allowedRoots = {root};
        original.enforceFolderRules = true;
        original.configurationToken = QStringLiteral("configuration-one");
        const auto firstPage = repository.listFiles(original);
        QVERIFY2(firstPage.error.isEmpty(), qPrintable(firstPage.error));
        QCOMPARE(firstPage.files.size(), qsizetype(1));
        QVERIFY(firstPage.hasMore);
        QVERIFY(!firstPage.nextCursor.isEmpty());
        original.cursor = firstPage.nextCursor;

        const QList<std::pair<QString, std::function<void(FileListOptions &)>>> changes{
            {QStringLiteral("code"), [](auto &options) { options.code = true; }},
            {QStringLiteral("kind"), [](auto &options) { options.kind = QStringLiteral("image"); }},
            {QStringLiteral("root"), [&](auto &options) { options.root = otherRoot; }},
            {QStringLiteral("allowed roots"), [&](auto &options) { options.allowedRoots = {otherRoot}; }},
            {QStringLiteral("excluded roots"), [&](auto &options) { options.excludedRoots = {first}; }},
            {QStringLiteral("folder enforcement"), [](auto &options) { options.enforceFolderRules = false; }},
            {QStringLiteral("configuration"), [](auto &options) {
                options.configurationToken = QStringLiteral("configuration-two");
            }}
        };
        for (const auto &[description, change] : changes) {
            auto changed = original;
            change(changed);
            const auto rejected = repository.listFiles(changed);
            QCOMPARE(rejected.errorCode, QStringLiteral("cursor_stale"));
            QVERIFY2(!rejected.error.isEmpty(), qPrintable(description));
            QVERIFY(rejected.files.isEmpty());
            QVERIFY(rejected.nextCursor.isEmpty());
            QVERIFY(!rejected.hasMore);
        }
        auto malformed = original;
        malformed.cursor = QStringLiteral("***not-a-cursor***");
        const auto rejected = repository.listFiles(malformed);
        QCOMPARE(rejected.errorCode, QStringLiteral("invalid_cursor"));
        QVERIFY(!rejected.error.isEmpty());
        QVERIFY(rejected.files.isEmpty());
        auto cursorFields = QJsonDocument::fromJson(QByteArray::fromBase64(
            original.cursor.toLatin1(), QByteArray::Base64UrlEncoding)).object();
        QVERIFY(!cursorFields.isEmpty());
        cursorFields.insert(QStringLiteral("v"), 2);
        malformed.cursor = QString::fromLatin1(QJsonDocument(cursorFields).toJson(QJsonDocument::Compact)
            .toBase64(QByteArray::Base64UrlEncoding | QByteArray::OmitTrailingEquals));
        const auto unsupportedVersion = repository.listFiles(malformed);
        QCOMPARE(unsupportedVersion.errorCode, QStringLiteral("invalid_cursor"));
        QVERIFY(unsupportedVersion.files.isEmpty());
        cursorFields.insert(QStringLiteral("v"), 1);
        cursorFields.insert(QStringLiteral("index"), QStringLiteral("another-index-uuid"));
        malformed.cursor = QString::fromLatin1(QJsonDocument(cursorFields).toJson(QJsonDocument::Compact)
            .toBase64(QByteArray::Base64UrlEncoding | QByteArray::OmitTrailingEquals));
        const auto differentIndex = repository.listFiles(malformed);
        QCOMPARE(differentIndex.errorCode, QStringLiteral("cursor_stale"));
        QVERIFY(differentIndex.files.isEmpty());
        const auto secondPage = repository.listFiles(original);
        QVERIFY2(secondPage.error.isEmpty(), qPrintable(secondPage.error));
        QCOMPARE(secondPage.files.size(), qsizetype(1));
        QCOMPARE(secondPage.files.first().path, first);
        QVERIFY(!secondPage.hasMore);
    }

    void fileListCursorBecomesStaleAfterMembershipOrMetadataChanges_data()
    {
        QTest::addColumn<QString>("change");
        for (const auto *change : {"insert", "remove", "modified-time", "kind", "code-membership"})
            QTest::newRow(change) << QString::fromLatin1(change);
    }

    void fileListCursorBecomesStaleAfterMembershipOrMetadataChanges()
    {
        QFETCH(QString, change);
        TestEnvironment environment(QStringLiteral("file-list-generation"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString first = environment.path(QStringLiteral("first.txt"));
        const QString second = environment.path(QStringLiteral("second.txt"));
        const QString third = environment.path(QStringLiteral("third.txt"));
        const auto segment = makeSegment(QStringLiteral("body"));
        QVERIFY2(repository.replaceFiles({
            {first, QStringLiteral("one"), false, {segment}, 1000, QStringLiteral("text")},
            {second, QStringLiteral("one"), false, {segment}, 2000, QStringLiteral("text")},
            {third, QStringLiteral("one"), false, {segment}, 3000, QStringLiteral("text")}
        }, &error), qPrintable(error));
        FileListOptions options;
        options.limit = 1;
        const auto before = repository.listFiles(options);
        QVERIFY2(before.error.isEmpty(), qPrintable(before.error));
        QVERIFY(before.hasMore);
        options.cursor = before.nextCursor;
        if (change == QStringLiteral("insert")) {
            QVERIFY2(repository.replaceFile(environment.path(QStringLiteral("fourth.txt")),
                QStringLiteral("one"), {segment}, false, &error, {}, 4000,
                QStringLiteral("text")), qPrintable(error));
        } else if (change == QStringLiteral("remove")) {
            QVERIFY2(repository.removeFile(first, false, &error), qPrintable(error));
        } else if (change == QStringLiteral("modified-time")) {
            QVERIFY2(repository.updateFileMetadata({{first, false, 4000, QStringLiteral("text"), {}}},
                &error), qPrintable(error));
        } else if (change == QStringLiteral("kind")) {
            QVERIFY2(repository.updateFileMetadata({{first, false, 1000, QStringLiteral("image"), {}}},
                &error), qPrintable(error));
        } else {
            QCOMPARE(change, QStringLiteral("code-membership"));
            QVERIFY2(repository.removeFile(first, false, &error), qPrintable(error));
            QVERIFY2(repository.replaceFile(first, QStringLiteral("one"), {segment}, true,
                &error, {}, 1000, QStringLiteral("code")), qPrintable(error));
        }
        const auto stale = repository.listFiles(options);
        QCOMPARE(stale.errorCode, QStringLiteral("cursor_stale"));
        QVERIFY(!stale.error.isEmpty());
        QVERIFY(stale.files.isEmpty());
        QVERIFY(stale.nextCursor.isEmpty());
        QVERIFY(!stale.hasMore);
        options.cursor.clear();
        const auto refreshed = repository.listFiles(options);
        QVERIFY2(refreshed.error.isEmpty(), qPrintable(refreshed.error));
        QVERIFY(refreshed.browseGeneration > before.browseGeneration);
        QVERIFY(!refreshed.files.isEmpty());
    }

    void fileListCursorSurvivesSegmentContinuationAndAnnPublication()
    {
        TestEnvironment environment(QStringLiteral("file-list-segment-continuation"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVector<float> vector(embeddingDimensions, 0.0f);
        vector[0] = 1.0f;
        const QString first = environment.path(QStringLiteral("first.txt"));
        const QString second = environment.path(QStringLiteral("second.txt"));
        const QString third = environment.path(QStringLiteral("third.txt"));
        const auto originalSegment = makeSegment(QStringLiteral("first segment"), vector);
        QVERIFY2(repository.replaceFiles({
            {first, QStringLiteral("one"), false, {originalSegment}, 1000, QStringLiteral("text")},
            {second, QStringLiteral("one"), false, {originalSegment}, 2000, QStringLiteral("text")},
            {third, QStringLiteral("one|partial:1"), false, {originalSegment}, 3000, QStringLiteral("text")}
        }, &error), qPrintable(error));
        FileListOptions options;
        options.limit = 1;
        const auto firstPage = repository.listFiles(options);
        QVERIFY2(firstPage.error.isEmpty(), qPrintable(firstPage.error));
        QCOMPARE(firstPage.files.size(), qsizetype(1));
        QCOMPARE(firstPage.files.first().path, third);
        QVERIFY(firstPage.hasMore);
        const auto originalId = firstPage.files.first().fileId;
        options.cursor = firstPage.nextCursor;
        const auto initialStatus = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.replaceFile(third, QStringLiteral("one|partial:2"),
            {originalSegment, makeSegment(QStringLiteral("continued segment"), vector)},
            false, &error, {}, 3000, QStringLiteral("text")), qPrintable(error));
        auto nextPage = repository.listFiles(options);
        QVERIFY2(nextPage.error.isEmpty(), qPrintable(nextPage.error));
        QCOMPARE(nextPage.browseGeneration, firstPage.browseGeneration);
        QCOMPARE(nextPage.files.size(), qsizetype(1));
        QCOMPARE(nextPage.files.first().path, second);
        QVERIFY(repository.status(&error).value(QStringLiteral("generation")).toInteger()
                > initialStatus.value(QStringLiteral("generation")).toInteger());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(repository.flush(&error), qPrintable(error));
        const auto published = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(published.value(QStringLiteral("pendingShards")).toInteger(), qint64(0));
        QCOMPARE(published.value(QStringLiteral("annVectors")).toInteger(), qint64(4));
        nextPage = repository.listFiles(options);
        QVERIFY2(nextPage.error.isEmpty(), qPrintable(nextPage.error));
        QCOMPARE(nextPage.browseGeneration, firstPage.browseGeneration);
        QCOMPARE(nextPage.files.size(), qsizetype(1));
        QCOMPARE(nextPage.files.first().path, second);
        options.cursor = nextPage.nextCursor;
        const auto lastPage = repository.listFiles(options);
        QVERIFY2(lastPage.error.isEmpty(), qPrintable(lastPage.error));
        QCOMPARE(lastPage.files.size(), qsizetype(1));
        QCOMPARE(lastPage.files.first().path, first);
        QVERIFY(!lastPage.hasMore);
        options.cursor.clear();
        const auto refreshed = repository.listFiles(options);
        QVERIFY2(refreshed.error.isEmpty(), qPrintable(refreshed.error));
        QCOMPARE(refreshed.files.size(), qsizetype(1));
        QCOMPARE(refreshed.files.first().fileId, originalId);
        QCOMPARE(refreshed.files.first().sourceVersion, QStringLiteral("one|partial:2"));
        QCOMPARE(refreshed.browseGeneration, firstPage.browseGeneration);
    }

    void fileListPageDoesNotPinReadTransactionBetweenRequests()
    {
        TestEnvironment environment(QStringLiteral("file-list-short-transaction"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString first = environment.path(QStringLiteral("first.txt"));
        const QString second = environment.path(QStringLiteral("second.txt"));
        const auto segment = makeSegment(QStringLiteral("body"));
        QVERIFY2(repository.replaceFiles({
            {first, QStringLiteral("one"), false, {segment}, 1000, QStringLiteral("text")},
            {second, QStringLiteral("one"), false, {segment}, 2000, QStringLiteral("text")}
        }, &error), qPrintable(error));
        auto checkpoint = openTestDatabase(repository.databasePath(), SQLITE_OPEN_READWRITE, &error);
        QVERIFY2(checkpoint != nullptr, qPrintable(error));
        QCOMPARE(testFileCount(checkpoint.get()), qint64(2));
        QCOMPARE(sqlite3_wal_checkpoint_v2(checkpoint.get(), "main", SQLITE_CHECKPOINT_TRUNCATE,
            nullptr, nullptr), SQLITE_OK);
        FileListOptions options;
        options.limit = 1;
        // Keep the returned page and cursor alive while committing new frames.
        const auto page = repository.listFiles(options);
        QVERIFY2(page.error.isEmpty(), qPrintable(page.error));
        QVERIFY(page.hasMore);
        QVERIFY2(repository.replaceFile(first, QStringLiteral("one|partial:2"),
            {segment, makeSegment(QStringLiteral("appended body"))}, false, &error, {},
            1000, QStringLiteral("text")), qPrintable(error));
        const QString walPath = repository.databasePath() + QStringLiteral("-wal");
        QVERIFY(QFileInfo(walPath).size() > 0);
        // TRUNCATE fails with SQLITE_BUSY if listFiles retained the old read mark.
        QCOMPARE(sqlite3_wal_checkpoint_v2(checkpoint.get(), "main", SQLITE_CHECKPOINT_TRUNCATE,
            nullptr, nullptr), SQLITE_OK);
        QCOMPARE(QFileInfo(walPath).size(), qint64(0));
        options.cursor = page.nextCursor;
        const auto nextPage = repository.listFiles(options);
        QVERIFY2(nextPage.error.isEmpty(), qPrintable(nextPage.error));
        QCOMPARE(nextPage.files.size(), qsizetype(1));
        QCOMPARE(nextPage.files.first().path, first);
        QCOMPARE(nextPage.browseGeneration, page.browseGeneration);
    }

    void punctuationOnlySearchDoesNotReturnRecentFiles()
    {
        TestEnvironment environment(QStringLiteral("punctuation-search"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVERIFY2(repository.replaceFile(environment.path(QStringLiteral("indexed.txt")),
            QStringLiteral("one"), {makeSegment(QStringLiteral("searchable body"))}, false,
            &error, {}, 1000, QStringLiteral("text")), qPrintable(error));
        SearchOptions options;
        options.query = QStringLiteral("searchable");
        QCOMPARE(repository.search(options, &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        for (const auto &query : {QStringLiteral("!!!"), QStringLiteral("，。！？"),
                                 QStringLiteral("()[]{}\""), QStringLiteral("***"),
                                 QStringLiteral("  ...  ")}) {
            options.query = query;
            const auto hits = repository.search(options, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QVERIFY2(hits.isEmpty(), qPrintable(QStringLiteral("Punctuation query returned recent files: %1").arg(query)));
        }
    }

    void exactCosineUsesTheSameFloatBoundaryForCalibration()
    {
        QVector<float> query(embeddingDimensions, 0.0f);
        query[0] = 0.6f;
        query[1] = 0.8f;
        QVector<float> stored(embeddingDimensions, 0.0f);
        stored[0] = 1.0f;
        const auto normalized = embedding_math::normalized(query);
        const double production = embedding_math::cosine(normalized, stored);
        const double calibration = embedding_math::cosine(embedding_math::normalized(query), stored);
        QCOMPARE(production, calibration);
        QCOMPARE(production, double(normalized[0]));
        const double threshold = std::nextafter(calibration, std::numeric_limits<double>::infinity());
        QVERIFY(production < threshold);
        // Re-normalizing the rounded float query in double precision changes
        // the boundary and cannot be used to fit production thresholds.
        const double doubleNorm = double(normalized[0]) * normalized[0]
            + double(normalized[1]) * normalized[1];
        const double otherCosine = double(normalized[0]) / std::sqrt(doubleNorm);
        QVERIFY(otherCosine < production);
        QVERIFY(std::nextafter(otherCosine, std::numeric_limits<double>::infinity()) < production);
    }

    void tokenlessSemanticQueriesKeepProvidedEmbedding()
    {
        TestEnvironment environment(QStringLiteral("tokenless-semantic-search"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVector<float> embedding(embeddingDimensions, 0.0f);
        embedding[0] = 1.0f;
        auto image = makeSegment({}, embedding);
        image.kind = QStringLiteral("image");
        image.modality = QStringLiteral("image");
        const QString path = environment.path(QStringLiteral("cat.png"));
        QVERIFY2(repository.replaceFile(path, QStringLiteral("one"), {image}, false,
            &error, {}, 1000, QStringLiteral("image")), qPrintable(error));
        SearchOptions options;
        options.query = QString::fromUtf8("\xf0\x9f\x90\xb1");
        options.queryEmbedding = embedding;
        options.semanticCandidateMode = SemanticCandidateMode::RawCandidates;
        const auto hits = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hits.size(), qsizetype(1));
        QCOMPARE(hits.first().path, path);
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
        options.semanticCandidateMode = SemanticCandidateMode::RawCandidates;
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
        if (semantic) {
            options.queryEmbedding = nearest;
            options.semanticCandidateMode = SemanticCandidateMode::RawCandidates;
        } else
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

    void indexedFolderRangePreservesLiteralAndBoundarySemantics()
    {
        TestEnvironment environment(QStringLiteral("folders-binary-range"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        const QString root = environment.path(QString::fromUtf8("资料 %_ 空格"));
        const QString child = QDir(root).filePath(QString::fromUtf8("中文/照片😀.txt"));
        const QString image = QDir(root).filePath(QStringLiteral("picture.png"));
        const QString excluded = QDir(root).filePath(QStringLiteral("hidden"));
        const QString hiddenChild = QDir(excluded).filePath(QStringLiteral("secret.txt"));
        const QString exclusionNeighbor = QDir(root).filePath(QStringLiteral("hidden0/visible.txt"));
        const QString upperBoundary = root + QStringLiteral("0");
        const QString prefixNeighbor = root + QStringLiteral("-extra/file.txt");
        const QString patternNeighbor = environment.path(QString::fromUtf8("资料 arbitraryX 空格/file.txt"));
        QVector<float> embedding(embeddingDimensions, 0.0f);
        embedding[0] = 1.0f;
        QVector<IndexedFile> files;
        for (const auto &path : {root, child, image, excluded, hiddenChild,
                                 exclusionNeighbor, upperBoundary, prefixNeighbor, patternNeighbor}) {
            auto segment = makeSegment(QStringLiteral("needle"), embedding);
            if (path == image)
                segment.kind = QStringLiteral("image");
            files.append({path, QStringLiteral("one"), false, {segment}});
        }
        QVERIFY2(repository.replaceFiles(files, &error), qPrintable(error));

        // Both FTS and exact-vector paths must retain the old directory
        // boundary semantics. No wildcard interpretation or root0 leak is
        // allowed when the SQL predicate becomes an indexed range.
        for (const bool semantic : {false, true}) {
            SearchOptions options;
            options.limit = 20;
            if (semantic) {
                options.queryEmbedding = embedding;
                options.semanticCandidateMode = SemanticCandidateMode::RawCandidates;
            } else
                options.query = QStringLiteral("needle");
            options.root = root + u'/';
            QCOMPARE(distinctPaths(repository.search(options, &error)),
                     (QSet<QString>{root, child, image, excluded, hiddenChild, exclusionNeighbor}));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            options.enforceFolderRules = true;
            options.allowedRoots = {root + u'/'};
            options.excludedRoots = {excluded};
            QCOMPARE(distinctPaths(repository.search(options, &error)),
                     (QSet<QString>{root, child, image, exclusionNeighbor}));
            QVERIFY2(error.isEmpty(), qPrintable(error));
            options.kind = QStringLiteral("text");
            QCOMPARE(distinctPaths(repository.search(options, &error)),
                     (QSet<QString>{root, child, exclusionNeighbor}));
            QVERIFY2(error.isEmpty(), qPrintable(error));
        }
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
            if (semantic) {
                options.queryEmbedding = embedding;
                options.semanticCandidateMode = SemanticCandidateMode::RawCandidates;
            } else
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
        auto snapshotState = std::make_shared<PairedSnapshotProbe::State>();
        StorageOptions storage;
        storage.faultHook = PairedSnapshotProbe::observer(snapshotState);
        IndexRepository repository(storage);
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QElapsedTimer elapsed;
        elapsed.start();

        constexpr int dominantSegments = 300;
        // Keep this strictly above the repository's 20,000-vector exact cutoff
        // after deleting the dominant file, so all filtered and unfiltered
        // queries with the live root still use the repository's index path.
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
        const QString sentinelA = QDir(liveRoot).filePath(QStringLiteral("sentinel-a"));
        const QString sentinelB = QDir(liveRoot).filePath(QStringLiteral("sentinel-b"));
        const QString sentinelD = QDir(liveRoot).filePath(QStringLiteral("sentinel-d"));
        constexpr int sentinelFiles = 3;
        QVector<IndexedFile> batch;
        batch.reserve(batchSize);
        // Place sentinels before the dominant file so old and replacement A
        // share a shard. The paired-snapshot assertions below also verify the
        // actual labels and outbox state instead of assuming particular IDs.
        batch.append({sentinelA, QStringLiteral("one"), false,
                      {makeSegment(QStringLiteral("old-a-token"), sentinelVector(0.995f))}});
        batch.append({sentinelB, QStringLiteral("one"), false,
                      {makeSegment(QStringLiteral("old-b-token"), sentinelVector(0.99f))}});
        batch.append({sentinelD, QStringLiteral("one"), false,
                      {makeSegment(QStringLiteral("stable-d-token"), sentinelVector(0.97f))}});
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
        QCOMPARE(ready.value(QStringLiteral("files")).toInteger(), qint64(liveFiles + sentinelFiles + 1));
        QCOMPARE(ready.value(QStringLiteral("vectors")).toInteger(), qint64(liveFiles + sentinelFiles + dominantSegments));
        QCOMPARE(ready.value(QStringLiteral("annVectors")).toInteger(), qint64(liveFiles + sentinelFiles + dominantSegments));
        QVERIFY(!ready.value(QStringLiteral("deferredMaintenance")).toBool());
        QCOMPARE(ready.value(QStringLiteral("pendingShards")).toInteger(), qint64(0));

        SearchOptions options;
        options.queryEmbedding = queryVector;
        options.semanticCandidateMode = SemanticCandidateMode::RawCandidates;
        options.limit = 2;
        // No text, root or kind filter: FTS and allow-list construction must not
        // accidentally hide either regression in the unfiltered ANN path.
        const auto beforeDeletion = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(beforeDeletion.size(), qsizetype(2));
        QCOMPARE(distinctPaths(beforeDeletion).size(), qsizetype(2));
        QCOMPARE(beforeDeletion.first().path, dominant);
        QVERIFY(beforeDeletion.last().path.startsWith(liveRoot + u'/'));

        // Excluding the dominant file still leaves >20,000 vectors, so folder
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
        QCOMPARE(afterRemoval.value(QStringLiteral("vectors")).toInteger(), qint64(liveFiles + sentinelFiles));
        // Do not flush or reopen here. The original near-neighbor IDs must still
        // exist in the immutable ANN graph and be excluded during traversal.
        QCOMPARE(afterRemoval.value(QStringLiteral("annVectors")).toInteger(), qint64(liveFiles + sentinelFiles + dominantSegments));
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
        // Reuse the same >20,000-vector published fixture. The sentinels have
        // wide cosine margins above the existing background codebook, so this
        // checks file identity/version rather than approximate score rounding.
        SearchOptions liveOptions = options;
        liveOptions.limit = 3;
        liveOptions.enforceFolderRules = true;
        liveOptions.allowedRoots = {liveRoot};
        const QString hiddenRoot = QDir(liveRoot).filePath(QStringLiteral("hidden"));
        const QString hiddenPath = QDir(hiddenRoot).filePath(QStringLiteral("image"));
        const QString outsideRoot = environment.path(QStringLiteral("outside"));
        const QString outsidePath = QDir(outsideRoot).filePath(QStringLiteral("best"));
        const QString newPath = QDir(liveRoot).filePath(QStringLiteral("sentinel-c"));
        const QString codePath = QDir(liveRoot).filePath(QStringLiteral("source.py"));
        liveOptions.excludedRoots = {hiddenRoot};
        const auto oldLive = repository.search(liveOptions, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hitPaths(oldLive), (QStringList{sentinelA, sentinelB, sentinelD}));
        for (const auto &hit : oldLive)
            QCOMPARE(hit.sourceVersion, QStringLiteral("one"));
        QVERIFY(oldLive.at(0).segmentId % 16 != oldLive.at(1).segmentId % 16);

        // The old reader captures SQL and ANN under the publication lock and
        // pauses at the first actual SQLite VM progress callback after capture.
        // The guard is declared after the repository, so every Qt early return
        // releases and joins the reader before the repository is destroyed.
        PairedSnapshotProbe pausedRead(snapshotState);
        pausedRead.start(repository, liveOptions);
        QVERIFY2(pausedRead.waitEntered(), "Paired-snapshot reader did not reach SQLite progress");
        auto hiddenSegment = makeSegment(QStringLiteral("hidden-token"), queryVector);
        hiddenSegment.kind = QStringLiteral("image");
        const QVector<IndexedFile> changes{
            {sentinelA, QStringLiteral("two"), false,
             {makeSegment(QStringLiteral("new-a-token"), sentinelVector(0.985f))}},
            {newPath, QStringLiteral("new-c"), false,
             {makeSegment(QStringLiteral("new-c-token"), queryVector)}},
            {hiddenPath, QStringLiteral("hidden"), false, {hiddenSegment}},
            {outsidePath, QStringLiteral("outside"), false,
             {makeSegment(QStringLiteral("outside-token"), queryVector)}},
            {codePath, QStringLiteral("code"), true,
             {makeSegment(QStringLiteral("code-token"), queryVector)}}};
        QVERIFY2(repository.replaceFiles(changes, &error), qPrintable(error));
        QVERIFY2(repository.removeFile(sentinelB, &error), qPrintable(error));
        const auto pending = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(pending.value(QStringLiteral("generation")).toInteger()
                > afterRemoval.value(QStringLiteral("generation")).toInteger());
        QCOMPARE(pending.value(QStringLiteral("annVectors")), ready.value(QStringLiteral("annVectors")));
        QCOMPARE(pending.value(QStringLiteral("annGeneration")), ready.value(QStringLiteral("annGeneration")));
        QVERIFY(!pending.value(QStringLiteral("deferredMaintenance")).toBool());
        QVERIFY(pending.value(QStringLiteral("pendingShards")).toInteger() >= 2);

        // Check pending-only delta/tombstone merge before any shard is rebuilt.
        // The barrier is one-shot, so this independent new reader must proceed.
        const auto pendingLive = repository.search(liveOptions, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hitPaths(pendingLive), (QStringList{newPath, sentinelA, sentinelD}));
        QCOMPARE(pendingLive.at(0).sourceVersion, QStringLiteral("new-c"));
        QCOMPARE(pendingLive.at(1).sourceVersion, QStringLiteral("two"));
        QCOMPARE(pendingLive.at(2).sourceVersion, QStringLiteral("one"));
        QVERIFY(pendingLive.at(1).segmentId != oldLive.at(0).segmentId);
        const int replacedShard = int(oldLive.at(0).segmentId % 16);
        QCOMPARE(int(pendingLive.at(1).segmentId % 16), replacedShard);
        const auto replacedShardPending = pendingEventsForShard(repository.databasePath(), replacedShard, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(replacedShardPending > 0);
        for (const auto &hit : pendingLive) {
            QVERIFY(hit.segmentId != oldLive.at(0).segmentId);
            QVERIFY(hit.segmentId != oldLive.at(1).segmentId);
        }

        // Only one publication is performed while the old SQLite reader is
        // pinned. Its checkpoint may wait for busy_timeout, so the barrier's
        // held-reader timeout is separate from its entry/completion timeouts.
        QVERIFY2(repository.flushOneShard(&error), qPrintable(error));
        const auto mixed = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY(mixed.value(QStringLiteral("pendingShards")).toInteger() > 0);
        QCOMPARE(mixed.value(QStringLiteral("pendingShards")).toInteger(),
                 pending.value(QStringLiteral("pendingShards")).toInteger() - 1);
        // Prove this publication rebuilt the shard containing old A. A buggy
        // old-SQL/new-ANN pairing would now miss that old label and fail the
        // old-reader identity assertion, rather than pass on unchanged shards.
        QCOMPARE(pendingEventsForShard(repository.databasePath(), replacedShard, &error), qint64(0));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const bool writerFinishedWhilePinned = pausedRead.remainsBlocked();
        const bool readerJoined = pausedRead.releaseAndJoin();
        QVERIFY(writerFinishedWhilePinned);
        QVERIFY(readerJoined);
        QVERIFY2(!pausedRead.hookTimedOut(), "Snapshot hook expired before explicit release");
        QVERIFY2(pausedRead.result().error.isEmpty(), qPrintable(pausedRead.result().error));
        QCOMPARE(hitIdentities(pausedRead.result().hits), hitIdentities(oldLive));
        const auto mixedLive = repository.search(liveOptions, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hitIdentities(mixedLive), hitIdentities(pendingLive));

        QVERIFY2(repository.flush(&error), qPrintable(error));
        const auto published = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(published.value(QStringLiteral("pendingShards")).toInteger(), qint64(0));
        QCOMPARE(published.value(QStringLiteral("annVectors")), published.value(QStringLiteral("vectors")));
        const auto publishedLive = repository.search(liveOptions, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hitIdentities(publishedLive), hitIdentities(pendingLive));

        // Alternate independent options without changing the data generation.
        // A future allow-list cache must not reuse another request's folder,
        // code, or kind permissions. Small kind/code subsets also verify their
        // SQL path but are not claimed as ANN branch coverage.
        auto changedRules = liveOptions;
        changedRules.excludedRoots.clear();
        QVERIFY(distinctPaths(repository.search(changedRules, &error)).contains(hiddenPath));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        changedRules = liveOptions;
        changedRules.allowedRoots = {outsideRoot};
        QCOMPARE(hitPaths(repository.search(changedRules, &error)), (QStringList{outsidePath}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        changedRules = liveOptions;
        changedRules.allowedRoots.clear();
        QVERIFY(repository.search(changedRules, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        changedRules = liveOptions;
        changedRules.allowedRoots = {liveRoot, hiddenRoot};
        QVERIFY(distinctPaths(repository.search(changedRules, &error)).contains(hiddenPath));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        changedRules = liveOptions;
        changedRules.kind = QStringLiteral("text");
        QCOMPARE(hitIdentities(repository.search(changedRules, &error)), hitIdentities(pendingLive));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        changedRules.kind = QStringLiteral("image");
        QVERIFY(repository.search(changedRules, &error).isEmpty());
        QVERIFY2(error.isEmpty(), qPrintable(error));
        changedRules = liveOptions;
        changedRules.code = true;
        QCOMPARE(hitPaths(repository.search(changedRules, &error)), (QStringList{codePath}));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(hitIdentities(repository.search(liveOptions, &error)), hitIdentities(pendingLive));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const auto unchanged = repository.status(&error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(unchanged.value(QStringLiteral("generation")), published.value(QStringLiteral("generation")));
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));

        qInfo().noquote() << "ANN aggregation/filter/snapshot fixture:" << liveFiles + sentinelFiles
                          << "initial live vectors," << dominantSegments << "deleted graph labels,"
                          << elapsed.elapsed() << "ms, observed logical file bytes"
                          << directoryBytes(environment.root());
    }
};

QTEST_GUILESS_MAIN(IndexRepositoryRegressionTest)
#include "index_repository_regression_test.moc"
