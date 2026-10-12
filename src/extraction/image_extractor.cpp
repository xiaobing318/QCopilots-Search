#include "image_extractor.h"
#include <QCoreApplication>
#include <QColorSpace>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QImageReader>
#include <QPainter>
#include <QPdfDocument>
#include <QPdfSelection>
#include <QSaveFile>
#include <QSet>
#include <QStorageInfo>
#include <QUuid>
#include <memory>
#include <cstring>
#include <algorithm>
#include <tesseract/baseapi.h>
#include <tesseract/publictypes.h>
#include <tesseract/ocrclass.h>
#include <libheif/heif.h>
#include <libraw/libraw.h>

namespace qcopilots_search {
namespace {
constexpr qint64 maximumPixels = 120'000'000;
constexpr qint64 maximumCompressedImage = 256 * 1024 * 1024;
const QSet<QString> rawFormats = {QStringLiteral("raw"), QStringLiteral("dng"), QStringLiteral("cr2"),
    QStringLiteral("cr3"), QStringLiteral("nef"), QStringLiteral("nrw"), QStringLiteral("arw"),
    QStringLiteral("sr2"), QStringLiteral("srf"), QStringLiteral("orf"), QStringLiteral("rw2"),
    QStringLiteral("raf"), QStringLiteral("pef"), QStringLiteral("srw"), QStringLiteral("kdc"),
    QStringLiteral("mrw"), QStringLiteral("erf"), QStringLiteral("mos"), QStringLiteral("iiq")};

ExtractionResult failure(const ExtractionOptions &options, const QString &error)
{
    ExtractionResult result;
    result.state = options.cancelled && options.cancelled() ? QStringLiteral("cancelled") : QStringLiteral("failed");
    result.error = error;
    result.complete = false;
    result.next = options.start;
    return result;
}

bool cancelled(const ExtractionOptions &options) { return options.cancelled && options.cancelled(); }

QImage opaqueOnWhite(const QImage &source)
{
    if (!source.hasAlphaChannel()) return source;
    QImage image(source.size(), QImage::Format_RGB32);
    if (image.isNull()) return {};
    image.fill(Qt::white);
    image.setColorSpace(source.colorSpace().isValid() ? source.colorSpace() : QColorSpace(QColorSpace::SRgb));
    QPainter painter(&image);
    painter.drawImage(0, 0, source);
    return image;
}

bool largeSingleLine(const QImage &image)
{
    // Restrict the scale retry to one horizontal ink band. Its geometry is
    // independent of OCR confidence, which can be high for a wrong CJK glyph.
    // Separate lines/columns retain the normal automatic-page-layout path.
    int top = -1, bottom = -1, left = image.width(), right = -1;
    int gap = 0;
    qint64 inkPixels = 0;
    for (int y = 0; y < image.height(); ++y) {
        const auto *row = image.constScanLine(y);
        int count = 0, rowLeft = image.width(), rowRight = -1;
        for (int x = 0; x < image.width(); ++x) {
            if (row[x] < 128) { ++count; rowLeft = qMin(rowLeft, x); rowRight = x; }
        }
        if (count < 3) { if (top >= 0) ++gap; continue; }
        if (top < 0) top = y;
        else if (gap > 2) return false;
        gap = 0; bottom = y;
        inkPixels += count;
        left = qMin(left, rowLeft); right = qMax(right, rowRight);
    }
    const int height = bottom - top + 1;
    const int width = right - left + 1;
    return top >= 0 && height >= 64 && width >= 2 * height && 2 * inkPixels < qint64(width) * height;
}

QString lettersAndNumbers(const QString &text)
{
    QString result;
    for (const char32_t character : text.toUcs4())
        if (QChar::isLetterOrNumber(character)) result += QString::fromUcs4(&character, 1);
    return result;
}

bool meaningfulNativePdfText(const QString &text)
{
    int letters = 0;
    int eastAsianLetters = 0;
    for (const char32_t character : text.toUcs4()) {
        if (!QChar::isLetter(character)) continue;
        ++letters;
        const auto script = QChar::script(character);
        if (script == QChar::Script_Han || script == QChar::Script_Hiragana
            || script == QChar::Script_Katakana || script == QChar::Script_Hangul)
            ++eastAsianLetters;
    }
    // A short paragraph can carry the whole page's meaning. Preserve its native
    // text channel without treating a lone page number or tiny header as prose.
    // CJK content needs fewer characters because it is not separated into words.
    return letters >= 24 || eastAsianLetters >= 8;
}

bool prepareDirectory(const QString &directory, QString *error)
{
    if (directory.isEmpty() || !QFileInfo(directory).isAbsolute()) {
        *error = QStringLiteral("An absolute caller-owned temporary directory is required for media extraction");
        return false;
    }
    if (!QDir().mkpath(directory)) {
        *error = QStringLiteral("Cannot create media temporary directory: %1").arg(directory);
        return false;
    }
    QStorageInfo storage(directory);
    if (storage.isValid() && storage.bytesAvailable() < 16 * 1024 * 1024) {
        *error = QStringLiteral("Insufficient space for extracted media");
        return false;
    }
    return true;
}

QString writeImage(const QImage &image, const QString &directory, QString *error)
{
    const QStorageInfo storage(directory);
    const qint64 required = qint64(image.sizeInBytes()) * 2 + 16 * 1024 * 1024;
    if (storage.isValid() && storage.bytesAvailable() >= 0 && storage.bytesAvailable() < required) {
        *error = QStringLiteral("Insufficient temporary space for the next prepared image");
        return {};
    }
    const QString path = QDir(directory).filePath(QStringLiteral("qcopilots-search-image-%1.png").arg(QUuid::createUuid().toString(QUuid::Id128)));
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly) || !image.save(&file, "PNG") || !file.commit()) {
        *error = QStringLiteral("Cannot write prepared image: %1").arg(file.errorString());
        return {};
    }
    return path;
}

