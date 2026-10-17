/*
 * Adapted from DigUp Sources/LlamaRuntime/ImageSizing.swift.
 * Copyright (c) 2026 Abdur Rahim
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
#include "ImageSizing.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

namespace qcopilots_search {
namespace {
constexpr int precision = 22;
constexpr std::uint32_t rounding = 1U << (precision - 1);

std::optional<qsizetype> rgbSize(int width, int height)
{
    if (width <= 0 || height <= 0)
        return std::nullopt;
    const auto pixels = static_cast<std::uint64_t>(width) * static_cast<unsigned>(height);
    if (pixels > static_cast<std::uint64_t>(std::numeric_limits<qsizetype>::max()) / 3)
        return std::nullopt;
    return static_cast<qsizetype>(pixels * 3);
}

double cubicFilter(double value)
{
    const double x = std::abs(value);
    constexpr double a = -0.5;
    if (x < 1)
        return ((a + 2) * x - (a + 3)) * x * x + 1;
    if (x < 2)
        return (((x - 5) * x + 8) * x - 4) * a;
    return 0;
}

struct Coefficients {
    std::vector<int> bounds;
    std::vector<std::int32_t> weights;
    int size = 0;
};

std::optional<Coefficients> coefficients(int inSize, int outSize)
{
    const double scale = static_cast<double>(inSize) / outSize;
    const double filterScale = std::max(scale, 1.0);
    const double support = 2 * filterScale;
    const double kernelSize = std::ceil(support) * 2 + 1;
    if (kernelSize > std::numeric_limits<int>::max())
        return std::nullopt;

    Coefficients result;
    result.size = static_cast<int>(kernelSize);
    const auto outCount = static_cast<std::size_t>(outSize);
    if (outCount > result.weights.max_size() / static_cast<std::size_t>(result.size)
        || outCount > result.bounds.max_size() / 2)
        return std::nullopt;
    result.bounds.resize(outCount * 2);
    result.weights.resize(outCount * result.size);
    std::vector<double> row(static_cast<std::size_t>(result.size));

    for (int out = 0; out < outSize; ++out) {
        const double center = (static_cast<double>(out) + 0.5) * scale;
        // Convert only after clipping to avoid overflowing int for huge input sizes.
        // Truncation toward zero here (not floor) matches Pillow and the Swift port.
        const int first = static_cast<int>(std::max(center - support + 0.5, 0.0));
        const int count = static_cast<int>(std::min(center + support + 0.5,
                                                    static_cast<double>(inSize))) - first;
        double total = 0;
        for (int x = 0; x < result.size; ++x) {
            row[x] = x < count
                ? cubicFilter((static_cast<double>(x + first) - center + 0.5) / filterScale)
                : 0;
            total += row[x];
        }
        for (int x = 0; x < result.size; ++x) {
            const double weight = x < count && total != 0 ? row[x] / total : row[x];
            result.weights[static_cast<std::size_t>(out) * result.size + x]
                = static_cast<std::int32_t>(weight * (1U << precision)
                                            + (weight < 0 ? -0.5 : 0.5));
        }
        result.bounds[static_cast<std::size_t>(out) * 2] = first;
        result.bounds[static_cast<std::size_t>(out) * 2 + 1] = count;
    }
    return result;
}

unsigned char clip(std::uint32_t value)
{
    // Unsigned accumulation gives the Swift implementation's defined wraparound.
    // C++20 signed right shift is arithmetic, as required by Pillow's clipping.
    return static_cast<unsigned char>(std::clamp(std::bit_cast<std::int32_t>(value) >> precision,
                                                0, 255));
}

QByteArray horizontal(const unsigned char* source, int inWidth, int rows, int outWidth)
{
    const auto byteCount = rgbSize(outWidth, rows);
    if (!byteCount)
        return {};
    const auto coeff = coefficients(inWidth, outWidth);
    if (!coeff)
        return {};
    QByteArray output(*byteCount, Qt::Uninitialized);
    auto* destination = reinterpret_cast<unsigned char*>(output.data());
    for (int y = 0; y < rows; ++y) {
        for (int x = 0; x < outWidth; ++x) {
            const auto bound = static_cast<std::size_t>(x) * 2;
            const auto* pixel = source + (static_cast<qsizetype>(y) * inWidth + coeff->bounds[bound]) * 3;
            std::uint32_t r = rounding, g = rounding, b = rounding;
            for (int k = 0; k < coeff->bounds[bound + 1]; ++k) {
                const auto weight = static_cast<std::uint32_t>(
                    coeff->weights[static_cast<std::size_t>(x) * coeff->size + k]);
                r += pixel[0] * weight;
                g += pixel[1] * weight;
                b += pixel[2] * weight;
                pixel += 3;
            }
            const auto offset = (static_cast<qsizetype>(y) * outWidth + x) * 3;
            destination[offset] = clip(r);
            destination[offset + 1] = clip(g);
            destination[offset + 2] = clip(b);
        }
    }
    return output;
}

QByteArray vertical(const unsigned char* source, int inHeight, int rowWidth, int outHeight)
{
    const auto byteCount = rgbSize(rowWidth, outHeight);
    if (!byteCount)
        return {};
    const auto coeff = coefficients(inHeight, outHeight);
    if (!coeff)
        return {};
    const auto stride = static_cast<qsizetype>(rowWidth) * 3;
    QByteArray output(*byteCount, Qt::Uninitialized);
    auto* destination = reinterpret_cast<unsigned char*>(output.data());
    std::vector<std::uint32_t> sums(static_cast<std::size_t>(stride));
    for (int y = 0; y < outHeight; ++y) {
        std::fill(sums.begin(), sums.end(), rounding);
        const auto bound = static_cast<std::size_t>(y) * 2;
        for (int k = 0; k < coeff->bounds[bound + 1]; ++k) {
            const auto* row = source + (static_cast<qsizetype>(coeff->bounds[bound]) + k) * stride;
            const auto weight = static_cast<std::uint32_t>(
                coeff->weights[static_cast<std::size_t>(y) * coeff->size + k]);
            for (qsizetype i = 0; i < stride; ++i)
                sums[static_cast<std::size_t>(i)] += row[i] * weight;
        }
        for (qsizetype i = 0; i < stride; ++i)
            destination[static_cast<qsizetype>(y) * stride + i] = clip(sums[static_cast<std::size_t>(i)]);
    }
    return output;
}
} // namespace

std::optional<QSize> ImageSizing::targetSize(int height, int width, int budget)
{
    if (height <= 0 || width <= 0 || budget <= 0)
        return std::nullopt;
    constexpr int side = 48; // 3 x 3 pooling of 16-pixel patches.
    const double factor = std::sqrt(static_cast<double>(budget) * side * side
                                   / (static_cast<double>(height) * width));
    auto targetHeight = static_cast<std::int64_t>(std::floor(factor * height / side)) * side;
    auto targetWidth = static_cast<std::int64_t>(std::floor(factor * width / side)) * side;
    if (targetHeight == 0 && targetWidth == 0)
        return std::nullopt;
    const auto maxSide = static_cast<std::int64_t>(budget) * side;
    if (targetHeight == 0) {
        targetHeight = side;
        targetWidth = std::min(static_cast<std::int64_t>(width / height) * side, maxSide);
    } else if (targetWidth == 0) {
        targetWidth = side;
        targetHeight = std::min(static_cast<std::int64_t>(height / width) * side, maxSide);
    }
    if (targetWidth > std::numeric_limits<int>::max()
        || targetHeight > std::numeric_limits<int>::max())
        return std::nullopt;
    return QSize(static_cast<int>(targetWidth), static_cast<int>(targetHeight));
}

int ImageSizing::qwenTokenBudget(int budget)
{
    switch (budget) {
    case 70: return 160;
    case 140: return 320;
    case 280: return 640;
    case 560: case 1120: return 1280;
    default: return 0;
    }
}

std::optional<QSize> ImageSizing::qwenTargetSize(int height, int width, int budget)
{
    const int tokens = qwenTokenBudget(budget);
    if (height <= 0 || width <= 0 || !tokens
        || double(std::max(height, width)) / std::min(height, width) > 200.0)
        return std::nullopt;
    constexpr int side = 32;
    constexpr double minPixels = 4.0 * side * side;
    const double maxPixels = double(tokens) * side * side;
    // Match Python round(), including exact half-grid ties, independently of
    // the process floating-point rounding mode.
    const auto roundGrid = [](int value) -> qint64 {
        const qint64 units = value / side;
        const int remainder = value % side;
        return (units + (remainder > side / 2 || (remainder == side / 2 && (units & 1)))) * side;
    };
    qint64 h = std::max<qint64>(side, roundGrid(height));
    qint64 w = std::max<qint64>(side, roundGrid(width));
    const double pixels = double(height) * width;
    if (double(h) * w > maxPixels) {
        const double scale = std::sqrt(pixels / maxPixels);
        h = qint64(std::floor(height / scale / side)) * side;
        w = qint64(std::floor(width / scale / side)) * side;
    } else if (double(h) * w < minPixels) {
        const double scale = std::sqrt(minPixels / pixels);
        h = qint64(std::ceil(height * scale / side)) * side;
        w = qint64(std::ceil(width * scale / side)) * side;
    }
    if (h <= 0 || w <= 0 || h > std::numeric_limits<int>::max()
        || w > std::numeric_limits<int>::max() || double(h) * w > maxPixels)
        return std::nullopt;
    return QSize(int(w), int(h));
}

QByteArray ImageSizing::resizeBicubic(const unsigned char* source, int width, int height,
                                    int toWidth, int toHeight)
{
    const auto sourceSize = rgbSize(width, height);
    if (!source || !sourceSize || !rgbSize(toWidth, toHeight))
        return {};
    if (toWidth != width) {
        auto pass = horizontal(source, width, height, toWidth);
        if (pass.isEmpty() || toHeight == height)
            return pass;
        return vertical(reinterpret_cast<const unsigned char*>(pass.constData()), height, toWidth, toHeight);
    }
    if (toHeight != height)
        return vertical(source, height, width, toHeight);
    return QByteArray(reinterpret_cast<const char*>(source), *sourceSize);
}

} // namespace qcopilots_search
