#include "runtime_paths.h"

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
        selectedProfile = QDir(base).filePath(QStringLiteral("qcopilots-search"));
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
    paths.configFile = root.filePath(QStringLiteral("qcopilots-search-settings.json"));
    paths.lockFile = root.filePath(QStringLiteral("qcopilots-search-service.lock"));
    QString identityPath = paths.profileDir;
#ifdef Q_OS_WIN
    identityPath = identityPath.toCaseFolded();
#endif
    const QByteArray identity = currentUserIdentity().toUtf8() + '\0' + identityPath.toUtf8();
    paths.ipcName = QStringLiteral("qcopilots-search-")
        + QString::fromLatin1(QCryptographicHash::hash(identity, QCryptographicHash::Sha256)
                                 .toHex().left(40));
    return paths;
}

bool RuntimePaths::ensure(QString *error) const
{
    if (error)
        error->clear();
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
    return true;
}

QString RuntimePaths::databaseFile() const
{
    return QDir(indexDir).filePath(QStringLiteral("qcopilots-search.sqlite3"));
}
} // namespace qcopilots_search
