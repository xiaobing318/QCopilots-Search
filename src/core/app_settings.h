#pragma once

#include <QJsonObject>
#include <QString>
#include <QStringList>

namespace qcopilots_search {

struct AppSettings {
    QStringList roots;
    QStringList excludes;
    bool includeCode = false;
    QString language = QStringLiteral("system");
    bool startAtLogin = false;
    bool minimizeToTray = true;
    bool checkUpdates = true;
    bool indexingPaused = false;
    QString editorCommand;

    // A missing settings file means first run. Malformed/unsupported files fail
    // without altering this object. Saves use an atomic file replacement.
    bool load(const QString &path, QString *error = nullptr);
    bool save(const QString &path, QString *error = nullptr) const;
    QJsonObject toJson() const;
    static bool fromJson(const QJsonObject &object, AppSettings *settings,
                         QString *error = nullptr);
    QString fingerprint() const;
};

} // namespace qcopilots_search
