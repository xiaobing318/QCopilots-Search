#include "index_repository.h"
#include "core/file_catalog.h"
#include "text_index.h"
#include "vector_index.h"
#include <QDir>
#include <QFileInfo>
#include <QHash>
#include <QJsonArray>
#include <QStorageInfo>
#include <QUuid>
#include <algorithm>
#include <array>
#include <cmath>
#include <mutex>
#include <sqlite3.h>
#include <stdexcept>
#include <unordered_map>

namespace qcopilots_search {
namespace {
constexpr int schemaVersion = 2;
constexpr int shardCount = 16;
constexpr qint64 exactSubsetLimit = 20000;
constexpr qint64 deltaLimit = 2048;

[[noreturn]] void fail(sqlite3 *db, const char *context) {
  throw std::runtime_error(std::string(context) + ": " + sqlite3_errmsg(db));
}
void execute(sqlite3 *db, const char *sql) {
  if (sqlite3_exec(db, sql, nullptr, nullptr, nullptr) != SQLITE_OK)
    fail(db, "SQLite");
}
struct Connection {
  sqlite3 *db = nullptr;
  Connection() = default;
  ~Connection() {
    if (db)
      sqlite3_close_v2(db);
  }
  void open(const QString &file, bool writable) {
    if (db) {
      sqlite3_close_v2(db);
      db = nullptr;
    }
    const auto path = file.toUtf8();
    if (sqlite3_open_v2(path.constData(), &db,
                        writable ? SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE |
                                       SQLITE_OPEN_NOMUTEX
                                 : SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX,
                        nullptr) != SQLITE_OK)
      fail(db, "Open database");
    sqlite3_busy_timeout(db, 5000);
    execute(db, "PRAGMA foreign_keys=ON");
    if (!writable)
      execute(db, "PRAGMA query_only=ON");
  }
};
struct Statement {
  sqlite3 *db;
  sqlite3_stmt *stmt = nullptr;
  Statement(sqlite3 *connection, const char *sql) : db(connection) {
    if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK)
      fail(db, "Prepare");
  }
  Statement(sqlite3 *connection, const QString &sql)
      : Statement(connection, sql.toUtf8().constData()) {}
  ~Statement() { sqlite3_finalize(stmt); }
  void bind(int pos, const QString &value) {
    const auto bytes = value.toUtf8();
    if (sqlite3_bind_text(stmt, pos, bytes.constData(), int(bytes.size()),
                          SQLITE_TRANSIENT) != SQLITE_OK)
      fail(db, "Bind text");
  }
  void bind(int pos, qint64 value) {
    if (sqlite3_bind_int64(stmt, pos, value) != SQLITE_OK)
      fail(db, "Bind integer");
  }
  void bind(int pos, const QByteArray &value) {
    const auto result =
        value.isEmpty()
            ? sqlite3_bind_null(stmt, pos)
            : sqlite3_bind_blob(stmt, pos, value.constData(), int(value.size()),
                                SQLITE_TRANSIENT);
    if (result != SQLITE_OK)
      fail(db, "Bind vector");
  }
  bool next() {
    const int rc = sqlite3_step(stmt);
    if (rc == SQLITE_ROW)
      return true;
    if (rc != SQLITE_DONE)
      fail(db, "Step");
    return false;
  }
  void run() {
    if (next())
      throw std::runtime_error("Unexpected SQLite result row");
  }
  void reset() {
    sqlite3_reset(stmt);
    sqlite3_clear_bindings(stmt);
  }
  qint64 integer(int col) const { return sqlite3_column_int64(stmt, col); }
  double number(int col) const { return sqlite3_column_double(stmt, col); }
  QString string(int col) const {
    const auto *p = sqlite3_column_text(stmt, col);
    return p ? QString::fromUtf8(reinterpret_cast<const char *>(p),
                                 sqlite3_column_bytes(stmt, col))
             : QString();
  }
  QByteArray bytes(int col) const {
    return QByteArray(static_cast<const char *>(sqlite3_column_blob(stmt, col)),
                      sqlite3_column_bytes(stmt, col));
  }
};
struct Transaction {
  sqlite3 *db;
  bool done = false;
  Transaction(sqlite3 *db_, bool write = false) : db(db_) {
    execute(db, write ? "BEGIN IMMEDIATE" : "BEGIN");
  }
  ~Transaction() {
    if (!done)
      sqlite3_exec(db, "ROLLBACK", nullptr, nullptr, nullptr);
  }
  void commit() {
    execute(db, "COMMIT");
    done = true;
  }
};
qint64 scalar(sqlite3 *db, const char *sql) {
  Statement q(db, sql);
  return q.next() ? q.integer(0) : 0;
}
QString meta(sqlite3 *db, const QString &key) {
  Statement q(db, "SELECT value FROM meta WHERE key=?");
  q.bind(1, key);
  return q.next() ? q.string(0) : QString();
}
void putMeta(sqlite3 *db, const QString &key, const QString &value) {
  Statement q(db, "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO "
                  "UPDATE SET value=excluded.value");
  q.bind(1, key);
  q.bind(2, value);
  q.run();
}
QString keyForPath(const QString &path) {
  auto result = normalizePath(path);
#ifdef Q_OS_WIN
  result = result.toCaseFolded();
#endif
  return result;
}
QVector<float> normalized(const QVector<float> &values) {
  if (values.size() != embeddingDimensions)
    throw std::runtime_error("Embedding must contain exactly 768 values");
  double squared = 0;
  for (float value : values) {
    if (!std::isfinite(value))
      throw std::runtime_error("Embedding contains a non-finite value");
    squared += double(value) * value;
  }
  if (!(squared > 1.e-20))
    throw std::runtime_error("Embedding has zero norm");
  auto result = values;
  const double scale = 1. / std::sqrt(squared);
  for (auto &value : result)
    value = float(value * scale);
  return result;
}
double cosine(const QVector<float> &a, const QVector<float> &b) {
  if (a.size() != b.size() || a.isEmpty())
    return -1;
  double dot = 0, norm = 0;
  for (qsizetype i = 0; i < a.size(); ++i) {
    dot += double(a[i]) * b[i];
    norm += double(b[i]) * b[i];
  }
  return norm > 0 ? dot / std::sqrt(norm) : -1;
}
const QString hitColumns =
    QStringLiteral("s.id,f.path,s.title,s.kind,s.text,s.page,s.line_start,s."
                   "line_end,s.cell,s.start_ms,s.end_ms,f.source_version");
SearchHit readHit(const Statement &s) {
  SearchHit hit;
  hit.segmentId = s.integer(0);
  hit.path = s.string(1);
  hit.title = s.string(2);
  hit.kind = s.string(3);
  hit.text = s.string(4);
  hit.page = int(s.integer(5));
  hit.lineStart = int(s.integer(6));
  hit.lineEnd = int(s.integer(7));
  hit.cell = int(s.integer(8));
  hit.startMs = s.integer(9);
  hit.endMs = s.integer(10);
  hit.sourceVersion = s.string(11);
  return hit;
}
struct Filter {
  QString sql = QStringLiteral("f.code=?");
  QStringList paths;
  QString kind;
  bool code = false;
  QString within(const QString &root) {
    const QString prefix = root.endsWith(u'/') ? root : root + u'/';
    // The normalized prefix always ends in ASCII '/'. Advancing just that
    // final byte to '0' gives the exclusive BINARY upper bound for every
    // descendant, including Unicode names and literal '%'/'_' characters.
    // Unlike substr(), these comparisons can use the path_key index before
    // joining a large segment table. The root itself remains an exact match.
    const QString upper = prefix.chopped(1) + u'0';
    paths.append({root, prefix, upper});
    return QStringLiteral("(f.path_key=? OR "
                          "(f.path_key>=? COLLATE BINARY AND "
                          "f.path_key<? COLLATE BINARY))");
  }
  explicit Filter(const SearchOptions &options)
      : kind(options.kind), code(options.code) {
    if (!kind.isEmpty())
      sql += QStringLiteral(" AND s.kind=?");
    if (!options.root.isEmpty())
      sql += QStringLiteral(" AND ") + within(keyForPath(options.root));
    if (options.enforceFolderRules) {
      QStringList selected;
      QStringList excluded;
      for (const auto &path : options.allowedRoots)
        if (!path.isEmpty()) selected.append(keyForPath(path));
      for (const auto &path : options.excludedRoots)
        if (!path.isEmpty()) excluded.append(keyForPath(path));
      selected.removeDuplicates();
      excluded.removeDuplicates();
      QStringList clauses;
      for (const auto &root : selected) {
        QString clause = within(root);
        const QString prefix = root.endsWith(u'/') ? root : root + u'/';
        // Keep FileCatalog's chosen-root semantics: an explicit nested root
        // overrides exclusions at or above itself. Only strict descendants
        // remain excluded from that root's branch of the union.
        for (const auto &exclude : excluded)
          if (exclude != root && exclude.startsWith(prefix))
            clause += QStringLiteral(" AND NOT ") + within(exclude);
        clauses.append(u'(' + clause + u')');
      }
      sql += clauses.isEmpty()
                 ? QStringLiteral(" AND 0")
                 : QStringLiteral(" AND (") + clauses.join(QStringLiteral(" OR ")) + u')';
    }
  }
  int bind(Statement &q, int first = 1) const {
    q.bind(first++, qint64(code));
    if (!kind.isEmpty())
      q.bind(first++, kind);
    for (const auto &path : paths)
      q.bind(first++, path);
    return first;
  }
};
void report(QString *error, const std::exception &e) {
  if (error)
    *error = QString::fromUtf8(e.what());
}
} // namespace

