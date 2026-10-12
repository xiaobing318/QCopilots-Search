#include "service_client.h"

#include "core/types.h"
#include "ipc_protocol.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QUuid>

#ifdef Q_OS_WIN
#include <qt_windows.h>
#endif

namespace qcopilots_search {

ServiceClient::ServiceClient(const RuntimePaths &paths, QObject *parent)
    : QObject(parent), paths_(paths)
{
    socket_.setReadBufferSize(maxMessageBytes + 4);
    connectionDeadline_.setSingleShot(true);
    retryTimer_.setSingleShot(true);
    retryTimer_.setInterval(150);
    connect(&socket_, &QLocalSocket::connected, this, &ServiceClient::socketConnected);
    connect(&socket_, &QLocalSocket::disconnected, this, &ServiceClient::socketDisconnected);
    connect(&socket_, &QLocalSocket::errorOccurred, this, &ServiceClient::socketError);
    connect(&socket_, &QLocalSocket::readyRead, this, &ServiceClient::readAvailable);
    connect(&retryTimer_, &QTimer::timeout, this, &ServiceClient::attemptConnection);
    connect(&connectionDeadline_, &QTimer::timeout, this, [this] {
        ensureMode_ = false;
        retryTimer_.stop();
        intentionalDisconnect_ = true;
        socket_.abort();
        intentionalDisconnect_ = false;
        const QString detail = lastConnectionError_.isEmpty()
            ? tr("The local service did not become ready before the deadline.")
            : tr("The local service did not become ready: %1").arg(lastConnectionError_);
        failPending(QStringLiteral("connection_timeout"), detail);
    });
}

ServiceClient::~ServiceClient()
{
    intentionalDisconnect_ = true;
    connectionDeadline_.stop();
    retryTimer_.stop();
    socket_.abort();
}

bool ServiceClient::isConnected() const
{
    return ready_ && socket_.state() == QLocalSocket::ConnectedState;
}

void ServiceClient::connectToService()
{
    if (isConnected() || connectionDeadline_.isActive())
        return;
    ensureMode_ = false;
    launching_ = false;
    lastConnectionError_.clear();
    connectionDeadline_.start(5000);
    attemptConnection();
}

void ServiceClient::ensureService(int timeoutMs)
{
    if (isConnected())
        return;
    if (connectionDeadline_.isActive()) {
        ensureMode_ = autoStart_;
        return;
    }
    if (!autoStart_) {
        ensureMode_ = false;
        launching_ = false;
        lastConnectionError_.clear();
        connectionDeadline_.start(qMax(1, timeoutMs));
        attemptConnection();
        return;
    }
    QString error;
    if (!paths_.ensure(&error)) {
        // Keep errors asynchronous so callers can retain the request ID first.
        QTimer::singleShot(0, this, [this, error] {
            failPending(QStringLiteral("profile_unavailable"), error);
        });
        return;
    }
    ensureMode_ = true;
    launching_ = false;
    lastConnectionError_.clear();
    connectionDeadline_.start(qMax(1, timeoutMs));
    attemptConnection();
}

void ServiceClient::attemptConnection()
{
    if (!connectionDeadline_.isActive() || isConnected()
        || socket_.state() == QLocalSocket::ConnectingState)
        return;
    intentionalDisconnect_ = true;
    socket_.abort();
    intentionalDisconnect_ = false;
    incoming_.clear();
    token_.clear();
    socket_.connectToServer(paths_.ipcName, QIODevice::ReadWrite);
}

bool ServiceClient::readToken(QString *error)
{
    QFile file(QDir(paths_.profileDir).filePath(QStringLiteral("qcopilots-search-ipc-token")));
    if (!file.open(QIODevice::ReadOnly)) {
        if (error)
            *error = tr("Cannot read the local service authentication token: %1")
                         .arg(file.errorString());
        return false;
    }
    if (file.size() > 4096) {
        if (error)
            *error = tr("The local service authentication token is too large.");
        return false;
    }
    const auto bytes = file.read(4097).trimmed();
    if (bytes.size() < 32 || bytes.size() > 4096 || bytes.contains('\0')) {
        if (error)
            *error = tr("The local service authentication token is invalid.");
        return false;
    }
    token_ = QString::fromUtf8(bytes);
    if (token_.toUtf8() != bytes) {
        token_.clear();
        if (error)
            *error = tr("The local service authentication token has invalid encoding.");
        return false;
    }
    return true;
}

void ServiceClient::socketConnected()
{
    QString error;
    if (!readToken(&error)) {
        lastConnectionError_ = error;
        intentionalDisconnect_ = true;
        socket_.abort();
        intentionalDisconnect_ = false;
        if (ensureMode_ && connectionDeadline_.isActive()) {
            retryTimer_.start();
        } else {
            connectionDeadline_.stop();
            failPending(QStringLiteral("authentication_unavailable"), error);
        }
        return;
    }
    connectionDeadline_.stop();
    retryTimer_.stop();
    ensureMode_ = false;
    ready_ = true;
    emit connected();
    sendQueuedRequests();
}

void ServiceClient::socketDisconnected()
{
    const bool wasReady = ready_;
    ready_ = false;
    incoming_.clear();
    token_.clear();
    if (wasReady)
        emit disconnected();
    if (intentionalDisconnect_)
        return;
    if (ensureMode_ && connectionDeadline_.isActive()) {
        retryTimer_.start();
        return;
    }
    if (wasReady)
        failPending(QStringLiteral("connection_closed"), tr("The local service disconnected."));
}

void ServiceClient::socketError(QLocalSocket::LocalSocketError)
{
    if (intentionalDisconnect_)
        return;
    lastConnectionError_ = socket_.errorString();
    if (ensureMode_ && connectionDeadline_.isActive()) {
        if (!launching_) {
            launching_ = true;
            QString error;
            if (!launchService(&error)) {
                ensureMode_ = false;
                connectionDeadline_.stop();
                retryTimer_.stop();
                failPending(QStringLiteral("service_start_failed"), error);
                return;
            }
        }
        retryTimer_.start();
        return;
    }
    if (!ready_) {
        connectionDeadline_.stop();
        failPending(QStringLiteral("connection_failed"), lastConnectionError_);
    }
}

bool ServiceClient::launchService(QString *error)
{
    QString name = QStringLiteral("qcopilots-search-service");
#ifdef Q_OS_WIN
    name += QStringLiteral(".exe");
#endif
    const QDir executableDirectory(QCoreApplication::applicationDirPath());
    QString executable;
    for (const auto &candidate : {executableDirectory.filePath(name),
                                 executableDirectory.filePath(QStringLiteral("../Helpers/") + name),
                                 executableDirectory.filePath(QStringLiteral("../bin/") + name)}) {
        if (QFileInfo(candidate).isFile() && QFileInfo(candidate).isExecutable()) {
            executable = QFileInfo(candidate).absoluteFilePath();
            break;
        }
    }
    if (executable.isEmpty()) {
        if (error)
            *error = tr("The local service executable is missing next to %1.")
                         .arg(QCoreApplication::applicationFilePath());
        return false;
    }
    QProcess process;
    process.setProgram(executable);
    process.setArguments({QStringLiteral("--profile-dir"), paths_.profileDir,
                          QStringLiteral("--model-dir"), paths_.modelDir});
    process.setWorkingDirectory(QCoreApplication::applicationDirPath());
    process.setStandardInputFile(QProcess::nullDevice());
    process.setStandardOutputFile(QProcess::nullDevice());
    process.setStandardErrorFile(QProcess::nullDevice());
#ifdef Q_OS_WIN
    process.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *args) {
        args->flags |= CREATE_NO_WINDOW;
    });
