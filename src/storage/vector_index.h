#pragma once

#include <QByteArray>
#include <QSet>
#include <QString>
#include <QVector>

#include <array>
#include <atomic>
#include <functional>
#include <memory>

namespace qcopilots_search {

struct VectorRecord {
    qint64 id = 0;
    QVector<float> vector;
};

// Cosine distance, where 0 is an exact match and 2 is the opposite direction.
struct VectorNeighbor {
    qint64 id = 0;
    float distance = 0;
};

// The SQLite representation is little-endian IEEE 754 binary16. Encoding rejects
// non-finite and overflowing inputs. Decoding reports invalid blobs via error.
QByteArray encodeFloat16(const QVector<float> &values);
QVector<float> decodeFloat16(const QByteArray &bytes, QString *error = nullptr);

class VectorIndex {
    struct Shard;

public:
    static constexpr int shardCount = 16;
    // Query breadth is a runtime policy, not part of the persisted graph shape.
    static constexpr int searchEf = 4096;

    class Snapshot {
    public:
        quint64 generation() const noexcept { return generation_; }
        qsizetype size() const noexcept { return size_; }
        quint64 shardGeneration(int shard) const noexcept;

    private:
        friend class VectorIndex;
        std::array<std::shared_ptr<const Shard>, shardCount> shards_{};
        std::array<quint64, shardCount> shardGenerations_{};
        quint64 generation_ = 0;
        qsizetype size_ = 0;
        int dimensions_ = 0;
    };

    using SnapshotPtr = std::shared_ptr<const Snapshot>;

    explicit VectorIndex(QString root, int dimensions = 768,
                         std::function<void(const QString &)> faultHook = {});
    ~VectorIndex();
    VectorIndex(const VectorIndex &) = delete;
    VectorIndex &operator=(const VectorIndex &) = delete;

    // Absence of a manifest means an empty index. A malformed manifest never
    // replaces the current snapshot. The root must be exclusively owned by the
    // service's profile lock, including while recovering or rebuilding shards.
    bool loadManifest(QString *error = nullptr);

    // Each record must have a positive ID belonging to the requested shard.
    // A new graph is built privately, saved, and atomically published. Existing
    // queries keep their graph and its backing file until their snapshot ends.
    bool rebuildShard(int shard, quint64 generation,
                      const QVector<VectorRecord> &records, QString *error = nullptr);

    SnapshotPtr snapshot() const;

    // The filter participates in ANN traversal. Small allowed sets use an exact
    // path. nullptr allows every ID, whereas an empty set allows none.
    QVector<VectorNeighbor> search(const SnapshotPtr &snapshot,
                                   const QVector<float> &query, int k,
                                   const QSet<qint64> *allowedIds = nullptr,
                                   QString *error = nullptr,
                                   const QSet<qint64> *excludedIds = nullptr,
                                   const std::atomic_bool *cancelled = nullptr) const;

    static int shardForId(qint64 id) noexcept;

private:
    class Private;
    std::unique_ptr<Private> d_;
};

} // namespace qcopilots_search
