#include "general_file_rules.h"

#include <QJsonArray>

#include <cmath>
#include <limits>

namespace qcopilots_search {
namespace {
bool fail(QString *error, const QString &message)
{
    if (error) *error = message;
    return false;
}

bool readNames(const QJsonObject &object, const QString &key, QStringList &names,
               bool suffixes, QString *error)
{
    if (!object.contains(key)) return true;
    if (!object.value(key).isArray())
        return fail(error, QStringLiteral("Setting 'generalFileRules.%1' must be an array").arg(key));
    QStringList parsed;
    for (const auto &value : object.value(key).toArray()) {
        const QString name = value.toString();
        if (!value.isString() || name.trimmed().isEmpty() || name == u"." || name == u".."
            || name.contains(QChar::Null) || name.contains(u'/') || name.contains(u'\\')
            || (suffixes && name.contains(u'.'))) {
            return fail(error, QStringLiteral("Setting 'generalFileRules.%1' requires non-empty %2 without path separators%3")
                                   .arg(key, suffixes ? QStringLiteral("suffixes") : QStringLiteral("names"),
                                        suffixes ? QStringLiteral(" or dots") : QString{}));
        }
        if (!parsed.contains(name)) parsed.append(name);
    }
    names = parsed;
    return true;
}

bool readInteger(const QJsonObject &object, const QString &key, int &destination,
                 int minimum, int maximum, QString *error)
{
    if (!object.contains(key)) return true;
    const auto value = object.value(key);
    const double number = value.toDouble();
    if (!value.isDouble() || !std::isfinite(number) || std::floor(number) != number
        || number < minimum || number > maximum) {
        return fail(error, QStringLiteral("Setting 'generalFileRules.%1' must be an integer from %2 to %3")
                               .arg(key).arg(minimum).arg(maximum));
    }
    destination = static_cast<int>(number);
    return true;
}
}

QJsonObject GeneralFileRules::toJson() const
{
    return {{QStringLiteral("excludeHidden"), excludeHidden},
            {QStringLiteral("excludeProjects"), excludeProjects},
            {QStringLiteral("excludeNamedDirectories"), excludeNamedDirectories},
            {QStringLiteral("excludeMediaDatasets"), excludeMediaDatasets},
            {QStringLiteral("projectFileNames"), QJsonArray::fromStringList(projectFileNames)},
            {QStringLiteral("projectDirectorySuffixes"), QJsonArray::fromStringList(projectDirectorySuffixes)},
            {QStringLiteral("directoryNames"), QJsonArray::fromStringList(directoryNames)},
            {QStringLiteral("directorySuffixes"), QJsonArray::fromStringList(directorySuffixes)},
            {QStringLiteral("mediaDatasetMinimumFiles"), mediaDatasetMinimumFiles},
            {QStringLiteral("mediaDatasetMachineNamedPercent"), mediaDatasetMachineNamedPercent}};
}

bool GeneralFileRules::fromJson(const QJsonObject &object, GeneralFileRules *rules, QString *error)
{
    if (error) error->clear();
    if (!rules) return fail(error, QStringLiteral("General file rules output is null"));
    GeneralFileRules parsed;
    const auto known = parsed.toJson();
    for (auto entry = object.begin(); entry != object.end(); ++entry) {
        if (!known.contains(entry.key()))
            return fail(error, QStringLiteral("Unknown setting 'generalFileRules.%1'").arg(entry.key()));
    }
    struct Boolean { const char *name; bool *destination; };
    const Boolean booleans[] = {{"excludeHidden", &parsed.excludeHidden},
                               {"excludeProjects", &parsed.excludeProjects},
                               {"excludeNamedDirectories", &parsed.excludeNamedDirectories},
                               {"excludeMediaDatasets", &parsed.excludeMediaDatasets}};
    for (const auto &field : booleans) {
        const auto key = QString::fromLatin1(field.name);
        if (!object.contains(key)) continue;
        if (!object.value(key).isBool())
            return fail(error, QStringLiteral("Setting 'generalFileRules.%1' must be boolean").arg(key));
        *field.destination = object.value(key).toBool();
    }
    if (!readNames(object, QStringLiteral("projectFileNames"), parsed.projectFileNames, false, error)
        || !readNames(object, QStringLiteral("projectDirectorySuffixes"), parsed.projectDirectorySuffixes, true, error)
        || !readNames(object, QStringLiteral("directoryNames"), parsed.directoryNames, false, error)
        || !readNames(object, QStringLiteral("directorySuffixes"), parsed.directorySuffixes, true, error)
        || !readInteger(object, QStringLiteral("mediaDatasetMinimumFiles"), parsed.mediaDatasetMinimumFiles,
                        1, std::numeric_limits<int>::max(), error)
        || !readInteger(object, QStringLiteral("mediaDatasetMachineNamedPercent"), parsed.mediaDatasetMachineNamedPercent,
                        0, 100, error)) return false;
    *rules = parsed;
    return true;
}
} // namespace qcopilots_search
