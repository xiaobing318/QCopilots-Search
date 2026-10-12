// Synthetic capacity and ANN accuracy benchmark. This is not a semantic-quality
// benchmark: every vector and logical filename is generated from the stated seed.
#include "core/runtime_paths.h"
#include "core/types.h"
#include "storage/index_repository.h"
#include "storage/vector_index.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QDirIterator>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcess>
#include <QSaveFile>
#include <QScopeGuard>
#include <QSet>
#include <QStorageInfo>
#include <QTemporaryDir>
#include <QTextStream>
#include <QUuid>

#include <sqlite3.h>

#include <algorithm>
#include <atomic>
#include <array>
#include <bit>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <memory>
#include <mutex>
#include <queue>
#include <random>
#include <stdexcept>
#include <thread>
#include <vector>

using namespace qcopilots_search;

namespace {
constexpr qint64 gib = 1024LL * 1024 * 1024;
constexpr int topK = 20;
constexpr int clusterCount = 64;
constexpr qint64 pilotSegments = 100000;

void require(bool condition, const QString &message)
{
    if (!condition)
        throw std::runtime_error(message.toUtf8().constData());
}

using BenchmarkDatabase = std::unique_ptr<sqlite3, decltype(&sqlite3_close)>;

BenchmarkDatabase openBenchmarkDatabase(const QString &file, int flags, const QString &context)
{
    QString nativeFile = file;
    const char *vfs = nullptr;
#ifdef Q_OS_WIN
    // Match the repository connection: the extended name and longpath VFS
    // are both required. Keep the original readonly/readwrite flags intact.
    nativeFile = QDir::toNativeSeparators(QFileInfo(file).absoluteFilePath());
    if (!nativeFile.startsWith(QStringLiteral("\\\\?\\"))) {
        nativeFile = nativeFile.startsWith(QStringLiteral("\\\\"))
            ? QStringLiteral("\\\\?\\UNC\\") + nativeFile.mid(2)
            : QStringLiteral("\\\\?\\") + nativeFile;
    }
    vfs = "win32-longpath";
    require(sqlite3_vfs_find(vfs) != nullptr,
            context + QStringLiteral(": required SQLite win32-longpath VFS is unavailable"));
#endif
    sqlite3 *rawDatabase = nullptr;
    const auto path = nativeFile.toUtf8();
    const int result = sqlite3_open_v2(path.constData(), &rawDatabase, flags, vfs);
    BenchmarkDatabase database(rawDatabase, sqlite3_close);
    require(result == SQLITE_OK, context + QStringLiteral(": ")
            + QString::fromUtf8(rawDatabase ? sqlite3_errmsg(rawDatabase) : sqlite3_errstr(result)));
    return database;
}

qint64 directoryBytes(const QString &root)
{
    qint64 total = 0;
    QDirIterator it(root, QDir::Files | QDir::Hidden | QDir::NoSymLinks,
                    QDirIterator::Subdirectories);
    while (it.hasNext()) {
        it.next();
        total += it.fileInfo().size();
    }
    return total;
}

struct Budget {
    QString root;
    qint64 reserve = 3 * gib;
    qint64 limit = 7 * gib;
    qint64 sampledPeak = 0;
    qint64 minimumFree = std::numeric_limits<qint64>::max();

    qint64 check(qint64 anticipatedGrowth = 0)
    {
        QStorageInfo volume(root);
        volume.refresh();
        require(volume.isValid() && volume.isReady(),
                QStringLiteral("Cannot inspect free space for %1").arg(root));
        const qint64 free = volume.bytesAvailable();
        minimumFree = std::min(minimumFree, free);
        const qint64 used = directoryBytes(root);
        sampledPeak = std::max(sampledPeak, used);
        require(free >= reserve + anticipatedGrowth,
                QStringLiteral("Free-space guard: available %1 bytes, required %2 bytes")
                    .arg(free).arg(reserve + anticipatedGrowth));
        require(used + anticipatedGrowth <= limit,
                QStringLiteral("Run-directory guard: current %1 bytes plus estimated %2 bytes exceeds %3 bytes")
                    .arg(used).arg(anticipatedGrowth).arg(limit));
        return used;
    }
};

// SplitMix64 and integer-to-float conversion make the generated input identical
// across standard-library implementations, unlike normal_distribution.
class Random {
public:
    explicit Random(quint64 seed) : state_(seed) {}
    quint64 next()
    {
        quint64 z = (state_ += 0x9e3779b97f4a7c15ULL);
        z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
        z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
        return z ^ (z >> 31);
    }
    float signedUnit()
    {
        return float(next() >> 40) * (2.0f / 16777216.0f) - 1.0f;
    }
private:
    quint64 state_;
};

void normalize(QVector<float> &values)
{
    double norm = 0;
    for (float value : values)
        norm += double(value) * value;
    require(norm > 0 && std::isfinite(norm), QStringLiteral("Invalid generated vector"));
    const float scale = float(1.0 / std::sqrt(norm));
    for (float &value : values)
        value *= scale;
}

class Generator {
public:
    explicit Generator(quint64 seed) : seed_(seed)
    {
        Random random(seed ^ 0xd657bf713c20ad0eULL);
        for (auto &center : centers_) {
            center.resize(embeddingDimensions);
            for (float &value : center)
                value = random.signedUnit();
            normalize(center);
        }
    }

