#pragma once

#include <QString>
#include <functional>

namespace qcopilots_search::legacy_doc {

// Called in the isolated extraction worker. Returns UTF-16 text from the body,
// tables, headers, footers, footnotes, endnotes and comments. The caller retains
// the source-file size limit and chunks the returned text into search segments.
QString extract(const QString &path, QString *error = nullptr,
                qsizetype maxCharacters = 64 * 1024 * 1024,
                const std::function<bool()> &cancelled = {});

} // namespace qcopilots_search::legacy_doc
