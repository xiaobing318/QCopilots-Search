#include "core/native_lock_path.h"
#include "support/test_environment.h"
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLockFile>
#include <QProcess>
#include <QStandardPaths>
#include <QTest>

using qcopilots_search::nativeLockFilePath;
using qcopilots_search::RuntimePaths;
using namespace qcopilots_search::test;

namespace {
class WorkerProcess final {
public:
    ~WorkerProcess()
    {
        if (process.state() == QProcess::NotRunning) return;
        send({{QStringLiteral("id"), QStringLiteral("cleanup")}, {QStringLiteral("operation"), QStringLiteral("shutdown")}});
        if (!process.waitForFinished(5000)) {
            process.kill();
            process.waitForFinished(5000);
        }
    }
    bool start(const TestEnvironment &environment, const QString &mode = QStringLiteral("extract"))
    {
        return start(environment, environment.runtimePaths(), mode);
    }
    bool start(const TestEnvironment &environment, const RuntimePaths &paths,
               const QString &mode = QStringLiteral("extract"))
    {
        process.setWorkingDirectory(environment.root());
        process.start(QString::fromUtf8(QCOPILOTS_SEARCH_TEST_WORKER_PATH),
            {QStringLiteral("--mode"), mode, QStringLiteral("--profile-dir"), paths.profileDir,
             QStringLiteral("--model-dir"), paths.modelDir, QStringLiteral("--threads"), QStringLiteral("1")});
        return process.waitForStarted(10000);
    }
    void send(const QJsonObject &object)
    {
        raw(QJsonDocument(object).toJson(QJsonDocument::Compact) + '\n');
    }
    void raw(const QByteArray &data)
    {
        process.write(data);
        process.waitForBytesWritten(10000);
    }
    QJsonObject receive(int timeout = 15000)
    {
        QElapsedTimer timer;
        timer.start();
        while (timer.elapsed() < timeout) {
            buffer += process.readAllStandardOutput();
            const auto newline = buffer.indexOf('\n');
            if (newline >= 0) {
                const auto frame = buffer.left(newline);
                buffer.remove(0, newline + 1);
                return QJsonDocument::fromJson(frame).object();
            }
            if (process.state() == QProcess::NotRunning) break;
            process.waitForReadyRead(50);
        }
        return {};
    }
    QString errors() { return QString::fromUtf8(process.readAllStandardError()); }
    QProcess process;
    QByteArray buffer;
};

QJsonObject extract(const QString &id, const QString &path)
{
    return {{QStringLiteral("id"), id}, {QStringLiteral("operation"), QStringLiteral("extract")},
            {QStringLiteral("path"), path}, {QStringLiteral("embed"), false},
            {QStringLiteral("enableOcr"), false}};
}

QString errorCode(const QJsonObject &reply)
{
    return reply.value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString();
}

RuntimePaths workerLockPaths(const TestEnvironment &environment, bool longPath)
{
    const auto paths = environment.runtimePaths();
    if (!longPath) return paths;
    QString profile = environment.path(QStringLiteral("中文 worker profile with spaces"));
    while (profile.size() < 320)
        profile = QDir(profile).filePath(QStringLiteral("中文 nested worker directory with spaces"));
    return RuntimePaths::resolve(profile, paths.modelDir);
}
}

