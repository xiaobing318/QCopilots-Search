#include "storage/vector_index.h"

#include <QCryptographicHash>
#include <QDataStream>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLoggingCategory>
#include <QRegularExpression>
#include <QSaveFile>
#include <QStorageInfo>
#include <QUuid>
#include <QtEndian>
#include <qfloat16.h>

#include <hnswlib/hnswlib.h>

#include <algorithm>
#include <atomic>
#include <bit>
#include <cmath>
#include <cstring>
#include <exception>
#include <limits>
#include <mutex>
#include <stdexcept>

namespace qcopilots_search {
namespace {

Q_LOGGING_CATEGORY(vectorIndexLog, "qcopilots-search.storage.vector-index")

constexpr char graphMagic[] = "qcopilots-search-hnsw-v1";
constexpr auto manifestName = "qcs-ann-manifest.json";
constexpr int graphM = 24;
constexpr int constructionEf = 200;
constexpr int queryEf = VectorIndex::searchEf;
constexpr qsizetype exactFilterLimit = 8192;
constexpr qsizetype maximumManifestBytes = 1024 * 1024;
constexpr quint64 maximumShardElements = 10000000;
using Graph = hnswlib::HierarchicalNSW<float>;

void setError(QString *error, const QString &message)
{
    if (error)
        *error = message;
}

bool fail(QString *error, const QString &message)
{
    setError(error, message);
    return false;
}

QString exceptionText(const std::exception &exception)
{
    return QString::fromUtf8(exception.what());
}

QVector<float> normalized(const QVector<float> &input, int dimensions)
{
    if (input.size() != dimensions)
        throw std::invalid_argument("Embedding dimension does not match the index");
    double sum = 0;
    for (float value : input) {
        if (!std::isfinite(value))
            throw std::invalid_argument("Embedding contains a non-finite value");
        sum += double(value) * value;
    }
    if (!(sum > 0) || !std::isfinite(sum))
        throw std::invalid_argument("Embedding must have a finite, non-zero norm");
    const double scale = 1.0 / std::sqrt(sum);
    QVector<float> result;
    result.reserve(input.size());
    for (float value : input)
        result.append(float(value * scale));
    return result;
}

bool readExactly(QIODevice &device, char *data, qint64 bytes)
{
    while (bytes > 0) {
        const auto count = device.read(data, bytes);
        if (count <= 0)
            return false;
        bytes -= count;
        data += count;
    }
    return true;
}

bool writeExactly(QIODevice &device, const char *data, qint64 bytes)
{
    while (bytes > 0) {
        const auto count = device.write(data, bytes);
        if (count <= 0)
            return false;
        bytes -= count;
        data += count;
    }
    return true;
}

QByteArray fileHash(const QString &path, QString *error)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        fail(error, QStringLiteral("Cannot read ANN shard: %1").arg(file.errorString()));
        return {};
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file)) {
        fail(error, QStringLiteral("Cannot hash ANN shard: %1").arg(file.errorString()));
        return {};
    }
    return hash.result().toHex();
}

// hnswlib's filename API uses narrow std::fstream paths. This pinned adapter
// instead serializes its immutable graph through QFile, which handles Unicode
// paths on Windows. The cache format is checked before touching graph memory.
// It is deliberately separate from hnswlib's native persistence format.
bool saveGraph(const Graph &graph, const QString &path, int dimensions, int shard,
               quint64 generation, QString *error)
{
    static_assert(std::endian::native == std::endian::little);
    static_assert(sizeof(hnswlib::labeltype) == 8);
    static_assert(sizeof(hnswlib::tableint) == 4);
    static_assert(sizeof(float) == 4);
    QSaveFile file(path);
    file.setDirectWriteFallback(false);
    if (!file.open(QIODevice::WriteOnly))
        return fail(error, QStringLiteral("Cannot create ANN shard: %1").arg(file.errorString()));
    QDataStream stream(&file);
    stream.setByteOrder(QDataStream::LittleEndian);
    const auto count = graph.cur_element_count.load();
    stream.writeRawData(graphMagic, sizeof(graphMagic));
    stream << quint32(1) << quint32(dimensions) << quint32(shard) << generation
           << quint64(count) << quint32(graphM) << quint32(constructionEf)
           << quint32(queryEf) << quint64(graph.size_data_per_element_)
           << qint32(graph.maxlevel_) << quint32(graph.enterpoint_node_);
    if (stream.status() != QDataStream::Ok ||
        !writeExactly(file, graph.data_level0_memory_, qint64(count * graph.size_data_per_element_)))
        return fail(error, QStringLiteral("Cannot write ANN shard: %1").arg(file.errorString()));
    for (size_t i = 0; i < count; ++i) {
        const int level = graph.element_levels_[i];
        stream << qint32(level);
        if (stream.status() != QDataStream::Ok ||
            (level > 0 && !writeExactly(file, graph.linkLists_[i],
                                       qint64(level * graph.size_links_per_element_))))
            return fail(error, QStringLiteral("Cannot write ANN links: %1").arg(file.errorString()));
    }
    if (!file.commit())
        return fail(error, QStringLiteral("Cannot publish ANN shard: %1").arg(file.errorString()));
    return true;
}

