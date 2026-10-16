#include "runtime/BackendSelection.h"

#include <QtTest>

using namespace qcopilots_search::detail;

namespace {
BackendDevice cpu() { return {QStringLiteral("CPU"), QStringLiteral("CPU"), QStringLiteral("Test CPU"), true, false}; }
BackendDevice gpu(const QString& backend, const QString& name = QStringLiteral("GPU0")) {
    return {backend, name, QStringLiteral("Policy fixture, not a real GPU"), false, true};
}
BackendDevice identifiedGpu(const QString& backend, const QString& id,
                            const QString& name = QStringLiteral("Identical GPU model")) {
    auto device = gpu(backend, name);
    device.deviceId = id;
    device.memoryTotal = 32ULL * 1024 * 1024 * 1024;
    device.memoryFree = 24ULL * 1024 * 1024 * 1024;
    return device;
}
BackendDeviceSelector selectorFor(const BackendDevice& device) {
    return {device.backend, device.deviceId, device.name};
}
BackendDevice unsupportedMetal(const QString& name) {
    auto device = gpu(QStringLiteral("MTL"), name);
    device.unavailableReason = QStringLiteral("Policy fixture: metal %1 rejects required matrix multiplication.").arg(name);
    return device;
}
}

class BackendSelectionTest final : public QObject {
    Q_OBJECT
private slots:
    void modesAreExplicit() {
        QString error;
        QCOMPARE(parseBackendMode(QStringLiteral("auto"), &error).value(), BackendMode::Auto);
        QCOMPARE(parseBackendMode(QStringLiteral(" CPU "), &error).value(), BackendMode::Cpu);
        QCOMPARE(parseBackendMode(QStringLiteral("gpu"), &error).value(), BackendMode::Gpu);
        for (const auto& invalid : {QString{}, QStringLiteral("cuda"), QStringLiteral("automatic"), QStringLiteral("gpu0")}) {
            QVERIFY(!parseBackendMode(invalid, &error));
            QVERIFY(!error.isEmpty());
        }
        QCOMPARE(backendModeName(BackendMode::Auto), QStringLiteral("auto"));
        QCOMPARE(backendModeName(BackendMode::Cpu), QStringLiteral("cpu"));
        QCOMPARE(backendModeName(BackendMode::Gpu), QStringLiteral("gpu"));
    }

    void packagedBackendChoosesOnlyItsOwnGpu_data() {
        QTest::addColumn<QString>("packaged");
        QTest::addColumn<QString>("registry");
        QTest::newRow("cuda") << QStringLiteral("cuda") << QStringLiteral("CUDA");
        QTest::newRow("hip") << QStringLiteral("hip") << QStringLiteral("ROCm");
        QTest::newRow("vulkan") << QStringLiteral("vulkan") << QStringLiteral("Vulkan");
        QTest::newRow("metal") << QStringLiteral("metal") << QStringLiteral("Metal");
        QTest::newRow("metal-registry") << QStringLiteral("metal") << QStringLiteral("MTL");
        QTest::newRow("metal-registry-case") << QStringLiteral("metal") << QStringLiteral(" mTl ");
    }

    void metalRegistryNameMatchesPackagedBackend() {
        QCOMPARE(canonicalBackendName(QStringLiteral("MTL")), QStringLiteral("metal"));
        QCOMPARE(canonicalBackendName(QStringLiteral(" mTl ")), QStringLiteral("metal"));
        QCOMPARE(canonicalBackendName(QStringLiteral("MTL0")), QStringLiteral("mtl0"));
    }