    QVector<float> vector(qint64 file, int segment) const
    {
        Random random(seed_ ^ (quint64(file + 1) * 0x9e3779b97f4a7c15ULL)
                      ^ (quint64(segment + 1) * 0x94d049bb133111ebULL));
        QVector<float> result(embeddingDimensions);
        const bool uniform = file % 10 == 0;
        const auto &center = centers_[size_t(file % clusterCount)];
        for (int i = 0; i < embeddingDimensions; ++i)
            result[i] = uniform ? random.signedUnit() : center[i] + 0.008f * random.signedUnit();
        normalize(result);
        return result;
    }

private:
    quint64 seed_;
    std::array<QVector<float>, clusterCount> centers_;
};

QString logicalPath(const QString &root, qint64 file)
{
    const qint64 bucket = file % 1000;
    return QDir(root).filePath(QStringLiteral("logical-corpus/decile-%1/percent-%2/permille-%3/file-%4.txt")
                                   .arg(bucket / 100).arg(bucket / 10).arg(bucket).arg(file));
}

struct Query {
    QVector<float> values;
    QString distribution;
    qint64 sourceFile = 0;
};

std::vector<Query> makeQueries(const Generator &generator, qint64 files, int count, int segments, quint64 seed)
{
    Random random(seed ^ 0x819d4a231ULL);
    std::vector<Query> queries;
    queries.reserve(size_t(count));
    for (int q = 0; q < count; ++q) {
        qint64 file = qint64(random.next() % quint64(files));
        // Keep approximately 10% uniform queries, even for a small smoke run.
        if (q % 10 == 0)
            file -= file % 10;
        else if (file % 10 == 0 && files > 1)
            file = (file + 1) % files;
        auto values = generator.vector(file, int(random.next() % quint64(segments)));
        for (float &value : values)
            value += 0.0001f * random.signedUnit();
        normalize(values);
        queries.push_back({std::move(values), file % 10 == 0 ? QStringLiteral("uniform")
                                                          : QStringLiteral("clustered"), file});
    }
    return queries;
}

// Independent binary16 decoding for the exact oracle. It deliberately does not
// call the storage codec or ANN search implementation under test.
float fromHalf(quint16 bits)
{
    const unsigned sign = unsigned(bits & 0x8000U) << 16;
    const unsigned exponent = (bits >> 10) & 0x1fU;
    unsigned fraction = bits & 0x3ffU;
    quint32 result = 0;
    if (exponent == 0) {
        if (!fraction)
            result = sign;
        else {
            int shift = 0;
            while (!(fraction & 0x400U)) {
                fraction <<= 1;
                ++shift;
            }
            result = sign | (quint32(127 - 14 - shift) << 23) | ((fraction & 0x3ffU) << 13);
        }
    } else if (exponent == 31) {
        result = sign | 0x7f800000U | (fraction << 13);
    } else {
        result = sign | ((exponent + 112U) << 23) | (fraction << 13);
    }
    return std::bit_cast<float>(result);
}

struct Candidate {
    float score;
    qint64 file;
    QString path;
};
struct WorstFirst {
    bool operator()(const Candidate &a, const Candidate &b) const
    {
        return a.score > b.score || (a.score == b.score && a.file < b.file);
    }
};
using Heap = std::priority_queue<Candidate, std::vector<Candidate>, WorstFirst>;

std::vector<QSet<QString>> exactOracle(const QString &databasePath, const std::vector<Query> &queries,
                                     qint64 expectedRows, QJsonObject &report, const QString &root = {})
{
    auto database = openBenchmarkDatabase(databasePath, SQLITE_OPEN_READONLY,
                                          QStringLiteral("Exact oracle cannot open SQLite database"));
    sqlite3_busy_timeout(database.get(), 30000);
    // file_id is indexed by the repository. ORDER BY groups one file's vectors,
    // so the oracle stores only each query's top 20 files, not a million scores.
    const char *sql = "SELECT f.id, f.path, s.embedding FROM files f "
                      "JOIN segments s ON s.file_id=f.id "
                      "WHERE length(s.embedding)>0 AND (?1='' OR substr(f.path,1,length(?1))=?1) ORDER BY f.id,s.id";
    sqlite3_stmt *rawStatement = nullptr;
    require(sqlite3_prepare_v2(database.get(), sql, -1, &rawStatement, nullptr) == SQLITE_OK,
            QStringLiteral("Exact oracle query failed: %1").arg(QString::fromUtf8(sqlite3_errmsg(database.get()))));
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(rawStatement, sqlite3_finalize);
    const QByteArray prefix = root.isEmpty() ? QByteArray{} : (QDir::fromNativeSeparators(root) + '/').toUtf8();
    require(sqlite3_bind_text(statement.get(), 1, prefix.constData(), int(prefix.size()), SQLITE_TRANSIENT) == SQLITE_OK,
            QStringLiteral("Cannot bind oracle filter"));
    std::vector<Heap> heaps(queries.size());
    std::vector<float> best(queries.size(), -std::numeric_limits<float>::infinity());
    qint64 currentFile = -1;
    QString currentPath;
    auto finishFile = [&] {
        if (currentFile < 0)
            return;
        for (size_t q = 0; q < queries.size(); ++q) {
            auto &heap = heaps[q];
            if (heap.size() < topK || best[q] > heap.top().score) {
                heap.push({best[q], currentFile, currentPath});
                if (heap.size() > topK)
                    heap.pop();
            }
            best[q] = -std::numeric_limits<float>::infinity();
        }
    };
    QElapsedTimer timer;
    timer.start();
    qint64 rows = 0;
    int step = SQLITE_OK;
    std::array<float, embeddingDimensions> values{};
    while ((step = sqlite3_step(statement.get())) == SQLITE_ROW) {
        const qint64 file = sqlite3_column_int64(statement.get(), 0);
        if (file != currentFile) {
            finishFile();
            currentFile = file;
            currentPath = QString::fromUtf8(reinterpret_cast<const char *>(sqlite3_column_text(statement.get(), 1)));
        }
        require(sqlite3_column_bytes(statement.get(), 2) == embeddingDimensions * 2,
                QStringLiteral("Exact oracle found a vector with unexpected dimensions"));
        const auto *bytes = static_cast<const unsigned char *>(sqlite3_column_blob(statement.get(), 2));
        double squaredNorm = 0;
        for (int i = 0; i < embeddingDimensions; ++i) {
            values[size_t(i)] = fromHalf(quint16(bytes[2 * i]) | quint16(bytes[2 * i + 1] << 8));
            require(std::isfinite(values[size_t(i)]), QStringLiteral("Exact oracle found a nonfinite vector"));
            squaredNorm += double(values[size_t(i)]) * values[size_t(i)];
        }
        require(squaredNorm > 0, QStringLiteral("Exact oracle found a zero vector"));
        const float scale = float(1.0 / std::sqrt(squaredNorm));
        for (float &value : values)
            value *= scale;
        for (size_t q = 0; q < queries.size(); ++q) {
            const float *query = queries[q].values.constData();
            float sum = 0;
            for (int i = 0; i < embeddingDimensions; ++i)
                sum += query[i] * values[size_t(i)];
            best[q] = std::max(best[q], sum);
        }
        ++rows;
        if (rows % 100000 == 0)
            QTextStream(stdout) << "oracle " << rows << '/' << expectedRows << Qt::endl;
    }
    require(step == SQLITE_DONE, QStringLiteral("Exact oracle SQLite read failed"));
    finishFile();
    require(expectedRows < 0 || rows == expectedRows, QStringLiteral("Exact oracle row count mismatch: %1 != %2").arg(rows).arg(expectedRows));
    report.insert(QStringLiteral("oracleRows"), rows);
    report.insert(QStringLiteral("oracleMilliseconds"), timer.elapsed());
    report.insert(QStringLiteral("oracleMethod"), QStringLiteral("SQLite read-only stream, independent FP16 decode, cosine, best segment per file, exact top-20 heaps"));
    std::vector<QSet<QString>> answers(queries.size());
    for (size_t q = 0; q < queries.size(); ++q) {
        while (!heaps[q].empty()) {
            answers[q].insert(heaps[q].top().path);
            heaps[q].pop();
        }
    }
    return answers;
}

double percentile(std::vector<double> values, double quantile)
{
    if (values.empty())
        return 0;
    std::sort(values.begin(), values.end());
    return values[std::min(values.size() - 1, size_t(std::ceil(quantile * double(values.size())) - 1))];
}

SearchOptions benchmarkSearchOptions(const RuntimePaths &paths, bool enforceFolderRules)
{
    SearchOptions options;
    options.enforceFolderRules = enforceFolderRules;
    options.cancelled = std::make_shared<std::atomic_bool>(false);
    options.allowedRoots = QStringList{paths.profileDir};
    return options;
}

// The hooks delimit real, nonempty graph construction inside VectorIndex.
// Thread launch and the later graph-file publication are outside this interval.
class BuildObserver {
public:
    struct Interval { qint64 start = -1; qint64 end = -1; };
    void begin()
    {
        std::lock_guard lock(mutex_);
        intervals_.clear();
        invalidEvents_ = 0;
        started_.store(false, std::memory_order_relaxed);
        origin_ = std::chrono::steady_clock::now();
        enabled_.store(true, std::memory_order_release);
    }
    qint64 now() const
    {
        return std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - origin_).count();
    }
    bool hasStarted() const { return started_.load(std::memory_order_acquire); }
    void observe(const QString &point)
    {
        if (!enabled_.load(std::memory_order_acquire))
            return;
        if (point != QStringLiteral("ann-build-started") && point != QStringLiteral("ann-build-completed"))
            return;
        std::lock_guard lock(mutex_);
        const qint64 timestamp = now();
        if (point == QStringLiteral("ann-build-started")) {
            if (!intervals_.empty() && intervals_.back().end < 0)
                ++invalidEvents_;
            intervals_.push_back({timestamp, -1});
            started_.store(true, std::memory_order_release);
        } else if (intervals_.empty() || intervals_.back().end >= 0) {
            ++invalidEvents_;
        } else {
            intervals_.back().end = timestamp;
        }
    }
    std::vector<Interval> finish()
    {
        enabled_.store(false, std::memory_order_release);
        std::lock_guard lock(mutex_);
        require(invalidEvents_ == 0, QStringLiteral("Invalid ANN build observer sequence"));
        return intervals_;
    }
private:
    std::chrono::steady_clock::time_point origin_;
    std::atomic<bool> enabled_{false};
    std::atomic<bool> started_{false};
    std::mutex mutex_;
    std::vector<Interval> intervals_;
    int invalidEvents_ = 0;
};

bool exerciseFilters(IndexRepository &repository, const RuntimePaths &paths,
                     const std::vector<Query> &queries, qint64 files, bool enforceFolderRules,
                     QJsonObject &report)
{
    const QString corpus = QDir(paths.profileDir).filePath(QStringLiteral("logical-corpus"));
    const std::array<std::pair<QString, double>, 3> filters{{
        {QDir(corpus).filePath(QStringLiteral("decile-0")), 10.0},
        {QDir(corpus).filePath(QStringLiteral("decile-0/percent-0")), 1.0},
        {QDir(corpus).filePath(QStringLiteral("decile-0/percent-0/permille-0")), .1}}};
    QJsonArray results;
    bool passed = true;
    for (const auto &[root, percentage] : filters) {
        QJsonObject item;
        const auto oracle = exactOracle(repository.databasePath(), queries, -1, item, root);
        double recallSum = 0;
        int checked = 0;
        std::vector<double> times;
        for (size_t q = 0; q < queries.size(); ++q) {
            if (oracle[q].isEmpty())
                continue;
            auto options = benchmarkSearchOptions(paths, enforceFolderRules);
            options.root = root;
            options.queryEmbedding = queries[q].values;
            options.limit = topK;
            QString error;
            QElapsedTimer timer;
            timer.start();
            const auto hits = repository.search(options, &error);
            times.push_back(double(timer.nsecsElapsed()) / 1000000.0);
            require(error.isEmpty(), error);
            QSet<QString> unique;
            int matched = 0;
            for (const auto &hit : hits) {
                require(hit.path.startsWith(root + '/'), QStringLiteral("Search leaked a result outside the filter"));
                require(!unique.contains(hit.path), QStringLiteral("Filtered search returned duplicate files"));
                unique.insert(hit.path);
                matched += oracle[q].contains(hit.path) ? 1 : 0;
            }
            recallSum += double(matched) / double(oracle[q].size());
            ++checked;
        }
        require(checked > 0, QStringLiteral("Filtered oracle contained no searchable files"));
        const double recall = recallSum / checked;
        const double p95 = percentile(times, .95);
        item.insert(QStringLiteral("nominalPercent"), percentage);
        item.insert(QStringLiteral("corpusFiles"), files);
        item.insert(QStringLiteral("checkedQueries"), checked);
        item.insert(QStringLiteral("recallAt20"), recall);
        item.insert(QStringLiteral("p95Milliseconds"), p95);
        item.insert(QStringLiteral("passed"), recall >= .95 && p95 <= 500);
        passed = passed && recall >= .95 && p95 <= 500;
        results.append(item);
    }
    report.insert(QStringLiteral("filters"), results);
    report.insert(QStringLiteral("unfilteredHundredPercentCoveredByBaseline"), true);
    report.insert(QStringLiteral("filtersPassed"), passed);
    return passed;
}

