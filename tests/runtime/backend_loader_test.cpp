#include "runtime/BackendLoader.h"
#include "runtime_test_paths.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QtTest>

using namespace qcopilots_search;

namespace {
QByteArray manifest(const QString& directory, const QString& library,
                    const QString& backend = QStringLiteral("hip"), int version = 1) {
    return QJsonDocument(QJsonObject{{QStringLiteral("schemaVersion"), version},
        {QStringLiteral("backend"), backend},
        {QStringLiteral("libraryDirectories"), QJsonArray{directory}},
        {QStringLiteral("preloadLibraries"), QJsonArray{library}}}).toJson(QJsonDocument::Compact);
}
bool createLibrary(const QString& path) {
    if (!QDir().mkpath(QFileInfo(path).absolutePath())) return false;
    QFile library(path);
    if (!library.open(QIODevice::WriteOnly)) return false;
    const QByteArray content("path test fixture, never loaded");
    return library.write(content) == content.size();
}
}

class BackendLoaderTest final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void cleanup() {
        const auto error = test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void resolvesOnlyBundledPathsIncludingUnicode() {
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        const QString directory = QStringLiteral("rocm/运行 库/bin");
        const QString library = directory + QStringLiteral("/amdhip64_7.dll");
        QVERIFY(createLibrary(root->filePath(library)));
        const auto parsed = detail::parseGpuRuntimeManifest(manifest(directory, library), root->path(), QStringLiteral("hip"), &error);
        QVERIFY2(parsed.has_value(), qPrintable(error));
        QCOMPARE(parsed->libraryDirectories, QStringList{QFileInfo(root->filePath(directory)).canonicalFilePath()});
        QCOMPARE(parsed->preloadLibraries, QStringList{QFileInfo(root->filePath(library)).canonicalFilePath()});
    }

    void rejectsPathsOutsideInstallation_data() {
        QTest::addColumn<QString>("path");
        for (const auto& path : {QStringLiteral("../outside.dll"), QStringLiteral("rocm/../outside.dll"),
             QStringLiteral("C:/windows/system32/amdhip64_7.dll"), QStringLiteral("C:relative.dll"),
             QStringLiteral("\\\\server\\share\\file.dll"), QStringLiteral("/tmp/file.dll"),
             QStringLiteral("rocm/./file.dll"), QStringLiteral("rocm//file.dll"),
             QStringLiteral("rocm/file.dll:stream"), QString{}})
            QTest::newRow(qPrintable(path.isEmpty() ? QStringLiteral("empty") : path)) << path;
    }

    void installationRootIsAllowedOnlyAsDirectory() {
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        QVERIFY(createLibrary(root->filePath(QStringLiteral("runtime.dll"))));
        const auto parsed = detail::parseGpuRuntimeManifest(manifest(QStringLiteral("."), QStringLiteral("runtime.dll")),
            root->path(), QStringLiteral("hip"), &error);
        QVERIFY2(parsed.has_value(), qPrintable(error));
        QCOMPARE(parsed->libraryDirectories, QStringList{QFileInfo(root->path()).canonicalFilePath()});
        QVERIFY(!detail::parseGpuRuntimeManifest(manifest(QStringLiteral("."), QStringLiteral(".")),
            root->path(), QStringLiteral("hip"), &error));
    }

    void rejectsPathsOutsideInstallation() {
        QFETCH(QString, path);
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        QVERIFY(createLibrary(root->filePath(QStringLiteral("rocm/file.dll"))));
        QVERIFY(!detail::parseGpuRuntimeManifest(manifest(QStringLiteral("rocm"), path), root->path(), QStringLiteral("hip"), &error));
        QVERIFY(!error.isEmpty());
    }

    void rejectsMalformedAndMissingRuntime() {
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        QVERIFY(createLibrary(root->filePath(QStringLiteral("rocm/file.dll"))));
        for (const auto& json : {QByteArrayLiteral("not json"), QByteArrayLiteral("[]"),
             manifest(QStringLiteral("rocm"), QStringLiteral("rocm/file.dll"), QStringLiteral("cuda")),
             manifest(QStringLiteral("rocm"), QStringLiteral("rocm/file.dll"), QStringLiteral("hip"), 2),
             manifest(QStringLiteral("missing"), QStringLiteral("rocm/file.dll")),
             manifest(QStringLiteral("rocm/file.dll"), QStringLiteral("rocm/file.dll")),
             manifest(QStringLiteral("rocm"), QStringLiteral("rocm")),
             manifest(QStringLiteral("rocm"), QStringLiteral("rocm/missing.dll")), QByteArray(4 * 1024 * 1024 + 1, ' ')}) {
            QVERIFY(!detail::parseGpuRuntimeManifest(json, root->path(), QStringLiteral("hip"), &error));
            QVERIFY(!error.isEmpty());
        }
    }

    void duplicateAndEmptyListsAreRejected() {
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        QVERIFY(createLibrary(root->filePath(QStringLiteral("rocm/file.dll"))));
        auto object = QJsonDocument::fromJson(manifest(QStringLiteral("rocm"), QStringLiteral("rocm/file.dll"))).object();
        object.insert(QStringLiteral("preloadLibraries"), QJsonArray{QStringLiteral("rocm/file.dll"), QStringLiteral("rocm/file.dll")});
        QVERIFY(!detail::parseGpuRuntimeManifest(QJsonDocument(object).toJson(), root->path(), QStringLiteral("hip"), &error));
        object.insert(QStringLiteral("preloadLibraries"), QJsonArray{});
        QVERIFY(!detail::parseGpuRuntimeManifest(QJsonDocument(object).toJson(), root->path(), QStringLiteral("hip"), &error));
    }

#ifdef Q_OS_UNIX
    void rejectsSymlinksEscapingInstallation() {
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        auto outside = test::temporaryDirectory(&error);
        QVERIFY2(outside != nullptr, qPrintable(error));
        QVERIFY(createLibrary(outside->filePath(QStringLiteral("outside.so"))));
        test::TestSymbolicLink link;
        QVERIFY2(link.create(root->filePath(QStringLiteral("linked.so")), outside->filePath(QStringLiteral("outside.so")), &error),
                 qPrintable(error));
        QVERIFY(!detail::parseGpuRuntimeManifest(manifest(QStringLiteral("."), QStringLiteral("linked.so")),
            root->path(), QStringLiteral("hip"), &error));
        QVERIFY(error.contains(QStringLiteral("escapes")));
    }
#endif

    void windowsHipRequiresManifestButCpuDoesNot() {
#ifdef Q_OS_WIN
        QString error;
        auto root = test::temporaryDirectory(&error);
        QVERIFY2(root != nullptr, qPrintable(error));
        QVERIFY(!detail::prepareGpuRuntime(root->path(), QStringLiteral("hip")).isEmpty());
        QVERIFY(detail::prepareGpuRuntime(root->path(), QStringLiteral("cpu")).isEmpty());
#else
        QVERIFY(detail::prepareGpuRuntime(QString{}, QStringLiteral("hip")).isEmpty());
#endif
    }
};

QTEST_GUILESS_MAIN(BackendLoaderTest)
#include "backend_loader_test.moc"
