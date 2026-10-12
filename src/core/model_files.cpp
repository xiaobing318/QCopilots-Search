#include "model_files.h"
#include "pipeline_manifest.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QLockFile>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QStorageInfo>
#include <QTimer>
#include <algorithm>
#include <limits>

#ifdef Q_OS_MACOS
#include <cerrno>
#include <cstring>
#include <sys/mount.h>
#endif

namespace qcopilots_search {
namespace {
bool setError(QString* error, const QString& message)
{
    if (error) *error = message;
    return false;
}
constexpr qint64 reserveBytes = 32 * 1024 * 1024;
constexpr qint64 copyBufferBytes = 1024 * 1024;
}

ModelFiles::ModelFiles(QString modelDir, QObject* parent)
    : ModelFiles(std::move(modelDir), manifest(), parent) {}

ModelFiles::ModelFiles(QString modelDir, QList<ModelArtifact> artifacts, QObject* parent)
    : QObject(parent), modelDir_(modelDir.trimmed().isEmpty() ? QString{} : QDir(modelDir).absolutePath()),
      artifacts_(std::move(artifacts)), network_(new QNetworkAccessManager(this)) {}

ModelFiles::~ModelFiles()
{
    if (reply_) {
        disconnect(reply_, nullptr, this, nullptr);
        reply_->abort();
    }
    partial_.close();
}

QList<ModelArtifact> ModelFiles::manifest()
{
    const QString prefix = QStringLiteral("https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/")
        + QString::fromLatin1(embeddingModelRevision) + QLatin1Char('/');
    return {
        {QStringLiteral("embeddinggemma-2-Q8_0.gguf"), QUrl(prefix + QStringLiteral("embeddinggemma-2-Q8_0.gguf")),
         309855456, QByteArray(embeddingTextSha256)},
        {QStringLiteral("mmproj-embeddinggemma-2-Q8_0.gguf"), QUrl(prefix + QStringLiteral("mmproj-embeddinggemma-2-Q8_0.gguf")),
         554821024, QByteArray(embeddingMediaSha256)}
    };
}

bool ModelFiles::validateManifest(QString* error) const
{
    if (modelDir_.isEmpty()) return setError(error, tr("The model directory must be specified."));
    if (artifacts_.isEmpty()) return setError(error, tr("The model manifest is empty."));
    static const QRegularExpression safeName(QStringLiteral("^[A-Za-z0-9][A-Za-z0-9_.-]*\\.gguf$"));
    static const QRegularExpression hex(QStringLiteral("^[0-9a-f]{64}$"));
    QSet<QString> names;
    qint64 total = 0;
    for (const auto& artifact : artifacts_) {
        if (!safeName.match(artifact.name).hasMatch() || names.contains(artifact.name.toLower()) ||
            artifact.size <= 0 || artifact.size > (std::numeric_limits<qint64>::max() - reserveBytes) / 2 ||
            total > std::numeric_limits<qint64>::max() - artifact.size ||
            !hex.match(QString::fromLatin1(artifact.sha256)).hasMatch() ||
            !artifact.url.isValid() || artifact.url.host().isEmpty() ||
            (artifact.url.scheme() != QStringLiteral("https") && artifact.url.scheme() != QStringLiteral("http")))
            return setError(error, tr("Invalid model manifest entry: %1").arg(artifact.name));
        names.insert(artifact.name.toLower());
        total += artifact.size;
    }
    return true;
}

bool ModelFiles::verifyArtifact(const QString& path, const ModelArtifact& artifact, QString* error) const
{
    const QFileInfo info(path);
    if (!info.isFile() || info.isSymLink() || info.size() != artifact.size)
        return setError(error, tr("Missing model or incorrect size: %1").arg(path));
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return setError(error, tr("Cannot read %1: %2").arg(path, file.errorString()));
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file) || file.error() != QFileDevice::NoError)
        return setError(error, tr("Cannot hash %1: %2").arg(path, file.errorString()));
    if (hash.result().toHex() != artifact.sha256)
        return setError(error, tr("SHA-256 mismatch: %1").arg(path));
    return true;
}

bool ModelFiles::verify(QString* error) const
{
    if (error) error->clear();
    if (!validateManifest(error)) return false;
    for (const auto& artifact : artifacts_)
        if (!verifyArtifact(QDir(modelDir_).filePath(artifact.name), artifact, error)) return false;
    return true;
}

