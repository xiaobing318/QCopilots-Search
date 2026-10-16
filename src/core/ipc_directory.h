#pragma once

#include <QString>

namespace qcopilots_search::detail {

// Internal filesystem contract, also exercised against isolated test fixtures.
// Existing directories are validated, never chmod'ed or followed through links.
bool preparePrivateIpcDirectory(const QString &directory, bool create, QString *error = nullptr);

} // namespace qcopilots_search::detail
