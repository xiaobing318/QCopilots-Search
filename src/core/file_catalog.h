#pragma once

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
};

struct FolderRules {
    QStringList roots;
    QStringList excludes;
    bool includeCode = false;
};

struct ScanResult {
    QList<FileRecord> files;
    // Unique, normalized directories actually visited, including empty directories.
    // Intended for directory watchers. Excluded directories and links are absent.
    QStringList directories;
    QStringList errors;
    bool cancelled = false;
};

class FileCatalog final {
public:
    // Re-read native identity and version metadata before committing a worker result.
    // Rejects links, non-regular files and unsupported file kinds.
    static bool describe(const QString &path, FileRecord *record, QString *error = nullptr);
    // Partial results are returned on cancellation or per-root/file failures.
    // Explicitly chosen roots inside an excluded ancestor remain searchable.
    // Directory links are never followed, so cycles cannot escape selected roots.
    // Called synchronously after each accepted record is appended. The count is
    // cumulative and the callback may set cancel. No locks are held by scan.
    static ScanResult scan(const FolderRules &rules, std::atomic_bool *cancel = nullptr,
                           const std::function<void(qsizetype)> &onDiscovered = {});
};

} // namespace qcopilots_search