bool loadGraph(Graph &graph, const QString &path, int dimensions, int shard,
               quint64 generation, quint64 expectedCount, QString *error)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return fail(error, QStringLiteral("Cannot open ANN shard: %1").arg(file.errorString()));
    char magic[sizeof(graphMagic)]{};
    if (!readExactly(file, magic, sizeof(magic)) || std::memcmp(magic, graphMagic, sizeof(magic)) != 0)
        return fail(error, QStringLiteral("ANN shard has an unsupported format"));
    QDataStream stream(&file);
    stream.setByteOrder(QDataStream::LittleEndian);
    quint32 format, savedDimensions, savedShard, m, efBuild, efQuery, entry;
    quint64 savedGeneration, count, stride;
    qint32 maxLevel;
    stream >> format >> savedDimensions >> savedShard >> savedGeneration >> count
           >> m >> efBuild >> efQuery >> stride >> maxLevel >> entry;
    if (stream.status() != QDataStream::Ok || format != 1 ||
        savedDimensions != quint32(dimensions) || savedShard != quint32(shard) ||
        savedGeneration != generation || count != expectedCount || count == 0 ||
        count > maximumShardElements || m != graphM || efBuild != constructionEf ||
        // Saved query breadth is informational. In particular, a graph built
        // with the initial ef=512 remains valid when the runtime budget changes.
        efQuery == 0 || stride != graph.size_data_per_element_ ||
        maxLevel < 0 || maxLevel > 64 || entry >= count ||
        quint64(file.bytesAvailable()) < count * stride + count * sizeof(qint32))
        return fail(error, QStringLiteral("ANN shard header is inconsistent with its manifest"));
    if (!readExactly(file, graph.data_level0_memory_, qint64(count * stride)))
        return fail(error, QStringLiteral("ANN shard is truncated"));
    graph.cur_element_count = size_t(count);
    graph.maxlevel_ = maxLevel;
    graph.enterpoint_node_ = entry;
    int actualMaxLevel = 0;
    for (size_t i = 0; i < count; ++i) {
        qint32 level;
        stream >> level;
        if (stream.status() != QDataStream::Ok || level < 0 || level > maxLevel)
            return fail(error, QStringLiteral("ANN shard has invalid graph levels"));
        if (level > 0) {
            const auto bytes = size_t(level) * graph.size_links_per_element_;
            if (quint64(file.bytesAvailable()) < bytes)
                return fail(error, QStringLiteral("ANN upper graph is truncated"));
            auto *links = static_cast<char *>(std::malloc(bytes));
            if (!links)
                throw std::bad_alloc();
            graph.linkLists_[i] = links;
            graph.element_levels_[i] = level;
            if (!readExactly(file, links, qint64(bytes)))
                return fail(error, QStringLiteral("ANN upper graph cannot be read"));
        }
        actualMaxLevel = std::max(actualMaxLevel, int(level));
        const auto label = graph.getExternalLabel(hnswlib::tableint(i));
        if (label == 0 || label > quint64(std::numeric_limits<qint64>::max()) ||
            VectorIndex::shardForId(qint64(label)) != shard ||
            !graph.label_lookup_.emplace(label, hnswlib::tableint(i)).second ||
            graph.isMarkedDeleted(hnswlib::tableint(i)))
            return fail(error, QStringLiteral("ANN shard contains invalid or duplicate labels"));
        double norm = 0;
        for (int coordinate = 0; coordinate < dimensions; ++coordinate) {
            float value;
            std::memcpy(&value, graph.getDataByInternalId(hnswlib::tableint(i)) + coordinate * sizeof(float), sizeof(value));
            if (!std::isfinite(value))
                return fail(error, QStringLiteral("ANN shard contains non-finite vectors"));
            norm += double(value) * value;
        }
        if (std::abs(norm - 1.0) > 0.001)
            return fail(error, QStringLiteral("ANN shard vectors are not normalized"));
    }
    if (!file.atEnd() || actualMaxLevel != maxLevel || graph.element_levels_[entry] != maxLevel)
        return fail(error, QStringLiteral("ANN shard graph has an invalid entry point or trailing bytes"));
    // Validate graph edges before hnswlib traverses them. A cache that fails
    // validation is rebuilt from SQLite instead of risking an out-of-bounds read.
    for (hnswlib::tableint i = 0; i < count; ++i) {
        for (int level = 0; level <= graph.element_levels_[i]; ++level) {
            auto *links = graph.get_linklist_at_level(i, level);
            const auto size = graph.getListCount(links);
            if (size > (level == 0 ? graph.maxM0_ : graph.maxM_))
                return fail(error, QStringLiteral("ANN shard has too many graph edges"));
            const auto *neighbors = reinterpret_cast<const hnswlib::tableint *>(links + 1);
            for (size_t j = 0; j < size; ++j) {
                const auto target = neighbors[j];
                if (target >= count || target == i || graph.element_levels_[target] < level)
                    return fail(error, QStringLiteral("ANN shard contains an invalid graph edge"));
            }
        }
    }
    return true;
}