bool ModelFiles::acquireLock(QString* error)
{
    if (!QDir().mkpath(modelDir_)) return setError(error, tr("Cannot create model directory: %1").arg(modelDir_));
    lock_ = std::make_unique<QLockFile>(QDir(modelDir_).filePath(QStringLiteral("qcopilots-search-models.lock")));
    lock_->setStaleLockTime(0);
    if (!lock_->tryLock()) {
        lock_.reset();
        return setError(error, tr("The model directory is busy or cannot be locked: %1").arg(modelDir_));
    }
    return true;
}

bool ModelFiles::checkSpace(qint64 required, QString* error) const
{
    qint64 available = -1;
#ifdef Q_OS_MACOS
    // Qt 6.12 resolves kCFURLVolumeURLKey before calling statfs. APFS firmlinks
    // such as /Users can therefore be mapped to the read-only System volume.
    // Query the actual target path so both flags and capacity describe the Data
    // volume that will receive the download and atomic publication copy.
    struct statfs volume = {};
    if (::statfs(QFile::encodeName(modelDir_).constData(), &volume) != 0) {
        const int failure = errno;
        return setError(error, tr("Cannot query model storage for %1: %2")
                        .arg(modelDir_, QString::fromLocal8Bit(std::strerror(failure))));
    }
    if ((volume.f_flags & MNT_RDONLY) != 0)
        return setError(error, tr("Cannot access writable model storage: %1").arg(modelDir_));
    const auto blockSize = static_cast<quint64>(volume.f_bsize);
    const auto blocksAvailable = static_cast<quint64>(volume.f_bavail);
    if (blockSize == 0 || volume.f_bsize == std::numeric_limits<decltype(volume.f_bsize)>::max() ||
        blocksAvailable > static_cast<quint64>(std::numeric_limits<qint64>::max()) / blockSize)
        return setError(error, tr("Cannot determine available model storage: %1").arg(modelDir_));
    available = static_cast<qint64>(blocksAvailable * blockSize);
#else
    QStorageInfo storage(modelDir_);
    if (!storage.isValid() || !storage.isReady() || storage.isReadOnly())
        return setError(error, tr("Cannot access writable model storage: %1").arg(modelDir_));
    available = storage.bytesAvailable();
#endif
    // Unknown capacity must not silently disable the download budget.
    if (available < 0 || required < 0 || required > std::numeric_limits<qint64>::max() - reserveBytes)
        return setError(error, tr("Cannot determine available model storage: %1").arg(modelDir_));
    if (available < required + reserveBytes)
        return setError(error, tr("Insufficient model storage. Need %1 bytes plus %2 bytes reserve.")
                        .arg(required).arg(reserveBytes));
    return true;
}

bool ModelFiles::publish(const QString& source, const ModelArtifact& artifact, QString* error)
{
    // QSaveFile publishes by an atomic same-directory rename, preserving any old
    // model on write failure. Direct-write fallback would break this guarantee.
    const QString destination = QDir(modelDir_).filePath(artifact.name);
    if (QFileInfo(destination).isSymLink()) return setError(error, tr("Refusing a model destination symlink: %1").arg(destination));
    if (!checkSpace(artifact.size, error)) return false;
    QFile input(source);
    QSaveFile output(destination);
    output.setDirectWriteFallback(false);
    if (!input.open(QIODevice::ReadOnly)) return setError(error, tr("Cannot read %1: %2").arg(source, input.errorString()));
    if (!output.open(QIODevice::WriteOnly)) return setError(error, tr("Cannot write %1: %2").arg(destination, output.errorString()));
    QCryptographicHash hash(QCryptographicHash::Sha256);
    qint64 written = 0;
    while (!input.atEnd()) {
        const QByteArray data = input.read(copyBufferBytes);
        if (input.error() != QFileDevice::NoError || (data.isEmpty() && !input.atEnd()))
            return setError(error, tr("Cannot read model input: %1").arg(input.errorString()));
        if (data.size() > artifact.size - written)
            return setError(error, tr("Model size changed during import: %1").arg(source));
        if (output.write(data) != data.size()) return setError(error, tr("Cannot write model: %1").arg(output.errorString()));
        hash.addData(data);
        written += data.size();
    }
    if (written != artifact.size || hash.result().toHex() != artifact.sha256)
        return setError(error, tr("Model changed or failed checksum validation during publication: %1").arg(source));
    input.close();
    if (!output.commit()) return setError(error, tr("Cannot publish verified model: %1").arg(output.errorString()));
    return true;
}