#endif
    if (!process.startDetached()) {
        if (error)
            *error = tr("Cannot start the local service: %1").arg(process.errorString());
        return false;
    }
    return true;
}

QString ServiceClient::request(const QString &method, const QJsonObject &params, int timeoutMs)
{
    const auto id = QUuid::createUuid().toString(QUuid::WithoutBraces);
    if (method.isEmpty() || method.size() > 128 || pending_.size() >= 256) {
        const bool overloaded = pending_.size() >= 256;
        QTimer::singleShot(0, this, [this, id, overloaded] {
            emit failed(id, overloaded ? QStringLiteral("too_many_requests")
                                       : QStringLiteral("invalid_method"),
                        overloaded ? tr("Too many local service requests are pending.")
                                   : tr("The local service method name is invalid."));
        });
        return id;
    }
    auto *timer = new QTimer(this);
    timer->setSingleShot(true);
    PendingRequest pending;
    pending.message = {{QStringLiteral("protocol"), protocolVersion},
                       {QStringLiteral("id"), id},
                       {QStringLiteral("method"), method},
                       {QStringLiteral("params"), params}};
    pending.timer = timer;
    pending_.insert(id, pending);
    connect(timer, &QTimer::timeout, this, [this, id] {
        const auto it = pending_.constFind(id);
        if (it == pending_.cend())
            return;
        if (it->sent)
            sendCancellation(id);
        finishFailure(id, QStringLiteral("request_timeout"), tr("The local service request timed out."));
    });
    timer->start(qMax(1, timeoutMs));
    // All completion paths happen after returning the ID, including a local
    // framing error or failure to launch an installed service.
    QTimer::singleShot(0, this, [this] {
        if (isConnected())
            sendQueuedRequests();
        else if (!pending_.isEmpty())
            ensureService();
    });
    return id;
}

