// Adapted from DigUp, Copyright (c) 2026 Abdur Rahim. SPDX-License-Identifier: MIT
#pragma once

#include <QByteArray>
#include <QSize>

#include <optional>

namespace qcopilots_search {

// Match the reference Gemma image processor and Pillow's packed RGB resize.
class ImageSizing final {
public:
    // Input order follows the reference. QSize stores the result as width, height.
    // Invalid dimensions, budgets or unrepresentable results return nullopt.
    static std::optional<QSize> targetSize(int height, int width, int budget);
    static int qwenTokenBudget(int budget);
    static std::optional<QSize> qwenTargetSize(int height, int width, int budget);

    // source must contain width * height * 3 bytes of packed RGB pixels.
    // Returns empty for null input or invalid/unrepresentable dimensions.
    static QByteArray resizeBicubic(const unsigned char* source, int width, int height,
                                   int toWidth, int toHeight);
};

} // namespace qcopilots_search
