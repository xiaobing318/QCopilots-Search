#pragma once

#include <QDir>
#include <QFileInfo>
#include <QString>
#include <sqlite3.h>

namespace qcopilots_search::test {

// Raw SQL observations and fault injection must open the same database as the
// repository. Preserve sqlite3_open_v2's flags, result and handle ownership.
inline int openSqliteDatabase(const QString &file, sqlite3 **database, int flags)
{
    QString nativeFile = file;
    const char *vfs = nullptr;
#ifdef Q_OS_WIN
    nativeFile = QDir::toNativeSeparators(QFileInfo(file).absoluteFilePath());
    if (!nativeFile.startsWith(QStringLiteral("\\\\?\\"))) {
        nativeFile = nativeFile.startsWith(QStringLiteral("\\\\"))
            ? QStringLiteral("\\\\?\\UNC\\") + nativeFile.mid(2)
            : QStringLiteral("\\\\?\\") + nativeFile;
    }
    // SQLite reports a missing VFS through its normal error handle and return
    // code. Do not fall back to a different path budget or alter test semantics.
    vfs = "win32-longpath";
#endif
    const auto path = nativeFile.toUtf8();
    return sqlite3_open_v2(path.constData(), database, flags, vfs);
}

} // namespace qcopilots_search::test
