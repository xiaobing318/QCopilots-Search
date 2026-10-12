#pragma once

#include <QString>

namespace qcopilots_search {

// All processes in one application instance receive the same explicit profile.
// Resolution does not create directories or read settings.
struct RuntimePaths {
    QString profileDir;
    QString modelDir;
    QString indexDir;
    QString cacheDir;
    QString logDir;
    QString tempDir;
    QString configFile;
    QString lockFile;
    QString ipcName;

    static RuntimePaths resolve(const QString &profileDir = {}, const QString &modelDir = {});
    bool ensure(QString *error = nullptr) const;
    QString databaseFile() const;
};

} // namespace qcopilots_search