bool exerciseMutationsAndRebuild(IndexRepository &repository, const RuntimePaths &paths,
                                const Generator &generator, const std::vector<Query> &queries,
                                qint64 files, int segmentsPerFile, int timings,
                                Budget &budget, bool enforceFolderRules, BuildObserver &observer,
                                QJsonObject &report)
{
    const qint64 affectedFiles = std::min(files, std::max(qint64(1), qint64(10000 / segmentsPerFile)));
    const qint64 affectedVectors = affectedFiles * segmentsPerFile;
    QString error;
    auto makeFile = [&](qint64 number, qint64 vectorNumber, const QString &version) {
        IndexedFile file;
        file.path = logicalPath(paths.profileDir, number);
        file.version = version;
        for (int s = 0; s < segmentsPerFile; ++s) {
            Segment segment;
            segment.path = file.path;
            segment.kind = QStringLiteral("text");
            segment.modality = QStringLiteral("text");
            segment.title = QStringLiteral("Capacity mutation %1").arg(number);
            QString uniqueToken = version;
            uniqueToken.remove('-');
            segment.text = uniqueToken + QStringLiteral("token mutation fixture");
            segment.embedding = generator.vector(vectorNumber, s);
            file.segments.push_back(std::move(segment));
        }
        return file;
    };
    QJsonObject mutationReport{{QStringLiteral("filesPerPhase"), affectedFiles},
                               {QStringLiteral("vectorsPerPhase"), affectedVectors},
                               {QStringLiteral("queryScope"), QStringLiteral("filtered-exact-file-plus-global-production-search-sample")},
                               {QStringLiteral("passed"), false}};
    QJsonArray phaseReports;
    using RetiredIds = std::vector<QSet<qint64>>;
    auto captureIds = [&](qint64 firstFile) {
        RetiredIds result(static_cast<size_t>(affectedFiles));
        for (qint64 i = 0; i < affectedFiles; ++i) {
            const auto segments = repository.fileSegments(logicalPath(paths.profileDir, firstFile + i), false, &error);
            require(error.isEmpty() && segments.size() == segmentsPerFile,
                    QStringLiteral("Cannot capture the previous mutation segments"));
            for (const auto &segment : segments)
                result[size_t(i)].insert(segment.id);
        }
        return result;
    };
    auto validatePhase = [&](const QString &phase, qint64 firstFile, const RetiredIds &retired) {
        const bool deleted = phase == QStringLiteral("delete");
        const QString version = phase == QStringLiteral("update") ? QStringLiteral("mutation-updated")
                                                                  : QStringLiteral("mutation-added");
        QString token = version;
        token.remove('-');
        token += QStringLiteral("token");
        qint64 filesChecked = 0, keywordQueries = 0, vectorQueries = 0;
        qint64 staleHits = 0, retiredChecked = 0, retiredRemaining = 0;
        auto database = openBenchmarkDatabase(repository.databasePath(), SQLITE_OPEN_READONLY,
                                              QStringLiteral("Cannot inspect retired mutation segments"));
        sqlite3_busy_timeout(database.get(), 30000);
        sqlite3_stmt *rawStatement = nullptr;
        require(sqlite3_prepare_v2(database.get(), "SELECT count(*) FROM segments WHERE id=?", -1,
                                   &rawStatement, nullptr) == SQLITE_OK,
                QStringLiteral("Cannot prepare retired-segment verification"));
        std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> retiredStatement(rawStatement, sqlite3_finalize);
        for (qint64 i = 0; i < affectedFiles; ++i) {
            const QString path = logicalPath(paths.profileDir, firstFile + i);
            const auto current = repository.fileSegments(path, false, &error);
            require(error.isEmpty() && current.size() == (deleted ? 0 : segmentsPerFile),
                    QStringLiteral("Mutation segment count mismatch"));
            QSet<qint64> currentIds;
            for (const auto &segment : current)
                currentIds.insert(segment.id);
            qint64 previousMaximum = 0;
            for (const qint64 id : retired[size_t(i)]) {
                ++retiredChecked;
                require(sqlite3_bind_int64(retiredStatement.get(), 1, id) == SQLITE_OK &&
                            sqlite3_step(retiredStatement.get()) == SQLITE_ROW,
                        QStringLiteral("Cannot observe a retired segment's global presence"));
                retiredRemaining += sqlite3_column_int64(retiredStatement.get(), 0);
                require(sqlite3_reset(retiredStatement.get()) == SQLITE_OK,
                        QStringLiteral("Cannot reset retired-segment verification"));
                require(!currentIds.contains(id), QStringLiteral("Retired mutation segment remained in its original file"));
                previousMaximum = std::max(previousMaximum, id);
            }
            require(retiredRemaining == 0, QStringLiteral("A retired mutation segment is still present"));
            const QString storedVersion = repository.fileVersion(path, false, &error);
            require(error.isEmpty() && storedVersion == (deleted ? QString{} : version),
                    QStringLiteral("Committed mutation version mismatch"));
            auto checkHits = [&](const QList<SearchHit> &hits, bool semantic) {
                for (const auto &hit : hits) {
                    if (deleted || hit.path != path || hit.sourceVersion != version ||
                        !currentIds.contains(hit.segmentId) || hit.segmentId <= previousMaximum ||
                        hit.text != token + QStringLiteral(" mutation fixture"))
                        ++staleHits;
                }
                require(staleHits == 0, QStringLiteral("Mutation search returned stale content or source version"));
                require(hits.size() == (deleted ? 0 : 1), QStringLiteral("Mutation search result count mismatch"));
                if (semantic && !deleted)
                    require(hits.first().segmentId == current.first().id,
                            QStringLiteral("The replacement's first vector was not recalled"));
            };
            auto keyword = benchmarkSearchOptions(paths, enforceFolderRules);
            keyword.root = path;
            keyword.query = token;
            keyword.limit = topK;
            const auto keywordHits = repository.search(keyword, &error);
            ++keywordQueries;
            require(error.isEmpty(), error);
            checkHits(keywordHits, false);
            auto semantic = benchmarkSearchOptions(paths, enforceFolderRules);
            semantic.root = path;
            semantic.queryEmbedding = generator.vector(files * 2 + i, 0);
            semantic.limit = topK;
            const auto vectorHits = repository.search(semantic, &error);
            ++vectorQueries;
            require(error.isEmpty(), error);
            checkHits(vectorHits, true);
            ++filesChecked;
        }
        // Every changed file is exercised through the production search API.
        // A small global production-search sample additionally checks returned identities
        // still belong to the current authoritative segments and source versions.
        const auto globalStatus = repository.status(&error);
        require(error.isEmpty(), error);
        const qint64 globalVectors = globalStatus.value(QStringLiteral("vectors")).toInteger();
        require(globalVectors == files * segmentsPerFile + (deleted ? 0 : affectedVectors),
                QStringLiteral("Unexpected vector count during mutation search verification"));
        // IndexRepository::search selects exact-subset for <= 20,000 eligible
        // vectors. Small native runs must not label that production path ANN.
        const QString globalBackend = globalVectors > 20000 ? QStringLiteral("ann") : QStringLiteral("exact-subset");
        const int globalQueries = int(std::min(qint64(16), affectedFiles));
        qint64 globalCheckedHits = 0, globalStaleHits = 0;
        for (int sample = 0; sample < globalQueries; ++sample) {
            const qint64 selected = qint64(sample) * affectedFiles / globalQueries;
            auto options = benchmarkSearchOptions(paths, enforceFolderRules);
            options.queryEmbedding = generator.vector(files * 2 + selected, 0);
            options.limit = topK;
            const auto hits = repository.search(options, &error);
            require(error.isEmpty() && !hits.isEmpty(), QStringLiteral("Global mutation search failed: %1").arg(error));
            for (const auto &hit : hits) {
                const QString currentVersion = repository.fileVersion(hit.path, false, &error);
                require(error.isEmpty(), error);
                const auto current = repository.fileSegments(hit.path, false, &error);
                require(error.isEmpty(), error);
                const bool currentSegment = std::any_of(current.cbegin(), current.cend(), [&](const Segment &segment) {
                    return segment.id == hit.segmentId && segment.text == hit.text;
                });
                globalStaleHits += currentVersion.isEmpty() || hit.sourceVersion != currentVersion || !currentSegment ? 1 : 0;
                ++globalCheckedHits;
            }
        }
        require(globalStaleHits == 0, QStringLiteral("Global search returned a retired mutation segment or source version"));
        phaseReports.append(QJsonObject{
            {QStringLiteral("phase"), phase}, {QStringLiteral("passed"), true},
            {QStringLiteral("filesChecked"), filesChecked},
            {QStringLiteral("keywordQueries"), keywordQueries}, {QStringLiteral("vectorQueries"), vectorQueries},
            {QStringLiteral("staleHitCount"), staleHits},
            {QStringLiteral("retiredSegmentIdsChecked"), retiredChecked},
            {QStringLiteral("retiredSegmentIdsRemaining"), retiredRemaining},
            {QStringLiteral("globalSearchQueries"), globalQueries}, {QStringLiteral("globalSearchCheckedHits"), globalCheckedHits},
            {QStringLiteral("globalSearchStaleHitCount"), globalStaleHits},
            {QStringLiteral("globalSearchVectorCount"), globalVectors}, {QStringLiteral("globalSearchBackend"), globalBackend}});
        mutationReport.insert(QStringLiteral("phases"), phaseReports);
        report.insert(QStringLiteral("mutations"), mutationReport);
    };
    auto replacePhase = [&](bool additions) {
        QElapsedTimer timer;
        timer.start();
        for (qint64 start = 0; start < affectedFiles; ) {
            const qint64 count = std::min(affectedFiles - start, qint64(std::max(1, 1024 / segmentsPerFile)));
            budget.check(count * segmentsPerFile * 8192);
            QVector<IndexedFile> batch;
            for (qint64 i = start; i < start + count; ++i)
                batch.push_back(makeFile(additions ? files + i : i, files * 2 + i,
                                        additions ? QStringLiteral("mutation-added") : QStringLiteral("mutation-updated")));
            require(repository.replaceFiles(batch, &error, true), error);
            start += count;
        }
        require(repository.flush(&error), error);
        require(repository.checkIntegrity(&error), error);
        budget.check();
        return timer.elapsed();
    };
    mutationReport.insert(QStringLiteral("addMilliseconds"), replacePhase(true));
    validatePhase(QStringLiteral("add"), files, RetiredIds(size_t(affectedFiles)));
    const auto replacedIds = captureIds(0);
    mutationReport.insert(QStringLiteral("updateMilliseconds"), replacePhase(false));
    validatePhase(QStringLiteral("update"), 0, replacedIds);
    const auto removedIds = captureIds(files);
    QElapsedTimer deleteTimer;
    deleteTimer.start();
    for (qint64 i = 0; i < affectedFiles; ++i) {
        require(repository.removeFile(logicalPath(paths.profileDir, files + i), &error), error);
        if (i % 100 == 0)
            budget.check();
    }
    require(repository.flush(&error), error);
    validatePhase(QStringLiteral("delete"), files, removedIds);
    auto removed = benchmarkSearchOptions(paths, enforceFolderRules);
    removed.query = QStringLiteral("mutationaddedtoken");
    require(repository.search(removed, &error).isEmpty() && error.isEmpty(), QStringLiteral("Deleted content remained searchable"));
    const auto status = repository.status(&error);
    require(error.isEmpty(), error);
    require(status.value(QStringLiteral("files")).toInteger() == files
                && status.value(QStringLiteral("vectors")).toInteger() == files * segmentsPerFile,
            QStringLiteral("Mutation phases changed corpus counts unexpectedly"));
    mutationReport.insert(QStringLiteral("deleteMilliseconds"), deleteTimer.elapsed());
    mutationReport.insert(QStringLiteral("passed"), true);
    report.insert(QStringLiteral("mutations"), mutationReport);

    // Add one replacement without deferred indexing so queries remain available.
    // flushOneShard rebuilds exactly one pending graph, using immutable snapshots.
    require(repository.replaceFiles({makeFile(0, files * 3, QStringLiteral("background-rebuild"))}, &error), error);
    std::atomic<bool> finished{false};
    bool rebuildSucceeded = false;
    QString rebuildError;
    QElapsedTimer rebuildTimer;
    rebuildTimer.start();
    observer.begin();
    std::thread rebuilding([&] {
        rebuildSucceeded = repository.flushOneShard(&rebuildError);
        finished.store(true, std::memory_order_release);
    });
    const auto joinRebuilding = qScopeGuard([&] {
        if (rebuilding.joinable())
            rebuilding.join();
    });
    while (!observer.hasStarted() && !finished.load(std::memory_order_acquire))
        std::this_thread::yield();
    std::vector<BuildObserver::Interval> queryIntervals;
    for (int i = 0; i < timings && !finished.load(std::memory_order_acquire); ++i) {
        auto options = benchmarkSearchOptions(paths, enforceFolderRules);
        options.queryEmbedding = queries[size_t(i) % queries.size()].values;
        options.limit = topK;
        const qint64 start = observer.now();
        const auto hits = repository.search(options, &error);
        queryIntervals.push_back({start, observer.now()});
        require(error.isEmpty() && !hits.isEmpty(), error.isEmpty() ? QStringLiteral("Search failed during a shard rebuild") : error);
        for (const auto &hit : hits)
            require(!hit.text.contains(QStringLiteral("mutationaddedtoken")), QStringLiteral("Deleted file returned during a shard rebuild"));
    }
    rebuilding.join();
    const auto buildIntervals = observer.finish();
    require(rebuildSucceeded, rebuildError);
    const bool singleShard = buildIntervals.size() == 1 && buildIntervals.front().start >= 0
        && buildIntervals.front().end > buildIntervals.front().start;
    QJsonArray rawBuildIntervals;
    for (const auto &interval : buildIntervals)
        rawBuildIntervals.append(QJsonObject{{QStringLiteral("startNanoseconds"), interval.start},
                                            {QStringLiteral("endNanoseconds"), interval.end}});
    QJsonArray rawQueryIntervals;
    std::vector<double> concurrentTimes;
    for (const auto &interval : queryIntervals) {
        const double duration = double(interval.end - interval.start) / 1000000.0;
        const bool overlaps = singleShard && interval.start < buildIntervals.front().end
            && interval.end > buildIntervals.front().start;
        if (overlaps)
            concurrentTimes.push_back(duration);
        rawQueryIntervals.append(QJsonObject{{QStringLiteral("startNanoseconds"), interval.start},
                                            {QStringLiteral("endNanoseconds"), interval.end},
                                            {QStringLiteral("durationMilliseconds"), duration},
                                            {QStringLiteral("overlapsBuild"), overlaps}});
    }
    const bool measured = singleShard && !concurrentTimes.empty();
    const double p95 = percentile(concurrentTimes, .95);
    report.insert(QStringLiteral("concurrentRebuild"), QJsonObject{
        {QStringLiteral("singleShard"), singleShard},
        {QStringLiteral("rebuildMilliseconds"), rebuildTimer.elapsed()},
        {QStringLiteral("timingClock"), QStringLiteral("steady-clock-relative-nanoseconds")},
        {QStringLiteral("observedBuilds"), int(buildIntervals.size())},
        {QStringLiteral("measuredQueries"), int(queryIntervals.size())},
        {QStringLiteral("buildIntervals"), rawBuildIntervals},
        {QStringLiteral("queryIntervals"), rawQueryIntervals},
        {QStringLiteral("overlappingQueries"), int(concurrentTimes.size())},
        {QStringLiteral("p50Milliseconds"), percentile(concurrentTimes, .50)},
        {QStringLiteral("p95Milliseconds"), p95},
        {QStringLiteral("p99Milliseconds"), percentile(concurrentTimes, .99)},
        {QStringLiteral("passed"), measured && p95 <= 1000}});
    report.insert(QStringLiteral("concurrentRebuildEvaluated"), measured);
    require(repository.flush(&error), error);
    require(repository.checkIntegrity(&error), error);
    budget.check();
    return measured && p95 <= 1000;
}

