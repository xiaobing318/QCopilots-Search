#pragma once

#include <QString>

namespace qcopilots_search {

// All processes in one application instance receive the same explicit profile.
// Resolution does not create directories or read settings.
struct RuntimePaths {
    QString resolutionError;
    QString profileBaseDir; // User-selected base, forwarded to child processes.
    QString profileDir;
    QString modelDir;
    QString indexDir;
    QString cacheDir;
    QString logDir;
    QString tempDir;
    QString configFile;
    QString lockFile;
    QString ipcName; // Windows pipe name, Unix absolute socket pathname.

    // --profile-dir and QCS_HOME select the base. Writable state is in profiles/vN.
    static RuntimePaths resolve(const QString &profileBaseDir = {}, const QString &modelDir = {});
    // Excludes Qt resources, embedded NULs and Windows drive-relative paths.
    static bool isAbsoluteFilesystemPath(const QString &path);
    // Platform data base, independent of profile selection environment variables.
    // This does not create or inspect any profile files.
    static QString defaultProfileDirectory();
    // Reject a legacy writable profile without creating or migrating files.
    bool validateProfileLayout(QString *error = nullptr) const;
    bool ensure(QString *error = nullptr) const;
    bool ensureIpcDirectory(QString *error = nullptr) const;
    bool validateIpcDirectory(QString *error = nullptr) const;
    QString databaseFile() const;
};

} // namespace qcopilots_search
