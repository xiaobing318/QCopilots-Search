#include "storage/index_repository.h"
#include "support/test_environment.h"

#include <QCoreApplication>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QProcess>
#include <QSaveFile>
#include <QStandardPaths>
#include <QUuid>
#include <QtTest>

#include <sqlite3.h>
#include <algorithm>
#include <memory>

#ifndef QCOPILOTS_SEARCH_RECOVERY_CHILD
#error "Set QCOPILOTS_SEARCH_RECOVERY_CHILD to the recovery child target file"
#endif

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

namespace {
class OwnedChild {
public:
    ~OwnedChild()
    {
        if (process.state() != QProcess::NotRunning) {
            process.kill();
            process.waitForFinished(3000);
        }
    }

    bool run(const TestEnvironment &environment, const RuntimePaths &paths,
             const QString &token, QString *error)
    {
        process.setProgram(QString::fromUtf8(QCOPILOTS_SEARCH_RECOVERY_CHILD));
        process.setArguments({QStringLiteral("--test-root"), environment.root(),
                              QStringLiteral("--profile-dir"), paths.profileDir,
                              QStringLiteral("--fault-token"), token});
        // An unrelated working directory catches accidental relative paths.
        process.setWorkingDirectory(paths.tempDir);
        QElapsedTimer elapsed;
        elapsed.start();
        process.start();
        if (!process.waitForStarted(5000)) {
            *error = QStringLiteral("Cannot start owned recovery child: %1").arg(process.errorString());
            return false;
        }
        const int remaining = std::max(1, 27000 - int(elapsed.elapsed()));
        if (!process.waitForFinished(remaining)) {
            process.kill();
            process.waitForFinished(3000);
            *error = QStringLiteral("Owned recovery child exceeded its 30-second deadline\n%1")
                         .arg(QString::fromUtf8(process.readAllStandardError()));
            return false;
        }
        *error = QString::fromUtf8(process.readAllStandardError());
        return true;
    }

    QProcess process;
};

bool writeOwnership(const TestEnvironment &environment, const RuntimePaths &paths,
                    const QString &token, QString *error)
{
    if (!paths.ensure(error))
        return false;
    const QJsonObject ownership{{QStringLiteral("token"), token},
                               {QStringLiteral("profile"), QFileInfo(paths.profileDir).canonicalFilePath()}};
    QSaveFile marker(environment.path(QStringLiteral("qcopilots-search-recovery-owner.json")));
    const QByteArray bytes = QJsonDocument(ownership).toJson(QJsonDocument::Compact);
    if (!marker.open(QIODevice::WriteOnly) || marker.write(bytes) != bytes.size() || !marker.commit()) {
        *error = marker.errorString();
        return false;
    }
    return true;
}

qint64 scalar(const QString &databasePath, const char *sql, QString *error,
              qint64 shard = -1, qint64 watermark = -1)
{
    sqlite3 *rawDatabase = nullptr;
    const int opened = sqlite3_open_v2(databasePath.toUtf8().constData(), &rawDatabase,
                                      SQLITE_OPEN_READONLY, nullptr);
    std::unique_ptr<sqlite3, decltype(&sqlite3_close)> database(rawDatabase, sqlite3_close);
    if (opened != SQLITE_OK) {
        *error = QStringLiteral("Cannot inspect recovery database: %1").arg(databasePath);
        return -1;
    }
    sqlite3_busy_timeout(database.get(), 5000);
    sqlite3_stmt *rawStatement = nullptr;
    const int prepared = sqlite3_prepare_v2(database.get(), sql, -1, &rawStatement, nullptr);
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(rawStatement, sqlite3_finalize);
    if (prepared != SQLITE_OK
        || (shard >= 0 && sqlite3_bind_int64(statement.get(), 1, shard) != SQLITE_OK)
        || (watermark >= 0 && sqlite3_bind_int64(statement.get(), 2, watermark) != SQLITE_OK)
        || sqlite3_step(statement.get()) != SQLITE_ROW) {
        *error = QString::fromUtf8(sqlite3_errmsg(database.get()));
        return -1;
    }
    error->clear();
    return sqlite3_column_int64(statement.get(), 0);
}

Segment segment(const QString &text, int component)
{
    Segment result;
    result.kind = QStringLiteral("text");
    result.modality = QStringLiteral("text");
    result.text = text;
    result.embedding = QVector<float>(embeddingDimensions, 0.0f);
    result.embedding[component] = 1.0f;
    return result;
}
} // namespace