bool ModelFiles::importFile(const QString& source, const QString& fileName, QString* error)
{
    if (error) error->clear();
    if (busy_) return setError(error, tr("A model operation is already running."));
    if (!validateManifest(error)) return false;
    const auto artifact = std::find_if(artifacts_.cbegin(), artifacts_.cend(), [&](const auto& item) { return item.name == fileName; });
    if (artifact == artifacts_.cend()) return setError(error, tr("Unknown model filename: %1").arg(fileName));
    if (!acquireLock(error)) return false;
    const bool success = verifyArtifact(source, *artifact, error) && publish(source, *artifact, error);
    lock_.reset();
    return success;
}

void ModelFiles::downloadMissing()
{
    if (busy_) return; // An existing operation owns its single finished signal.
    QString error;
    if (!validateManifest(&error) || !acquireLock(&error)) {
        emit finished(false, error);
        return;
    }
    busy_ = true;
    const quint64 operation = ++operation_;
    current_ = 0;
    completedBytes_ = 0;
    totalBytes_ = 0;
    for (const auto& artifact : artifacts_) totalBytes_ += artifact.size;
    emit progress(0, totalBytes_);
    QTimer::singleShot(0, this, [this, operation] {
        if (busy_ && operation_ == operation) startNext();
    });
}

void ModelFiles::startNext()
{
    if (!busy_) return;
    const quint64 operation = operation_;
    QString error;
    while (current_ < artifacts_.size()) {
        const auto& artifact = artifacts_.at(current_);
        const QString destination = QDir(modelDir_).filePath(artifact.name);
        if (verifyArtifact(destination, artifact, nullptr)) {
            completedBytes_ += artifact.size;
            ++current_;
            emit progress(completedBytes_, totalBytes_);
            if (!busy_ || operation_ != operation) return;
            continue;
        }
        const QString partialPath = destination + QStringLiteral(".download");
        if (QFileInfo(destination).isSymLink() || QFileInfo(partialPath).isSymLink()) {
            fail(tr("Refusing a model download symlink: %1").arg(destination));
            return;
        }
        partial_.setFileName(partialPath);
        if (!partial_.open(QIODevice::ReadWrite)) { fail(tr("Cannot open partial model: %1").arg(partial_.errorString())); return; }
        offset_ = partial_.size();
        if (offset_ >= artifact.size) {
            partial_.close();
            if (offset_ == artifact.size && verifyArtifact(partialPath, artifact, nullptr)) {
                if (!publish(partialPath, artifact, &error)) { fail(error); return; }
                QFile::remove(partialPath);
                completedBytes_ += artifact.size;
                ++current_;
                emit progress(completedBytes_, totalBytes_);
                if (!busy_ || operation_ != operation) return;
                continue;
            }
            if (!partial_.open(QIODevice::ReadWrite | QIODevice::Truncate)) { fail(partial_.errorString()); return; }
            offset_ = 0;
        }
        // Reserve both remaining download bytes and atomic publication space.
        if (!checkSpace(artifact.size - offset_ + artifact.size, &error)) { fail(error); return; }
        if (!partial_.seek(offset_)) { fail(partial_.errorString()); return; }
        QNetworkRequest request(artifact.url);
        request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
        request.setMaximumRedirectsAllowed(8);
        request.setTransferTimeout(30000);
        request.setRawHeader("Accept-Encoding", "identity");
        request.setRawHeader("User-Agent", "qcopilots-search-model-downloader/1");
        if (offset_ > 0) request.setRawHeader("Range", "bytes=" + QByteArray::number(offset_) + '-');
        responseAccepted_ = false;
        reply_ = network_->get(request);
        connect(reply_, &QIODevice::readyRead, this, &ModelFiles::readAvailable);
        connect(reply_, &QNetworkReply::finished, this, &ModelFiles::replyFinished);
        emit progress(completedBytes_ + offset_, totalBytes_);
        return;
    }
    complete(true);
}

