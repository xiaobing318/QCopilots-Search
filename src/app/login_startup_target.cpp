#include "login_startup_target.h"
#include "core/runtime_paths.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLockFile>
#include <QSaveFile>

namespace qcopilots_search {
namespace {

bool fail(QString *error, const QString &message)
{
    if (error) *error = message;
    return false;
}

bool validLocator(const QString &locatorFile, QString *error)
{
    const QFileInfo info(locatorFile);
    if (locatorFile.isEmpty() || locatorFile.contains(QChar(0))
        || !RuntimePaths::isAbsoluteFilesystemPath(locatorFile) || info.fileName().isEmpty()
        || info.isSymbolicLink() || (info.exists() && !info.isFile()))
        return fail(error, QStringLiteral("An absolute regular login startup locator file is required: %1")
                               .arg(locatorFile));
    return true;
}

bool normalizeTarget(const LoginStartupTarget &target, LoginStartupTarget *normalized,
                     QString *error)
{
    for (const auto &path : {target.profileDir, target.modelDir}) {
        if (!RuntimePaths::isAbsoluteFilesystemPath(path))
            return fail(error, QStringLiteral("Login startup requires absolute profile and model directories: %1")
                                   .arg(path));
        QString parent = path;
        while (!QFileInfo::exists(parent) && !QFileInfo(parent).isSymbolicLink()) {
            const auto next = QFileInfo(parent).absolutePath();
            if (next == parent) break;
            parent = next;
        }
        const QFileInfo info(parent);
        if ((info.exists() || info.isSymbolicLink()) && !info.isDir())
            return fail(error, QStringLiteral("Login startup target is not a directory: %1").arg(parent));
    }
    const RuntimePaths paths = RuntimePaths::resolve(target.profileDir, target.modelDir);
    if (!paths.validateProfileLayout(error)) return false;
    *normalized = {paths.profileDir, paths.modelDir};
    return true;
}

bool decode(const QByteArray &bytes, LoginStartupTarget *target, QString *error)
{
    QJsonParseError parseError;
    const auto document = QJsonDocument::fromJson(bytes, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject())
        return fail(error, QStringLiteral("Invalid login startup locator JSON: %1")
                               .arg(parseError.errorString()));
    const auto object = document.object();
    if (!object.value(QStringLiteral("version")).isDouble()
        || object.value(QStringLiteral("version")).toDouble() != 1.0
        || !object.value(QStringLiteral("profileDir")).isString()
        || !object.value(QStringLiteral("modelDir")).isString())
        return fail(error, QStringLiteral("Login startup locator requires version 1 and profileDir/modelDir strings."));
    return normalizeTarget({object.value(QStringLiteral("profileDir")).toString(),
                            object.value(QStringLiteral("modelDir")).toString()}, target, error);
}

bool readBytes(const QString &path, QByteArray *bytes, QString *error)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return fail(error, QStringLiteral("Cannot read login startup locator %1: %2")
                               .arg(path, file.errorString()));
    *bytes = file.readAll();
    if (file.error() != QFileDevice::NoError)
        return fail(error, QStringLiteral("Cannot read login startup locator %1: %2")
                               .arg(path, file.errorString()));
    return true;
}

bool writeBytes(const QString &path, const QByteArray &bytes, QString *error)
{
    QSaveFile file(path);
    file.setDirectWriteFallback(false);
    if (!file.open(QIODevice::WriteOnly) || file.write(bytes) != bytes.size()
        || !file.commit())
        return fail(error, QStringLiteral("Cannot save login startup locator %1: %2")
                               .arg(path, file.errorString()));
    return true;
}

QString pendingFile(const QString &locatorFile)
{
    return locatorFile + QStringLiteral(".pending");
}

bool hasPendingUpdate(const QString &locatorFile)
{
    const QFileInfo marker(pendingFile(locatorFile));
    return marker.exists() || marker.isSymbolicLink();
}

QString pendingRecoveryMessage()
{
    return QStringLiteral(" Login startup has an unfinished update. Choose 'Use current directories at sign-in' "
                          "in Settings to enable the intended directories and repair it.");
}

bool clearPendingUpdate(const QString &locatorFile, QString *error)
{
    QFile marker(pendingFile(locatorFile));
    if (marker.remove()) return true;
    return fail(error, QStringLiteral("Cannot finish login startup update: %1.")
                           .arg(marker.errorString()) + pendingRecoveryMessage());
}

bool sameTarget(const LoginStartupTarget &left, const LoginStartupTarget &right)
{
#ifdef Q_OS_WIN
    constexpr auto sensitivity = Qt::CaseInsensitive;
#else
    constexpr auto sensitivity = Qt::CaseSensitive;
#endif
    return left.profileDir.compare(right.profileDir, sensitivity) == 0
        && left.modelDir.compare(right.modelDir, sensitivity) == 0;
}

bool accessibleTarget(const LoginStartupTarget &target, QString *error)
{
#ifdef Q_OS_WIN
    // Enable ACL-aware QFileInfo permission checks instead of only consulting
    // Windows' DOS read-only attribute.
    QNtfsPermissionCheckGuard permissionChecks;
#endif
    for (const auto &path : {target.profileDir, target.modelDir}) {
        const QFileInfo info(path);
        if (!info.exists() || !info.isDir())
            return fail(error, QStringLiteral("Saved login startup directory is unavailable: %1").arg(path));
        if (!info.isReadable()
#ifdef Q_OS_UNIX
            || !info.isExecutable()
#endif
        )
            return fail(error, QStringLiteral("Saved login startup directory is not accessible: %1").arg(path));
    }
    if (!QFileInfo(target.profileDir).isWritable())
        return fail(error, QStringLiteral("Saved login startup profile is not writable: %1").arg(target.profileDir));
    // A verified shared model directory may be read-only.
    return true;
}

} // namespace