class WorkerTests final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void commandLineValidation()
    {
        TestEnvironment environment(QStringLiteral("worker cli 中文"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QProcess process;
        process.setWorkingDirectory(environment.root());
        process.start(QString::fromUtf8(QCOPILOTS_SEARCH_TEST_WORKER_PATH), {QStringLiteral("--help")});
        QVERIFY(process.waitForFinished(10000));
        QCOMPARE(process.exitCode(), 0);
        QVERIFY(process.readAllStandardOutput().contains("qcs-worker"));
        process.start(QString::fromUtf8(QCOPILOTS_SEARCH_TEST_WORKER_PATH), {QStringLiteral("--threads"), QStringLiteral("0")});
        QVERIFY(process.waitForFinished(10000));
        QCOMPARE(process.exitCode(), 2);
    }
    void invalidFramesDoNotPoisonNextRequest()
    {
        TestEnvironment environment(QStringLiteral("worker protocol"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        WorkerProcess worker;
        QVERIFY(worker.start(environment));
        worker.raw("this is not json\n");
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("invalid_json"));
        worker.send({{QStringLiteral("id"), QStringLiteral("version")}, {QStringLiteral("version"), 99},
                     {QStringLiteral("operation"), QStringLiteral("extract")}});
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("protocol_mismatch"));
        worker.send({{QStringLiteral("id"), QStringLiteral("unknown")}, {QStringLiteral("operation"), QStringLiteral("unknown")}});
        const auto reply = worker.receive();
        QCOMPARE(reply.value(QStringLiteral("id")).toString(), QStringLiteral("unknown"));
        QCOMPARE(errorCode(reply), QStringLiteral("unknown_operation"));
        worker.send({{QStringLiteral("operation"), QStringLiteral("query")}});
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("invalid_request"));
    }
    void runtimeDiagnosticsDoNotRequireModels_data()
    {
        QTest::addColumn<QString>("backend");
        QTest::newRow("automatic") << QStringLiteral("auto");
        QTest::newRow("explicit-cpu") << QStringLiteral("cpu");
        QTest::newRow("strict-gpu") << QStringLiteral("gpu");
    }
    void runtimeDiagnosticsDoNotRequireModels()
    {
        QFETCH(QString, backend);
        TestEnvironment environment(QStringLiteral("worker runtime diagnostics"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ScopedEnvironment selectedBackend("QCOPILOTS_SEARCH_BACKEND", backend.toUtf8());
        WorkerProcess worker;
        QVERIFY(worker.start(environment, QStringLiteral("query")));
        worker.send({{"id", "state"}, {"operation", "diagnostics"}});
        const auto reply = worker.receive();
        QVERIFY2(reply.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(reply).toJson())));
        const auto runtime = reply.value("result").toObject().value("runtime").toObject();
        QCOMPARE(runtime.value("requestedBackend").toString(), backend);
        QVERIFY(!runtime.value("buildVariant").toString().isEmpty());
        QVERIFY(!runtime.value("packagedBackend").toString().isEmpty());
        QVERIFY(!runtime.value("loaded").toBool());
        QVERIFY(!runtime.value("gpuSelected").toBool());
        for (const auto *key : {"selectedBackend", "deviceName", "deviceDescription", "fallbackReason", "error"})
            QVERIFY2(runtime.contains(QLatin1String(key)), key);
        worker.send({{"id", "load"}, {"operation", "diagnostics"}, {"load", true}, {"textOnly", true}});
        const auto failed = worker.receive();
        QVERIFY(!failed.value("ok").toBool());
        QCOMPARE(errorCode(failed), QStringLiteral("model_unavailable"));
        const auto diagnostic = failed.value("error").toObject().value("runtime").toObject();
        QCOMPARE(diagnostic.value("requestedBackend").toString(), backend);
        QVERIFY(!diagnostic.value("loaded").toBool());
        QVERIFY(!diagnostic.value("error").toString().isEmpty());
        QVERIFY(!failed.value("result").toObject().contains("embedding"));
        worker.send({{"id", "events"}, {"operation", "diagnostics"}, {"load", true},
            {"textOnly", true}, {"runtimeEvents", true}});
        const auto begin = worker.receive();
        QCOMPARE(begin.value("event").toString(), QString("runtime"));
        QCOMPARE(begin.value("id").toString(), QString("events"));
        QCOMPARE(begin.value("data").toObject().value("phase").toString(), QString("inference-start"));
        QCOMPARE(begin.value("data").toObject().value("activity").toString(), QString("model-load"));
        QCOMPARE(begin.value("data").toObject().value("runtime").toObject().value("requestedBackend").toString(), backend);
        const auto end = worker.receive();
        QCOMPARE(end.value("event").toString(), QString("runtime"));
        QCOMPARE(end.value("data").toObject().value("phase").toString(), QString("inference-end"));
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("model_unavailable"));
    }
    void unicodePathsAndKeywordOnlyWithoutModels_data()
    {
        QTest::addColumn<bool>("longPath");
        QTest::newRow("normal") << false;
#ifdef Q_OS_WIN
        QTest::newRow("windows-long-unicode-path") << true;
#endif
    }
    void unicodePathsAndKeywordOnlyWithoutModels()
    {
        QFETCH(bool, longPath);
        TestEnvironment environment(QStringLiteral("worker 可移植 路径"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = workerLockPaths(environment, longPath);
        if (longPath) QVERIFY(paths.profileDir.size() >= 320);
        const QString relative = QStringLiteral("source 文件/会议 记录.txt");
        const QByteArray content = QStringLiteral("中文搜索测试。The aurora expedition leaves on Friday.\n").toUtf8();
        QVERIFY(environment.writeFile(relative, content));
        WorkerProcess worker;
        QVERIFY(worker.start(environment, paths));
        worker.send(extract(QStringLiteral("text"), environment.path(relative)));
        const auto reply = worker.receive();
        QVERIFY2(reply.value(QStringLiteral("ok")).toBool(), qPrintable(worker.errors() + QString::fromUtf8(QJsonDocument(reply).toJson())));
        const auto result = reply.value(QStringLiteral("result")).toObject();
        QCOMPARE(result.value(QStringLiteral("state")).toString(), QStringLiteral("complete"));
        QVERIFY(!result.value(QStringLiteral("embedded")).toBool());
        const auto segments = result.value(QStringLiteral("segments")).toArray();
        QVERIFY(!segments.isEmpty());
        const auto segment = segments.first().toObject();
        QVERIFY(segment.value(QStringLiteral("text")).toString().contains(QStringLiteral("中文搜索测试")));
        QCOMPARE(segment.value(QStringLiteral("path")).toString(), environment.path(relative));
        QVERIFY(segment.value(QStringLiteral("embedding")).toArray().isEmpty());
        QVERIFY(segment.value(QStringLiteral("mediaPath")).toString().isEmpty());
        QVERIFY(!result.value(QStringLiteral("sourceVersion")).toString().isEmpty());
        QFile original(environment.path(relative));
        QVERIFY(original.open(QIODevice::ReadOnly));
        QCOMPARE(original.readAll(), content);
        QDir temporary(paths.tempDir);
        QCOMPARE(temporary.entryList({QStringLiteral("qcs-worker-*")}, QDir::Dirs | QDir::NoDotAndDotDot).size(), 0);
    }
    void inputAndVersionErrors()
    {
        TestEnvironment environment(QStringLiteral("worker versions"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("sample.txt"), "version one"));
        WorkerProcess worker;
        QVERIFY(worker.start(environment));
        auto request = extract(QStringLiteral("relative"), QStringLiteral("sample.txt"));
        worker.send(request);
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("invalid_path"));
        request = extract(QStringLiteral("missing"), environment.path(QStringLiteral("missing.txt")));
        worker.send(request);
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("source_unavailable"));
        request = extract(QStringLiteral("stale"), environment.path(QStringLiteral("sample.txt")));
        request.insert(QStringLiteral("expectedVersion"), QStringLiteral("no-longer-current"));
        worker.send(request);
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("stale_source"));
        request.remove(QStringLiteral("expectedVersion"));
        request.insert(QStringLiteral("maxSegments"), 257);
        worker.send(request);
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("invalid_limits"));
    }
    void persistentCursorAndIsolatedProfiles()
    {
        TestEnvironment environment(QStringLiteral("worker cursor"));
        TestEnvironment separate(QStringLiteral("worker other profile"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY2(separate.isValid(), qPrintable(separate.error()));
        QByteArray text;
        for (int index = 0; index < 150; ++index)
            text += QByteArray::number(index) + ": A detailed record about local semantic search and reliable extraction.\n";
        QVERIFY(environment.writeFile(QStringLiteral("long.txt"), text));
        WorkerProcess worker;
        WorkerProcess other;
        QVERIFY(worker.start(environment));
        QVERIFY(other.start(separate));
        QVERIFY(worker.process.processId() != other.process.processId());
        QVERIFY(environment.runtimePaths().ipcName != separate.runtimePaths().ipcName);
        auto request = extract(QStringLiteral("page1"), environment.path(QStringLiteral("long.txt")));
        request.insert(QStringLiteral("maxSegments"), 1);
        worker.send(request);
        const auto first = worker.receive().value(QStringLiteral("result")).toObject();
        QVERIFY(!first.value(QStringLiteral("complete")).toBool());
        QVERIFY(first.value(QStringLiteral("next")).toInt() > 0);
        request.insert(QStringLiteral("id"), QStringLiteral("page2"));
        request.insert(QStringLiteral("offset"), first.value(QStringLiteral("next")));
        request.insert(QStringLiteral("expectedVersion"), first.value(QStringLiteral("sourceVersion")));
        worker.send(request);
        const auto second = worker.receive().value(QStringLiteral("result")).toObject();
        QCOMPARE(second.value(QStringLiteral("sourceVersion")), first.value(QStringLiteral("sourceVersion")));
        QVERIFY(second.value(QStringLiteral("next")).toInt() > first.value(QStringLiteral("next")).toInt());
        QVERIFY(second.value(QStringLiteral("segments")).toArray() != first.value(QStringLiteral("segments")).toArray());
    }
    void boundedQueueCancellationAndShutdown()
    {
        TestEnvironment environment(QStringLiteral("worker cancellation"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("large.txt"), QByteArray(4 * 1024 * 1024, 'a')));
        WorkerProcess worker;
        QVERIFY(worker.start(environment));
        QByteArray burst = QJsonDocument(extract(QStringLiteral("active"), environment.path(QStringLiteral("large.txt")))).toJson(QJsonDocument::Compact) + '\n';
        burst += QJsonDocument(extract(QStringLiteral("busy"), environment.path(QStringLiteral("large.txt")))).toJson(QJsonDocument::Compact) + '\n';
        burst += QJsonDocument(QJsonObject{{QStringLiteral("id"), QStringLiteral("cancel")},
            {QStringLiteral("operation"), QStringLiteral("cancel")}, {QStringLiteral("targetId"), QStringLiteral("active")}}).toJson(QJsonDocument::Compact) + '\n';
        worker.raw(burst);
        QHash<QString, QJsonObject> replies;
        for (int index = 0; index < 3; ++index) {
            const auto reply = worker.receive();
            replies.insert(reply.value(QStringLiteral("id")).toString(), reply);
        }
        QCOMPARE(errorCode(replies.value(QStringLiteral("busy"))), QStringLiteral("busy"));
        QVERIFY(replies.value(QStringLiteral("cancel")).value(QStringLiteral("result")).toObject().value(QStringLiteral("cancelled")).toBool());
        const auto active = replies.value(QStringLiteral("active"));
        QVERIFY(errorCode(active) == QStringLiteral("cancelled")
            || active.value(QStringLiteral("result")).toObject().value(QStringLiteral("state")).toString() == QStringLiteral("cancelled"));
        worker.send({{QStringLiteral("id"), QStringLiteral("stop")}, {QStringLiteral("operation"), QStringLiteral("shutdown")}});
        QVERIFY(worker.receive().value(QStringLiteral("ok")).toBool());
        QVERIFY(worker.process.waitForFinished(10000));
        QCOMPARE(worker.process.exitCode(), 0);
    }
    void queryModeRejectsEmptyQueryAndMissingModel()
    {
        TestEnvironment environment(QStringLiteral("worker query"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        WorkerProcess worker;
        QVERIFY(worker.start(environment, QStringLiteral("query")));
        worker.send({{QStringLiteral("id"), QStringLiteral("empty")}, {QStringLiteral("operation"), QStringLiteral("query")},
                     {QStringLiteral("query"), QStringLiteral("   ")}});
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("invalid_query"));
        worker.send({{QStringLiteral("id"), QStringLiteral("model")}, {QStringLiteral("operation"), QStringLiteral("query")},
                     {QStringLiteral("query"), QStringLiteral("海边夕阳")}});
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("model_unavailable"));
        worker.send(extract(QStringLiteral("wrong"), environment.path(QStringLiteral("example.txt"))));
        QCOMPARE(errorCode(worker.receive()), QStringLiteral("wrong_mode"));
    }
    void abandonedTemporaryJobsRespectOwnershipAndLocks_data()
    {
        QTest::addColumn<bool>("longPath");
        QTest::newRow("normal") << false;
#ifdef Q_OS_WIN
        QTest::newRow("windows-long-unicode-path") << true;
#endif
    }
    void abandonedTemporaryJobsRespectOwnershipAndLocks()
    {
        QFETCH(bool, longPath);
        TestEnvironment environment(QStringLiteral("worker recovery"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = workerLockPaths(environment, longPath);
        if (longPath) QVERIFY(paths.profileDir.size() >= 320);
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        const QDir root(paths.tempDir);
        const QString abandoned = root.filePath(QStringLiteral("qcs-worker-abandoned"));
        const QString active = root.filePath(QStringLiteral("qcs-worker-active"));
        const QString other = root.filePath(QStringLiteral("qcs-worker-other"));
        const QString unknown = root.filePath(QStringLiteral("qcs-worker-unmarked"));
        for (const auto &directory : {abandoned, active, other, unknown}) {
            QVERIFY(QDir().mkpath(directory));
            QFile content(QDir(directory).filePath(QStringLiteral("content.txt")));
            QVERIFY(content.open(QIODevice::WriteOnly));
            QCOMPARE(content.write("fixture"), 7);
            content.close();
            if (directory == unknown) continue;
            QFile marker(QDir(directory).filePath(QStringLiteral("qcs-worker-owner.json")));
            QVERIFY(marker.open(QIODevice::WriteOnly));
            const auto bytes = QJsonDocument(QJsonObject{{QStringLiteral("version"), 1},
                {QStringLiteral("kind"), QStringLiteral("qcopilots-search-worker-temporary")},
                {QStringLiteral("profile"), directory == other ? environment.path(QStringLiteral("another-profile")) : paths.profileDir}})
                .toJson(QJsonDocument::Compact);
            QCOMPARE(marker.write(bytes), bytes.size());
        }
        QLockFile activeLock(nativeLockFilePath(QDir(active).filePath(QStringLiteral("qcs-worker-lock"))));
        activeLock.setStaleLockTime(0);
        QVERIFY(activeLock.tryLock(0));
        WorkerProcess worker;
        QVERIFY(worker.start(environment, paths));
        worker.send({{QStringLiteral("id"), QStringLiteral("ready")}, {QStringLiteral("operation"), QStringLiteral("cancel")}});
        QVERIFY(worker.receive().value(QStringLiteral("ok")).toBool());
        QVERIFY(!QFileInfo::exists(abandoned));
        QVERIFY(QFileInfo::exists(QDir(active).filePath(QStringLiteral("content.txt"))));
        QVERIFY(QFileInfo::exists(QDir(other).filePath(QStringLiteral("content.txt"))));
        QVERIFY(QFileInfo::exists(QDir(unknown).filePath(QStringLiteral("content.txt"))));
    }
    void inputClosureExitsAndReleasesProfile()
    {
        TestEnvironment environment(QStringLiteral("worker eof"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        WorkerProcess worker;
        QVERIFY(worker.start(environment));
        worker.process.closeWriteChannel();
        QVERIFY(worker.process.waitForFinished(10000));
        QCOMPARE(worker.process.exitCode(), 0);
        QVERIFY2(environment.cleanup(), qPrintable(environment.error()));
    }
    void finiteInputDrainsFinalJob()
    {
        TestEnvironment environment(QStringLiteral("worker finite input"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("finite.txt"), "The final request must finish before standard input closes."));
        WorkerProcess worker;
        QVERIFY(worker.start(environment));
        worker.send(extract(QStringLiteral("finite"), environment.path(QStringLiteral("finite.txt"))));
        worker.process.closeWriteChannel();
        const auto reply = worker.receive();
        QVERIFY(reply.value(QStringLiteral("ok")).toBool());
        QCOMPARE(reply.value(QStringLiteral("result")).toObject().value(QStringLiteral("state")).toString(), QStringLiteral("complete"));
        QVERIFY(worker.process.state() == QProcess::NotRunning || worker.process.waitForFinished(10000));
        QCOMPARE(worker.process.exitCode(), 0);
    }
    void oversizedFrameIsTerminal()
    {
        TestEnvironment environment(QStringLiteral("worker frame limit"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        WorkerProcess worker;
        QVERIFY(worker.start(environment));
        worker.raw(QByteArray(16 * 1024 * 1024 + 1, 'x'));
        QCOMPARE(errorCode(worker.receive(30000)), QStringLiteral("framing_error"));
        QVERIFY(worker.process.waitForFinished(10000));
        QCOMPARE(worker.process.exitCode(), 2);
    }
};

QTEST_GUILESS_MAIN(WorkerTests)
#include "worker_tests.moc"
