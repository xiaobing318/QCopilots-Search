#include "storage/index_repository.h"
#include "../support/sqlite_database.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QStandardPaths>

#include <sqlite3.h>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <stdexcept>

using namespace qcopilots_search;

namespace {
void require(bool condition, const QString &message)
{
    if (!condition)
        throw std::runtime_error(message.toStdString());
}

// This program must never open a profile until its parent-created ownership
// marker has been validated. It deliberately bypasses destructors at the fault.
RuntimePaths ownedPaths(const QCommandLineParser &parser)
{
    const QFileInfo rootInfo(parser.value(QStringLiteral("test-root")));
    const QFileInfo profileInfo(parser.value(QStringLiteral("profile-dir")));
    const QString root = rootInfo.canonicalFilePath();
    const QString profile = profileInfo.canonicalFilePath();
    require(rootInfo.isAbsolute() && rootInfo.isDir() && !rootInfo.isSymLink()
                && rootInfo.fileName().startsWith(QStringLiteral("qcs-t-"))
                && profileInfo.isAbsolute() && profileInfo.isDir() && !profileInfo.isSymLink()
                && !root.isEmpty() && !profile.isEmpty(),
            QStringLiteral("Recovery child requires an owned test directory"));
    const QString relative = QDir(root).relativeFilePath(profile);
    require(!QDir::isAbsolutePath(relative) && relative != QStringLiteral("..")
                && !relative.startsWith(QStringLiteral("../")) && relative != QStringLiteral("."),
            QStringLiteral("Recovery profile is outside the owned test root"));
    const QFileInfo markerInfo(QDir(root).filePath(QStringLiteral("qcs-recovery-owner.json")));
    require(markerInfo.isFile() && !markerInfo.isSymLink(),
            QStringLiteral("Recovery child ownership marker is missing"));
    QFile marker(markerInfo.filePath());
    require(marker.open(QIODevice::ReadOnly) && marker.size() <= 4096,
            QStringLiteral("Recovery child cannot read its ownership marker"));
    const QJsonObject ownership = QJsonDocument::fromJson(marker.readAll()).object();
    const QString token = parser.value(QStringLiteral("fault-token"));
    require(!token.isEmpty() && ownership.value(QStringLiteral("token")).toString() == token
                && ownership.value(QStringLiteral("profile")).toString() == profile,
            QStringLiteral("Recovery child ownership token or profile mismatch"));
    return RuntimePaths::resolve(profile);
}
} // namespace

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-tests-recovery-child"));
    QStandardPaths::setTestModeEnabled(true);
    QCommandLineParser parser;
    parser.addOptions({
        {{QStringLiteral("test-root")}, QStringLiteral("Owned test root"), QStringLiteral("path")},
        {{QStringLiteral("profile-dir")}, QStringLiteral("Owned child profile"), QStringLiteral("path")},
        {{QStringLiteral("fault-token")}, QStringLiteral("Parent ownership token"), QStringLiteral("token")}
    });
    try {
        const bool parsed = parser.parse(application.arguments());
        require(parsed, parser.errorText());
        const auto paths = ownedPaths(parser);
        bool armed = false;
        StorageOptions options;
        options.faultHook = [&armed](const QString &point) {
            if (armed && point == QStringLiteral("ann-manifest-published-before-outbox-cleanup"))
                std::_Exit(73);
        };
        IndexRepository repository(options);
        QString error;
        require(repository.open(paths, &error), error);
        Segment segment;
        segment.kind = QStringLiteral("text");
        segment.modality = QStringLiteral("text");
        segment.text = QStringLiteral("recoverycommittedtoken");
        segment.embedding = QVector<float>(embeddingDimensions, 0.0f);
        segment.embedding[700] = 1.0f;
        const QString source = QDir(paths.profileDir).filePath(QStringLiteral("committed.txt"));
        require(repository.replaceFile(source, QStringLiteral("recovery-committed-v1"),
                                       {segment}, false, &error), error);

        // Retain the one genuine event and add exactly 2048 duplicate events.
        // Only an owned test database may use this synthetic threshold setup.
        {
            sqlite3 *rawDatabase = nullptr;
            const int result = test::openSqliteDatabase(paths.databaseFile(), &rawDatabase,
                                                       SQLITE_OPEN_READWRITE);
            std::unique_ptr<sqlite3, decltype(&sqlite3_close)> database(rawDatabase, sqlite3_close);
            require(result == SQLITE_OK, QStringLiteral("Cannot open the recovery outbox"));
            sqlite3_busy_timeout(database.get(), 5000);
            constexpr auto sql =
                "WITH RECURSIVE copies(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM copies WHERE n<2048) "
                "INSERT INTO ann_events(generation,shard,segment_id,operation) "
                "SELECT s.generation,s.shard,s.id,'upsert' FROM segments s "
                "JOIN files f ON f.id=s.file_id CROSS JOIN copies WHERE f.path=?";
            sqlite3_stmt *rawStatement = nullptr;
            const int prepared = sqlite3_prepare_v2(database.get(), sql, -1, &rawStatement, nullptr);
            std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(rawStatement, sqlite3_finalize);
            require(prepared == SQLITE_OK, QStringLiteral("Cannot prepare the recovery events"));
            const QByteArray encoded = source.toUtf8();
            require(sqlite3_bind_text(statement.get(), 1, encoded.constData(), int(encoded.size()),
                                     SQLITE_TRANSIENT) == SQLITE_OK
                        && sqlite3_step(statement.get()) == SQLITE_DONE
                        && sqlite3_changes(database.get()) == 2048,
                    QStringLiteral("Did not inject exactly 2048 same-shard events"));
        }
        armed = true;
        require(repository.flushOneShard(&error), error);
        throw std::runtime_error("The requested manifest fault point was not reached");
    } catch (const std::exception &exception) {
        // Qt may send diagnostic messages to the debugger on Windows instead
        // of the QProcess stderr pipe. Keep the child protocol explicit.
        std::fprintf(stderr, "%s\n", exception.what());
        std::fflush(stderr);
        return 2;
    }
}