    void packagedBackendChoosesOnlyItsOwnGpu() {
        QFETCH(QString, packaged);
        QFETCH(QString, registry);
        const QVector<BackendDevice> devices{cpu(), gpu(QStringLiteral("Unpackaged")), gpu(registry)};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackend(mode, packaged, devices);
            QVERIFY(selected.error.isEmpty());
            QVERIFY(selected.gpu);
            QCOMPARE(selected.deviceIndex, 2);
            QCOMPARE(selected.backend, packaged);
            QVERIFY(selected.fallbackReason.isEmpty());
        }
    }

    void cpuModeDoesNotUseAvailableGpu() {
        const QVector<BackendDevice> devices{gpu(QStringLiteral("CUDA")), cpu()};
        const auto selected = selectBackend(BackendMode::Cpu, QStringLiteral("cuda"), devices,
                                             QStringLiteral("GPU loader unavailable"));
        QVERIFY(selected.error.isEmpty());
        QVERIFY(!selected.gpu);
        QCOMPARE(selected.deviceIndex, 1);
        QCOMPARE(selected.backend, QStringLiteral("cpu"));
        QVERIFY(selected.fallbackReason.isEmpty());
    }

    void autoReportsMissingGpuAndStrictModeRejectsIt() {
        const QVector<BackendDevice> devices{cpu(), gpu(QStringLiteral("Vulkan"))};
        const auto automatic = selectBackend(BackendMode::Auto, QStringLiteral("hip"), devices);
        QVERIFY(automatic.error.isEmpty());
        QCOMPARE(automatic.deviceIndex, 0);
        QVERIFY(!automatic.gpu);
        QVERIFY(automatic.fallbackReason.contains(QStringLiteral("hip")));
        const auto strict = selectBackend(BackendMode::Gpu, QStringLiteral("hip"), devices);
        QCOMPARE(strict.deviceIndex, -1);
        QVERIFY(!strict.gpu);
        QVERIFY(strict.error.contains(QStringLiteral("Strict GPU")));
    }

    void loadErrorsArePreserved() {
        const QVector<BackendDevice> devices{cpu(), gpu(QStringLiteral("CUDA"))};
        const QString failure = QStringLiteral("Missing packaged cublas dependency");
        const auto automatic = selectBackend(BackendMode::Auto, QStringLiteral("cuda"), devices, failure);
        QVERIFY(!automatic.gpu);
        QCOMPARE(automatic.fallbackReason, failure);
        const auto strict = selectBackend(BackendMode::Gpu, QStringLiteral("cuda"), devices, failure);
        QCOMPARE(strict.deviceIndex, -1);
        QVERIFY(strict.error.contains(failure));
    }

    void metalRegistryDoesNotBypassCpuOrFailurePolicy() {
        const QVector<BackendDevice> devices{cpu(), gpu(QStringLiteral("MTL"), QStringLiteral("MTL0"))};
        const auto forcedCpu = selectBackend(BackendMode::Cpu, QStringLiteral("metal"), devices);
        QCOMPARE(forcedCpu.deviceIndex, 0);
        QVERIFY(!forcedCpu.gpu);
        const auto missing = selectBackend(BackendMode::Auto, QStringLiteral("metal"), {cpu()});
        QCOMPARE(missing.deviceIndex, 0);
        QVERIFY(missing.fallbackReason.contains(QStringLiteral("metal")));
        const QString failure = QStringLiteral("Metal initialization failed");
        const auto automatic = selectBackend(BackendMode::Auto, QStringLiteral("metal"), devices, failure);
        QCOMPARE(automatic.deviceIndex, 0);
        QVERIFY(!automatic.gpu);
        QCOMPARE(automatic.fallbackReason, failure);
        const auto strict = selectBackend(BackendMode::Gpu, QStringLiteral("metal"), devices, failure);
        QCOMPARE(strict.deviceIndex, -1);
        QVERIFY(!strict.gpu);
        QVERIFY(strict.error.contains(failure));
    }

    void metalNilWrapperIsUnavailable_data() {
        QTest::addColumn<QString>("registry");
        QTest::newRow("upstream") << QStringLiteral("MTL");
        QTest::newRow("canonical") << QStringLiteral("metal");
        QTest::newRow("case-and-space") << QStringLiteral(" mTl ");
    }

    void metalNilWrapperIsUnavailable() {
        QFETCH(QString, registry);
        const BackendDevice missing{registry, {}, {}, false, true};
        const QVector<BackendDevice> devices{missing, cpu()};
        const auto automatic = selectBackend(BackendMode::Auto, QStringLiteral("metal"), devices);
        QVERIFY(automatic.error.isEmpty());
        QCOMPARE(automatic.deviceIndex, 1);
        QCOMPARE(automatic.backend, QStringLiteral("cpu"));
        QVERIFY(!automatic.gpu);
        QVERIFY(automatic.fallbackReason.contains(QStringLiteral("metal")));
        const auto strict = selectBackend(BackendMode::Gpu, QStringLiteral("metal"), devices);
        QCOMPARE(strict.deviceIndex, -1);
        QVERIFY(strict.backend.isEmpty());
        QVERIFY(!strict.gpu);
        QVERIFY(strict.error.contains(QStringLiteral("Strict GPU")));
        QVERIFY(strict.error.contains(QStringLiteral("metal")));
    }

    void namedMetalDeviceRemainsAvailableWithoutDescription() {
        const BackendDevice missing{QStringLiteral("MTL"), {}, {}, false, true};
        const BackendDevice named{QStringLiteral("MTL"), QStringLiteral("MTL0"), {}, false, true};
        const QVector<BackendDevice> devices{cpu(), missing, named};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackend(mode, QStringLiteral("metal"), devices);
            QVERIFY(selected.error.isEmpty());
            QCOMPARE(selected.deviceIndex, 2);
            QCOMPARE(selected.backend, QStringLiteral("metal"));
            QVERIFY(selected.gpu);
            QVERIFY(selected.fallbackReason.isEmpty());
        }
    }

    void unsupportedMetalFallsBackBeforeGpuSelection() {
        const auto unsupported = unsupportedMetal(QStringLiteral("MTL0"));
        const QVector<BackendDevice> devices{unsupported, cpu()};
        const auto automatic = selectBackend(BackendMode::Auto, QStringLiteral("metal"), devices);
        QVERIFY(automatic.error.isEmpty());
        QCOMPARE(automatic.deviceIndex, 1);
        QCOMPARE(automatic.backend, QStringLiteral("cpu"));
        QVERIFY(!automatic.gpu);
        QCOMPARE(automatic.fallbackReason, unsupported.unavailableReason);
        const auto strict = selectBackend(BackendMode::Gpu, QStringLiteral("metal"), devices);
        QCOMPARE(strict.deviceIndex, -1);
        QVERIFY(strict.backend.isEmpty());
        QVERIFY(!strict.gpu);
        QVERIFY(strict.fallbackReason.isEmpty());
        QVERIFY(strict.error.startsWith(QStringLiteral("Strict GPU mode cannot start:")));
        QVERIFY(strict.error.contains(unsupported.unavailableReason));
    }

    void unsupportedMetalDoesNotHideLaterCompatibleDevice() {
        const BackendDevice missing{QStringLiteral("MTL"), {}, {}, false, true};
        const QVector<BackendDevice> devices{gpu(QStringLiteral("CUDA")), unsupportedMetal(QStringLiteral("MTL0")),
            cpu(), missing, gpu(QStringLiteral("MTL"), QStringLiteral("MTL1")),
            gpu(QStringLiteral("MTL"), QStringLiteral("MTL2"))};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackend(mode, QStringLiteral("metal"), devices);
            QVERIFY(selected.error.isEmpty());
            QCOMPARE(selected.deviceIndex, 4);
            QCOMPARE(selected.backend, QStringLiteral("metal"));
            QVERIFY(selected.gpu);
            QVERIFY(selected.fallbackReason.isEmpty());
        }
    }

    void allUnsupportedMetalReasonsAreRetained() {
        const auto first = unsupportedMetal(QStringLiteral("MTL0"));
        const auto second = unsupportedMetal(QStringLiteral("MTL1"));
        auto foreign = gpu(QStringLiteral("CUDA"));
        foreign.unavailableReason = QStringLiteral("An unrelated backend failure");
        const QVector<BackendDevice> devices{first, cpu(), foreign, second};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackend(mode, QStringLiteral("metal"), devices);
            const auto reason = mode == BackendMode::Auto ? selected.fallbackReason : selected.error;
            QVERIFY(!selected.gpu);
            QCOMPARE(selected.deviceIndex, mode == BackendMode::Auto ? 1 : -1);
            QVERIFY(reason.contains(first.unavailableReason));
            QVERIFY(reason.contains(second.unavailableReason));
            QVERIFY(reason.indexOf(first.unavailableReason) < reason.indexOf(second.unavailableReason));
            QVERIFY(!reason.contains(foreign.unavailableReason));
        }
    }

    void capabilityRejectionsPreserveCpuAndLoaderPriority() {
        const auto unsupported = unsupportedMetal(QStringLiteral("MTL0"));
        const QVector<BackendDevice> devices{unsupported, cpu()};
        const QString loadError = QStringLiteral("The packaged Metal module could not be loaded");
        const auto cpuOnly = selectBackend(BackendMode::Cpu, QStringLiteral("metal"), devices, loadError);
        QVERIFY(cpuOnly.error.isEmpty());
        QCOMPARE(cpuOnly.deviceIndex, 1);
        QCOMPARE(cpuOnly.backend, QStringLiteral("cpu"));
        QVERIFY(cpuOnly.fallbackReason.isEmpty());
        QVERIFY(!cpuOnly.gpu);
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto failedLoad = selectBackend(mode, QStringLiteral("metal"), devices, loadError);
            const auto reason = mode == BackendMode::Auto ? failedLoad.fallbackReason : failedLoad.error;
            QVERIFY(reason.contains(loadError));
            QVERIFY(!reason.contains(unsupported.unavailableReason));
            QVERIFY(!failedLoad.gpu);
            const auto missingCpu = selectBackend(mode, QStringLiteral("metal"), {unsupported}, loadError);
            QCOMPARE(missingCpu.deviceIndex, -1);
            QVERIFY(missingCpu.error.contains(QStringLiteral("CPU")));
            QVERIFY(!missingCpu.error.contains(loadError));
            QVERIFY(!missingCpu.error.contains(unsupported.unavailableReason));
        }
    }

    void emptyNamesDoNotChangeOtherBackendPolicies() {
        for (const auto& registry : {QStringLiteral("CUDA"), QStringLiteral("ROCm"), QStringLiteral("Vulkan")}) {
            const BackendDevice unnamed{registry, {}, {}, false, true};
            const auto packaged = canonicalBackendName(registry);
            for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
                const auto selected = selectBackend(mode, packaged, {cpu(), unnamed});
                QVERIFY(selected.error.isEmpty());
                QCOMPARE(selected.deviceIndex, 1);
                QCOMPARE(selected.backend, packaged);
                QVERIFY(selected.gpu);
            }
        }
    }

    void cpuPackagesHaveNoAutomaticGpuSelection() {
        const QVector<BackendDevice> devices{cpu(), gpu(QStringLiteral("CUDA"))};
        const auto automatic = selectBackend(BackendMode::Auto, QStringLiteral("cpu"), devices);
        QCOMPARE(automatic.deviceIndex, 0);
        QVERIFY(automatic.error.isEmpty());
        QVERIFY(automatic.fallbackReason.isEmpty());
        QVERIFY(!selectBackend(BackendMode::Gpu, QStringLiteral("cpu"), devices).error.isEmpty());
    }

    void cpuBackendRemainsRequiredAndUnknownBuildIsRejected() {
        const QVector<BackendDevice> onlyGpu{gpu(QStringLiteral("CUDA"))};
        const auto missingCpu = selectBackend(BackendMode::Gpu, QStringLiteral("cuda"), onlyGpu);
        QCOMPARE(missingCpu.deviceIndex, -1);
        QVERIFY(missingCpu.error.contains(QStringLiteral("CPU")));
        QVERIFY(!selectBackend(BackendMode::Auto, QStringLiteral("invalid"), {cpu()}).error.isEmpty());
    }

    void firstCompatibleDeviceIsDeterministic() {
        const QVector<BackendDevice> devices{cpu(), gpu(QStringLiteral("CUDA"), QStringLiteral("GPU1")),
                                             gpu(QStringLiteral("CUDA"), QStringLiteral("GPU2"))};
        QCOMPARE(selectBackend(BackendMode::Auto, QStringLiteral("cuda"), devices).deviceIndex, 1);
    }

    void compatibleGpuEnumerationHandlesZeroOneAndTwoDevices() {
        const auto first = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("0000:03:00.0"));
        const auto second = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("0000:0b:00.0"));
        QVERIFY(compatibleGpuDevices(QStringLiteral("hip"), {}).isEmpty());
        QVERIFY(compatibleGpuDevices(QStringLiteral("hip"), {cpu()}).isEmpty());
        QCOMPARE(compatibleGpuDevices(QStringLiteral("hip"), {cpu(), first}), QVector<int>({1}));
        QCOMPARE(compatibleGpuDevices(QStringLiteral("hip"), {first, cpu(), second}), QVector<int>({0, 2}));
        QVERIFY(deviceIdentityError(first, {cpu(), first, second}).isEmpty());
        QVERIFY(deviceIdentityError(second, {cpu(), first, second}).isEmpty());
    }

    void compatibleGpuEnumerationFiltersBackendsAndCapabilities() {
        const auto cuda = identifiedGpu(QStringLiteral("CUDA"), QStringLiteral("GPU-cuda"));
        const auto hip = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("GPU-hip"));
        const auto vulkan = identifiedGpu(QStringLiteral("Vulkan"), QStringLiteral("GPU-vulkan"));
        const auto metal = gpu(QStringLiteral("MTL"), QStringLiteral("MTL0"));
        const BackendDevice nilMetal{QStringLiteral("MTL"), {}, {}, false, true};
        auto unavailable = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("GPU-unavailable"));
        unavailable.unavailableReason = QStringLiteral("Required operations are unsupported");
        const QVector<BackendDevice> devices{cpu(), cuda, hip, vulkan, nilMetal, metal,
                                             unsupportedMetal(QStringLiteral("MTL1")), unavailable};
        QCOMPARE(compatibleGpuDevices(QStringLiteral("cuda"), devices), QVector<int>({1}));
        QCOMPARE(compatibleGpuDevices(QStringLiteral("hip"), devices), QVector<int>({2}));
        QCOMPARE(compatibleGpuDevices(QStringLiteral("vulkan"), devices), QVector<int>({3}));
        QCOMPARE(compatibleGpuDevices(QStringLiteral("metal"), devices), QVector<int>({5}));
        QVERIFY(compatibleGpuDevices(QStringLiteral("cpu"), devices).isEmpty());
        QVERIFY(compatibleGpuDevices(QStringLiteral("unknown"), devices).isEmpty());
    }

    void exactIdentitySelectsBothCardsWithTheSameModelName() {
        for (const auto& backend : {QStringLiteral("CUDA"), QStringLiteral("ROCm"), QStringLiteral("Vulkan")}) {
            const auto first = identifiedGpu(backend, QStringLiteral("first-device"));
            const auto second = identifiedGpu(backend, QStringLiteral("second-device"));
            // Enumeration order is deliberately different from discovery order.
            const QVector<BackendDevice> devices{cpu(), second, first};
            for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
                for (const auto& device : {first, second}) {
                    const auto selected = selectBackendDevice(mode, canonicalBackendName(backend), devices,
                                                              selectorFor(device));
                    QVERIFY2(selected.error.isEmpty(), qPrintable(selected.error));
                    QCOMPARE(selected.deviceIndex, device.deviceId == first.deviceId ? 2 : 1);
                    QCOMPARE(selected.backend, canonicalBackendName(backend));
                    QVERIFY(selected.gpu);
                    QVERIFY(selected.fallbackReason.isEmpty());
                }
            }
        }
    }

    void missingExactIdentityDoesNotFallBackToSameNameOrCpu() {
        const auto removed = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("removed-device"));
        const auto remaining = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("remaining-device"));
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackendDevice(mode, QStringLiteral("hip"), {cpu(), remaining},
                                                      selectorFor(removed));
            QCOMPARE(selected.deviceIndex, -1);
            QVERIFY(!selected.gpu);
            QVERIFY(!selected.error.isEmpty());
            QVERIFY(selected.fallbackReason.isEmpty());
        }
    }

    void duplicateIdsAreRejectedWithoutDisablingHealthyCards() {
        const auto duplicate = identifiedGpu(QStringLiteral("CUDA"), QStringLiteral("duplicate-id"));
        auto sameIdDifferentName = duplicate;
        sameIdDifferentName.name = QStringLiteral("Different label for duplicate identity");
        const auto healthy = identifiedGpu(QStringLiteral("CUDA"), QStringLiteral("healthy-id"));
        const auto foreign = identifiedGpu(QStringLiteral("Vulkan"), QStringLiteral("healthy-id"));
        const QVector<BackendDevice> devices{cpu(), duplicate, sameIdDifferentName, healthy, foreign};
        QVERIFY(!deviceIdentityError(duplicate, devices).isEmpty());
        QVERIFY(!deviceIdentityError(sameIdDifferentName, devices).isEmpty());
        QVERIFY(deviceIdentityError(healthy, devices).isEmpty());
        QCOMPARE(compatibleGpuDevices(QStringLiteral("cuda"), devices), QVector<int>({3}));
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackendDevice(mode, QStringLiteral("cuda"), devices,
                                                      selectorFor(duplicate));
            QCOMPARE(selected.deviceIndex, -1);
            QVERIFY(!selected.error.isEmpty());
            QVERIFY(selected.fallbackReason.isEmpty());
        }
    }

    void nameOnlySelectionRequiresUniqueUnnamedIdentity() {
        const auto first = gpu(QStringLiteral("Vulkan"), QStringLiteral("Vulkan0"));
        const auto second = gpu(QStringLiteral("Vulkan"), QStringLiteral("Vulkan1"));
        const auto foreign = gpu(QStringLiteral("CUDA"), first.name);
        const QVector<BackendDevice> devices{cpu(), first, foreign, second};
        QVERIFY(deviceIdentityError(first, devices).isEmpty());
        QCOMPARE(compatibleGpuDevices(QStringLiteral("vulkan"), devices), QVector<int>({1, 3}));
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackendDevice(mode, QStringLiteral("vulkan"), devices,
                                                      selectorFor(first));
            QVERIFY2(selected.error.isEmpty(), qPrintable(selected.error));
            QCOMPARE(selected.deviceIndex, 1);
            QVERIFY(selected.gpu);
        }
        const QVector<BackendDevice> duplicateNames{cpu(), first, first, second};
        QVERIFY(!deviceIdentityError(first, duplicateNames).isEmpty());
        QCOMPARE(compatibleGpuDevices(QStringLiteral("vulkan"), duplicateNames), QVector<int>({3}));
        const auto ambiguous = selectBackendDevice(BackendMode::Auto, QStringLiteral("vulkan"), duplicateNames,
                                                   selectorFor(first));
        QCOMPARE(ambiguous.deviceIndex, -1);
        QVERIFY(!ambiguous.error.isEmpty());
        QVERIFY(ambiguous.fallbackReason.isEmpty());
    }

    void nameOnlySelectorCannotWeakenKnownIdentity() {
        const auto identified = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("known-id"));
        const BackendDeviceSelector weakSelector{QStringLiteral("hip"), {}, identified.name};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto selected = selectBackendDevice(mode, QStringLiteral("hip"), {cpu(), identified}, weakSelector);
            QCOMPARE(selected.deviceIndex, -1);
            QVERIFY(!selected.error.isEmpty());
            QVERIFY(selected.fallbackReason.isEmpty());
        }
        // A newly visible identified card with the same label also makes a
        // previously name-only identity ambiguous.
        const auto noId = gpu(QStringLiteral("ROCm"), identified.name);
        const QVector<BackendDevice> devices{cpu(), noId, identified};
        QVERIFY(!deviceIdentityError(noId, devices).isEmpty());
        QCOMPARE(compatibleGpuDevices(QStringLiteral("hip"), devices), QVector<int>({2}));
        QVERIFY(!selectBackendDevice(BackendMode::Auto, QStringLiteral("hip"), devices, weakSelector).error.isEmpty());
    }

    void selectedBackendMustMatchPackageAndMode() {
        const auto cuda = identifiedGpu(QStringLiteral("CUDA"), QStringLiteral("GPU-cuda"));
        const auto hip = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("GPU-hip"));
        const QVector<BackendDevice> devices{cpu(), cuda, hip};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto foreign = selectBackendDevice(mode, QStringLiteral("cuda"), devices, selectorFor(hip));
            QCOMPARE(foreign.deviceIndex, -1);
            QVERIFY(!foreign.error.isEmpty());
            QVERIFY(foreign.fallbackReason.isEmpty());
            QVERIFY(!selectBackendDevice(mode, QStringLiteral("cpu"), devices, selectorFor(cuda)).error.isEmpty());
            QVERIFY(!selectBackendDevice(mode, QStringLiteral("unknown"), devices, selectorFor(cuda)).error.isEmpty());
            const BackendDeviceSelector unknown{QStringLiteral("unknown"), {}, QStringLiteral("GPU0")};
            QVERIFY(!selectBackendDevice(mode, QStringLiteral("cuda"), devices, unknown).error.isEmpty());
        }
        const auto forcedCpu = selectBackendDevice(BackendMode::Cpu, QStringLiteral("cuda"), devices, selectorFor(cuda));
        QCOMPARE(forcedCpu.deviceIndex, -1);
        QVERIFY(!forcedCpu.error.isEmpty());
    }

    void explicitCpuRemainsAvailableExceptInStrictGpuMode() {
        const auto host = cpu();
        const auto card = identifiedGpu(QStringLiteral("CUDA"), QStringLiteral("GPU-cuda"));
        const QVector<BackendDevice> devices{card, host};
        for (const auto mode : {BackendMode::Auto, BackendMode::Cpu}) {
            const auto selected = selectBackendDevice(mode, QStringLiteral("cuda"), devices, selectorFor(host),
                                                      QStringLiteral("GPU loader failed"));
            QVERIFY2(selected.error.isEmpty(), qPrintable(selected.error));
            QCOMPARE(selected.deviceIndex, 1);
            QCOMPARE(selected.backend, QStringLiteral("cpu"));
            QVERIFY(!selected.gpu);
            QVERIFY(selected.fallbackReason.isEmpty());
        }
        const auto strict = selectBackendDevice(BackendMode::Gpu, QStringLiteral("cuda"), devices, selectorFor(host));
        QCOMPARE(strict.deviceIndex, -1);
        QVERIFY(!strict.error.isEmpty());
        QVERIFY(strict.fallbackReason.isEmpty());
        const auto missing = selectBackendDevice(BackendMode::Cpu, QStringLiteral("cuda"), {card}, selectorFor(host));
        QCOMPARE(missing.deviceIndex, -1);
        QVERIFY(missing.error.contains(QStringLiteral("CPU")));
    }

    void selectedGpuPreservesCapabilityAndLoaderFailures() {
        auto unavailable = identifiedGpu(QStringLiteral("ROCm"), QStringLiteral("GPU-unsupported"));
        unavailable.unavailableReason = QStringLiteral("Required matrix multiplication is unsupported");
        const QString loadError = QStringLiteral("The packaged HIP runtime could not load");
        const QVector<BackendDevice> devices{cpu(), unavailable};
        for (const auto mode : {BackendMode::Auto, BackendMode::Gpu}) {
            const auto rejected = selectBackendDevice(mode, QStringLiteral("hip"), devices, selectorFor(unavailable));
            QCOMPARE(rejected.deviceIndex, -1);
            QVERIFY(rejected.error.contains(unavailable.unavailableReason));
            QVERIFY(rejected.fallbackReason.isEmpty());
            const auto failedLoad = selectBackendDevice(mode, QStringLiteral("hip"), devices,
                                                        selectorFor(unavailable), loadError);
            QCOMPARE(failedLoad.deviceIndex, -1);
            QVERIFY(failedLoad.error.contains(loadError));
            QVERIFY(!failedLoad.error.contains(unavailable.unavailableReason));
            QVERIFY(failedLoad.fallbackReason.isEmpty());
            const auto missingCpu = selectBackendDevice(mode, QStringLiteral("hip"), {unavailable},
                                                        selectorFor(unavailable));
            QCOMPARE(missingCpu.deviceIndex, -1);
            QVERIFY(missingCpu.error.contains(QStringLiteral("CPU")));
        }
    }
};

QTEST_GUILESS_MAIN(BackendSelectionTest)
#include "backend_selection_test.moc"
