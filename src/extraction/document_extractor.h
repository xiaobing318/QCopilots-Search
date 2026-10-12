#pragma once

#include "extractor.h"

namespace qcopilots_search {
bool supportsDocument(QString suffix);
bool supportsCode(QString suffix, QString filename);
ExtractionResult extractDocument(QString path, const ExtractionOptions &options);
} // namespace qcopilots_search