bool parseUnsigned(const QJsonValue &value, quint64 *output)
{
    if (!value.isString())
        return false;
    bool valid = false;
    *output = value.toString().toULongLong(&valid);
    return valid;
}

class AllowedFilter final : public hnswlib::BaseFilterFunctor {
public:
    AllowedFilter(const QSet<qint64> *allowed, const QSet<qint64> *excluded)
        : allowed_(allowed), excluded_(excluded) {}
    bool operator()(hnswlib::labeltype id) override
    {
        return (!allowed_ || allowed_->contains(qint64(id))) &&
               (!excluded_ || !excluded_->contains(qint64(id)));
    }
private:
    const QSet<qint64> *allowed_;
    const QSet<qint64> *excluded_;
};

// Throwing from hnswlib's filter would bypass releaseVisitedList() and leak its
// traversal buffer. The supported stop-condition API lets cancellation end a
// traversal normally, including when no ID filter is otherwise needed.
class CancellableTopK final : public hnswlib::BaseSearchStopCondition<float> {
public:
    CancellableTopK(size_t k, const std::atomic_bool &cancelled,
                    const std::function<void(const QString &)> &faultHook)
        : k_(k), ef_(std::max(k, size_t(queryEf))), cancelled_(cancelled), faultHook_(faultHook) {}
    void add_point_to_result(hnswlib::labeltype, const void *, float) override { ++count_; }
    void remove_point_from_result(hnswlib::labeltype, const void *, float) override { --count_; }
    bool should_stop_search(float distance, float lowerBound) override
    {
        if (!enteredTraversal_) {
            enteredTraversal_ = true;
            if (faultHook_) {
                try {
                    faultHook_(QStringLiteral("ann-search-traversal-started"));
                } catch (...) {
                    // Save the exception and let hnswlib return its visited list
                    // before reporting an injected callback failure outside it.
                    callbackFailure_ = std::current_exception();
                }
            }
        }
        return callbackFailure_ || cancelled_.load(std::memory_order_relaxed) ||
               (distance > lowerBound && count_ >= ef_);
    }
    bool should_consider_candidate(float distance, float lowerBound) override
    {
        return !callbackFailure_ && !cancelled_.load(std::memory_order_relaxed) &&
               (count_ < ef_ || distance < lowerBound);
    }
    bool should_remove_extra() override { return count_ > ef_; }
    void filter_results(std::vector<std::pair<float, hnswlib::labeltype>> &values) override
    {
        if (callbackFailure_ || cancelled_.load(std::memory_order_relaxed))
            values.clear();
        else if (values.size() > k_)
            values.resize(k_);
    }
    void reportCallbackFailure() const
    {
        if (!callbackFailure_)
            return;
        try {
            std::rethrow_exception(callbackFailure_);
        } catch (const std::exception &exception) {
            throw std::runtime_error(std::string("ANN traversal checkpoint failed: ") + exception.what());
        } catch (...) {
            throw std::runtime_error("ANN traversal checkpoint failed with a non-standard exception");
        }
    }
private:
    size_t k_;
    size_t ef_;
    size_t count_ = 0;
    const std::atomic_bool &cancelled_;
    const std::function<void(const QString &)> &faultHook_;
    bool enteredTraversal_ = false;
    std::exception_ptr callbackFailure_;
};

} // namespace

