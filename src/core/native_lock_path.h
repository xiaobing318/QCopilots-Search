#pragma once

#include <QDir>
#include <QFileInfo>
#include <QString>

namespace qcopilots_search {

// Qt 6.12 creates Windows lock files through its extended-path conversion,
// but QLockFile's removal passes its stored name directly to CreateFileW.
// Adapt only the constructor argument so both operations address the same file.
// Public runtime paths, persisted configuration and IPC identities stay intact.
inline QString nativeLockFilePath(const QString &path)
{
#ifdef Q_OS_WIN
    if (path.isEmpty()) return path;
    QString native = QDir::toNativeSeparators(path);
    if (native.startsWith(QStringLiteral("\\\\?\\")) || native.startsWith(QStringLiteral("\\\\.\\")))
        return native;
    native = QDir::toNativeSeparators(QDir::cleanPath(QFileInfo(path).absoluteFilePath()));
    return native.startsWith(QStringLiteral("\\\\"))
        ? QStringLiteral("\\\\?\\UNC\\") + native.mid(2)
        : QStringLiteral("\\\\?\\") + native;
#else
    return path;
#endif
}

} // namespace qcopilots_search
