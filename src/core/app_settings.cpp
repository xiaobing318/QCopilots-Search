#include "app_settings.h"
#include "settings_version.h"

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

QJsonObject openerJson(const ExternalOpenerSettings &opener)
{
    return {{QStringLiteral("preset"), opener.preset}, {QStringLiteral("program"), opener.program},
            {QStringLiteral("arguments"), QJsonArray::fromStringList(opener.arguments)}};
}

bool readOpener(const QJsonObject &object, const QString &key, const QStringList &presets,
                ExternalOpenerSettings &opener, QString *error)
{
    if (!object.contains(key)) return true;
    if (!object.value(key).isObject())
        return fail(error, QStringLiteral("Setting '%1' must be an object").arg(key));
    const auto value = object.value(key).toObject();
    for (const auto &field : {QStringLiteral("preset"), QStringLiteral("program")}) {
        if (value.contains(field) && (!value.value(field).isString()
            || value.value(field).toString().contains(QChar::Null)))
            return fail(error, QStringLiteral("Setting '%1.%2' must be a string without NUL").arg(key, field));
    }
    opener.preset = value.value(QStringLiteral("preset")).toString(opener.preset);
    opener.program = value.value(QStringLiteral("program")).toString();
    if (!presets.contains(opener.preset))
        return fail(error, QStringLiteral("Unsupported '%1' preset: %2").arg(key, opener.preset));
    if (value.contains(QStringLiteral("arguments"))) {
        if (!value.value(QStringLiteral("arguments")).isArray())
            return fail(error, QStringLiteral("Setting '%1.arguments' must be an array").arg(key));
        for (const auto &argument : value.value(QStringLiteral("arguments")).toArray()) {
            if (!argument.isString() || argument.toString().contains(QChar::Null))
                return fail(error, QStringLiteral("Setting '%1.arguments' contains an invalid argument").arg(key));
            opener.arguments.append(argument.toString());
        }
    }
    if (opener.preset == QStringLiteral("custom") && opener.program.trimmed().isEmpty())
        return fail(error, QStringLiteral("Setting '%1.program' is required for a custom opener").arg(key));
    return true;
}
}

QJsonObject AppSettings::toJson() const
{
    return {{QStringLiteral("version"), settingsVersion},
            {QStringLiteral("roots"), QJsonArray::fromStringList(roots)},
            {QStringLiteral("excludes"), QJsonArray::fromStringList(excludes)},
            {QStringLiteral("generalFileRules"), generalFileRules.toJson()},
            {QStringLiteral("codeRoots"), QJsonArray::fromStringList(codeRoots)},
            {QStringLiteral("codeExcludes"), QJsonArray::fromStringList(codeExcludes)},
            {QStringLiteral("includeCode"), includeCode},
            {QStringLiteral("language"), language},
            {QStringLiteral("startAtLogin"), startAtLogin},
            {QStringLiteral("minimizeToTray"), minimizeToTray},
            {QStringLiteral("checkUpdates"), checkUpdates},
            {QStringLiteral("indexingPaused"), indexingPaused},
            {QStringLiteral("editorCommand"), editorCommand},
            {QStringLiteral("pdfOpener"), openerJson(pdfOpener)},
            {QStringLiteral("mediaOpener"), openerJson(mediaOpener)}};
}

bool AppSettings::fromJson(const QJsonObject &object, AppSettings *settings, QString *error)
{
    if (error)
        error->clear();
    if (!settings)
        return fail(error, QStringLiteral("Settings output is null"));
    const auto version = object.value(QStringLiteral("version"));
    if (!version.isDouble() || version.toDouble() != settingsVersion)
        return fail(error, QStringLiteral("Unsupported or missing settings version. This profile requires version %1. Older profiles are not migrated.").arg(settingsVersion));
    if (object.contains(QStringLiteral("modelId")))
        return fail(error, QStringLiteral("Setting 'modelId' is no longer supported. Remove this obsolete setting from the version %1 configuration.").arg(settingsVersion));
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
    parsed.migrationPending_ = !object.contains(QStringLiteral("generalFileRules"));
    if (object.contains(QStringLiteral("generalFileRules"))) {
        const auto rules = object.value(QStringLiteral("generalFileRules"));
        if (!rules.isObject())
            return fail(error, QStringLiteral("Setting 'generalFileRules' must be an object"));
        if (!GeneralFileRules::fromJson(rules.toObject(), &parsed.generalFileRules, error))
            return false;
        // Persist defaults for omitted fields so every active rule is inspectable.
        parsed.migrationPending_ = parsed.migrationPending_
            || rules.toObject().size() != parsed.generalFileRules.toJson().size();
    }
    if (!readStrings(object, QStringLiteral("codeRoots"), parsed.codeRoots, error)
               || !readStrings(object, QStringLiteral("codeExcludes"), parsed.codeExcludes, error)) {
        return false;
    }
    for (const auto &key : {QStringLiteral("language"), QStringLiteral("editorCommand")}) {
        if (object.contains(key) && (!object.value(key).isString()
            || object.value(key).toString().contains(QChar::Null)))
            return fail(error, QStringLiteral("Setting '%1' must be a string").arg(key));
    }
    parsed.language = object.value(QStringLiteral("language")).toString(parsed.language);
    const QStringList languages{QStringLiteral("system"), QStringLiteral("en"),
                               QStringLiteral("zh"), QStringLiteral("zh_CN"),
                               QStringLiteral("zh_TW")};
    if (!languages.contains(parsed.language))
        return fail(error, QStringLiteral("Unsupported interface language: %1").arg(parsed.language));
    parsed.editorCommand = object.value(QStringLiteral("editorCommand")).toString();
    if (!readOpener(object, QStringLiteral("pdfOpener"), {"system", "sumatra", "okular", "skim", "custom"},
                    parsed.pdfOpener, error)
        || !readOpener(object, QStringLiteral("mediaOpener"), {"system", "mpv", "custom"},
                       parsed.mediaOpener, error))
        return false;
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
                       {QStringLiteral("generalFileRules"), generalFileRules.toJson()},
                       {QStringLiteral("codeRoots"), QJsonArray::fromStringList(codeRoots)},
                       {QStringLiteral("codeExcludes"), QJsonArray::fromStringList(codeExcludes)},
                       {QStringLiteral("includeCode"), includeCode}};
    return QString::fromLatin1(QCryptographicHash::hash(QJsonDocument(object).toJson(QJsonDocument::Compact),
                                                       QCryptographicHash::Sha256).toHex());
}
} // namespace qcopilots_search