QByteArray encodeFloat16(const QVector<float> &values)
{
    QByteArray result(values.size() * qsizetype(sizeof(quint16)), Qt::Uninitialized);
    for (qsizetype i = 0; i < values.size(); ++i) {
        if (!std::isfinite(values[i]) || std::abs(values[i]) > 65504.0f)
            throw std::invalid_argument("Cannot store non-finite or overflowing FP16 values");
        const qfloat16 half(values[i]);
        static_assert(sizeof(half) == sizeof(quint16));
        const auto bits = std::bit_cast<quint16>(half);
        qToLittleEndian(bits, result.data() + i * sizeof(bits));
    }
    return result;
}

QVector<float> decodeFloat16(const QByteArray &bytes, QString *error)
{
    setError(error, {});
    if (bytes.size() % qsizetype(sizeof(quint16))) {
        fail(error, QStringLiteral("FP16 blob length must be a multiple of two"));
        return {};
    }
    QVector<float> result;
    result.reserve(bytes.size() / qsizetype(sizeof(quint16)));
    for (qsizetype offset = 0; offset < bytes.size(); offset += sizeof(quint16)) {
        const auto bits = qFromLittleEndian<quint16>(bytes.constData() + offset);
        const float value = float(std::bit_cast<qfloat16>(bits));
        if (!std::isfinite(value)) {
            fail(error, QStringLiteral("FP16 blob contains a non-finite value"));
            return {};
        }
        result.append(value);
    }
    return result;
}

struct VectorIndex::Shard {
    explicit Shard(int dimensions, qsizetype count)
        : space(dimensions), graph(&space, size_t(std::max(qsizetype(1), count)), graphM,
                                  constructionEf, 20261011)
    {
        graph.setEf(queryEf);
    }
    ~Shard()
    {
        if (removeWhenUnused.load() && !path.isEmpty())
            QFile::remove(path);
    }
    hnswlib::InnerProductSpace space;
    Graph graph;
    QString path;
    QString filename;
    QByteArray sha256;
    qint64 bytes = 0;
    mutable std::atomic<bool> removeWhenUnused{false};
};

class VectorIndex::Private {
public:
    Private(QString rootValue, int dimensionsValue, std::function<void(const QString &)> faultHookValue)
        : root(QDir::cleanPath(QFileInfo(rootValue).absoluteFilePath())), dimensions(dimensionsValue),
          faultHook(std::move(faultHookValue))
    {
        if (rootValue.isEmpty())
            throw std::invalid_argument("ANN index root must not be empty");
        if (dimensions <= 0 || dimensions > 8192)
            throw std::invalid_argument("ANN dimensions must be between 1 and 8192");
        auto initial = std::make_shared<Snapshot>();
        initial->dimensions_ = dimensions;
        current = std::move(initial);
    }
    QString root;
    int dimensions;
    std::function<void(const QString &)> faultHook;
    mutable std::mutex publicationMutex;
    std::mutex writerMutex;
    SnapshotPtr current;
    std::vector<std::weak_ptr<const Shard>> liveFiles;

    QJsonObject manifest(const Snapshot &snapshot) const
    {
        QJsonArray shards;
        for (int index = 0; index < shardCount; ++index) {
            const auto &shard = snapshot.shards_[index];
            QJsonObject entry{{QStringLiteral("shard"), index},
                              {QStringLiteral("generation"), QString::number(snapshot.shardGenerations_[index])},
                              {QStringLiteral("count"), QString::number(shard ? shard->graph.cur_element_count.load() : 0)}};
            if (shard) {
                entry.insert(QStringLiteral("file"), shard->filename);
                entry.insert(QStringLiteral("bytes"), QString::number(shard->bytes));
                entry.insert(QStringLiteral("sha256"), QString::fromLatin1(shard->sha256));
            }
            shards.append(entry);
        }
        return {{QStringLiteral("format"), 1},
                {QStringLiteral("hnswlib"), QStringLiteral("0.8.0")},
                {QStringLiteral("dimensions"), dimensions},
                {QStringLiteral("generation"), QString::number(snapshot.generation_)},
                {QStringLiteral("shards"), shards}};
    }

    bool saveManifest(const Snapshot &snapshot, QString *error)
    {
        QSaveFile file(QDir(root).filePath(QString::fromLatin1(manifestName)));
        file.setDirectWriteFallback(false);
        if (!file.open(QIODevice::WriteOnly))
            return fail(error, QStringLiteral("Cannot create ANN manifest: %1").arg(file.errorString()));
        const auto bytes = QJsonDocument(manifest(snapshot)).toJson(QJsonDocument::Compact);
        if (!writeExactly(file, bytes.constData(), bytes.size()) || !file.commit())
            return fail(error, QStringLiteral("Cannot publish ANN manifest: %1").arg(file.errorString()));
        return true;
    }

