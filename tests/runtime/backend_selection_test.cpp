#include "runtime/BackendSelection.h"

#include <QtTest>

using namespace qcopilots_search::detail;

namespace {
BackendDevice cpu() { return {QStringLiteral("CPU"), QStringLiteral("CPU"), QStringLiteral("Test CPU"), true, false}; }
BackendDevice gpu(const QString& backend, const QString& name = QStringLiteral("GPU0")) {
    return {backend, name, QStringLiteral("Policy fixture, not a real GPU"), false, true};
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
};

QTEST_GUILESS_MAIN(BackendSelectionTest)
#include "backend_selection_test.moc"
