#pragma once

#include "core/runtime_paths.h"
#include "core/types.h"
#include <QSet>
#include <functional>
#include <memory>
#include <optional>

namespace qcopilots_search {
using PublicationGate = std::function<std::shared_ptr<void>()>;

struct IndexedFile {
  QString path;
  QString version;
  bool code = false;
  QVector<Segment> segments;
  std::optional<qint64> modifiedMs = std::nullopt;
  QString kind = {};
};

struct FileMetadata {
  QString path;
  bool code = false;
  qint64 modifiedMs = 0;
  QString kind;
  // When present, stale scan metadata cannot overwrite a different source.
  QString sourceFingerprint;
  // A scan may describe a changed source while its old index is retained.
  // This exact indexed-version snapshot permits metadata-only refresh without
  // allowing an older scan to overwrite a newly published file version.
  QString expectedIndexedVersion = {};
};

struct StorageOptions {
  // Applications may override this budget for a selected volume. An injected
  // provider makes disk-full tests deterministic.
  qint64 diskReserveBytes = 64LL * 1024 * 1024;
  std::function<qint64(const QString &)> availableBytes;
  std::function<void(const QString &)> faultHook;
  // Pause new writes while readers or another connection prevent reclaiming
  // a WAL larger than this budget. Must be positive. Keeping this configurable
  // also permits real SQLite recovery tests without a 256 MiB fixture.
  qint64 maxWalBytes = 256LL * 1024 * 1024;
};

// One service owns the writer. Each search creates an independent SQLite read
// transaction and holds a matching immutable ANN snapshot until completion.
class IndexRepository {
public:
  explicit IndexRepository(StorageOptions options = {});
  ~IndexRepository();
  IndexRepository(const IndexRepository &) = delete;
  IndexRepository &operator=(const IndexRepository &) = delete;

  bool open(const RuntimePaths &paths, QString *error = nullptr);
  bool replaceFile(const QString &path, const QString &version,
                   const QVector<Segment> &segments, bool code = false,
                   QString *error = nullptr,
                   const PublicationGate &publicationGate = {},
                   std::optional<qint64> modifiedMs = std::nullopt,
                   const QString &kind = {});
  // Offline initial imports may defer derived ANN maintenance until flush.
  // Keyword search remains available. Semantic queries reject a deferred
  // import explicitly until flush completes, preventing unbounded delta work.
  bool replaceFiles(const QVector<IndexedFile> &files, QString *error = nullptr,
                    bool deferMaintenance = false,
                    const PublicationGate &publicationGate = {});
  // The legacy overloads affect the ordinary space only.
  bool removeFile(const QString &path, QString *error = nullptr);
  bool removeFile(const QString &path, bool code, QString *error = nullptr);
  bool prune(const QString &root, const QSet<QString> &presentPaths,
             QString *error = nullptr);
  bool prune(const QString &root, const QSet<QString> &presentPaths,
             bool code, QString *error = nullptr);
  // Reconciliation fills metadata for existing indexed members only. Missing
  // members and stale scan snapshots are left untouched. An explicit indexed
  // version snapshot also permits refreshing metadata for a changed source.
  bool updateFileMetadata(const QVector<FileMetadata> &files,
                          QString *error = nullptr,
                          const PublicationGate &publicationGate = {});
  // Remove stale content before reconciled index data becomes searchable.
  // The scan's exact indexed version prevents deleting a newer publication.
  bool invalidateChangedFiles(const QVector<FileMetadata> &files,
                              QString *error = nullptr,
                              const PublicationGate &publicationGate = {});
  // Each page owns a short read snapshot. Cursors become stale when file
  // membership or sorting metadata changes, never on segment-only progress.
  FileListPage listFiles(const FileListOptions &options) const;
  qint64 browseGeneration(QString *error = nullptr) const;
  QString fileVersion(const QString &path, bool code = false,
                      QString *error = nullptr) const;
  bool fileHasEmbeddings(const QString &path, bool code = false,
                         QString *error = nullptr) const;
  QVector<Segment> fileSegments(const QString &path, bool code = false,
                                QString *error = nullptr) const;
  QList<SearchHit> search(const SearchOptions &options,
                          QString *error = nullptr) const;
  QJsonObject status(QString *error = nullptr) const;
  bool flush(QString *error = nullptr);
  bool maintenance(QString *error = nullptr);
  bool flushOneShard(QString *error = nullptr);
  bool checkIntegrity(QString *error = nullptr) const;
  bool setPipelineFingerprint(const QString &fingerprint,
                              QString *error = nullptr);
  QString pipelineFingerprint(QString *error = nullptr) const;
  QString databasePath() const;

private:
  class Impl;
  std::unique_ptr<Impl> d;
};

} // namespace qcopilots_search
