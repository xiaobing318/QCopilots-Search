#include "relevance_policy.h"
#include "relevance_policy_data.h"
#include "pipeline_manifest.h"

#include <cmath>

namespace qcopilots_search {
namespace {
bool calibratedForCurrentModel()
{
    static const bool matches = relevance_data::calibrated
        && QString::fromLatin1(relevance_data::modelFingerprint) == embeddingPipelineFingerprint();
    return matches;
}
}

QString semanticPolicyModality(const QString &modality, bool code)
{
    if (code)
        return QStringLiteral("code");
    if (modality == QLatin1String("image") || modality == QLatin1String("video"))
        return QStringLiteral("image");
    if (modality == QLatin1String("audio"))
        return QStringLiteral("audio");
    return QStringLiteral("text");
}

double semanticMinimumCosine(const QString &modality, bool code) noexcept
{
    if (code)
        return relevance_data::codeMinimum;
    if (modality == QLatin1String("image") || modality == QLatin1String("video"))
        return relevance_data::imageMinimum;
    if (modality == QLatin1String("audio"))
        return relevance_data::audioMinimum;
    return relevance_data::textMinimum;
}

bool acceptsSemanticCandidate(const QString &modality, bool code, double cosine) noexcept
{
    return calibratedForCurrentModel() && std::isfinite(cosine)
        && cosine >= semanticMinimumCosine(modality, code);
}

QJsonObject semanticRelevancePolicy()
{
    return {{"version", QString::fromLatin1(relevance_data::version)},
            {"calibrated", calibratedForCurrentModel()},
            {"modelFingerprint", QString::fromLatin1(relevance_data::modelFingerprint)},
            {"corpusSha256", QString::fromLatin1(relevance_data::corpusSha256)},
            {"rawExportSha256", QString::fromLatin1(relevance_data::rawExportSha256)},
            {"calibrationReportSha256", QString::fromLatin1(relevance_data::calibrationReportSha256)},
            {"thresholds", QJsonObject{{"text", relevance_data::textMinimum},
                                       {"image", relevance_data::imageMinimum},
                                       {"audio", relevance_data::audioMinimum},
                                       {"code", relevance_data::codeMinimum}}}};
}

} // namespace qcopilots_search
