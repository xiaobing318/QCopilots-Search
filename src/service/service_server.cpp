#include "service_server.h"
#include "ipc_protocol.h"
#include "ipc_security.h"
#include "core/app_settings.h"
#include "core/file_catalog.h"
#include "core/model_files.h"
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
#include <QQueue>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QThreadPool>
#include <QTimer>
#include <QUuid>
#include <QtConcurrent>
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
};

struct ScanPackage { ScanResult scan; QHash<QString, QString> versions; };
struct SearchPackage { QList<SearchHit> hits; QString error; };
}

class ServiceServer::Private {
public:
    explicit Private(ServiceServer *owner, RuntimePaths value, ServiceDependencies dependencies) : q(owner), paths(std::move(value)),
        repository(std::move(dependencies.storage)), modelVerifier(std::move(dependencies.verifyModels)),
        lock(paths.lockFile), server(owner), monitor(owner), reconcile(owner), debounce(owner),
        indexWorker(owner), queryWorker(owner), indexTimeout(owner), queryTimeout(owner) {
        scanPool.setMaxThreadCount(1);
        writePool.setMaxThreadCount(1);
        searchPool.setMaxThreadCount(2);
    }

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
    QLockFile lock;
    QLocalServer server;
    QFileSystemWatcher monitor;
    QTimer reconcile, debounce;
    QProcess indexWorker, queryWorker;
    QTimer indexTimeout, queryTimeout;
    QByteArray indexBuffer, queryBuffer, token;
    QHash<QLocalSocket *, QByteArray> buffers;
    QHash<QString, Pending> requests;
    QHash<QString, Pending> activeSearches;
    QHash<QString, QPointer<QProcess>> previewWorkers;
    QQueue<QString> queryQueue;
    QString activeQuery;
    QQueue<Job> jobs;
    std::unique_ptr<Job> current;
    QString currentWorkerId;
    QJsonObject failures;
    IndexProgress progress;
    QThreadPool scanPool, writePool, searchPool;
    std::mutex publicationConfigurationMutex;
    std::atomic_bool scanCancelled{false};
    quint64 generation = 1;
    bool started = false, stopping = false, scanning = false, publishing = false;
    bool indexRestartPending = false, queryRestartPending = false;
    bool modelAvailable = false, pendingScan = false;
    bool pipelineReady = false, retryIncomplete = false;
    QString pipeline;
    QString pipelineError;
    QString watcherMode = QStringLiteral("periodic");
    QString watcherWarning;
    QStringList previousRoots;

    QString tokenPath() const { return QDir(paths.profileDir).filePath(QStringLiteral("qcopilots-search-ipc-token")); }
    QString journalPath() const { return QDir(paths.indexDir).filePath(QStringLiteral("qcopilots-search-progress.json")); }

