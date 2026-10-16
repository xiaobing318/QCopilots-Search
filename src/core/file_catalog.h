#pragma once

#include "general_file_rules.h"

#include <QList>
#include <QString>
#include <QStringList>
#include <atomic>
#include <functional>

namespace qcopilots_search {

// Existing paths and the existing ancestors of missing paths are canonicalized.
// An empty path stays empty and never implicitly selects the working directory.
QString normalizePath(const QString &path);
// Component-aware containment, including equality. Windows comparisons ignore case.
bool isPathWithin(const QString &path, const QString &root);

struct FileRecord {
    QString path;
    QString identity;
    QString fingerprint;
    qint64 size = 0;
    qint64 modifiedMs = 0;
    QString kind; // text, pdf, image, audio, video, code
    bool screenshot = false;
};

struct FolderRules {
    enum class Scope { General, Code };
    QStringList roots;
    QStringList excludes;
    // Retained for legacy mixed callers. Services use an explicit Code scope.
    bool includeCode = false;
    Scope scope = Scope::General;
    GeneralFileRules generalFileRules;
};

struct ScanResult {
    QList<FileRecord> files;
    // Unique, normalized directories actually visited, including empty directories.
    // Intended for directory watchers. Excluded directories and links are absent.
    QStringList directories;
    // Normalized directories rejected by enabled General rules. Callers that
    // scan roots separately carry these forward as exclusions for descendants.
    QStringList policyExcludedDirectories;
    QStringList errors;
    bool cancelled = false;
};

class FileCatalog final {
public:
    // Re-read native identity and version metadata before committing a worker result.
    // Rejects links, non-regular files and unsupported file kinds.
    static bool describe(const QString &path, FileRecord *record, QString *error = nullptr);
    // Partial results are returned on cancellation or per-root/file failures.
    // General search includes supported files unless an explicit exclusion or
    // an enabled generalFileRules policy applies. General exclusions take
    // precedence over overlapping selected roots. Code keeps its own filters.
    // Directory links are never followed, so cycles cannot escape selected roots.
    // Called synchronously after each accepted record is appended. The count is
    // cumulative and the callback may set cancel. No locks are held by scan.
    static ScanResult scan(const FolderRules &rules, std::atomic_bool *cancel = nullptr,
                           const std::function<void(qsizetype)> &onDiscovered = {});
};

} // namespace qcopilots_search