bool ModelFiles::acceptResponse(bool finalResponse)
{
    if (responseAccepted_) return true;
    if (!reply_) return false;
    const int status = reply_->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    // During an automatic redirect Qt may expose the intermediate headers and
    // body. Only the final response can complete this operation. A terminal 3xx
    // (for example 300 or 304) must fail rather than keep the directory locked.
    if (status >= 300 && status < 400 && !finalResponse) {
        reply_->readAll();
        return false;
    }
    const auto& artifact = artifacts_.at(current_);
    if (status == 200) {
        // Some mirrors ignore Range. Start over before writing a single byte.
        if (offset_ != 0) {
            QString error;
            if (!checkSpace(artifact.size * 2, &error)) { fail(error); return false; }
            if (!partial_.resize(0) || !partial_.seek(0)) { fail(partial_.errorString()); return false; }
            offset_ = 0;
        }
    } else if (status == 206) {
        static const QRegularExpression range(QStringLiteral("^bytes (\\d+)-(\\d+)/(\\d+)$"));
        const auto match = range.match(QString::fromLatin1(reply_->rawHeader("Content-Range")));
        bool firstOk = false, lastOk = false, totalOk = false;
        const qint64 first = match.captured(1).toLongLong(&firstOk);
        const qint64 last = match.captured(2).toLongLong(&lastOk);
        const qint64 total = match.captured(3).toLongLong(&totalOk);
        if (!match.hasMatch() || !firstOk || !lastOk || !totalOk || first != offset_ ||
            last != artifact.size - 1 || total != artifact.size) {
            fail(tr("The server returned an invalid Content-Range for %1.").arg(artifact.name));
            return false;
        }
    } else {
        fail(tr("Model server returned HTTP %1 for %2.").arg(status).arg(artifact.name));
        return false;
    }
    const QByteArray encoding = reply_->rawHeader("Content-Encoding").toLower().trimmed();
    if (!encoding.isEmpty() && encoding != "identity") { fail(tr("Compressed model transfers are not supported.")); return false; }
    const QByteArray length = reply_->rawHeader("Content-Length");
    if (!length.isEmpty()) {
        bool ok = false;
        const qint64 count = length.toLongLong(&ok);
        if (!ok || count != artifact.size - offset_) { fail(tr("Model response has an incorrect Content-Length.")); return false; }
    }
    responseAccepted_ = true;
    return true;
}

void ModelFiles::readAvailable()
{
    if (!busy_ || !reply_ || !acceptResponse()) return;
    const quint64 operation = operation_;
    while (reply_ && operation_ == operation && reply_->bytesAvailable() > 0) {
        const QByteArray data = reply_->read(copyBufferBytes);
        if (data.size() > artifacts_.at(current_).size - partial_.size()) {
            fail(tr("Model response exceeds the expected file size."));
            return;
        }
        if (partial_.write(data) != data.size()) { fail(tr("Cannot save model download: %1").arg(partial_.errorString())); return; }
        emit progress(completedBytes_ + partial_.size(), totalBytes_);
    }
}

void ModelFiles::replyFinished()
{
    if (!busy_ || !reply_) return;
    const quint64 operation = operation_;
    if (reply_->error() != QNetworkReply::NoError) {
        fail(tr("Model transfer failed: %1").arg(reply_->errorString()));
        return;
    }
    if (!acceptResponse(true)) return;
    readAvailable();
    if (!reply_ || !busy_ || operation_ != operation) return;
    reply_->deleteLater();
    reply_.clear();
    const QString partialPath = partial_.fileName();
    if (!partial_.flush()) { fail(partial_.errorString()); return; }
    partial_.close();
    QString error;
    const auto& artifact = artifacts_.at(current_);
    if (!verifyArtifact(partialPath, artifact, &error)) {
        // A full, bad file cannot be resumed. Incomplete files remain resumable.
        if (QFileInfo(partialPath).size() >= artifact.size) QFile::remove(partialPath);
        fail(error);
        return;
    }
    if (!publish(partialPath, artifact, &error)) { fail(error); return; }
    QFile::remove(partialPath);
    completedBytes_ += artifact.size;
    ++current_;
    emit progress(completedBytes_, totalBytes_);
    QTimer::singleShot(0, this, [this, operation] {
        if (busy_ && operation_ == operation) startNext();
    });
}

void ModelFiles::fail(const QString& error) { complete(false, error); }

void ModelFiles::complete(bool success, const QString& error)
{
    if (reply_) {
        disconnect(reply_, nullptr, this, nullptr);
        reply_->abort();
        reply_->deleteLater();
        reply_.clear();
    }
    partial_.close();
    lock_.reset();
    busy_ = false;
    emit finished(success, error);
}

void ModelFiles::cancel()
{
    if (busy_) complete(false, tr("Model download cancelled. Partial files are kept for resuming."));
}

} // namespace qcopilots_search