    void log(const QString &message) {
        QFile file(QDir(paths.logDir).filePath(QStringLiteral("qcopilots-search-service.log")));
        if (file.size() > 4 * 1024 * 1024) {
            const QString previous = file.fileName() + QStringLiteral(".previous");
            QFile::remove(previous);
            file.rename(previous);
            file.setFileName(QDir(paths.logDir).filePath(QStringLiteral("qcopilots-search-service.log")));
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
        if (!server.listen(paths.ipcName)) {
            if (error) *error = QStringLiteral("%1 (endpoint %2, %3 encoded bytes)")
                .arg(server.errorString(), paths.ipcName).arg(QFile::encodeName(paths.ipcName).size());
            lock.unlock();
            return false;
        }
        QObject::connect(&server, &QLocalServer::newConnection, q, [this] { accept(); });
        QObject::connect(&monitor, &QFileSystemWatcher::directoryChanged, q, [this] { debounce.start(); });
        QObject::connect(&monitor, &QFileSystemWatcher::fileChanged, q, [this] { debounce.start(); });
        debounce.setSingleShot(true);
        debounce.setInterval(750);
        QObject::connect(&debounce, &QTimer::timeout, q, [this] { startScan(); });
        reconcile.setInterval(30000);
        QObject::connect(&reconcile, &QTimer::timeout, q, [this] { startScan(); });
        setupWorker(indexWorker, false);
        setupWorker(queryWorker, true);
        indexTimeout.setSingleShot(true);
        queryTimeout.setSingleShot(true);
        QObject::connect(&indexTimeout, &QTimer::timeout, q, [this] {
            indexRestartPending = indexWorker.state() != QProcess::NotRunning;
            publishFailedCurrent(QStringLiteral("Index worker timed out")); indexWorker.kill();
        });
        QObject::connect(&queryTimeout, &QTimer::timeout, q, [this] {
            const QString id = activeQuery;
            activeQuery.clear();
            finishError(id, QStringLiteral("timeout"), QStringLiteral("Query encoder timed out"));
            queryRestartPending = queryWorker.state() != QProcess::NotRunning;
            queryWorker.kill();
        });
        loadJournal();
        previousRoots = settings.roots;
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
        scanCancelled.store(true);
        { const std::lock_guard guard(publicationConfigurationMutex); ++generation; }
        saveJournal();
        server.close();
        const auto clients = buffers.keys();
        for (auto *socket : clients) socket->disconnectFromServer();
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
        for (auto *socket : buffers.keys()) if (socket->property("authenticated").toBool())
            send(socket, {{"event", name}, {"data", data}});
    }
    void publishProgress() { event(QStringLiteral("progress"), progress.toJson()); }

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
        if ((method == "search" || method == "document.preview")
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
                {"queuedFiles", jobs.size() + (current ? 1 : 0)}});
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
            settings = next;
            if (changed) invalidateIndexing();
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
                || params.value("limit").toInt(100) < 1 || params.value("limit").toInt(100) > 500) {
                sendError(socket, id, QStringLiteral("invalid_request"), QStringLiteral("Search requires a query up to 8192 characters and limit 1–500")); return;
            }
            const QString internal = QUuid::createUuid().toString(QUuid::WithoutBraces);
            requests.insert(internal, {socket, id, params, generation});
            QTimer::singleShot(120000, q, [this, internal] {
                if (!requests.contains(internal)) return;
                finishError(internal, QStringLiteral("timeout"), QStringLiteral("Search request timed out"));
                if (activeQuery == internal) {
                    activeQuery.clear(); queryRestartPending = queryWorker.state() != QProcess::NotRunning; queryWorker.kill();
                }
            });
            if (modelAvailable && params.value("semantic").toBool(true) && !params.value("query").toString().trimmed().isEmpty()) {
                queryQueue.enqueue(internal); nextQuery();
            } else runSearch(internal, {}, pipelineError);
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
            publishProgress(); sendResult(socket, id, progress.toJson());
        } else if (method == "index.start" || method == "index.reconcile" || method == "index.retry") {
            if (method == "index.retry") { failures = {}; retryIncomplete = true; }
            startScan(); sendResult(socket, id, progress.toJson());
        } else if (method == "models.reload") {
            if (publishing) { sendError(socket, id, QStringLiteral("busy"), QStringLiteral("Index publication is in progress, retry shortly")); return; }
            invalidateIndexing();
            for (const auto &key : requests.keys()) finishError(key, QStringLiteral("model_changed"), QStringLiteral("Model configuration changed"));
            activeQuery.clear(); queryQueue.clear();
            queryRestartPending = queryWorker.state() != QProcess::NotRunning; queryWorker.kill();
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
        if (directories.isEmpty()) for (const auto &root : settings.roots) if (QFileInfo(root).isDir()) directories.append(root);
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

    void invalidateIndexing() {
        { const std::lock_guard guard(publicationConfigurationMutex); ++generation; }
        scanCancelled.store(true);
        jobs.clear();
        if (!currentWorkerId.isEmpty()) indexWorker.write(QJsonDocument(QJsonObject{{"id", "cancel"}, {"operation", "cancel"},
            {"targetId", currentWorkerId}}).toJson(QJsonDocument::Compact) + '\n');
        if (!publishing) current.reset();
        currentWorkerId.clear(); indexTimeout.stop();
        indexRestartPending = indexWorker.state() != QProcess::NotRunning; indexWorker.kill();
        pendingScan = true;
    }

    void startScan() {
        if (stopping) return;
        if (!pipelineReady) { progress.state = QStringLiteral("error"); progress.error = pipelineError; publishProgress(); return; }
        if (settings.indexingPaused) { progress.state = QStringLiteral("paused"); publishProgress(); return; }
        if (scanning || current || publishing || !jobs.isEmpty()) { pendingScan = true; return; }
        scanning = true; pendingScan = false; scanCancelled.store(false);
        const quint64 epoch = generation;
        const FolderRules rules{settings.roots, settings.excludes, settings.includeCode};
        progress.state = QStringLiteral("scanning"); progress.error.clear(); publishProgress();
        auto *watcher = new QFutureWatcher<ScanPackage>(q);
        QObject::connect(watcher, &QFutureWatcher<ScanPackage>::finished, q, [this, watcher, epoch] {
            const auto result = watcher->result(); watcher->deleteLater(); scanning = false;
            if (stopping) return;
            if (epoch != generation || result.scan.cancelled) { startScan(); return; }
            updateWatchers(result.scan.directories);
            progress = {}; progress.state = QStringLiteral("indexing"); progress.total = result.scan.files.size();
            progress.error = result.scan.errors.join(u'\n');
            QSet<QString> present;
            const bool retryCapped = std::exchange(retryIncomplete, false);
            for (const auto &file : result.scan.files) {
                present.insert(file.path);
                const QString version = file.fingerprint + u'|' + pipeline;
                const QString stored = result.versions.value(file.path);
                if (!retryCapped && (stored == version + QStringLiteral("|capped:5000")
                    || stored == version + QStringLiteral("|partial:5000"))) {
                    const QString message = QStringLiteral("Index limit reached: the first 5000 segments remain searchable. Change the file or explicitly retry to rebuild it.");
                    failures.insert(file.path, message); ++progress.failed; progress.error = message;
                    continue;
                }
                if (result.versions.value(file.path) == version && !failures.contains(file.path)) { ++progress.completed; continue; }
                Job job{file, sourceVersion(file.path), version, epoch};
                restoreContinuation(job, result.versions.value(file.path));
                jobs.enqueue(std::move(job));
            }
            // An incomplete traversal must never erase valid data from an inaccessible directory.
            if (result.scan.errors.isEmpty()) {
                for (const auto &path : failures.keys()) if (!present.contains(path)) failures.remove(path);
                const QStringList roots = previousRoots;
                publishing = true;
                auto *pruner = new QFutureWatcher<QString>(q);
                QObject::connect(pruner, &QFutureWatcher<QString>::finished, q, [this, pruner, epoch] {
                    const QString error = pruner->result(); pruner->deleteLater(); publishing = false;
                    if (!error.isEmpty()) { progress.error = error; log(error); }
                    else if (epoch == generation) previousRoots = settings.roots;
                    event(QStringLiteral("indexChanged"), {}); scheduleNext();
                });
                pruner->setFuture(QtConcurrent::run(&writePool, [this, roots, present, epoch] {
                    QString error;
                    const std::lock_guard guard(publicationConfigurationMutex);
                    if (epoch != generation) return QString{};
                    for (const auto &root : roots) if (!repository.prune(root, present, &error)) break;
                    return error;
                }));
            } else scheduleNext();
            publishProgress();
        });
        watcher->setFuture(QtConcurrent::run(&scanPool, [this, rules] {
            ScanPackage result; result.scan = FileCatalog::scan(rules, &scanCancelled);
            for (const auto &file : result.scan.files) {
                if (scanCancelled.load()) { result.scan.cancelled = true; break; }
                result.versions.insert(file.path, repository.fileVersion(file.path, file.kind == "code"));
            }
            return result;
        }));
    }

    void setupWorker(QProcess &worker, bool query) {
        worker.setProcessChannelMode(QProcess::SeparateChannels);
        QObject::connect(&worker, &QProcess::readyReadStandardOutput, q, [this, &worker, query] {
            QByteArray &buffer = query ? queryBuffer : indexBuffer;
            buffer += worker.readAllStandardOutput();
            if (buffer.size() > maxMessageBytes) {
                if (query) queryRestartPending = true; else indexRestartPending = true;
                worker.kill(); workerFailure(query, QStringLiteral("Worker response exceeds protocol limit")); return;
            }
            while (true) {
                const qsizetype line = buffer.indexOf('\n');
                if (line < 0) break;
                const QByteArray bytes = buffer.left(line); buffer.remove(0, line + 1);
                QJsonParseError parse;
                const auto document = QJsonDocument::fromJson(bytes, &parse);
                if (parse.error != QJsonParseError::NoError || !document.isObject()) {
                    if (query) queryRestartPending = true; else indexRestartPending = true;
                    worker.kill(); workerFailure(query, QStringLiteral("Malformed worker response")); return;
                }
                if (query) receiveQuery(document.object()); else receiveIndex(document.object());
            }
        });
        QObject::connect(&worker, &QProcess::readyReadStandardError, q, [this, &worker] {
            const auto text = QString::fromUtf8(worker.readAllStandardError()).trimmed();
            if (!text.isEmpty()) log(text.left(16384));
        });
        QObject::connect(&worker, &QProcess::errorOccurred, q, [this, query](QProcess::ProcessError error) {
            if (error == QProcess::FailedToStart) workerFailure(query, QStringLiteral("Cannot start installed worker executable"));
        });
        QObject::connect(&worker, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), q,
            [this, query](int code, QProcess::ExitStatus status) {
                if (stopping) return;
                if (query) queryRestartPending = false; else indexRestartPending = false;
                if (query) queryBuffer.clear(); else indexBuffer.clear();
                workerFailure(query, QStringLiteral("Worker exited (%1, %2)").arg(code).arg(int(status)));
                QTimer::singleShot(50, q, [this, query] { if (query) nextQuery(); else scheduleNext(); });
            });
    }

    bool ensureWorker(QProcess &worker, bool query) {
        if (worker.state() == QProcess::Running) return true;
        if (worker.state() == QProcess::Starting) return false;
        worker.setProgram(executablePath(QStringLiteral("qcopilots-search-worker")));
        worker.setArguments({"--profile-dir", paths.profileDir, "--model-dir", paths.modelDir,
                             "--mode", query ? "query" : (modelAvailable ? "full" : "extract"), "--threads", query ? "4" : "2"});
        worker.start();
        // Process startup is bounded. File decoding and inference are always asynchronous.
        return worker.waitForStarted(3000);
    }

    void scheduleNext() {
        if (stopping || scanning || publishing || current) return;
        if (!pipelineReady) { progress.state = QStringLiteral("error"); progress.error = pipelineError; publishProgress(); return; }
        if (indexRestartPending && indexWorker.state() != QProcess::NotRunning) return;
        if (settings.indexingPaused) { progress.state = QStringLiteral("paused"); publishProgress(); return; }
        if (jobs.isEmpty()) {
            progress.state = progress.failed > 0 || !progress.error.isEmpty() ? QStringLiteral("partial") : QStringLiteral("idle");
            progress.currentPath.clear(); publishProgress(); saveJournal();
            if (pendingScan) QTimer::singleShot(0, q, [this] { startScan(); });
            return;
        }
        current = std::make_unique<Job>(jobs.dequeue());
        if (current->generation != generation) { current.reset(); scheduleNext(); return; }
        progress.currentPath = current->file.path; progress.state = QStringLiteral("indexing"); publishProgress();
        dispatchIndex();
    }

    void dispatchIndex() {
        if (!current || stopping || settings.indexingPaused || !pipelineReady) return;
        if (!ensureWorker(indexWorker, false)) { publishFailedCurrent(QStringLiteral("Cannot start index worker")); return; }
        currentWorkerId = QUuid::createUuid().toString(QUuid::WithoutBraces);
        const QJsonObject request{{"id", currentWorkerId}, {"operation", "extract"}, {"path", current->file.path},
            {"expectedVersion", current->workerVersion}, {"code", current->file.kind == "code"}, {"embed", modelAvailable},
            {"offset", current->offset}, {"maxSegments", 40}, {"pipelineFingerprint", pipeline}};
        indexWorker.write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
        indexTimeout.start(10 * 60 * 1000);
    }

    void receiveIndex(const QJsonObject &message) {
        if (!current || message.value("id").toString() != currentWorkerId) return;
        indexTimeout.stop(); currentWorkerId.clear();
        if (!message.value("ok").toBool()) { publishFailedCurrent(message.value("error").toObject().value("message").toString("Worker failed")); return; }
        const auto result = message.value("result").toObject();
        if (result.value("extractionFingerprint").toString() != extractionPipelineFingerprint()
            || (modelAvailable && result.value("modelFingerprint").toString() != embeddingPipelineFingerprint())) {
            failCurrent(QStringLiteral("Worker pipeline fingerprint differs from the service build")); return;
        }
        FileRecord now;
        QString error;
        if (current->generation != generation || !FileCatalog::describe(current->file.path, &now, &error)
            || now.fingerprint != current->file.fingerprint || sourceVersion(current->file.path) != current->workerVersion
            || result.value("sourceVersion").toString() != current->workerVersion) {
            current.reset(); pendingScan = true; scheduleNext(); return;
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
            if (!current || current->generation != generation) { current.reset(); scheduleNext(); return; }
            if (!error.isEmpty()) { failCurrent(error); return; }
            progress.segments += current->segments.size() - previousCount;
            event(QStringLiteral("indexChanged"), {{"path", job.file.path}});
            saveJournal();
            if (complete) {
                failures.remove(job.file.path); ++progress.completed; current.reset();
                scheduleNext();
            } else if (capped) {
                failCurrent(QStringLiteral("Index limit reached: the first 5000 segments remain searchable. Change the file or explicitly retry to rebuild it."));
            } else if (resume) {
                publishProgress();
                if (!settings.indexingPaused) QTimer::singleShot(0, q, [this] { dispatchIndex(); });
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
            if (!repository.replaceFile(job.file.path, version, job.segments, job.file.kind == "code", &error, gate)) return error;
            return QString{};
        }));
    }

    void publishFailedCurrent(const QString &failure) {
        if (!current || publishing) return;
        indexTimeout.stop(); currentWorkerId.clear();
        const Job job = *current;
        publishing = true;
        auto *watcher = new QFutureWatcher<QString>(q);
        QObject::connect(watcher, &QFutureWatcher<QString>::finished, q, [this, watcher, job, failure] {
            const QString error = watcher->result(); watcher->deleteLater(); publishing = false;
            if (stopping) return;
            if (!current || current->generation != generation) { current.reset(); scheduleNext(); return; }
            if (error.isEmpty()) event(QStringLiteral("indexChanged"), {{"path", job.file.path}});
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
                segments, job.file.kind == "code", &error, gate);
            return error;
        }));
    }

    void failCurrent(const QString &error) {
        indexTimeout.stop(); currentWorkerId.clear();
        if (current) {
            failures.insert(current->file.path, error); ++progress.failed;
            log(QStringLiteral("Index failed %1: %2").arg(current->file.path, error));
        }
        progress.error = error; current.reset(); saveJournal(); publishProgress();
        QTimer::singleShot(0, q, [this] { scheduleNext(); });
    }

    void workerFailure(bool query, const QString &error) {
        if (query) {
            queryTimeout.stop();
            const auto id = activeQuery; activeQuery.clear();
            if (!id.isEmpty()) runSearch(id, {}, error);
        } else if (current && !publishing && !currentWorkerId.isEmpty()) publishFailedCurrent(error);
    }

    void nextQuery() {
        if (stopping || !activeQuery.isEmpty()) return;
        if (queryRestartPending && queryWorker.state() != QProcess::NotRunning) return;
        while (!queryQueue.isEmpty() && !requests.contains(queryQueue.head())) queryQueue.dequeue();
        if (queryQueue.isEmpty()) return;
        activeQuery = queryQueue.dequeue();
        if (!ensureWorker(queryWorker, true)) {
            const QString id = activeQuery; activeQuery.clear(); runSearch(id, {}, QStringLiteral("Query encoder unavailable"));
            QTimer::singleShot(0, q, [this] { nextQuery(); }); return;
        }
        const auto params = requests.value(activeQuery).params;
        const QJsonObject request{{"id", activeQuery}, {"operation", "query"}, {"query", params.value("query")}, {"code", params.value("code").toBool()}};
        queryWorker.write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
        queryTimeout.start(90000);
    }

    void receiveQuery(const QJsonObject &message) {
        if (activeQuery.isEmpty() || message.value("id").toString() != activeQuery) return;
        queryTimeout.stop(); const auto id = activeQuery; activeQuery.clear();
        QVector<float> embedding;
        QString warning;
        if (message.value("ok").toBool()) {
            const auto result = message.value("result").toObject();
            if (result.value("modelFingerprint").toString() != embeddingPipelineFingerprint())
                warning = QStringLiteral("Query encoder pipeline fingerprint differs from the service build");
            else for (const auto &value : result.value("embedding").toArray()) embedding.append(float(value.toDouble()));
            if (embedding.size() != embeddingDimensions) { embedding.clear(); warning = QStringLiteral("Query encoder returned an invalid vector"); }
        } else warning = message.value("error").toObject().value("message").toString();
        runSearch(id, embedding, warning); nextQuery();
    }

    bool pathAllowed(const QString &path) const {
        for (const auto &root : settings.roots) {
            if (!isPathWithin(path, root)) continue;
            bool excluded = false;
            for (const auto &exclude : settings.excludes)
                if (isPathWithin(exclude, root) && normalizePath(exclude) != normalizePath(root) && isPathWithin(path, exclude)) excluded = true;
            if (!excluded) return true;
        }
        return false;
    }

    void runSearch(const QString &id, const QVector<float> &embedding, const QString &warning = {}) {
        if (!requests.contains(id)) return;
        const auto request = requests.value(id);
        SearchOptions options;
        options.query = request.params.value("query").toString(); options.kind = request.params.value("kind").toString();
        options.root = request.params.value("root").toString(); options.code = request.params.value("code").toBool();
        options.allowedRoots = settings.roots; options.excludedRoots = settings.excludes; options.enforceFolderRules = true;
        options.limit = request.params.value("limit").toInt(100); options.queryEmbedding = embedding;
        options.cancelled = request.cancelled;
        activeSearches.insert(id, request);
        auto *watcher = new QFutureWatcher<SearchPackage>(q);
        QObject::connect(watcher, &QFutureWatcher<SearchPackage>::finished, q, [this, watcher, id, warning, semantic = !embedding.isEmpty()] {
            const auto result = watcher->result(); watcher->deleteLater();
            activeSearches.remove(id);
            if (!requests.contains(id) || stopping) return;
            const auto pendingSnapshot = requests.value(id);
            if (pendingSnapshot.params.value("code").toBool() && !settings.includeCode) {
                finishError(id, QStringLiteral("code_search_disabled"), QStringLiteral("Code search was disabled while the query was running")); return;
            }
            if (pendingSnapshot.generation != generation) {
                finishError(id, QStringLiteral("configuration_changed"), QStringLiteral("Search configuration changed while the query was running")); return;
            }
            if (!result.error.isEmpty()) { finishError(id, QStringLiteral("storage_error"), result.error); return; }
            QJsonArray hits;
            for (const auto &hit : result.hits) {
                if (!pathAllowed(hit.path)) continue;
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
            queryRestartPending = queryWorker.state() != QProcess::NotRunning;
            queryWorker.kill();
        }
    }

    void startPreview(QLocalSocket *socket, const QString &id, const QJsonObject &params) {
        const QString path = params.value("path").toString();
        const int start = params.value("start").toInt();
        const int maximum = params.value("maxSegments").toInt(100);
        FileRecord source;
        QString error;
        if (!QFileInfo(path).isAbsolute() || !pathAllowed(path) || start < 0 || maximum < 1 || maximum > 100
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
        worker->setProgram(executablePath(QStringLiteral("qcopilots-search-worker")));
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
            if (!pathAllowed(source.path) || !FileCatalog::describe(source.path, &now) || now.fingerprint != source.fingerprint) {
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
    void restoreContinuation(Job &job, const QString &storedVersion) {
        const QString prefix = job.indexedVersion + QStringLiteral("|partial:");
        if (!storedVersion.startsWith(prefix)) return;
        bool valid = false;
        const int offset = storedVersion.mid(prefix.size()).toInt(&valid);
        if (!valid || offset <= 0 || offset >= 5000) return;
        // Cursor and segments are committed in one SQLite transaction. The JSON
        // journal is diagnostic, so losing it cannot invalidate resumable data.
        // Native source fingerprint, pipeline and code space were checked above.
        QString error;
        auto segments = repository.fileSegments(job.file.path, job.file.kind == "code", &error);
        if (error.isEmpty()) { job.offset = offset; job.segments = std::move(segments); }
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
