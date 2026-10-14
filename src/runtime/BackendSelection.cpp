#include "BackendSelection.h"

namespace qcopilots_search::detail {

std::optional<BackendMode> parseBackendMode(const QString& value, QString* error)
{
    if (error) error->clear();
    const auto mode = value.trimmed().toLower();
    if (mode == QStringLiteral("auto")) return BackendMode::Auto;
    if (mode == QStringLiteral("cpu")) return BackendMode::Cpu;
    if (mode == QStringLiteral("gpu")) return BackendMode::Gpu;
    if (error) *error = QStringLiteral("Invalid inference backend mode '%1'. Expected auto, cpu or gpu.").arg(value);
    return std::nullopt;
}

QString backendModeName(BackendMode mode)
{
    switch (mode) {
    case BackendMode::Auto: return QStringLiteral("auto");
    case BackendMode::Cpu: return QStringLiteral("cpu");
    case BackendMode::Gpu: return QStringLiteral("gpu");
    }
    return {};
}

QString canonicalBackendName(const QString& registryName)
{
    const auto name = registryName.trimmed().toLower();
    // HIP and Metal registry names differ from their packaged backend names.
    if (name == QStringLiteral("rocm")) return QStringLiteral("hip");
    if (name == QStringLiteral("mtl")) return QStringLiteral("metal");
    return name;
}

BackendSelection selectBackend(BackendMode mode, const QString& packagedBackend,
                               const QVector<BackendDevice>& devices, const QString& gpuLoadError)
{
    BackendSelection result;
    const auto packaged = canonicalBackendName(packagedBackend);
    if (packaged != QStringLiteral("cpu") && packaged != QStringLiteral("cuda")
        && packaged != QStringLiteral("hip") && packaged != QStringLiteral("vulkan")
        && packaged != QStringLiteral("metal")) {
        result.error = QStringLiteral("Unknown packaged inference backend '%1'.").arg(packagedBackend);
        return result;
    }
    int cpuIndex = -1;
    int gpuIndex = -1;
    for (qsizetype index = 0; index < devices.size(); ++index) {
        const auto& device = devices[index];
        if (device.cpu && cpuIndex < 0) cpuIndex = int(index);
        if (device.gpu && gpuIndex < 0 && packaged != QStringLiteral("cpu")
            && canonicalBackendName(device.backend) == packaged) gpuIndex = int(index);
    }
    // ggml still needs its CPU backend for unsupported operations even when all
    // model layers are offloaded. A broken CPU installation is not a GPU fallback.
    if (cpuIndex < 0) {
        result.error = QStringLiteral("No compatible CPU device is available in the packaged runtime.");
        return result;
    }
    if (mode != BackendMode::Cpu && gpuIndex >= 0 && gpuLoadError.isEmpty()) {
        result.deviceIndex = gpuIndex;
        result.backend = packaged;
        result.gpu = true;
        return result;
    }
    QString reason;
    if (mode != BackendMode::Cpu) {
        if (packaged == QStringLiteral("cpu")) {
            if (mode == BackendMode::Gpu) reason = QStringLiteral("This installation includes CPU inference only.");
        } else {
            reason = gpuLoadError.isEmpty()
                ? QStringLiteral("No compatible %1 GPU device was found. Check the installed GPU and driver.").arg(packaged)
                : gpuLoadError;
        }
    }
    if (mode == BackendMode::Gpu) {
        result.error = QStringLiteral("Strict GPU mode cannot start: %1").arg(reason);
        return result;
    }
    result.deviceIndex = cpuIndex;
    result.backend = QStringLiteral("cpu");
    result.fallbackReason = reason;
    return result;
}

}