QString recoveryPath(const RuntimePaths &paths)
{
    return QDir(paths.profileDir).filePath(QStringLiteral("recovery/committed.txt"));
}

struct RecoveryExpectation {
    QString version;
    QString token;
    int vectorDimension;
};

RecoveryExpectation recoveryExpectation(const QString &phase)
{
    if (phase == QStringLiteral("committed-before-ann") || phase == QStringLiteral("uncommitted-sqlite"))
        return {QStringLiteral("recovery-committed-before-ann"),
                QStringLiteral("recoverycommittedbeforeanntoken"), 700};
    if (phase == QStringLiteral("ann-file-saved-before-manifest"))
        return {QStringLiteral("recovery-ann-file-saved-before-manifest"),
                QStringLiteral("recoveryannfilesavedbeforemanifesttoken"), 702};
    require(phase == QStringLiteral("ann-manifest-published-before-outbox-cleanup"),
            QStringLiteral("Unknown recovery identity"));
    return {QStringLiteral("recovery-ann-manifest-published-before-outbox-cleanup"),
            QStringLiteral("recoveryannmanifestpublishedbeforeoutboxcleanuptoken"), 703};
}

int runCrashChild(const QCommandLineParser &parser)
{
    const QString profile = parser.value(QStringLiteral("profile-dir"));
    QFile marker(QDir(profile).filePath(QStringLiteral("qcopilots-search-benchmark-owner")));
    require(marker.open(QIODevice::ReadOnly), QStringLiteral("Recovery child has no benchmark ownership marker"));
    require(marker.readAll() == parser.value(QStringLiteral("fault-token")).toUtf8()
                && !parser.value(QStringLiteral("fault-token")).isEmpty(),
            QStringLiteral("Recovery child ownership token mismatch"));
    marker.close();
    const auto paths = RuntimePaths::resolve(profile);
    const QString mode = parser.value(QStringLiteral("fault-child"));
    const auto expected = recoveryExpectation(mode);
    const bool annFileFault = mode == QStringLiteral("ann-file-saved-before-manifest");
    const bool annOutboxFault = mode == QStringLiteral("ann-manifest-published-before-outbox-cleanup");
    if (mode == QStringLiteral("committed-before-ann") || annFileFault || annOutboxFault) {
        StorageOptions options;
        options.diskReserveBytes = parser.value(QStringLiteral("min-free-gib")).toLongLong() * gib;
        if (annFileFault || annOutboxFault) {
            options.faultHook = [mode](const QString &point) {
                if (point == mode)
                    std::_Exit(73);
            };
        }
        IndexRepository repository(options);
        QString error;
        require(repository.open(paths, &error), error);
        Segment segment;
        segment.text = expected.token;
        segment.kind = QStringLiteral("text");
        segment.modality = QStringLiteral("text");
        segment.embedding = QVector<float>(embeddingDimensions, 0);
        segment.embedding[expected.vectorDimension] = 1;
        require(repository.replaceFiles({{recoveryPath(paths), expected.version, false, {segment}}}, &error), error);
        if (annOutboxFault) {
            // The caller's ownership token was checked before entering this
            // test-only SQL path. A large same-shard outbox reproduces the exact
            // threshold that previously allowed stale events to keep triggering
            // maintenance after the manifest had already committed.
            auto database = openBenchmarkDatabase(paths.databaseFile(), SQLITE_OPEN_READWRITE,
                                                  QStringLiteral("Cannot prepare the recovery outbox"));
            sqlite3_busy_timeout(database.get(), 30000);
            const char *sql =
                "WITH RECURSIVE copies(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM copies WHERE n<2048) "
                "INSERT INTO ann_events(generation,shard,segment_id,operation) "
                "SELECT s.generation,s.shard,s.id,'upsert' FROM segments s "
                "JOIN files f ON f.id=s.file_id CROSS JOIN copies WHERE f.path=?";
            sqlite3_stmt *rawStatement = nullptr;
            require(sqlite3_prepare_v2(database.get(), sql, -1, &rawStatement, nullptr) == SQLITE_OK,
                    QStringLiteral("Cannot prepare duplicate outbox events"));
            std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(rawStatement, sqlite3_finalize);
            const QByteArray path = recoveryPath(paths).toUtf8();
            require(sqlite3_bind_text(statement.get(), 1, path.constData(), int(path.size()), SQLITE_TRANSIENT) == SQLITE_OK
                        && sqlite3_step(statement.get()) == SQLITE_DONE && sqlite3_changes(database.get()) == 2048,
                    QStringLiteral("Did not create exactly 2048 same-shard recovery events"));
        }
        if (annFileFault || annOutboxFault) {
            require(annOutboxFault ? repository.flushOneShard(&error) : repository.flush(&error), error);
            require(false, QStringLiteral("ANN fault callback did not execute"));
        }
        std::_Exit(73); // No destructors or ANN flush: emulate abrupt process loss.
    }
    require(mode == QStringLiteral("uncommitted-sqlite"), QStringLiteral("Unknown recovery child phase"));
    auto database = openBenchmarkDatabase(paths.databaseFile(), SQLITE_OPEN_READWRITE,
                                          QStringLiteral("Recovery child cannot open database"));
    const char *sql = "BEGIN IMMEDIATE; UPDATE files SET version='uncommitted-corruption';";
    require(sqlite3_exec(database.get(), sql, nullptr, nullptr, nullptr) == SQLITE_OK,
            QStringLiteral("Recovery child could not start uncommitted transaction"));
    std::_Exit(73); // SQLite must roll this transaction back on the next open.
}

