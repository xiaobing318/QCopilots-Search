#pragma once

#include "core/types.h"
#include <cmath>
#include <stdexcept>

namespace qcopilots_search::embedding_math {

// Normalize query vectors once in float precision before comparing them with
// the persisted FP16 vectors. The same arithmetic is used by exact and ANN
// candidate scoring.
inline QVector<float> normalized(const QVector<float> &values)
{
    if (values.size() != embeddingDimensions)
        throw std::runtime_error("Embedding must contain exactly 768 values");
    double squared = 0;
    for (float value : values) {
        if (!std::isfinite(value))
            throw std::runtime_error("Embedding contains a non-finite value");
        squared += double(value) * value;
    }
    if (!(squared > 1.e-20))
        throw std::runtime_error("Embedding has zero norm");
    auto result = values;
    const double scale = 1. / std::sqrt(squared);
    for (auto &value : result)
        value = float(value * scale);
    return result;
}

// The query must already be normalized() once. The stored vector is the
// decoded persisted FP16 vector, whose norm is corrected in double precision.
inline double cosine(const QVector<float> &normalizedQuery,
                     const QVector<float> &storedVector)
{
    if (normalizedQuery.size() != storedVector.size() || normalizedQuery.isEmpty())
        return -1;
    double dot = 0, norm = 0;
    for (qsizetype i = 0; i < normalizedQuery.size(); ++i) {
        dot += double(normalizedQuery[i]) * storedVector[i];
        norm += double(storedVector[i]) * storedVector[i];
    }
    return norm > 0 ? dot / std::sqrt(norm) : -1;
}

} // namespace qcopilots_search::embedding_math
