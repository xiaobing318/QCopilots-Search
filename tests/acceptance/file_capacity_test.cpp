#include "core/file_catalog.h"
#include "support/test_environment.h"

#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSaveFile>
#include <QStorageInfo>
#include <QtTest>
#include <atomic>

using namespace qcopilots_search;

class FileCapacityTest final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void physicalFilesAndReconciliation();
};

void FileCapacityTest::physicalFilesAndReconciliation()
{
    test::TestEnvironment env(QStringLiteral("physical-files"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    bool countOk = false;
    int count = qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_PHYSICAL_FILE_COUNT", &countOk);
    if (!countOk) count = 1000;
    QVERIFY2(count >= 100 && count <= 1000000, "Physical fixture count must be between 100 and 1000000");
    const qint64 reserve = qEnvironmentVariable("QCOPILOTS_SEARCH_DISK_RESERVE_BYTES").toLongLong();
    QStorageInfo volume(env.root());
    volume.refresh();
    QVERIFY2(volume.isValid() && volume.isReady(), "Cannot determine test volume free space");
    QVERIFY2(volume.bytesAvailable() > reserve + qint64(count) * 8192,
             "Physical-file test cannot fit within the configured free-space reserve");

    const QString root = env.path(QStringLiteral("样本 source files"));
    QVERIFY(QDir().mkpath(root));
    QElapsedTimer clock;
    clock.start();
    qint64 writtenBytes = 0;
    QStringList paths;
    paths.reserve(count);
    for (int i = 0; i < count; ++i) {
        if (i % 1000 == 0) {
            volume.refresh();
            QVERIFY2(volume.bytesAvailable() > reserve + 8 * 1024 * 1024,
                     "Free-space reserve reached while generating physical fixtures");
        }
        const QString directory = QDir(root).filePath(QStringLiteral("分组 %1").arg(i % 100, 3, 10, QLatin1Char('0')));
        QVERIFY(QDir().mkpath(directory));
        const QString path = QDir(directory).filePath(QStringLiteral("文档 %1.txt").arg(i, 8, 10, QLatin1Char('0')));
        QVERIFY(isPathWithin(path, env.root()));
        QFile file(path);
        QVERIFY2(file.open(QIODevice::WriteOnly | QIODevice::NewOnly), qPrintable(file.errorString()));
        const QByteArray content = QStringLiteral("物理文件遍历样本 %1\nPortable search fixture.\n").arg(i).toUtf8();
        QCOMPARE(file.write(content), content.size());
        writtenBytes += content.size();
        paths.append(path);
    }
    const qint64 createMs = clock.restart();
    FolderRules rules{{root}, {}, false};
    const ScanResult initial = FileCatalog::scan(rules);
    QVERIFY2(initial.errors.isEmpty(), qPrintable(initial.errors.join(QLatin1Char('\n'))));
    QVERIFY(!initial.cancelled);
    QCOMPARE(initial.files.size(), count);
    const qint64 scanMs = clock.restart();
    QHash<QString, QString> versions;
    for (const FileRecord &file : initial.files) versions.insert(file.path, file.fingerprint);

    rules.excludes = {QDir(root).filePath(QStringLiteral("分组 000"))};
    const ScanResult excluded = FileCatalog::scan(rules);
    QVERIFY(excluded.errors.isEmpty());
    QCOMPARE(excluded.files.size(), count - ((count + 99) / 100));
    for (const FileRecord &file : excluded.files) QVERIFY(!isPathWithin(file.path, rules.excludes.first()));
    rules.excludes.clear();

    std::atomic_bool cancel{true};
    const ScanResult cancelled = FileCatalog::scan(rules, &cancel);
    QVERIFY(cancelled.cancelled);
    QVERIFY(cancelled.files.isEmpty());

    cancel.store(false);
    const qsizetype cancelAfter = qMin(100, count / 2);
    qsizetype discoveredBeforeCancellation = 0;
    const ScanResult interrupted = FileCatalog::scan(rules, &cancel, [&](qsizetype discovered) {
        discoveredBeforeCancellation = discovered;
        if (discovered == cancelAfter) cancel.store(true);
    });
    QVERIFY(interrupted.cancelled);
    QCOMPARE(discoveredBeforeCancellation, cancelAfter);
    QCOMPARE(interrupted.files.size(), cancelAfter);
    QVERIFY(interrupted.files.size() < count);

    const int changes = qMin(100, count / 4);
    for (int i = 0; i < changes; ++i) {
        QFile file(paths[i]);
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Append));
        QCOMPARE(file.write("changed version\n"), qint64(16));
    }
    for (int i = changes; i < changes * 2; ++i) {
        QVERIFY(isPathWithin(paths[i], env.root()));
        QVERIFY(QFile::remove(paths[i]));
        const QString replacement = QDir(root).filePath(QStringLiteral("新增 replacement %1.txt").arg(i));
        QFile file(replacement);
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::NewOnly));
        QCOMPARE(file.write("new document\n"), qint64(13));
    }
    const ScanResult reconciled = FileCatalog::scan(rules);
    QVERIFY2(reconciled.errors.isEmpty(), qPrintable(reconciled.errors.join(QLatin1Char('\n'))));
    QCOMPARE(reconciled.files.size(), count);
    QHash<QString, QString> current;
    for (const FileRecord &file : reconciled.files) current.insert(file.path, file.fingerprint);
    for (int i = 0; i < changes; ++i) QVERIFY(current.value(normalizePath(paths[i])) != versions.value(normalizePath(paths[i])));
    for (int i = changes; i < changes * 2; ++i) QVERIFY(!current.contains(normalizePath(paths[i])));
    const qint64 reconcileMs = clock.elapsed();

    const QString actualRoot = env.root();
    QString cleanupError;
    QVERIFY2(env.cleanup(&cleanupError), qPrintable(cleanupError));
    const QJsonObject report{{"schema", 1}, {"scenario", "physical-file-capacity"},
        {"createdPhysicalFiles", count}, {"remainingPhysicalFiles", count},
        {"payloadBytes", writtenBytes}, {"createMs", createMs}, {"scanMs", scanMs},
        {"exclusionCancellationAndReconcileMs", reconcileMs}, {"modified", changes},
        {"deleted", changes}, {"added", changes}, {"staleResults", 0},
        {"cancelledAfterDiscoveredFiles", qint64(discoveredBeforeCancellation)},
        {"cleanupVerified", true}, {"generatedFixture", true}, {"passed", true},
        {"timestampUtc", QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
        {"actualRoot", actualRoot}};
    const QString reportPath = qEnvironmentVariable("QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT");
    if (!reportPath.isEmpty()) {
        QVERIFY2(QDir::isAbsolutePath(reportPath), "Report path must be explicit and absolute");
        QVERIFY(QDir().mkpath(QFileInfo(reportPath).absolutePath()));
        QSaveFile output(reportPath);
        QVERIFY(output.open(QIODevice::WriteOnly));
        const QByteArray json = QJsonDocument(report).toJson();
        QCOMPARE(output.write(json), json.size());
        QVERIFY(output.commit());
    }
    qInfo().noquote() << QJsonDocument(report).toJson(QJsonDocument::Compact);
}

QTEST_GUILESS_MAIN(FileCapacityTest)
#include "file_capacity_test.moc"
