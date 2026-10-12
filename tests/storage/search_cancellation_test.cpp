#include "storage/index_repository.h"
#include "support/test_environment.h"
#include "support/sqlite_database.h"

#include <QtTest>
#include <sqlite3.h>
#include <condition_variable>
#include <future>
#include <mutex>
#include <stdexcept>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

class SearchCancellationTest : public QObject {
    Q_OBJECT
private slots:
    void cancelFromInsideSqliteVirtualMachineReleasesReader()
    {
        TestEnvironment environment(QStringLiteral("running-sqlite-cancellation"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        std::mutex mutex;
        std::condition_variable changed;
        bool entered = false;
        bool release = false;
        std::atomic_bool armed = false;
        auto cancelled = std::make_shared<std::atomic_bool>(false);
        StorageOptions storage;
        storage.faultHook = [&](const QString &point) {
            if (point != QStringLiteral("sqlite-search-progress") || !armed.exchange(false))
                return;
            std::unique_lock lock(mutex);
            entered = true;
            changed.notify_all();
            changed.wait(lock, [&] { return release; });
        };
        IndexRepository repository(storage);
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVector<IndexedFile> files;
        for (int i = 0; i < 512; ++i) {
            Segment segment;
            segment.kind = QStringLiteral("text");
            segment.text = QStringLiteral("needle deterministic SQLite progress record %1").arg(i);
            files.append({environment.path(QStringLiteral("files/%1.txt").arg(i)),
                          QStringLiteral("version-%1").arg(i), false, {segment}});
        }
        QVERIFY2(repository.replaceFiles(files, &error), qPrintable(error));
        SearchOptions options;
        options.query = QStringLiteral("needle");
        options.limit = 100;
        options.cancelled = cancelled;
        armed = true;
        QString searchError;
        auto running = std::async(std::launch::async, [&] {
            return repository.search(options, &searchError);
        });
        bool observed = false;
        bool startedWithoutCancellation = false;
        {
            std::unique_lock lock(mutex);
            observed = changed.wait_for(lock, std::chrono::seconds(10), [&] { return entered; });
            startedWithoutCancellation = !cancelled->load();
            cancelled->store(true);
            release = true;
        }
        changed.notify_all();
        const auto completed = running.wait_for(std::chrono::seconds(10));
        // Assertions follow barrier release so a failure never leaves the test
        // thread or the SQLite read transaction waiting on a destroyed fixture.
        QVERIFY(observed);
        QVERIFY(startedWithoutCancellation);
        QVERIFY(completed == std::future_status::ready);
        QVERIFY(running.get().isEmpty());
        QCOMPARE(searchError, QStringLiteral("Search cancelled"));

        sqlite3 *raw = nullptr;
        const int opened = test::openSqliteDatabase(repository.databasePath(), &raw,
                                                    SQLITE_OPEN_READWRITE | SQLITE_OPEN_NOMUTEX);
        std::unique_ptr<sqlite3, decltype(&sqlite3_close)> database(raw, sqlite3_close);
        QCOMPARE(opened, SQLITE_OK);
        sqlite3_busy_timeout(raw, 1000);
        QCOMPARE(sqlite3_exec(raw, "SELECT count(*) FROM meta", nullptr, nullptr, nullptr), SQLITE_OK);
        int log = -1, checkpointed = -1;
        QCOMPARE(sqlite3_wal_checkpoint_v2(raw, nullptr, SQLITE_CHECKPOINT_TRUNCATE,
                                          &log, &checkpointed), SQLITE_OK);
        QCOMPARE(log, 0);
        QCOMPARE(checkpointed, 0);

        cancelled->store(false);
        const auto retry = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(retry.size(), qsizetype(100));
        Segment newSegment;
        newSegment.kind = QStringLiteral("text");
        newSegment.text = QStringLiteral("aftercancellationtoken");
        const auto newPath = environment.path(QStringLiteral("after.txt"));
        QVERIFY2(repository.replaceFile(newPath, QStringLiteral("new-version"), {newSegment}, false, &error), qPrintable(error));
        options.query = QStringLiteral("aftercancellationtoken");
        const auto after = repository.search(options, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(after.size(), qsizetype(1));
        QCOMPARE(after.first().path, newPath);
        QVERIFY2(repository.checkIntegrity(&error), qPrintable(error));
    }

    void observationExceptionNeverCrossesSqliteCAbi()
    {
        TestEnvironment environment(QStringLiteral("sqlite-hook-exception"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        std::atomic_bool armed = false;
        StorageOptions storage;
        storage.faultHook = [&](const QString &point) {
            if (point == QStringLiteral("sqlite-search-progress") && armed.exchange(false))
                throw std::runtime_error("injected observation failure");
        };
        IndexRepository repository(storage);
        QString error;
        QVERIFY2(repository.open(environment.runtimePaths(), &error), qPrintable(error));
        QVector<Segment> segments;
        for (int i = 0; i < 256; ++i) {
            Segment segment;
            segment.kind = QStringLiteral("text");
            segment.text = QStringLiteral("hookexceptionneedle %1").arg(i);
            segments.append(segment);
        }
        QVERIFY2(repository.replaceFile(environment.path(QStringLiteral("file.txt")), QStringLiteral("v1"), segments, false, &error), qPrintable(error));
        SearchOptions options;
        options.query = QStringLiteral("hookexceptionneedle");
        armed = true;
        QVERIFY(repository.search(options, &error).isEmpty());
        QVERIFY(!error.isEmpty());
        QVERIFY(!armed.load());
        QCOMPARE(repository.search(options, &error).size(), qsizetype(1));
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }
};

QTEST_GUILESS_MAIN(SearchCancellationTest)
#include "search_cancellation_test.moc"
