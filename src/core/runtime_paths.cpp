#include "runtime_paths.h"
#include "ipc_directory.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QStandardPaths>
#include <QStringList>

#ifdef Q_OS_WIN
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <sddl.h>
#else
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#endif

namespace qcopilots_search {
namespace {
QString canonicalWithMissingTail(const QString &value)
{
    QString current = QDir::cleanPath(QFileInfo(value).absoluteFilePath());
    QStringList tail;
    while (!QFileInfo::exists(current)) {
        const QFileInfo info(current);
        const QString parent = info.absolutePath();
        if (parent == current)
            break;
        tail.prepend(info.fileName());
        current = parent;
    }
    const QString canonical = QFileInfo(current).canonicalFilePath();
    if (!canonical.isEmpty())
        current = canonical;
    for (const QString &component : tail)
        current = QDir(current).filePath(component);
    return QDir::cleanPath(current);
}

QString currentUserIdentity()
{
#ifdef Q_OS_WIN
    HANDLE token = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        DWORD size = 0;
        GetTokenInformation(token, TokenUser, nullptr, 0, &size);
        QByteArray storage(static_cast<qsizetype>(size), '\0');
        if (GetTokenInformation(token, TokenUser, storage.data(), size, &size)) {
            const auto *user = reinterpret_cast<const TOKEN_USER *>(storage.constData());
            LPWSTR sid = nullptr;
            if (ConvertSidToStringSidW(user->User.Sid, &sid)) {
                const QString result = QString::fromWCharArray(sid);
                LocalFree(sid);
                CloseHandle(token);
                return result;
            }
        }
        CloseHandle(token);
    }
    // This fallback is only a namespace discriminator. Access enforcement is
    // performed by the IPC server and filesystem, never by this hash.
    return qEnvironmentVariable("USERDOMAIN") + QLatin1Char('/')
           + qEnvironmentVariable("USERNAME");
#else
    return QString::number(static_cast<qulonglong>(geteuid()));
#endif
}

#ifndef Q_OS_WIN
bool checkIpcEndpoint(const QString &endpoint, QString *error)
{
    const sockaddr_un address{};
    const QByteArray encoded = QFile::encodeName(endpoint);
    // Qt reserves two bytes on Linux for abstract namespace compatibility,
    // even though this application uses a filesystem socket on every Unix.
#ifdef Q_OS_LINUX
    constexpr int terminators = 2;
#else
    constexpr int terminators = 1;
#endif
    if (!QDir::isAbsolutePath(endpoint) || encoded.contains('\0')
        || encoded.size() + terminators > qsizetype(sizeof(address.sun_path))) {
        if (error) *error = QStringLiteral("Invalid local IPC endpoint (%1 encoded bytes): %2")
            .arg(encoded.size()).arg(endpoint);
        return false;
    }
    return true;
}
#endif
}