class RecoveryProcessTest : public QObject {
    Q_OBJECT

private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
        QVERIFY2(QFileInfo::exists(QString::fromUtf8(QCOPILOTS_SEARCH_RECOVERY_CHILD)),
                 "The configured recovery child executable must exist");
    }

    void rejectsUnownedProfileBeforeOpeningDatabase()
    {
        TestEnvironment environment(QStringLiteral("recovery-owner"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        const QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
        QString error;
        QVERIFY2(writeOwnership(environment, paths, token, &error), qPrintable(error));
        OwnedChild child;
        QVERIFY2(child.run(environment, paths, QUuid::createUuid().toString(), &error), qPrintable(error));
        QCOMPARE(child.process.exitStatus(), QProcess::NormalExit);
        QCOMPARE(child.process.exitCode(), 2);
        QVERIFY2(error.contains(QStringLiteral("ownership token or profile mismatch")), qPrintable(error));
        QVERIFY(!QFileInfo::exists(paths.databaseFile()));
        if (qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") != 1)
            QVERIFY2(environment.cleanup(&error), qPrintable(error));
    }

    void recoversPublishedManifestAndRemovesCoveredOutbox()
    {
        TestEnvironment environment(QStringLiteral("recovery-process"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = RuntimePaths::resolve(environment.path(QStringLiteral("profile/恢复 空格")));
        const QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
        QString error;
        QVERIFY2(writeOwnership(environment, paths, token, &error), qPrintable(error));
        {
            OwnedChild child;
            QVERIFY2(child.run(environment, paths, token, &error), qPrintable(error));
            QCOMPARE(child.process.exitStatus(), QProcess::NormalExit);
            QCOMPARE(child.process.exitCode(), 73);
        }

        // Inspect the durable fault boundary before opening the repository.
        // One real event plus 2048 duplicates must still be present, while the
        // persisted manifest already covers their shard and generation.
        QCOMPARE(scalar(paths.databaseFile(), "SELECT count(*) FROM ann_events", &error), qint64(2049));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const qint64 shard = scalar(paths.databaseFile(), "SELECT shard FROM segments", &error);
        QVERIFY2(shard >= 0 && shard < 16, qPrintable(error));
        const auto annDirectories = QDir(paths.indexDir).entryList(
            {QStringLiteral("qcopilots-search-ann-*")}, QDir::Dirs | QDir::NoDotAndDotDot);
        QCOMPARE(annDirectories.size(), qsizetype(1));
        QFile manifest(QDir(paths.indexDir).filePath(
            annDirectories.first() + QStringLiteral("/qcopilots-search-ann-manifest.json")));
        QVERIFY2(manifest.open(QIODevice::ReadOnly), qPrintable(manifest.errorString()));
        const QJsonArray shards = QJsonDocument::fromJson(manifest.readAll()).object()
                                      .value(QStringLiteral("shards")).toArray();
        manifest.close();
        QCOMPARE(shards.size(), qsizetype(16));
        const qint64 watermark = shards.at(int(shard)).toObject()
                                     .value(QStringLiteral("generation")).toString().toLongLong();
        QVERIFY(watermark > 0);
        constexpr auto coveredSql = "SELECT count(*) FROM ann_events WHERE shard=? AND generation<=?";
        QCOMPARE(scalar(paths.databaseFile(), coveredSql, &error, shard, watermark), qint64(2049));
        QVERIFY2(error.isEmpty(), qPrintable(error));

        {
        IndexRepository reopened;
        QVERIFY2(reopened.open(paths, &error), qPrintable(error));
        QCOMPARE(scalar(paths.databaseFile(), coveredSql, &error, shard, watermark), qint64(0));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(reopened.status(&error).value(QStringLiteral("pendingShards")).toInteger(), qint64(0));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        const QString original = QDir(paths.profileDir).filePath(QStringLiteral("committed.txt"));
        QCOMPARE(reopened.fileVersion(original, false, &error), QStringLiteral("recovery-committed-v1"));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        for (const bool semantic : {false, true}) {
            SearchOptions query;
            if (semantic)
                query.queryEmbedding = segment({}, 700).embedding;
            else
                query.query = QStringLiteral("recoverycommittedtoken");
            query.limit = 1;
            const auto hits = reopened.search(query, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(hits.size(), qsizetype(1));
            QCOMPARE(hits.first().path, original);
            QCOMPARE(hits.first().sourceVersion, QStringLiteral("recovery-committed-v1"));
        }

        const QString next = QDir(paths.profileDir).filePath(QStringLiteral("after-recovery.txt"));
        const auto nextSegment = segment(QStringLiteral("postmanifestrecoverytoken"), 701);
        QVERIFY2(reopened.replaceFile(next, QStringLiteral("after-recovery-v2"),
                                      {nextSegment}, false, &error), qPrintable(error));
        for (const bool semantic : {false, true}) {
            SearchOptions query;
            if (semantic)
                query.queryEmbedding = nextSegment.embedding;
            else
                query.query = nextSegment.text;
            query.limit = 1;
            const auto hits = reopened.search(query, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(hits.size(), qsizetype(1));
            QCOMPARE(hits.first().path, next);
            QCOMPARE(hits.first().sourceVersion, QStringLiteral("after-recovery-v2"));
        }
        QVERIFY2(reopened.flush(&error), qPrintable(error));
        QCOMPARE(reopened.status(&error).value(QStringLiteral("pendingShards")).toInteger(), qint64(0));
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QVERIFY2(reopened.checkIntegrity(&error), qPrintable(error));
        }
        // Close all SQLite and ANN handles before asking the portable fixture
        // owner to remove its directory. Cleanup failure must fail this test.
        if (qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") != 1)
            QVERIFY2(environment.cleanup(&error), qPrintable(error));
    }
};

QTEST_GUILESS_MAIN(RecoveryProcessTest)
#include "recovery_process_test.moc"
