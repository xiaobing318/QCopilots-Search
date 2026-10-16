#pragma once

#include <QJsonObject>
#include <QString>
#include <QStringList>

namespace qcopilots_search {

// Optional exclusions for ordinary files. Code indexing has its own policy.
// Lists remain visible in settings even while their exclusion is disabled.
struct GeneralFileRules {
    bool excludeHidden = false;
    bool excludeProjects = false;
    bool excludeNamedDirectories = false;
    bool excludeMediaDatasets = false;
    QStringList projectFileNames{
        QStringLiteral(".git"), QStringLiteral("package.json"), QStringLiteral("Package.swift"),
        QStringLiteral("pyproject.toml"), QStringLiteral("setup.py"), QStringLiteral("requirements.txt"),
        QStringLiteral("Cargo.toml"), QStringLiteral("go.mod"), QStringLiteral("pom.xml"),
        QStringLiteral("build.gradle"), QStringLiteral("build.gradle.kts"), QStringLiteral("Gemfile"),
        QStringLiteral("composer.json"), QStringLiteral("CMakeLists.txt")};
    QStringList projectDirectorySuffixes{QStringLiteral("xcodeproj"), QStringLiteral("xcworkspace")};
    QStringList directoryNames{
        QStringLiteral(".git"), QStringLiteral(".svn"), QStringLiteral(".hg"),
        QStringLiteral("node_modules"), QStringLiteral("bower_components"), QStringLiteral("venv"),
        QStringLiteral(".venv"), QStringLiteral("site-packages"), QStringLiteral("__pycache__"),
        QStringLiteral("DerivedData"), QStringLiteral("Pods"), QStringLiteral("Carthage")};
    QStringList directorySuffixes{
        QStringLiteral("app"), QStringLiteral("bundle"), QStringLiteral("framework"),
        QStringLiteral("plugin"), QStringLiteral("kext"), QStringLiteral("prefpane"),
        QStringLiteral("appex"), QStringLiteral("xcodeproj"), QStringLiteral("xcworkspace"),
        QStringLiteral("pkg"), QStringLiteral("photoslibrary"), QStringLiteral("noindex")};
    int mediaDatasetMinimumFiles = 300;
    int mediaDatasetMachineNamedPercent = 70;

    QJsonObject toJson() const;
    static bool fromJson(const QJsonObject &object, GeneralFileRules *rules,
                         QString *error = nullptr);
};

} // namespace qcopilots_search
