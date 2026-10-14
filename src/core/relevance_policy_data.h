#pragma once

// Replaced only by calibrate_relevance.py --header after a real model report
// passes both the frozen calibration and untouched holdout criteria. An
// uncalibrated build cannot silently present arbitrary candidates as matches.
namespace qcopilots_search::relevance_data {
inline constexpr bool calibrated = false;
inline constexpr const char *version = "unvalidated-v1";
inline constexpr const char *modelFingerprint = "";
inline constexpr const char *corpusSha256 = "";
inline constexpr const char *rawExportSha256 = "";
inline constexpr const char *calibrationReportSha256 = "";
inline constexpr double textMinimum = 1.000001;
inline constexpr double imageMinimum = 1.000001;
inline constexpr double audioMinimum = 1.000001;
inline constexpr double codeMinimum = 1.000001;
} // namespace qcopilots_search::relevance_data