QByteArray readBoundedImage(const QString &path, QString *error)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) { *error = file.errorString(); return {}; }
    if (file.size() > maximumCompressedImage) { *error = QStringLiteral("Compressed image exceeds 256 MiB limit"); return {}; }
    const QByteArray bytes = file.readAll();
    if (bytes.size() != file.size()) { *error = QStringLiteral("Image read failed"); return {}; }
    return bytes;
}

QImage decodeHeif(const QString &path, QString *error, const ExtractionOptions *options, qint64 pixelLimit)
{
    const QByteArray bytes = readBoundedImage(path, error);
    if (bytes.isEmpty()) return {};
    std::unique_ptr<heif_context, decltype(&heif_context_free)> context(heif_context_alloc(), heif_context_free);
    if (!context) { *error = QStringLiteral("Cannot allocate HEIF decoder"); return {}; }
    auto status = heif_context_read_from_memory_without_copy(context.get(), bytes.constData(), size_t(bytes.size()), nullptr);
    if (status.code != heif_error_Ok) { *error = QString::fromUtf8(status.message); return {}; }
    heif_image_handle *handleValue = nullptr;
    status = heif_context_get_primary_image_handle(context.get(), &handleValue);
    if (status.code != heif_error_Ok) { *error = QString::fromUtf8(status.message); return {}; }
    std::unique_ptr<heif_image_handle, decltype(&heif_image_handle_release)> handle(handleValue, heif_image_handle_release);
    const int width = heif_image_handle_get_width(handle.get());
    const int height = heif_image_handle_get_height(handle.get());
    if (width <= 0 || height <= 0 || qint64(width) * height > pixelLimit) {
        *error = QStringLiteral("HEIF dimensions exceed the decoder limit"); return {};
    }
    heif_image *imageValue = nullptr;
    std::unique_ptr<heif_decoding_options, decltype(&heif_decoding_options_free)>
        decodeOptions(heif_decoding_options_alloc(), heif_decoding_options_free);
    if (!decodeOptions) { *error = QStringLiteral("Cannot allocate HEIF decoding options"); return {}; }
    decodeOptions->convert_hdr_to_8bit = 1;
    decodeOptions->strict_decoding = 1;
    if (options) {
        decodeOptions->progress_user_data = const_cast<ExtractionOptions *>(options);
        decodeOptions->cancel_decoding = [](void *context) {
            return cancelled(*static_cast<const ExtractionOptions *>(context)) ? 1 : 0;
        };
    }
    status = heif_decode_image(handle.get(), &imageValue, heif_colorspace_RGB, heif_chroma_interleaved_RGBA, decodeOptions.get());
    if (status.code != heif_error_Ok) { *error = QString::fromUtf8(status.message); return {}; }
    std::unique_ptr<heif_image, decltype(&heif_image_release)> image(imageValue, heif_image_release);
    int stride = 0;
    const uint8_t *plane = heif_image_get_plane_readonly(image.get(), heif_channel_interleaved, &stride);
    const int decodedWidth = heif_image_get_width(image.get(), heif_channel_interleaved);
    const int decodedHeight = heif_image_get_height(image.get(), heif_channel_interleaved);
    if (!plane || decodedWidth <= 0 || decodedHeight <= 0 || qint64(decodedWidth) * decodedHeight > pixelLimit
        || stride < qint64(decodedWidth) * 4) {
        *error = QStringLiteral("Invalid decoded HEIF plane"); return {};
    }
    QImage result = QImage(plane, decodedWidth, decodedHeight, stride, QImage::Format_RGBA8888).copy();
    const size_t profileSize = heif_image_handle_get_raw_color_profile_size(handle.get());
    if (profileSize > 0 && profileSize <= 4 * 1024 * 1024) {
        QByteArray profile(qsizetype(profileSize), Qt::Uninitialized);
        if (heif_image_handle_get_raw_color_profile(handle.get(), profile.data()).code == heif_error_Ok)
            result.setColorSpace(QColorSpace::fromIccProfile(profile));
    }
    return result;
}