    void publish(const std::shared_ptr<Snapshot> &replacement)
    {
        std::lock_guard lock(publicationMutex);
        for (int index = 0; index < shardCount; ++index) {
            const auto &old = current->shards_[index];
            const auto &fresh = replacement->shards_[index];
            if (old && (!fresh || old->path != fresh->path))
                old->removeWhenUnused = true;
            if (fresh && old != fresh)
                liveFiles.emplace_back(fresh);
        }
        std::erase_if(liveFiles, [](const auto &file) { return file.expired(); });
        current = replacement;
    }

    void collectOrphans()
    {
        // Only a successful manifest load establishes which crash leftovers are
        // obsolete. Held query snapshots remain protected by the weak registry.
        QSet<QString> retained;
        for (const auto &weak : liveFiles) {
            if (const auto shard = weak.lock())
                retained.insert(shard->path);
        }
        static const QRegularExpression ownedName(QStringLiteral(
            "^qcs-ann-shard-(?:[0-9]|1[0-5])-[0-9]+-"
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.bin$"));
        const QDir directory(root);
        const auto entries = directory.entryInfoList(
            {QStringLiteral("qcs-ann-shard-*.bin")}, QDir::Files | QDir::NoSymLinks);
        for (const auto &entry : entries) {
            if (ownedName.match(entry.fileName()).hasMatch() && !retained.contains(entry.absoluteFilePath()) &&
                !QFile::remove(entry.absoluteFilePath()))
                qCWarning(vectorIndexLog).noquote() << "Cannot remove obsolete ANN shard:" << entry.absoluteFilePath();
        }
    }
};

VectorIndex::VectorIndex(QString root, int dimensions, std::function<void(const QString &)> faultHook)
    : d_(std::make_unique<Private>(std::move(root), dimensions, std::move(faultHook)))
{}

VectorIndex::~VectorIndex() = default;

quint64 VectorIndex::Snapshot::shardGeneration(int shard) const noexcept
{
    return shard >= 0 && shard < shardCount ? shardGenerations_[shard] : 0;
}

int VectorIndex::shardForId(qint64 id) noexcept
{
    return id > 0 ? int(quint64(id) % shardCount) : -1;
}

VectorIndex::SnapshotPtr VectorIndex::snapshot() const
{
    std::lock_guard lock(d_->publicationMutex);
    return d_->current;
}

bool VectorIndex::loadManifest(QString *error)
{
    setError(error, {});
    std::lock_guard writer(d_->writerMutex);
    try {
        const QString path = QDir(d_->root).filePath(QString::fromLatin1(manifestName));
        if (!QFileInfo::exists(path)) {
            // A disappeared manifest must not invalidate existing live queries.
            if (snapshot()->generation() != 0)
                return fail(error, QStringLiteral("ANN manifest disappeared after publication"));
            return true;
        }
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly) || file.size() > maximumManifestBytes)
            return fail(error, QStringLiteral("ANN manifest cannot be read or exceeds its size limit"));
        QJsonParseError parseError;
        const auto document = QJsonDocument::fromJson(file.readAll(), &parseError);
        const auto object = document.object();
        quint64 generation = 0;
        if (parseError.error != QJsonParseError::NoError || !document.isObject() ||
            object.value(QStringLiteral("format")).toInt(-1) != 1 ||
            object.value(QStringLiteral("hnswlib")).toString() != QStringLiteral("0.8.0") ||
            object.value(QStringLiteral("dimensions")).toInt(-1) != d_->dimensions ||
            !parseUnsigned(object.value(QStringLiteral("generation")), &generation))
            return fail(error, QStringLiteral("ANN manifest format, version or dimensions are invalid"));
        const auto entries = object.value(QStringLiteral("shards")).toArray();
        if (entries.size() != shardCount)
            return fail(error, QStringLiteral("ANN manifest must contain exactly 16 shards"));
        const auto previous = snapshot();
        auto replacement = std::make_shared<Snapshot>();
        replacement->dimensions_ = d_->dimensions;
        replacement->generation_ = generation;
        quint64 actualGeneration = 0;
        for (int index = 0; index < shardCount; ++index) {
            const auto entry = entries[index].toObject();
            quint64 shardGeneration, count;
            if (entry.value(QStringLiteral("shard")).toInt(-1) != index ||
                !parseUnsigned(entry.value(QStringLiteral("generation")), &shardGeneration) ||
                !parseUnsigned(entry.value(QStringLiteral("count")), &count) ||
                count > maximumShardElements || shardGeneration > generation)
                return fail(error, QStringLiteral("ANN manifest shard metadata is invalid"));
            replacement->shardGenerations_[index] = shardGeneration;
            actualGeneration = std::max(actualGeneration, shardGeneration);
            if (count == 0) {
                if (entry.contains(QStringLiteral("file")))
                    return fail(error, QStringLiteral("Empty ANN shard must not reference a file"));
                continue;
            }
            quint64 bytes;
            const auto filename = entry.value(QStringLiteral("file")).toString();
            const auto expectedPrefix = QStringLiteral("qcs-ann-shard-%1-%2-").arg(index).arg(shardGeneration);
            const auto sha256 = entry.value(QStringLiteral("sha256")).toString().toLatin1();
            if (!filename.startsWith(expectedPrefix) || !filename.endsWith(QStringLiteral(".bin")) ||
                filename.contains(QLatin1Char('/')) || filename.contains(QLatin1Char('\\')) ||
                filename.contains(QLatin1Char(':')) || filename.contains(QStringLiteral("..")) ||
                !parseUnsigned(entry.value(QStringLiteral("bytes")), &bytes) || bytes > quint64(std::numeric_limits<qint64>::max()) ||
                sha256.size() != 64 || QByteArray::fromHex(sha256).toHex() != sha256)
                return fail(error, QStringLiteral("ANN manifest contains an invalid shard path or checksum"));
            const QString shardPath = QDir(d_->root).filePath(filename);
            const QFileInfo info(shardPath);
            if (!info.isFile() || info.isSymLink() || quint64(info.size()) != bytes)
                return fail(error, QStringLiteral("ANN shard is missing, redirected or has the wrong size"));
            const quint64 minimumGraphBytes = count * (quint64(d_->dimensions) * sizeof(float) +
                (2 * graphM + 1) * sizeof(hnswlib::tableint) + sizeof(hnswlib::labeltype) + sizeof(qint32));
            if (bytes < minimumGraphBytes)
                return fail(error, QStringLiteral("ANN shard count exceeds the available graph data"));
            if (fileHash(shardPath, error) != sha256)
                return fail(error, QStringLiteral("ANN shard SHA-256 verification failed: %1").arg(filename));
            const auto &old = previous->shards_[index];
            if (old && old->path == shardPath && old->sha256 == sha256 &&
                previous->shardGenerations_[index] == shardGeneration && old->graph.cur_element_count == count) {
                replacement->shards_[index] = old;
            } else {
                auto shard = std::make_shared<Shard>(d_->dimensions, qsizetype(count));
                shard->filename = filename;
                shard->path = shardPath;
                shard->sha256 = sha256;
                shard->bytes = qint64(bytes);
                if (!loadGraph(shard->graph, shardPath, d_->dimensions, index, shardGeneration, count, error))
                    return false;
                replacement->shards_[index] = std::move(shard);
            }
            replacement->size_ += qsizetype(count);
        }
        if (actualGeneration != generation)
            return fail(error, QStringLiteral("ANN manifest generation is inconsistent"));
        d_->publish(replacement);
        d_->collectOrphans();
        return true;
    } catch (const std::exception &exception) {
        return fail(error, QStringLiteral("Cannot load ANN index: %1").arg(exceptionText(exception)));
    }
}

