#include "types.h"

#include <cmath>

namespace qcopilots_search {
namespace {
QJsonObject location(const QString &path, const QString &title, const QString &kind,
                     const QString &text, int page, int lineStart, int lineEnd,
                     int cell, qint64 startMs, qint64 endMs)
{
    return {{QStringLiteral("path"), path}, {QStringLiteral("title"), title},
            {QStringLiteral("kind"), kind}, {QStringLiteral("text"), text},
            {QStringLiteral("page"), page}, {QStringLiteral("lineStart"), lineStart},
            {QStringLiteral("lineEnd"), lineEnd}, {QStringLiteral("cell"), cell},
            {QStringLiteral("startMs"), startMs}, {QStringLiteral("endMs"), endMs}};
}

template <typename T> void readLocation(T &target, const QJsonObject &value)
{
    target.path = value.value(QStringLiteral("path")).toString();
    target.title = value.value(QStringLiteral("title")).toString();
    target.kind = value.value(QStringLiteral("kind")).toString();
    target.text = value.value(QStringLiteral("text")).toString();
    target.page = value.value(QStringLiteral("page")).toInt();
    target.lineStart = value.value(QStringLiteral("lineStart")).toInt();
    target.lineEnd = value.value(QStringLiteral("lineEnd")).toInt();
    target.cell = value.value(QStringLiteral("cell")).toInt();
    target.startMs = value.value(QStringLiteral("startMs")).toInteger();
    target.endMs = value.value(QStringLiteral("endMs")).toInteger();
}
}

QJsonObject Segment::toJson(bool includeEmbedding) const
{
    auto object = location(path, title, kind, text, page, lineStart, lineEnd, cell, startMs, endMs);
    object.insert(QStringLiteral("id"), id);
    object.insert(QStringLiteral("modality"), modality);
    object.insert(QStringLiteral("mediaPath"), mediaPath);
    object.insert(QStringLiteral("imageBudget"), imageBudget);
    if (includeEmbedding) {
        QJsonArray values;
        for (const float component : embedding)
            values.append(static_cast<double>(component));
        object.insert(QStringLiteral("embedding"), values);
    }
    return object;
}

Segment Segment::fromJson(const QJsonObject &value)
{
    Segment segment;
    readLocation(segment, value);
    segment.id = value.value(QStringLiteral("id")).toInteger();
    segment.modality = value.value(QStringLiteral("modality")).toString();
    segment.mediaPath = value.value(QStringLiteral("mediaPath")).toString();
    segment.imageBudget = value.value(QStringLiteral("imageBudget")).toInt(140);
    const auto values = value.value(QStringLiteral("embedding")).toArray();
    if (values.size() <= maximumEmbeddingDimensions) {
        segment.embedding.reserve(values.size());
        for (const auto &component : values) {
            const float converted = static_cast<float>(component.toDouble());
            if (!component.isDouble() || !std::isfinite(converted)) {
                segment.embedding.clear();
                break;
            }
            segment.embedding.append(converted);
        }
    }
    return segment;
}

QJsonObject SearchHit::toJson() const
{
    auto object = location(path, title, kind, text, page, lineStart, lineEnd, cell, startMs, endMs);
    object.insert(QStringLiteral("segmentId"), segmentId);
    object.insert(QStringLiteral("sourceVersion"), sourceVersion);
    object.insert(QStringLiteral("matchSource"), matchSource);
    object.insert(QStringLiteral("score"), score);
    return object;
}

SearchHit SearchHit::fromJson(const QJsonObject &value)
{
    SearchHit hit;
    readLocation(hit, value);
    hit.segmentId = value.value(QStringLiteral("segmentId")).toInteger();
    hit.sourceVersion = value.value(QStringLiteral("sourceVersion")).toString();
    hit.matchSource = value.value(QStringLiteral("matchSource")).toString();
    hit.score = value.value(QStringLiteral("score")).toDouble();
    return hit;
}

QJsonObject FileListEntry::toJson() const
{
    return {{"fileId", QString::number(fileId)}, {"path", path}, {"title", title},
            {"kind", kind}, {"modifiedMs", QString::number(modifiedMs)},
            {"sourceVersion", sourceVersion}, {"code", code}};
}

FileListEntry FileListEntry::fromJson(const QJsonObject &value)
{
    FileListEntry entry;
    entry.fileId = value.value("fileId").toString().toLongLong();
    entry.path = value.value("path").toString();
    entry.title = value.value("title").toString();
    entry.kind = value.value("kind").toString();
    entry.modifiedMs = value.value("modifiedMs").toString().toLongLong();
    entry.sourceVersion = value.value("sourceVersion").toString();
    entry.code = value.value("code").toBool();
    return entry;
}

QJsonObject FileListPage::toJson() const
{
    QJsonArray values;
    for (const auto &file : files) values.append(file.toJson());
    return {{"files", values}, {"nextCursor", nextCursor}, {"hasMore", hasMore},
            {"browseGeneration", QString::number(browseGeneration)},
            {"metadataPending", metadataPending}};
}

QJsonObject IndexProgress::toJson() const
{
    return {{QStringLiteral("state"), state}, {QStringLiteral("currentPath"), currentPath},
            {QStringLiteral("total"), total}, {QStringLiteral("completed"), completed},
            {QStringLiteral("failed"), failed}, {QStringLiteral("segments"), segments},
            {QStringLiteral("error"), error}};
}
} // namespace qcopilots_search