QImage decodeRaw(const QString &path, QString *error, const ExtractionOptions *options, qint64 pixelLimit)
{
    QByteArray bytes = readBoundedImage(path, error);
    if (bytes.isEmpty()) return {};
    // LibRaw embeds hundreds of KiB of decoder state. macOS worker threads may
    // have only a 512 KiB stack, even when this branch is inlined with PNG code.
    auto decoder = std::make_unique<LibRaw>();
    if (options) decoder->set_progress_handler([](void *context, LibRaw_progress, int, int) {
        return cancelled(*static_cast<const ExtractionOptions *>(context)) ? 1 : 0;
    }, const_cast<ExtractionOptions *>(options));
    decoder->imgdata.params.half_size = 1;
    decoder->imgdata.params.output_bps = 8;
    decoder->imgdata.params.output_color = 1;
    decoder->imgdata.params.use_camera_wb = 1;
    int status = decoder->open_buffer(bytes.data(), size_t(bytes.size()));
    if (status != LIBRAW_SUCCESS) { *error = QString::fromLatin1(libraw_strerror(status)); return {}; }
    if (qint64(decoder->imgdata.sizes.raw_width) * decoder->imgdata.sizes.raw_height > pixelLimit) {
        *error = QStringLiteral("RAW dimensions exceed the decoder limit"); return {};
    }
    status = decoder->unpack();
    if (status == LIBRAW_SUCCESS) status = decoder->dcraw_process();
    if (status != LIBRAW_SUCCESS) { *error = QString::fromLatin1(libraw_strerror(status)); return {}; }
    libraw_processed_image_t *rawImage = decoder->dcraw_make_mem_image(&status);
    std::unique_ptr<libraw_processed_image_t, decltype(&LibRaw::dcraw_clear_mem)> image(rawImage, &LibRaw::dcraw_clear_mem);
    if (!image || status != LIBRAW_SUCCESS) { *error = QString::fromLatin1(libraw_strerror(status)); return {}; }
    if (image->type != LIBRAW_IMAGE_BITMAP || image->bits != 8 || image->colors != 3
        || qint64(image->width) * image->height > pixelLimit
        || image->data_size < qint64(image->width) * image->height * 3) {
        *error = QStringLiteral("RAW decoder returned an unsupported pixel layout"); return {};
    }
    QImage result = QImage(image->data, image->width, image->height, image->width * 3, QImage::Format_RGB888).copy();
    result.setColorSpace(QColorSpace::SRgb);
    return result;
}

QImage decodeImage(const QString &path, QString *error, const ExtractionOptions *options = nullptr,
                   qint64 pixelLimit = maximumPixels)
{
    const QString suffix = QFileInfo(path).suffix().toLower();
    QImage image;
    if (suffix == QStringLiteral("heic") || suffix == QStringLiteral("heif") || suffix == QStringLiteral("avif"))
        image = decodeHeif(path, error, options, pixelLimit);
    else if (rawFormats.contains(suffix)) image = decodeRaw(path, error, options, pixelLimit);
    else {
    QImageReader reader(path);
    reader.setAutoTransform(true);
    const QSize size = reader.size();
    if (!size.isValid() || qint64(size.width()) * size.height() > pixelLimit) {
        *error = QStringLiteral("Invalid image dimensions or pixel count exceeds %1 million").arg(pixelLimit / 1'000'000); return {};
    }
    // OCR keeps a generous resolution. Qt's decoder applies source orientation.
    if (qMax(size.width(), size.height()) > 6000) reader.setScaledSize(size.scaled(6000, 6000, Qt::KeepAspectRatio));
    image = reader.read();
    if (image.isNull()) *error = reader.errorString();
    }
    if (!image.isNull() && image.colorSpace().isValid())
        image = image.convertedToColorSpace(QColorSpace::SRgb);
    return image;
}
} // namespace

