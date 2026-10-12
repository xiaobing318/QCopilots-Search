#pragma once

#include "core/runtime_paths.h"
#include "core/types.h"
#include <QSet>
#include <functional>
#include <memory>

namespace qcopilots_search {
using PublicationGate = std::function<std::shared_ptr<void>()>;

struct IndexedFile {
  QString path;
  QString version;
  bool code = false;
  QVector<Segment> segments;
};

struct StorageOptions {
  // Applications may override this budget for a selected volume. The capacity
  // benchmark uses 3 GiB. Ordinary installations do not inherit that machine's
  // disk requirement. An injected provider makes disk-full tests deterministic.
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
                   const PublicationGate &publicationGate = {});
  // Offline initial imports may defer derived ANN maintenance until flush.
  // Keyword search remains available. Semantic queries reject a deferred
  // import explicitly until flush completes, preventing unbounded delta work.
  bool replaceFiles(const QVector<IndexedFile> &files, QString *error = nullptr,
                    bool deferMaintenance = false,
                    const PublicationGate &publicationGate = {});
  bool removeFile(const QString &path, QString *error = nullptr);
  bool prune(const QString &root, const QSet<QString> &presentPaths,
             QString *error = nullptr);
  QString fileVersion(const QString &path, bool code = false,
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
