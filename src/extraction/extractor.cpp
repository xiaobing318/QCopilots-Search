#include "extractor.h"
#include "core/file_formats.h"
#include "core/pipeline_manifest.h"
#include "document_extractor.h"
#include "image_extractor.h"
#include "media_extractor.h"
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#ifdef Q_OS_MACOS
#include <sys/xattr.h>
#endif

namespace qcopilots_search {

QString Extractor::sourceVersion(const QString &path)
{
    QFileInfo file(path);
    if (!file.exists() || !file.isFile()) return {};
    // The service additionally verifies its platform file identity and version.
    // This token prevents extraction from committing a visibly changed source.
    const QByteArray identity = file.canonicalFilePath().toUtf8() + '\0'
        + QByteArray::number(file.size()) + ':'
        + QByteArray::number(file.lastModified().toMSecsSinceEpoch());
    return QString::fromLatin1(QCryptographicHash::hash(identity, QCryptographicHash::Sha256).toHex());
}

QString Extractor::pipelineFingerprint()
{
    return extractionPipelineFingerprint();
}

bool Extractor::isScreenshotPath(const QString &path)
{
#ifdef Q_OS_MACOS
    const QByteArray native = QFile::encodeName(path);
    if (getxattr(native.constData(), "com.apple.metadata:kMDItemIsScreenCapture", nullptr, 0, 0, 0) >= 0)
        return true;
#endif
    const QString name = QFileInfo(path).fileName();
    for (const QString &prefix : {QStringLiteral("Screenshot"), QStringLiteral("Screen Shot"),
            QStringLiteral("CleanShot"), QStringLiteral("SCR-"), QStringLiteral("截图"), QStringLiteral("屏幕截图")})
        if (name.startsWith(prefix, Qt::CaseInsensitive)) return true;
    return false;
}

QString Extractor::kindForPath(const QString &path, bool code)
{
    const QFileInfo info(path);
    const QString suffix = info.suffix().toLower();
    if (code && suffix != QStringLiteral("ts") && suffix != QStringLiteral("mts"))
        return supportsCode(suffix, info.fileName()) ? QStringLiteral("code") : QString();
    if (suffix == QStringLiteral("pdf")) return QStringLiteral("pdf");
    if (supportsImage(suffix)) return QStringLiteral("image");
    if (supportsAudio(suffix)) return QStringLiteral("audio");
    if (supportsVideo(suffix)) {
        if (suffix == QStringLiteral("ts") || suffix == QStringLiteral("mts") || suffix == QStringLiteral("m2ts")) {
            QFile file(path);
            if (!file.open(QIODevice::ReadOnly)) return {};
            const bool transport = file_formats::isTransportStream(file.read(600));
            if (code) return !transport && supportsCode(suffix, info.fileName()) ? QStringLiteral("code") : QString();
            if (!transport) return {};
        }
        return QStringLiteral("video");
    }
    return supportsDocument(suffix) ? QStringLiteral("text") : QString();
}

ExtractionResult Extractor::extract(const QString &path, const ExtractionOptions &options)
{
    ExtractionResult result;
    result.next = options.start;
    auto fail = [&](const QString &message, const QString &state = QStringLiteral("failed")) {
        result.state = state;
        result.error = message;
        result.complete = false;
        return result;
    };
    if (path.isEmpty() || !QFileInfo(path).isAbsolute()) return fail(QStringLiteral("Extraction requires an absolute source path"));
    if (options.start < 0 || options.maxSegments < 1 || options.maxSegments > 5000
        || options.chunkCharacters < 16 || options.overlapCharacters < 0
        || options.overlapCharacters >= options.chunkCharacters || options.maxInputBytes < 1)
        return fail(QStringLiteral("Invalid extraction limits"));
    if (options.cancelled && options.cancelled()) return fail(QStringLiteral("Extraction cancelled"), QStringLiteral("cancelled"));
    QFileInfo file(path);
    if (!file.exists() || !file.isFile() || !file.isReadable()) return fail(QStringLiteral("Source is not a readable regular file"));
    const QString before = sourceVersion(path);
    const QString kind = kindForPath(path, options.code);
    if (kind.isEmpty()) return fail(QStringLiteral("Unsupported file type"), QStringLiteral("unsupported"));
    if (kind == QStringLiteral("text") || kind == QStringLiteral("code"))
        result = extractDocument(path, options);
    else if (kind == QStringLiteral("pdf")) result = extractPdf(path, options);
    else if (kind == QStringLiteral("image")) result = extractImage(path, options);
    else result = extractMedia(path, options, kind == QStringLiteral("video"));
    result.sourceVersion = before;
    if (sourceVersion(path) != before) {
        result.segments.clear();
        result.state = QStringLiteral("failed");
        result.error = QStringLiteral("Source changed during extraction");
        result.complete = false;
        result.next = options.start;
    }
    for (Segment &segment : result.segments) {
        segment.path = file.absoluteFilePath();
        if (segment.title.isEmpty()) segment.title = file.completeBaseName();
        if (segment.kind.isEmpty()) segment.kind = kind;
    }
    return result;
}

} // namespace qcopilots_search
