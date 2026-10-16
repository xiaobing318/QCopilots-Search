#include "service/ipc_protocol.h"
#include "service/ipc_security.h"
#include "service/service_client.h"
#include "service/service_server.h"
#include "core/runtime_paths.h"
#include "core/types.h"
#include "core/app_settings.h"
#include "core/file_catalog.h"
#include "core/pipeline_manifest.h"
#include "storage/index_repository.h"
#include "support/test_environment.h"
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QLocalSocket>
#include <QProcess>
#include <QSignalSpy>
#include <QScopeGuard>
#include <QStorageInfo>
#include <QTest>
#include <QThread>
#include <QTimer>
#include <QUuid>
#include <QtEndian>
#ifndef Q_OS_WIN
#include <sys/un.h>
#endif
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <stdexcept>
#include <iostream>

using namespace qcopilots_search;
using namespace qcopilots_search::test;

namespace {
// This executable doubles as a protocol peer for lifecycle and scheduling
// tests. The seam is C++ construction-only and never affects installed workers.
int controlledWorker(const QStringList &arguments) {
    if (arguments.size() != 6) return 2;
    const QString logPath = arguments.at(2);
    const QString mode = arguments.at(3);
    const QString gates = arguments.at(4);
    const int delayMs = arguments.at(5).toInt();
    const auto record = [&](QJsonObject event) {
        event.insert("pid", QString::number(QCoreApplication::applicationPid()));
        QFile log(logPath);
        if (!log.open(QIODevice::WriteOnly | QIODevice::Append)) return false;
        const auto bytes = QJsonDocument(event).toJson(QJsonDocument::Compact) + '\n';
        return log.write(bytes) == bytes.size() && log.flush();
    };
    const auto waitForGate = [&](const QString &name) {
        QElapsedTimer elapsed; elapsed.start();
        while (QFileInfo::exists(QDir(gates).filePath(mode + u'-' + name))) {
            if (elapsed.elapsed() > 15000) return false;
            QThread::msleep(5);
        }
        return true;
    };
    const QString requestedBackend = qEnvironmentVariable("QCOPILOTS_SEARCH_BACKEND", "auto");
    if (!record({{"event", "started"}, {"backend", requestedBackend}})) return 3;
    std::string line;
    while (std::getline(std::cin, line)) {
        const auto request = QJsonDocument::fromJson(QByteArray::fromStdString(line)).object();
        const QString operation = request.value("operation").toString();
        if (operation == "shutdown") {
            if (!record({{"event", "shutdown"}}) || !waitForGate("shutdown")) return 4;
            return record({{"event", "exited"}}) ? 0 : 3;
        }
        if (operation == "cancel") continue;
        QFile crashPolicy(QDir(gates).filePath(mode + "-crash-policy"));
        if (crashPolicy.open(QIODevice::ReadOnly)) {
            const auto policy = QString::fromUtf8(crashPolicy.readAll()).trimmed();
            const auto runtimeEvent = [&](const char *phase) {
                const QJsonObject runtime{{"requestedBackend", requestedBackend}, {"packagedBackend", "cuda"},
                    {"buildVariant", "test-cuda"}, {"loaded", policy == "reload"},
                    {"gpuSelected", policy != "reload" && requestedBackend != "cpu"}};
                const auto event = QJsonDocument(QJsonObject{{"id", request.value("id")}, {"event", "runtime"},
                    {"data", QJsonObject{{"phase", phase}, {"activity", policy == "reload" ? "model-load" : "encode"},
                        {"runtime", runtime}}}}).toJson(QJsonDocument::Compact);
                std::cout << event.constData() << '\n' << std::flush;
            };
            if (policy != "parser") runtimeEvent("inference-start");
            if (policy == "finished") runtimeEvent("inference-end");
            if ((policy != "gpu" && policy != "reload") || requestedBackend != "cpu") {
                if (!record({{"event", "aborted"}, {"backend", requestedBackend}})) return 3;
                // A nonzero child exit models an SDK's fatal termination while
                // keeping CI free of OS crash dialogs and core dump files.
                return 71;
            }
            runtimeEvent("inference-end");
        }
        QJsonObject result;
        if (operation == "extract") {
            const QString path = request.value("path").toString();
            const int offset = request.value("offset").toInt();
            if (!record({{"event", operation}, {"path", path}, {"offset", offset},
                         {"code", request.value("code")}})) return 3;
            if (!waitForGate(QFileInfo(path).fileName() + u'-' + QString::number(offset))) return 4;
            QFile source(path);
            if (!source.open(QIODevice::ReadOnly)) return 5;
            const auto fixture = QJsonDocument::fromJson(source.readAll()).object();
            const int count = fixture.value("segments").toInt(1);
            const int next = qMin(count, offset + request.value("maxSegments").toInt(40));
            QJsonArray segments;
            for (int i = offset; i < next; ++i) {
                Segment segment;
                segment.path = path; segment.title = QFileInfo(path).fileName();
                segment.kind = request.value("code").toBool() ? "code" : "text";
                segment.modality = "text";
                segment.text = fixture.value("token").toString("controlledkeyword") + u' ' + QString::number(i);
                segment.lineStart = i + 1; segment.lineEnd = i + 1;
                if (request.value("embed").toBool()) {
                    segment.embedding.fill(0.0f, embeddingDimensions);
                    segment.embedding[0] = 1.0f;
                }
                segments.append(segment.toJson());
            }
            const bool complete = next == count;
            result = {{"segments", segments}, {"state", complete ? "complete" : "partial"},
                {"complete", complete}, {"next", next}, {"sourceVersion", request.value("expectedVersion")},
                {"extractionFingerprint", extractionPipelineFingerprint()},
                {"modelFingerprint", embeddingPipelineFingerprint()}};
        } else if (operation == "query") {
            if (!record({{"event", operation}}) || !waitForGate("query")) return 4;
            QJsonArray embedding;
            for (int i = 0; i < embeddingDimensions; ++i) embedding.append(i == 0 ? 1.0 : 0.0);
            result = {{"embedding", embedding}, {"modelFingerprint", embeddingPipelineFingerprint()}};
        } else return 6;
        // Deliberately identify this protocol test peer as CPU. This makes
        // strict GPU service tests detect a peer that silently fell back.
        result.insert("runtime", QJsonObject{{"buildVariant", "test-cpu"}, {"requestedBackend", "auto"},
            {"packagedBackend", "cpu"}, {"selectedBackend", "cpu"}, {"deviceName", "Protocol test CPU"},
            {"deviceDescription", "Lifecycle test peer"}, {"gpuSelected", false},
            {"fallbackReason", "Test fixture has no GPU"}, {"loaded", true}, {"error", ""}});
        if (delayMs > 0) QThread::msleep(static_cast<unsigned long>(delayMs));
        const auto response = QJsonDocument(QJsonObject{{"id", request.value("id")}, {"ok", true}, {"result", result}})
            .toJson(QJsonDocument::Compact);
        std::cout << response.constData() << '\n' << std::flush;
    }
    return record({{"event", "eof"}}) ? 0 : 3;
}

struct ControlledWorkers {
    explicit ControlledWorkers(const TestEnvironment &environment) : root(environment.path("worker-control")) {
        valid = QDir().mkpath(root);
    }
    QString root;
    bool valid = false;
    QString logPath(bool query) const { return QDir(root).filePath(query ? "query.jsonl" : "index.jsonl"); }
    QString gatePath(bool query, const QString &name) const {
        return QDir(root).filePath((query ? QStringLiteral("query-") : QStringLiteral("index-")) + name);
    }
    bool block(bool query, const QString &name) const {
        QFile gate(gatePath(query, name)); return gate.open(QIODevice::WriteOnly);
    }
    bool release(bool query, const QString &name) const { return QFile::remove(gatePath(query, name)); }
    QList<QJsonObject> events(bool query, const QString &type = {}) const {
        QList<QJsonObject> result;
        QFile file(logPath(query));
        if (!file.open(QIODevice::ReadOnly)) return result;
        while (!file.atEnd()) {
            const auto line = file.readLine();
            if (!line.endsWith('\n')) break;
            const auto event = QJsonDocument::fromJson(line).object();
            if (!event.isEmpty() && (type.isEmpty() || event.value("event").toString() == type)) result.append(event);
        }
        return result;
    }
    ServiceDependencies dependencies(bool models = false, int delayMs = 0) const {
        ServiceDependencies result;
        result.verifyModels = [models](QString *) { return models; };
        result.configureWorker = [*this, delayMs](QProcess &worker, bool query) {
            worker.setProgram(QCoreApplication::applicationFilePath());
            worker.setArguments({"--qcs-test-worker", logPath(query), query ? "query" : "index", root, QString::number(delayMs)});
        };
        return result;
    }
};

QByteArray controlledDocument(const QString &token, int segments = 1) {
    return QJsonDocument(QJsonObject{{"token", token}, {"segments", segments}}).toJson(QJsonDocument::Compact);
}

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

