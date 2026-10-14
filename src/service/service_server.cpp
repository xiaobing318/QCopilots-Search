#include "service_server.h"
#include "ipc_protocol.h"
#include "ipc_security.h"
#include "core/app_settings.h"
#include "core/file_catalog.h"
#include "core/model_files.h"
#include "core/native_lock_path.h"
#include "core/pipeline_manifest.h"
#include "storage/index_repository.h"
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QFutureWatcher>
#include <QJsonDocument>
#include <QLocalServer>
#include <QLocalSocket>
#include <QLockFile>
#include <QPointer>
#include <QProcess>
#include <QProcessEnvironment>
#include <QQueue>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QThreadPool>
#include <QTimer>
#include <QUuid>
#include <QtConcurrent>
#include <algorithm>
#include <atomic>
#include <cstring>
#include <mutex>
#include <utility>

namespace qcopilots_search {
namespace {
QString sourceVersion(const QString &path)
{
    const QFileInfo file(path);
    if (!file.exists() || !file.isFile()) return {};
    const auto bytes = file.canonicalFilePath().toUtf8() + '\0' + QByteArray::number(file.size())
        + ':' + QByteArray::number(file.lastModified().toMSecsSinceEpoch());
    return QString::fromLatin1(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex());
}

QString executablePath(const QString &name)
{
    QString executable = name;
#ifdef Q_OS_WIN
    executable += QStringLiteral(".exe");
#endif
    const QDir bin(QCoreApplication::applicationDirPath());
    for (const QString &candidate : {bin.filePath(executable), bin.filePath(QStringLiteral("../Helpers/") + executable),
                                    bin.filePath(QStringLiteral("../bin/") + executable)}) {
        if (QFileInfo(candidate).isExecutable()) return QFileInfo(candidate).absoluteFilePath();
    }
    return bin.filePath(executable);
}

QString pipelineVersion(bool models)
{
    QByteArray material("qcopilots-search-pipeline-v1:");
    material += extractionPipelineFingerprint().toUtf8() + ':';
    material += models ? embeddingPipelineFingerprint().toUtf8() : QByteArray("keyword-only");
    if (models) for (const auto &artifact : ModelFiles::manifest()) material += ':' + artifact.sha256;
    return QString::fromLatin1(QCryptographicHash::hash(material, QCryptographicHash::Sha256).toHex());
}

bool constantEquals(const QByteArray &a, const QByteArray &b)
{
    if (a.size() != b.size()) return false;
    uchar difference = 0;
    for (qsizetype i = 0; i < a.size(); ++i) difference |= uchar(a[i]) ^ uchar(b[i]);
    return difference == 0;
}

struct Job {
    FileRecord file;
    QString workerVersion;
    QString indexedVersion;
    quint64 generation = 0;
    int offset = 0;
    QVector<Segment> segments;
    quint64 batch = 0;
    bool retry = false;
    quint64 queueToken = 0;
};

struct QueueEntry { QString key; quint64 token = 0; };

QString pathKey(QString path, bool code)
{
#ifdef Q_OS_WIN
    path = path.toCaseFolded();
#endif
    return (code ? QStringLiteral("code:") : QStringLiteral("general:")) + path;
}

QString fileKey(const FileRecord &file) { return pathKey(file.path, file.kind == "code"); }

QString failureKey(const FileRecord &file)
{
    return file.kind == "code" ? QStringLiteral("code:") + file.path : file.path;
}

struct ScanPackage {
    ScanResult scan;
    QHash<QString, QString> versions;
    QStringList generalRoots, codeRoots;
    quint64 generation = 0;
};
struct SearchPackage { QList<SearchHit> hits; QString error; };
}

class ServiceServer::Private {
public:
    explicit Private(ServiceServer *owner, RuntimePaths value, ServiceDependencies dependencies) : q(owner), paths(std::move(value)),
        repository(std::move(dependencies.storage)), modelVerifier(std::move(dependencies.verifyModels)),
        listenFunction(std::move(dependencies.listen)),
        configureWorker(std::move(dependencies.configureWorker)),
        indexIdleMs(qMax(1, dependencies.indexIdleMs)), queryIdleMs(qMax(1, dependencies.queryIdleMs)),
        shutdownGraceMs(qMax(1, dependencies.workerShutdownGraceMs)), reconcileMs(qMax(1, dependencies.reconcileMs)),
        lock(nativeLockFilePath(paths.lockFile)), server(owner), monitor(owner), reconcile(owner), debounce(owner),
        indexWorker(owner), queryWorker(owner), indexTimeout(owner), queryTimeout(owner),
        indexIdle(owner), queryIdle(owner), indexShutdownGrace(owner), queryShutdownGrace(owner) {
        scanPool.setMaxThreadCount(1);
        writePool.setMaxThreadCount(1);
        searchPool.setMaxThreadCount(2);
        // Windows can emit newConnection synchronously from listen(). Register
        // once before listening so an early client is not left pending.
        QObject::connect(&server, &QLocalServer::newConnection, q, [this] { accept(); });
    }
    ~Private() { releaseClients(); }

    struct Pending {
        QPointer<QLocalSocket> socket;
        QString id;
        QJsonObject params;
        quint64 generation = 0;
        std::shared_ptr<std::atomic_bool> cancelled = std::make_shared<std::atomic_bool>(false);
    };
    ServiceServer *q;
    RuntimePaths paths;
    AppSettings settings;
    IndexRepository repository;
    std::function<bool(QString *)> modelVerifier;
    std::function<bool(QLocalServer &, const QString &)> listenFunction;
    std::function<void(QProcess &, bool)> configureWorker;
    int indexIdleMs, queryIdleMs, shutdownGraceMs, reconcileMs;
    QLockFile lock;
    QLocalServer server;
    QFileSystemWatcher monitor;
    QTimer reconcile, debounce;
    QProcess indexWorker, queryWorker;
    QTimer indexTimeout, queryTimeout;
    QTimer indexIdle, queryIdle, indexShutdownGrace, queryShutdownGrace;
    enum class ExitReason { None, Idle, Restart, Fault, Stop };
    ExitReason indexExit = ExitReason::None, queryExit = ExitReason::None;
    QByteArray indexBuffer, queryBuffer, token;
    QHash<QLocalSocket *, QByteArray> buffers;
    QHash<QString, Pending> requests;
    QHash<QString, Pending> activeSearches;
    QHash<QString, QPointer<QProcess>> previewWorkers;
    QQueue<QString> queryQueue;
    QString activeQuery;
    // Queues hold only keys. The map owns metadata, never accumulated segments.
    QQueue<QueueEntry> recentJobs, backfillJobs, continuationJobs;
    QHash<QString, Job> jobs;
    int queueTurn = 0;
    quint64 nextBatch = 0;
    quint64 nextQueueToken = 0;
    std::unique_ptr<Job> current;
    std::unique_ptr<ScanPackage> pendingCatalog;
    QSet<QString> scanProtectedGeneral, scanProtectedCode;
    QHash<QString, QString> knownVersions;
    QSet<QString> completedFiles, failedFiles;
    QSet<QString> catalogFiles;
    QString currentWorkerId;
    QJsonObject failures;
    QJsonObject indexRuntime, queryRuntime;
    bool latestRuntimeIsQuery = false;
    bool indexGpuOperation = false, queryGpuOperation = false;
    QString indexCpuFallback, queryCpuFallback;
    IndexProgress progress;
    QThreadPool scanPool, writePool, searchPool;
    std::mutex publicationConfigurationMutex;
    std::atomic_bool scanCancelled{false};
    quint64 generation = 1;
    quint64 visibilityEpoch = 1;
    bool started = false, stopping = false, scanning = false, publishing = false;
    bool indexRestartPending = false, queryRestartPending = false;
    bool modelAvailable = false, pendingScan = false;
    bool pipelineReady = false, retryIncomplete = false;
    bool catalogReady = false, firstScan = true;
    QString pipeline;
    QString pipelineError;
    QString watcherMode = QStringLiteral("periodic");
    QString watcherWarning;
    QStringList previousRoots;
    QStringList previousCodeRoots;
    QStringList catalogRoots, catalogCodeRoots;

    QString tokenPath() const { return QDir(paths.profileDir).filePath(QStringLiteral("qcs-ipc-token")); }
    QString journalPath() const { return QDir(paths.indexDir).filePath(QStringLiteral("qcs-progress.json")); }

    void releaseClients() {
        // QLocalServer owns accepted sockets. Destroy them before Private's
        // maps: a Windows socket destructor can emit disconnected synchronously.
        // deleteLater is insufficient when the event loop is already stopping.
        QSet<QLocalSocket *> clients;
        for (auto *socket : buffers.keys()) clients.insert(socket);
        // A peer may already have disconnected and been removed from buffers,
        // while its deleteLater event is still pending on the server's child.
        for (auto *socket : server.findChildren<QLocalSocket *>()) clients.insert(socket);
        buffers.clear();
        for (auto *socket : clients) {
            QObject::disconnect(socket, nullptr, q, nullptr);
            socket->abort();
            delete socket;
        }
    }

    void log(const QString &message) {
        QFile file(QDir(paths.logDir).filePath(QStringLiteral("qcs-service.log")));
        if (file.size() > 4 * 1024 * 1024) {
            const QString previous = file.fileName() + QStringLiteral(".previous");
            QFile::remove(previous);
            file.rename(previous);
            file.setFileName(QDir(paths.logDir).filePath(QStringLiteral("qcs-service.log")));
        }
        if (file.open(QIODevice::Append)) file.write((QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)
            + u' ' + message + u'\n').toUtf8());
    }