bool ServiceClient::sendMessage(QJsonObject message, QString *error)
{
    if (!isConnected()) {
        if (error)
            *error = tr("The local service is disconnected.");
        return false;
    }
    message.insert(QStringLiteral("token"), token_);
    const auto frame = encodeIpcFrame(message, error);
    if (frame.isEmpty())
        return false;
    if (socket_.bytesToWrite() + frame.size() > maxMessageBytes * 2) {
        if (error)
            *error = tr("The local service write queue is full.");
        return false;
    }
    if (socket_.write(frame) != frame.size()) {
        if (error)
            *error = socket_.errorString();
        return false;
    }
    return true;
}

void ServiceClient::sendQueuedRequests()
{
    const auto ids = pending_.keys();
    for (const auto &id : ids) {
        auto it = pending_.find(id);
        if (it == pending_.end() || it->sent || !isConnected())
            continue;
        QString error;
        if (sendMessage(it->message, &error)) {
            // A device error may synchronously invoke slots that remove a
            // pending request, so do not retain an iterator across write().
            it = pending_.find(id);
            if (it != pending_.end())
                it->sent = true;
        } else {
            finishFailure(id, QStringLiteral("send_failed"), error);
        }
    }
}

void ServiceClient::readAvailable()
{
    while (socket_.bytesAvailable() > 0) {
        incoming_.append(socket_.read(64 * 1024));
        QList<QJsonObject> messages;
        QString error;
        if (!takeIpcFrames(incoming_, messages, &error)) {
            intentionalDisconnect_ = true;
            socket_.abort();
            intentionalDisconnect_ = false;
            failPending(QStringLiteral("protocol_error"), error);
            return;
        }
        for (const auto &message : messages) {
            if (message.contains(QStringLiteral("event"))) {
                const auto name = message.value(QStringLiteral("event")).toString();
                if (!name.isEmpty() && message.value(QStringLiteral("data")).isObject())
                    emit eventReceived(name, message.value(QStringLiteral("data")).toObject());
                continue;
            }
            const auto id = message.value(QStringLiteral("id")).toString();
            auto it = pending_.find(id);
            if (it == pending_.end())
                continue; // A response to an already canceled or expired request.
            if (!message.value(QStringLiteral("ok")).isBool()) {
                finishFailure(id, QStringLiteral("protocol_error"), tr("The local service response is malformed."));
                continue;
            }
            if (!message.value(QStringLiteral("ok")).toBool()) {
                const auto failure = message.value(QStringLiteral("error")).toObject();
                finishFailure(id, failure.value(QStringLiteral("code")).toString(QStringLiteral("service_error")),
                              failure.value(QStringLiteral("message")).toString(tr("The local service request failed.")));
                continue;
            }
            if (!message.value(QStringLiteral("result")).isObject()) {
                finishFailure(id, QStringLiteral("protocol_error"), tr("The local service returned an invalid result."));
                continue;
            }
            it->timer->stop();
            it->timer->deleteLater();
            pending_.erase(it);
            emit response(id, message.value(QStringLiteral("result")).toObject());
        }
    }
}

void ServiceClient::finishFailure(const QString &id, const QString &code, const QString &message)
{
    auto it = pending_.find(id);
    if (it == pending_.end())
        return;
    it->timer->stop();
    it->timer->deleteLater();
    pending_.erase(it);
    emit failed(id, code, message);
}

void ServiceClient::failPending(const QString &code, const QString &message)
{
    const auto ids = pending_.keys();
    if (ids.isEmpty())
        emit failed({}, code, message);
    for (const auto &id : ids)
        finishFailure(id, code, message);
}

void ServiceClient::sendCancellation(const QString &id)
{
    if (!isConnected())
        return;
    sendMessage({{QStringLiteral("protocol"), protocolVersion},
                 {QStringLiteral("id"), QUuid::createUuid().toString(QUuid::WithoutBraces)},
                 {QStringLiteral("method"), QStringLiteral("cancel")},
                 {QStringLiteral("params"), QJsonObject{{QStringLiteral("id"), id}}}});
}

void ServiceClient::cancel(const QString &requestId)
{
    const auto it = pending_.constFind(requestId);
    if (it == pending_.cend())
        return;
    if (it->sent)
        sendCancellation(requestId);
    finishFailure(requestId, QStringLiteral("cancelled"), tr("The local service request was canceled."));
}

void ServiceClient::disconnectFromService()
{
    connectionDeadline_.stop();
    retryTimer_.stop();
    ensureMode_ = false;
    intentionalDisconnect_ = true;
    socket_.abort();
    intentionalDisconnect_ = false;
    incoming_.clear();
    token_.clear();
    if (!pending_.isEmpty())
        failPending(QStringLiteral("cancelled"), tr("The local service connection was closed by the client."));
}

} // namespace qcopilots_search
