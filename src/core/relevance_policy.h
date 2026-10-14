#pragma once

#include <QJsonObject>
#include <QString>

namespace qcopilots_search {

// Exact cosine is a retrieval signal, never a probability or the public RRF
// score. This gate is applied to each segment before choosing a file's match.
QString semanticPolicyModality(const QString &modality, bool code);
double semanticMinimumCosine(const QString &modality, bool code) noexcept;
bool acceptsSemanticCandidate(const QString &modality, bool code, double cosine) noexcept;
QJsonObject semanticRelevancePolicy();

} // namespace qcopilots_search
