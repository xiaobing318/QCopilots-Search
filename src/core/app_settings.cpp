#include "app_settings.h"

#include <QCryptographicHash>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QSaveFile>

namespace qcopilots_search {
namespace {
bool fail(QString *error, const QString &message)
{
    if (error)
        *error = message;
    return false;
}

bool readStrings(const QJsonObject &object, const QString &key, QStringList &values,
                 QString *error)
{
    if (!object.contains(key))
        return true;
    if (!object.value(key).isArray())
        return fail(error, QStringLiteral("Setting '%1' must be an array").arg(key));
    for (const auto &entry : object.value(key).toArray()) {
        if (!entry.isString() || entry.toString().trimmed().isEmpty()
            || entry.toString().contains(QChar::Null))
            return fail(error, QStringLiteral("Setting '%1' contains an invalid path").arg(key));
        if (!QFileInfo(entry.toString()).isAbsolute())
            return fail(error, QStringLiteral("Setting '%1' requires absolute paths").arg(key));
        if (!values.contains(entry.toString()))
            values.append(entry.toString());
    }
    return true;
}
}

QJsonObject AppSettings::toJson() const
{
    return {{QStringLiteral("version"), 1},
            {QStringLiteral("roots"), QJsonArray::fromStringList(roots)},
            {QStringLiteral("excludes"), QJsonArray::fromStringList(excludes)},
            {QStringLiteral("includeCode"), includeCode},
            {QStringLiteral("language"), language},
            {QStringLiteral("startAtLogin"), startAtLogin},
            {QStringLiteral("minimizeToTray"), minimizeToTray},
            {QStringLiteral("checkUpdates"), checkUpdates},
            {QStringLiteral("indexingPaused"), indexingPaused},
            {QStringLiteral("editorCommand"), editorCommand}};
}

bool AppSettings::fromJson(const QJsonObject &object, AppSettings *settings, QString *error)
{
    if (error)
        error->clear();
    if (!settings)
        return fail(error, QStringLiteral("Settings output is null"));
    if (!object.value(QStringLiteral("version")).isDouble()
        || object.value(QStringLiteral("version")).toDouble() != 1.0)
        return fail(error, QStringLiteral("Unsupported or missing settings version"));
    AppSettings parsed;
    if (!readStrings(object, QStringLiteral("roots"), parsed.roots, error)
        || !readStrings(object, QStringLiteral("excludes"), parsed.excludes, error))
        return false;
    struct Boolean { const char *name; bool *destination; };
    const Boolean booleans[] = {{"includeCode", &parsed.includeCode},
                               {"startAtLogin", &parsed.startAtLogin},
                               {"minimizeToTray", &parsed.minimizeToTray},
                               {"checkUpdates", &parsed.checkUpdates},
                               {"indexingPaused", &parsed.indexingPaused}};
    for (const auto &field : booleans) {
        const QString key = QString::fromLatin1(field.name);
        if (object.contains(key)) {
            if (!object.value(key).isBool())
                return fail(error, QStringLiteral("Setting '%1' must be boolean").arg(key));
            *field.destination = object.value(key).toBool();
        }
    }
    for (const auto &key : {QStringLiteral("language"), QStringLiteral("editorCommand")}) {
        if (object.contains(key) && !object.value(key).isString())
            return fail(error, QStringLiteral("Setting '%1' must be a string").arg(key));
    }
    parsed.language = object.value(QStringLiteral("language")).toString(parsed.language);
    const QStringList languages{QStringLiteral("system"), QStringLiteral("en"),
                               QStringLiteral("zh"), QStringLiteral("zh_CN"),
                               QStringLiteral("zh_TW")};
    if (!languages.contains(parsed.language))
        return fail(error, QStringLiteral("Unsupported interface language: %1").arg(parsed.language));
    parsed.editorCommand = object.value(QStringLiteral("editorCommand")).toString();
    *settings = parsed;
    return true;
}

bool AppSettings::load(const QString &path, QString *error)
{
    if (error)
        error->clear();
    if (!QFileInfo::exists(path)) {
        *this = AppSettings{};
        return true;
    }
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return fail(error, QStringLiteral("Cannot read settings %1: %2").arg(path, file.errorString()));
    if (file.size() > 1024 * 1024)
        return fail(error, QStringLiteral("Settings file exceeds the 1 MiB limit: %1").arg(path));
    QJsonParseError parseError;
    const auto document = QJsonDocument::fromJson(file.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject())
        return fail(error, QStringLiteral("Invalid settings JSON in %1: %2").arg(path, parseError.errorString()));
    return fromJson(document.object(), this, error);
}

bool AppSettings::save(const QString &path, QString *error) const
{
    if (error)
        error->clear();
    AppSettings validated;
    if (!fromJson(toJson(), &validated, error))
        return false;
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly))
        return fail(error, QStringLiteral("Cannot write settings %1: %2").arg(path, file.errorString()));
    const QByteArray bytes = QJsonDocument(toJson()).toJson(QJsonDocument::Indented);
    if (file.write(bytes) != bytes.size() || !file.commit())
        return fail(error, QStringLiteral("Cannot commit settings %1: %2").arg(path, file.errorString()));
    return true;
}

QString AppSettings::fingerprint() const
{
    // Only options which affect indexing invalidate in-flight extraction jobs.
    QJsonObject object{{QStringLiteral("roots"), QJsonArray::fromStringList(roots)},
                       {QStringLiteral("excludes"), QJsonArray::fromStringList(excludes)},
                       {QStringLiteral("includeCode"), includeCode}};
    return QString::fromLatin1(QCryptographicHash::hash(QJsonDocument(object).toJson(QJsonDocument::Compact),
                                                       QCryptographicHash::Sha256).toHex());
}
} // namespace qcopilots_search
