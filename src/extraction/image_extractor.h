#pragma once
#include "extractor.h"
#include <QImage>

namespace qcopilots_search {
ExtractionResult extractImage(const QString &path, const ExtractionOptions &options);
ExtractionResult extractPdf(const QString &path, const ExtractionOptions &options);
bool supportsImage(const QString &suffix);
// Shares the validated local image decoders with the preview UI. The caller must
// run this off the GUI thread for large camera files. maxPixels is longest side.
QImage loadPreviewImage(const QString &path, int maxPixels, QString *error = nullptr,
                       std::function<bool()> cancelled = {});
QString recognizeText(const QImage &image, const ExtractionOptions &options, QString *error);
} // namespace qcopilots_search
