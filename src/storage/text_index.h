#pragma once
#include <QString>

namespace qcopilots_search {
struct IndexedText {
  QString terms;
  QString han;
};
IndexedText tokenizeForIndex(const QString &text);
QString keywordMatchExpression(const QString &query);
QString chineseDictionaryPath();
} // namespace qcopilots_search
