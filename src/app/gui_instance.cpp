#include "gui_instance.h"
#include "core/native_lock_path.h"
#include "service/ipc_security.h"

#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QLocalServer>
#include <QLocalSocket>
#include <QLockFile>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QThread>
#include <QTimer>
#include <algorithm>
#include <cstring>

#ifdef Q_OS_WIN
#include <qt_windows.h>
#else
#include <fcntl.h>
#endif

namespace qcopilots_search {
namespace {
constexpr int maxRequestBytes = 128;
const QByteArray showPrefix = QByteArrayLiteral("qcs-gui/1 show ");
const QByteArray shownReply = QByteArrayLiteral("qcs-gui/1 shown\n");

RuntimePaths guiPaths(const RuntimePaths &paths)
{
    auto result = paths;
    // Retain RuntimePaths' user/profile hash and short Unix socket directory.
    // Never append the profile path or replace the shared directory name.
#ifdef Q_OS_WIN
    result.ipcName = QStringLiteral("qcs-gui-") + paths.ipcName.mid(4);
#else
    const QFileInfo endpoint(paths.ipcName);
    result.ipcName = QDir(endpoint.absolutePath()).filePath(
        QStringLiteral("qcs-gui-") + endpoint.fileName().mid(4));
#endif
    result.lockFile = QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui.lock"));
    return result;
}

bool readToken(const QString &path, QByteArray *token, QString *error)
{
    QFile file(path);
#ifdef Q_OS_WIN
    if (QFileInfo(path).isSymbolicLink() || !file.open(QIODevice::ReadOnly)) {
#else
    // The private directory prevents access by other users. Also reject a
    // replaced token, symlink or permissive file before reading any secret.
    const int descriptor = ::open(QFile::encodeName(path).constData(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat state{};
    if (descriptor < 0) {
        if (error) *error = QStringLiteral("Cannot open the GUI authentication token.");
        return false;
    }
    if (::fstat(descriptor, &state) != 0 || !S_ISREG(state.st_mode)
        || state.st_uid != ::geteuid() || (state.st_mode & 0777) != 0600) {
        ::close(descriptor);
        if (error) *error = QStringLiteral("The GUI authentication token must be a private file owned by this user.");
        return false;
    }
    if (!file.open(descriptor, QIODevice::ReadOnly, QFileDevice::AutoCloseHandle)) {
        ::close(descriptor);
#endif
        if (error) *error = QStringLiteral("Cannot read the GUI authentication token: %1").arg(file.errorString());
        return false;
    }
    const auto bytes = file.read(65);
    if (bytes.size() != 64 || std::any_of(bytes.cbegin(), bytes.cend(), [](char c) {
            return !(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f');
        })) {
        if (error) *error = QStringLiteral("The GUI authentication token is invalid.");
        return false;
    }
    *token = bytes;
    return true;
}
}

class GuiInstance::Private {
public:
    Private(GuiInstance *owner, const RuntimePaths &runtimePaths)
        : q(owner), paths(guiPaths(runtimePaths)), lock(nativeLockFilePath(paths.lockFile))
    {
#ifdef Q_OS_WIN
        tokenPath = QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui-token"));
#else
        tokenPath = paths.ipcName + QStringLiteral(".token");
#endif
        lock.setStaleLockTime(0);
        server.setSocketOptions(QLocalServer::UserAccessOption);
        server.setMaxPendingConnections(8);
        QObject::connect(&server, &QLocalServer::newConnection, q, [this] { accept(); });
    }

    ~Private()
    {
        // Keep the lock until every resource owned by this primary is closed.
        // Failed secondary instances never remove another process's state.
        server.close();
        const auto sockets = server.findChildren<QLocalSocket *>();
        for (auto *socket : sockets) delete socket;
        if (!token.isEmpty() && lock.isLocked()) {
            QByteArray current;
            if (readToken(tokenPath, &current, nullptr) && current == token)
                QFile::remove(tokenPath);
        }
        lock.unlock();
    }

    bool listen(QString *error)
    {
        if (!removeStaleIpcSocket(paths, error)) return false;
        const QFileInfo oldToken(tokenPath);
        if (oldToken.isSymbolicLink() || (oldToken.exists() && !oldToken.isFile())) {
            if (error) *error = QStringLiteral("Refusing to replace an unexpected GUI token path: %1").arg(tokenPath);
            return false;
        }
#ifndef Q_OS_WIN
        if (oldToken.exists() && oldToken.ownerId() != ::geteuid()) {
            if (error) *error = QStringLiteral("Refusing to replace another user's GUI token.");
            return false;
        }
#endif
        QByteArray random(32, '\0');
        for (int i = 0; i < random.size(); i += 4) {
            const auto value = QRandomGenerator::system()->generate();
            std::memcpy(random.data() + i, &value, sizeof(value));
        }
        const auto secretBytes = random.toHex();
        QSaveFile secret(tokenPath);
        if (!secret.open(QIODevice::WriteOnly)
            || !secret.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner)
            || secret.write(secretBytes) != secretBytes.size() || !secret.commit()) {
            if (error) *error = QStringLiteral("Cannot create the GUI authentication token: %1").arg(secret.errorString());
            return false;
        }
        token = secretBytes;
        if (!server.listen(paths.ipcName)) {
            if (error) *error = QStringLiteral("Cannot listen on the GUI activation endpoint: %1").arg(server.errorString());
            return false;
        }
        return true;
    }

    void accept()
    {
        while (auto *socket = server.nextPendingConnection()) {
            if (!ipcPeerIsCurrentUser(socket->socketDescriptor())) {
                socket->abort();
                socket->deleteLater();
                continue;
            }
            socket->setReadBufferSize(maxRequestBytes + 1);
            auto *deadline = new QTimer(socket);
            deadline->setSingleShot(true);
            deadline->start(2000);
            QObject::connect(deadline, &QTimer::timeout, socket, [socket] { socket->abort(); socket->deleteLater(); });
            QObject::connect(socket, &QLocalSocket::disconnected, socket, &QObject::deleteLater);
            const auto incoming = std::make_shared<QByteArray>();
            const auto complete = std::make_shared<bool>(false);
            auto read = [this, socket, deadline, incoming, complete] {
                if (*complete) return;
                incoming->append(socket->readAll());
                if (incoming->size() > maxRequestBytes
                    || (incoming->contains('\n') && *incoming != showPrefix + token + '\n')) {
                    *complete = true;
                    socket->abort();
                    return;
                }
                if (!incoming->endsWith('\n')) return;
                *complete = true;
                emit q->showRequested();
                if (socket->write(shownReply) != shownReply.size()) {
                    socket->abort();
                    return;
                }
                // Let the client close after consuming its ACK. On Windows,
                // disconnectFromServer can discard unread named-pipe bytes
                // once Qt's write buffer is empty. Bound abandoned clients.
                deadline->start();
            };
            QObject::connect(socket, &QLocalSocket::readyRead, socket, read);
            read();
        }
    }

    GuiInstance *q;
    RuntimePaths paths;
    QString tokenPath;
    QLockFile lock;
    QLocalServer server;
    QByteArray token;
    bool attempted = false;
};

GuiInstance::GuiInstance(const RuntimePaths &paths, QObject *parent)
    : QObject(parent), d(std::make_unique<Private>(this, paths)) {}
GuiInstance::~GuiInstance() = default;
QString GuiInstance::endpointName() const { return d->paths.ipcName; }

GuiInstance::Result GuiInstance::start(QString *error, int timeoutMs)
{
    if (error) error->clear();
    if (d->attempted) {
        if (error) *error = QStringLiteral("GUI instance startup was already attempted.");
        return Result::Failed;
    }
    d->attempted = true;
    if (!d->paths.ensure(error)) return Result::Failed;
    QElapsedTimer elapsed;
    elapsed.start();
    timeoutMs = qMax(1, timeoutMs);
    QString lastError;
    do {
        if (d->lock.tryLock(0)) return d->listen(error) ? Result::Primary : Result::Failed;
        if (d->lock.error() != QLockFile::LockFailedError) {
            if (error) *error = QStringLiteral("Cannot acquire the GUI profile lock (error %1): %2")
                .arg(int(d->lock.error())).arg(d->paths.lockFile);
            return Result::Failed;
        }
        if (!d->paths.validateIpcDirectory(error)) return Result::Failed;
        QLocalSocket socket;
        socket.setReadBufferSize(maxRequestBytes + 1);
        socket.connectToServer(d->paths.ipcName);
        if (socket.waitForConnected(qMax(1, qMin(100, timeoutMs - int(elapsed.elapsed()))))) {
            if (!ipcPeerIsCurrentUser(socket.socketDescriptor(), error)) return Result::Failed;
#ifdef Q_OS_WIN
            // The second Explorer launch can grant the foreground privilege to
            // the existing GUI. Check the pipe server against our lock owner
            // first, so an unrelated endpoint cannot receive that privilege.
            ULONG processId = 0;
            qint64 lockProcessId = 0;
            QString lockHost, lockApplication;
            if (!GetNamedPipeServerProcessId(reinterpret_cast<HANDLE>(socket.socketDescriptor()), &processId)
                || !d->lock.getLockInfo(&lockProcessId, &lockHost, &lockApplication)
                || lockProcessId != processId) {
                if (error) *error = QStringLiteral("The GUI activation server does not own this profile's lock.");
                return Result::Failed;
            }
#endif
            QByteArray token;
            if (!readToken(d->tokenPath, &token, error)) return Result::Failed;
#ifdef Q_OS_WIN
            AllowSetForegroundWindow(processId);
#endif
            const auto request = showPrefix + token + '\n';
            if (socket.write(request) != request.size()
                || (socket.bytesToWrite() > 0 && !socket.waitForBytesWritten(
                    qMax(1, timeoutMs - int(elapsed.elapsed()))))) {
                if (error) *error = QStringLiteral("Cannot send the GUI activation request: %1").arg(socket.errorString());
                return Result::Failed;
            }
            QByteArray reply;
            while (!reply.contains('\n') && elapsed.elapsed() < timeoutMs) {
                if (socket.bytesAvailable() == 0
                    && !socket.waitForReadyRead(qMax(1, timeoutMs - int(elapsed.elapsed())))) break;
                reply += socket.readAll();
                if (reply.size() > maxRequestBytes) break;
            }
            if (reply == shownReply) {
                socket.disconnectFromServer();
                return Result::ActivatedExisting;
            }
            if (error) *error = QStringLiteral("The existing GUI did not acknowledge window activation. %1").arg(socket.errorString());
            return Result::Failed;
        }
        lastError = socket.errorString();
        // A live owner may still be between lock acquisition and listen.
        // Never create a second GUI just because the first connection failed.
        if (elapsed.elapsed() < timeoutMs) QThread::msleep(25);
    } while (elapsed.elapsed() < timeoutMs);
    if (error) *error = QStringLiteral("Another GUI owns this profile but did not become ready: %1").arg(lastError);
    return Result::Failed;
}
} // namespace qcopilots_search
