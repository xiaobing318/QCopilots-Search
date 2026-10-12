#pragma once

#include <QDir>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <memory>

namespace qcopilots_search::test {

// No test may fall back to a real user profile or a source-tree directory.
inline std::unique_ptr<QTemporaryDir> temporaryDirectory(QString* error)
{
    const auto requestedRoot = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT");
    const auto root = requestedRoot.isEmpty() ? QDir::tempPath() : requestedRoot;
    if (!QDir().mkpath(root)) {
        *error = QStringLiteral("Cannot create the requested test root: %1").arg(root);
        return {};
    }
    auto directory = std::make_unique<QTemporaryDir>(
        QDir(root).filePath(QStringLiteral("qcopilots-search-runtime-tests-XXXXXX")));
    if (!directory->isValid()) {
        *error = QStringLiteral("Cannot create an isolated test directory under %1: %2")
                     .arg(root, directory->errorString());
        return {};
    }
    return directory;
}

}
