#pragma once

#include "core/runtime_paths.h"

#include <QFile>
#include <QString>
#include <QtGlobal>
#include <limits>

#ifndef Q_OS_WIN
#include <cerrno>
#include <cstring>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

namespace qcopilots_search {
namespace ipc_security_detail {
inline bool matchesUser(quint64 peer, quint64 expected, QString *error)
{
    if (peer == expected)
        return true;
    if (error)
        *error = QStringLiteral("The IPC peer belongs to a different operating-system user.");
    return false;
}
}

// Verify both endpoints before exchanging an authentication token or request.
// macOS ignores Unix socket access mode bits, so UserAccessOption alone cannot
// enforce the same-user contract. Windows keeps its named-pipe access control.
inline bool ipcPeerIsCurrentUser(qintptr descriptor, QString *error = nullptr)
{
    if (error)
        error->clear();
#ifdef Q_OS_WIN
    Q_UNUSED(descriptor);
    return true;
#else
    if (descriptor < 0 || descriptor > std::numeric_limits<int>::max()) {
        if (error)
            *error = QStringLiteral("Cannot verify an invalid IPC socket descriptor.");
        return false;
    }
    uid_t peerUser = static_cast<uid_t>(-1);
#if defined(Q_OS_MACOS)
    gid_t peerGroup = static_cast<gid_t>(-1);
    if (::getpeereid(static_cast<int>(descriptor), &peerUser, &peerGroup) != 0) {
        const int code = errno;
        if (error)
            *error = QStringLiteral("Cannot verify the IPC peer identity: %1")
                         .arg(QString::fromLocal8Bit(std::strerror(code)));
        return false;
    }
#elif defined(Q_OS_LINUX) && defined(SO_PEERCRED)
    // The Linux SO_PEERCRED ABI is pid_t, uid_t, gid_t. Declaring the layout
    // locally avoids requiring a GNU feature-test macro in consumers' headers.
    struct Credentials { pid_t process; uid_t user; gid_t group; } credentials{};
    socklen_t size = sizeof(credentials);
    if (::getsockopt(static_cast<int>(descriptor), SOL_SOCKET, SO_PEERCRED,
                     &credentials, &size) != 0) {
        const int code = errno;
        if (error)
            *error = QStringLiteral("Cannot verify the IPC peer identity: %1")
                         .arg(QString::fromLocal8Bit(std::strerror(code)));
        return false;
    }
    if (size != sizeof(credentials)) {
        if (error)
            *error = QStringLiteral("The operating system returned invalid IPC peer credentials.");
        return false;
    }
    peerUser = credentials.user;
#else
    if (error)
        *error = QStringLiteral("IPC peer identity verification is unavailable on this platform.");
    return false;
#endif
    return ipc_security_detail::matchesUser(static_cast<quint64>(peerUser),
                                            static_cast<quint64>(::geteuid()), error);
#endif
}

// The caller must hold this profile's QLockFile. Never remove a shared parent,
// another profile's endpoint, or an unexpected file at the endpoint path.
inline bool removeStaleIpcSocket(const RuntimePaths &paths, QString *error = nullptr)
{
    if (error)
        error->clear();
#ifdef Q_OS_WIN
    Q_UNUSED(paths);
    return true;
#else
    if (!paths.validateIpcDirectory(error))
        return false;
    const auto path = QFile::encodeName(paths.ipcName);
    struct stat metadata{};
    if (::lstat(path.constData(), &metadata) != 0) {
        const int code = errno;
        if (code == ENOENT)
            return true;
        if (error)
            *error = QStringLiteral("Cannot inspect the IPC endpoint %1: %2")
                         .arg(paths.ipcName, QString::fromLocal8Bit(std::strerror(code)));
        return false;
    }
    if (!S_ISSOCK(metadata.st_mode) || metadata.st_uid != ::geteuid()) {
        if (error)
            *error = QStringLiteral("Refusing to remove an IPC endpoint that is not a socket owned by this user: %1")
                         .arg(paths.ipcName);
        return false;
    }
    if (::unlink(path.constData()) != 0) {
        const int code = errno;
        if (error)
            *error = QStringLiteral("Cannot remove the stale IPC endpoint %1: %2")
                         .arg(paths.ipcName, QString::fromLocal8Bit(std::strerror(code)));
        return false;
    }
    return true;
#endif
}

} // namespace qcopilots_search