    void connectionArrivingBeforeListenReturnsIsAccepted() {
        TestEnvironment environment("service-early-listen");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));

        QLocalSocket socket;
        QSignalSpy disconnected(&socket, &QLocalSocket::disconnected);
        QString listenError;
        int signalsBeforeListenReturned = 0;
        ServiceDependencies dependencies;
        dependencies.listen = [&](QLocalServer &listener, const QString &name) {
            QSignalSpy connections(&listener, &QLocalServer::newConnection);
            if (!listener.listen(name)) {
                listenError = listener.errorString();
                return false;
            }
            socket.connectToServer(name);
            if (!socket.waitForConnected(5000)) {
                listenError = socket.errorString();
                return false;
            }
            // Drive a real pending connection synchronously before the listen
            // callback returns. The production receiver may consume it, so
            // waitForNewConnection's pending-queue return value is not proof.
            if (connections.isEmpty()) listener.waitForNewConnection(5000);
            signalsBeforeListenReturned = connections.count();
            if (signalsBeforeListenReturned != 1)
                listenError = QStringLiteral("Expected one real newConnection before listen returned, saw %1")
                    .arg(signalsBeforeListenReturned);
            return listenError.isEmpty();
        };
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error + u'\n' + listenError));
        QCOMPARE(signalsBeforeListenReturned, 1);
        const auto listeners = service.findChildren<QLocalServer *>(QString(), Qt::FindDirectChildrenOnly);
        QCOMPARE(listeners.size(), 1);
        auto *listener = listeners.first();

        QFile tokenFile(QDir(paths.profileDir).filePath("qcs-ipc-token"));
        QVERIFY(tokenFile.open(QIODevice::ReadOnly));
        const QString token = QString::fromLatin1(tokenFile.readAll());
        tokenFile.close();
        const QString requestId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        QByteArray buffer;
        QString protocolError;
        QList<QJsonObject> replies;
        const auto receive = QObject::connect(&socket, &QLocalSocket::readyRead, &socket, [&] {
            buffer += socket.readAll();
            QList<QJsonObject> frames;
            if (!takeIpcFrames(buffer, frames, &protocolError)) return;
            for (const auto &frame : frames)
                if (frame.value("id").toString() == requestId) replies.append(frame);
        });
        const auto disconnectReceive = qScopeGuard([&] { QObject::disconnect(receive); });
        // Send only after start returns. This isolates the lost newConnection
        // from any separate question about pre-buffered readyRead delivery.
        const auto frame = encodeIpcFrame({{"protocol", protocolVersion}, {"id", requestId},
            {"token", token}, {"method", "status"}, {"params", QJsonObject{}}});
        QVERIFY(!frame.isEmpty());
        QCOMPARE(socket.write(frame), qint64(frame.size()));
        socket.flush();
        qInfo().noquote() << QStringLiteral("early-listen request=%1 signalsBeforeReturn=%2 pendingAfterStart=%3")
            .arg(requestId).arg(signalsBeforeListenReturned).arg(listener->hasPendingConnections());
        const auto diagnostics = [&] {
            return QStringLiteral("request=%1 signalsBeforeReturn=%2 pending=%3 replies=%4 disconnected=%5 socketError=%6 protocolError=%7")
                .arg(requestId).arg(signalsBeforeListenReturned).arg(listener->hasPendingConnections())
                .arg(replies.size()).arg(disconnected.count()).arg(socket.errorString(), protocolError);
        };
        QVERIFY2(QTest::qWaitFor([&] {
            return !replies.isEmpty() || !protocolError.isEmpty() || !disconnected.isEmpty();
        }, 3000), qPrintable(diagnostics()));
        QVERIFY2(protocolError.isEmpty(), qPrintable(diagnostics()));
        QCOMPARE(replies.size(), 1);
        QCOMPARE(replies.first().value("protocol").toInt(), protocolVersion);
        QVERIFY2(replies.first().value("ok").toBool(), qPrintable(QJsonDocument(replies.first()).toJson()));
        QVERIFY(replies.first().value("result").toObject().contains("progress"));
        QVERIFY(!listener->hasPendingConnections());
        qInfo().noquote() << QStringLiteral("early-listen response=%1 accepted=true").arg(requestId);
        socket.abort();
        service.stop();
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
        TestSymbolicLink ownedLink;
        bool regularLeafCreated = false;
        const auto cleanup = qScopeGuard([&] {
            if (regularLeafCreated && !QFile::remove(paths.ipcName))
                recordCleanupFailure(QStringLiteral("Cannot remove test-owned IPC leaf: %1").arg(paths.ipcName));
        });
        if (symbolicLink) {
            QVERIFY2(ownedLink.create(paths.ipcName, target, &error), qPrintable(error));
        } else {
            QFile leaf(paths.ipcName);
            QVERIFY(leaf.open(QIODevice::WriteOnly | QIODevice::NewOnly));
            regularLeafCreated = true;
            QCOMPARE(leaf.write(marker), marker.size());
        }
        ServiceProcess service(paths);
        QVERIFY(!service.start());
        QVERIFY2(service.error.contains(QStringLiteral("Refusing to remove")), qPrintable(service.error));
        QCOMPARE(QFileInfo(paths.ipcName).isSymLink(), symbolicLink);
        QFile leaf(paths.ipcName);
        QVERIFY(leaf.open(QIODevice::ReadOnly));
        QCOMPARE(leaf.readAll(), marker);
        leaf.close();
        if (symbolicLink) {
            QVERIFY2(ownedLink.remove(&error), qPrintable(error));
        } else {
            QVERIFY(QFile::remove(paths.ipcName));
            regularLeafCreated = false;
        }
        QVERIFY(!QFileInfo(paths.ipcName).isSymLink());
        QVERIFY(!QFileInfo::exists(paths.ipcName));
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
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
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
        auto ready = [&] { return service.call("status").value("result").toObject().value("catalogReady").toBool(); };
        QTRY_VERIFY_WITH_TIMEOUT(ready(), 5000);
        auto limited = [&](const QString &root = {}) {
            return service.call("search", {{"query", "needle"}, {"semantic", false}, {"limit", 1}, {"root", root}})
                .value("result").toObject().value("hits").toArray();
        };
        QCOMPARE(limited().size(), 1);
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("first/needle.txt"));
        // A query root filters before limit without deleting either fixture row.
        const auto second = limited(environment.path("second"));
        QCOMPARE(second.size(), 1);
        QCOMPARE(second.first().toObject().value("path").toString(), environment.path("second/document.txt"));
        QVERIFY(service.call("settings.set", {{"excludes", QJsonArray{environment.path("first")}}}).value("ok").toBool());
        QTRY_VERIFY_WITH_TIMEOUT(ready(), 5000);
        // An explicitly selected root overrides an equal exclusion, as in FileCatalog.
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("first/needle.txt"));
        QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.root()}},
            {"excludes", QJsonArray{environment.path("first")}}}).value("ok").toBool());
        QTRY_VERIFY_WITH_TIMEOUT(ready(), 5000);
        QCOMPARE(limited().size(), 1);
        QCOMPARE(limited().first().toObject().value("path").toString(), environment.path("second/document.txt"));
        // Pausing stops extraction. Successful metadata reconciliation still prunes exclusions.
        QCOMPARE(service.call("status").value("result").toObject().value("repository").toObject().value("files").toInt(), 1);
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
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
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
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
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
        int catalogRequest = 0;
        QTRY_VERIFY_WITH_TIMEOUT(call(QStringLiteral("catalog-ready-%1").arg(++catalogRequest), "status")
            .value("result").toObject().value("catalogReady").toBool(), 5000);
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
        QTRY_VERIFY_WITH_TIMEOUT(service.call("status").value("result").toObject().value("catalogReady").toBool(), 5000);
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
        const QString relativeRoot = QStringLiteral("repository");
        // Upstream libgit2 supports long working-tree paths through the APP
        // configuration, while repository metadata retains its own path limit.
        QString relativeWorkTree = relativeRoot;
        if (longPath) while (environment.path(relativeWorkTree).size() < 320)
            relativeWorkTree += QStringLiteral("/中文 空格 nested 🔍");
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
        QVERIFY(environment.writeFile(relativeWorkTree + "/README.md", "classificationtoggleword documentation"));
        QVERIFY(environment.writeFile(relativeWorkTree + "/untracked.cpp", "int untrackedcodetoken = 1;\n"));
        QVERIFY(environment.writeFile(relativeRoot + "/.gitignore", "ignored.cpp\n"));
        QVERIFY(environment.writeFile(relativeWorkTree + "/ignored.cpp", "int ignoredcodetoken = 1;\n"));
        QVERIFY(environment.writeFile("ordinary/README.md", "classificationtoggleword ordinary documentation"));
        const QString path = environment.path(relativeWorkTree + "/README.md");
        const QString ordinaryPath = environment.path("ordinary/README.md");
        ServiceProcess service(environment.runtimePaths());
        QVERIFY2(service.start(), qPrintable(service.error));
        QVERIFY(service.call("settings.set", {{"roots", QJsonArray{environment.path("ordinary")}},
            {"codeRoots", QJsonArray{repository}}, {"includeCode", false}}).value("ok").toBool());
        QVERIFY2(service.waitForHit("classificationtoggleword", ordinaryPath), qPrintable(serviceDiagnostics(service.paths, service.call("status"))));
        QVERIFY(service.call("settings.set", {{"includeCode", true}}).value("ok").toBool());
        auto codeHits = [&] {
            return service.call("search", {{"query", "classificationtoggleword"}, {"code", true}, {"semantic", false}})
                .value("result").toObject().value("hits").toArray();
        };
        QTRY_VERIFY2_WITH_TIMEOUT(codeHits().size() == 1,
            qPrintable(serviceDiagnostics(service.paths, service.call("status"))), 30000);
        QCOMPARE(codeHits().first().toObject().value("path").toString(), path);
        const auto ordinary = service.search("classificationtoggleword");
        QCOMPARE(ordinary.size(), 1);
        QCOMPARE(ordinary.first().value("path").toString(), ordinaryPath);
        auto sourceHits = [&] (const QString &query) {
            return service.call("search", {{"query", query}, {"code", true}, {"semantic", false}})
                .value("result").toObject().value("hits").toArray();
        };
        QTRY_VERIFY2_WITH_TIMEOUT(sourceHits("untrackedcodetoken").size() == 1,
            qPrintable(serviceDiagnostics(service.paths, service.call("status"))), 30000);
        QVERIFY(sourceHits("ignoredcodetoken").isEmpty());
        QVERIFY(service.call("settings.set", {{"includeCode", false}}).value("ok").toBool());
        QVERIFY2(service.waitForHit("classificationtoggleword", ordinaryPath), qPrintable(serviceDiagnostics(service.paths, service.call("status"))));
        const auto retained = service.call("settings.get").value("result").toObject();
        QCOMPARE(retained.value("codeRoots").toArray(), QJsonArray{repository});
        const auto disabled = service.call("search", {{"query", "classificationtoggleword"}, {"code", true}});
        QCOMPARE(disabled.value("error").toObject().value("code").toString(), QStringLiteral("code_search_disabled"));
        QFile configFile(repository + "/.git/config");
        QVERIFY(configFile.open(QIODevice::ReadOnly));
        QCOMPARE(configFile.readAll(), config);
        QVERIFY(!QFileInfo::exists(repository + "/.git/index"));
    }

    void unavailableRootPreservesItsIndexWithoutBlockingHealthyRoot() {
        TestEnvironment environment("service-partial-catalog");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QStringList relativePaths{"healthy/live.txt", "healthy/deleted.txt", "unavailable/retained.txt"};
        for (const auto &path : relativePaths) QVERIFY(environment.writeFile(path, "partialcatalogkeyword"));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("healthy"), environment.path("unavailable")};
        settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            for (const auto &relative : relativePaths) {
                FileRecord file;
                QVERIFY(FileCatalog::describe(environment.path(relative), &file));
                Segment segment;
                segment.path = file.path; segment.title = QFileInfo(file.path).fileName();
                segment.kind = "text"; segment.modality = "text"; segment.text = "partialcatalogkeyword";
                QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|legacy", {segment}, false, &error), qPrintable(error));
            }
        }
        // Only the two named fixture files and the now-empty fixture directory
        // are removed. The unavailable root's database row must remain intact.
        QVERIFY(QFile::remove(environment.path("healthy/deleted.txt")));
        QVERIFY(QFile::remove(environment.path("unavailable/retained.txt")));
        QVERIFY(QDir().rmdir(environment.path("unavailable")));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        ServiceServer service(paths, workers.dependencies());
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
        const auto search = localCall(client, "search", {{"query", "partialcatalogkeyword"}, {"semantic", false}});
        QVERIFY2(search.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(search).toJson())));
        const auto hits = search.value("result").toObject().value("hits").toArray();
        QCOMPARE(hits.size(), 1);
        QCOMPARE(hits.first().toObject().value("path").toString(), environment.path("healthy/live.txt"));
        const auto listing = localCall(client, "files.list");
        QVERIFY2(listing.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(listing).toJson())));
        const auto files = listing.value("result").toObject().value("files").toArray();
        QCOMPARE(files.size(), 1);
        QCOMPARE(files.first().toObject().value("path").toString(), environment.path("healthy/live.txt"));
        QCOMPARE(files.first().toObject().value("modifiedMs").toString().toLongLong(),
            QFileInfo(environment.path("healthy/live.txt")).lastModified().toMSecsSinceEpoch());
        const auto status = localCall(client, "status").value("result").toObject();
        QCOMPARE(status.value("repository").toObject().value("files").toInt(), 2);
        QVERIFY(status.value("progress").toObject().value("error").toString().contains(environment.path("unavailable")));
        QVERIFY(workers.events(false, "started").isEmpty());
        client.disconnectFromService(); service.stop();
        IndexRepository repository;
        QVERIFY2(repository.open(paths, &error), qPrintable(error));
        QVERIFY(repository.fileVersion(environment.path("healthy/deleted.txt")).isEmpty());
        QVERIFY(!repository.fileVersion(environment.path("unavailable/retained.txt")).isEmpty());
    }

    void pausedLegacyMetadataRepairHidesChangedSourceWithoutBlockingHealthyFile() {
        TestEnvironment environment("service-stale-legacy-metadata");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/healthy.txt", "legacymetadatakeyword healthy source"));
        QVERIFY(environment.writeFile("selected/changed.txt", "legacymetadatakeyword original source"));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("selected")}; settings.indexingPaused = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        QString originalChangedVersion;
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            for (const QString &relative : {QStringLiteral("selected/healthy.txt"), QStringLiteral("selected/changed.txt")}) {
                FileRecord file;
                QVERIFY(FileCatalog::describe(environment.path(relative), &file));
                Segment segment;
                segment.path = file.path; segment.title = QFileInfo(file.path).fileName();
                segment.kind = "text"; segment.modality = "text"; segment.text = "legacymetadatakeyword";
                const QString version = file.fingerprint + "|legacy";
                QVERIFY2(repository.replaceFile(file.path, version, {segment}, false, &error), qPrintable(error));
                if (relative.endsWith("changed.txt")) originalChangedVersion = version;
            }
        }
        QVERIFY(environment.writeFile("selected/changed.txt", "A changed source whose old body must stay hidden while extraction is paused."));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        ServiceServer service(paths, workers.dependencies());
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
        const auto listing = localCall(client, "files.list");
        QVERIFY2(listing.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(listing).toJson())));
        const auto files = listing.value("result").toObject().value("files").toArray();
        QCOMPARE(files.size(), 1);
        QCOMPARE(files.first().toObject().value("path").toString(), environment.path("selected/healthy.txt"));
        QVERIFY(!listing.value("result").toObject().value("metadataPending").toBool());
        const auto search = localCall(client, "search", {{"query", "legacymetadatakeyword"}, {"semantic", false}});
        QVERIFY(search.value("ok").toBool());
        const auto hits = search.value("result").toObject().value("hits").toArray();
        QCOMPARE(hits.size(), 1);
        QCOMPARE(hits.first().toObject().value("path").toString(), environment.path("selected/healthy.txt"));
        const auto status = localCall(client, "status").value("result").toObject();
        QCOMPARE(status.value("repository").toObject().value("files").toInt(), 2);
        QCOMPARE(status.value("repository").toObject().value("metadataPendingFiles").toInteger(), 0);
        QCOMPARE(status.value("progress").toObject().value("state").toString(), QStringLiteral("paused"));
        QVERIFY(workers.events(false, "started").isEmpty());
        client.disconnectFromService(); service.stop();
        IndexRepository repository;
        QVERIFY2(repository.open(paths, &error), qPrintable(error));
        // Metadata refresh must never relabel old indexed content as the new source.
        QCOMPARE(repository.fileVersion(environment.path("selected/changed.txt")), originalChangedVersion);
    }

    void independentSpacesRepairMetadataAndBrowseWithoutContent() {
        TestEnvironment environment("service-browse-spaces");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QStringList relativePaths{"ordinary/a.txt", "ordinary/b.txt", "shared/note.txt", "code/source.cpp"};
        for (const auto &path : relativePaths)
            QVERIFY(environment.writeFile(path, controlledDocument("browsingkeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots = {environment.path("ordinary"), environment.path("shared")};
        settings.codeRoots = {environment.path("code"), environment.path("shared")};
        settings.includeCode = true;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        {
            IndexRepository repository;
            QVERIFY2(repository.open(paths, &error), qPrintable(error));
            for (const auto &relative : relativePaths) {
                FileRecord file;
                QVERIFY(FileCatalog::describe(environment.path(relative), &file));
                const QList<bool> spaces = relative.startsWith("shared/") ? QList<bool>{false, true}
                    : QList<bool>{relative.startsWith("code/")};
                for (const bool code : spaces) {
                    Segment segment;
                    segment.path = file.path; segment.title = QFileInfo(file.path).fileName();
                    segment.kind = code ? "code" : "text"; segment.modality = "text";
                    segment.text = "legacybodymustnotappearinfilelist";
                    // Omit mtime deliberately, as in a database from before file browsing.
                    QVERIFY2(repository.replaceFile(file.path, file.fingerprint + "|legacy", {segment}, code, &error), qPrintable(error));
                }
            }
        }
        ControlledWorkers workers(environment);
        QVERIFY(workers.valid);
        for (const auto &relative : relativePaths)
            QVERIFY(workers.block(false, QFileInfo(relative).fileName() + "-0"));
        ServiceServer service(paths, workers.dependencies());
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QJsonObject firstPage;
        QTRY_VERIFY_WITH_TIMEOUT(([&] {
            firstPage = localCall(client, "files.list", {{"limit", 1}});
            return firstPage.value("ok").toBool()
                && !firstPage.value("result").toObject().value("metadataPending").toBool()
                && firstPage.value("result").toObject().value("files").toArray().size() == 1;
        })(), 5000);
        QVERIFY(firstPage.value("result").toObject().value("hasMore").toBool());
        const QString oldCursor = firstPage.value("result").toObject().value("nextCursor").toString();
        QVERIFY(!oldCursor.isEmpty());
        for (const bool code : {false, true}) {
            QSet<QString> actual;
            QString cursor;
            bool more = true;
            for (int page = 0; more && page < 8; ++page) {
                const auto response = localCall(client, "files.list", {{"code", code}, {"limit", 1}, {"cursor", cursor}});
                QVERIFY2(response.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(response).toJson())));
                const auto result = response.value("result").toObject();
                const auto entries = result.value("files").toArray();
                QCOMPARE(entries.size(), 1);
                const auto entry = entries.first().toObject();
                const QString path = entry.value("path").toString();
                QVERIFY(!actual.contains(path)); actual.insert(path);
                QCOMPARE(entry.value("code").toBool(), code);
                QVERIFY(!entry.value("fileId").toString().isEmpty());
                QCOMPARE(entry.value("modifiedMs").toString().toLongLong(), QFileInfo(path).lastModified().toMSecsSinceEpoch());
                QVERIFY(!entry.value("sourceVersion").toString().isEmpty());
                QVERIFY(!entry.contains("text")); QVERIFY(!entry.contains("embedding"));
                more = result.value("hasMore").toBool();
                cursor = result.value("nextCursor").toString();
                QCOMPARE(cursor.isEmpty(), !more);
            }
            QVERIFY(!more);
            const QSet<QString> expected = code
                ? QSet<QString>{environment.path("code/source.cpp"), environment.path("shared/note.txt")}
                : QSet<QString>{environment.path("ordinary/a.txt"), environment.path("ordinary/b.txt"), environment.path("shared/note.txt")};
            QCOMPARE(actual, expected);
        }
        // Removing code roots invalidates old cursors and prunes only that space.
        QVERIFY(localCall(client, "settings.set", {{"codeRoots", QJsonArray{}}}).value("ok").toBool());
        const auto stale = localCall(client, "files.list", {{"limit", 1}, {"cursor", oldCursor}});
        QCOMPARE(stale.value("error").toObject().value("code").toString(), QStringLiteral("cursor_stale"));
        QJsonObject ordinary;
        QTRY_VERIFY_WITH_TIMEOUT(([&] {
            ordinary = localCall(client, "files.list", {{"limit", 100}});
            return ordinary.value("ok").toBool()
                && ordinary.value("result").toObject().value("files").toArray().size() == 3;
        })(), 5000);
        const auto code = localCall(client, "files.list", {{"code", true}});
        QVERIFY(code.value("ok").toBool());
        QVERIFY(code.value("result").toObject().value("files").toArray().isEmpty());
        client.disconnectFromService(); service.stop();
    }

    void idleWorkersReleaseAndRestartDespiteFrequentReconciliation() {
        TestEnvironment environment("service-idle-workers");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/first.txt", controlledDocument("idlefirstkeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        auto dependencies = workers.dependencies(true);
        dependencies.indexIdleMs = 150; dependencies.queryIdleMs = 150;
        dependencies.reconcileMs = 25; dependencies.workerShutdownGraceMs = 1000;
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(false, "exited").size(), 1, 5000);
        const auto firstQuery = localCall(client, "search", {{"query", "idlefirstkeyword"}, {"semantic", true}});
        QVERIFY(firstQuery.value("ok").toBool());
        QVERIFY(firstQuery.value("result").toObject().value("semantic").toBool());
        QVERIFY(firstQuery.value("result").toObject().value("warning").toString().isEmpty());
        const auto diagnosticStatus = localCall(client, "status").value("result").toObject();
        const auto queryRuntime = diagnosticStatus.value("runtimeWorkers").toObject().value("query").toObject();
        QCOMPARE(queryRuntime.value("buildVariant").toString(), QString("test-cpu"));
        QCOMPARE(queryRuntime.value("selectedBackend").toString(), QString("cpu"));
        QCOMPARE(queryRuntime.value("deviceName").toString(), QString("Protocol test CPU"));
        QCOMPARE(queryRuntime.value("fallbackReason").toString(), QString("Test fixture has no GPU"));
        QVERIFY(!queryRuntime.value("gpuSelected").toBool());
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(true, "exited").size(), 1, 5000);
        QTRY_VERIFY_WITH_TIMEOUT(!localCall(client, "status").value("result").toObject()
            .value("runtimeWorkers").toObject().value("query").toObject().value("loaded").toBool(), 5000);
        QVERIFY(environment.writeFile("selected/second.txt", controlledDocument("idlesecondkeyword")));
        QVERIFY(localCall(client, "index.start").value("ok").toBool());
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(false, "exited").size(), 2, 5000);
        const auto secondQuery = localCall(client, "search", {{"query", "idlesecondkeyword"}, {"semantic", true}});
        QVERIFY(secondQuery.value("ok").toBool());
        const auto result = secondQuery.value("result").toObject();
        QVERIFY(result.value("semantic").toBool());
        QVERIFY(result.value("warning").toString().isEmpty());
        bool foundSecond = false;
        for (const auto &hit : result.value("hits").toArray())
            if (hit.toObject().value("path").toString() == environment.path("selected/second.txt")) foundSecond = true;
        QVERIFY(foundSecond);
        QCOMPARE(workers.events(false, "started").size(), 2);
        QCOMPARE(workers.events(true, "started").size(), 2);
        const auto progress = localCall(client, "status").value("result").toObject().value("progress").toObject();
        QCOMPARE(progress.value("failed").toInteger(), 0);
        QVERIFY(progress.value("error").toString().isEmpty());
        client.disconnectFromService(); service.stop();
    }

    void strictGpuDoesNotAcceptACpuWorkerResponse() {
        TestEnvironment environment("service-strict-gpu");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ScopedEnvironment backend("QCOPILOTS_SEARCH_BACKEND", "gpu");
        QVERIFY(environment.writeFile("selected/entry.txt", controlledDocument("strictquerykeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        ServiceServer service(paths, workers.dependencies(true));
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
        const auto reply = localCall(client, "search", {{"query", "strictquerykeyword"}, {"semantic", true}});
        QVERIFY(!reply.value("ok").toBool());
        QCOMPARE(reply.value("error").toObject().value("code").toString(), QString("gpu_unavailable"));
        QVERIFY(localCall(client, "search", {{"query", "strictquerykeyword"}, {"semantic", false}}).value("ok").toBool());
        client.disconnectFromService(); service.stop();
    }

    void gpuWorkerTerminationRetriesCpuOnlyWithInferenceEvidence_data() {
        QTest::addColumn<bool>("queryWorker");
        QTest::addColumn<QString>("mode");
        QTest::addColumn<QString>("policy");
        QTest::addColumn<int>("expectedStarts");
        QTest::addColumn<bool>("succeeds");
        for (bool query : {false, true}) {
            const QByteArray role = query ? "query-" : "index-";
            QTest::newRow((role + "auto-gpu-crash").constData()) << query << QString("auto") << QString("gpu") << 2 << true;
            QTest::newRow((role + "normalized-auto-gpu-crash").constData()) << query << QString(" AuTo ") << QString("gpu") << 2 << true;
            QTest::newRow((role + "auto-reload-after-cpu").constData()) << query << QString("auto") << QString("reload") << 2 << true;
            QTest::newRow((role + "cpu-retry-also-crashes").constData()) << query << QString("auto") << QString("always") << 2 << false;
            QTest::newRow((role + "parser-crash").constData()) << query << QString("auto") << QString("parser") << 1 << false;
            QTest::newRow((role + "after-inference-crash").constData()) << query << QString("auto") << QString("finished") << 1 << false;
            QTest::newRow((role + "strict-gpu-crash").constData()) << query << QString("gpu") << QString("gpu") << 1 << false;
            QTest::newRow((role + "explicit-cpu-crash").constData()) << query << QString("cpu") << QString("always") << 1 << false;
        }
    }

    void gpuWorkerTerminationRetriesCpuOnlyWithInferenceEvidence() {
        QFETCH(bool, queryWorker);
        QFETCH(QString, mode);
        QFETCH(QString, policy);
        QFETCH(int, expectedStarts);
        QFETCH(bool, succeeds);
        TestEnvironment environment("service-gpu-crash-recovery");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ScopedEnvironment selectedBackend("QCOPILOTS_SEARCH_BACKEND", mode.toUtf8());
        QVERIFY(environment.writeFile("selected/entry.txt", controlledDocument("gpurecoverykeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        QVERIFY(environment.writeFile(QString("worker-control/%1-crash-policy").arg(queryWorker ? "query" : "index"), policy.toUtf8()));
        auto dependencies = workers.dependencies(true);
        dependencies.reconcileMs = 60000;
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("catalogReady").toBool(), 5000);
        if (queryWorker) {
            const auto reply = localCall(client, "search", {{"query", "gpurecoverykeyword"}, {"semantic", true}});
            if (succeeds) {
                QVERIFY2(reply.value("ok").toBool(), qPrintable(QString::fromUtf8(QJsonDocument(reply).toJson())));
                QVERIFY(reply.value("result").toObject().value("semantic").toBool());
            } else if (mode == "gpu") {
                QVERIFY(!reply.value("ok").toBool());
                QCOMPARE(reply.value("error").toObject().value("code").toString(), QString("gpu_unavailable"));
            } else {
                QVERIFY(reply.value("ok").toBool());
                QVERIFY(!reply.value("result").toObject().value("semantic").toBool());
                QVERIFY(!reply.value("result").toObject().value("warning").toString().isEmpty());
            }
        } else {
            QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "status").value("result").toObject().value("progress").toObject()
                .value(succeeds ? "completed" : "failed").toInteger() == 1, 5000);
        }
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(queryWorker, "started").size(), expectedStarts, 5000);
        const auto starts = workers.events(queryWorker, "started");
        QCOMPARE(starts.first().value("backend").toString(), mode);
        if (expectedStarts == 2) {
            QCOMPARE(starts.last().value("backend").toString(), QString("cpu"));
            const auto runtime = localCall(client, "status").value("result").toObject()
                .value("runtimeWorkers").toObject().value(queryWorker ? "query" : "index").toObject();
            QVERIFY(runtime.value("fallbackReason").toString().contains("GPU worker exited"));
            if (succeeds) {
                QCOMPARE(runtime.value("selectedBackend").toString(), QString("cpu"));
                QCOMPARE(runtime.value("requestedBackend").toString(), QString("auto"));
                QCOMPARE(runtime.value("effectiveBackendRequest").toString(), QString("cpu"));
            }
        }
        QTest::qWait(100);
        QCOMPARE(workers.events(queryWorker, "started").size(), expectedStarts);
        client.disconnectFromService(); service.stop();
    }

    void workArrivingWhileWorkerCloses_data() {
        QTest::addColumn<bool>("queryWorker");
        QTest::newRow("index") << false;
        QTest::newRow("query") << true;
    }

    void workArrivingWhileWorkerCloses() {
        QFETCH(bool, queryWorker);
        TestEnvironment environment("service-closing-worker");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/first.txt", controlledDocument("closingfirstkeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        QVERIFY(workers.block(queryWorker, "shutdown"));
        auto dependencies = workers.dependencies(queryWorker);
        dependencies.indexIdleMs = queryWorker ? 60000 : 100;
        dependencies.queryIdleMs = 100;
        dependencies.workerShutdownGraceMs = 3000;
        dependencies.reconcileMs = 25;
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "search", {{"query", "closingfirstkeyword"}, {"semantic", false}})
            .value("result").toObject().value("hits").toArray().size() == 1, 5000);
        if (queryWorker) {
            const auto initial = localCall(client, "search", {{"query", "closingfirstkeyword"}, {"semantic", true}});
            QVERIFY(initial.value("result").toObject().value("semantic").toBool());
        }
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(queryWorker, "shutdown").size(), 1, 5000);
        QVERIFY(workers.events(queryWorker, "exited").isEmpty());
        const QString firstPid = workers.events(queryWorker, "started").first().value("pid").toString();
        if (queryWorker) {
            bool released = false;
            QTimer::singleShot(150, &client, [&] { released = workers.release(true, "shutdown"); });
            const auto response = localCall(client, "search", {{"query", "closingfirstkeyword"}, {"semantic", true}});
            QVERIFY(released);
            QVERIFY(response.value("ok").toBool());
            QVERIFY(response.value("result").toObject().value("semantic").toBool());
            QVERIFY(response.value("result").toObject().value("warning").toString().isEmpty());
            QCOMPARE(response.value("result").toObject().value("hits").toArray().size(), 1);
        } else {
            QVERIFY(environment.writeFile("selected/second.txt", controlledDocument("closingsecondkeyword")));
            QVERIFY(localCall(client, "index.start").value("ok").toBool());
            QTest::qWait(100);
            QCOMPARE(workers.events(false, "extract").size(), 1);
            QVERIFY(workers.release(false, "shutdown"));
            QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "search", {{"query", "closingsecondkeyword"}, {"semantic", false}})
                .value("result").toObject().value("hits").toArray().size() == 1, 5000);
        }
        QCOMPARE(workers.events(queryWorker, "started").size(), 2);
        const QString secondPid = workers.events(queryWorker, "started").last().value("pid").toString();
        QVERIFY(secondPid != firstPid);
        const auto operations = workers.events(queryWorker, queryWorker ? "query" : "extract");
        QCOMPARE(operations.size(), 2);
        QCOMPARE(operations.last().value("pid").toString(), secondPid);
        const auto progress = localCall(client, "status").value("result").toObject().value("progress").toObject();
        QCOMPARE(progress.value("failed").toInteger(), 0);
        QVERIFY(progress.value("error").toString().isEmpty());
        client.disconnectFromService(); service.stop();
    }

    void scansMergeNewFilesBetweenLongDocumentBatches() {
        TestEnvironment environment("service-fair-batches");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/a-long.txt", controlledDocument("fairlongkeyword", 240)));
        QVERIFY(environment.writeFile("selected/z-existing.txt", controlledDocument("fairexistingkeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        QVERIFY(workers.block(false, "a-long.txt-0"));
        auto dependencies = workers.dependencies(false, 20);
        dependencies.reconcileMs = 25;
        ServiceServer service(paths, dependencies);
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        const auto longPath = environment.path("selected/a-long.txt");
        QTRY_VERIFY_WITH_TIMEOUT(([&] {
            for (const auto &event : workers.events(false, "extract"))
                if (event.value("path").toString() == longPath) return true;
            return false;
        })(), 5000);
        QVERIFY(environment.writeFile("selected/b-arriving.txt", controlledDocument("fairarrivingkeyword")));
        // The in-flight batch remains blocked while a new scan observes this file.
        QVERIFY(localCall(client, "index.start").value("ok").toBool());
        QTest::qWait(150);
        QVERIFY(localCall(client, "status").value("ok").toBool());
        QVERIFY(workers.release(false, "a-long.txt-0"));
        QTRY_VERIFY_WITH_TIMEOUT(workers.events(false, "extract").size() >= 8, 10000);
        const auto operations = workers.events(false, "extract");
        QList<int> longOffsets;
        int existingIndex = -1, arrivingIndex = -1, lastLongIndex = -1;
        for (qsizetype i = 0; i < operations.size(); ++i) {
            const auto &operation = operations.at(i);
            const QString path = operation.value("path").toString();
            if (path == longPath) {
                longOffsets.append(operation.value("offset").toInt()); lastLongIndex = int(i);
            }
            if (path == environment.path("selected/z-existing.txt")) existingIndex = int(i);
            if (path == environment.path("selected/b-arriving.txt")) arrivingIndex = int(i);
        }
        QCOMPARE(longOffsets, QList<int>({0, 40, 80, 120, 160, 200}));
        QVERIFY(existingIndex >= 0 && existingIndex < lastLongIndex);
        QVERIFY(arrivingIndex >= 0 && arrivingIndex < lastLongIndex);
        QTRY_VERIFY_WITH_TIMEOUT(localCall(client, "search", {{"query", "fairlongkeyword 239"}, {"semantic", false}})
            .value("result").toObject().value("hits").toArray().size() == 1, 5000);
        const auto progress = localCall(client, "status").value("result").toObject().value("progress").toObject();
        QCOMPARE(progress.value("failed").toInteger(), 0);
        client.disconnectFromService(); service.stop();
    }

    void changedQueuedFileIsPrioritizedAheadOfBulkBacklog() {
        TestEnvironment environment("service-recent-queued-change");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        // The first-created tail is oldest, and its name sorts last if the
        // filesystem gives several fixture writes the same timestamp.
        const QString tailRelative = QStringLiteral("selected/z-tail.txt");
        QVERIFY(environment.writeFile(tailRelative, controlledDocument("queuedoldversionkeyword")));
        constexpr int bulkCount = 52;
        for (int i = 0; i < bulkCount; ++i)
            QVERIFY(environment.writeFile(QStringLiteral("selected/bulk-%1.txt").arg(i, 2, 10, QLatin1Char('0')),
                controlledDocument("bulkbacklogkeyword")));
        QVERIFY(environment.writeFile("selected/a-block.txt", controlledDocument("recentblockkeyword")));
        const auto paths = environment.runtimePaths();
        const QString tailPath = environment.path(tailRelative);
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        QVERIFY(workers.block(false, "a-block.txt-0"));
        ServiceServer service(paths, workers.dependencies());
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(false, "extract").size(), 1, 5000);
        QCOMPARE(workers.events(false, "extract").first().value("path").toString(), environment.path("selected/a-block.txt"));
        QCOMPARE(localCall(client, "status").value("result").toObject().value("queuedFiles").toInt(), bulkCount + 2);
        QSignalSpy events(&client, &ServiceClient::eventReceived);
        QVERIFY(localCall(client, "index.pause").value("ok").toBool());
        QVERIFY(environment.writeFile(tailRelative, controlledDocument("queuednewversionkeyword with a different source length")));
        QVERIFY(localCall(client, "index.reconcile").value("ok").toBool());
        QVERIFY(workers.release(false, "a-block.txt-0"));
        // Pause holds later extraction until the metadata merge confirms the
        // changed source, rather than relying on a fixed scan duration.
        const auto catalogMerged = [&] {
            for (const auto &event : events)
                if (event.at(0).toString() == "indexChanged"
                    && event.at(1).value<QJsonObject>().value("path").toString().isEmpty()) return true;
            return false;
        };
        QTRY_VERIFY_WITH_TIMEOUT(catalogMerged(), 5000);
        QCOMPARE(workers.events(false, "extract").size(), 1);
        QVERIFY(localCall(client, "index.resume").value("ok").toBool());
        QTRY_COMPARE_WITH_TIMEOUT(localCall(client, "status").value("result").toObject()
            .value("progress").toObject().value("state").toString(), QStringLiteral("idle"), 10000);
        const auto operations = workers.events(false, "extract");
        QCOMPARE(operations.size(), bulkCount + 2);
        int tailIndex = -1, tailAttempts = 0;
        QSet<QString> pathsSeen;
        for (qsizetype i = 0; i < operations.size(); ++i) {
            const QString path = operations.at(i).value("path").toString();
            QVERIFY(!pathsSeen.contains(path)); pathsSeen.insert(path);
            if (path == tailPath) { tailIndex = int(i); ++tailAttempts; }
        }
        QCOMPARE(tailAttempts, 1);
        // The updated source must reach the user near the front, before most
        // of the unchanged backlog, without prescribing the internal lanes.
        QVERIFY2(tailIndex > 0 && tailIndex < 6, qPrintable(QStringLiteral("Changed file remained at extraction position %1 of %2")
            .arg(tailIndex).arg(operations.size())));
        const auto current = localCall(client, "search", {{"query", "queuednewversionkeyword"}, {"semantic", false}});
        QVERIFY(current.value("ok").toBool());
        const auto hits = current.value("result").toObject().value("hits").toArray();
        QCOMPARE(hits.size(), 1);
        QCOMPARE(hits.first().toObject().value("path").toString(), tailPath);
        const auto obsolete = localCall(client, "search", {{"query", "queuedoldversionkeyword"}, {"semantic", false}});
        QVERIFY(obsolete.value("ok").toBool());
        QVERIFY(obsolete.value("result").toObject().value("hits").toArray().isEmpty());
        client.disconnectFromService(); service.stop();
    }

    void newProjectMarkerWithdrawsQueuedOrdinaryFile() {
        TestEnvironment environment("service-queued-exclusion");
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile("selected/z-project/queued.txt", controlledDocument("excludedqueuedkeyword")));
        QVERIFY(environment.writeFile("selected/a-block.txt", controlledDocument("retainedblockkeyword")));
        const auto paths = environment.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings; settings.roots = {environment.path("selected")};
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ControlledWorkers workers(environment); QVERIFY(workers.valid);
        QVERIFY(workers.block(false, "a-block.txt-0"));
        ServiceServer service(paths, workers.dependencies());
        QVERIFY2(service.start(&error), qPrintable(error));
        ServiceClient client(paths); client.setAutoStart(false); client.connectToService();
        QTRY_VERIFY(client.isConnected());
        QTRY_COMPARE_WITH_TIMEOUT(workers.events(false, "extract").size(), 1, 5000);
        QCOMPARE(workers.events(false, "extract").first().value("path").toString(), environment.path("selected/a-block.txt"));
        QCOMPARE(localCall(client, "status").value("result").toObject().value("queuedFiles").toInt(), 2);
        QVERIFY(localCall(client, "index.pause").value("ok").toBool());
        QVERIFY(environment.writeFile("selected/z-project/CMakeLists.txt", "project(ExcludedFixture)\n"));
        QVERIFY(localCall(client, "index.reconcile").value("ok").toBool());
        QVERIFY(workers.release(false, "a-block.txt-0"));
        // The in-flight batch may finish, while pause holds every later batch.
        // A completed reconciliation must withdraw the now-ineligible job.
        QJsonObject settled;
        QTRY_VERIFY_WITH_TIMEOUT(([&] {
            settled = localCall(client, "status").value("result").toObject();
            return settled.value("catalogReady").toBool()
                && settled.value("queuedFiles").toInt() == 0
                && settled.value("progress").toObject().value("state").toString() == "paused";
        })(), 5000);
        QVERIFY(localCall(client, "index.resume").value("ok").toBool());
        QTRY_COMPARE_WITH_TIMEOUT(localCall(client, "status").value("result").toObject()
            .value("progress").toObject().value("state").toString(), QStringLiteral("idle"), 5000);
        QCOMPARE(workers.events(false, "extract").size(), 1);
        const auto excluded = localCall(client, "search", {{"query", "excludedqueuedkeyword"}, {"semantic", false}});
        QVERIFY(excluded.value("ok").toBool());
        QVERIFY(excluded.value("result").toObject().value("hits").toArray().isEmpty());
        const auto listing = localCall(client, "files.list");
        QVERIFY(listing.value("ok").toBool());
        const auto files = listing.value("result").toObject().value("files").toArray();
        QCOMPARE(files.size(), 1);
        QCOMPARE(files.first().toObject().value("path").toString(), environment.path("selected/a-block.txt"));
        client.disconnectFromService(); service.stop();
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

int main(int argc, char **argv) {
    QCoreApplication application(argc, argv);
    if (application.arguments().value(1) == "--qcs-test-worker")
        return controlledWorker(application.arguments());
    ServiceTests tests;
    return QTest::qExec(&tests, argc, argv);
}
#include "test_service.moc"
