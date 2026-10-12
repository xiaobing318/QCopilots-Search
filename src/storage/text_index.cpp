#include "text_index.h"
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QRegularExpression>
#include <QStringList>
#include <cppjieba/Jieba.hpp>
#include <memory>
#include <mutex>

namespace qcopilots_search {
namespace {
bool isHan(char32_t c) {
  return (c >= 0x3400 && c <= 0x9fff) || (c >= 0x20000 && c <= 0x323af);
}
QStringList hanRuns(const QString &text) {
  QStringList runs;
  QStringList current;
  for (const auto c : text.toUcs4()) {
    if (isHan(c))
      current.append(QStringLiteral("u%1").arg(uint(c), 0, 16));
    else if (!current.isEmpty()) {
      runs.append(current.join(u' '));
      current.clear();
    }
  }
  if (!current.isEmpty())
    runs.append(current.join(u' '));
  return runs;
}
QString quote(const QString &value) {
  auto escaped = value;
  escaped.replace(u'"', QStringLiteral("\"\""));
  return u'"' + escaped + u'"';
}
std::shared_ptr<cppjieba::Jieba> dictionary() {
  static std::mutex mutex;
  static std::shared_ptr<cppjieba::Jieba> jieba;
  static bool initialized = false;
  std::lock_guard lock(mutex);
  if (initialized)
    return jieba;
  initialized = true;
  const auto dir = chineseDictionaryPath();
  if (dir.isEmpty())
    return {};
  {
    auto filename = [&](const char *name) {
      return QDir(dir).filePath(QString::fromLatin1(name)).toStdString();
    };
    jieba = std::make_shared<cppjieba::Jieba>(
        filename("jieba.dict.utf8"), filename("hmm_model.utf8"),
        filename("user.dict.utf8"), filename("idf.utf8"),
        filename("stop_words.utf8"));
  }
  return jieba;
}
} // namespace

QString chineseDictionaryPath() {
  QStringList candidates;
  const auto explicitDir =
      qEnvironmentVariable("QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR");
  if (!explicitDir.isEmpty())
    candidates.append(explicitDir);
  QDir app(QCoreApplication::applicationDirPath());
  candidates << app.filePath("../share/qcopilots-search/dict")
             << app.filePath("../Resources/qcopilots-search/dict")
             << app.filePath("dict");
  for (const auto &dir : candidates) {
    bool valid = true;
    for (const auto *file : {"jieba.dict.utf8", "hmm_model.utf8",
                             "user.dict.utf8", "idf.utf8", "stop_words.utf8"})
      valid =
          valid &&
          QFileInfo(QDir(dir).filePath(QString::fromLatin1(file))).isReadable();
    if (valid)
      return QDir(dir).absolutePath();
  }
  return {};
}

IndexedText tokenizeForIndex(const QString &text) {
  const auto normalized =
      text.normalized(QString::NormalizationForm_KC).toCaseFolded();
  QStringList terms;
  static const QRegularExpression word(QStringLiteral("[\\p{L}\\p{N}_]+"));
  auto matches = word.globalMatch(normalized);
  while (matches.hasNext())
    terms.append(matches.next().captured());
  if (auto jieba = dictionary()) {
    std::vector<std::string> words;
    jieba->CutForSearch(normalized.toUtf8().toStdString(), words, true);
    for (const auto &part : words) {
      const auto token =
          QString::fromUtf8(part.data(), qsizetype(part.size())).trimmed();
      if (!token.isEmpty() && word.match(token).hasMatch())
        terms.append(token);
    }
  }
  return {terms.join(u' '), hanRuns(normalized).join(QStringLiteral(" x "))};
}

QString keywordMatchExpression(const QString &query) {
  const auto normalized =
      query.normalized(QString::NormalizationForm_KC).toCaseFolded().left(2048);
  QStringList pieces;
  // Character phrases retain positions, so an unknown Chinese name or a single
  // character remains searchable even when it is absent from the dictionary.
  for (const auto &run : hanRuns(normalized))
    pieces.append(QStringLiteral("han:") + quote(run));
  static const QRegularExpression latin(QStringLiteral("[\\p{L}\\p{N}_]+"));
  auto matches = latin.globalMatch(normalized);
  while (matches.hasNext()) {
    const auto token = matches.next().captured();
    bool containsHan = false;
    for (auto c : token.toUcs4())
      containsHan = containsHan || isHan(c);
    if (!containsHan)
      pieces.append(QStringLiteral("terms:") + quote(token));
  }
  // Every keyword must be present. Broad OR matches on ordinary words would
  // give unrelated text files an extra RRF vote over strong image/audio hits
  // that have no corresponding text. Natural-language recall remains the
  // semantic branch's responsibility when a model is available.
  return pieces.join(QStringLiteral(" AND "));
}
} // namespace qcopilots_search