bool detail::preparePrivateIpcDirectory(const QString &directory, bool create, QString *error)
{
    if (error) error->clear();
#ifdef Q_OS_WIN
    Q_UNUSED(directory);
    Q_UNUSED(create);
    return true;
#else
    const QFileInfo target(directory);
    if (!QDir::isAbsolutePath(directory) || target.fileName().isEmpty()
        || target.fileName() == "." || target.fileName() == "..") {
        if (error) *error = QStringLiteral("An absolute private IPC directory is required: %1").arg(directory);
        return false;
    }
    // /tmp is a system symlink to /private/tmp on macOS. Resolve this parent,
    // then open it without following a replacement final component.
    const QString parentPath = QFileInfo(target.absolutePath()).canonicalFilePath();
    const int parent = ::open(QFile::encodeName(parentPath).constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (parent < 0) {
        if (error) *error = QStringLiteral("Cannot open IPC parent directory %1: %2")
            .arg(target.absolutePath(), QString::fromLocal8Bit(std::strerror(errno)));
        return false;
    }
    struct CloseDescriptor { int fd; ~CloseDescriptor() { ::close(fd); } } closeParent{parent};
    struct stat parentState{};
    if (::fstat(parent, &parentState) != 0 || !S_ISDIR(parentState.st_mode)
        || (parentState.st_uid != 0 && parentState.st_uid != ::geteuid())
        || ((parentState.st_mode & (S_IWGRP | S_IWOTH)) && !(parentState.st_mode & S_ISVTX))) {
        if (error) *error = QStringLiteral("IPC parent must be owned by this user or root and protect directory entries: %1").arg(parentPath);
        return false;
    }
    const auto name = QFile::encodeName(target.fileName());
    if (create && ::mkdirat(parent, name.constData(), 0700) != 0 && errno != EEXIST) {
        if (error) *error = QStringLiteral("Cannot create private IPC directory %1: %2")
            .arg(directory, QString::fromLocal8Bit(std::strerror(errno)));
        return false;
    }
    const int child = ::openat(parent, name.constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (child < 0) {
        if (error) *error = QStringLiteral("Cannot open private IPC directory without following links %1: %2")
            .arg(directory, QString::fromLocal8Bit(std::strerror(errno)));
        return false;
    }
    CloseDescriptor closeChild{child};
    struct stat state{};
    if (::fstat(child, &state) != 0 || !S_ISDIR(state.st_mode)
        || state.st_uid != ::geteuid() || (state.st_mode & 0777) != 0700) {
        if (error) *error = QStringLiteral("Private IPC directory must belong to this user with permissions 0700: %1").arg(directory);
        return false;
    }
    return true;
#endif
}

RuntimePaths RuntimePaths::resolve(const QString &profile, const QString &models)
{
    RuntimePaths paths;
    QString selectedProfile = profile;
    if (selectedProfile.isEmpty())
        selectedProfile = qEnvironmentVariable("QCOPILOTS_SEARCH_PROFILE_DIR");
    if (selectedProfile.isEmpty()) {
        const QString base = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
        if (base.isEmpty())
            return paths; // ensure() reports the unavailable platform directory.
        selectedProfile = QDir(base).filePath(QStringLiteral("qcs"));
    }
    paths.profileDir = canonicalWithMissingTail(selectedProfile);
    QString selectedModels = models;
    if (selectedModels.isEmpty())
        selectedModels = qEnvironmentVariable("QCOPILOTS_SEARCH_MODEL_DIR");
    paths.modelDir = selectedModels.isEmpty()
        ? QDir(paths.profileDir).filePath(QStringLiteral("models"))
        : canonicalWithMissingTail(selectedModels);
    const QDir root(paths.profileDir);
    paths.indexDir = root.filePath(QStringLiteral("index"));
    paths.cacheDir = root.filePath(QStringLiteral("cache"));
    paths.logDir = root.filePath(QStringLiteral("logs"));
    paths.tempDir = root.filePath(QStringLiteral("tmp"));
    paths.configFile = root.filePath(QStringLiteral("qcs-settings.json"));
    paths.lockFile = root.filePath(QStringLiteral("qcs-service.lock"));
    QString identityPath = paths.profileDir;
#ifdef Q_OS_WIN
    identityPath = identityPath.toCaseFolded();
#endif
    const QByteArray identity = currentUserIdentity().toUtf8() + '\0' + identityPath.toUtf8();
    paths.ipcName = QStringLiteral("qcs-")
        + QString::fromLatin1(QCryptographicHash::hash(identity, QCryptographicHash::Sha256)
                                 .toHex().left(40));
#ifndef Q_OS_WIN
    // Socket addresses have a much smaller byte budget than filesystem paths.
    // Never derive this location from TMPDIR, the profile or the current cwd.
    const QString ipcRoot = QStringLiteral("/tmp/qcs-%1").arg(currentUserIdentity());
    paths.ipcName = QDir(ipcRoot).filePath(paths.ipcName);
#endif
    return paths;
}

bool RuntimePaths::ensureIpcDirectory(QString *error) const
{
    if (error) error->clear();
#ifdef Q_OS_WIN
    return true;
#else
    return checkIpcEndpoint(ipcName, error)
        && detail::preparePrivateIpcDirectory(QFileInfo(ipcName).absolutePath(), true, error);
#endif
}

bool RuntimePaths::validateIpcDirectory(QString *error) const
{
    if (error) error->clear();
#ifdef Q_OS_WIN
    return true;
#else
    return checkIpcEndpoint(ipcName, error)
        && detail::preparePrivateIpcDirectory(QFileInfo(ipcName).absolutePath(), false, error);
#endif
}

bool RuntimePaths::ensure(QString *error) const
{
    if (!validateProfileLayout(error))
        return false;
    const QStringList directories{profileDir, indexDir, cacheDir, logDir, tempDir, modelDir};
    for (qsizetype index = 0; index < directories.size(); ++index) {
        const QString &directory = directories.at(index);
        if (directory.isEmpty() || !QDir::isAbsolutePath(directory)) {
            if (error)
                *error = QStringLiteral("An absolute runtime directory is required: %1").arg(directory);
            return false;
        }
        const QFileInfo before(directory);
        if (before.exists() && !before.isDir()) {
            if (error)
                *error = QStringLiteral("Runtime path is not a directory: %1").arg(directory);
            return false;
        }
        if (!QDir().mkpath(directory)) {
            if (error)
                *error = QStringLiteral("Cannot create runtime directory: %1").arg(directory);
            return false;
        }
#ifndef Q_OS_WIN
        if (!before.exists()
            && !QFile::setPermissions(directory, QFileDevice::ReadOwner
                  | QFileDevice::WriteOwner | QFileDevice::ExeOwner)) {
            if (error)
                *error = QStringLiteral("Cannot restrict runtime directory permissions: %1").arg(directory);
            return false;
        }
#endif
        // A verified shared model cache may be read-only. Import/download code
        // checks write access only when it actually needs to change that cache.
        const bool isModelDirectory = index == directories.size() - 1;
        if (!isModelDirectory && !QFileInfo(directory).isWritable()) {
            if (error)
                *error = QStringLiteral("Runtime directory is not writable: %1").arg(directory);
            return false;
        }
        if (isModelDirectory && !QFileInfo(directory).isReadable()) {
            if (error)
                *error = QStringLiteral("Model directory is not readable: %1").arg(directory);
            return false;
        }
    }
    return ensureIpcDirectory(error);
}

bool RuntimePaths::validateProfileLayout(QString *error) const
{
    if (error)
        error->clear();
    if (profileDir.isEmpty() || !QDir::isAbsolutePath(profileDir)) {
        if (error)
            *error = QStringLiteral("An absolute runtime directory is required: %1").arg(profileDir);
        return false;
    }
    if (indexDir.isEmpty() || !QDir::isAbsolutePath(indexDir)) {
        if (error)
            *error = QStringLiteral("An absolute runtime directory is required: %1").arg(indexDir);
        return false;
    }

    // A new lock/IPC namespace must never start a second writer in an old
    // profile. Inspect only the known persistent entry points, including
    // dangling links, before ensure() creates any new layout directories.
    const QDir root(profileDir);
    QStringList legacyPaths;
    for (const auto &name : {QStringLiteral("qcopilots-search-settings.json"),
                             QStringLiteral("qcopilots-search-service.lock"),
                             QStringLiteral("qcopilots-search-ipc-token")}) {
        const QFileInfo entry(root.filePath(name));
        if (entry.exists() || entry.isSymbolicLink())
            legacyPaths.append(entry.filePath());
    }
    const QDir index(indexDir);
    const auto entries = index.entryInfoList(QDir::AllEntries | QDir::Hidden | QDir::System
                                             | QDir::NoDotAndDotDot, QDir::Name);
    for (const QFileInfo &entry : entries) {
        const QString name = entry.fileName();
        if (name == QStringLiteral("qcopilots-search.sqlite3")
            || name.startsWith(QStringLiteral("qcopilots-search.sqlite3-"))
            || name == QStringLiteral("qcopilots-search-progress.json")
            || name.startsWith(QStringLiteral("qcopilots-search-ann-")))
            legacyPaths.append(entry.filePath());
    }
    if (!legacyPaths.isEmpty()) {
        if (error)
            *error = QStringLiteral("legacy_profile_layout: This directory contains an older profile (%1). "
                                    "Select a new empty --profile-dir. Existing files were not changed.")
                         .arg(legacyPaths.first());
        return false;
    }
    return true;
}

QString RuntimePaths::databaseFile() const
{
    return QDir(indexDir).filePath(QStringLiteral("qcs.sqlite3"));
}
} // namespace qcopilots_search