class IndexRepository::Impl {
public:
  RuntimePaths paths;
  Connection writer;
  mutable std::mutex publication;
  std::mutex maintenanceMutex;
  std::unique_ptr<VectorIndex> ann;
  VectorIndex::SnapshotPtr published;
  QString recoveryWarning;
  QString maintenanceWarning;
  StorageOptions options;
  bool opened = false;
  void requireOpen() const {
    if (!opened)
      throw std::runtime_error("Index repository is not open");
  }
  void diskGuard(qint64 extra = 0) const {
    QStorageInfo disk(paths.indexDir);
    const qint64 available =
        options.availableBytes
            ? options.availableBytes(paths.indexDir)
            : (disk.isValid() && disk.isReady() ? disk.bytesAvailable() : -1);
    if (available >= 0 && available < options.diskReserveBytes + extra)
      throw std::runtime_error("Insufficient disk space for the configured "
                               "index reserve and pending write");
  }
  qint64 generation() const {
    return meta(writer.db, QStringLiteral("generation")).toLongLong();
  }
  std::pair<qint64, qint64> pendingEvents() const {
    qint64 total = 0, largest = 0;
    for (int shard = 0; shard < shardCount; ++shard) {
      Statement q(
          writer.db,
          "SELECT count(*) FROM ann_events WHERE shard=? AND generation>?");
      q.bind(1, qint64(shard));
      q.bind(2, qint64(published->shardGeneration(shard)));
      q.next();
      total += q.integer(0);
      largest = std::max(largest, q.integer(0));
    }
    return {total, largest};
  }
  void checkpoint() {
    int log = 0, checkpointed = 0;
    const int rc = sqlite3_wal_checkpoint_v2(
        writer.db, nullptr, SQLITE_CHECKPOINT_PASSIVE, &log, &checkpointed);
    if (rc != SQLITE_OK && rc != SQLITE_BUSY)
      fail(writer.db, "Checkpoint");
    if (QFileInfo(paths.databaseFile() + QStringLiteral("-wal")).size() >
        256LL * 1024 * 1024)
      throw std::runtime_error("Index write paused because a reader is "
                               "retaining more than 256 MiB of WAL");
  }
  bool buildShard(int shard, QString *error) {
    QVector<VectorRecord> records;
    quint64 generation = 0;
    {
      Connection reader;
      std::unique_ptr<Transaction> transaction;
      {
        std::lock_guard lock(publication);
        requireOpen();
        reader.open(paths.databaseFile(), false);
        transaction=std::make_unique<Transaction>(reader.db);
        generation=meta(reader.db,QStringLiteral("generation")).toULongLong();
      }
      Statement q(reader.db, "SELECT id,embedding FROM segments WHERE shard=? "
                             "AND embedding IS NOT NULL ORDER BY id");
      q.bind(1, qint64(shard));
      while (q.next()) {
        auto vector = decodeFloat16(q.bytes(1), error);
        if (vector.size() != embeddingDimensions)
          throw std::runtime_error("Invalid persisted vector");
        records.append({q.integer(0), std::move(vector)});
      }
      transaction->commit();
      diskGuard(qint64(records.size()) *
                (embeddingDimensions * sizeof(float) + 256));
    }
    // The SQLite read transaction is closed before the expensive graph build,
    // which prevents a long build from retaining an unbounded WAL file.
    if (!ann->rebuildShard(shard, generation, records, error))
      return false;
    if (options.faultHook)
      options.faultHook(
          QStringLiteral("ann-manifest-published-before-outbox-cleanup"));
    {
      std::lock_guard lock(publication);
      published = ann->snapshot();
      Transaction transaction(writer.db, true);
      Statement cleanup(
          writer.db, "DELETE FROM ann_events WHERE shard=? AND generation<=?");
      cleanup.bind(1, qint64(shard));
      cleanup.bind(2, qint64(generation));
      cleanup.run();
      transaction.commit();
      // The graph and cleanup transaction are already durable here. A
      // checkpoint delay is a maintenance warning, never a rolled-back
      // file replacement or a failed graph publication.
      try {
        checkpoint();
      } catch (const std::exception &e) {
        maintenanceWarning = QString::fromUtf8(e.what());
      }
    }
    return true;
  }
};

