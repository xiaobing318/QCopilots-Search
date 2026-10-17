#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QString>
#include <QStringList>
#include <QVector>
#include <atomic>
#include <memory>

namespace qcopilots_search {

struct Segment {
    qint64 id = 0;
    QString path;
    QString title;
    QString kind;
    QString modality;
    QString text;
    QString mediaPath;
    int imageBudget = 140;
    int page = 0;
    int lineStart = 0;
    int lineEnd = 0;
    int cell = 0;
    qint64 startMs = 0;
    qint64 endMs = 0;
    QVector<float> embedding;
    QJsonObject toJson(bool includeEmbedding = true) const;
    static Segment fromJson(const QJsonObject &value);
};

struct SearchHit {
    qint64 segmentId = 0;
    QString sourceVersion;
    QString path;
    QString title;
    QString kind;
    QString text;
    QString matchSource;
    double score = 0;
    int page = 0;
    int lineStart = 0;
    int lineEnd = 0;
    int cell = 0;
    qint64 startMs = 0;
    qint64 endMs = 0;
    QJsonObject toJson() const;
    static SearchHit fromJson(const QJsonObject &value);
};

struct SearchOptions {
    QString query;
    QString kind;
    QString root;
    QStringList allowedRoots;
    QStringList excludedRoots;
    bool enforceFolderRules = false;
    bool code = false;
    int limit = 100;
    QVector<float> queryEmbedding;
    // Process-local cancellation shared by service, SQLite and ANN search.
    // The pointer is never serialized or sent over IPC.
    std::shared_ptr<std::atomic_bool> cancelled;
};

// Browsing operates on indexed files, independently of segment search/ranking.
struct FileListEntry {
    qint64 fileId = 0;
    QString path;
    QString title;
    QString kind;
    qint64 modifiedMs = 0;
    QString sourceVersion;
    bool code = false;
    QJsonObject toJson() const;
    static FileListEntry fromJson(const QJsonObject &value);
};

struct FileListOptions {
    QString kind;
    QString root;
    QStringList allowedRoots;
    QStringList excludedRoots;
    bool enforceFolderRules = false;
    bool code = false;
    int limit = 100;
    QString cursor;
    // Service-owned visibility epoch. Never populated from a client cursor.
    QString configurationToken;
    std::shared_ptr<std::atomic_bool> cancelled;
};

struct FileListPage {
    QList<FileListEntry> files;
    QString nextCursor;
    bool hasMore = false;
    qint64 browseGeneration = 0;
    bool metadataPending = false;
    QString errorCode;
    QString error;
    QJsonObject toJson() const;
};

struct IndexProgress {
    QString state = QStringLiteral("idle");
    QString currentPath;
    qint64 total = 0;
    qint64 completed = 0;
    qint64 failed = 0;
    qint64 segments = 0;
    QString error;
    QJsonObject toJson() const;
};

inline constexpr int protocolVersion = 1;
inline constexpr int maximumSearchResults = 500;
inline constexpr int embeddingDimensions = 768;
inline constexpr int maximumEmbeddingDimensions = 4096;
inline constexpr qsizetype maxMessageBytes = 16 * 1024 * 1024;

} // namespace qcopilots_search
