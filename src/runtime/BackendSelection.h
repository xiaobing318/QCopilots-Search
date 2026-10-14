#pragma once

#include <QString>
#include <QVector>
#include <optional>

namespace qcopilots_search::detail {

enum class BackendMode { Auto, Cpu, Gpu };

std::optional<BackendMode> parseBackendMode(const QString& value, QString* error = nullptr);
QString backendModeName(BackendMode mode);
QString canonicalBackendName(const QString& registryName);

// A snapshot of real ggml enumeration at runtime. Tests exercise policy using
// descriptors only and never claim to execute GPU kernels.
struct BackendDevice {
    QString backend;
    QString name;
    QString description;
    bool cpu = false;
    bool gpu = false;
    // An explicit capability rejection, never an initialization failure.
    QString unavailableReason;
};

struct BackendSelection {
    int deviceIndex = -1;
    QString backend;
    QString fallbackReason;
    QString error;
    bool gpu = false;
};

BackendSelection selectBackend(BackendMode mode, const QString& packagedBackend,
                               const QVector<BackendDevice>& devices,
                               const QString& gpuLoadError = {});

}