    bool start(QString *error) {
        if (!paths.ensure(error) || !settings.load(paths.configFile, error)) return false;
        lock.setStaleLockTime(0);
        if (!lock.tryLock(0)) {
            if (error) *error = lock.error() == QLockFile::LockFailedError
                ? QStringLiteral("already_running: Another service owns this profile")
                : QStringLiteral("Cannot acquire the profile lock (error %1): %2").arg(int(lock.error())).arg(paths.lockFile);
            return false;
        }
        // Only the authoritative profile-lock holder may remove a stale socket.
        // Reject symlinks and ordinary files instead of unlinking arbitrary data.
        if (!removeStaleIpcSocket(paths, error)) { lock.unlock(); return false; }
        if (!repository.open(paths, error)) { lock.unlock(); return false; }
        if (settings.needsMigration() && !settings.save(paths.configFile, error)) { lock.unlock(); return false; }
        if (!refreshModels(error)) { lock.unlock(); return false; }
        token.resize(32);
        for (int i = 0; i < 32; i += 4) {
            const auto value = QRandomGenerator::system()->generate();
            memcpy(token.data() + i, &value, 4);
        }
        token = token.toHex();
        QSaveFile secret(tokenPath());
        if (!secret.open(QIODevice::WriteOnly)
            || !secret.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner)
            || secret.write(token) != token.size() || !secret.commit()) {
            if (error) *error = QStringLiteral("Cannot create private IPC authentication token: %1").arg(secret.errorString());
            lock.unlock();
            return false;
        }
        server.setSocketOptions(QLocalServer::UserAccessOption);
        if (!(listenFunction ? listenFunction(server, paths.ipcName) : server.listen(paths.ipcName))) {
            if (error) *error = QStringLiteral("%1 (endpoint %2, %3 encoded bytes)")
                .arg(server.errorString(), paths.ipcName).arg(QFile::encodeName(paths.ipcName).size());
            lock.unlock();
            return false;
        }
        QObject::connect(&monitor, &QFileSystemWatcher::directoryChanged, q, [this] { debounce.start(); });
        QObject::connect(&monitor, &QFileSystemWatcher::fileChanged, q, [this] { debounce.start(); });
        debounce.setSingleShot(true);
        debounce.setInterval(750);
        QObject::connect(&debounce, &QTimer::timeout, q, [this] { startScan(); });
        reconcile.setInterval(reconcileMs);
        QObject::connect(&reconcile, &QTimer::timeout, q, [this] { startScan(); });
        setupWorker(indexWorker, false);
        setupWorker(queryWorker, true);
        setupIdle(false);
        setupIdle(true);
        indexTimeout.setSingleShot(true);
        queryTimeout.setSingleShot(true);
        QObject::connect(&indexTimeout, &QTimer::timeout, q, [this] {
            publishFailedCurrent(QStringLiteral("Index worker timed out")); stopWorkerForRestart(false, ExitReason::Fault);
        });
        QObject::connect(&queryTimeout, &QTimer::timeout, q, [this] {
            const QString id = activeQuery;
            activeQuery.clear();
            finishError(id, QStringLiteral("timeout"), QStringLiteral("Query encoder timed out"));
            stopWorkerForRestart(true, ExitReason::Fault);
        });
        loadJournal();
        previousRoots = settings.roots;
        previousCodeRoots = settings.codeRoots;
        updateWatchers();
        started = true;
        reconcile.start();
        QTimer::singleShot(0, q, [this] { startScan(); });
        log(QStringLiteral("Service started for profile %1").arg(paths.profileDir));
        return true;
    }

    void stop() {
        if (!started || stopping) return;
        stopping = true;
        for (const auto &request : std::as_const(requests)) request.cancelled->store(true);
        for (const auto &request : std::as_const(activeSearches)) request.cancelled->store(true);
        reconcile.stop(); debounce.stop(); indexTimeout.stop(); queryTimeout.stop();
        indexIdle.stop(); queryIdle.stop(); indexShutdownGrace.stop(); queryShutdownGrace.stop();
        indexExit = queryExit = ExitReason::Stop;
        scanCancelled.store(true);
        { const std::lock_guard guard(publicationConfigurationMutex); ++generation; }
        saveJournal();
        server.close();
        releaseClients();
        for (auto *worker : {&indexWorker, &queryWorker}) {
            if (worker->state() != QProcess::NotRunning) {
                worker->write("{\"id\":\"shutdown\",\"operation\":\"shutdown\"}\n");
                worker->closeWriteChannel();
                if (!worker->waitForFinished(1500)) { worker->kill(); worker->waitForFinished(3000); }
            }
        }
        const auto previews = previewWorkers.values();
        for (auto worker : previews) if (worker) {
            worker->kill(); worker->waitForFinished(3000);
        }
        scanPool.waitForDone(); writePool.waitForDone(); searchPool.waitForDone();
        QString error;
        if (!repository.flush(&error)) log(error);
        QFile::remove(tokenPath());
        lock.unlock();
        started = false;
    }

    void accept() {
        while (auto *socket = server.nextPendingConnection()) {
            if (buffers.size() >= 32) { socket->abort(); socket->deleteLater(); continue; }
            QString peerError;
            if (!ipcPeerIsCurrentUser(socket->socketDescriptor(), &peerError)) {
                log(peerError);
                socket->abort(); socket->deleteLater(); continue;
            }
            buffers.insert(socket, {});
            socket->setReadBufferSize(maxMessageBytes + 4);
            QObject::connect(socket, &QLocalSocket::readyRead, q, [this, socket] {
                if (!buffers.contains(socket)) return;
                while (socket->bytesAvailable() > 0 && buffers.contains(socket)) {
                    auto &buffer = buffers[socket];
                    buffer += socket->read(64 * 1024);
                    QList<QJsonObject> messages;
                    QString error;
                    if (!takeIpcFrames(buffer, messages, &error) || buffer.size() > maxMessageBytes + 4) {
                        sendError(socket, {}, QStringLiteral("invalid_protocol"), error.isEmpty() ? QStringLiteral("Message exceeds limit") : error);
                        socket->disconnectFromServer();
                        return;
                    }
                    for (const auto &message : messages) dispatch(socket, message);
                }
            });
            QObject::connect(socket, &QLocalSocket::disconnected, q, [this, socket] {
                buffers.remove(socket);
                const auto ids = requests.keys();
                for (const auto &id : ids) if (requests.value(id).socket == socket) cancelRequest(id);
                socket->deleteLater();
            });
            QPointer<QLocalSocket> guarded(socket);
            QTimer::singleShot(10000, q, [this, guarded] {
                if (guarded && !guarded->property("authenticated").toBool()) guarded->disconnectFromServer();
            });
        }
    }

    void send(QLocalSocket *socket, QJsonObject object) {
        if (!socket || socket->state() != QLocalSocket::ConnectedState) return;
        object.insert(QStringLiteral("protocol"), protocolVersion);
        QString error;
        const QByteArray frame = encodeIpcFrame(object, &error);
        if (frame.isEmpty() || socket->bytesToWrite() + frame.size() > 2 * maxMessageBytes) { socket->abort(); return; }
        socket->write(frame);
    }
    void sendResult(QLocalSocket *socket, const QString &id, const QJsonObject &result) {
        send(socket, {{"id", id}, {"ok", true}, {"result", result}});
    }
    void sendError(QLocalSocket *socket, const QString &id, const QString &code, const QString &message) {
        send(socket, {{"id", id}, {"ok", false}, {"error", QJsonObject{{"code", code}, {"message", message}}}});
    }
    void event(const QString &name, const QJsonObject &data) {
        auto payload = data;
        if (name == "indexChanged") {
            QString error;
            const auto value = repository.browseGeneration(&error);
            if (error.isEmpty() && value >= 0) payload.insert(QStringLiteral("browseGeneration"), QString::number(value));
            payload.insert(QStringLiteral("visibilityEpoch"), QString::number(visibilityEpoch));
        }
        for (auto *socket : buffers.keys()) if (socket->property("authenticated").toBool())
            send(socket, {{"event", name}, {"data", payload}});
    }
    void publishProgress() { event(QStringLiteral("progress"), progress.toJson()); }

    QJsonObject runtimeStatus() const {
        return {{"runtime", latestRuntimeIsQuery ? queryRuntime : indexRuntime},
                {"runtimeWorkers", QJsonObject{{"index", indexRuntime}, {"query", queryRuntime}}}};
    }

    void recordRuntime(bool query, const QJsonObject &message) {
        const auto payload = message.value(message.value("ok").toBool() ? "result" : "error").toObject();
        auto diagnostics = payload.value("runtime").toObject();
        if (diagnostics.isEmpty()) return;
        const auto &cpuFallback = query ? queryCpuFallback : indexCpuFallback;
        if (!cpuFallback.isEmpty()) {
            diagnostics.insert("requestedBackend", "auto");
            diagnostics.insert("effectiveBackendRequest", "cpu");
            const auto reason = diagnostics.value("fallbackReason").toString();
            diagnostics.insert("fallbackReason", reason.isEmpty() ? cpuFallback : cpuFallback + '\n' + reason);
        }
        diagnostics.insert("workerRole", query ? "query" : "index");
        auto &previous = query ? queryRuntime : indexRuntime;
        if (previous == diagnostics && latestRuntimeIsQuery == query) return;
        previous = diagnostics;
        latestRuntimeIsQuery = query;
        log(QStringLiteral("Inference runtime: %1").arg(QString::fromUtf8(QJsonDocument(diagnostics).toJson(QJsonDocument::Compact))));
        event(QStringLiteral("runtime"), runtimeStatus());
    }

    void runtimeStopped(bool query) {
        auto &diagnostics = query ? queryRuntime : indexRuntime;
        if (diagnostics.isEmpty() || !diagnostics.value("loaded").toBool()) return;
        diagnostics.insert("loaded", false);
        event(QStringLiteral("runtime"), runtimeStatus());
    }

    bool strictGpu() const {
        return qEnvironmentVariable("QCOPILOTS_SEARCH_BACKEND").trimmed().compare("gpu", Qt::CaseInsensitive) == 0;
    }

    bool runtimeEvent(bool query, const QJsonObject &message) {
        if (message.value("event").toString() != "runtime") return false;
        const auto id = message.value("id").toString();
        if (id.isEmpty() || id != (query ? activeQuery : currentWorkerId)) return true;
        const auto data = message.value("data").toObject();
        const auto diagnostics = data.value("runtime").toObject();
        auto &gpuOperation = query ? queryGpuOperation : indexGpuOperation;
        const auto phase = data.value("phase").toString();
        if (phase == "inference-start") {
            const QString packaged = diagnostics.value("packagedBackend").toString();
            gpuOperation = diagnostics.value("requestedBackend").toString().trimmed().compare("auto", Qt::CaseInsensitive) == 0
                && !packaged.isEmpty() && packaged != "cpu"
                && (data.value("activity").toString() == "model-load"
                    || !diagnostics.value("loaded").toBool() || diagnostics.value("gpuSelected").toBool());
        } else if (phase == "inference-end") gpuOperation = false;
        else return true;
        recordRuntime(query, {{"ok", true}, {"result", QJsonObject{{"runtime", diagnostics}}}});
        return true;
    }

    bool retryGpuCrashOnCpu(bool query, int code, QProcess::ExitStatus status) {
        auto &fallback = query ? queryCpuFallback : indexCpuFallback;
        const bool gpuOperation = std::exchange(query ? queryGpuOperation : indexGpuOperation, false);
        if (!gpuOperation || strictGpu() || !fallback.isEmpty()
            || (status == QProcess::NormalExit && code == 0)) return false;
        if (query ? activeQuery.isEmpty() || !requests.contains(activeQuery)
                  : !current || currentWorkerId.isEmpty() || publishing) return false;
        fallback = QStringLiteral("The GPU worker exited during inference (exit %1, status %2). Retrying once on CPU.")
            .arg(code).arg(int(status));
        log(fallback);
        auto &diagnostics = query ? queryRuntime : indexRuntime;
        diagnostics.insert("loaded", false);
        diagnostics.insert("gpuSelected", false);
        diagnostics.insert("gpuSelfTestPassed", false);
        diagnostics.insert("textWarmupCompleted", false);
        diagnostics.insert("selectedBackend", "");
        diagnostics.insert("deviceName", "");
        diagnostics.insert("deviceDescription", "");
        diagnostics.insert("fallbackReason", fallback);
        diagnostics.insert("error", "");
        event(QStringLiteral("runtime"), runtimeStatus());
        if (query) {
            queryTimeout.stop();
            queryQueue.prepend(activeQuery);
            activeQuery.clear();
        } else {
            indexTimeout.stop();
            currentWorkerId.clear();
        }
        return true;
    }

    void dispatch(QLocalSocket *socket, const QJsonObject &message) {
        const QString id = message.value("id").toString();
        if (id.isEmpty() || id.size() > 128 || !message.value("method").isString()
            || (!message.value("params").isUndefined() && !message.value("params").isObject())) {
            sendError(socket, id, QStringLiteral("invalid_request"), QStringLiteral("A bounded request ID, method and object params are required")); return;
        }
        if (!constantEquals(message.value("token").toString().toLatin1(), token)) {
            sendError(socket, id, QStringLiteral("not_authenticated"), QStringLiteral("Profile authentication failed"));
            socket->disconnectFromServer(); return;
        }
        socket->setProperty("authenticated", true);
        const auto method = message.value("method").toString();
        int count = 0;
        for (const auto &pending : std::as_const(requests)) if (pending.socket == socket) {
            ++count;
            if (pending.id == id) { sendError(socket, id, QStringLiteral("duplicate_id"), QStringLiteral("Request ID is already in flight")); return; }
        }
        for (const auto &pending : std::as_const(activeSearches)) if (pending.socket == socket) ++count;
        if ((method == "search" || method == "files.list" || method == "document.preview")
            && (count >= 64 || requests.size() + activeSearches.size() >= 128)) {
            sendError(socket, id, QStringLiteral("busy"), QStringLiteral("Too many queued or running requests")); return;
        }
        const auto params = message.value("params").toObject();
        if (method == "status") {
            QString error;
            const auto status = repository.status(&error);
            sendResult(socket, id, {{"progress", progress.toJson()}, {"repository", status}, {"settings", settings.toJson()},
                {"modelAvailable", modelAvailable}, {"pipelineFingerprint", pipeline}, {"pipelineTransitionError", pipelineError},
                {"failures", failures}, {"warning", error},
                {"watcherMode", watcherMode}, {"watcherWarning", watcherWarning}, {"reconcileIntervalMs", reconcile.interval()},
                {"activeSearches", activeSearches.size()}, {"queuedQueries", queryQueue.size()},
                {"queuedFiles", jobs.size() + (current ? 1 : 0)}, {"catalogReady", catalogReady},
                {"runtime", latestRuntimeIsQuery ? queryRuntime : indexRuntime},
                {"runtimeWorkers", QJsonObject{{"index", indexRuntime}, {"query", queryRuntime}}}});
        } else if (method == "settings.get") sendResult(socket, id, settings.toJson());
        else if (method == "settings.set") {
            auto json = settings.toJson();
            for (auto it = params.begin(); it != params.end(); ++it) json.insert(it.key(), it.value());
            AppSettings next;
            QString error;
            if (!AppSettings::fromJson(json, &next, &error) || !next.save(paths.configFile, &error)) {
                sendError(socket, id, QStringLiteral("invalid_settings"), error); return;
            }
            const bool changed = next.fingerprint() != settings.fingerprint();
            previousRoots.append(settings.roots);
            previousRoots.removeDuplicates();
            previousCodeRoots.append(settings.codeRoots);
            previousCodeRoots.removeDuplicates();
            settings = next;
            if (changed) { ++visibilityEpoch; invalidateIndexing(); }
            updateWatchers();
            sendResult(socket, id, settings.toJson());
            event(QStringLiteral("settings"), settings.toJson());
            if (changed) startScan();
            else if (!settings.indexingPaused && current && currentWorkerId.isEmpty() && !publishing) dispatchIndex();
            else scheduleNext();
        } else if (method == "search") {
            if (params.value("code").toBool() && !settings.includeCode) {
                sendError(socket, id, QStringLiteral("code_search_disabled"), QStringLiteral("Enable code search in folder settings before querying the code index")); return;
            }
            if (!params.value("query").isString() || params.value("query").toString().size() > 8192
                || params.value("limit").toInt(100) < 1 || params.value("limit").toInt(100) > maximumSearchResults) {
                sendError(socket, id, QStringLiteral("invalid_request"), QStringLiteral("Search requires a query up to 8192 characters and limit 1–%1").arg(maximumSearchResults)); return;
            }
            if (!catalogReady) {
                sendError(socket, id, QStringLiteral("metadata_pending"), QStringLiteral("Folder metadata reconciliation has not completed successfully")); return;
            }
            if (strictGpu() && params.value("semantic").toBool(true)
                && !params.value("query").toString().trimmed().isEmpty() && !modelAvailable) {
                sendError(socket, id, QStringLiteral("gpu_unavailable"), QStringLiteral("Strict GPU search requires an available inference model")); return;
            }
            const QString internal = QUuid::createUuid().toString(QUuid::WithoutBraces);
            requests.insert(internal, {socket, id, params, generation});
            QTimer::singleShot(120000, q, [this, internal] {
                if (!requests.contains(internal)) return;
                finishError(internal, QStringLiteral("timeout"), QStringLiteral("Search request timed out"));
                if (activeQuery == internal) {
                    activeQuery.clear(); stopWorkerForRestart(true, ExitReason::Fault);
                }
            });
            if (modelAvailable && params.value("semantic").toBool(true) && !params.value("query").toString().trimmed().isEmpty()) {
                queryQueue.enqueue(internal); nextQuery();
            } else runSearch(internal, {}, pipelineError);
        } else if (method == "files.list") {
            if (params.value("code").toBool() && !settings.includeCode) {
                sendError(socket, id, QStringLiteral("code_search_disabled"), QStringLiteral("Enable code search before browsing the code index")); return;
            }
            if ((!params.value("cursor").isUndefined() && !params.value("cursor").isString())
                || params.value("cursor").toString().size() > 8192
                || params.value("limit").toInt(100) < 1 || params.value("limit").toInt(100) > 100) {
                sendError(socket, id, QStringLiteral("invalid_request"), QStringLiteral("File browsing requires a bounded cursor and limit 1–100")); return;
            }
            if (!catalogReady) {
                const bool oldCursor = !params.value("cursor").toString().isEmpty();
                sendError(socket, id, oldCursor ? QStringLiteral("cursor_stale") : QStringLiteral("metadata_pending"),
                    oldCursor ? QStringLiteral("Folder selection changed. Refresh the list.")
                              : QStringLiteral("Folder metadata reconciliation has not completed successfully")); return;
            }
            const QString internal = QUuid::createUuid().toString(QUuid::WithoutBraces);
            requests.insert(internal, {socket, id, params, generation});
            runFileList(internal);
        } else if (method == "document.preview") {
            startPreview(socket, id, params);
        } else if (method == "cancel") {
            QString target;
            for (auto it = requests.cbegin(); it != requests.cend(); ++it)
                if (it->socket == socket && it->id == params.value("id").toString()) { target = it.key(); break; }
            if (!target.isEmpty()) cancelRequest(target);
            sendResult(socket, id, {{"cancelled", !target.isEmpty()}});
        } else if (method == "index.pause" || method == "index.resume") {
            auto next = settings;
            next.indexingPaused = method == "index.pause";
            QString error;
            if (!next.save(paths.configFile, &error)) { sendError(socket, id, QStringLiteral("settings_error"), error); return; }
            settings = next;
            progress.state = settings.indexingPaused ? QStringLiteral("paused") : QStringLiteral("indexing");
            if (!settings.indexingPaused) {
                if (current && currentWorkerId.isEmpty() && !publishing) dispatchIndex();
                else if (jobs.isEmpty() && !current) startScan();
                else scheduleNext();
            }
            updateIdleWorkers();
            publishProgress(); sendResult(socket, id, progress.toJson());
        } else if (method == "index.start" || method == "index.reconcile" || method == "index.retry") {
            if (method == "index.retry") { failures = {}; retryIncomplete = true; }
            startScan(); sendResult(socket, id, progress.toJson());
        } else if (method == "models.reload") {
            if (publishing) { sendError(socket, id, QStringLiteral("busy"), QStringLiteral("Index publication is in progress, retry shortly")); return; }
            invalidateIndexing(false);
            for (const auto &key : requests.keys()) finishError(key, QStringLiteral("model_changed"), QStringLiteral("Model configuration changed"));
            activeQuery.clear(); queryQueue.clear();
            stopWorkerForRestart(true);
            QString error;
            if (!refreshModels(&error)) {
                progress.state = QStringLiteral("error"); progress.error = error; publishProgress();
                sendError(socket, id, QStringLiteral("pipeline_transition_failed"), error); return;
            }
            sendResult(socket, id, {{"modelAvailable", modelAvailable}, {"fingerprint", pipeline}});
            startScan();
        } else if (method == "shutdown") {
            sendResult(socket, id, {{"stopping", true}});
            QTimer::singleShot(100, QCoreApplication::instance(), &QCoreApplication::quit);
        } else sendError(socket, id, QStringLiteral("unknown_method"), QStringLiteral("Unknown method: %1").arg(method));
    }

    bool refreshModels(QString *error = nullptr) {
        indexRuntime = {}; queryRuntime = {}; latestRuntimeIsQuery = false;
        indexGpuOperation = queryGpuOperation = false;
        indexCpuFallback.clear(); queryCpuFallback.clear();
        if (started) event(QStringLiteral("runtime"), runtimeStatus());
        QString verificationError;
        ModelFiles models(paths.modelDir);
        const bool available = modelVerifier ? modelVerifier(&verificationError) : models.verify(&verificationError);
        const QString candidate = pipelineVersion(available);
        QString transitionError;
        if (!repository.setPipelineFingerprint(candidate, &transitionError)) {
            modelAvailable = false;
            pipelineReady = false;
            pipelineError = QStringLiteral("Cannot transition the index pipeline: %1").arg(transitionError);
            if (error) *error = pipelineError;
            log(pipelineError);
            return false;
        }
        // Only a committed SQLite transition may publish a new inference space.
        modelAvailable = available;
        pipeline = candidate;
        pipelineReady = true;
        pipelineError.clear();
        if (error) error->clear();
        if (!modelAvailable) log(QStringLiteral("Keyword mode: %1").arg(verificationError));
        return true;
    }

    void updateWatchers(QStringList directories = {}) {
        if (!monitor.directories().isEmpty()) monitor.removePaths(monitor.directories());
        watcherWarning.clear();
        if (directories.isEmpty()) {
            auto roots = settings.roots;
            if (settings.includeCode) roots.append(settings.codeRoots);
            for (const auto &root : roots) if (QFileInfo(root).isDir()) directories.append(root);
        }
        directories.removeDuplicates();
        constexpr qsizetype watchLimit = 8192;
        if (directories.size() > watchLimit) {
            watcherWarning = QStringLiteral("Directory watch limit reached, additional directories use periodic reconciliation");
            directories = directories.first(watchLimit);
        }
        if (!directories.isEmpty()) {
            const auto unavailable = monitor.addPaths(directories);
            if (!unavailable.isEmpty()) watcherWarning = QStringLiteral("Some directory watches are unavailable, periodic reconciliation is active");
        }
        watcherMode = watcherWarning.isEmpty() ? QStringLiteral("recursive-and-periodic") : QStringLiteral("partial-watch-and-periodic");
        if (!watcherWarning.isEmpty()) log(watcherWarning);
    }

    void invalidateIndexing(bool invalidateCatalog = true) {
        { const std::lock_guard guard(publicationConfigurationMutex); ++generation; }
        scanCancelled.store(true);
        jobs.clear();
        recentJobs.clear(); backfillJobs.clear(); continuationJobs.clear();
        pendingCatalog.reset(); knownVersions.clear(); completedFiles.clear(); failedFiles.clear();
        if (invalidateCatalog) { catalogReady = false; catalogRoots.clear(); catalogCodeRoots.clear(); catalogFiles.clear(); }
        firstScan = true;
        if (!currentWorkerId.isEmpty()) indexWorker.write(QJsonDocument(QJsonObject{{"id", "cancel"}, {"operation", "cancel"},
            {"targetId", currentWorkerId}}).toJson(QJsonDocument::Compact) + '\n');
        if (!publishing) current.reset();
        currentWorkerId.clear(); indexTimeout.stop();
        stopWorkerForRestart(false);
        pendingScan = true;
    }

    void refreshProgressCounts() {
        progress.total = knownVersions.size();
        progress.completed = completedFiles.size();
        progress.failed = failedFiles.size();
    }

    void protectForScan(const Job &job) {
        if (!scanning && !pendingCatalog) return;
        (job.file.kind == "code" ? scanProtectedCode : scanProtectedGeneral).insert(job.file.path);
    }

    enum class QueueClass { Recent, Backfill, Continuation };
    void enqueue(Job job, QueueClass queue) {
        const QString key = fileKey(job.file);
        job.segments = {};
        if (auto found = jobs.find(key); found != jobs.end()) {
            if (found->indexedVersion == job.indexedVersion) {
                found->retry = found->retry || job.retry; return;
            }
        }
        protectForScan(job);
        job.queueToken = ++nextQueueToken;
        const auto token = job.queueToken;
        jobs.insert(key, std::move(job));
        (queue == QueueClass::Recent ? recentJobs : queue == QueueClass::Backfill ? backfillJobs : continuationJobs).enqueue({key, token});
    }

    void mergeCatalog() {
        if (!pendingCatalog || current || publishing || stopping) return;
        auto result = std::move(pendingCatalog);
        const quint64 epoch = result->generation;
        if (epoch != generation) { scheduleNext(); return; }
        updateWatchers(result->scan.directories);
        progress.error = result->scan.errors.join(u'\n');
        QSet<QString> generalPresent, codePresent, presentKeys;
        QVector<FileMetadata> metadata;
        QList<Job> added;
        const bool retryCapped = std::exchange(retryIncomplete, false);
        for (const auto &file : result->scan.files) {
            const QString key = fileKey(file);
            const QString version = file.fingerprint + u'|' + pipeline;
            const QString stored = result->versions.value(key);
            (file.kind == "code" ? codePresent : generalPresent).insert(file.path);
            presentKeys.insert(key);
            metadata.append({file.path, file.kind == "code", file.modifiedMs, file.kind, file.fingerprint, stored});
            const bool unchanged = knownVersions.value(key) == version;
            knownVersions.insert(key, version);
            if (!unchanged) { completedFiles.remove(key); failedFiles.remove(key); }
            if (unchanged && completedFiles.contains(key) && !retryCapped) continue;
            if (!retryCapped && (stored == version + QStringLiteral("|capped:5000")
                || stored == version + QStringLiteral("|partial:5000"))) {
                const QString message = QStringLiteral("Index limit reached: the first 5000 segments remain searchable. Change the file or explicitly retry to rebuild it.");
                failures.insert(failureKey(file), message); failedFiles.insert(key); progress.error = message;
                continue;
            }
            if (stored == version && !failures.contains(failureKey(file))) {
                completedFiles.insert(key); failedFiles.remove(key); continue;
            }
            if (jobs.contains(key) && jobs.value(key).indexedVersion == version && !retryCapped) continue;
            Job job{file, sourceVersion(file.path), version, epoch};
            job.retry = retryCapped && (stored == version + QStringLiteral("|capped:5000")
                || stored == version + QStringLiteral("|partial:5000"));
            added.append(std::move(job));
        }
        std::stable_sort(added.begin(), added.end(), [](const Job &a, const Job &b) {
            if (a.file.screenshot != b.file.screenshot) return a.file.screenshot;
            if (a.file.modifiedMs != b.file.modifiedMs) return a.file.modifiedMs > b.file.modifiedMs;
            return a.file.path < b.file.path;
        });
        const auto lane = !firstScan && added.size() <= 50 ? QueueClass::Recent : QueueClass::Backfill;
        firstScan = false;
        for (auto &job : added) enqueue(std::move(job), lane);
        // A successful root scan is authoritative about eligibility. In
        // particular, an unchanged file newly ignored by Git must not retain
        // an old queued extraction. Unavailable-root work is rediscovered on
        // recovery, with its committed partial cursor still in SQLite.
        for (auto it = jobs.begin(); it != jobs.end();) {
            if (!presentKeys.contains(it.key())) it = jobs.erase(it);
            else ++it;
        }
        for (auto *queue : {&recentJobs, &backfillJobs, &continuationJobs}) {
            for (auto it = queue->begin(); it != queue->end();) {
                const auto found = jobs.constFind(it->key);
                if (found == jobs.cend() || found->queueToken != it->token) it = queue->erase(it);
                else ++it;
            }
        }
        // The scan may predate a completed publication or a newly observed job.
        // Protect these paths for this scan only, then let the next scan settle them.
        generalPresent.unite(scanProtectedGeneral); codePresent.unite(scanProtectedCode);
        scanProtectedGeneral.clear(); scanProtectedCode.clear();
        refreshProgressCounts();
        const bool completeScan = result->scan.errors.isEmpty();
        for (auto it = knownVersions.begin(); it != knownVersions.end();) {
            if (!presentKeys.contains(it.key()) && !jobs.contains(it.key())) {
                completedFiles.remove(it.key()); failedFiles.remove(it.key()); it = knownVersions.erase(it);
            } else ++it;
        }
        for (const auto &key : failures.keys()) {
            const bool code = key.startsWith(QStringLiteral("code:"));
            const auto path = code ? key.mid(5) : key;
            const auto &successfulRoots = code ? result->codeRoots : result->generalRoots;
            const bool scanned = std::any_of(successfulRoots.cbegin(), successfulRoots.cend(), [&path](const QString &root) {
                return isPathWithin(path, root);
            });
            if ((!code || settings.includeCode) && (completeScan || scanned)
                && !(code ? codePresent : generalPresent).contains(path)) failures.remove(key);
        }
        const auto generalRoots = completeScan ? previousRoots : result->generalRoots;
        const auto codeRoots = completeScan ? previousCodeRoots : result->codeRoots;
        auto visibleGeneral = result->generalRoots;
        auto visibleCode = result->codeRoots;
        visibleGeneral.removeDuplicates(); visibleGeneral.sort();
        visibleCode.removeDuplicates(); visibleCode.sort();
        const bool includeCode = settings.includeCode;
        publishing = true;
        auto *writer = new QFutureWatcher<QString>(q);
        QObject::connect(writer, &QFutureWatcher<QString>::finished, q, [this, writer, epoch, includeCode, completeScan,
                                                                        visibleGeneral, visibleCode, presentKeys] {
            const QString error = writer->result(); writer->deleteLater(); publishing = false;
            if (stopping) return;
            if (!error.isEmpty()) { progress.error = error; log(error); }
            else if (epoch == generation) {
                if (completeScan) {
                    previousRoots = settings.roots;
                    if (includeCode) previousCodeRoots = settings.codeRoots;
                }
                if (catalogRoots != visibleGeneral || catalogCodeRoots != visibleCode || catalogFiles != presentKeys) ++visibilityEpoch;
                catalogRoots = visibleGeneral; catalogCodeRoots = visibleCode; catalogFiles = presentKeys;
                catalogReady = !catalogRoots.isEmpty() || !catalogCodeRoots.isEmpty()
                    || (settings.roots.isEmpty() && (!settings.includeCode || settings.codeRoots.isEmpty()));
            }
            refreshProgressCounts(); event(QStringLiteral("indexChanged"), {}); scheduleNext();
        });
        writer->setFuture(QtConcurrent::run(&writePool, [this, generalRoots, codeRoots, generalPresent, codePresent,
                                                        metadata = std::move(metadata), epoch, includeCode] {
            QString error;
            const std::lock_guard guard(publicationConfigurationMutex);
            if (epoch != generation) return QString{};
            if (!repository.updateFileMetadata(metadata, &error)) return error;
            for (const auto &root : generalRoots) if (!repository.prune(root, generalPresent, false, &error)) return error;
            if (includeCode) for (const auto &root : codeRoots) if (!repository.prune(root, codePresent, true, &error)) return error;
            return error;
        }));
        publishProgress();
    }

    void startScan() {
        if (stopping) return;
        if (!pipelineReady) { progress.state = QStringLiteral("error"); progress.error = pipelineError; publishProgress(); return; }
        // Catalog reconciliation is metadata-only and remains available while
        // extraction is paused. One pending result bounds memory and merge work.
        if (scanning || pendingCatalog) { pendingScan = true; return; }
        scanning = true; pendingScan = false; scanCancelled.store(false);
        const quint64 epoch = generation;
        FolderRules general{settings.roots, settings.excludes, false};
        FolderRules code{settings.codeRoots, settings.codeExcludes, true};
        code.scope = FolderRules::Scope::Code;
        const bool includeCode = settings.includeCode;
        scanProtectedGeneral.clear(); scanProtectedCode.clear();
        for (const auto &job : std::as_const(jobs)) protectForScan(job);
        if (current) protectForScan(*current);
        if (!current && jobs.isEmpty()) progress.state = QStringLiteral("scanning");
        publishProgress();
        auto *watcher = new QFutureWatcher<ScanPackage>(q);
        QObject::connect(watcher, &QFutureWatcher<ScanPackage>::finished, q, [this, watcher, epoch] {
            auto result = watcher->result(); watcher->deleteLater(); scanning = false;
            if (stopping) return;
            if (epoch != generation || result.scan.cancelled) { startScan(); return; }
            pendingCatalog = std::make_unique<ScanPackage>(std::move(result));
            mergeCatalog();
        });
        watcher->setFuture(QtConcurrent::run(&scanPool, [this, general, code, includeCode, epoch] {
            ScanPackage result; result.generation = epoch;
            QSet<QString> seen;
            const auto scanSpace = [this, &result, &seen](const FolderRules &rules, QStringList &successfulRoots) {
                for (const auto &root : rules.roots) {
                    if (scanCancelled.load()) { result.scan.cancelled = true; return; }
                    auto selected = rules; selected.roots = {root};
                    const auto source = FileCatalog::scan(selected, &scanCancelled);
                    result.scan.directories.append(source.directories); result.scan.errors.append(source.errors);
                    if (source.cancelled) { result.scan.cancelled = true; return; }
                    if (!source.errors.isEmpty()) continue;
                    successfulRoots.append(root);
                    for (const auto &file : source.files) {
                        const auto key = fileKey(file);
                        if (!seen.contains(key)) { seen.insert(key); result.scan.files.append(file); }
                    }
                }
            };
            scanSpace(general, result.generalRoots);
            if (includeCode && !result.scan.cancelled) scanSpace(code, result.codeRoots);
            for (const auto &file : result.scan.files) {
                if (scanCancelled.load()) { result.scan.cancelled = true; break; }
                result.versions.insert(fileKey(file), repository.fileVersion(file.path, file.kind == "code"));
            }
            return result;
        }));
    }

    bool workerIdle(bool query) const {
        if (query) {
            if (!activeQuery.isEmpty()) return false;
            for (const auto &id : queryQueue) if (requests.contains(id)) return false;
            return true;
        }
        return currentWorkerId.isEmpty() && (!publishing || !current)
            && (settings.indexingPaused || (!current && jobs.isEmpty()));
    }

    void updateIdleWorkers() {
        if (stopping) return;
        for (const bool query : {false, true}) {
            auto &worker = query ? queryWorker : indexWorker;
            auto &timer = query ? queryIdle : indexIdle;
            const auto reason = query ? queryExit : indexExit;
            if (worker.state() != QProcess::Running || reason != ExitReason::None || !workerIdle(query)) {
                timer.stop(); continue;
            }
            // Repeated empty scans must not extend the existing idle deadline.
            if (!timer.isActive()) timer.start(query ? queryIdleMs : indexIdleMs);
        }
    }

    void setupIdle(bool query) {
        auto &idle = query ? queryIdle : indexIdle;
        auto &grace = query ? queryShutdownGrace : indexShutdownGrace;
        idle.setSingleShot(true); grace.setSingleShot(true);
        QObject::connect(&idle, &QTimer::timeout, q, [this, query] {
            auto &worker = query ? queryWorker : indexWorker;
            auto &reason = query ? queryExit : indexExit;
            if (stopping || reason != ExitReason::None || worker.state() != QProcess::Running || !workerIdle(query)) return;
            reason = ExitReason::Idle;
            (query ? queryRestartPending : indexRestartPending) = true;
            worker.write("{\"id\":\"shutdown\",\"operation\":\"shutdown\"}\n");
            worker.closeWriteChannel();
            (query ? queryShutdownGrace : indexShutdownGrace).start(shutdownGraceMs);
        });
        QObject::connect(&grace, &QTimer::timeout, q, [this, query] {
            auto &worker = query ? queryWorker : indexWorker;
            if ((query ? queryExit : indexExit) == ExitReason::Idle && worker.state() != QProcess::NotRunning)
                worker.kill();
        });
    }

    void stopWorkerForRestart(bool query, ExitReason reason = ExitReason::Restart) {
        auto &worker = query ? queryWorker : indexWorker;
        (query ? queryIdle : indexIdle).stop();
        (query ? queryShutdownGrace : indexShutdownGrace).stop();
        (query ? queryExit : indexExit) = reason;
        (query ? queryRestartPending : indexRestartPending) = worker.state() != QProcess::NotRunning;
        if (worker.state() != QProcess::NotRunning) worker.kill();
        else (query ? queryExit : indexExit) = ExitReason::None;
    }

    void setupWorker(QProcess &worker, bool query) {
        worker.setProcessChannelMode(QProcess::SeparateChannels);
        QObject::connect(&worker, &QProcess::readyReadStandardOutput, q, [this, &worker, query] {
            QByteArray &buffer = query ? queryBuffer : indexBuffer;
            buffer += worker.readAllStandardOutput();
            if (buffer.size() > maxMessageBytes) {
                workerFailure(query, QStringLiteral("Worker response exceeds protocol limit"));
                stopWorkerForRestart(query, ExitReason::Fault); return;
            }
            while (true) {
                const qsizetype line = buffer.indexOf('\n');
                if (line < 0) break;
                const QByteArray bytes = buffer.left(line); buffer.remove(0, line + 1);
                QJsonParseError parse;
                const auto document = QJsonDocument::fromJson(bytes, &parse);
                if (parse.error != QJsonParseError::NoError || !document.isObject()) {
                    workerFailure(query, QStringLiteral("Malformed worker response"));
                    stopWorkerForRestart(query, ExitReason::Fault); return;
                }
                if (runtimeEvent(query, document.object())) continue;
                if (query) receiveQuery(document.object()); else receiveIndex(document.object());
            }
        });
        QObject::connect(&worker, &QProcess::readyReadStandardError, q, [this, &worker] {
            const auto text = QString::fromUtf8(worker.readAllStandardError()).trimmed();
            if (!text.isEmpty()) log(text.left(16384));
        });
        QObject::connect(&worker, &QProcess::errorOccurred, q, [this, query](QProcess::ProcessError error) {
            if (error != QProcess::FailedToStart) return;
            (query ? queryIdle : indexIdle).stop();
            (query ? queryShutdownGrace : indexShutdownGrace).stop();
            (query ? queryRestartPending : indexRestartPending) = false;
            (query ? queryExit : indexExit) = ExitReason::None;
            // ensureWorker's caller owns the one failed-start reply/publication.
        });
        QObject::connect(&worker, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), q,
            [this, query](int code, QProcess::ExitStatus status) {
                runtimeStopped(query);
                if (stopping) return;
                (query ? queryIdle : indexIdle).stop();
                (query ? queryShutdownGrace : indexShutdownGrace).stop();
                auto &reason = query ? queryExit : indexExit;
                const auto previousReason = std::exchange(reason, ExitReason::None);
                if (query) queryRestartPending = false; else indexRestartPending = false;
                if (query) queryBuffer.clear(); else indexBuffer.clear();
                if (previousReason == ExitReason::None && !retryGpuCrashOnCpu(query, code, status))
                    workerFailure(query, QStringLiteral("Worker exited (%1, %2)").arg(code).arg(int(status)));
                QTimer::singleShot(0, q, [this, query] {
                    if (query) nextQuery();
                    else if (current && currentWorkerId.isEmpty() && !publishing) dispatchIndex();
                    else scheduleNext();
                });
            });
    }

    bool ensureWorker(QProcess &worker, bool query) {
        if ((query ? queryExit : indexExit) != ExitReason::None) return false;
        if (worker.state() == QProcess::Running) return true;
        if (worker.state() == QProcess::Starting) return false;
        worker.setProgram(executablePath(QStringLiteral("qcs-worker")));
        worker.setArguments({"--profile-dir", paths.profileDir, "--model-dir", paths.modelDir,
                             "--mode", query ? "query" : (modelAvailable ? "full" : "extract"), "--threads", query ? "4" : "2"});
        worker.setProcessEnvironment(QProcessEnvironment::systemEnvironment());
        if (configureWorker) configureWorker(worker, query);
        if (!(query ? queryCpuFallback : indexCpuFallback).isEmpty()) {
            auto environment = worker.processEnvironment();
            if (environment.inheritsFromParent()) environment = QProcessEnvironment::systemEnvironment();
            environment.insert("QCOPILOTS_SEARCH_BACKEND", "cpu");
            worker.setProcessEnvironment(environment);
        }
        (query ? queryGpuOperation : indexGpuOperation) = false;
        worker.start();
        // Process startup is bounded. File decoding and inference are always asynchronous.
        return worker.waitForStarted(3000);
    }

    void scheduleNext() {
        if (stopping || publishing || current) { updateIdleWorkers(); return; }
        if (pendingCatalog) { mergeCatalog(); return; }
        if (pendingScan && !scanning) startScan();
        if (!pipelineReady) { progress.state = QStringLiteral("error"); progress.error = pipelineError; publishProgress(); return; }
        if (indexExit != ExitReason::None || (indexRestartPending && indexWorker.state() != QProcess::NotRunning)) return;
        if (settings.indexingPaused) { progress.state = QStringLiteral("paused"); publishProgress(); updateIdleWorkers(); return; }
        if (jobs.isEmpty()) {
            progress.state = scanning ? QStringLiteral("scanning")
                : progress.failed > 0 || !progress.error.isEmpty() ? QStringLiteral("partial") : QStringLiteral("idle");
            progress.currentPath.clear(); publishProgress(); saveJournal();
            updateIdleWorkers();
            return;
        }
        // Four recent batches, one bulk first pass and one continuation. Empty
        // classes are skipped, retaining a finite batch bound for every class.
        QString key;
        for (int attempts = 0; attempts < 6 && key.isEmpty(); ++attempts) {
            auto &queue = queueTurn < 4 ? recentJobs : queueTurn == 4 ? backfillJobs : continuationJobs;
            queueTurn = (queueTurn + 1) % 6;
            while (!queue.isEmpty()) {
                const auto candidate = queue.dequeue();
                const auto found = jobs.constFind(candidate.key);
                if (found != jobs.cend() && found->queueToken == candidate.token) { key = candidate.key; break; }
            }
        }
        if (key.isEmpty()) return;
        current = std::make_unique<Job>(jobs.take(key));
        if (current->generation != generation) { current.reset(); QTimer::singleShot(0, q, [this] { scheduleNext(); }); return; }
        QString error;
        const auto stored = repository.fileVersion(current->file.path, current->file.kind == "code", &error);
        if (!error.isEmpty()) { failCurrent(error); return; }
        if (!current->retry && stored == current->indexedVersion && !failures.contains(failureKey(current->file))) {
            completedFiles.insert(key); failedFiles.remove(key); refreshProgressCounts(); current.reset();
            QTimer::singleShot(0, q, [this] { scheduleNext(); }); return;
        }
        if (!current->retry && (stored == current->indexedVersion + QStringLiteral("|capped:5000")
            || stored == current->indexedVersion + QStringLiteral("|partial:5000"))) {
            failCurrent(QStringLiteral("Index limit reached: the first 5000 segments remain searchable. Change the file or explicitly retry to rebuild it.")); return;
        }
        current->offset = 0;
        if (!current->retry) {
            const auto restoreError = restoreContinuation(*current, stored);
            if (!restoreError.isEmpty()) { failCurrent(restoreError); return; }
        }
        current->retry = false;
        current->batch = ++nextBatch;
        progress.currentPath = current->file.path; progress.state = QStringLiteral("indexing"); publishProgress();
        dispatchIndex();
    }

    void dispatchIndex() {
        if (!current || stopping || settings.indexingPaused || !pipelineReady) { updateIdleWorkers(); return; }
        if (publishing || !currentWorkerId.isEmpty() || indexExit != ExitReason::None
            || (indexRestartPending && indexWorker.state() != QProcess::NotRunning)) return;
        // A paused job can outlive its source version. Reconcile now instead of
        // sending a knowingly stale request and waiting for the periodic scan.
        if (!currentSourceMatches()) { rescanStaleCurrent(); return; }
        if (!ensureWorker(indexWorker, false)) { publishFailedCurrent(QStringLiteral("Cannot start index worker")); return; }
        indexIdle.stop();
        currentWorkerId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        const QJsonObject request{{"id", currentWorkerId}, {"operation", "extract"}, {"path", current->file.path},
            {"expectedVersion", current->workerVersion}, {"code", current->file.kind == "code"}, {"embed", modelAvailable},
            {"offset", current->offset}, {"maxSegments", 40}, {"pipelineFingerprint", pipeline}, {"runtimeEvents", true}};
        indexWorker.write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
        indexTimeout.start(10 * 60 * 1000);
    }

    void receiveIndex(const QJsonObject &message) {
        if (!current || message.value("id").toString() != currentWorkerId) return;
        indexGpuOperation = false;
        recordRuntime(false, message);
        indexTimeout.stop(); currentWorkerId.clear();
        if (!message.value("ok").toBool()) {
            const auto error = message.value("error").toObject();
            if (error.value("code").toString() == "stale_source" || !currentSourceMatches()) {
                rescanStaleCurrent(); return;
            }
            publishFailedCurrent(error.value("message").toString("Worker failed")); return;
        }
        const auto result = message.value("result").toObject();
        if (modelAvailable && strictGpu() && (!indexRuntime.value("gpuSelected").toBool()
            || !indexRuntime.value("loaded").toBool() || !indexRuntime.value("gpuSelfTestPassed").toBool()
            || !indexRuntime.value("textWarmupCompleted").toBool())) {
            publishFailedCurrent(QStringLiteral("Strict GPU inference did not confirm a loaded GPU backend")); return;
        }
        if (result.value("extractionFingerprint").toString() != extractionPipelineFingerprint()
            || (modelAvailable && result.value("modelFingerprint").toString() != embeddingPipelineFingerprint())) {
            failCurrent(QStringLiteral("Worker pipeline fingerprint differs from the service build")); return;
        }
        if (!currentSourceMatches()
            || result.value("sourceVersion").toString() != current->workerVersion) {
            rescanStaleCurrent(); return;
        }
        const auto state = result.value("state").toString();
        if (state == "cancelled") { failCurrent(result.value("error").toString(state)); return; }
        if (state == "failed" || state == "unsupported") { publishFailedCurrent(result.value("error").toString(state)); return; }
        const qsizetype previousCount = current->segments.size();
        for (const auto &value : result.value("segments").toArray()) {
            auto segment = Segment::fromJson(value.toObject());
            segment.path = current->file.path;
            segment.mediaPath.clear(); // Worker temporary files are never persisted as preview paths.
            current->segments.append(std::move(segment));
        }
        const bool exceedsLimit = current->segments.size() > 5000;
        if (exceedsLimit) current->segments.resize(5000);
        const bool complete = !exceedsLimit && result.value("complete").toBool(false) && state == "complete";
        const int next = result.value("next").toInt(current->offset);
        const QString extractionError = result.value("error").toString();
        const bool capped = !complete && (next >= 5000 || current->segments.size() >= 5000);
        const bool resume = !complete && !capped && next > current->offset && extractionError.isEmpty();
        current->offset = next;
        // Empty documents remain searchable by filename without inventing semantic vectors.
        if (current->segments.isEmpty() && complete) {
            Segment segment; segment.path = current->file.path; segment.title = QFileInfo(segment.path).fileName();
            segment.kind = current->file.kind; segment.modality = QStringLiteral("text"); current->segments.append(segment);
        }
        const Job job = *current;
        const QString version = job.indexedVersion + (complete ? QString{}
            : capped ? QStringLiteral("|capped:5000")
            : (resume ? QStringLiteral("|partial:%1") : QStringLiteral("|failed:%1")).arg(next));
        publishing = true;
        auto *watcher = new QFutureWatcher<QString>(q);
        QObject::connect(watcher, &QFutureWatcher<QString>::finished, q, [this, watcher, job, complete, capped, resume, extractionError, previousCount] {
            const auto error = watcher->result(); watcher->deleteLater(); publishing = false;
            if (stopping) return;
            if (!current || current->batch != job.batch || job.generation != generation) {
                if (current && current->batch == job.batch) current.reset();
                scheduleNext(); return;
            }
            // The source can change after decoding or while the writer waits
            // to enter its publication gate. Such a rejection requires work
            // for the new version, never a failed placeholder for the old one.
            if (!currentSourceMatches()) { rescanStaleCurrent(); return; }
            if (!error.isEmpty()) { failCurrent(error); return; }
            progress.segments += current->segments.size() - previousCount;
            protectForScan(job);
            event(QStringLiteral("indexChanged"), {{"path", job.file.path}});
            saveJournal();
            if (complete) {
                failures.remove(failureKey(job.file)); completedFiles.insert(fileKey(job.file));
                failedFiles.remove(fileKey(job.file)); refreshProgressCounts(); current.reset();
                scheduleNext();
            } else if (capped) {
                failCurrent(QStringLiteral("Index limit reached: the first 5000 segments remain searchable. Change the file or explicitly retry to rebuild it."));
            } else if (resume) {
                Job continuation = std::move(*current); current.reset();
                enqueue(std::move(continuation), QueueClass::Continuation);
                publishProgress();
                QTimer::singleShot(0, q, [this] { scheduleNext(); });
            } else failCurrent(extractionError.isEmpty() ? QStringLiteral("Worker returned a partial result without a continuation") : extractionError);
        });
        watcher->setFuture(QtConcurrent::run(&writePool, [this, job, version] {
            QString error;
            // The lease is held only across the SQLite transaction. ANN rebuilding
            // happens after its release, so configuration and IPC remain responsive.
            const auto gate = [this, job]() -> std::shared_ptr<void> {
                auto lease = std::make_shared<std::unique_lock<std::mutex>>(publicationConfigurationMutex);
                FileRecord now;
                if (job.generation != generation || !FileCatalog::describe(job.file.path, &now)
                    || now.fingerprint != job.file.fingerprint) return {};
                return lease;
            };
            if (!repository.replaceFile(job.file.path, version, job.segments, job.file.kind == "code", &error, gate,
                                        job.file.modifiedMs, job.file.kind)) return error;
            return QString{};
        }));
    }

    void publishFailedCurrent(const QString &failure) {
        if (!current || publishing) return;
        indexTimeout.stop(); currentWorkerId.clear();
        if (!currentSourceMatches()) { rescanStaleCurrent(); return; }
        const Job job = *current;
        publishing = true;
        auto *watcher = new QFutureWatcher<QString>(q);
        QObject::connect(watcher, &QFutureWatcher<QString>::finished, q, [this, watcher, job, failure] {
            const QString error = watcher->result(); watcher->deleteLater(); publishing = false;
            if (stopping) return;
            if (!current || current->batch != job.batch || job.generation != generation) {
                if (current && current->batch == job.batch) current.reset();
                scheduleNext(); return;
            }
            if (!currentSourceMatches()) { rescanStaleCurrent(); return; }
            if (error.isEmpty()) { protectForScan(job); event(QStringLiteral("indexChanged"), {{"path", job.file.path}}); }
            failCurrent(error.isEmpty() ? failure : failure + QStringLiteral("\nCannot preserve filename index: ") + error);
        });
        watcher->setFuture(QtConcurrent::run(&writePool, [this, job] {
            QString error;
            auto segments = job.segments;
            if (segments.isEmpty()) {
                const auto stored = repository.fileVersion(job.file.path, job.file.kind == "code", &error);
                if (!error.isEmpty()) return error;
                if (stored == job.indexedVersion || stored.startsWith(job.indexedVersion + u'|')) {
                    segments = repository.fileSegments(job.file.path, job.file.kind == "code", &error);
                    if (!error.isEmpty()) return error;
                }
            }
            if (segments.isEmpty()) {
                Segment metadata;
                metadata.path = job.file.path; metadata.title = QFileInfo(job.file.path).fileName(); metadata.kind = job.file.kind;
                // File identity is useful even when decoding fails. No body,
                // semantic vector or generated preview is claimed for this row.
                segments.append(std::move(metadata));
            }
            const auto gate = [this, job]() -> std::shared_ptr<void> {
                auto lease = std::make_shared<std::unique_lock<std::mutex>>(publicationConfigurationMutex);
                FileRecord now;
                if (job.generation != generation || !FileCatalog::describe(job.file.path, &now)
                    || now.fingerprint != job.file.fingerprint) return {};
                return lease;
            };
            repository.replaceFile(job.file.path, job.indexedVersion + QStringLiteral("|failed:%1").arg(job.offset),
                segments, job.file.kind == "code", &error, gate, job.file.modifiedMs, job.file.kind);
            return error;
        }));
    }

    bool currentSourceMatches() const {
        if (!current || current->generation != generation) return false;
        if (!pathAllowed(current->file.path, current->file.kind == "code")) return false;
        FileRecord now;
        return FileCatalog::describe(current->file.path, &now)
            && now.fingerprint == current->file.fingerprint
            && sourceVersion(current->file.path) == current->workerVersion;
    }

    void rescanStaleCurrent() {
        indexTimeout.stop(); currentWorkerId.clear();
        if (current) log(QStringLiteral("Source changed, rescheduling index: %1").arg(current->file.path));
        current.reset();
        // Let already queued files make progress before retrying this source.
        // Re-sorting the whole queue here can starve every later path when the
        // first file keeps changing. An empty queue triggers the pending scan.
        pendingScan = true;
        QTimer::singleShot(0, q, [this] { scheduleNext(); });
    }

    void failCurrent(const QString &error) {
        indexTimeout.stop(); currentWorkerId.clear();
        if (current) {
            failures.insert(failureKey(current->file), error);
            failedFiles.insert(fileKey(current->file)); completedFiles.remove(fileKey(current->file));
            refreshProgressCounts();
            log(QStringLiteral("Index failed %1: %2").arg(current->file.path, error));
        }
        progress.error = error; current.reset(); saveJournal(); publishProgress();
        QTimer::singleShot(0, q, [this] { scheduleNext(); });
    }

    void workerFailure(bool query, const QString &error) {
        if (query) {
            queryTimeout.stop();
            const auto id = activeQuery; activeQuery.clear();
            if (!id.isEmpty()) completeQuery(id, {}, error);
        } else if (current && !publishing && !currentWorkerId.isEmpty()) publishFailedCurrent(error);
    }

    void nextQuery() {
        if (stopping || !activeQuery.isEmpty()) return;
        if (queryExit != ExitReason::None || (queryRestartPending && queryWorker.state() != QProcess::NotRunning)) return;
        while (!queryQueue.isEmpty() && !requests.contains(queryQueue.head())) queryQueue.dequeue();
        if (queryQueue.isEmpty()) { updateIdleWorkers(); return; }
        activeQuery = queryQueue.dequeue();
        if (!ensureWorker(queryWorker, true)) {
            const QString id = activeQuery; activeQuery.clear(); completeQuery(id, {}, QStringLiteral("Query encoder unavailable"));
            QTimer::singleShot(0, q, [this] { nextQuery(); }); return;
        }
        const auto params = requests.value(activeQuery).params;
        queryIdle.stop();
        const QJsonObject request{{"id", activeQuery}, {"operation", "query"}, {"query", params.value("query")},
            {"code", params.value("code").toBool()}, {"runtimeEvents", true}};
        queryWorker.write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
        queryTimeout.start(90000);
    }

    void receiveQuery(const QJsonObject &message) {
        if (activeQuery.isEmpty() || message.value("id").toString() != activeQuery) return;
        queryGpuOperation = false;
        recordRuntime(true, message);
        queryTimeout.stop(); const auto id = activeQuery; activeQuery.clear();
        QVector<float> embedding;
        QString warning;
        if (message.value("ok").toBool()) {
            const auto result = message.value("result").toObject();
            if (result.value("modelFingerprint").toString() != embeddingPipelineFingerprint())
                warning = QStringLiteral("Query encoder pipeline fingerprint differs from the service build");
            else for (const auto &value : result.value("embedding").toArray()) embedding.append(float(value.toDouble()));
            if (embedding.size() != embeddingDimensions) { embedding.clear(); warning = QStringLiteral("Query encoder returned an invalid vector"); }
            if (strictGpu() && (!queryRuntime.value("gpuSelected").toBool() || !queryRuntime.value("loaded").toBool()
                || !queryRuntime.value("gpuSelfTestPassed").toBool() || !queryRuntime.value("textWarmupCompleted").toBool())) {
                embedding.clear(); warning = QStringLiteral("Strict GPU inference did not confirm a loaded GPU backend");
            }
        } else warning = message.value("error").toObject().value("message").toString();
        completeQuery(id, embedding, warning); nextQuery();
    }

    void completeQuery(const QString &id, const QVector<float> &embedding, const QString &warning) {
        if (strictGpu() && embedding.isEmpty()) {
            finishError(id, QStringLiteral("gpu_unavailable"), warning.isEmpty()
                ? QStringLiteral("Strict GPU inference did not return an embedding") : warning);
            return;
        }
        runSearch(id, embedding, warning);
    }

    bool pathAllowed(const QString &path, bool code = false) const {
        if (code && !settings.includeCode) return false;
        if (!catalogReady || !catalogFiles.contains(pathKey(normalizePath(path), code))) return false;
        const auto &roots = code ? catalogCodeRoots : catalogRoots;
        const auto &excludes = code ? settings.codeExcludes : settings.excludes;
        for (const auto &root : roots) {
            if (!isPathWithin(path, root)) continue;
            bool excluded = false;
            for (const auto &exclude : excludes)
                if (isPathWithin(exclude, root) && normalizePath(exclude) != normalizePath(root) && isPathWithin(path, exclude)) excluded = true;
            if (!excluded) return true;
        }
        return false;
    }

    void runFileList(const QString &id) {
        if (!requests.contains(id)) return;
        const auto request = requests.value(id);
        FileListOptions options;
        options.kind = request.params.value("kind").toString();
        options.root = request.params.value("root").toString();
        options.code = request.params.value("code").toBool();
        options.allowedRoots = options.code ? catalogCodeRoots : catalogRoots;
        options.excludedRoots = options.code ? settings.codeExcludes : settings.excludes;
        options.enforceFolderRules = true;
        options.limit = request.params.value("limit").toInt(100);
        options.cursor = request.params.value("cursor").toString();
        options.configurationToken = settings.fingerprint() + u':' + QString::number(visibilityEpoch);
        options.cancelled = request.cancelled;
        activeSearches.insert(id, request);
        auto *watcher = new QFutureWatcher<FileListPage>(q);
        const auto requestVisibility = visibilityEpoch;
        QObject::connect(watcher, &QFutureWatcher<FileListPage>::finished, q, [this, watcher, id, requestVisibility] {
            auto page = watcher->result(); watcher->deleteLater(); activeSearches.remove(id);
            if (!requests.contains(id) || stopping) return;
            const auto pending = requests.value(id);
            const bool code = pending.params.value("code").toBool();
            if (pending.generation != generation || requestVisibility != visibilityEpoch || !catalogReady) {
                finishError(id, QStringLiteral("cursor_stale"), QStringLiteral("Folder selection changed during browsing. Refresh the list.")); return;
            }
            if (code && !settings.includeCode) {
                finishError(id, QStringLiteral("code_search_disabled"), QStringLiteral("Code search was disabled while browsing")); return;
            }
            if (!page.errorCode.isEmpty() || !page.error.isEmpty()) {
                finishError(id, page.errorCode.isEmpty() ? QStringLiteral("storage_error") : page.errorCode, page.error); return;
            }
            // Native identity is rechecked after the read transaction. The page
            // cursor still advances over stale rows, avoiding an endless page.
            for (auto it = page.files.begin(); it != page.files.end();) {
                FileRecord source;
                if (!pathAllowed(it->path, code) || !FileCatalog::describe(it->path, &source)
                    || !it->sourceVersion.startsWith(source.fingerprint + u'|')) it = page.files.erase(it);
                else ++it;
            }
            requests.remove(id);
            auto payload = page.toJson();
            payload.insert(QStringLiteral("visibilityEpoch"), QString::number(requestVisibility));
            sendResult(pending.socket, pending.id, payload);
        });
        watcher->setFuture(QtConcurrent::run(&searchPool, [this, options] { return repository.listFiles(options); }));
        QTimer::singleShot(120000, q, [this, id] {
            finishError(id, QStringLiteral("timeout"), QStringLiteral("File browsing timed out"));
        });
    }

    void runSearch(const QString &id, const QVector<float> &embedding, const QString &warning = {}) {
        if (!requests.contains(id)) return;
        const auto request = requests.value(id);
        SearchOptions options;
        options.query = request.params.value("query").toString(); options.kind = request.params.value("kind").toString();
        options.root = request.params.value("root").toString(); options.code = request.params.value("code").toBool();
        options.allowedRoots = options.code ? catalogCodeRoots : catalogRoots;
        options.excludedRoots = options.code ? settings.codeExcludes : settings.excludes; options.enforceFolderRules = true;
        options.limit = request.params.value("limit").toInt(100); options.queryEmbedding = embedding;
        options.cancelled = request.cancelled;
        activeSearches.insert(id, request);
        auto *watcher = new QFutureWatcher<SearchPackage>(q);
        const auto requestVisibility = visibilityEpoch;
        QObject::connect(watcher, &QFutureWatcher<SearchPackage>::finished, q, [this, watcher, id, warning, requestVisibility, semantic = !embedding.isEmpty()] {
            const auto result = watcher->result(); watcher->deleteLater();
            activeSearches.remove(id);
            if (!requests.contains(id) || stopping) return;
            const auto pendingSnapshot = requests.value(id);
            if (pendingSnapshot.params.value("code").toBool() && !settings.includeCode) {
                finishError(id, QStringLiteral("code_search_disabled"), QStringLiteral("Code search was disabled while the query was running")); return;
            }
            if (pendingSnapshot.generation != generation || requestVisibility != visibilityEpoch) {
                finishError(id, QStringLiteral("configuration_changed"), QStringLiteral("Search configuration changed while the query was running")); return;
            }
            if (!catalogReady) {
                finishError(id, QStringLiteral("metadata_pending"), QStringLiteral("Folder metadata reconciliation has not completed successfully")); return;
            }
            if (!result.error.isEmpty()) { finishError(id, QStringLiteral("storage_error"), result.error); return; }
            QJsonArray hits;
            for (const auto &hit : result.hits) {
                if (!pathAllowed(hit.path, pendingSnapshot.params.value("code").toBool())) continue;
                FileRecord current;
                if (!FileCatalog::describe(hit.path, &current)) continue;
                if (!hit.sourceVersion.startsWith(current.fingerprint + u'|')) continue;
                hits.append(hit.toJson());
            }
            const auto pending = requests.take(id);
            sendResult(pending.socket, pending.id, {{"hits", hits}, {"semantic", semantic}, {"warning", warning}});
        });
        watcher->setFuture(QtConcurrent::run(&searchPool, [this, options] {
            SearchPackage result;
            if (options.cancelled && options.cancelled->load()) { result.error = QStringLiteral("Search cancelled"); return result; }
            result.hits = repository.search(options, &result.error); return result;
        }));
    }

    void finishError(const QString &id, const QString &code, const QString &message) {
        if (!requests.contains(id)) return;
        const auto pending = requests.take(id);
        pending.cancelled->store(true);
        sendError(pending.socket, pending.id, code, message);
    }
    void cancelRequest(const QString &id) {
        finishError(id, QStringLiteral("cancelled"), QStringLiteral("Request cancelled"));
        if (auto worker = previewWorkers.value(id)) worker->kill();
        queryQueue.removeAll(id);
        if (activeQuery == id) {
            queryWorker.write(QJsonDocument(QJsonObject{{"id", "cancel"}, {"operation", "cancel"}, {"targetId", id}})
                                  .toJson(QJsonDocument::Compact) + '\n');
            activeQuery.clear(); queryTimeout.stop();
            // Process isolation guarantees the cancelled query cannot delay later requests indefinitely.
            stopWorkerForRestart(true);
        }
        updateIdleWorkers();
    }

    void startPreview(QLocalSocket *socket, const QString &id, const QJsonObject &params) {
        if (!catalogReady) {
            sendError(socket, id, QStringLiteral("metadata_pending"), QStringLiteral("Folder metadata reconciliation has not completed successfully")); return;
        }
        const QString path = params.value("path").toString();
        const int start = params.value("start").toInt();
        const int maximum = params.value("maxSegments").toInt(100);
        FileRecord source;
        QString error;
        if (!QFileInfo(path).isAbsolute() || !pathAllowed(path, params.value("code").toBool()) || start < 0 || maximum < 1 || maximum > 100
            || (params.value("code").toBool() && !settings.includeCode)
            || !FileCatalog::describe(path, &source, &error)) {
            sendError(socket, id, QStringLiteral("invalid_request"), QStringLiteral("Preview requires an allowed source path and a bounded cursor")); return;
        }
        if (previewWorkers.size() >= 2) {
            sendError(socket, id, QStringLiteral("busy"), QStringLiteral("Two document previews are already running")); return;
        }
        const QString internal = QUuid::createUuid().toString(QUuid::WithoutBraces);
        requests.insert(internal, {socket, id, params, generation});
        auto *worker = new QProcess(q);
        previewWorkers.insert(internal, worker);
        auto buffer = std::make_shared<QByteArray>();
        worker->setProgram(executablePath(QStringLiteral("qcs-worker")));
        worker->setArguments({"--profile-dir", paths.profileDir, "--model-dir", paths.modelDir, "--mode", "extract", "--threads", "2"});
        QObject::connect(worker, &QProcess::started, q, [worker, internal, source, params, start, maximum] {
            const QJsonObject request{{"id", internal}, {"operation", "extract"}, {"path", source.path},
                {"expectedVersion", sourceVersion(source.path)}, {"embed", false}, {"code", params.value("code").toBool()},
                {"offset", start}, {"maxSegments", maximum}};
            worker->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
            worker->closeWriteChannel();
        });
        QObject::connect(worker, &QProcess::readyReadStandardOutput, q, [this, worker, buffer, internal, source] {
            buffer->append(worker->readAllStandardOutput());
            if (buffer->size() > maxMessageBytes) {
                finishError(internal, QStringLiteral("message_too_large"), QStringLiteral("Preview response exceeds its limit")); worker->kill(); return;
            }
            const auto newline = buffer->indexOf('\n');
            if (newline < 0) return;
            const auto response = QJsonDocument::fromJson(buffer->left(newline)).object();
            buffer->remove(0, newline + 1);
            if (!requests.contains(internal)) return;
            if (response.value("id").toString() != internal || !response.value("ok").toBool()) {
                finishError(internal, QStringLiteral("preview_error"), response.value("error").toObject().value("message").toString("Invalid preview worker response")); return;
            }
            FileRecord now;
            const auto pendingSnapshot = requests.value(internal);
            if (pendingSnapshot.generation != generation
                || !pathAllowed(source.path, pendingSnapshot.params.value("code").toBool())
                || !FileCatalog::describe(source.path, &now) || now.fingerprint != source.fingerprint) {
                finishError(internal, QStringLiteral("source_changed"), QStringLiteral("Source or folder selection changed during preview")); return;
            }
            auto result = response.value("result").toObject();
            const QString state = result.value("state").toString();
            if (state == "failed" || state == "unsupported" || state == "cancelled") {
                finishError(internal, QStringLiteral("preview_error"), result.value("error").toString(state)); return;
            }
            if (result.value("extractionFingerprint").toString() != extractionPipelineFingerprint()) {
                finishError(internal, QStringLiteral("pipeline_mismatch"), QStringLiteral("Preview worker uses a different extraction pipeline")); return;
            }
            QStringList text;
            QJsonArray safeSegments;
            for (const auto &value : result.value("segments").toArray()) {
                auto segment = value.toObject(); segment.remove("mediaPath"); segment.remove("embedding");
                text.append(segment.value("text").toString()); safeSegments.append(segment);
            }
            result.insert("segments", safeSegments);
            result.insert("text", text.join(QStringLiteral("\n\n")));
            result.insert("partial", !result.value("complete").toBool());
            const auto pending = requests.take(internal);
            sendResult(pending.socket, pending.id, result);
        });
        QObject::connect(worker, &QProcess::readyReadStandardError, q, [this, worker] {
            const auto output = QString::fromUtf8(worker->readAllStandardError()).trimmed();
            if (!output.isEmpty()) log(output.left(16384));
        });
        QObject::connect(worker, &QProcess::errorOccurred, q, [this, internal, worker](QProcess::ProcessError error) {
            if (error != QProcess::FailedToStart) return;
            finishError(internal, QStringLiteral("worker_error"), worker->errorString());
            previewWorkers.remove(internal); worker->deleteLater();
        });
        QObject::connect(worker, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), q, [this, worker, internal](int, QProcess::ExitStatus) {
            finishError(internal, QStringLiteral("worker_error"), QStringLiteral("Preview worker exited without a complete response"));
            previewWorkers.remove(internal); worker->deleteLater();
        });
        QPointer<QProcess> guarded(worker);
        QTimer::singleShot(90000, q, [this, guarded, internal] {
            if (!guarded) return;
            finishError(internal, QStringLiteral("timeout"), QStringLiteral("Document preview timed out")); guarded->kill();
        });
        worker->start();
    }

    void loadJournal() {
        QFile file(journalPath());
        if (file.open(QIODevice::ReadOnly) && file.size() <= 16 * 1024 * 1024) {
            const auto json = QJsonDocument::fromJson(file.readAll()).object();
            if (json.value("version").toInt() == 1) failures = json.value("failures").toObject();
        }
    }
    QString restoreContinuation(Job &job, const QString &storedVersion) {
        const QString prefix = job.indexedVersion + QStringLiteral("|partial:");
        if (!storedVersion.startsWith(prefix)) return {};
        bool valid = false;
        const int offset = storedVersion.mid(prefix.size()).toInt(&valid);
        if (!valid || offset <= 0 || offset >= 5000) return {};
        // Cursor and segments are committed in one SQLite transaction. The JSON
        // journal is diagnostic, so losing it cannot invalidate resumable data.
        // Native source fingerprint, pipeline and code space were checked above.
        QString error;
        auto segments = repository.fileSegments(job.file.path, job.file.kind == "code", &error);
        if (error.isEmpty()) { job.offset = offset; job.segments = std::move(segments); }
        return error;
    }
    void saveJournal() {
        QJsonObject journal{{"version", 1}, {"failures", failures}, {"progress", progress.toJson()}};
        if (current) journal.insert("current", QJsonObject{{"path", current->file.path}, {"version", current->indexedVersion},
            {"offset", current->offset}, {"settings", settings.fingerprint()}});
        QSaveFile file(journalPath());
        if (!file.open(QIODevice::WriteOnly)) { log(QStringLiteral("Cannot write index journal: %1").arg(file.errorString())); return; }
        const auto bytes = QJsonDocument(journal).toJson(QJsonDocument::Compact);
        if (file.write(bytes) != bytes.size() || !file.commit()) log(QStringLiteral("Cannot commit index journal: %1").arg(file.errorString()));
    }
};

ServiceServer::ServiceServer(RuntimePaths paths, QObject *parent) : ServiceServer(std::move(paths), ServiceDependencies{}, parent) {}
ServiceServer::ServiceServer(RuntimePaths paths, ServiceDependencies dependencies, QObject *parent)
    : QObject(parent), d(std::make_unique<Private>(this, std::move(paths), std::move(dependencies))) {}
ServiceServer::~ServiceServer() { d->stop(); }
bool ServiceServer::start(QString *error) { return d->start(error); }
void ServiceServer::stop() { d->stop(); }

} // namespace qcopilots_search