IndexRepository::IndexRepository(StorageOptions options)
    : d(std::make_unique<Impl>()) {
  d->options = std::move(options);
}
IndexRepository::~IndexRepository() = default;

bool IndexRepository::open(const RuntimePaths &paths, QString *error) {
  if (error)
    error->clear();
  try {
    std::lock_guard lock(d->publication);
    if (d->opened)
      throw std::runtime_error("Index repository is already open");
    if (d->options.diskReserveBytes < 0)
      throw std::runtime_error("Disk reserve must not be negative");
    if (!paths.ensure(error))
      return false;
    d->paths = paths;
    const QFileInfo databaseInfo(paths.databaseFile());
    const bool newDatabase = !databaseInfo.exists() || databaseInfo.size() == 0;
    d->writer.open(paths.databaseFile(), true);
    auto *db = d->writer.db;
    const auto version = scalar(db, "PRAGMA user_version");
    if (version > schemaVersion)
      throw std::runtime_error(
          "Database schema was created by a newer QCopilots Search version");
    // A 768-dimensional FP16 vector and its text metadata often exceed half
    // of a 4 KiB page. Larger pages pack several such rows instead of leaving
    // nearly half of each page unused. Existing databases retain their layout,
    // so opening a profile never triggers a space-doubling VACUUM migration.
    if (newDatabase)
      execute(db, "PRAGMA page_size=16384");
    execute(db, "PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;PRAGMA "
                "wal_autocheckpoint=2048;PRAGMA "
                "journal_size_limit=134217728;PRAGMA temp_store=MEMORY");
    Transaction transaction(db, true);
    execute(db, R"SQL(
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES('generation','0');
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,path TEXT NOT NULL,path_key TEXT NOT NULL,version TEXT NOT NULL,source_version TEXT NOT NULL,code INTEGER NOT NULL,UNIQUE(path_key,code));
CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY AUTOINCREMENT,file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,generation INTEGER NOT NULL,shard INTEGER NOT NULL DEFAULT 0,title TEXT NOT NULL,kind TEXT NOT NULL,modality TEXT NOT NULL,text TEXT NOT NULL,media_path TEXT NOT NULL,page INTEGER NOT NULL,line_start INTEGER NOT NULL,line_end INTEGER NOT NULL,cell INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,embedding BLOB,terms TEXT NOT NULL,han TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS segments_file ON segments(file_id);
CREATE INDEX IF NOT EXISTS segments_generation ON segments(shard,generation) WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS segments_kind ON segments(kind,file_id);
CREATE INDEX IF NOT EXISTS segments_vector_files ON segments(file_id,kind) WHERE embedding IS NOT NULL;
CREATE TABLE IF NOT EXISTS ann_events(generation INTEGER NOT NULL,shard INTEGER NOT NULL,segment_id INTEGER NOT NULL,operation TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ann_events_shard ON ann_events(shard,generation);
CREATE VIRTUAL TABLE IF NOT EXISTS segment_fts USING fts5(terms,han,content='segments',content_rowid='id',tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN INSERT INTO segment_fts(rowid,terms,han) VALUES(new.id,new.terms,new.han); END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN INSERT INTO segment_fts(segment_fts,rowid,terms,han) VALUES('delete',old.id,old.terms,old.han); END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE OF terms,han ON segments BEGIN INSERT INTO segment_fts(segment_fts,rowid,terms,han) VALUES('delete',old.id,old.terms,old.han); INSERT INTO segment_fts(rowid,terms,han) VALUES(new.id,new.terms,new.han); END;
PRAGMA user_version=2;
)SQL");
    if (version == 1) {
      execute(db, "ALTER TABLE files ADD COLUMN source_version TEXT NOT NULL "
                  "DEFAULT '';UPDATE files SET source_version=version");
    }
    if (meta(db, QStringLiteral("index_uuid")).isEmpty())
      putMeta(db, QStringLiteral("index_uuid"),
              QUuid::createUuid().toString(QUuid::WithoutBraces));
    transaction.commit();
    const auto annRoot = QDir(paths.indexDir)
                             .filePath(QStringLiteral("qcopilots-search-ann-") +
                                       meta(db, QStringLiteral("index_uuid")));
    d->ann = std::make_unique<VectorIndex>(annRoot, embeddingDimensions,
                                           d->options.faultHook);
    if (!d->ann->loadManifest(error)) {
      // An ANN manifest is derived state. Reopen with an empty in-memory
      // graph and search the authoritative vectors exactly until flush.
      // Preserve the damaged file for diagnostics instead of trusting it.
      d->recoveryWarning =
          error ? *error : QStringLiteral("Invalid ANN manifest");
    }
    if (d->ann->snapshot()->generation() > quint64(d->generation())) {
      d->recoveryWarning =
          QStringLiteral("ANN generation is newer than this database snapshot. "
                         "Rebuild is required.");
      d->ann = std::make_unique<VectorIndex>(annRoot, embeddingDimensions,
                                             d->options.faultHook);
    }
    d->published = d->ann->snapshot();
    {
      Transaction cleanup(db, true);
      Statement covered(
          db, "DELETE FROM ann_events WHERE shard=? AND generation<=?");
      for (int shard = 0; shard < shardCount; ++shard) {
        covered.bind(1, qint64(shard));
        covered.bind(2, qint64(d->published->shardGeneration(shard)));
        covered.run();
        covered.reset();
      }
      cleanup.commit();
    }
    d->opened = true;
    if (error)
      error->clear();
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}

bool IndexRepository::replaceFile(const QString &path, const QString &version,
                                  const QVector<Segment> &segments, bool code,
                                  QString *error,
                                  const PublicationGate &publicationGate) {
  if (error)
    error->clear();
  return replaceFiles({IndexedFile{path, version, code, segments}}, error,
                      false, publicationGate);
}

bool IndexRepository::replaceFiles(const QVector<IndexedFile> &files,
                                   QString *error, bool deferMaintenance,
                                   const PublicationGate &publicationGate) {
  if (error)
    error->clear();
  try {
    if (files.isEmpty())
      return true;
    struct Prepared {
      const IndexedFile *file;
      QString path, key;
      QVector<QByteArray> vectors;
      QVector<IndexedText> text;
    };
    QVector<Prepared> batch;
    qint64 estimate = 0;
    for (const auto &file : files) {
      if (file.path.isEmpty() || file.version.isEmpty())
        throw std::runtime_error(
            "File path and content version must not be empty");
      Prepared prepared{
          &file, normalizePath(file.path), keyForPath(file.path), {}, {}};
      for (const auto &segment : file.segments) {
        auto encoded = segment.embedding.isEmpty()
                           ? QByteArray()
                           : encodeFloat16(normalized(segment.embedding));
        estimate += encoded.size() + segment.text.size() * 4 + 4096;
        prepared.vectors.append(std::move(encoded));
        prepared.text.append(tokenizeForIndex(
            prepared.path + u' ' + segment.title + u' ' + segment.text));
      }
      batch.append(std::move(prepared));
    }
    {
      std::lock_guard lock(d->publication);
      const auto lease =
          publicationGate ? publicationGate() : std::shared_ptr<void>();
      if (publicationGate && !lease)
        throw std::runtime_error(
            "Discarded obsolete file extraction before committing its index");
      d->requireOpen();
      d->diskGuard(estimate);
      d->checkpoint();
      auto *db = d->writer.db;
      Transaction transaction(db, true);
      const auto generation = d->generation() + 1;
      Statement insertFile(
          db, "INSERT INTO files(path,path_key,version,source_version,code) "
              "VALUES(?,?,?,?,?) "
              "ON CONFLICT(path_key,code) DO UPDATE SET "
              "path=excluded.path,version=excluded.version,source_version="
              "excluded.source_version RETURNING id");
      Statement oppositeEvents(
          db, "INSERT INTO ann_events SELECT ?,s.shard,s.id,'delete' FROM "
              "segments s JOIN files f ON f.id=s.file_id WHERE f.path_key=? "
              "AND f.code<>? AND s.embedding IS NOT NULL");
      Statement removeOpposite(
          db, "DELETE FROM files WHERE path_key=? AND code<>?");
      Statement eventDelete(
          db, "INSERT INTO ann_events SELECT ?,shard,id,'delete' FROM segments "
              "WHERE file_id=? AND embedding IS NOT NULL");
      Statement deleteSegments(db, "DELETE FROM segments WHERE file_id=?");
      Statement insertSegment(
          db, "INSERT INTO "
              "segments(file_id,generation,title,kind,modality,text,media_path,"
              "page,line_start,line_end,cell,start_ms,end_ms,embedding,terms,"
              "han) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
      Statement shardUpdate(db, "UPDATE segments SET shard=? WHERE id=?");
      Statement eventInsert(db,
                            "INSERT INTO ann_events VALUES(?,?,?,'upsert')");
      for (const auto &prepared : batch) {
        const auto &file = *prepared.file;
        oppositeEvents.bind(1, generation);
        oppositeEvents.bind(2, prepared.key);
        oppositeEvents.bind(3, qint64(file.code));
        oppositeEvents.run();
        oppositeEvents.reset();
        removeOpposite.bind(1, prepared.key);
        removeOpposite.bind(2, qint64(file.code));
        removeOpposite.run();
        removeOpposite.reset();
        insertFile.bind(1, prepared.path);
        insertFile.bind(2, prepared.key);
        insertFile.bind(3, file.version);
        insertFile.bind(4, file.version);
        insertFile.bind(5, qint64(file.code));
        if (!insertFile.next())
          throw std::runtime_error("Missing inserted file id");
        const qint64 fileId = insertFile.integer(0);
        insertFile.reset();
        eventDelete.bind(1, generation);
        eventDelete.bind(2, fileId);
        eventDelete.run();
        eventDelete.reset();
        deleteSegments.bind(1, fileId);
        deleteSegments.run();
        deleteSegments.reset();
        for (qsizetype i = 0; i < file.segments.size(); ++i) {
          const auto &s = file.segments[i];
          int p = 1;
          insertSegment.bind(p++, fileId);
          insertSegment.bind(p++, generation);
          insertSegment.bind(p++, s.title);
          insertSegment.bind(p++, s.kind);
          insertSegment.bind(p++, s.modality);
          insertSegment.bind(p++, s.text);
          insertSegment.bind(p++, s.mediaPath);
          insertSegment.bind(p++, qint64(s.page));
          insertSegment.bind(p++, qint64(s.lineStart));
          insertSegment.bind(p++, qint64(s.lineEnd));
          insertSegment.bind(p++, qint64(s.cell));
          insertSegment.bind(p++, s.startMs);
          insertSegment.bind(p++, s.endMs);
          insertSegment.bind(p++, prepared.vectors[i]);
          insertSegment.bind(p++, prepared.text[i].terms);
          insertSegment.bind(p++, prepared.text[i].han);
          insertSegment.run();
          insertSegment.reset();
          const auto id = sqlite3_last_insert_rowid(db);
          const auto shard = VectorIndex::shardForId(id);
          shardUpdate.bind(1, qint64(shard));
          shardUpdate.bind(2, id);
          shardUpdate.run();
          shardUpdate.reset();
          if (!prepared.vectors[i].isEmpty()) {
            eventInsert.bind(1, generation);
            eventInsert.bind(2, qint64(shard));
            eventInsert.bind(3, id);
            eventInsert.run();
            eventInsert.reset();
          }
        }
      }
      if (deferMaintenance)
        putMeta(db, QStringLiteral("deferred_maintenance"),
                QStringLiteral("1"));
      putMeta(db, QStringLiteral("generation"), QString::number(generation));
      transaction.commit();
    }
    if (!deferMaintenance) {
      while (true) {
        qint64 beforeTotal = 0;
        bool pending;
        {
          std::lock_guard lock(d->publication);
          const auto state = d->pendingEvents();
          beforeTotal = state.first;
          pending = state.second >= deltaLimit;
        }
        if (!pending)
          break;
        QString warning;
        if (!flushOneShard(&warning)) {
          std::lock_guard lock(d->publication);
          d->maintenanceWarning = warning;
          break;
        }
        {
          std::lock_guard lock(d->publication);
          if (d->pendingEvents().first >= beforeTotal) {
            d->maintenanceWarning =
                QStringLiteral("ANN maintenance made no progress. Indexing is "
                               "paused for retry.");
            break;
          }
        }
      }
    }
    if (error)
      error->clear();
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}

bool IndexRepository::removeFile(const QString &path, QString *error) {
  if (error)
    error->clear();
  try {
    std::lock_guard lock(d->publication);
    d->requireOpen();
    auto *db = d->writer.db;
    Transaction transaction(db, true);
    const auto gen = d->generation() + 1;
    Statement events(db,
                     "INSERT INTO ann_events SELECT ?,s.shard,s.id,'delete' "
                     "FROM segments s JOIN files f ON f.id=s.file_id WHERE "
                     "f.path_key=? AND s.embedding IS NOT NULL");
    events.bind(1, gen);
    events.bind(2, keyForPath(path));
    events.run();
    Statement remove(db, "DELETE FROM files WHERE path_key=?");
    remove.bind(1, keyForPath(path));
    remove.run();
    putMeta(db, "generation", QString::number(gen));
    transaction.commit();
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}

bool IndexRepository::prune(const QString &root,
                            const QSet<QString> &presentPaths, QString *error) {
  if (error)
    error->clear();
  try {
    if (root.isEmpty())
      throw std::runtime_error("Pruning requires an explicit nonempty root");
    QSet<QString> present;
    for (const auto &path : presentPaths)
      present.insert(keyForPath(path));
    std::lock_guard lock(d->publication);
    d->requireOpen();
    auto *db = d->writer.db;
    Transaction transaction(db, true);
    const auto gen = d->generation() + 1;
    Statement all(db, "SELECT id,path,path_key FROM files");
    QVector<qint64> removed;
    while (all.next())
      if (isPathWithin(all.string(1), root) && !present.contains(all.string(2)))
        removed.append(all.integer(0));
    Statement event(db,
                    "INSERT INTO ann_events SELECT ?,shard,id,'delete' FROM "
                    "segments WHERE file_id=? AND embedding IS NOT NULL");
    Statement remove(db, "DELETE FROM files WHERE id=?");
    for (auto id : removed) {
      event.bind(1, gen);
      event.bind(2, id);
      event.run();
      event.reset();
      remove.bind(1, id);
      remove.run();
      remove.reset();
    }
    if (!removed.isEmpty())
      putMeta(db, "generation", QString::number(gen));
    transaction.commit();
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}

QString IndexRepository::fileVersion(const QString &path, bool code,
                                     QString *error) const {
  if (error)
    error->clear();
  try {
    std::lock_guard lock(d->publication);
    d->requireOpen();
    Statement q(d->writer.db,
                "SELECT version FROM files WHERE path_key=? AND code=?");
    q.bind(1, keyForPath(path));
    q.bind(2, qint64(code));
    return q.next() ? q.string(0) : QString();
  } catch (const std::exception &e) {
    report(error, e);
    return {};
  }
}

QVector<Segment> IndexRepository::fileSegments(const QString &path, bool code,
                                               QString *error) const {
  if (error)
    error->clear();
  try {
    Connection reader;
    {
      std::lock_guard lock(d->publication);
      d->requireOpen();
      reader.open(d->paths.databaseFile(), false);
    }
    Statement q(reader.db,
                "SELECT "
                "s.id,f.path,s.title,s.kind,s.modality,s.text,s.media_path,s."
                "page,s.line_start,s.line_end,s.cell,s.start_ms,s.end_ms,s."
                "embedding FROM segments s JOIN files f ON f.id=s.file_id "
                "WHERE f.path_key=? AND f.code=? ORDER BY s.id");
    q.bind(1, keyForPath(path));
    q.bind(2, qint64(code));
    QVector<Segment> result;
    while (q.next()) {
      Segment s;
      s.id = q.integer(0);
      s.path = q.string(1);
      s.title = q.string(2);
      s.kind = q.string(3);
      s.modality = q.string(4);
      s.text = q.string(5);
      s.mediaPath = q.string(6);
      s.page = int(q.integer(7));
      s.lineStart = int(q.integer(8));
      s.lineEnd = int(q.integer(9));
      s.cell = int(q.integer(10));
      s.startMs = q.integer(11);
      s.endMs = q.integer(12);
      const auto bytes = q.bytes(13);
      if (!bytes.isEmpty()) {
        s.embedding = decodeFloat16(bytes, error);
        if (s.embedding.size() != embeddingDimensions)
          throw std::runtime_error("Invalid persisted embedding");
      }
      result.append(std::move(s));
    }
    return result;
  } catch (const std::exception &e) {
    report(error, e);
    return {};
  }
}

QList<SearchHit> IndexRepository::search(const SearchOptions &options,
                                         QString *error) const {
  if (error)
    error->clear();
  try {
    auto checkCancelled = [&]() {
      if (options.cancelled &&
          options.cancelled->load(std::memory_order_relaxed))
        throw std::runtime_error("Search cancelled");
    };
    checkCancelled();
    struct SqliteProgress {
      const std::atomic_bool *cancelled = nullptr;
      const std::function<void(const QString &)> *hook = nullptr;
      bool notified = false;
    } progress{options.cancelled.get(), &d->options.faultHook, false};
    Connection reader;
    VectorIndex::SnapshotPtr snapshot;
    std::unique_ptr<Transaction> transaction;
    {
      std::lock_guard lock(d->publication);
      d->requireOpen();
      reader.open(d->paths.databaseFile(), false);
      transaction = std::make_unique<Transaction>(reader.db);
      // BEGIN DEFERRED alone does not establish a read snapshot. This read
      // must happen under the publication lock before capturing ANN state.
      (void)meta(reader.db, QStringLiteral("generation"));
      snapshot = d->published;
    }
    if (options.cancelled || d->options.faultHook)
      sqlite3_progress_handler(
          reader.db, 1000,
          [](void *state) noexcept {
            auto &progress = *static_cast<SqliteProgress *>(state);
            // This optional observation hook fires once from inside SQLite's
            // virtual machine, after the publication lock has been released.
            // No C++ exception may cross the SQLite C callback boundary.
            if (!progress.notified && progress.hook && *progress.hook) {
              progress.notified = true;
              try {
                (*progress.hook)(QStringLiteral("sqlite-search-progress"));
              } catch (...) {
                return 1;
              }
            }
            return progress.cancelled &&
                           progress.cancelled->load(std::memory_order_relaxed)
                       ? 1
                       : 0;
          },
          &progress);
    const int limit = std::clamp(options.limit, 1, 1000);
    const int candidates = std::clamp(limit * 16, 256, 16000);
    const Filter filter(options);
    auto *db = reader.db;
    QList<SearchHit> keyword, semantic;
    const auto expression = keywordMatchExpression(options.query);
    if (!expression.isEmpty()) {
      Statement q(
          db,
          QStringLiteral("SELECT ") + hitColumns +
              QStringLiteral(" FROM segment_fts JOIN segments s ON "
                             "s.id=segment_fts.rowid JOIN files f ON "
                             "f.id=s.file_id WHERE segment_fts MATCH ? AND ") +
              filter.sql + QStringLiteral(" ORDER BY bm25(segment_fts),s.id"));
      q.bind(1, expression);
      filter.bind(q, 2);
      QSet<QString> keywordPaths;
      while (q.next()) {
        checkCancelled();
        auto hit = readHit(q);
        if (!keywordPaths.contains(hit.path)) {
          keywordPaths.insert(hit.path);
          keyword.append(std::move(hit));
        }
        if (keyword.size() >= candidates)
          break;
      }
    }
    if (!options.queryEmbedding.isEmpty()) {
      if (meta(db, QStringLiteral("deferred_maintenance")) ==
          QStringLiteral("1"))
        throw std::runtime_error(
            "Semantic search is unavailable during an offline bulk import. "
            "Complete ANN maintenance with flush before querying.");
      const auto query = normalized(options.queryEmbedding);
      Statement count(
          db,
          QStringLiteral("SELECT count(*) FROM (SELECT 1 FROM segments s JOIN files f ON "
                         "f.id=s.file_id WHERE s.embedding IS NOT NULL AND ") +
              filter.sql + QStringLiteral(" LIMIT 20001)"));
      filter.bind(count);
      count.next();
      const auto size = count.integer(0);
      QHash<qint64, double> scores;
      auto scoreRows = [&](Statement &q) {
        while (q.next()) {
          checkCancelled();
          const auto vec = decodeFloat16(q.bytes(1), error);
          if (vec.size() != embeddingDimensions)
            throw std::runtime_error("Invalid persisted embedding");
          scores.insert(q.integer(0), cosine(query, vec));
        }
      };
      if (size <= exactSubsetLimit) {
        Statement q(
            db, QStringLiteral(
                    "SELECT s.id,s.embedding FROM segments s JOIN files f ON "
                    "f.id=s.file_id WHERE s.embedding IS NOT NULL AND ") +
                    filter.sql);
        filter.bind(q);
        scoreRows(q);
      } else {
        QSet<qint64> allowed;
        auto fileOptions = options;
        fileOptions.kind.clear();
        const Filter fileFilter(fileOptions);
        Statement outside(db, QStringLiteral("SELECT EXISTS(SELECT 1 FROM files f WHERE NOT(") +
                                  fileFilter.sql + QStringLiteral("))"));
        fileFilter.bind(outside);
        outside.next();
        // This proof uses the same established SQLite snapshot as the vector
        // count and ANN pointer. Only complete file-filter coverage permits
        // skipping the potentially million-entry allow-list. Kind filters
        // continue to build it, and pending tombstones are always applied.
        const bool filtered = !options.kind.isEmpty() || outside.integer(0) != 0;
        if (filtered) {
          Statement q(db,
                      QStringLiteral(
                          "SELECT s.id FROM segments s JOIN files f ON "
                          "f.id=s.file_id WHERE s.embedding IS NOT NULL AND ") +
                          filter.sql);
          filter.bind(q);
          while (q.next())
            allowed.insert(q.integer(0));
        }
        QSet<qint64> excluded;
        for (int shard = 0; shard < shardCount; ++shard) {
          Statement q(db, "SELECT segment_id FROM ann_events WHERE shard=? AND "
                          "generation>? AND operation='delete'");
          q.bind(1, qint64(shard));
          q.bind(2, qint64(snapshot->shardGeneration(shard)));
          while (q.next())
            excluded.insert(q.integer(0));
        }
        QString annError;
        Statement get(db, QStringLiteral(
                              "SELECT s.id,s.embedding,f.path_key FROM "
                              "segments s JOIN files f ON f.id=s.file_id WHERE "
                              "s.id=? AND s.embedding IS NOT NULL AND ") +
                              filter.sql);
        int requested = candidates;
        QSet<QString> candidatePaths;
        while (true) {
          if (requested > 100000) {
            // An exceptionally long file can occupy the whole bounded ANN
            // candidate budget. Stream the authoritative set instead of
            // silently returning fewer files even though more matching files
            // exist.
            Statement all(
                db,
                QStringLiteral(
                    "SELECT s.id,s.embedding FROM segments s JOIN files f ON "
                    "f.id=s.file_id WHERE s.embedding IS NOT NULL AND ") +
                    filter.sql);
            filter.bind(all);
            scoreRows(all);
            break;
          }
          const auto neighbors = d->ann->search(
              snapshot, query, requested, filtered ? &allowed : nullptr,
              &annError, &excluded, options.cancelled.get());
          if (!annError.isEmpty())
            throw std::runtime_error(annError.toStdString());
          for (const auto &neighbor : neighbors) {
            checkCancelled();
            if (scores.contains(neighbor.id))
              continue;
            get.bind(1, neighbor.id);
            filter.bind(get, 2);
            if (get.next()) {
              const auto vec = decodeFloat16(get.bytes(1), error);
              if (vec.size() != embeddingDimensions)
                throw std::runtime_error("Invalid persisted embedding");
              scores.insert(get.integer(0), cosine(query, vec));
              candidatePaths.insert(get.string(2));
            }
            get.reset();
          }
          if (candidatePaths.size() >= limit || neighbors.size() < requested ||
              requested >= snapshot->size())
            break;
          requested = int(
              std::min<qsizetype>(snapshot->size(), qsizetype(requested) * 2));
        }
        // New and replaced vectors are visible from the same SQLite
        // snapshot even before their derived shard has been rebuilt.
        for (int shard = 0; shard < shardCount; ++shard) {
          Statement delta(
              db, QStringLiteral(
                      "SELECT s.id,s.embedding FROM segments s JOIN files f ON "
                      "f.id=s.file_id WHERE s.shard=? AND s.generation>? AND "
                      "s.embedding IS NOT NULL AND ") +
                      filter.sql);
          delta.bind(1, qint64(shard));
          delta.bind(2, qint64(snapshot->shardGeneration(shard)));
          filter.bind(delta, 3);
          scoreRows(delta);
        }
      }
      QVector<QPair<qint64, double>> ranked;
      ranked.reserve(scores.size());
      for (auto i = scores.cbegin(); i != scores.cend(); ++i)
        ranked.append({i.key(), i.value()});
      std::sort(ranked.begin(), ranked.end(),
                [&](const auto &a, const auto &b) {
                  checkCancelled();
                  return a.second == b.second ? a.first < b.first
                                              : a.second > b.second;
                });
      Statement get(db, QStringLiteral("SELECT ") + hitColumns +
                            QStringLiteral(" FROM segments s JOIN files f ON "
                                           "f.id=s.file_id WHERE s.id=?"));
      QSet<QString> paths;
      for (const auto &value : ranked) {
        get.bind(1, value.first);
        if (get.next()) {
          auto hit = readHit(get);
          if (!paths.contains(hit.path)) {
            paths.insert(hit.path);
            hit.score = value.second;
            semantic.append(std::move(hit));
          }
        }
        get.reset();
        if (semantic.size() >= candidates)
          break;
      }
    }
    if (expression.isEmpty() && options.queryEmbedding.isEmpty()) {
      Statement q(
          db,
          QStringLiteral("SELECT ") + hitColumns +
              QStringLiteral(
                  " FROM segments s JOIN files f ON f.id=s.file_id WHERE s.id IN "
                  "(SELECT max(s.id) FROM segments s JOIN files f ON f.id=s.file_id WHERE ") +
              filter.sql + QStringLiteral(" GROUP BY s.file_id ORDER BY max(s.id) DESC LIMIT ?) "
                                           "ORDER BY s.id DESC"));
      const auto end = filter.bind(q);
      q.bind(end, qint64(candidates));
      while (q.next())
        keyword.append(readHit(q));
    }
    QHash<QString, SearchHit> combined;
    auto fuse = [&](const QList<SearchHit> &hits, const QString &source) {
      QSet<QString> visited;
      int rank = 0;
      for (auto hit : hits) {
        if (visited.contains(hit.path))
          continue;
        visited.insert(hit.path);
        const double score = 1.0 / (60.0 + ++rank);
        auto it = combined.find(hit.path);
        if (it == combined.end()) {
          hit.score = score;
          hit.matchSource = source;
          combined.insert(hit.path, std::move(hit));
        } else {
          it->score += score;
          it->matchSource = QStringLiteral("hybrid");
        }
      }
    };
    fuse(keyword, QStringLiteral("keyword"));
    fuse(semantic, QStringLiteral("semantic"));
    QList<SearchHit> hits = combined.values();
    std::sort(hits.begin(), hits.end(), [](const auto &a, const auto &b) {
      return a.score == b.score ? a.path < b.path : a.score > b.score;
    });
    if (hits.size() > limit)
      hits.resize(limit);
    checkCancelled();
    transaction->commit();
    return hits;
  } catch (const std::exception &e) {
    report(error, e);
    if (error && options.cancelled &&
        options.cancelled->load(std::memory_order_relaxed))
      *error = QStringLiteral("Search cancelled");
    return {};
  }
}

QJsonObject IndexRepository::status(QString *error) const {
  if (error)
    error->clear();
  try {
    std::lock_guard lock(d->publication);
    d->requireOpen();
    auto *db = d->writer.db;
    return {
        {"files", scalar(db, "SELECT count(*) FROM files")},
        {"segments", scalar(db, "SELECT count(*) FROM segments")},
        {"vectors",
         scalar(db,
                "SELECT count(*) FROM segments WHERE embedding IS NOT NULL")},
        {"generation", d->generation()},
        {"annVectors", qint64(d->published->size())},
        {"annGeneration", qint64(d->published->generation())},
        {"annEfSearch", VectorIndex::searchEf},
        {"schemaVersion", schemaVersion},
        {"pendingShards",
         scalar(db, "SELECT count(DISTINCT shard) FROM ann_events")},
        {"pipelineFingerprint", meta(db, "pipeline_fingerprint")},
        {"databasePath", d->paths.databaseFile()},
        {"chineseDictionary", chineseDictionaryPath()},
        {"deferredMaintenance",
         meta(db, "deferred_maintenance") == QStringLiteral("1")},
        {"recoveryWarning", d->recoveryWarning},
        {"maintenanceWarning", d->maintenanceWarning}};
  } catch (const std::exception &e) {
    report(error, e);
    return {};
  }
}

bool IndexRepository::flush(QString *error) {
  if (error)
    error->clear();
  try {
    std::lock_guard builder(d->maintenanceMutex);
    for (int shard = 0; shard < shardCount; ++shard) {
      bool needed;
      {
        std::lock_guard lock(d->publication);
        d->requireOpen();
        Statement q(d->writer.db,
                    "SELECT EXISTS(SELECT 1 FROM ann_events WHERE shard=? AND "
                    "generation>?) OR EXISTS(SELECT 1 FROM segments WHERE "
                    "shard=? AND embedding IS NOT NULL AND generation>?)");
        q.bind(1, qint64(shard));
        q.bind(2, qint64(d->published->shardGeneration(shard)));
        q.bind(3, qint64(shard));
        q.bind(4, qint64(d->published->shardGeneration(shard)));
        q.next();
        needed = q.integer(0) != 0;
      }
      if (needed && !d->buildShard(shard, error))
        return false;
    }
    {
      std::lock_guard lock(d->publication);
      putMeta(d->writer.db, "deferred_maintenance", "0");
      d->recoveryWarning.clear();
    }
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}
bool IndexRepository::maintenance(QString *error) {
  if (error)
    error->clear();
  try {
    std::unique_lock builder(d->maintenanceMutex, std::try_to_lock);
    if (!builder.owns_lock())
      return true;
    int selected = -1;
    qint64 largest = deltaLimit - 1;
    {
      std::lock_guard lock(d->publication);
      d->requireOpen();
      for (int shard = 0; shard < shardCount; ++shard) {
        Statement q(
            d->writer.db,
            "SELECT count(*) FROM ann_events WHERE shard=? AND generation>?");
        q.bind(1, qint64(shard));
        q.bind(2, qint64(d->published->shardGeneration(shard)));
        q.next();
        if (q.integer(0) > largest) {
          largest = q.integer(0);
          selected = shard;
        }
      }
    }
    return selected < 0 || d->buildShard(selected, error);
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}
bool IndexRepository::flushOneShard(QString *error) {
  if (error)
    error->clear();
  try {
    std::lock_guard builder(d->maintenanceMutex);
    int selected = -1;
    qint64 largest = 0;
    {
      std::lock_guard lock(d->publication);
      d->requireOpen();
      for (int shard = 0; shard < shardCount; ++shard) {
        Statement q(
            d->writer.db,
            "SELECT count(*) FROM ann_events WHERE shard=? AND generation>?");
        q.bind(1, qint64(shard));
        q.bind(2, qint64(d->published->shardGeneration(shard)));
        q.next();
        if (q.integer(0) > largest) {
          largest = q.integer(0);
          selected = shard;
        }
      }
    }
    return selected < 0 || d->buildShard(selected, error);
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}
bool IndexRepository::checkIntegrity(QString *error) const {
  if (error)
    error->clear();
  try {
    std::lock_guard lock(d->publication);
    d->requireOpen();
    Statement q(d->writer.db, "PRAGMA integrity_check");
    if (!q.next() || q.string(0) != QStringLiteral("ok"))
      throw std::runtime_error("SQLite integrity check failed");
    execute(d->writer.db, "INSERT INTO segment_fts(segment_fts,rank) "
                          "VALUES('integrity-check',1)");
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}
bool IndexRepository::setPipelineFingerprint(const QString &fingerprint,
                                             QString *error) {
  if (error)
    error->clear();
  try {
    if (fingerprint.isEmpty())
      throw std::runtime_error("Pipeline fingerprint must not be empty");
    std::lock_guard lock(d->publication);
    d->requireOpen();
    auto *db = d->writer.db;
    const auto old = meta(db, "pipeline_fingerprint");
    if (old == fingerprint)
      return true;
    Transaction transaction(db, true);
    const auto gen = d->generation() + 1;
    if (!old.isEmpty()) {
      Statement events(db, "INSERT INTO ann_events SELECT ?,shard,id,'delete' "
                           "FROM segments WHERE embedding IS NOT NULL");
      events.bind(1, gen);
      events.run();
      execute(db,
              "UPDATE segments SET embedding=NULL;UPDATE files SET version=''");
    }
    putMeta(db, "pipeline_fingerprint", fingerprint);
    putMeta(db, "generation", QString::number(gen));
    if (d->options.faultHook)
      d->options.faultHook(QStringLiteral("before-pipeline-commit"));
    transaction.commit();
    return true;
  } catch (const std::exception &e) {
    report(error, e);
    return false;
  }
}
QString IndexRepository::pipelineFingerprint(QString *error) const {
  if (error)
    error->clear();
  try {
    std::lock_guard lock(d->publication);
    d->requireOpen();
    return meta(d->writer.db, "pipeline_fingerprint");
  } catch (const std::exception &e) {
    report(error, e);
    return {};
  }
}
QString IndexRepository::databasePath() const {
  return d->paths.databaseFile();
}

} // namespace qcopilots_search
