#pragma once

#include <QDir>
#include <QString>

namespace qcopilots_search {

// The settings format and writable profile generation advance together.
inline constexpr int settingsVersion = 4;

inline QString versionedProfileDirectory(const QString &base)
{
    return base.isEmpty() ? QString{}
        : QDir(base).filePath(QStringLiteral("profiles/v%1").arg(settingsVersion));
}

} // namespace qcopilots_search