bool VectorIndex::rebuildShard(int shardNumber, quint64 generation,
                               const QVector<VectorRecord> &records, QString *error)
{
    setError(error, {});
    std::lock_guard writer(d_->writerMutex);
    try {
        const auto previous = snapshot();
        if (shardNumber < 0 || shardNumber >= shardCount ||
            generation <= previous->shardGeneration(shardNumber) ||
            quint64(records.size()) > maximumShardElements)
            return fail(error, QStringLiteral("ANN shard, generation or record count is invalid"));
        if (!QDir().mkpath(d_->root))
            return fail(error, QStringLiteral("Cannot create ANN index directory"));
        // Keep at least the pending graph's space plus a small filesystem margin.
        // The service additionally applies its configured global disk budget.
        const QStorageInfo storage(d_->root);
        const qint64 estimate = qint64(records.size()) * (d_->dimensions * 4 + 512) + 1024 * 1024;
        if (storage.isValid() && storage.isReady() && storage.bytesAvailable() < estimate)
            return fail(error, QStringLiteral("Insufficient disk space for a new ANN shard"));
        std::shared_ptr<Shard> built;
        if (!records.isEmpty()) {
            built = std::make_shared<Shard>(d_->dimensions, records.size());
            QSet<qint64> labels;
            labels.reserve(records.size());
            // Observe the actual graph construction, excluding serialization
            // and publication. Failed builds deliberately have no completion.
            if (d_->faultHook)
                d_->faultHook(QStringLiteral("ann-build-started"));
            for (const auto &record : records) {
                if (shardForId(record.id) != shardNumber || labels.contains(record.id))
                    return fail(error, QStringLiteral("ANN labels must be positive, unique and belong to their shard"));
                labels.insert(record.id);
                const auto vector = normalized(record.vector, d_->dimensions);
                built->graph.addPoint(vector.constData(), hnswlib::labeltype(record.id));
            }
            if (d_->faultHook)
                d_->faultHook(QStringLiteral("ann-build-completed"));
            built->filename = QStringLiteral("qcs-ann-shard-%1-%2-%3.bin")
                                  .arg(shardNumber).arg(generation).arg(QUuid::createUuid().toString(QUuid::WithoutBraces));
            built->path = QDir(d_->root).filePath(built->filename);
            built->removeWhenUnused = true;
            if (!saveGraph(built->graph, built->path, d_->dimensions, shardNumber, generation, error))
                return false;
            built->bytes = QFileInfo(built->path).size();
            built->sha256 = fileHash(built->path, error);
            if (built->sha256.isEmpty())
                return false;
            if (d_->faultHook)
                d_->faultHook(QStringLiteral("ann-file-saved-before-manifest"));
        }
        auto replacement = std::make_shared<Snapshot>(*previous);
        if (const auto &old = replacement->shards_[shardNumber])
            replacement->size_ -= qsizetype(old->graph.cur_element_count.load());
        replacement->shards_[shardNumber] = built;
        replacement->size_ += records.size();
        replacement->shardGenerations_[shardNumber] = generation;
        replacement->generation_ = std::max(previous->generation_, generation);
        if (!d_->saveManifest(*replacement, error))
            return false;
        if (built)
            built->removeWhenUnused = false;
        d_->publish(replacement);
        return true;
    } catch (const std::exception &exception) {
        return fail(error, QStringLiteral("Cannot rebuild ANN shard: %1").arg(exceptionText(exception)));
    }
}

