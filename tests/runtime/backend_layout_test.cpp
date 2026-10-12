#include "runtime/BackendPaths.h"
#include "runtime_test_paths.h"

#include <QFile>
#include <QtTest>

using namespace qcopilots_search;

class BackendLayoutTest final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
    }

    void cleanup()
    {
        const auto error = test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void macBundleUsesOnlySignedPluginTree()
    {
        QString error;
        auto temporary = test::temporaryDirectory(&error);
        QVERIFY2(temporary != nullptr, qPrintable(error));
        const auto contents = temporary->filePath(QStringLiteral("中文 安装/qcs.app/Contents"));
        const auto executable = QDir(contents).filePath(QStringLiteral("MacOS"));
        const auto plugins = QDir(contents).filePath(QStringLiteral("PlugIns/ggml"));
        const auto frameworks = QDir(contents).filePath(QStringLiteral("Frameworks"));
        QVERIFY(QDir().mkpath(executable));
        QVERIFY(QDir().mkpath(plugins));
        QVERIFY(QDir().mkpath(frameworks));
        QFile obsolete(QDir(frameworks).filePath(QStringLiteral("libggml-cpu-x64.so")));
        QVERIFY(obsolete.open(QIODevice::WriteOnly));
        QCOMPARE(obsolete.write("obsolete unsigned module"), qint64(24));
        obsolete.close();
        const auto roots = detail::cpuBackendSearchRoots(executable, detail::BackendPlatform::MacOS);
        QCOMPARE(roots, QStringList{plugins});
        QVERIFY(!roots.contains(frameworks));
        QVERIFY(!roots.contains(executable));
    }

    void standaloneLayoutsRemainCompatible_data()
    {
        QTest::addColumn<int>("platform");
        QTest::newRow("windows") << int(detail::BackendPlatform::Windows);
        QTest::newRow("linux") << int(detail::BackendPlatform::Unix);
        QTest::newRow("mac-developer-binary") << int(detail::BackendPlatform::MacOS);
    }

    void standaloneLayoutsRemainCompatible()
    {
        QFETCH(int, platform);
        QString error;
        auto temporary = test::temporaryDirectory(&error);
        QVERIFY2(temporary != nullptr, qPrintable(error));
        const QDir bin(temporary->filePath(QStringLiteral("工具 空格/bin")));
        QVERIFY(QDir().mkpath(bin.absolutePath()));
        const auto roots = detail::cpuBackendSearchRoots(bin.absolutePath(), detail::BackendPlatform(platform));
        QCOMPARE(roots, (QStringList{bin.absolutePath(), bin.filePath(QStringLiteral("../lib")),
                                    bin.filePath(QStringLiteral("../Frameworks"))}));
    }
};

QTEST_GUILESS_MAIN(BackendLayoutTest)
#include "backend_layout_test.moc"
