#include "service/ipc_protocol.h"
#include "service/ipc_security.h"
#include "service/service_client.h"
#include "service/service_server.h"
#include "core/runtime_paths.h"
#include "core/types.h"
#include "core/app_settings.h"
#include "core/file_catalog.h"
#include "storage/index_repository.h"
#include "support/test_environment.h"
#include <QDir>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFile>
#include <QJsonDocument>
#include <QLocalSocket>
#include <QProcess>
#include <QSignalSpy>
#include <QScopeGuard>
#include <QStorageInfo>
#include <QTest>
#include <QTimer>
#include <QtEndian>
#ifndef Q_OS_WIN
#include <sys/un.h>
#endif
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <stdexcept>

using namespace qcopilots_search;
using namespace qcopilots_search::test;

namespace {
QJsonObject localCall(ServiceClient &client, const QString &method, const QJsonObject &params = {}, int timeout = 9000) {
    QJsonObject reply;
    QEventLoop loop;
    QString requestId;
    const auto response = QObject::connect(&client, &ServiceClient::response, &loop, [&](const QString &id, const QJsonObject &result) {
        if (id != requestId) return;
        reply = {{"ok", true}, {"result", result}}; loop.quit();
    });
    const auto failure = QObject::connect(&client, &ServiceClient::failed, &loop, [&](const QString &id, const QString &code, const QString &message) {
        if (id != requestId) return;
        reply = {{"ok", false}, {"error", QJsonObject{{"code", code}, {"message", message}}}}; loop.quit();
    });
    QTimer::singleShot(timeout + 100, &loop, &QEventLoop::quit);
    requestId = client.request(method, params, timeout);
    loop.exec();
    QObject::disconnect(response); QObject::disconnect(failure);
    return reply;
}

QString serviceDiagnostics(const RuntimePaths &paths, const QJsonObject &status) {
    QFile log(QDir(paths.logDir).filePath(QStringLiteral("qcs-service.log")));
    QString contents;
    if (log.open(QIODevice::ReadOnly)) {
        log.seek(qMax<qint64>(0, log.size() - 16384));
        contents = QString::fromUtf8(log.readAll());
    } else contents = log.errorString();
    return QStringLiteral("Profile: %1\nStatus: %2\nService log: %3")
        .arg(paths.profileDir, QString::fromUtf8(QJsonDocument(status).toJson(QJsonDocument::Compact)), contents);
}

class ServiceProcess {
public:
    explicit ServiceProcess(RuntimePaths value) : paths(std::move(value)) {}
    ~ServiceProcess() {
        if (process.state() == QProcess::NotRunning) return;
        call(QStringLiteral("shutdown"));
        if (!process.waitForFinished(5000)) { process.kill(); process.waitForFinished(5000); }
    }
    bool start() {
        process.setProgram(qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_SERVICE"));
        process.setArguments({"--profile-dir", paths.profileDir, "--model-dir", paths.modelDir});
        process.start();
        if (!process.waitForStarted(5000)) { error = process.errorString(); return false; }
        QElapsedTimer elapsed; elapsed.start();
        while (elapsed.elapsed() < 15000) {
            if (process.state() == QProcess::NotRunning) {
                error = QStringLiteral("Service exited with code %1: %2").arg(process.exitCode()).arg(QString::fromUtf8(process.readAllStandardError()));
                return false;
            }
            QFile tokenFile(QDir(paths.profileDir).filePath("qcs-ipc-token"));
            if (tokenFile.open(QIODevice::ReadOnly)) { token = tokenFile.readAll(); tokenFile.close(); }
            if (!token.isEmpty() && call("status").value("ok").toBool()) return true;
            QTest::qWait(20);
        }
        error = QStringLiteral("Service startup timed out: %1").arg(QString::fromUtf8(process.readAllStandardError()));
        return false;
    }
    QJsonObject call(const QString &method, const QJsonObject &params = {}, int timeout = 5000) {
        QLocalSocket socket;
        socket.connectToServer(paths.ipcName);
        if (!socket.waitForConnected(500)) return {};
        const QString id = QString::number(++counter);
        socket.write(encodeIpcFrame({{"protocol", protocolVersion}, {"id", id}, {"token", QString::fromLatin1(token)},
            {"method", method}, {"params", params}}));
        socket.flush();
        QByteArray buffer;
        QElapsedTimer elapsed; elapsed.start();
        while (elapsed.elapsed() < timeout) {
            if (!socket.bytesAvailable()) socket.waitForReadyRead(qMin(100, timeout - int(elapsed.elapsed())));
            buffer += socket.readAll();
            QList<QJsonObject> frames;
            if (!takeIpcFrames(buffer, frames)) return {};
            for (const auto &frame : frames) if (frame.value("id").toString() == id) return frame;
            if (socket.state() == QLocalSocket::UnconnectedState) return {};
        }
        return {};
    }
    QList<QJsonObject> search(const QString &query, int timeout = 5000) {
        QList<QJsonObject> result;
        const auto response = call("search", {{"query", query}, {"semantic", false}}, timeout);
        for (const auto &hit : response.value("result").toObject().value("hits").toArray()) result.append(hit.toObject());
        return result;
    }
    bool waitForHit(const QString &query, const QString &path, int timeout = 30000) {
        QElapsedTimer elapsed; elapsed.start();
        while (elapsed.elapsed() < timeout) {
            for (const auto &hit : search(query, qMax(1, timeout - int(elapsed.elapsed()))))
                if (QDir::cleanPath(hit.value("path").toString()) == QDir::cleanPath(path)) return true;
            QTest::qWait(30);
        }
        return false;
    }
    RuntimePaths paths;
    QProcess process;
    QByteArray token;
    QString error;
    int counter = 0;
};
}

class ServiceTests final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

#ifndef Q_OS_WIN
    void unixLongTemporaryDirectoryUsesStableEndpoint() {
        TestEnvironment environment("service-long-tmpdir");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString longTemporary = environment.path(QStringLiteral("temporary/") + QString(120, u't'));
        QVERIFY(QDir().mkpath(longTemporary));
        ScopedEnvironment temporary("TMPDIR", longTemporary.toUtf8());
        const auto paths = environment.runtimePaths();
        QVERIFY(QDir::isAbsolutePath(paths.ipcName));
        sockaddr_un address{};
        QVERIFY(QFile::encodeName(paths.ipcName).size() + 1 <= qsizetype(sizeof(address.sun_path)));
        ServiceProcess service(paths);
        QVERIFY2(service.start(), qPrintable(service.error));
        // The already-running child retains the long TMPDIR. A client resolving
        // its path under another TMPDIR must still find that exact service.
        ScopedEnvironment changedTemporary("TMPDIR", environment.root().toUtf8());
        QCOMPARE(RuntimePaths::resolve(paths.profileDir, paths.modelDir).ipcName, paths.ipcName);
        QVERIFY(service.call("status").value("ok").toBool());
    }