bool supportsImage(const QString &suffix)
{
    static const QSet<QString> common = {QStringLiteral("png"), QStringLiteral("jpg"), QStringLiteral("jpeg"),
        QStringLiteral("webp"), QStringLiteral("bmp"), QStringLiteral("gif"), QStringLiteral("tif"),
        QStringLiteral("tiff"), QStringLiteral("ico"), QStringLiteral("ppm"), QStringLiteral("pgm"),
        QStringLiteral("pbm"), QStringLiteral("heic"), QStringLiteral("heif"), QStringLiteral("avif")};
    return common.contains(suffix.toLower()) || rawFormats.contains(suffix.toLower());
}

QImage loadPreviewImage(const QString &path, int maxPixels, QString *error, std::function<bool()> cancel)
{
    QString localError;
    if (!error) error = &localError;
    error->clear();
    if (path.isEmpty() || !QFileInfo(path).isAbsolute() || maxPixels < 1 || maxPixels > 8192) {
        *error = QStringLiteral("Invalid preview path or maximum dimension");
        return {};
    }
    ExtractionOptions options; options.cancelled = std::move(cancel);
    if (cancelled(options)) { *error = QStringLiteral("Preview cancelled"); return {}; }
    QImage image = decodeImage(path, error, &options, 64'000'000);
    if (cancelled(options)) { *error = QStringLiteral("Preview cancelled"); return {}; }
    if (!image.isNull() && qMax(image.width(), image.height()) > maxPixels)
        image = image.scaled(maxPixels, maxPixels, Qt::KeepAspectRatio, Qt::SmoothTransformation);
    return image;
}