void exerciseRecovery(const RuntimePaths &paths, Budget &budget, bool enforceFolderRules, QJsonObject &report)
{
    const QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
    QSaveFile marker(QDir(paths.profileDir).filePath(QStringLiteral("qcopilots-search-benchmark-owner")));
    require(marker.open(QIODevice::WriteOnly) && marker.write(token.toUtf8()) == token.toUtf8().size() && marker.commit(),
            QStringLiteral("Cannot create recovery-child ownership marker"));
    QJsonArray phases;
    qint64 previousSegmentId = 0;
    QSet<QString> previousTokens;
    for (const QString &phase : {QStringLiteral("committed-before-ann"), QStringLiteral("uncommitted-sqlite"),
                                 QStringLiteral("ann-file-saved-before-manifest"),
                                 QStringLiteral("ann-manifest-published-before-outbox-cleanup")}) {
        const auto expected = recoveryExpectation(phase);
        QJsonObject phaseReport{{QStringLiteral("faultPoint"), phase},
                                {QStringLiteral("expectedVersion"), expected.version},
                                {QStringLiteral("expectedToken"), expected.token},
                                {QStringLiteral("expectedVectorDimension"), expected.vectorDimension},
                                {QStringLiteral("previousSegmentId"), previousSegmentId},
                                {QStringLiteral("passed"), false}};
        // Retain completed phases and the failed phase's actual observations if
        // a later assertion throws. The outer driver writes the final report.
        const auto preservePhase = qScopeGuard([&] {
            phases.append(phaseReport);
            report.insert(QStringLiteral("recoveryFaults"), phases);
        });
        QProcess child;
        child.setProgram(QCoreApplication::applicationFilePath());
        child.setArguments({QStringLiteral("--profile-dir"), paths.profileDir,
                            QStringLiteral("--fault-child"), phase, QStringLiteral("--fault-token"), token,
                            QStringLiteral("--min-free-gib"), QString::number(budget.reserve / gib)});
        // All recovery inputs are absolute. Inherit the valid launch cwd:
        // Windows process creation cannot use every long filesystem directory.
        child.start();
        require(child.waitForStarted(10000), QStringLiteral("Cannot start recovery child: %1").arg(child.errorString()));
        if (!child.waitForFinished(300000)) {
            child.kill();
            child.waitForFinished(10000);
            require(false, QStringLiteral("Recovery child timed out"));
        }
        phaseReport.insert(QStringLiteral("childExitCode"), child.exitCode());
        require(child.exitCode() == 73, QStringLiteral("Recovery child failed before fault point: %1").arg(QString::fromUtf8(child.readAllStandardError())));
        StorageOptions options;
        options.diskReserveBytes = budget.reserve;
        IndexRepository reopened(options);
        QString error;
        require(reopened.open(paths, &error), error);
        const QString observedVersion = reopened.fileVersion(recoveryPath(paths), false, &error);
        phaseReport.insert(QStringLiteral("observedVersion"), observedVersion);
        require(observedVersion == expected.version && error.isEmpty(),
                QStringLiteral("Recovery lost the committed file or exposed an uncommitted version"));
        auto keyword = benchmarkSearchOptions(paths, enforceFolderRules);
        keyword.query = expected.token;
        const auto keywordHits = reopened.search(keyword, &error);
        require(error.isEmpty() && keywordHits.size() == 1 && keywordHits.first().path == recoveryPath(paths),
                QStringLiteral("Recovery keyword result is inconsistent"));
        const auto &keywordHit = keywordHits.first();
        phaseReport.insert(QStringLiteral("observedToken"), keywordHit.text);
        phaseReport.insert(QStringLiteral("observedSegmentId"), keywordHit.segmentId);
        require(keywordHit.text == expected.token,
                QStringLiteral("Recovery returned a previous phase's text"));
        require(phase == QStringLiteral("uncommitted-sqlite")
                    ? keywordHit.segmentId == previousSegmentId
                    : keywordHit.segmentId > previousSegmentId,
                QStringLiteral("Recovery segment identity did not match this phase's commit or rollback"));
        qint64 oldKeywordHits = 0;
        for (const auto &oldToken : previousTokens) {
            if (oldToken == expected.token)
                continue;
            auto retired = benchmarkSearchOptions(paths, enforceFolderRules);
            retired.query = oldToken;
            const auto stale = reopened.search(retired, &error);
            require(error.isEmpty(), error);
            oldKeywordHits += stale.size();
        }
        phaseReport.insert(QStringLiteral("oldKeywordHits"), oldKeywordHits);
        require(oldKeywordHits == 0, QStringLiteral("A replaced recovery phase's keyword remains searchable"));
        auto semantic = benchmarkSearchOptions(paths, enforceFolderRules);
        semantic.queryEmbedding = QVector<float>(embeddingDimensions, 0);
        semantic.queryEmbedding[expected.vectorDimension] = 1;
        semantic.limit = 1;
        const auto vectorHits = reopened.search(semantic, &error);
        require(error.isEmpty() && vectorHits.size() == 1 && vectorHits.first().path == recoveryPath(paths),
                QStringLiteral("Recovery did not replay the committed vector delta"));
        const auto &vectorHit = vectorHits.first();
        const qint64 staleVersionHits = qint64(keywordHit.sourceVersion != expected.version)
                                     + qint64(vectorHit.sourceVersion != expected.version);
        phaseReport.insert(QStringLiteral("staleVersionHits"), staleVersionHits);
        require(staleVersionHits == 0 && vectorHit.segmentId == keywordHit.segmentId,
                QStringLiteral("Recovery keyword and semantic results refer to inconsistent source versions or segments"));

        // Observe the persisted FP16 vector independently of the ANN and the
        // storage decoder. A matching path alone cannot prove this phase's
        // replacement vector survived the crash.
        {
            auto database = openBenchmarkDatabase(paths.databaseFile(), SQLITE_OPEN_READONLY,
                                                  QStringLiteral("Cannot inspect recovered vector and rollback state"));
            sqlite3_busy_timeout(database.get(), 30000);
            sqlite3_stmt *rawStatement = nullptr;
            require(sqlite3_prepare_v2(database.get(), "SELECT embedding FROM segments WHERE id=?", -1,
                                       &rawStatement, nullptr) == SQLITE_OK,
                    QStringLiteral("Cannot prepare recovered-vector observation"));
            std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(rawStatement, sqlite3_finalize);
            require(sqlite3_bind_int64(statement.get(), 1, vectorHit.segmentId) == SQLITE_OK
                        && sqlite3_step(statement.get()) == SQLITE_ROW
                        && sqlite3_column_bytes(statement.get(), 0) == embeddingDimensions * 2,
                    QStringLiteral("Recovered semantic result has no valid FP16 vector"));
            const auto *bytes = static_cast<const unsigned char *>(sqlite3_column_blob(statement.get(), 0));
            int observedDimension = -1;
            double maximumAbsolute = -1;
            double normSquared = 0;
            double expectedComponent = 0;
            for (int dimension = 0; dimension < embeddingDimensions; ++dimension) {
                const double value = fromHalf(quint16(bytes[2 * dimension])
                                              | quint16(bytes[2 * dimension + 1] << 8));
                require(std::isfinite(value), QStringLiteral("Recovered vector contains a non-finite value"));
                normSquared += value * value;
                if (std::abs(value) > maximumAbsolute) {
                    maximumAbsolute = std::abs(value);
                    observedDimension = dimension;
                }
                if (dimension == expected.vectorDimension)
                    expectedComponent = value;
            }
            require(normSquared > 0, QStringLiteral("Recovered vector has zero norm"));
            const double observedCosine = expectedComponent / std::sqrt(normSquared);
            const bool vectorMatched = observedDimension == expected.vectorDimension && observedCosine >= .999;
            phaseReport.insert(QStringLiteral("observedVectorDimension"), observedDimension);
            phaseReport.insert(QStringLiteral("observedVectorCosine"), observedCosine);
            phaseReport.insert(QStringLiteral("vectorMatched"), vectorMatched);
            require(vectorMatched, QStringLiteral("Recovered vector belongs to an earlier fault phase"));
            statement.reset();

            rawStatement = nullptr;
            require(sqlite3_prepare_v2(database.get(),
                                       "SELECT count(*) FROM files WHERE version='uncommitted-corruption'", -1,
                                       &rawStatement, nullptr) == SQLITE_OK,
                    QStringLiteral("Cannot prepare whole-database rollback observation"));
            statement.reset(rawStatement);
            require(sqlite3_step(statement.get()) == SQLITE_ROW,
                    QStringLiteral("Cannot inspect whole-database rollback state"));
            const qint64 corruptionRows = sqlite3_column_int64(statement.get(), 0);
            phaseReport.insert(QStringLiteral("uncommittedCorruptionRows"), corruptionRows);
            require(corruptionRows == 0, QStringLiteral("Recovery exposed uncommitted versions in the corpus"));
        }
        if (phase == QStringLiteral("ann-manifest-published-before-outbox-cleanup")) {
            auto database = openBenchmarkDatabase(paths.databaseFile(), SQLITE_OPEN_READONLY,
                                                  QStringLiteral("Cannot inspect the recovered outbox"));
            const char *sql =
                "SELECT count(*) FROM ann_events e JOIN segments s ON e.shard=s.shard "
                "JOIN files f ON f.id=s.file_id WHERE f.path=? AND e.generation<=s.generation";
            sqlite3_stmt *rawStatement = nullptr;
            require(sqlite3_prepare_v2(database.get(), sql, -1, &rawStatement, nullptr) == SQLITE_OK,
                    QStringLiteral("Cannot inspect stale published outbox events"));
            std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(rawStatement, sqlite3_finalize);
            const QByteArray path = recoveryPath(paths).toUtf8();
            require(sqlite3_bind_text(statement.get(), 1, path.constData(), int(path.size()), SQLITE_TRANSIENT) == SQLITE_OK
                        && sqlite3_step(statement.get()) == SQLITE_ROW && sqlite3_column_int64(statement.get(), 0) == 0,
                    QStringLiteral("Recovery retained already-published outbox events"));
            statement.reset();
            database.reset();
            const QString newPath = QDir(paths.profileDir).filePath(QStringLiteral("recovery/after-published-manifest.txt"));
            Segment next;
            next.kind = QStringLiteral("text");
            next.modality = QStringLiteral("text");
            next.text = QStringLiteral("postmanifestrecoverytoken");
            next.embedding = QVector<float>(embeddingDimensions, 0);
            next.embedding[701] = 1;
            QElapsedTimer timer;
            timer.start();
            require(reopened.replaceFile(newPath, QStringLiteral("post-recovery"), {next}, false, &error), error);
            phaseReport.insert(QStringLiteral("postRecoveryWriteMilliseconds"), timer.elapsed());
            require(reopened.fileVersion(newPath, false, &error) == QStringLiteral("post-recovery") && error.isEmpty(),
                    QStringLiteral("A new file could not be indexed after outbox recovery"));
            auto nextQuery = benchmarkSearchOptions(paths, enforceFolderRules);
            nextQuery.query = next.text;
            const auto nextHits = reopened.search(nextQuery, &error);
            require(error.isEmpty() && nextHits.size() == 1 && nextHits.first().path == newPath,
                    QStringLiteral("A new file was not searchable after outbox recovery"));
            nextQuery.query.clear();
            nextQuery.queryEmbedding = next.embedding;
            nextQuery.limit = 1;
            const auto nextVectorHits = reopened.search(nextQuery, &error);
            require(error.isEmpty() && nextVectorHits.size() == 1 && nextVectorHits.first().path == newPath,
                    QStringLiteral("A new vector was not searchable after outbox recovery"));
            phaseReport.insert(QStringLiteral("injectedSameShardEvents"), 2048);
            phaseReport.insert(QStringLiteral("stalePublishedOutboxEvents"), 0);
            phaseReport.insert(QStringLiteral("postRecoveryWritePassed"), true);
        }
        require(reopened.checkIntegrity(&error), error);
        require(reopened.flush(&error), error);
        budget.check();
        phaseReport.insert(QStringLiteral("passed"), true);
        previousSegmentId = keywordHit.segmentId;
        previousTokens.insert(expected.token);
    }
    report.insert(QStringLiteral("recoveryFaultInjectionEvaluated"), true);
    report.insert(QStringLiteral("recoveryFaults"), phases);
    report.insert(QStringLiteral("annManifestInterruptionEvaluated"), true);
    report.insert(QStringLiteral("publishedManifestOutboxInterruptionEvaluated"), true);
}