QVector<VectorNeighbor> VectorIndex::search(const SnapshotPtr &state,
                                           const QVector<float> &query, int k,
                                           const QSet<qint64> *allowedIds, QString *error,
                                           const QSet<qint64> *excludedIds,
                                           const std::atomic_bool *cancelled) const
{
    setError(error, {});
    QVector<VectorNeighbor> result;
    auto wasCancelled = [&] {
        if (cancelled && cancelled->load(std::memory_order_relaxed)) {
            setError(error, QStringLiteral("Search cancelled"));
            return true;
        }
        return false;
    };
    if (wasCancelled())
        return {};
    try {
        if (!state || state->dimensions_ != d_->dimensions || k < 0 || k > 100000)
            throw std::invalid_argument("Invalid ANN snapshot or requested result count");
        const auto unitQuery = normalized(query, d_->dimensions);
        if (k == 0 || state->size_ == 0 || (allowedIds && allowedIds->isEmpty()))
            return result;
        const auto neighborLess = [](const VectorNeighbor &left, const VectorNeighbor &right) {
            return left.distance < right.distance || (left.distance == right.distance && left.id < right.id);
        };
        if (allowedIds && allowedIds->size() <= exactFilterLimit) {
            result.reserve(allowedIds->size());
            qsizetype visited = 0;
            for (const auto id : *allowedIds) {
                if ((visited++ % 128) == 0 && wasCancelled())
                    return {};
                if (excludedIds && excludedIds->contains(id))
                    continue;
                const int shardNumber = shardForId(id);
                if (shardNumber < 0)
                    continue;
                const auto &shard = state->shards_[shardNumber];
                if (!shard)
                    continue;
                const auto found = shard->graph.label_lookup_.find(hnswlib::labeltype(id));
                if (found == shard->graph.label_lookup_.end())
                    continue;
                const float distance = shard->graph.fstdistfunc_(unitQuery.constData(),
                    shard->graph.getDataByInternalId(found->second), shard->graph.dist_func_param_);
                result.append({id, std::clamp(distance, 0.0f, 2.0f)});
            }
        } else {
            const auto checkpoint = [&](const QString &point) {
                if (!d_->faultHook)
                    return;
                try {
                    d_->faultHook(point);
                } catch (const std::exception &) {
                    throw;
                } catch (...) {
                    throw std::runtime_error("ANN search checkpoint failed with a non-standard exception");
                }
            };
            std::array<QVector<hnswlib::tableint>, shardCount> allowedByShard;
            if (allowedIds) {
                qsizetype visited = 0;
                for (const auto id : *allowedIds) {
                    const int shardNumber = shardForId(id);
                    if (shardNumber >= 0 && (!excludedIds || !excludedIds->contains(id))) {
                        const auto &shard = state->shards_[shardNumber];
                        if (shard) {
                            const auto found = shard->graph.label_lookup_.find(hnswlib::labeltype(id));
                            if (found != shard->graph.label_lookup_.end())
                                allowedByShard[shardNumber].append(found->second);
                        }
                    }
                    if ((++visited % 128) == 0) {
                        checkpoint(QStringLiteral("ann-search-filter-progress"));
                        if (wasCancelled())
                            return {};
                    }
                }
            }
            for (int shardNumber = 0; shardNumber < shardCount; ++shardNumber) {
                if (wasCancelled())
                    return {};
                const auto &shard = state->shards_[shardNumber];
                if (!shard || (allowedIds && allowedByShard[shardNumber].isEmpty()))
                    continue;
                const quint64 nodes = shard->graph.cur_element_count.load();
                const auto &allowed = allowedByShard[shardNumber];
                const quint64 accepted = quint64(allowed.size());
                const quint64 breadth = std::min(nodes, std::max(quint64(k), quint64(queryEf)));
                // A <= N <= maximumShardElements (validated on build/load),
                // so both products fit in quint64. Sparse filters can cost
                // more graph visits than scoring their accepted vectors.
                if (allowedIds && accepted * accepted <= breadth * nodes) {
                    checkpoint(QStringLiteral("ann-search-exact-started"));
                    if (wasCancelled())
                        return {};
                    QVector<VectorNeighbor> exact;
                    exact.reserve(allowed.size());
                    qsizetype scored = 0;
                    for (const auto internalId : allowed) {
                        const float distance = shard->graph.fstdistfunc_(unitQuery.constData(),
                            shard->graph.getDataByInternalId(internalId), shard->graph.dist_func_param_);
                        exact.append({qint64(shard->graph.getExternalLabel(internalId)),
                                      std::clamp(distance, 0.0f, 2.0f)});
                        if ((++scored % 128) == 0) {
                            checkpoint(QStringLiteral("ann-search-exact-progress"));
                            if (wasCancelled())
                                return {};
                        }
                    }
                    if (wasCancelled())
                        return {};
                    if (exact.size() > k) {
                        // Any shard can supply all global k neighbors. Use
                        // the final distance/ID order when resolving ties.
                        std::nth_element(exact.begin(), exact.begin() + k, exact.end(), neighborLess);
                        exact.resize(k);
                    }
                    if (wasCancelled())
                        return {};
                    result += exact;
                    continue;
                }
                std::unique_ptr<AllowedFilter> filter;
                if (allowedIds || (excludedIds && !excludedIds->isEmpty()))
                    filter = std::make_unique<AllowedFilter>(allowedIds, excludedIds);
                const auto count = std::min(size_t(k), shard->graph.cur_element_count.load());
                if (cancelled) {
                    CancellableTopK condition(count, *cancelled, d_->faultHook);
                    const auto neighbors = shard->graph.searchStopConditionClosest(unitQuery.constData(), condition, filter.get());
                    condition.reportCallbackFailure();
                    if (wasCancelled())
                        return {};
                    // hnswlib 0.8.0's stop-condition API returns internal graph
                    // positions, unlike searchKnn(), which returns labels.
                    for (const auto &[distance, internalId] : neighbors)
                        result.append({qint64(shard->graph.getExternalLabel(hnswlib::tableint(internalId))),
                                       std::clamp(distance, 0.0f, 2.0f)});
                } else {
                    auto neighbors = shard->graph.searchKnn(unitQuery.constData(), count, filter.get());
                    while (!neighbors.empty()) {
                        const auto [distance, id] = neighbors.top();
                        neighbors.pop();
                        result.append({qint64(id), std::clamp(distance, 0.0f, 2.0f)});
                    }
                }
                if (wasCancelled())
                    return {};
            }
        }
        if (wasCancelled())
            return {};
        std::sort(result.begin(), result.end(), neighborLess);
        if (result.size() > k)
            result.resize(k);
        if (wasCancelled())
            return {};
        return result;
    } catch (const std::exception &exception) {
        fail(error, QStringLiteral("ANN search failed: %1").arg(exceptionText(exception)));
        return {};
    }
}

} // namespace qcopilots_search