QString recognizeText(const QImage &source, const ExtractionOptions &options, QString *error)
{
    QString localError;
    if (!error) error = &localError;
    error->clear();
    if (!options.enableOcr || source.isNull() || cancelled(options)) return {};
    QString directory = options.tessdataDir;
    if (directory.isEmpty()) {
        const QDir applicationDirectory(QCoreApplication::applicationDirPath());
        QStringList candidates{QStringLiteral("../share/qcopilots-search/tessdata")};
#ifdef Q_OS_MACOS
        candidates.prepend(QStringLiteral("../Resources/qcopilots-search/tessdata"));
#endif
        directory = applicationDirectory.filePath(candidates.first());
        for (const QString &candidate : candidates) {
            const QString path = applicationDirectory.filePath(candidate);
            if (QFileInfo(path).isDir()) { directory = path; break; }
        }
    }
    for (const QString &language : {QStringLiteral("eng"), QStringLiteral("chi_sim"), QStringLiteral("chi_tra")}) {
        if (!QFileInfo::exists(QDir(directory).filePath(language + QStringLiteral(".traineddata")))) {
            *error = QStringLiteral("OCR language data missing: %1 in %2").arg(language, directory);
            return {};
        }
    }
    tesseract::TessBaseAPI api;
    const QByteArray dataPath = directory.toUtf8();
    const auto readData = [](const char *filename, std::vector<char> *output) {
        QFile file(QString::fromUtf8(filename));
        if (!file.open(QIODevice::ReadOnly) || file.size() > 64 * 1024 * 1024) return false;
        const QByteArray bytes = file.readAll();
        if (bytes.size() != file.size()) return false;
        output->assign(bytes.cbegin(), bytes.cend());
        return true;
    };
    // The FileReader overload preserves Unicode paths on Windows rather than
    // relying on a narrow stdio pathname interpreted in the local codepage.
    if (api.Init(dataPath.constData(), 0, "eng+chi_sim+chi_tra", tesseract::OEM_LSTM_ONLY,
                 nullptr, 0, nullptr, nullptr, false, readData) != 0) {
        *error = QStringLiteral("Could not initialize English and Chinese OCR"); return {};
    }
    std::vector<std::string> languages;
    api.GetLoadedLanguagesAsVector(&languages);
    for (const auto *language : {"eng", "chi_sim", "chi_tra"}) {
        if (std::find(languages.begin(), languages.end(), language) == languages.end()) {
            *error = QStringLiteral("OCR failed to load required language: %1").arg(QString::fromLatin1(language));
            return {};
        }
    }
    const QImage bounded = qMax(source.width(), source.height()) > 6000
        ? source.scaled(6000, 6000, Qt::KeepAspectRatio, Qt::SmoothTransformation) : source;
    const QImage image = opaqueOnWhite(bounded).convertToFormat(QImage::Format_Grayscale8);
    if (image.isNull()) { *error = QStringLiteral("Cannot allocate OCR image"); return {}; }
    tesseract::ETEXT_DESC monitor;
    monitor.set_deadline_msecs(120'000);
    monitor.cancel = [](void *context, int) { return cancelled(*static_cast<const ExtractionOptions *>(context)); };
    monitor.cancel_this = const_cast<ExtractionOptions *>(&options);
    const auto recognize = [&](const QImage &input, tesseract::PageSegMode mode, QString *text, int *confidence) {
        if (cancelled(options) || monitor.deadline_exceeded()) {
            *error = cancelled(options) ? QStringLiteral("OCR cancelled") : QStringLiteral("OCR exceeded its deadline");
            return false;
        }
        api.SetPageSegMode(mode);
        api.SetImage(input.constBits(), input.width(), input.height(), 1, int(input.bytesPerLine()));
        if (api.Recognize(&monitor) != 0 || cancelled(options) || monitor.deadline_exceeded()) {
            *error = cancelled(options) ? QStringLiteral("OCR cancelled") : QStringLiteral("OCR failed or exceeded its deadline");
            return false;
        }
        std::unique_ptr<char[]> buffer(api.GetUTF8Text());
        if (!buffer) { *error = QStringLiteral("OCR produced no text buffer"); return false; }
        *text = QString::fromUtf8(buffer.get()).trimmed();
        *confidence = api.MeanTextConf();
        return true;
    };
    QString text;
    int confidence = 0;
    if (!recognize(image, tesseract::PSM_AUTO, &text, &confidence)) return {};
    const bool largeLine = largeSingleLine(image);
    // Automatic page layout can mistake a short CJK sign for Latin fragments
    // or discard it entirely. A bounded block-layout retry recovers those
    // images without changing confident recognition of multi-column pages.
    if (text.isEmpty() || confidence < 70 || largeLine) {
        QString alternative;
        int alternativeConfidence = 0;
        if (!recognize(image, tesseract::PSM_SINGLE_BLOCK, &alternative, &alternativeConfidence)) return {};
        if (!alternative.isEmpty() && (text.isEmpty() || alternativeConfidence > confidence)) {
            text = alternative; confidence = alternativeConfidence;
        }
        // Large glyphs can still normalize poorly with high LSTM confidence.
        // A confirmed single large ink band gets the same bounded scale retry
        // as an uncertain result. Multiple rows keep automatic page layout.
        if ((text.isEmpty() || confidence < 70 || largeLine) && qMin(image.width(), image.height()) >= 64) {
            const QImage smaller = image.scaled(qMax(1, image.width() / 2), qMax(1, image.height() / 2),
                Qt::KeepAspectRatio, Qt::SmoothTransformation);
            if (smaller.isNull()) { *error = QStringLiteral("Cannot allocate OCR retry image"); return {}; }
            if (!recognize(smaller, tesseract::PSM_SINGLE_BLOCK, &alternative, &alternativeConfidence)) return {};
            const auto currentLetters = lettersAndNumbers(text);
            const auto alternativeLetters = lettersAndNumbers(alternative);
            const bool completesCurrentLine = largeLine && alternativeConfidence == confidence
                && !currentLetters.isEmpty() && alternativeLetters.size() > currentLetters.size()
                && alternativeLetters.contains(currentLetters);
            if (!alternative.isEmpty() && (text.isEmpty() || alternativeConfidence > confidence || completesCurrentLine))
                text = alternative;
        }
    }
    return text;
}

ExtractionResult extractImage(const QString &path, const ExtractionOptions &options)
{
    ExtractionResult result;
    result.state = QStringLiteral("complete");
    result.next = options.start;
    if (options.start > 0) return result;
    QString error;
    if (!prepareDirectory(options.tempDir, &error)) return failure(options, error);
    QImage image = decodeImage(path, &error, &options);
    if (image.isNull()) {
        result = failure(options, error);
        if (cancelled(options)) result.state = QStringLiteral("cancelled");
        return result;
    }
    if (cancelled(options)) {
        result.complete = false; result.state = QStringLiteral("cancelled"); return result;
    }
    const QString text = recognizeText(image, options, &error);
    if (!error.isEmpty()) return failure(options, error);
    if (cancelled(options)) { result.complete = false; result.state = QStringLiteral("cancelled"); return result; }
    const int maxPixel = Extractor::isScreenshotPath(path) ? 1536 : 1024;
    const QImage prepared = opaqueOnWhite(qMax(image.width(), image.height()) > maxPixel
        ? image.scaled(maxPixel, maxPixel, Qt::KeepAspectRatio, Qt::SmoothTransformation) : image);
    if (prepared.isNull()) return failure(options, QStringLiteral("Cannot allocate prepared image"));
    const QString media = writeImage(prepared, options.tempDir, &error);
    if (media.isEmpty()) return failure(options, error);
    Segment segment;
    segment.kind = QStringLiteral("image");
    segment.modality = QStringLiteral("image");
    segment.imageBudget = Extractor::isScreenshotPath(path) ? 280 : 140;
    segment.text = text;
    segment.mediaPath = media;
    result.segments.append(segment);
    result.next = 1;
    return result;
}

ExtractionResult extractPdf(const QString &path, const ExtractionOptions &options)
{
    QString error;
    if (!prepareDirectory(options.tempDir, &error)) return failure(options, error);
    QPdfDocument document;
    const auto loadError = document.load(path);
    if (loadError != QPdfDocument::Error::None) {
        const QString message = loadError == QPdfDocument::Error::IncorrectPassword
            ? QStringLiteral("PDF is password-protected") : QStringLiteral("PDF cannot be opened (error %1)").arg(int(loadError));
        return failure(options, message);
    }
    const int pages = document.pageCount();
    if (pages < 1) return failure(options, QStringLiteral("PDF contains no readable pages"));
    ExtractionResult result;
    result.next = options.start;
    const int limit = qMin(pages, 5000);
    const int stop = qMin(limit, options.start + options.maxSegments);
    const QString title = document.metaData(QPdfDocument::MetaDataField::Title).toString();
    for (int index = options.start; index < stop; ++index) {
        if (cancelled(options)) {
            result.state = QStringLiteral("cancelled"); result.complete = false; return result;
        }
        Segment segment;
        segment.kind = QStringLiteral("pdf");
        segment.imageBudget = 280;
        segment.page = index + 1;
        segment.title = title;
        // PDF font ToUnicode tables can map visible CJK letters to compatibility
        // radicals (for example U+2F4A instead of 木). Canonicalize these and
        // ligatures before both semantic encoding and the short-page decision.
        segment.text = document.getAllText(index).text().normalized(QString::NormalizationForm_KC).trimmed();
        segment.modality = QStringLiteral("text");
        if (segment.text.size() < 200 && !meaningfulNativePdfText(segment.text)) {
            const QSizeF points = document.pagePointSize(index);
            if (!points.isValid() || points.isEmpty()) return failure(options, QStringLiteral("Invalid PDF page size"));
            const QSize size = points.toSize().scaled(2000, 2000, Qt::KeepAspectRatio);
            const QImage rendered = document.render(index, size);
            if (rendered.isNull()) return failure(options, QStringLiteral("PDF page %1 cannot be rendered").arg(index + 1));
            // PDFium may return transparent paper with opaque black glyphs. An
            // RGB encoder dropping alpha turns such a page entirely black, so
            // composite onto actual white paper before OCR and model input.
            const QImage image = opaqueOnWhite(rendered);
            if (image.isNull()) return failure(options, QStringLiteral("Cannot allocate PDF page image"));
            const QString recognized = recognizeText(image, options, &error);
            if (!error.isEmpty()) return failure(options, QStringLiteral("PDF page %1: %2").arg(index + 1).arg(error));
            if (recognized.size() > segment.text.size()) segment.text = recognized;
            segment.mediaPath = writeImage(image, options.tempDir, &error);
            if (segment.mediaPath.isEmpty()) return failure(options, error);
            segment.modality = QStringLiteral("image");
        }
        result.segments.append(segment);
        result.next = index + 1;
    }
    result.complete = result.next >= pages;
    result.state = result.complete ? QStringLiteral("complete") : QStringLiteral("partial");
    if (pages > limit && result.next >= limit) {
        result.state = QStringLiteral("failed");
        result.error = QStringLiteral("PDF exceeds the 5000-page extraction limit");
    }
    return result;
}

} // namespace qcopilots_search
