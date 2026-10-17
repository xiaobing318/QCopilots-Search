#include "core/native_lock_path.h"
#include "core/runtime_paths.h"
#include "core/types.h"
#include "core/model_catalog.h"
#include "extraction/extractor.h"
#include "runtime/EmbeddingRuntime.h"

#include <QCommandLineParser>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFutureWatcher>
#include <QGuiApplication>
#include <QImage>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLockFile>
#include <QScopeGuard>
#include <QTemporaryDir>
#include <QThread>
#include <QThreadPool>
#include <QTimer>
#include <QtConcurrentRun>
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <functional>
#include <memory>
#include <stdexcept>
#ifdef Q_OS_WIN
#include <windows.h>
#else
#include <poll.h>
#include <unistd.h>
#endif

using namespace qcopilots_search;

namespace {
QJsonObject failure(const QJsonValue &id, const QString &code, const QString &message)
{
    return {{QStringLiteral("id"), id}, {QStringLiteral("ok"), false},
            {QStringLiteral("error"), QJsonObject{{QStringLiteral("code"), code},
                                                 {QStringLiteral("message"), message}}}};
}

QJsonObject success(const QJsonValue &id, const QJsonObject &result)
{
    return {{QStringLiteral("id"), id}, {QStringLiteral("ok"), true},
            {QStringLiteral("result"), result}};
}

constexpr auto temporaryMarker = "qcs-worker-owner.json";
constexpr auto temporaryLock = "qcs-worker-lock";

// Only reclaim directories carrying our exact profile marker and an abandoned
// lock. A parallel worker's lock prevents its media from being removed.
void reclaimAbandonedJobs(const RuntimePaths &paths)
{
    const QDir temporaryRoot(paths.tempDir);
    const auto canonicalRoot = QFileInfo(paths.tempDir).canonicalFilePath();
    if (canonicalRoot.isEmpty()) return;
    const auto jobs = temporaryRoot.entryInfoList({QStringLiteral("qcs-worker-*")},
        QDir::Dirs | QDir::NoDotAndDotDot | QDir::NoSymLinks);
    for (const auto &job : jobs) {
        if (QFileInfo(job.absolutePath()).canonicalFilePath() != canonicalRoot) continue;
        QDir directory(job.absoluteFilePath());
        QFile marker(directory.filePath(QString::fromLatin1(temporaryMarker)));
        if (!marker.open(QIODevice::ReadOnly) || marker.size() > 4096) continue;
        const auto owner = QJsonDocument::fromJson(marker.readAll()).object();
        marker.close();
        if (owner.value(QStringLiteral("version")).toInt() != 1
            || owner.value(QStringLiteral("kind")).toString() != QStringLiteral("qcopilots-search-worker-temporary")
            || owner.value(QStringLiteral("profile")).toString() != paths.profileDir) continue;
        QLockFile lock(nativeLockFilePath(directory.filePath(QString::fromLatin1(temporaryLock))));
        lock.setStaleLockTime(0);
        if (!lock.tryLock(0)) continue;
        lock.unlock();
        if (!directory.removeRecursively())
            std::fprintf(stderr, "Could not reclaim abandoned worker temporary directory: %s\n", job.absoluteFilePath().toUtf8().constData());
    }
}

// QSocketNotifier cannot monitor a Windows anonymous pipe. A short, nonblocking
// poll keeps cancellation responsive while the one computation thread is busy.
class InputPump final : public QObject {
public:
    explicit InputPump(QObject *parent = nullptr) : QObject(parent)
    {
        timer_.setInterval(10);
        connect(&timer_, &QTimer::timeout, this, [this] { readAvailable(); });
    }
    std::function<void(QByteArray)> onFrame;
    std::function<void(QString)> onError;
    std::function<void()> onEnd;
    void start() { timer_.start(); }
    void stop() { timer_.stop(); }

private:
    void end()
    {
        stop();
        if (!buffer_.isEmpty() && onError)
            onError(QStringLiteral("Input ended before the NDJSON newline"));
        buffer_.clear();
        if (onEnd) onEnd();
    }
    void fail(const QString &message)
    {
        stop();
        if (onError) onError(message);
        if (onEnd) onEnd();
    }
    void accept(const char *data, qsizetype size)
    {
        buffer_.append(data, size);
        for (;;) {
            const auto newline = buffer_.indexOf('\n');
            if (newline < 0) break;
            if (newline > maxMessageBytes) {
                fail(QStringLiteral("Worker request exceeds the 16 MiB message limit"));
                return;
            }
            auto frame = buffer_.left(newline);
            buffer_.remove(0, newline + 1);
            if (frame.endsWith('\r')) frame.chop(1);
            if (!frame.isEmpty() && onFrame) onFrame(std::move(frame));
            if (!timer_.isActive()) return;
        }
        if (buffer_.size() > maxMessageBytes)
            fail(QStringLiteral("Worker request exceeds the 16 MiB message limit"));
    }
    void readAvailable()
    {
        char data[65536];
        // Bound work on the input thread even when a peer writes continuously.
        for (int iteration = 0; iteration < 16; ++iteration) {
#ifdef Q_OS_WIN
            const HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
            const DWORD type = GetFileType(input);
            DWORD available = 0;
            if (type == FILE_TYPE_PIPE) {
                if (!PeekNamedPipe(input, nullptr, 0, nullptr, &available, nullptr)) {
                    if (GetLastError() == ERROR_BROKEN_PIPE) end();
                    else fail(QStringLiteral("Cannot inspect the worker input pipe"));
                    return;
                }
                if (available == 0) return;
            } else if (type == FILE_TYPE_DISK) {
                available = DWORD(sizeof(data));
            } else {
                fail(QStringLiteral("Worker input requires a pipe or an NDJSON file"));
                return;
            }
            DWORD read = 0;
            if (!ReadFile(input, data, std::min(available, DWORD(sizeof(data))), &read, nullptr)) {
                if (GetLastError() == ERROR_BROKEN_PIPE) end();
                else fail(QStringLiteral("Cannot read the worker input pipe"));
                return;
            }
            if (read == 0) { end(); return; }
            accept(data, qsizetype(read));
#else
            pollfd descriptor{STDIN_FILENO, POLLIN, 0};
            const int ready = ::poll(&descriptor, 1, 0);
            if (ready < 0) {
                if (errno == EINTR) return;
                fail(QStringLiteral("Cannot inspect the worker input descriptor"));
                return;
            }
            if (ready == 0) return;
            if (descriptor.revents & (POLLERR | POLLNVAL)) {
                fail(QStringLiteral("Worker input descriptor is unavailable"));
                return;
            }
            const auto count = ::read(STDIN_FILENO, data, sizeof(data));
            if (count == 0) { end(); return; }
            if (count < 0) {
                if (errno == EINTR || errno == EAGAIN) return;
                fail(QStringLiteral("Cannot read the worker input descriptor"));
                return;
            }
            accept(data, qsizetype(count));
#endif
            if (!timer_.isActive()) return;
        }
    }
    QTimer timer_;
    QByteArray buffer_;
};

class Worker final : public QObject {
public:
    Worker(RuntimePaths paths, QString mode, QString tessdata, QJsonObject device, QString modelId, QObject *parent = nullptr)
        : QObject(parent), paths_(std::move(paths)), mode_(std::move(mode)),
          tessdata_(std::move(tessdata)), device_(std::move(device)), modelId_(std::move(modelId)), input_(this)
    {
        pool_.setMaxThreadCount(1);
        pool_.setExpiryTimeout(-1);
        (void)output_.open(stdout, QIODevice::WriteOnly);
        input_.onFrame = [this](const QByteArray &frame) { receive(frame); };
        input_.onError = [this](const QString &error) {
            send(failure(QJsonValue(), QStringLiteral("framing_error"), error));
            exitCode_ = 2;
        };
        // A finite NDJSON input may close immediately after its last request.
        // Drain that job before exiting. Broken framing and explicit shutdown
        // cancel it instead, while the service can terminate a crashed peer.
        input_.onEnd = [this] { shutdown(exitCode_ != 0); };
        connect(&watcher_, &QFutureWatcher<QJsonObject>::finished, this, [this] {
            send(watcher_.result());
            activeId_ = {};
            active_ = false;
            if (stopping_) QCoreApplication::exit(exitCode_);
        });
    }
    ~Worker() override
    {
        cancelled_.store(true);
        (void)pool_.waitForDone();
    }
    void start() { input_.start(); }

private:
    void send(QJsonObject frame)
    {
        QByteArray data = QJsonDocument(frame).toJson(QJsonDocument::Compact);
        if (data.size() > maxMessageBytes)
            data = QJsonDocument(failure(frame.value(QStringLiteral("id")), QStringLiteral("response_too_large"),
                QStringLiteral("Worker response exceeds 16 MiB. Reduce maxSegments."))).toJson(QJsonDocument::Compact);
        data.append('\n');
        if (output_.write(data) != data.size() || !output_.flush()) {
            exitCode_ = 3;
            shutdown();
        }
    }
    void shutdown(bool cancel = true)
    {
        input_.stop();
        stopping_ = true;
        if (cancel) cancelled_.store(true);
        if (!active_) QCoreApplication::exit(exitCode_);
    }
    void receive(const QByteArray &frame)
    {
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(frame, &parse);
        if (parse.error != QJsonParseError::NoError || !document.isObject()) {
            send(failure(QJsonValue(), QStringLiteral("invalid_json"), QStringLiteral("Expected one JSON object per line")));
            return;
        }
        const auto request = document.object();
        const auto id = request.value(QStringLiteral("id"));
        if (!(id.isString() && !id.toString().isEmpty()) && !id.isDouble()) {
            send(failure(id, QStringLiteral("invalid_request"), QStringLiteral("A nonempty request id is required")));
            return;
        }
        if (request.contains(QStringLiteral("version")) && request.value(QStringLiteral("version")).toInt(-1) != protocolVersion) {
            send(failure(id, QStringLiteral("protocol_mismatch"), QStringLiteral("Unsupported worker protocol version")));
            return;
        }
        const auto operation = request.value(QStringLiteral("operation")).toString();
        if (operation == QStringLiteral("shutdown")) {
            send(success(id, {{QStringLiteral("stopping"), true}}));
            shutdown();
            return;
        }
        if (operation == QStringLiteral("cancel")) {
            const auto target = request.value(QStringLiteral("targetId"));
            const bool found = active_ && (target.isUndefined() || target == activeId_);
            if (found) cancelled_.store(true);
            send(success(id, {{QStringLiteral("cancelled"), found}, {QStringLiteral("targetId"), activeId_}}));
            return;
        }
        if (operation != QStringLiteral("extract") && operation != QStringLiteral("query")
            && operation != QStringLiteral("diagnostics") && operation != QStringLiteral("devices")
            && operation != QStringLiteral("model.probe")) {
            send(failure(id, QStringLiteral("unknown_operation"), QStringLiteral("Supported operations: extract, query, diagnostics, devices, model.probe, cancel, shutdown")));
            return;
        }
        if (operation != QStringLiteral("diagnostics") && operation != QStringLiteral("devices")
            && operation != QStringLiteral("model.probe")
            && ((mode_ == QStringLiteral("query") && operation != QStringLiteral("query"))
                || (mode_ == QStringLiteral("extract") && operation != QStringLiteral("extract")))) {
            send(failure(id, QStringLiteral("wrong_mode"), QStringLiteral("Operation is unavailable in this worker mode")));
            return;
        }
        if (active_) {
            send(failure(id, QStringLiteral("busy"), QStringLiteral("The worker accepts only one active job. Retry after its response.")));
            return;
        }
        active_ = true;
        activeId_ = id;
        cancelled_.store(false);
        watcher_.setFuture(QtConcurrent::run(&pool_, [this, request] {
            QJsonObject response;
            try { response = run(request); }
            catch (const std::exception &error) {
                response = failure(request.value(QStringLiteral("id")), QStringLiteral("worker_error"), QString::fromUtf8(error.what()));
            }
            catch (...) {
                response = failure(request.value(QStringLiteral("id")), QStringLiteral("worker_error"), QStringLiteral("Unexpected worker failure"));
            }
            // Extraction-only workers still identify the selected pipeline.
            // Their keyword path must not load model weights for this metadata.
            const QString key = response.value(QStringLiteral("ok")).toBool()
                ? QStringLiteral("result") : QStringLiteral("error");
            auto payload = response.value(key).toObject();
            const auto* model = findModel(modelId_);
            payload.insert(QStringLiteral("modelId"), modelId_);
            payload.insert(QStringLiteral("dimensions"), model ? model->dimensions : 0);
            payload.insert(QStringLiteral("embeddingFingerprint"), modelPipelineFingerprint(modelId_));
            // Read runtime state on its owner thread, including failed model
            // initialization. A strict GPU failure must remain an error reply.
            if (runtime_) {
                const auto diagnostic = runtime_->diagnostics();
                payload.insert(QStringLiteral("runtime"), diagnostic);
                if (!response.value(QStringLiteral("ok")).toBool()) {
                    const bool deviceFailure = runtimeUsed_ && diagnostic.value(QStringLiteral("deviceFailure")).toBool()
                        && payload.value(QStringLiteral("code")).toString() != QStringLiteral("cancelled");
                    payload.insert(QStringLiteral("deviceFailure"), deviceFailure);
                    if (deviceFailure) payload.insert(QStringLiteral("code"), QStringLiteral("gpu_unavailable"));
                }
            }
            response.insert(key, payload);
            return response;
        }));
    }
    void ensureRuntime()
    {
        if (!runtime_) {
            runtime_ = std::make_unique<EmbeddingRuntime>(modelId_);
            runtime_->setThreadCount(qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_THREADS"));
            runtime_->setCancellationCallback([this] { return cancelled_.load(); });
            QString error;
            if (!runtime_->setDevice(device_, &error)) throw std::runtime_error(error.toStdString());
        }
    }
    void reportRuntime(const QJsonValue &id, const QString &phase, bool loadingModel)
    {
        if (!runtimeEvents_) return;
        const QJsonObject event{{"id", id}, {"event", "runtime"},
            {"data", QJsonObject{{"phase", phase}, {"activity", loadingModel ? "model-load" : "encode"},
                {"runtime", runtime_->diagnostics()}}}};
        // Flush the evidence before entering a vendor backend that may abort
        // without unwinding. All stdout writes remain on the main thread.
        QMetaObject::invokeMethod(this, [this, event] { send(event); }, Qt::BlockingQueuedConnection);
    }
    template<typename Function> auto infer(const QJsonValue &id, Function operation, bool loadingModel = false)
    {
        runtimeUsed_ = true;
        reportRuntime(id, QStringLiteral("inference-start"), loadingModel);
        const auto finished = qScopeGuard([this, id, loadingModel] { reportRuntime(id, QStringLiteral("inference-end"), loadingModel); });
        return operation();
    }
    bool loadRuntime(bool textOnly, QString *error, const QJsonValue &id)
    {
        ensureRuntime();
        if (runtime_->isLoaded() && runtime_->isTextOnly() == textOnly && runtime_->modelId() == modelId_) return true;
        return infer(id, [this, textOnly, error] { return runtime_->load(paths_.modelDir, modelId_, textOnly, error); }, true);
    }
    QJsonObject run(const QJsonObject &request)
    {
        const auto id = request.value(QStringLiteral("id"));
        runtimeUsed_ = false;
        runtimeEvents_ = request.value(QStringLiteral("runtimeEvents")).toBool(false);
        if (request.contains(QStringLiteral("modelId")) && request.value(QStringLiteral("modelId")).toString() != modelId_)
            return failure(id, QStringLiteral("model_mismatch"), QStringLiteral("Request model does not match the worker model"));
        if (request.contains(QStringLiteral("dimensions"))
            && request.value(QStringLiteral("dimensions")).toInt(-1) != findModel(modelId_)->dimensions)
            return failure(id, QStringLiteral("model_mismatch"), QStringLiteral("Request dimensions do not match the worker model"));
        if (request.contains(QStringLiteral("embeddingFingerprint"))
            && request.value(QStringLiteral("embeddingFingerprint")).toString() != modelPipelineFingerprint(modelId_))
            return failure(id, QStringLiteral("model_mismatch"), QStringLiteral("Request fingerprint does not match the worker model"));
        if (cancelled_.load()) return failure(id, QStringLiteral("cancelled"), QStringLiteral("Job cancelled"));
        if (request.value(QStringLiteral("operation")).toString() == QStringLiteral("devices")) {
            QString error;
            const auto devices = EmbeddingRuntime::discoverDevices(
                qEnvironmentVariable("QCOPILOTS_SEARCH_BACKEND", QStringLiteral("auto")), &error);
            if (!error.isEmpty()) return failure(id, QStringLiteral("device_discovery"), error);
            return success(id, devices);
        }
        if (request.value(QStringLiteral("operation")).toString() == QStringLiteral("diagnostics")) {
            ensureRuntime();
            QString error;
            if (request.value(QStringLiteral("load")).toBool()
                && !loadRuntime(request.value(QStringLiteral("textOnly")).toBool(mode_ != QStringLiteral("full")), &error, id))
                return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("model_unavailable"), error);
            return success(id, {});
        }
        if (request.value(QStringLiteral("operation")).toString() == QStringLiteral("model.probe")) {
            QString error;
            if (!loadRuntime(false, &error, id))
                return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("model_unavailable"), error);
            const auto text = infer(id, [&] { return runtime_->embedText(QStringLiteral("A red and blue geometric picture"), PromptKind::Query, {}, &error); });
            if (text.size() != runtime_->dimensions())
                return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("inference_error"), error);
            QJsonObject probes{{QStringLiteral("text"), true}};
            if (runtime_->capabilities().value(QStringLiteral("image")).toBool()) {
                QTemporaryDir temporary(QDir(paths_.tempDir).filePath(QStringLiteral("qcs-model-probe-XXXXXX")));
                if (!temporary.isValid()) return failure(id, QStringLiteral("temporary_directory"), temporary.errorString());
                QImage image(64, 64, QImage::Format_RGB888);
                for (int y = 0; y < image.height(); ++y)
                    for (int x = 0; x < image.width(); ++x)
                        image.setPixelColor(x, y, (x < 32) != (y < 32) ? QColor(Qt::red) : QColor(Qt::blue));
                const auto path = QDir(temporary.path()).filePath(QStringLiteral("probe.png"));
                if (!image.save(path, "PNG")) return failure(id, QStringLiteral("probe_image"), QStringLiteral("Cannot create model probe image"));
                const auto vector = infer(id, [&] { return runtime_->embedImage(path, 70, &error); });
                if (!temporary.remove()) return failure(id, QStringLiteral("temporary_directory"), QStringLiteral("Cannot remove model probe files"));
                if (vector.size() != runtime_->dimensions())
                    return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("inference_error"), error);
                probes.insert(QStringLiteral("image"), true);
            }
            return success(id, {{QStringLiteral("status"), QStringLiteral("ready")},
                {QStringLiteral("capabilities"), runtime_->capabilities()}, {QStringLiteral("probes"), probes},
                {QStringLiteral("modelFingerprint"), runtime_->modelFingerprint()}});
        }
        if (request.value(QStringLiteral("operation")).toString() == QStringLiteral("query")) {
            const auto text = request.value(QStringLiteral("query")).toString();
            if (text.trimmed().isEmpty() || text.size() > 65536)
                return failure(id, QStringLiteral("invalid_query"), QStringLiteral("Query must contain between 1 and 65536 characters"));
            QString error;
            if (!loadRuntime(true, &error, id))
                return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("model_unavailable"), error);
            const auto vector = infer(id, [&] { return runtime_->embedText(text, request.value(QStringLiteral("code")).toBool()
                ? PromptKind::CodeQuery : PromptKind::Query, {}, &error); });
            if (cancelled_.load()) return failure(id, QStringLiteral("cancelled"), QStringLiteral("Query cancelled"));
            if (vector.size() != runtime_->dimensions()) return failure(id, QStringLiteral("inference_error"), error);
            QJsonArray values;
            for (float value : vector) values.append(double(value));
            return success(id, {{QStringLiteral("embedding"), values},
                                {QStringLiteral("modelFingerprint"), runtime_->modelFingerprint()},
                                {QStringLiteral("extractionFingerprint"), Extractor::pipelineFingerprint()}});
        }
        const auto path = request.value(QStringLiteral("path")).toString();
        if (path.isEmpty() || !QFileInfo(path).isAbsolute())
            return failure(id, QStringLiteral("invalid_path"), QStringLiteral("An absolute source path is required"));
        const auto before = Extractor::sourceVersion(path);
        if (before.isEmpty()) return failure(id, QStringLiteral("source_unavailable"), QStringLiteral("Source is not an existing regular file"));
        const auto expected = request.value(QStringLiteral("expectedVersion")).toString();
        if (!expected.isEmpty() && before != expected)
            return failure(id, QStringLiteral("stale_source"), QStringLiteral("Source changed before extraction"));
        const bool embed = request.value(QStringLiteral("embed")).toBool(mode_ == QStringLiteral("full"));
        if (mode_ == QStringLiteral("extract") && embed)
            return failure(id, QStringLiteral("wrong_mode"), QStringLiteral("Extraction-only worker cannot run inference"));
        const int offset = request.value(QStringLiteral("offset")).toInt(0);
        const int limit = request.value(QStringLiteral("maxSegments")).toInt(40);
        if (offset < 0 || limit < 1 || limit > 256)
            return failure(id, QStringLiteral("invalid_limits"), QStringLiteral("offset must be nonnegative and maxSegments must be between 1 and 256"));
        QString error;
        if (embed && !loadRuntime(false, &error, id))
            return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("model_unavailable"), error);
        QTemporaryDir temporary(QDir(paths_.tempDir).filePath(QStringLiteral("qcs-worker-XXXXXX")));
        if (!temporary.isValid())
            return failure(id, QStringLiteral("temporary_directory"), QStringLiteral("Cannot create isolated worker temporary directory: %1").arg(temporary.errorString()));
        QLockFile jobLock(nativeLockFilePath(QDir(temporary.path()).filePath(QString::fromLatin1(temporaryLock))));
        jobLock.setStaleLockTime(0);
        if (!jobLock.tryLock(0))
            return failure(id, QStringLiteral("temporary_directory"), QStringLiteral("Cannot lock worker temporary directory"));
        QFile marker(QDir(temporary.path()).filePath(QString::fromLatin1(temporaryMarker)));
        const auto owner = QJsonDocument(QJsonObject{{QStringLiteral("version"), 1},
            {QStringLiteral("kind"), QStringLiteral("qcopilots-search-worker-temporary")},
            {QStringLiteral("profile"), paths_.profileDir}}).toJson(QJsonDocument::Compact);
        if (!marker.open(QIODevice::WriteOnly) || marker.write(owner) != owner.size())
            return failure(id, QStringLiteral("temporary_directory"), QStringLiteral("Cannot record worker temporary ownership"));
        marker.close();
        ExtractionOptions options;
        options.tempDir = temporary.path();
        options.tessdataDir = tessdata_;
        options.start = offset;
        options.maxSegments = limit;
        options.code = request.value(QStringLiteral("code")).toBool();
        options.enableOcr = request.value(QStringLiteral("enableOcr")).toBool(true);
        options.cancelled = [this] { return cancelled_.load(); };
        auto result = Extractor::extract(path, options);
        QJsonArray embeddingErrors;
        QJsonArray unsupportedModalities;
        for (qsizetype index = 0; index < result.segments.size(); ++index) {
            if (cancelled_.load()) {
                result.state = QStringLiteral("cancelled");
                result.error = QStringLiteral("Extraction cancelled");
                result.complete = false;
                break;
            }
            auto &segment = result.segments[index];
            if (!embed) continue;
            error.clear();
            if (segment.modality == QStringLiteral("image")) {
                segment.embedding = infer(id, [&] { return runtime_->embedImage(segment.mediaPath, segment.imageBudget, &error); });
            } else if (segment.modality == QStringLiteral("audio")) {
                if (!runtime_->capabilities().value(QStringLiteral("audio")).toBool()) {
                    if (unsupportedModalities.isEmpty()) unsupportedModalities.append(QStringLiteral("audio"));
                    continue;
                }
                segment.embedding = infer(id, [&] { return runtime_->embedAudio(segment.mediaPath, &error); });
            } else if (!segment.text.trimmed().isEmpty()) {
                segment.embedding = infer(id, [&] { return runtime_->embedText(segment.text, PromptKind::Document, segment.title, &error); });
            } else {
                // A filename-only record carries no content embedding. Keyword
                // retrieval remains available for the original source name.
                continue;
            }
            if (segment.embedding.size() != runtime_->dimensions()
                && runtime_->diagnostics().value(QStringLiteral("deviceFailure")).toBool())
                return failure(id, cancelled_.load() ? QStringLiteral("cancelled") : QStringLiteral("gpu_unavailable"), error);
            if (segment.embedding.size() != runtime_->dimensions())
                embeddingErrors.append(QJsonObject{{QStringLiteral("index"), qint64(index)},
                    {QStringLiteral("error"), error.isEmpty() ? QStringLiteral("No embedding produced") : error}});
        }
        if (Extractor::sourceVersion(path) != before)
            return failure(id, QStringLiteral("stale_source"), QStringLiteral("Source changed before extraction and inference finished"));
        if (cancelled_.load()) {
            result.state = QStringLiteral("cancelled");
            result.complete = false;
            result.error = QStringLiteral("Job cancelled");
        } else if (!embeddingErrors.isEmpty()) {
            result.state = QStringLiteral("partial");
            result.complete = false;
            result.error = QStringLiteral("One or more content embeddings failed");
        }
        QJsonArray segments;
        for (auto segment : result.segments) {
            // These files belong to this job and are removed after inference.
            // Do not persist a dangling temporary media path in the database.
            segment.mediaPath.clear();
            segments.append(segment.toJson());
        }
        return success(id, {{QStringLiteral("segments"), segments},
            {QStringLiteral("state"), result.state}, {QStringLiteral("error"), result.error},
            {QStringLiteral("sourceVersion"), before}, {QStringLiteral("next"), result.next},
            {QStringLiteral("complete"), result.complete}, {QStringLiteral("embedded"), embed},
            {QStringLiteral("embeddingErrors"), embeddingErrors},
            {QStringLiteral("unsupportedModalities"), unsupportedModalities},
            {QStringLiteral("pipelineFingerprint"), request.value(QStringLiteral("pipelineFingerprint"))},
            {QStringLiteral("extractionFingerprint"), Extractor::pipelineFingerprint()},
            {QStringLiteral("modelFingerprint"), embed ? runtime_->modelFingerprint() : QString()}});
    }

    RuntimePaths paths_;
    QString mode_;
    QString tessdata_;
    QJsonObject device_;
    QString modelId_;
    QFile output_;
    InputPump input_;
    QThreadPool pool_;
    QFutureWatcher<QJsonObject> watcher_;
    std::unique_ptr<EmbeddingRuntime> runtime_;
    std::atomic_bool cancelled_{false};
    QJsonValue activeId_;
    bool active_ = false;
    bool runtimeEvents_ = false;
    bool runtimeUsed_ = false;
    bool stopping_ = false;
    int exitCode_ = 0;
};
}