    void unixUnexpectedSocketLeafIsNotRemoved_data() {
        QTest::addColumn<bool>("symbolicLink");
        QTest::newRow("regular-file") << false;
        QTest::newRow("symbolic-link") << true;
    }
    void unixUnexpectedSocketLeafIsNotRemoved() {
        QFETCH(bool, symbolicLink);
        TestEnvironment environment("service-occupied-endpoint");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        QVERIFY2(paths.ensureIpcDirectory(&error), qPrintable(error));
        const QByteArray marker("Never delete this test-owned non-socket file.");
        const QString target = environment.path(QStringLiteral("marker"));
        QVERIFY(environment.writeFile("marker", marker));
        if (symbolicLink) {
            QVERIFY(QFile::link(target, paths.ipcName));
        } else {
            QFile leaf(paths.ipcName);
            QVERIFY(leaf.open(QIODevice::WriteOnly | QIODevice::NewOnly));
            QCOMPARE(leaf.write(marker), marker.size());
        }
        const auto cleanup = qScopeGuard([&] { QFile::remove(paths.ipcName); });
        ServiceProcess service(paths);
        QVERIFY(!service.start());
        QVERIFY2(service.error.contains(QStringLiteral("Refusing to remove")), qPrintable(service.error));
        QCOMPARE(QFileInfo(paths.ipcName).isSymLink(), symbolicLink);
        QFile leaf(paths.ipcName);
        QVERIFY(leaf.open(QIODevice::ReadOnly));
        QCOMPARE(leaf.readAll(), marker);
        leaf.close();
        QFile original(target);
        QVERIFY(original.open(QIODevice::ReadOnly));
        QCOMPARE(original.readAll(), marker);
    }
#endif
    void corruptDocumentRemainsSearchableByFilename() {
        TestEnvironment environment("service-corrupt-filename");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString relative = QStringLiteral("selected/corruptfilenameprobe.docx");
        QVERIFY(environment.writeFile(relative, "This is not a ZIP document. neverindexthisbodytoken"));
        FileRecord file;
        QVERIFY(FileCatalog::describe(environment.path(relative), &file));
        const auto paths = environment.runtimePaths();
        {
            ServiceProcess service(paths);
            QVERIFY2(service.start(), qPrintable(service.error));
            QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.path("selected")}}}).value("ok").toBool());
            auto status = [&] { return service.call("status").value("result").toObject(); };
            QTRY_VERIFY_WITH_TIMEOUT(status().value("failures").toObject().contains(file.path), 30000);
            QVERIFY(service.waitForHit("corruptfilenameprobe", file.path, 5000));
            QCOMPARE(service.search("neverindexthisbodytoken").size(), 0);
            QTRY_COMPARE_WITH_TIMEOUT(status().value("progress").toObject().value("state").toString(), QStringLiteral("partial"), 5000);
            QVERIFY(!status().value("failures").toObject().value(file.path).toString().isEmpty());
            QVERIFY(service.call("index.pause").value("ok").toBool());
        }
        {
            IndexRepository repository;
            QString error;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            const auto rows = repository.fileSegments(file.path, false, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QCOMPARE(rows.size(), 1);
            QCOMPARE(rows.first().title, QStringLiteral("corruptfilenameprobe.docx"));
            QCOMPARE(rows.first().kind, file.kind);
            QVERIFY(rows.first().text.isEmpty());
            QVERIFY(rows.first().embedding.isEmpty());
            QVERIFY(rows.first().mediaPath.isEmpty());
            QVERIFY(repository.fileVersion(file.path).contains("|failed:"));
        }
        ServiceProcess restarted(paths);
        QVERIFY2(restarted.start(), qPrintable(restarted.error));
        QVERIFY(restarted.waitForHit("corruptfilenameprobe", file.path, 5000));
        QVERIFY(restarted.call("status").value("result").toObject().value("failures").toObject().contains(file.path));
    }

    void failedRetryPreservesPreviouslyPublishedPrefix() {
        TestEnvironment environment("service-failed-prefix");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString relative = QStringLiteral("selected/partiallyreadableprobe.docx");
        QVERIFY(environment.writeFile(relative, "Not a ZIP container. failedattemptbodytoken"));
        FileRecord file;
        QVERIFY(FileCatalog::describe(environment.path(relative), &file));
        const auto paths = environment.runtimePaths();
        QString pipeline;
        {
            ServiceProcess service(paths);
            QVERIFY2(service.start(), qPrintable(service.error));
            pipeline = service.call("status").value("result").toObject().value("pipelineFingerprint").toString();
            QVERIFY(!pipeline.isEmpty());
            QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.path("selected")}}, {"indexingPaused", true}}).value("ok").toBool());
        }
        {
            // Simulate the authoritative commit from an earlier partial extraction
            // of this unchanged source before its remaining data failed to decode.
            IndexRepository repository;
            QString error;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            Segment prefix;
            prefix.path = file.path;
            prefix.title = QStringLiteral("partiallyreadableprobe.docx");
            prefix.kind = file.kind;
            prefix.modality = QStringLiteral("text");
            prefix.text = QStringLiteral("preservedpartialprefix");
            QVERIFY2(repository.replaceFile(file.path, file.fingerprint + '|' + pipeline + "|failed:1", {prefix}, false, &error), qPrintable(error));
        }
        {
            ServiceProcess service(paths);
            QVERIFY2(service.start(), qPrintable(service.error));
            QVERIFY(service.waitForHit("preservedpartialprefix", file.path, 5000));
            QVERIFY(service.call("index.resume").value("ok").toBool());
            auto status = [&] { return service.call("status").value("result").toObject(); };
            QTRY_VERIFY_WITH_TIMEOUT(status().value("failures").toObject().contains(file.path), 30000);
            QVERIFY(service.waitForHit("preservedpartialprefix", file.path, 5000));
            QVERIFY(service.waitForHit("partiallyreadableprobe", file.path, 5000));
            QCOMPARE(service.search("failedattemptbodytoken").size(), 0);
            QVERIFY(service.call("index.pause").value("ok").toBool());
        }
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(paths, &error), qPrintable(error));
        const auto rows = repository.fileSegments(file.path, false, &error);
        QVERIFY2(error.isEmpty(), qPrintable(error));
        QCOMPARE(rows.size(), 1);
        QCOMPARE(rows.first().text, QStringLiteral("preservedpartialprefix"));
        QVERIFY(rows.first().embedding.isEmpty());
        QVERIFY(repository.fileVersion(file.path).contains("|failed:"));
    }

    void pipelineFailureRollsBackAndDisablesInference() {
        TestEnvironment environment("service-pipeline-fault");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/document.txt", "pipelinefailurekeyword"));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("selected")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        FileRecord file;
        QVERIFY(FileCatalog::describe(environment.path("selected/document.txt"), &file));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY(repository.setPipelineFingerprint("initial-old-space", &error));
            Segment segment; segment.path = file.path; segment.kind = "text"; segment.title = "document";
            segment.text = "pipelinefailurekeyword"; segment.embedding = QVector<float>(768, 0); segment.embedding[0] = 1;
            QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|initial-old-space", {segment}, false, &error), qPrintable(error));
        }
        bool available = false, failCommit = true;
        ServiceDependencies dependencies;
        dependencies.verifyModels = [&](QString *) { return available; };
        dependencies.storage.faultHook = [&](const QString &phase) {
            if (failCommit && phase == "before-pipeline-commit") throw std::runtime_error("injected pipeline disk failure");
        };
        {
            ServiceServer rejected(paths, dependencies);
            QVERIFY(!rejected.start(&error));
            QVERIFY(error.contains("injected pipeline disk failure"));
        }
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QCOMPARE(repository.pipelineFingerprint(), QStringLiteral("initial-old-space"));
            QCOMPARE(repository.fileSegments(file.path).first().embedding.size(), 768);
        }
        failCommit = false;
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        const auto initial = localCall(client, "status").value("result").toObject();
        const auto oldPipeline = initial.value("pipelineFingerprint").toString();
        QVERIFY(!oldPipeline.isEmpty());
        available = true; failCommit = true;
        const auto failure = localCall(client, "models.reload");
        QCOMPARE(failure.value("error").toObject().value("code").toString(), QStringLiteral("pipeline_transition_failed"));
        const auto status = localCall(client, "status").value("result").toObject();
        QVERIFY(!status.value("modelAvailable").toBool());
        QCOMPARE(status.value("pipelineFingerprint").toString(), oldPipeline);
        QVERIFY(status.value("pipelineTransitionError").toString().contains("injected pipeline disk failure"));
        const auto search = localCall(client, "search", {{"query", "pipelinefailurekeyword"}, {"semantic", true}}).value("result").toObject();
        QVERIFY(!search.value("semantic").toBool());
        QCOMPARE(search.value("hits").toArray().size(), 1);
        QVERIFY(search.value("warning").toString().contains("pipeline"));
        QCOMPARE(localCall(client, "index.start").value("result").toObject().value("state").toString(), QStringLiteral("error"));
        failCommit = false;
        QVERIFY(localCall(client, "models.reload").value("ok").toBool());
        const auto recovered = localCall(client, "status").value("result").toObject();
        QVERIFY(recovered.value("modelAvailable").toBool());
        QVERIFY(recovered.value("pipelineTransitionError").toString().isEmpty());
        QVERIFY(recovered.value("pipelineFingerprint").toString() != oldPipeline);
        client.disconnectFromService(); service.stop();
    }

    void currentFolderRulesParticipateBeforeLimit() {
        TestEnvironment environment("service-folder-filter");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("first/needle.txt", "needle"));
        QVERIFY(environment.writeFile("second/document.txt", "needle"));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("first"), environment.path("second")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY(repository.setPipelineFingerprint("folder-filter-fixture", &error));
            for (const QString &relative : {QStringLiteral("first/needle.txt"), QStringLiteral("second/document.txt")}) {
                FileRecord file; QVERIFY(FileCatalog::describe(environment.path(relative), &file));
                Segment segment; segment.path = file.path; segment.title = QFileInfo(file.path).fileName();
                segment.kind = "text"; segment.text = "needle";
                QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|folder-filter-fixture", {segment}, false, &error), qPrintable(error));
            }
        }
        ServiceProcess service(paths); QVERIFY2(service.start(), qPrintable(service.error));
        auto limited = [&] { return service.call("search", {{"query", "needle"}, {"semantic", false}, {"limit", 1}}).value("result").toObject().value("hits").toArray(); };
        QCOMPARE(limited().size(), 1);
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("first/needle.txt"));
        QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.path("second")}}}).value("ok").toBool());
        QCOMPARE(limited().size(), 1);
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("second/document.txt"));
        QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.path("first"), environment.path("second")}},
            {"excludes", QJsonArray{environment.path("first")}}}).value("ok").toBool());
        // An explicitly selected root overrides an equal exclusion, as in FileCatalog.
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("first/needle.txt"));
        QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.root()}},
            {"excludes", QJsonArray{environment.path("first")}}}).value("ok").toBool());
        QCOMPARE(limited().size(), 1);
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("second/document.txt"));
        QCOMPARE(service.call("status").value("result").toObject().value("repository").toObject().value("files").toInt(), 2);
    }

    void singletonAndExplicitProfile_data() {
        QTest::addColumn<bool>("longProfile");
        QTest::newRow("ordinary-profile") << false;
#ifdef Q_OS_WIN
        QTest::newRow("long-unicode-profile") << true;
#endif
    }

    void singletonAndExplicitProfile() {
        QFETCH(bool, longProfile);
        TestEnvironment environment("service-singleton");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QString profile = environment.path("profile");
        if (longProfile) while (profile.size() < 350)
            profile += QStringLiteral("/中文 空格 configuration");
        const auto paths = RuntimePaths::resolve(profile, environment.path("models"));
        ServiceProcess service(paths);
        QVERIFY2(service.start(), qPrintable(service.error));
        QVERIFY(QFileInfo::exists(paths.lockFile));
        QCOMPARE(RuntimePaths::resolve(paths.profileDir, paths.modelDir).ipcName, paths.ipcName);
        QCOMPARE(paths.lockFile, QDir(paths.profileDir).filePath(QStringLiteral("qcs-service.lock")));
        const auto status = service.call("status");
        QVERIFY(status.value("ok").toBool());
        QVERIFY(!status.value("result").toObject().value("modelAvailable").toBool());
        QProcess duplicate;
        duplicate.start(service.process.program(), service.process.arguments());
        QVERIFY(duplicate.waitForStarted(5000));
        QVERIFY(duplicate.waitForFinished(10000));
        QCOMPARE(duplicate.exitCode(), 0);
        QVERIFY(duplicate.readAllStandardError().contains("already_running:"));
        QCOMPARE(service.process.state(), QProcess::Running);
        QVERIFY(service.call("status").value("ok").toBool());
        QVERIFY(service.call("shutdown").value("ok").toBool());
        QVERIFY(service.process.state() == QProcess::NotRunning || service.process.waitForFinished(5000));
        QCOMPARE(service.process.exitStatus(), QProcess::NormalExit);
        QCOMPARE(service.process.exitCode(), 0);
        QVERIFY2(!QFileInfo::exists(paths.lockFile), qPrintable(paths.lockFile));
        QVERIFY2(!service.process.readAllStandardError().contains("Could not remove our own lock file"),
                 qPrintable(serviceDiagnostics(paths, status)));
        ServiceProcess restarted(paths);
        QVERIFY2(restarted.start(), qPrintable(restarted.error));
        QVERIFY(QFileInfo::exists(paths.lockFile));
        QVERIFY(restarted.call("status").value("ok").toBool());
        QVERIFY(restarted.call("shutdown").value("ok").toBool());
        QVERIFY(restarted.process.state() == QProcess::NotRunning || restarted.process.waitForFinished(5000));
        QCOMPARE(restarted.process.exitStatus(), QProcess::NormalExit);
        QCOMPARE(restarted.process.exitCode(), 0);
        QVERIFY2(!QFileInfo::exists(paths.lockFile), qPrintable(paths.lockFile));
    }

    void cappedPrefixSurvivesReconcileAndExplicitRetry() {
        TestEnvironment environment("service-capped-prefix");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/long.txt", "Source for a persisted capped index fixture"));
        const auto paths = environment.runtimePaths();
        QString error, pipeline;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        {
            ServiceProcess initial(paths); QVERIFY2(initial.start(), qPrintable(initial.error));
            pipeline = initial.call("status").value("result").toObject().value("pipelineFingerprint").toString();
        }
        FileRecord file; QVERIFY(FileCatalog::describe(environment.path("selected/long.txt"), &file));
        QVector<Segment> prefix;
        prefix.reserve(5000);
        for (int i = 0; i < 5000; ++i) {
            Segment segment; segment.path = file.path; segment.kind = "text"; segment.title = "long";
            segment.text = i == 4999 ? QStringLiteral("cappedfinalprefixneedle") : QStringLiteral("capped prefix %1").arg(i);
            prefix.append(std::move(segment));
        }
        {
            IndexRepository repository; QVERIFY2(repository.open(paths, &error), qPrintable(error));
            // This legacy marker reproduces the prior restart bug. New batches
            // persist capped:5000, and both must retain the authoritative prefix.
            QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|" + pipeline + "|partial:5000", prefix, false, &error), qPrintable(error));
        }
        settings.indexingPaused = false; QVERIFY(settings.save(paths.configFile, &error));
        ServiceProcess service(paths); QVERIFY2(service.start(), qPrintable(service.error));
        auto status = [&] { return service.call("status").value("result").toObject(); };
        QTRY_COMPARE_WITH_TIMEOUT(status().value("progress").toObject().value("state").toString(), QStringLiteral("partial"), 10000);
        QVERIFY(status().value("failures").toObject().value(file.path).toString().contains("5000"));
        for (int repetition = 0; repetition < 2; ++repetition) {
            QVERIFY(service.call("index.reconcile").value("ok").toBool());
            QTRY_COMPARE_WITH_TIMEOUT(status().value("progress").toObject().value("state").toString(), QStringLiteral("partial"), 10000);
            QCOMPARE(status().value("repository").toObject().value("segments").toInteger(), 5000);
            QVERIFY(service.waitForHit("cappedfinalprefixneedle", file.path, 5000));
        }
        // Process both requests in one readiness event. The scan can queue a
        // deliberate retry, while pause prevents any worker batch publishing.
        QLocalSocket socket; socket.connectToServer(paths.ipcName); QVERIFY(socket.waitForConnected(3000));
        const QString token = QString::fromLatin1(service.token);
        socket.write(encodeIpcFrame({{"protocol", 1}, {"id", "retry-cap"}, {"token", token}, {"method", "index.retry"}})
            + encodeIpcFrame({{"protocol", 1}, {"id", "pause-cap"}, {"token", token}, {"method", "index.pause"}}));
        socket.flush();
        QTRY_COMPARE_WITH_TIMEOUT(status().value("queuedFiles").toInt(), 1, 10000);
        QCOMPARE(status().value("repository").toObject().value("segments").toInteger(), 5000);
        QVERIFY(service.waitForHit("cappedfinalprefixneedle", file.path, 5000));
        QVERIFY(environment.writeFile("selected/long.txt", "changedsourceaftercapneedle"));
        QVERIFY(service.call("index.resume").value("ok").toBool());
        const bool changedSourceFound = service.waitForHit("changedsourceaftercapneedle", file.path, 5000);
        QString diagnostics;
        if (!changedSourceFound) diagnostics = serviceDiagnostics(paths, service.call("status"))
            + QStringLiteral("\nService stderr: ") + QString::fromUtf8(service.process.readAllStandardError());
        QVERIFY2(changedSourceFound, qPrintable(diagnostics));
        QTRY_COMPARE_WITH_TIMEOUT(status().value("progress").toObject().value("state").toString(), QStringLiteral("idle"), 10000);
        QCOMPARE(status().value("repository").toObject().value("segments").toInteger(), 1);
        QVERIFY(status().value("failures").toObject().isEmpty());
    }

    void sourceChangedDuringPublicationIsRescanned() {
        TestEnvironment environment("service-publication-source-race");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/document.txt", "publicationoldsourcekeyword"));
        const auto paths = environment.runtimePaths();
        const QString source = environment.path("selected/document.txt");
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("selected")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));

        std::atomic_bool armed{false}, changed{false};
        ServiceDependencies dependencies;
        dependencies.storage.availableBytes = [&](const QString &directory) -> qint64 {
            // This existing disk-space provider runs after PublicationGate has
            // accepted the old source and before its SQLite transaction. The
            // completion callback must detect the new source and schedule it.
            if (armed.exchange(false)) {
                QFile file(source);
                const QByteArray replacement("publicationnewsourcekeyword with a changed file size");
                if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)
                    || file.write(replacement) != replacement.size() || !file.flush())
                    throw std::runtime_error("Could not inject source change during publication");
                file.close();
                changed.store(true);
            }
            const QStorageInfo volume(directory);
            return volume.isValid() && volume.isReady() ? volume.bytesAvailable() : -1;
        };
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        armed.store(true);
        QVERIFY(localCall(client, "index.resume").value("ok").toBool());

        bool found = false;
        QJsonObject lastSearch;
        QElapsedTimer elapsed; elapsed.start();
        while (!found && elapsed.elapsed() < 5000) {
            lastSearch = localCall(client, "search", {{"query", "publicationnewsourcekeyword"}, {"semantic", false}},
                qMax(1, qMin(500, 5000 - int(elapsed.elapsed()))));
            for (const auto &hit : lastSearch.value("result").toObject().value("hits").toArray())
                if (QDir::cleanPath(hit.toObject().value("path").toString()) == QDir::cleanPath(source)) found = true;
            if (!found) QTest::qWait(20);
        }
        QString diagnostics;
        if (!found || !changed.load()) diagnostics = serviceDiagnostics(paths, localCall(client, "status"))
            + QStringLiteral("\nSource mutation injected: %1\nLast search: %2").arg(changed.load())
                .arg(QString::fromUtf8(QJsonDocument(lastSearch).toJson(QJsonDocument::Compact)));
        QVERIFY2(changed.load(), qPrintable(diagnostics));
        QVERIFY2(found, qPrintable(diagnostics));
        const auto oldSearch = localCall(client, "search", {{"query", "publicationoldsourcekeyword"}, {"semantic", false}});
        QVERIFY(oldSearch.value("ok").toBool());
        QVERIFY(oldSearch.value("result").toObject().value("hits").toArray().isEmpty());
        QTRY_COMPARE_WITH_TIMEOUT(localCall(client, "status").value("result").toObject()
            .value("progress").toObject().value("state").toString(), QStringLiteral("idle"), 5000);
        QVERIFY(localCall(client, "status").value("result").toObject().value("failures").toObject().isEmpty());
        client.disconnectFromService(); service.stop();
    }

    void changingSourceDoesNotStarveStableQueuedFile() {
        TestEnvironment environment("service-stale-source-fairness");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/a.txt", "fairnessoldsourcekeyword"));
        const auto paths = environment.runtimePaths();
        const QString changingSource = environment.path("selected/a.txt");
        const QString stableSource = environment.path("selected/b.txt");
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("selected")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));

        std::mutex mutationMutex;
        bool keepChanging = false;
        std::atomic_int mutations{0};
        ServiceDependencies dependencies;
        dependencies.storage.availableBytes = [&](const QString &directory) -> qint64 {
            const std::lock_guard guard(mutationMutex);
            if (keepChanging) {
                const int mutation = mutations.fetch_add(1) + 1;
                QFile changingFile(changingSource);
                const QByteArray replacement = QByteArray("fairnessintermediatesourcekeyword ") + QByteArray(mutation, 'x');
                if (!changingFile.open(QIODevice::WriteOnly | QIODevice::Truncate)
                    || changingFile.write(replacement) != replacement.size() || !changingFile.flush())
                    throw std::runtime_error("Could not inject continuously changing source");
                changingFile.close();
                // Until this point only a exists, so both initial mutations
                // interrupt publication of a. Subsequent scans queue a then b.
                if (mutation == 2) {
                    QFile stableFile(stableSource);
                    const QByteArray stableText("fairnessstablefilekeyword");
                    if (!stableFile.open(QIODevice::WriteOnly | QIODevice::NewOnly)
                        || stableFile.write(stableText) != stableText.size() || !stableFile.flush())
                        throw std::runtime_error("Could not create stable file for fairness regression");
                }
            }
            const QStorageInfo volume(directory);
            return volume.isValid() && volume.isReady() ? volume.bytesAvailable() : -1;
        };
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        {
            const std::lock_guard guard(mutationMutex);
            keepChanging = true;
        }
        QVERIFY(localCall(client, "index.resume").value("ok").toBool());
        QJsonObject lastSearch;
        auto waitForToken = [&](const QString &query, const QString &path) {
            QElapsedTimer elapsed; elapsed.start();
            while (elapsed.elapsed() < 5000) {
                lastSearch = localCall(client, "search", {{"query", query}, {"semantic", false}},
                    qMax(1, qMin(500, 5000 - int(elapsed.elapsed()))));
                for (const auto &hit : lastSearch.value("result").toObject().value("hits").toArray())
                    if (QDir::cleanPath(hit.toObject().value("path").toString()) == QDir::cleanPath(path))
                        return elapsed.elapsed() <= 5000;
                QTest::qWait(20);
            }
            return false;
        };
        const bool stableFound = waitForToken("fairnessstablefilekeyword", stableSource);
        int mutationsBeforeStable;
        {
            // Stop only after observing b or exhausting the deadline. A fixed
            // number of changes would let the queue-clearing bug pass eventually.
            const std::lock_guard guard(mutationMutex);
            keepChanging = false;
            mutationsBeforeStable = mutations.load();
        }
        QString diagnostics;
        if (!stableFound || mutationsBeforeStable < 3)
            diagnostics = serviceDiagnostics(paths, localCall(client, "status"))
                + QStringLiteral("\nSource mutations before stable result: %1\nLast search: %2")
                    .arg(mutationsBeforeStable)
                    .arg(QString::fromUtf8(QJsonDocument(lastSearch).toJson(QJsonDocument::Compact)));
        QVERIFY2(mutationsBeforeStable >= 3, qPrintable(diagnostics));
        QVERIFY2(stableFound, qPrintable(diagnostics));

        // Taking the same mutex above has drained any in-flight mutation. The
        // writer can now publish the final version without a concurrent rewrite.
        QVERIFY(environment.writeFile("selected/a.txt", "fairnessfinalsourcekeyword"));
        const bool changingFound = waitForToken("fairnessfinalsourcekeyword", changingSource);
        if (!changingFound) diagnostics = serviceDiagnostics(paths, localCall(client, "status"));
        QVERIFY2(changingFound, qPrintable(diagnostics));
        for (const QString &oldToken : {QStringLiteral("fairnessoldsourcekeyword"), QStringLiteral("fairnessintermediatesourcekeyword")}) {
            const auto result = localCall(client, "search", {{"query", oldToken}, {"semantic", false}});
            QVERIFY(result.value("ok").toBool());
            QVERIFY(result.value("result").toObject().value("hits").toArray().isEmpty());
        }
        QTRY_COMPARE_WITH_TIMEOUT(localCall(client, "status").value("result").toObject()
            .value("progress").toObject().value("state").toString(), QStringLiteral("idle"), 5000);
        QVERIFY(localCall(client, "status").value("result").toObject().value("failures").toObject().isEmpty());
        client.disconnectFromService(); service.stop();
    }

    void authenticationAndProtocolLimits() {
        TestEnvironment environment("service-protocol");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ServiceProcess service(environment.runtimePaths());
        QVERIFY2(service.start(), qPrintable(service.error));
        QLocalSocket unauthorized;
        unauthorized.connectToServer(service.paths.ipcName);
        QVERIFY(unauthorized.waitForConnected(3000));
        unauthorized.write(encodeIpcFrame({{"protocol", 1}, {"id", "bad"}, {"method", "status"}, {"token", "wrong"}}));
        QVERIFY(unauthorized.waitForReadyRead(3000));
        QByteArray buffer = unauthorized.readAll();
        QList<QJsonObject> frames;
        QVERIFY(takeIpcFrames(buffer, frames));
        QVERIFY(!frames.isEmpty());
        QCOMPARE(frames.first().value("error").toObject().value("code").toString(), QStringLiteral("not_authenticated"));
        for (const QByteArray &invalid : {QByteArray::fromHex("01000001"), QByteArray::fromHex("00000000")}) {
            QLocalSocket socket;
            socket.connectToServer(service.paths.ipcName);
            QVERIFY(socket.waitForConnected(3000));
            socket.write(invalid);
            QVERIFY(socket.waitForReadyRead(3000));
            QByteArray bytes = socket.readAll(); QList<QJsonObject> replies;
            QVERIFY(takeIpcFrames(bytes, replies));
            QVERIFY(!replies.isEmpty());
            QCOMPARE(replies.first().value("error").toObject().value("code").toString(), QStringLiteral("invalid_protocol"));
        }
        QLocalSocket mismatch;
        mismatch.connectToServer(service.paths.ipcName);
        QVERIFY(mismatch.waitForConnected(3000));
        const auto payload = QJsonDocument(QJsonObject{{"protocol", 999}, {"id", "future"}, {"method", "status"}}).toJson(QJsonDocument::Compact);
        QByteArray message(4, '\0'); qToBigEndian(quint32(payload.size()), message.data()); message += payload;
        mismatch.write(message);
        QVERIFY(mismatch.waitForReadyRead(3000));
        QVERIFY(service.call("status").value("ok").toBool());
        const auto unknown = service.call("does.not.exist");
        QCOMPARE(unknown.value("error").toObject().value("code").toString(), QStringLiteral("unknown_method"));
        const auto invalidSettings = service.call("settings.set", {{"roots", QJsonArray{"relative/path"}}});
        QCOMPARE(invalidSettings.value("error").toObject().value("code").toString(), QStringLiteral("invalid_settings"));
    }

    void isolatedParallelProfiles() {
        TestEnvironment first("service-profile-one"), second("service-profile-two");
        QVERIFY(first.isValid()); QVERIFY(second.isValid());
        ServiceProcess a(first.runtimePaths()), b(second.runtimePaths());
        QVERIFY2(a.start(), qPrintable(a.error)); QVERIFY2(b.start(), qPrintable(b.error));
        QVERIFY(a.paths.ipcName != b.paths.ipcName);
        QVERIFY(a.token != b.token);
        QVERIFY(a.call("settings.set", {{"language", "zh_CN"}}).value("ok").toBool());
        QCOMPARE(b.call("settings.get").value("result").toObject().value("language").toString(), QStringLiteral("system"));
        a.call("shutdown"); QVERIFY(a.process.waitForFinished(5000));
        QVERIFY(b.call("status").value("ok").toBool());
    }

    void cancellationRejectsLateCompletion() {
        TestEnvironment environment("service-cancel");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/document.txt", "boundedcancel source document"));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("selected")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        FileRecord file;
        QVERIFY(FileCatalog::describe(environment.path("selected/document.txt"), &file));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVector<Segment> segments;
            for (int index = 0; index < 512; ++index) {
                Segment segment;
                segment.path = file.path; segment.kind = "text"; segment.title = "document";
                segment.text = QStringLiteral("boundedcancel SQLite progress fixture %1").arg(index);
                segments.append(std::move(segment));
            }
            QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|cancel-fixture", segments, false, &error), qPrintable(error));
        }

        std::mutex barrierMutex;
        std::condition_variable barrierChanged;
        bool released = false;
        std::atomic_int entered{0};
        ServiceDependencies dependencies;
        dependencies.storage.faultHook = [&](const QString &phase) {
            if (phase != "sqlite-search-progress") return;
            std::unique_lock lock(barrierMutex);
            if (released) return;
            entered.fetch_add(1);
            // The hook is inside SQLite, after its publication lock is released.
            // Every running query waits, irrespective of search-pool size.
            barrierChanged.wait(lock, [&] { return released; });
        };
        ServiceServer service(paths, dependencies);
        auto releaseBarrier = [&] {
            {
                const std::lock_guard guard(barrierMutex);
                released = true;
            }
            barrierChanged.notify_all();
        };
        // On every assertion failure release SQLite readers before service.stop()
        // waits for the search pool during destruction.
        const auto releaseGuard = qScopeGuard(releaseBarrier);
        QVERIFY2(service.start(&error), qPrintable(error));
        QFile tokenFile(QDir(paths.profileDir).filePath("qcs-ipc-token"));
        QVERIFY(tokenFile.open(QIODevice::ReadOnly));
        const QString token = QString::fromLatin1(tokenFile.readAll());
        tokenFile.close();

        QByteArray buffer;
        QString protocolError;
        QHash<QString, QList<QJsonObject>> replies;
        QLocalSocket socket;
        QObject::connect(&socket, &QLocalSocket::readyRead, &socket, [&] {
            if (!protocolError.isEmpty()) return;
            buffer += socket.readAll();
            QList<QJsonObject> frames;
            if (!takeIpcFrames(buffer, frames, &protocolError)) return;
            for (const auto &frame : frames) {
                const QString id = frame.value("id").toString();
                if (!id.isEmpty()) replies[id].append(frame);
            }
        });
        socket.connectToServer(paths.ipcName);
        QTRY_COMPARE_WITH_TIMEOUT(socket.state(), QLocalSocket::ConnectedState, 5000);
        auto frame = [&](const QString &id, const QString &method, const QJsonObject &params = {}) {
            return encodeIpcFrame({{"protocol", 1}, {"id", id}, {"token", token}, {"method", method}, {"params", params}});
        };
        auto call = [&](const QString &id, const QString &method, const QJsonObject &params = {}) {
            QEventLoop loop;
            QTimer deadline;
            deadline.setSingleShot(true);
            QObject::connect(&deadline, &QTimer::timeout, &loop, &QEventLoop::quit);
            const auto ready = QObject::connect(&socket, &QLocalSocket::readyRead, &loop, [&] {
                if (replies.contains(id) || !protocolError.isEmpty()) loop.quit();
            });
            socket.write(frame(id, method, params)); socket.flush();
            deadline.start(5000);
            if (!replies.contains(id)) loop.exec();
            QObject::disconnect(ready);
            return replies.value(id).value(0);
        };
        const QJsonObject searchParams{{"query", "boundedcancel"}, {"semantic", false}};
        socket.write(frame("pending-search", "search", searchParams)); socket.flush();
        QTRY_VERIFY_WITH_TIMEOUT(entered.load() >= 1, 5000);
        QVERIFY(!replies.contains("pending-search"));
        const auto cancelled = call("cancel-command", "cancel", {{"id", "pending-search"}});
        QVERIFY(cancelled.value("ok").toBool());
        QVERIFY(cancelled.value("result").toObject().value("cancelled").toBool());
        QCOMPARE(replies.value("pending-search").size(), 1);
        QCOMPARE(replies.value("pending-search").first().value("error").toObject().value("code").toString(), QStringLiteral("cancelled"));
        const auto held = call("held-status", "status");
        QVERIFY(held.value("ok").toBool());
        QCOMPARE(held.value("result").toObject().value("activeSearches").toInt(), 1);

        // Leave these queries uncancelled until backpressure is observed. Every
        // running query is held in SQLite and queued queries cannot complete, so
        // socket fragmentation and search-thread count cannot hide saturation.
        QByteArray flood;
        for (int index = 0; index < 80; ++index)
            flood += frame(QStringLiteral("flood-%1").arg(index), "search", searchParams);
        socket.write(flood); socket.flush();
        auto hasBackpressure = [&] {
            for (int index = 0; index < 80; ++index)
                for (const auto &reply : replies.value(QStringLiteral("flood-%1").arg(index)))
                    if (reply.value("error").toObject().value("code").toString() == "busy") return true;
            return false;
        };
        QTRY_VERIFY_WITH_TIMEOUT(hasBackpressure(), 5000);
        // This response on the same socket follows dispatch of all 80 searches.
        const auto saturated = call("saturated-status", "status");
        QVERIFY(saturated.value("ok").toBool());
        const int active = saturated.value("result").toObject().value("activeSearches").toInt();
        QVERIFY(active > 1); QVERIFY(active <= 64);

        QByteArray cancellations;
        for (int index = 0; index < 80; ++index)
            cancellations += frame(QStringLiteral("stop-%1").arg(index), "cancel", {{"id", QStringLiteral("flood-%1").arg(index)}});
        socket.write(cancellations); socket.flush();
        const auto cancelledStatus = call("cancelled-status", "status");
        QVERIFY(cancelledStatus.value("ok").toBool());
        QVERIFY(cancelledStatus.value("result").toObject().value("activeSearches").toInt() >= 1);
        auto verifyTerminals = [&] {
            if (replies.value("pending-search").size() != 1
                || replies.value("pending-search").first().value("error").toObject().value("code").toString() != "cancelled") return false;
            for (int index = 0; index < 80; ++index) {
                const auto searches = replies.value(QStringLiteral("flood-%1").arg(index));
                const auto stops = replies.value(QStringLiteral("stop-%1").arg(index));
                if (searches.size() != 1 || stops.size() != 1 || !stops.first().value("ok").toBool()) return false;
                const QString code = searches.first().value("error").toObject().value("code").toString();
                if (code != "cancelled" && code != "busy") return false;
                if (stops.first().value("result").toObject().value("cancelled").toBool() != (code == "cancelled")) return false;
            }
            return true;
        };
        QVERIFY2(protocolError.isEmpty(), qPrintable(protocolError));
        QVERIFY(verifyTerminals());

        releaseBarrier();
        int statusSequence = 0;
        auto drained = [&] {
            const auto status = call(QStringLiteral("drained-status-%1").arg(++statusSequence), "status");
            return status.value("ok").toBool() && status.value("result").toObject().value("activeSearches").toInt() == 0;
        };
        QTRY_VERIFY_WITH_TIMEOUT(drained(), 5000);
        // A successful query on the original socket is also a reply-order
        // barrier. Any erroneous late terminal would already be in replies.
        const auto recovered = call("responsive-after-flood", "search", searchParams);
        QVERIFY(recovered.value("ok").toBool());
        QCOMPARE(recovered.value("result").toObject().value("hits").toArray().size(), 1);
        QCOMPARE(recovered.value("result").toObject().value("hits").toArray().first().toObject().value("path").toString(), file.path);
        QVERIFY2(protocolError.isEmpty(), qPrintable(protocolError));
        QVERIFY(verifyTerminals());
        socket.disconnectFromServer(); service.stop();
    }

    void pipelineChangePreservesKeywordWhilePaused() {
        TestEnvironment environment("service-pipeline-preserve");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/document.txt", "pipelinepreservedkeyword"));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("selected")};
        settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        FileRecord file;
        QVERIFY(FileCatalog::describe(environment.path("selected/document.txt"), &file));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            QVERIFY(repository.setPipelineFingerprint("older-test-pipeline", &error));
            Segment segment;
            segment.path = file.path; segment.kind = "text"; segment.title = "document"; segment.text = "pipelinepreservedkeyword";
            QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|older-test-pipeline", {segment}, false, &error), qPrintable(error));
        }
        ServiceProcess service(paths);
        QVERIFY2(service.start(), qPrintable(service.error));
        QVERIFY(service.waitForHit("pipelinepreservedkeyword", file.path, 5000));
        QCOMPARE(service.call("status").value("result").toObject().value("progress").toObject().value("state").toString(), QStringLiteral("paused"));
    }

    void crashedServiceReleasesProfileOwnership() {
        TestEnvironment environment("service-crash");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ServiceProcess original(environment.runtimePaths());
        QVERIFY2(original.start(), qPrintable(original.error));
        const QByteArray previousToken = original.token;
        original.process.kill();
        QVERIFY(original.process.waitForFinished(5000));
        ServiceProcess recovered(environment.runtimePaths());
        QVERIFY2(recovered.start(), qPrintable(recovered.error));
        QVERIFY(recovered.token != previousToken);
        QVERIFY(recovered.call("status").value("ok").toBool());
    }

    void disablingCodeRejectsInFlightResponse() {
        TestEnvironment environment("service-code-race");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ServiceProcess service(environment.runtimePaths());
        QVERIFY2(service.start(), qPrintable(service.error));
        QVERIFY(service.call("settings.set", {{"includeCode", true}}).value("ok").toBool());
        QLocalSocket socket;
        socket.connectToServer(service.paths.ipcName);
        QVERIFY(socket.waitForConnected(3000));
        const QString token = QString::fromLatin1(service.token);
        socket.write(encodeIpcFrame({{"protocol", 1}, {"id", "old-code-query"}, {"token", token}, {"method", "search"},
            {"params", QJsonObject{{"query", "code"}, {"code", true}, {"semantic", false}}}})
            + encodeIpcFrame({{"protocol", 1}, {"id", "disable-code"}, {"token", token}, {"method", "settings.set"},
                {"params", QJsonObject{{"includeCode", false}}}}));
        QByteArray buffer;
        QList<QJsonObject> replies;
        QElapsedTimer timer; timer.start();
        bool rejected = false;
        while (timer.elapsed() < 5000 && !rejected) {
            if (!socket.bytesAvailable()) socket.waitForReadyRead(100);
            buffer += socket.readAll();
            replies.clear();
            QVERIFY(takeIpcFrames(buffer, replies));
            for (const auto &reply : replies) if (reply.value("id").toString() == "old-code-query") {
                QCOMPARE(reply.value("error").toObject().value("code").toString(), QStringLiteral("code_search_disabled"));
                rejected = true;
            }
        }
        QVERIFY(rejected);
    }

    void indexingChangePauseExcludeAndRestart() {
        TestEnvironment environment("service-index 中文 space");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("资料 空格/报告.txt", QByteArray("A searchable needle contract. ") + QStringLiteral("中文搜索与文档").toUtf8()));
        QVERIFY(environment.writeFile("资料 空格/排除/hidden.txt", "needle hidden"));
        const QString root = environment.path("资料 空格");
        const QString document = environment.path("资料 空格/报告.txt");
        {
            ServiceProcess service(environment.runtimePaths());
            QVERIFY2(service.start(), qPrintable(service.error));
            QVERIFY(service.call("settings.set", {{"roots", QJsonArray{root}}, {"excludes", QJsonArray{environment.path("资料 空格/排除")}}}).value("ok").toBool());
            QVERIFY2(service.waitForHit("needle", document), qPrintable(QString::fromUtf8(service.process.readAllStandardError())));
            QCOMPARE(service.search("needle").size(), 1);
            QVERIFY(service.waitForHit(QStringLiteral("中文"), document));
            const auto preview = service.call("document.preview", {{"path", document}, {"maxSegments", 1}}, 30000);
            QVERIFY2(preview.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(preview).toJson())));
            QVERIFY(preview.value("result").toObject().value("text").toString().contains("needle"));
            QVERIFY(environment.writeFile("outside/private.txt", "outside_selected_roots"));
            const auto forbidden = service.call("document.preview", {{"path", environment.path("outside/private.txt")}});
            QCOMPARE(forbidden.value("error").toObject().value("code").toString(), QStringLiteral("invalid_request"));
            QVERIFY(service.call("index.pause").value("ok").toBool());
            QCOMPARE(service.call("status").value("result").toObject().value("progress").toObject().value("state").toString(), QStringLiteral("paused"));
            QVERIFY(environment.writeFile("资料 空格/报告.txt", "changednewtoken with a different byte length"));
            // Stale source versions are rejected during query even before reconciliation.
            QCOMPARE(service.search("needle").size(), 0);
            QVERIFY(service.call("index.resume").value("ok").toBool());
            QVERIFY(service.waitForHit("changednewtoken", document));
            QVERIFY(service.call("index.pause").value("ok").toBool());
        }
        {
            ServiceProcess restarted(environment.runtimePaths());
            QVERIFY2(restarted.start(), qPrintable(restarted.error));
            QVERIFY(restarted.waitForHit("changednewtoken", document));
            QCOMPARE(restarted.call("settings.get").value("result").toObject().value("indexingPaused").toBool(), true);
            QVERIFY(restarted.call("settings.set", {{"roots", QJsonArray{}}}).value("ok").toBool());
            QCOMPARE(restarted.search("changednewtoken").size(), 0);
        }
    }

    void codeClassificationToggle_data() {
        QTest::addColumn<bool>("longPath");
        QTest::newRow("ordinary-path") << false;
        QTest::newRow("long-unicode-path") << true;
    }

    void codeClassificationToggle() {
        QFETCH(bool, longPath);
        TestEnvironment environment("service-code-toggle");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QString relativeRoot = QStringLiteral("repository");
        if (longPath) while (environment.path(relativeRoot).size() < 320)
            relativeRoot += QStringLiteral("/中文 空格 nested");
        const QString repository = environment.path(relativeRoot);
        const QString excludesPath = repository + "/.git/qcs-empty-excludes";
        QString escapedExcludesPath = QDir::fromNativeSeparators(excludesPath);
        escapedExcludesPath.replace('\\', "\\\\").replace('"', "\\\"");
        const QByteArray config = QByteArray("[core]\nrepositoryformatversion = 0\nbare = false\nlongpaths = false\nexcludesfile = \"")
            + escapedExcludesPath.toUtf8() + "\"\n";
        // A valid empty repository fixture needs no installed Git executable.
        QVERIFY(environment.writeFile(relativeRoot + "/.git/HEAD", "ref: refs/heads/main\n"));
        // User-level ignore patterns must not hide this fixture's untracked source.
        QVERIFY(environment.writeFile(relativeRoot + "/.git/qcs-empty-excludes", {}));
        QVERIFY(environment.writeFile(relativeRoot + "/.git/config", config));
        QVERIFY(QDir().mkpath(repository + "/.git/objects"));
        QVERIFY(QDir().mkpath(repository + "/.git/refs/heads"));
        QVERIFY(environment.writeFile(relativeRoot + "/README.md", "classificationtoggleword documentation"));
        QVERIFY(environment.writeFile(relativeRoot + "/untracked.cpp", "int untrackedcodetoken = 1;\n"));
        QVERIFY(environment.writeFile(relativeRoot + "/.gitignore", "ignored.cpp\n"));
        QVERIFY(environment.writeFile(relativeRoot + "/ignored.cpp", "int ignoredcodetoken = 1;\n"));
        const QString path = repository + "/README.md";
        ServiceProcess service(environment.runtimePaths());
        QVERIFY2(service.start(), qPrintable(service.error));
        QVERIFY(service.call("settings.set", {{"roots", QJsonArray{repository}}, {"includeCode", false}}).value("ok").toBool());
        QVERIFY2(service.waitForHit("classificationtoggleword", path), qPrintable(serviceDiagnostics(service.paths, service.call("status"))));
        QVERIFY(service.call("settings.set", {{"includeCode", true}}).value("ok").toBool());
        auto codeHits = [&] {
            return service.call("search", {{"query", "classificationtoggleword"}, {"code", true}, {"semantic", false}})
                .value("result").toObject().value("hits").toArray();
        };
        QTRY_VERIFY2_WITH_TIMEOUT(codeHits().size() == 1,
            qPrintable(serviceDiagnostics(service.paths, service.call("status"))), 30000);
        QCOMPARE(service.search("classificationtoggleword").size(), 0);
        auto sourceHits = [&] (const QString &query) {
            return service.call("search", {{"query", query}, {"code", true}, {"semantic", false}})
                .value("result").toObject().value("hits").toArray();
        };
        QTRY_VERIFY2_WITH_TIMEOUT(sourceHits("untrackedcodetoken").size() == 1,
            qPrintable(serviceDiagnostics(service.paths, service.call("status"))), 30000);
        QVERIFY(sourceHits("ignoredcodetoken").isEmpty());
        QVERIFY(service.call("settings.set", {{"includeCode", false}}).value("ok").toBool());
        QVERIFY2(service.waitForHit("classificationtoggleword", path), qPrintable(serviceDiagnostics(service.paths, service.call("status"))));
        const auto disabled = service.call("search", {{"query", "classificationtoggleword"}, {"code", true}});
        QCOMPARE(disabled.value("error").toObject().value("code").toString(), QStringLiteral("code_search_disabled"));
        QFile configFile(repository + "/.git/config");
        QVERIFY(configFile.open(QIODevice::ReadOnly));
        QCOMPARE(configFile.readAll(), config);
        QVERIFY(!QFileInfo::exists(repository + "/.git/index"));
    }

    void continuationSurvivesMissingJournal() {
        TestEnvironment environment("service-continuation");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QByteArray content = QByteArray("A repeatable long document with distinct paragraphs.\n").repeated(4000)
            + QByteArray("continuationfinaltoken\n");
        QVERIFY(environment.writeFile("selected/long.txt", content));
        const auto paths = environment.runtimePaths();
        const QString path = environment.path("selected/long.txt");
        QString indexedVersion;
        QVector<Segment> original;
        {
            ServiceProcess service(paths);
            QVERIFY2(service.start(), qPrintable(service.error));
            QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.path("selected")}}}).value("ok").toBool());
            QVERIFY(service.waitForHit("continuationfinaltoken", path));
            QVERIFY(service.call("index.pause").value("ok").toBool());
        }
        {
            IndexRepository repository;
            QString error;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            original = repository.fileSegments(path, false, &error);
            QVERIFY2(error.isEmpty(), qPrintable(error));
            QVERIFY(original.size() > 40);
            indexedVersion = repository.fileVersion(path);
            QVERIFY(!indexedVersion.contains("|partial:"));
            QVERIFY2(repository.replaceFile(path, indexedVersion + "|partial:40", original.first(40), false, &error), qPrintable(error));
        }
        // Simulate loss of diagnostic state after the authoritative batch commit.
        const QString journal = QDir(paths.indexDir).filePath("qcs-progress.json");
        QVERIFY(!QFileInfo::exists(journal) || QFile::remove(journal));
        {
            ServiceProcess resumed(paths);
            QVERIFY2(resumed.start(), qPrintable(resumed.error));
            QVERIFY(resumed.call("index.resume").value("ok").toBool());
            auto progress = [&] { return resumed.call("status").value("result").toObject().value("progress").toObject(); };
            QTRY_COMPARE_WITH_TIMEOUT(progress().value("state").toString(), QStringLiteral("idle"), 30000);
            QCOMPARE(progress().value("segments").toInteger(), original.size() - 40);
            QVERIFY(resumed.waitForHit("continuationfinaltoken", path));
        }
        IndexRepository repository;
        QString error;
        QVERIFY2(repository.open(paths, &error), qPrintable(error));
        const auto restored = repository.fileSegments(path, false, &error);
        QCOMPARE(restored.size(), original.size());
        QCOMPARE(repository.fileVersion(path), indexedVersion);
        for (qsizetype index = 0; index < restored.size(); ++index) QCOMPARE(restored[index].text, original[index].text);
    }
};

QTEST_GUILESS_MAIN(ServiceTests)
#include "test_service.moc"
