#pragma once

#include "extractor.h"

namespace qcopilots_search {

// Cursor counts emitted (non-silent, deduplicated) pieces. Video pieces precede
// the audio track. Replaying a cursor is deterministic for the same file version.
ExtractionResult extractMedia(const QString &path, const ExtractionOptions &options, bool video);
bool supportsAudio(const QString &pathOrExtension);
bool supportsVideo(const QString &pathOrExtension);

// Reports the locked runtime's availability without decoding a file.
QString mediaBackendError();

} // namespace qcopilots_search