bool readLoginStartupTarget(const QString &locatorFile, LoginStartupTarget *target, QString *error)
{
    if (error) error->clear();
    if (!target) return fail(error, QStringLiteral("A login startup target output is required."));
    *target = {};
    if (!validLocator(locatorFile, error)) return false;
    if (!QFileInfo(QFileInfo(locatorFile).absolutePath()).isDir())
        return fail(error, QStringLiteral("Login startup locator directory is unavailable: %1").arg(locatorFile));
    QLockFile lock(locatorFile + QStringLiteral(".lock"));
    lock.setStaleLockTime(0);
    if (!lock.tryLock(0))
        return fail(error, QStringLiteral("Cannot lock login startup target for reading: %1").arg(locatorFile));
    if (hasPendingUpdate(locatorFile)) return fail(error, pendingRecoveryMessage().trimmed());
    QByteArray bytes;
    LoginStartupTarget saved;
    if (!validLocator(locatorFile, error) || !readBytes(locatorFile, &bytes, error)
        || !decode(bytes, &saved, error) || !accessibleTarget(saved, error))
        return false;
    *target = saved;
    return true;
}

bool updateLoginStartupTarget(const QString &locatorFile, const LoginStartupTarget &target,
                              bool enabled,
                              const std::function<bool(bool, QString *)> &nativeSet, QString *error)
{
    if (error) error->clear();
    LoginStartupTarget normalized;
    if (!validLocator(locatorFile, error) || !normalizeTarget(target, &normalized, error))
        return false;
    if (!nativeSet)
        return fail(error, QStringLiteral("A native login startup operation is required."));
    // Only the small locator's parent is created. Runtime directories remain
    // untouched until an actual application launch validates and opens them.
    const QString parent = QFileInfo(locatorFile).absolutePath();
    if (enabled && !QDir().mkpath(parent))
        return fail(error, QStringLiteral("Cannot create login startup locator directory: %1").arg(parent));
    if (!enabled && !QFileInfo(parent).isDir())
        return fail(error, QStringLiteral("No saved login startup target is available to disable."));
    QLockFile lock(locatorFile + QStringLiteral(".lock"));
    lock.setStaleLockTime(0);
    if (!lock.tryLock(0))
        return fail(error, QStringLiteral("Cannot lock login startup target for update: %1").arg(locatorFile));
    // Recheck under the operation lock, including the locator's file type.
    if (!validLocator(locatorFile, error)) return false;
    const bool wasPending = hasPendingUpdate(locatorFile);
    if (!enabled && wasPending) return fail(error, pendingRecoveryMessage().trimmed());
    const bool existed = QFileInfo::exists(locatorFile);
    QByteArray previous;
    if (existed && !readBytes(locatorFile, &previous, error)) return false;

    if (!enabled) {
        LoginStartupTarget saved;
        if (!existed)
            return fail(error, QStringLiteral("No saved login startup target is available to disable."));
        if (!decode(previous, &saved, error)) return false;
        if (!sameTarget(normalized, saved))
            return fail(error, QStringLiteral("Login startup now belongs to another profile or model directory. "
                                              "This window cannot disable that target."));
        if (!validLocator(pendingFile(locatorFile), error)
            || !writeBytes(pendingFile(locatorFile), "1\n", error)) return false;
        QString nativeError;
        if (!nativeSet(false, &nativeError)) {
            if (nativeError.isEmpty()) nativeError = QStringLiteral("Cannot disable native login startup.");
            QString markerError;
            if (!clearPendingUpdate(locatorFile, &markerError)) nativeError += QLatin1Char(' ') + markerError;
            return fail(error, nativeError);
        }
        QFile locator(locatorFile);
        if (locator.remove()) return clearPendingUpdate(locatorFile, error);
        const QString removeError = QStringLiteral("Cannot remove login startup locator %1: %2")
                                        .arg(locatorFile, locator.errorString());
        QString restoreError;
        if (!nativeSet(true, &restoreError))
            return fail(error, removeError + QStringLiteral(" Native registration restore failed: ")
                                   + restoreError + pendingRecoveryMessage());
        QString markerError;
        const QString restored = removeError + QStringLiteral(" The previous native registration was restored.");
        if (!clearPendingUpdate(locatorFile, &markerError))
            return fail(error, restored + QLatin1Char(' ') + markerError);
        return fail(error, restored);
    }

    const QByteArray bytes = QJsonDocument(QJsonObject{
        {QStringLiteral("version"), 1},
        {QStringLiteral("profileDir"), normalized.profileDir},
        {QStringLiteral("modelDir"), normalized.modelDir}
    }).toJson(QJsonDocument::Indented);
    if (!validLocator(pendingFile(locatorFile), error)
        || !writeBytes(pendingFile(locatorFile), "1\n", error)) return false;
    QString writeError;
    if (!writeBytes(locatorFile, bytes, &writeError)) {
        QString markerError;
        if (wasPending) writeError += pendingRecoveryMessage();
        else if (!clearPendingUpdate(locatorFile, &markerError)) writeError += QLatin1Char(' ') + markerError;
        return fail(error, writeError);
    }
    QString nativeError;
    if (nativeSet(true, &nativeError)) return clearPendingUpdate(locatorFile, error);
    if (nativeError.isEmpty()) nativeError = QStringLiteral("Cannot enable native login startup.");
    QString restoreError;
    bool restored = false;
    if (existed) {
        restored = writeBytes(locatorFile, previous, &restoreError);
    } else {
        QFile locator(locatorFile);
        restored = locator.remove();
        if (!restored) restoreError = locator.errorString();
    }
    if (!restored) {
        nativeError += QStringLiteral(" Restoring the previous login startup locator failed: ")
            + restoreError + pendingRecoveryMessage();
    } else if (wasPending) {
        // The earlier locator was already uncommitted. Restoring its bytes
        // cannot establish a valid native registration, so keep it blocked.
        nativeError += pendingRecoveryMessage();
    } else {
        QString markerError;
        if (!clearPendingUpdate(locatorFile, &markerError)) nativeError += QLatin1Char(' ') + markerError;
    }
    return fail(error, nativeError);
}

} // namespace qcopilots_search
