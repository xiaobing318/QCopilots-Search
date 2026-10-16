#pragma once

#include "general_file_rules.h"

#include <QJsonObject>
#include <QString>
#include <QStringList>

namespace qcopilots_search {

struct ExternalOpenerSettings {
    QString preset = QStringLiteral("system");
    QString program;
    QStringList arguments;
};

struct AppSettings {
    QStringList roots;
    QStringList excludes;
    GeneralFileRules generalFileRules;
    QStringList codeRoots;
    QStringList codeExcludes;
    bool includeCode = false;
    QString language = QStringLiteral("system");
    bool startAtLogin = false;
    bool minimizeToTray = true;
    bool checkUpdates = true;
    bool indexingPaused = false;
    QString editorCommand;
    ExternalOpenerSettings pdfOpener;
    ExternalOpenerSettings mediaOpener;

    // A missing settings file means first run. Malformed/unsupported files fail
    // without altering this object. Saves use an atomic file replacement.
    bool load(const QString &path, QString *error = nullptr);
    bool save(const QString &path, QString *error = nullptr) const;
    QJsonObject toJson() const;
    static bool fromJson(const QJsonObject &object, AppSettings *settings,
                         QString *error = nullptr);
    QString fingerprint() const;
    // Loading older/incomplete settings changes memory only. The profile owner
    // persists the complete v2 configuration after locking.
    bool needsMigration() const { return migrationPending_; }

private:
    bool migrationPending_ = false;
};

} // namespace qcopilots_search