void writeReport(const QString &path, const QJsonObject &report)
{
    require(QDir().mkpath(QFileInfo(path).absolutePath()), QStringLiteral("Cannot create report parent directory"));
    QSaveFile file(path);
    require(file.open(QIODevice::WriteOnly), QStringLiteral("Cannot open report %1: %2").arg(path, file.errorString()));
    const auto bytes = QJsonDocument(report).toJson(QJsonDocument::Indented);
    require(file.write(bytes) == bytes.size() && file.commit(), QStringLiteral("Cannot commit report %1").arg(path));
}

qint64 positiveInteger(const QCommandLineParser &parser, const QString &name, qint64 maximum)
{
    bool valid = false;
    const auto value = parser.value(name).toLongLong(&valid);
    require(valid && value > 0 && value <= maximum, QStringLiteral("--%1 must be between 1 and %2").arg(name).arg(maximum));
    return value;
}
} // namespace

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcopilots-search-benchmark"));
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Real SQLite/ANN capacity benchmark using seeded synthetic 768-dimensional vectors. Profiles are isolated below an explicit external directory."));
    parser.addHelpOption();
    parser.addOptions({
        {{QStringLiteral("profile-dir")}, QStringLiteral("Required absolute parent of an isolated benchmark profile"), QStringLiteral("directory")},
        {{QStringLiteral("files")}, QStringLiteral("Logical file count (no physical corpus files are created)"), QStringLiteral("count"), QStringLiteral("100000")},
        {{QStringLiteral("segments-per-file")}, QStringLiteral("Vectors per logical file"), QStringLiteral("count"), QStringLiteral("10")},
        {{QStringLiteral("queries")}, QStringLiteral("Queries checked against exhaustive exact top-20 file retrieval"), QStringLiteral("count"), QStringLiteral("100")},
        {{QStringLiteral("timings")}, QStringLiteral("Timed searches after 10 warmup searches"), QStringLiteral("count"), QStringLiteral("500")},
        {{QStringLiteral("seed")}, QStringLiteral("Deterministic unsigned generator seed"), QStringLiteral("integer"), QStringLiteral("20261011")},
        {{QStringLiteral("report")}, QStringLiteral("JSON report file outside the temporary benchmark profile"), QStringLiteral("file")},
        {{QStringLiteral("min-free-gib")}, QStringLiteral("Minimum available volume space"), QStringLiteral("GiB"), QStringLiteral("3")},
        {{QStringLiteral("budget-gib")}, QStringLiteral("Maximum sampled benchmark directory size"), QStringLiteral("GiB"), QStringLiteral("7")},
        {{QStringLiteral("enforce-folder-rules")}, QStringLiteral("Use production folder authorization during every search (true or false)"), QStringLiteral("boolean"), QStringLiteral("true")},
        {{QStringLiteral("load-note")}, QStringLiteral("Describe concurrent machine load and whether measurements are final"), QStringLiteral("text")},
        {{QStringLiteral("keep")}, QStringLiteral("Keep this run's database and ANN after completion or failure")},
        {{QStringLiteral("fault-child")}, QStringLiteral("Internal recovery subprocess mode, requires an ownership token"), QStringLiteral("phase")},
        {{QStringLiteral("fault-token")}, QStringLiteral("Internal recovery subprocess ownership token"), QStringLiteral("token")}
    });
    if (!parser.parse(QCoreApplication::arguments())) {
        QTextStream(stderr) << parser.errorText() << Qt::endl;
        return 1;
    }
    if (parser.isSet(QStringLiteral("help")) || parser.isSet(QStringLiteral("help-all"))) {
        QTextStream(stdout) << parser.helpText();
        return 0;
    }
    QJsonObject report{{QStringLiteral("formatVersion"), 1},
                       {QStringLiteral("benchmark"), QStringLiteral("synthetic-capacity")},
                       {QStringLiteral("startedUtc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
                       {QStringLiteral("passed"), false}};
    QString reportPath;
    std::unique_ptr<QTemporaryDir> temporary;
    std::unique_ptr<Budget> budget;
    int exitCode = 1;
    try {
        if (parser.isSet(QStringLiteral("fault-child")))
            return runCrashChild(parser);
        const QString parent = parser.value(QStringLiteral("profile-dir"));
        require(!parent.isEmpty() && QDir::isAbsolutePath(parent), QStringLiteral("--profile-dir is required and must be absolute"));
        // Reject a source tree supplied by mistake, including nested build dirs.
        for (QDir ancestor(QDir::cleanPath(parent)); ; ) {
            require(!(QFileInfo::exists(ancestor.filePath(QStringLiteral("CMakeLists.txt")))
                      && QDir(ancestor.filePath(QStringLiteral("src"))).exists()),
                    QStringLiteral("Benchmark profiles must be outside a CMake source tree"));
            const QString current = ancestor.absolutePath();
            const QString next = QFileInfo(current).absolutePath();
            if (current == next)
                break;
            ancestor.setPath(next);
        }
        require(QDir().mkpath(parent), QStringLiteral("Cannot create benchmark profile parent"));
        temporary = std::make_unique<QTemporaryDir>(QDir(parent).filePath(QStringLiteral("qcopilots-search-benchmark-XXXXXX")));
        require(temporary->isValid(), QStringLiteral("Cannot create isolated benchmark profile: %1").arg(temporary->errorString()));
        temporary->setAutoRemove(!parser.isSet(QStringLiteral("keep")));
        reportPath = parser.isSet(QStringLiteral("report"))
            ? QFileInfo(parser.value(QStringLiteral("report"))).absoluteFilePath()
            : QDir(parent).filePath(QFileInfo(temporary->path()).fileName() + QStringLiteral(".json"));
        const auto reportRelative = QDir(temporary->path()).relativeFilePath(reportPath);
        require(reportRelative.startsWith(QStringLiteral("../")) || QDir::isAbsolutePath(reportRelative),
                QStringLiteral("--report must be outside the temporary run directory"));
        const qint64 files = positiveInteger(parser, QStringLiteral("files"), 100000000);
        const int segmentsPerFile = int(positiveInteger(parser, QStringLiteral("segments-per-file"), 1024));
        const int queryCount = int(positiveInteger(parser, QStringLiteral("queries"), 10000));
        const int timings = int(positiveInteger(parser, QStringLiteral("timings"), 1000000));
        const QString folderRulesValue = parser.value(QStringLiteral("enforce-folder-rules")).trimmed().toLower();
        require(folderRulesValue == QStringLiteral("true") || folderRulesValue == QStringLiteral("false")
                    || folderRulesValue == QStringLiteral("1") || folderRulesValue == QStringLiteral("0"),
                QStringLiteral("--enforce-folder-rules must be true, false, 1, or 0"));
        const bool enforceFolderRules = folderRulesValue == QStringLiteral("true") || folderRulesValue == QStringLiteral("1");
        bool validSeed = false;
        const quint64 seed = parser.value(QStringLiteral("seed")).toULongLong(&validSeed);
        require(validSeed, QStringLiteral("Invalid --seed"));
        budget = std::make_unique<Budget>();
        budget->root = temporary->path();
        budget->reserve = positiveInteger(parser, QStringLiteral("min-free-gib"), 100000) * gib;
        budget->limit = positiveInteger(parser, QStringLiteral("budget-gib"), 100000) * gib;
        budget->check();
        const auto paths = RuntimePaths::resolve(temporary->path());
        QString error;
        require(paths.ensure(&error), error);
        report.insert(QStringLiteral("profileDir"), temporary->path());
        report.insert(QStringLiteral("keep"), parser.isSet(QStringLiteral("keep")));
        report.insert(QStringLiteral("files"), files);
        report.insert(QStringLiteral("segmentsPerFile"), segmentsPerFile);
        report.insert(QStringLiteral("segments"), files * segmentsPerFile);
        report.insert(QStringLiteral("dimensions"), embeddingDimensions);
        report.insert(QStringLiteral("annEfSearch"), VectorIndex::searchEf);
        report.insert(QStringLiteral("seed"), QString::number(seed));
        report.insert(QStringLiteral("generatorVersion"), QStringLiteral("splitmix64-v1-cluster64-noise008-uniform10percent"));
        report.insert(QStringLiteral("semanticQualityEvaluated"), false);
        report.insert(QStringLiteral("physicalFileTraversalEvaluated"), false);
        report.insert(QStringLiteral("enforceFolderRules"), enforceFolderRules);
        report.insert(QStringLiteral("cancellationTokenEnabled"), true);
        report.insert(QStringLiteral("allowedRoots"), QJsonArray{paths.profileDir});
        report.insert(QStringLiteral("loadNote"), parser.value(QStringLiteral("load-note")));
        report.insert(QStringLiteral("concurrentRebuildEvaluated"), false);
        report.insert(QStringLiteral("recoveryFaultInjectionEvaluated"), false);
        report.insert(QStringLiteral("minimumFreeBytes"), budget->reserve);
        report.insert(QStringLiteral("directoryBudgetBytes"), budget->limit);
        const Generator generator(seed);
        QElapsedTimer buildTimer;
        buildTimer.start();
        QString databasePath;
        {
            StorageOptions options;
            options.diskReserveBytes = budget->reserve;
            IndexRepository repository(options);
            require(repository.open(paths, &error), error);
            require(repository.setPipelineFingerprint(QStringLiteral("synthetic-capacity-768-v1"), &error), error);
            const int batchFiles = std::max(1, 1024 / segmentsPerFile);
            bool pilotDone = false;
            qint64 completed = 0;
            while (completed < files) {
                const int count = int(std::min(qint64(batchFiles), files - completed));
                budget->check(qint64(count) * segmentsPerFile * 8192);
                QVector<IndexedFile> batch;
                batch.reserve(count);
                for (int i = 0; i < count; ++i) {
                    const qint64 fileNumber = completed + i;
                    IndexedFile file;
                    file.path = logicalPath(paths.profileDir, fileNumber);
                    file.version = QStringLiteral("synthetic-v1");
                    file.segments.reserve(segmentsPerFile);
                    for (int segmentNumber = 0; segmentNumber < segmentsPerFile; ++segmentNumber) {
                        Segment segment;
                        segment.path = file.path;
                        segment.title = QStringLiteral("Synthetic file %1").arg(fileNumber);
                        segment.kind = QStringLiteral("text");
                        segment.modality = QStringLiteral("text");
                        segment.text = QStringLiteral("Synthetic capacity fixture %1 segment %2").arg(fileNumber).arg(segmentNumber);
                        segment.lineStart = segmentNumber + 1;
                        segment.lineEnd = segmentNumber + 1;
                        segment.embedding = generator.vector(fileNumber, segmentNumber);
                        file.segments.push_back(std::move(segment));
                    }
                    batch.push_back(std::move(file));
                }
                require(repository.replaceFiles(batch, &error, true), error);
                completed += count;
                budget->check();
                if (!pilotDone && completed * segmentsPerFile >= std::min(pilotSegments, files * segmentsPerFile)) {
                    // Finalize the pilot's ANN before extrapolating actual storage.
                    require(repository.flush(&error), error);
                    const qint64 bytes = budget->check();
                    const qint64 projected = qint64(std::ceil(double(bytes) * double(files) / double(completed) * 1.10));
                    report.insert(QStringLiteral("pilotSegments"), completed * segmentsPerFile);
                    report.insert(QStringLiteral("pilotBytes"), bytes);
                    report.insert(QStringLiteral("projectedBytesIncludingTenPercentHeadroom"), projected);
                    require(projected <= budget->limit, QStringLiteral("Pilot predicts %1 bytes, exceeding the directory budget").arg(projected));
                    budget->check(std::max(qint64(0), projected - bytes));
                    pilotDone = true;
                    writeReport(reportPath, report);
                }
                if (completed == files || completed % (qint64(batchFiles) * 20) == 0)
                    QTextStream(stdout) << "indexed " << completed << '/' << files << " files" << Qt::endl;
            }
            require(repository.flush(&error), error);
            require(repository.checkIntegrity(&error), error);
            budget->check();
            report.insert(QStringLiteral("builtStatus"), repository.status(&error));
            require(error.isEmpty(), error);
            databasePath = repository.databasePath();
        }
        report.insert(QStringLiteral("buildMilliseconds"), buildTimer.elapsed());
        const auto queries = makeQueries(generator, files, queryCount, segmentsPerFile, seed);
        const auto oracle = exactOracle(databasePath, queries, files * segmentsPerFile, report);
        StorageOptions searchStorageOptions;
        searchStorageOptions.diskReserveBytes = budget->reserve;
        BuildObserver observer;
        searchStorageOptions.faultHook = [&](const QString &point) { observer.observe(point); };
        auto capacityRepository = std::make_unique<IndexRepository>(searchStorageOptions);
        IndexRepository &reopened = *capacityRepository;
        require(reopened.open(paths, &error), error);
        require(reopened.checkIntegrity(&error), error);
        report.insert(QStringLiteral("reopenedStatus"), reopened.status(&error));
        require(error.isEmpty(), error);
        QJsonArray queryReports;
        double recallSum = 0;
        double clusteredSum = 0;
        double uniformSum = 0;
        int clusteredCount = 0;
        int uniformCount = 0;
        for (size_t q = 0; q < queries.size(); ++q) {
            auto options = benchmarkSearchOptions(paths, enforceFolderRules);
            options.queryEmbedding = queries[q].values;
            options.limit = topK;
            const auto hits = reopened.search(options, &error);
            require(error.isEmpty(), error);
            QSet<QString> found;
            for (const auto &hit : hits)
                found.insert(hit.path);
            require(found.size() == hits.size(), QStringLiteral("Search returned duplicate files"));
            int correct = 0;
            for (const auto &path : found)
                correct += oracle[q].contains(path) ? 1 : 0;
            const double recall = oracle[q].isEmpty() ? 0 : double(correct) / double(oracle[q].size());
            recallSum += recall;
            if (queries[q].distribution == QStringLiteral("uniform")) {
                uniformSum += recall;
                ++uniformCount;
            } else {
                clusteredSum += recall;
                ++clusteredCount;
            }
            queryReports.append(QJsonObject{{QStringLiteral("query"), int(q)},
                                           {QStringLiteral("distribution"), queries[q].distribution},
                                           {QStringLiteral("sourceFile"), queries[q].sourceFile},
                                           {QStringLiteral("recallAt20"), recall},
                                           {QStringLiteral("returnedFiles"), hits.size()}});
        }
        report.insert(QStringLiteral("queries"), queryReports);
        const double recall = recallSum / double(queries.size());
        report.insert(QStringLiteral("recallAt20"), recall);
        if (clusteredCount)
            report.insert(QStringLiteral("clusteredRecallAt20"), clusteredSum / clusteredCount);
        if (uniformCount)
            report.insert(QStringLiteral("uniformRecallAt20"), uniformSum / uniformCount);
        report.insert(QStringLiteral("recallTarget"), 0.95);
        report.insert(QStringLiteral("recallPassed"), recall >= 0.95);
        std::vector<double> elapsed;
        elapsed.reserve(size_t(timings));
        for (int iteration = -10; iteration < timings; ++iteration) {
            auto options = benchmarkSearchOptions(paths, enforceFolderRules);
            options.queryEmbedding = queries[size_t(iteration + 10) % queries.size()].values;
            options.limit = topK;
            QElapsedTimer timer;
            timer.start();
            const auto hits = reopened.search(options, &error);
            const double milliseconds = double(timer.nsecsElapsed()) / 1000000.0;
            require(error.isEmpty() && !hits.isEmpty(), error.isEmpty() ? QStringLiteral("Timed search returned no results") : error);
            if (iteration >= 0)
                elapsed.push_back(milliseconds);
        }
        const double p95 = percentile(elapsed, .95);
        report.insert(QStringLiteral("timedQueries"), timings);
        report.insert(QStringLiteral("warmupQueries"), 10);
        report.insert(QStringLiteral("latencyMilliseconds"), QJsonObject{{QStringLiteral("p50"), percentile(elapsed, .50)},
                                                                        {QStringLiteral("p95"), p95},
                                                                        {QStringLiteral("p99"), percentile(elapsed, .99)}});
        report.insert(QStringLiteral("latencyIncludesQueryEncoding"), false);
        report.insert(QStringLiteral("p95TargetMilliseconds"), 500);
        report.insert(QStringLiteral("latencyPassed"), p95 <= 500);
        budget->check();
        writeReport(reportPath, report);
        QTextStream(stdout) << "checking filtered exact recall" << Qt::endl;
        const bool filtersPassed = exerciseFilters(reopened, paths, queries, files, enforceFolderRules, report);
        writeReport(reportPath, report);
        QTextStream(stdout) << "checking additions, replacements, deletions, and concurrent single-shard rebuild" << Qt::endl;
        const bool rebuildPassed = exerciseMutationsAndRebuild(reopened, paths, generator, queries, files,
                                                               segmentsPerFile, timings, *budget, enforceFolderRules,
                                                               observer, report);
        writeReport(reportPath, report);
        capacityRepository.reset(); // Child fault injection requires exclusive profile ownership.
        QTextStream(stdout) << "checking process-loss recovery" << Qt::endl;
        exerciseRecovery(paths, *budget, enforceFolderRules, report);
        budget->check();
        const bool passed = recall >= 0.95 && p95 <= 500 && filtersPassed && rebuildPassed;
        report.insert(QStringLiteral("passed"), passed);
        exitCode = passed ? 0 : 2;
    } catch (const std::exception &exception) {
        report.insert(QStringLiteral("error"), QString::fromUtf8(exception.what()));
        QTextStream(stderr) << exception.what() << Qt::endl;
    }
    if (budget) {
        report.insert(QStringLiteral("sampledDirectoryPeakBytes"), budget->sampledPeak);
        report.insert(QStringLiteral("minimumObservedFreeBytes"), budget->minimumFree);
        report.insert(QStringLiteral("diskMeasurement"), QStringLiteral("Sampled at batch and phase boundaries, not a continuous peak measurement"));
    }
    if (temporary && !parser.isSet(QStringLiteral("keep"))) {
        const QString runDirectory = temporary->path();
        const bool removed = temporary->remove();
        report.insert(QStringLiteral("cleanupSucceeded"), removed);
        if (!removed) {
            report.insert(QStringLiteral("passed"), false);
            report.insert(QStringLiteral("cleanupError"), QStringLiteral("Could not remove isolated benchmark directory: %1").arg(runDirectory));
            QTextStream(stderr) << "Could not remove benchmark directory: " << runDirectory << Qt::endl;
            exitCode = 1;
        }
    }
    report.insert(QStringLiteral("finishedUtc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
    if (!reportPath.isEmpty()) {
        try {
            writeReport(reportPath, report);
            QTextStream(stdout) << "report " << reportPath << Qt::endl;
        } catch (const std::exception &exception) {
            QTextStream(stderr) << exception.what() << Qt::endl;
            exitCode = 1;
        }
    }
    return exitCode;
}