int main(int argc, char **argv)
{
    qputenv("QT_COMMAND_LINE_PARSER_NO_GUI_MESSAGE_BOXES", "1");
    if (qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")) qputenv("QT_QPA_PLATFORM", "offscreen");
    QGuiApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-worker"));
    QCoreApplication::setApplicationVersion(QStringLiteral(QCOPILOTS_SEARCH_VERSION));
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Isolated QCopilots Search extraction and embedding worker. NDJSON protocol on standard input/output."));
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addOption({QStringLiteral("profile-dir"), QStringLiteral("Explicit writable instance profile."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("model-dir"), QStringLiteral("Verified model cache root. Additional presets use their ID subdirectory."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("model"), QStringLiteral("Embedding model preset ID."), QStringLiteral("id"), defaultModelId()});
    parser.addOption({QStringLiteral("tessdata-dir"), QStringLiteral("OCR language data directory."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("mode"), QStringLiteral("Worker mode: full, extract, query."), QStringLiteral("mode"), QStringLiteral("full")});
    parser.addOption({QStringLiteral("device"), QStringLiteral("Bind one device using JSON with backend, deviceId and name. Disables automatic CPU fallback."), QStringLiteral("json")});
    parser.addOption({QStringLiteral("threads"), QStringLiteral("CPU inference threads, from 1 to 32."), QStringLiteral("count"), QString::number(std::clamp(QThread::idealThreadCount() / 2, 1, 32))});
    parser.process(application);
    const auto mode = parser.value(QStringLiteral("mode"));
    const auto modelId = parser.value(QStringLiteral("model"));
    if (!findModel(modelId)) {
        std::fputs("Unknown embedding model preset. Use --help.\n", stderr);
        return 2;
    }
    bool threadsOk = false;
    const int threads = parser.value(QStringLiteral("threads")).toInt(&threadsOk);
    if ((mode != QStringLiteral("full") && mode != QStringLiteral("extract") && mode != QStringLiteral("query"))
        || !threadsOk || threads < 1 || threads > 32) {
        std::fputs("Invalid worker mode or thread count. Use --help.\n", stderr);
        return 2;
    }
    qputenv("QCOPILOTS_SEARCH_THREADS", QByteArray::number(threads));
    QJsonObject device;
    if (parser.isSet(QStringLiteral("device"))) {
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(parser.value(QStringLiteral("device")).toUtf8(), &parse);
        EmbeddingRuntime validation;
        QString error;
        if (parse.error != QJsonParseError::NoError || !document.isObject() || document.object().isEmpty()
            || !validation.setDevice(document.object(), &error)) {
            std::fprintf(stderr, "Invalid device selector: %s\n", error.isEmpty()
                ? "Expected a nonempty JSON object with backend and deviceId or name" : error.toUtf8().constData());
            return 2;
        }
        device = document.object();
    }
    const auto paths = RuntimePaths::resolve(parser.value(QStringLiteral("profile-dir")), parser.value(QStringLiteral("model-dir")));
    QString error;
    if (!paths.ensure(&error)) {
        std::fprintf(stderr, "Cannot initialize worker profile: %s\n", error.toUtf8().constData());
        return 2;
    }
    reclaimAbandonedJobs(paths);
    QString tessdata = parser.value(QStringLiteral("tessdata-dir"));
    if (tessdata.isEmpty()) tessdata = qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR");
    if (tessdata.isEmpty()) {
        const QDir applicationDir(QCoreApplication::applicationDirPath());
        for (const auto &relative : {QStringLiteral("tessdata"), QStringLiteral("../share/qcs/tessdata"),
                                    QStringLiteral("../Resources/qcs/tessdata"),
                                    QStringLiteral("../Resources/tessdata")}) {
            const auto candidate = applicationDir.absoluteFilePath(relative);
            if (QFileInfo::exists(QDir(candidate).filePath(QStringLiteral("eng.traineddata")))) {
                tessdata = candidate;
                break;
            }
        }
    }
    Worker worker(paths, mode, tessdata, device, modelId);
    worker.start();
    return application.exec();
}
