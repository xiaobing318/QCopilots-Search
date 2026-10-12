#pragma once

#include "core/types.h"
#include <functional>

namespace qcopilots_search {

// All writable media is placed in this caller-owned directory. Keep it alive
// until embeddings have consumed every returned Segment::mediaPath.
struct ExtractionOptions {
    QString tempDir;
    QString tessdataDir;
    bool code = false;
    bool enableOcr = true;
    int start = 0;
    int maxSegments = 40;
    int chunkCharacters = 1800;
    int overlapCharacters = 200;
    qint64 maxInputBytes = 64 * 1024 * 1024;
    std::function<bool()> cancelled;
};

struct ExtractionResult {
    QList<Segment> segments;
    QString state; // complete, partial, cancelled, failed, unsupported
    QString error;
    QString sourceVersion;
    // Deterministic cursor. Only resume with the same sourceVersion and options.
    int next = 0;
    bool complete = true;
};

class Extractor final {
public:
    static ExtractionResult extract(const QString &path, const ExtractionOptions &options = {});
    static QString kindForPath(const QString &path, bool code = false);
    static bool isScreenshotPath(const QString &path);
    static QString sourceVersion(const QString &path);
    static QString pipelineFingerprint();
};

} // namespace qcopilots_search
