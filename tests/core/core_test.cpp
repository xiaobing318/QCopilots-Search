#include "core/app_settings.h"
#include "core/runtime_paths.h"
#include "core/ipc_directory.h"
#include "core/pipeline_manifest.h"
#include "core/model_files.h"
#include "core/native_lock_path.h"
#include "core/types.h"
#include "support/test_environment.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QLocalServer>
#include <QLocalSocket>
#include <QScopeGuard>
#include <QStandardPaths>
#include <QtTest>
#include <limits>
#ifdef Q_OS_WIN
#include <windows.h>
#endif
#ifdef Q_OS_UNIX
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#endif

using namespace qcopilots_search;
using namespace qcopilots_search::test;

class CoreTest : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }
    void explicitRuntimePaths();
    void nativeLockPaths();
    void runtimePathIdentity();
    void ipcIgnoresLongTemporaryDirectory();
#ifdef Q_OS_UNIX
    void privateIpcDirectoryRejectsUnsafeEntries();
    void retainedSymbolicLinksAreExplicitlyRemoved_data();
    void retainedSymbolicLinksAreExplicitlyRemoved();
    void symbolicLinkCreationFailurePreservesExistingFile();
    void symbolicLinkCleanupRejectsReplacement();
#endif
    void environmentOverrides();
    void absoluteFilesystemPaths();
    void defaultProfileDirectoryIgnoresOverrides();
    void legacyProfileEnvironmentRejected();
    void invalidProfileSelection_data();
    void invalidProfileSelection();
#ifdef Q_OS_WIN
    void absoluteWindowsProfilePaths();
#endif
    void workingDirectoryIndependence();
    void longAndUnicodePaths();
    void runtimeCreationFailure();
    void readOnlyModelDirectory();
    void temporaryRootsUnique();
    void customTemporaryRoot();
    void temporaryFailureDoesNotFallback();
    void cleanupBoundaries();
    void cleanupRejectsMovedRoot();
    void cleanupRejectsReplacedRoot();
    void temporaryWriteFailure();
    void destructorCleanupFailuresAreObservable();
#ifdef Q_OS_WIN
    void cleanupRequiresClosedWindowsHandles();
#endif
    void legacyProfileRejected_data();
    void legacyProfileRejected();
    void settingsRoundTrip();
    void generalFileRulesDefaults();
    void generalFileRulesUpgrade();
    void generalFileRulesRoundTrip();
    void generalFileRulesRejectInvalid_data();
    void generalFileRulesRejectInvalid();
    void generalFileRulesFingerprint();
    void settingsV1Migration_data();
    void settingsV1Migration();
    void settingsRejectCorruption();
    void settingsRejectUnsupportedTypes();
    void settingsRejectInvalidOpeners_data();
    void settingsRejectInvalidOpeners();
    void settingsOpenerPresets();
    void settingsAtomicFailure();
    void settingsFingerprint();
    void segmentSerialization();
    void invalidVectors();
    void searchHitSerialization();
    void progressSerialization();
    void pipelineContracts();
};

void CoreTest::nativeLockPaths()
{
    QCOMPARE(nativeLockFilePath({}), QString{});
#ifdef Q_OS_WIN
    QCOMPARE(nativeLockFilePath(QStringLiteral("C:/中文 folder/qcs.lock")),
             QStringLiteral("\\\\?\\C:\\中文 folder\\qcs.lock"));
    QCOMPARE(nativeLockFilePath(QStringLiteral("//server/share/中文 folder/qcs.lock")),
             QStringLiteral("\\\\?\\UNC\\server\\share\\中文 folder\\qcs.lock"));
    for (const QString &extended : {QStringLiteral("\\\\?\\C:\\目录\\lock"),
                                   QStringLiteral("\\\\?\\UNC\\server\\share\\目录\\lock"),
                                   QStringLiteral("\\\\.\\C:\\目录\\lock")}) {
        QCOMPARE(nativeLockFilePath(extended), extended);
        QCOMPARE(nativeLockFilePath(nativeLockFilePath(extended)), extended);
    }
    QCOMPARE(nativeLockFilePath(QStringLiteral("C:/folder/../folder/lock")),
             QStringLiteral("\\\\?\\C:\\folder\\lock"));
#else
    for (const QString &path : {QStringLiteral("/tmp/中文 folder/lock"), QStringLiteral("relative/lock"),
                               QStringLiteral("C:/literal-Windows-path/lock")})
        QCOMPARE(nativeLockFilePath(path), path);
#endif
}

void CoreTest::explicitRuntimePaths()
{
    TestEnvironment env(QStringLiteral("runtime"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = RuntimePaths::resolve(env.path(QStringLiteral("中文 profile")),
                                            env.path(QStringLiteral("独立 模型")));
    QVERIFY(!QFileInfo::exists(paths.profileDir)); // Resolution is read-only.
    QString error;
    QVERIFY2(paths.ensure(&error), qPrintable(error));
    QVERIFY(paths.profileDir.contains(QStringLiteral("中文 profile")));
    QVERIFY(paths.modelDir.contains(QStringLiteral("独立 模型")));
    QVERIFY(QFileInfo(paths.indexDir).isDir());
    QVERIFY(QFileInfo(paths.tempDir).isDir());
    QVERIFY(QFileInfo(paths.cacheDir).isDir());
    QVERIFY(QFileInfo(paths.logDir).isDir());
    QCOMPARE(QFileInfo(paths.configFile).absolutePath(), paths.profileDir);
    QCOMPARE(QFileInfo(paths.databaseFile()).absolutePath(), paths.indexDir);
    QVERIFY(QFileInfo(paths.ipcName).fileName().startsWith(QStringLiteral("qcs-")));
    QVERIFY(paths.ipcName.size() < 100);
#ifdef Q_OS_UNIX
    QVERIFY(QDir::isAbsolutePath(paths.ipcName));
    const sockaddr_un address{};
    QVERIFY(QFile::encodeName(paths.ipcName).size() + 2 <= qsizetype(sizeof(address.sun_path)));
#endif
}

void CoreTest::ipcIgnoresLongTemporaryDirectory()
{
    TestEnvironment env(QStringLiteral("ipc-long-temp"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = env.runtimePaths();
    const auto firstTemp = env.path(QStringLiteral("temporary path 中文/") + QString(70, u'a') + u'/' + QString(70, u'b'));
    const auto secondTemp = env.path(QStringLiteral("other temporary path 中文/") + QString(70, u'c'));
    QVERIFY(QDir().mkpath(firstTemp));
    QVERIFY(QDir().mkpath(secondTemp));
    ScopedEnvironment first("TMPDIR", firstTemp.toUtf8());
    QCOMPARE(env.runtimePaths().ipcName, paths.ipcName);
    QString error;
    QVERIFY2(paths.ensure(&error), qPrintable(error));
    QLocalServer server;
    server.setSocketOptions(QLocalServer::UserAccessOption);
    QVERIFY2(server.listen(paths.ipcName), qPrintable(server.errorString()));
#ifdef Q_OS_UNIX
    QCOMPARE(server.fullServerName(), paths.ipcName);
#endif
    ScopedEnvironment second("TMPDIR", secondTemp.toUtf8());
    QCOMPARE(env.runtimePaths().ipcName, paths.ipcName);
    QLocalSocket client;
    client.connectToServer(paths.ipcName);
    QVERIFY2(client.waitForConnected(3000), qPrintable(client.errorString()));
    QVERIFY(server.waitForNewConnection(3000));
    auto *peer = server.nextPendingConnection();
    QVERIFY(peer);
    const QByteArray payload("qcopilots-search-ipc");
    QCOMPARE(client.write(payload), payload.size());
    client.flush();
    QVERIFY(peer->waitForReadyRead(3000));
    QCOMPARE(peer->readAll(), payload);
    client.abort();
    peer->abort();
    delete peer;
    server.close();
}

#ifdef Q_OS_UNIX
void CoreTest::privateIpcDirectoryRejectsUnsafeEntries()
{
    TestEnvironment env(QStringLiteral("ipc-permissions"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto directory = env.path("private-ipc");
    QString error;
    QVERIFY(!detail::preparePrivateIpcDirectory(directory, false, &error));
    QVERIFY(!QFileInfo::exists(directory));
    QVERIFY2(detail::preparePrivateIpcDirectory(directory, true, &error), qPrintable(error));
    QVERIFY(detail::preparePrivateIpcDirectory(directory, false, &error));
    struct stat state{};
    QVERIFY(::lstat(QFile::encodeName(directory).constData(), &state) == 0);
    QCOMPARE(state.st_uid, ::geteuid());
    QCOMPARE(state.st_mode & 0777, mode_t(0700));
    QVERIFY(::chmod(QFile::encodeName(directory).constData(), 0755) == 0);
    QVERIFY(!detail::preparePrivateIpcDirectory(directory, false, &error));
    QVERIFY(error.contains("0700"));
    QVERIFY(!detail::preparePrivateIpcDirectory(directory, true, &error));
    QVERIFY(::lstat(QFile::encodeName(directory).constData(), &state) == 0);
    QCOMPARE(state.st_mode & 0777, mode_t(0755)); // Never silently chmod existing directories.
    QVERIFY(::chmod(QFile::encodeName(directory).constData(), 0700) == 0);
    QVERIFY(env.writeFile("private-ipc/marker", "preserve link target"));
    const auto link = env.path("private-link");
    TestSymbolicLink ownedLink;
    QVERIFY2(ownedLink.create(link, directory, &error), qPrintable(error));
    QVERIFY(!detail::preparePrivateIpcDirectory(link, false, &error));
    QVERIFY(!detail::preparePrivateIpcDirectory(link, true, &error));
    QVERIFY(QFileInfo(link).isSymLink());
    QVERIFY(env.writeFile("not-directory", "preserve existing file"));
    QVERIFY(!detail::preparePrivateIpcDirectory(env.path("not-directory"), true, &error));
    QFile regular(env.path("not-directory"));
    QVERIFY(regular.open(QIODevice::ReadOnly));
    QCOMPARE(regular.readAll(), QByteArray("preserve existing file"));
    QVERIFY2(ownedLink.remove(&error), qPrintable(error));
    QVERIFY(!QFileInfo(link).isSymLink());
    QVERIFY(!QFileInfo::exists(link));
    QFile marker(env.path("private-ipc/marker"));
    QVERIFY(marker.open(QIODevice::ReadOnly));
    QCOMPARE(marker.readAll(), QByteArray("preserve link target"));
}

void CoreTest::retainedSymbolicLinksAreExplicitlyRemoved_data()
{
    QTest::addColumn<int>("cleanupMode");
    QTest::newRow("explicit-unlink") << 0;
    QTest::newRow("scope-exit") << 1;
    QTest::newRow("early-return") << 2;
}

void CoreTest::retainedSymbolicLinksAreExplicitlyRemoved()
{
    QFETCH(int, cleanupMode);
    ScopedEnvironment retain("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS", "1");
    TestEnvironment target("link-target");
    QVERIFY2(target.isValid(), qPrintable(target.error()));
    QVERIFY(target.writeFile("marker", "outside target survives"));
    QString root, link, error;
    {
        TestEnvironment fixture("retained-link");
        QVERIFY2(fixture.isValid(), qPrintable(fixture.error()));
        root = fixture.root();
        link = fixture.path("outside-link");
        const auto exercise = [&]() -> bool {
            TestSymbolicLink ownedLink;
            if (!ownedLink.create(link, target.root(), &error)) return false;
            if (cleanupMode == 2) return true; // Simulates an assertion's early return.
            if (cleanupMode == 0 && (!ownedLink.remove(&error) || !ownedLink.remove(&error))) return false;
            return true;
        };
        QVERIFY2(exercise(), qPrintable(error));
    }
    QVERIFY(QFileInfo(root).isDir()); // Preservation was active, not bypassed.
    QVERIFY(!QFileInfo(link).isSymLink());
    QVERIFY(!QFileInfo::exists(link));
    QFile marker(target.path("marker"));
    QVERIFY(marker.open(QIODevice::ReadOnly));
    QCOMPARE(marker.readAll(), QByteArray("outside target survives"));
}

void CoreTest::symbolicLinkCreationFailurePreservesExistingFile()
{
    TestEnvironment env("link-create");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    QVERIFY(env.writeFile("occupied", "original contents"));
    TestSymbolicLink link;
    QString error;
    QVERIFY(!link.create(env.path("occupied"), env.path("missing-target"), &error));
    QVERIFY(!error.isEmpty());
    QVERIFY2(link.remove(&error), qPrintable(error));
    QVERIFY2(link.remove(&error), qPrintable(error));
    QVERIFY2(link.create(env.path("dangling-link"), env.path("missing-target"), &error), qPrintable(error));
    QVERIFY(QFileInfo(env.path("dangling-link")).isSymLink());
    QVERIFY2(link.remove(&error), qPrintable(error));
    QVERIFY2(link.remove(&error), qPrintable(error));
    QVERIFY(!QFileInfo(env.path("dangling-link")).isSymLink());
    QFile original(env.path("occupied"));
    QVERIFY(original.open(QIODevice::ReadOnly));
    QCOMPARE(original.readAll(), QByteArray("original contents"));
}

void CoreTest::symbolicLinkCleanupRejectsReplacement()
{
    TestEnvironment env("link-replaced");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto path = env.path("link");
    const auto original = env.path("original-link");
    TestSymbolicLink link;
    QString error;
    QVERIFY(env.writeFile("target", "target survives replacement"));
    QVERIFY2(link.create(path, env.path("target"), &error), qPrintable(error));
    const auto restore = qScopeGuard([&] {
        if (!QFileInfo(original).isSymLink()) return;
        // Restore the deliberately displaced fixture before its owner exits,
        // including if an assertion above the explicit restore returns early.
        if ((QFileInfo::exists(path) && !QFile::remove(path)) || !QFile::rename(original, path))
            recordCleanupFailure(QStringLiteral("Cannot restore displaced test link: %1").arg(path));
    });
    QVERIFY(QFile::rename(path, original));
    QVERIFY(env.writeFile("link", "replacement survives"));
    QVERIFY(!link.remove(&error));
    QVERIFY(error.contains("replaced symbolic link"));
    QFile replacement(path);
    QVERIFY(replacement.open(QIODevice::ReadOnly));
    QCOMPARE(replacement.readAll(), QByteArray("replacement survives"));
    replacement.close();
    QVERIFY(QFile::remove(path));
    QVERIFY(QFile::rename(original, path));
    QVERIFY2(link.remove(&error), qPrintable(error));
    QVERIFY2(link.remove(&error), qPrintable(error));
    QVERIFY(!QFileInfo(path).isSymLink());
    QFile target(env.path("target"));
    QVERIFY(target.open(QIODevice::ReadOnly));
    QCOMPARE(target.readAll(), QByteArray("target survives replacement"));
}
#endif

void CoreTest::runtimePathIdentity()
{
    TestEnvironment env(QStringLiteral("identity"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto one = RuntimePaths::resolve(env.path(QStringLiteral("profile")), env.path("models"));
    const auto alternate = RuntimePaths::resolve(env.path(QStringLiteral("a/../profile/.")), env.path("models"));
    QCOMPARE(one.profileDir, alternate.profileDir);
    QCOMPARE(one.ipcName, alternate.ipcName);
    const auto two = RuntimePaths::resolve(env.path(QStringLiteral("other")), env.path("models"));
    QVERIFY(one.ipcName != two.ipcName);
    QString error;
    QVERIFY2(one.ensure(&error), qPrintable(error));
    QCOMPARE(RuntimePaths::resolve(one.profileDir, one.modelDir).ipcName, one.ipcName);
#ifdef Q_OS_WIN
    QCOMPARE(RuntimePaths::resolve(one.profileDir.toUpper(), one.modelDir).ipcName, one.ipcName);
#endif
}

void CoreTest::environmentOverrides()
{
    TestEnvironment env(QStringLiteral("environment"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    ScopedEnvironment profile("QCS_HOME", env.path("环境 profile").toUtf8());
    ScopedEnvironment legacy("QCOPILOTS_SEARCH_PROFILE_DIR", env.path("ignored legacy profile").toUtf8());
    ScopedEnvironment model("QCOPILOTS_SEARCH_MODEL_DIR", env.path("env model").toUtf8());
    const auto fromEnvironment = RuntimePaths::resolve();
    QVERIFY(fromEnvironment.resolutionError.isEmpty());
    QCOMPARE(fromEnvironment.profileDir, env.path("环境 profile"));
    QCOMPARE(fromEnvironment.modelDir, env.path("env model"));
    const auto fromArguments = RuntimePaths::resolve(env.path("explicit profile"), env.path("explicit model"));
    QCOMPARE(fromArguments.profileDir, env.path("explicit profile"));
    QCOMPARE(fromArguments.modelDir, env.path("explicit model"));
    {
        ScopedEnvironment noModel("QCOPILOTS_SEARCH_MODEL_DIR", {});
        const auto defaultModels = RuntimePaths::resolve();
        QCOMPARE(defaultModels.modelDir, QDir(defaultModels.profileDir).filePath("models"));
    }
    {
        ScopedEnvironment invalidHome("QCS_HOME", "relative-home");
        const auto explicitProfile = RuntimePaths::resolve(env.path("explicit profile"));
        QVERIFY(explicitProfile.resolutionError.isEmpty());
        QCOMPARE(explicitProfile.profileDir, fromArguments.profileDir);
        QCOMPARE(explicitProfile.modelDir, fromEnvironment.modelDir);
    }
    QVERIFY(!QFileInfo::exists(env.path("环境 profile")));
    QVERIFY(!QFileInfo::exists(env.path("ignored legacy profile")));
}

void CoreTest::defaultProfileDirectoryIgnoresOverrides()
{
    TestEnvironment env("default-profile-path");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    ScopedEnvironment profile("QCS_HOME", env.path("selected-profile").toUtf8());
    ScopedEnvironment legacy("QCOPILOTS_SEARCH_PROFILE_DIR", env.path("legacy-profile").toUtf8());
    // Check only the computed name. Do not resolve, inspect or create the real
    // platform profile while exercising the default-location contract.
    const QString base = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
    const QString expected = base.isEmpty() ? QString{} : QDir::cleanPath(QDir(base).filePath("qcs"));
    QCOMPARE(RuntimePaths::defaultProfileDirectory(), expected);
    QVERIFY(RuntimePaths::defaultProfileDirectory() != env.path("selected-profile"));
}

void CoreTest::absoluteFilesystemPaths()
{
    QVERIFY(!RuntimePaths::isAbsoluteFilesystemPath({}));
    QVERIFY(!RuntimePaths::isAbsoluteFilesystemPath(QStringLiteral("relative/path")));
    QVERIFY(!RuntimePaths::isAbsoluteFilesystemPath(QStringLiteral(":/resource")));
#ifdef Q_OS_WIN
    for (const QString &path : {QStringLiteral("C:/root"), QStringLiteral("C:\\root"),
                                QStringLiteral("C:/"), QStringLiteral("//server/share"),
                                QStringLiteral("\\\\server\\share\\profile"),
                                QStringLiteral("\\\\?\\C:\\root"),
                                QStringLiteral("\\\\?\\UNC\\server\\share\\profile")})
        QVERIFY2(RuntimePaths::isAbsoluteFilesystemPath(path), qPrintable(path));
    for (const QString &path : {QStringLiteral("/root"), QStringLiteral("\\root"),
                                QStringLiteral("C:relative"), QStringLiteral("C:"),
                                QStringLiteral("//server"), QStringLiteral("//server//share"),
                                QStringLiteral("//?/UNC/server"), QStringLiteral("//./pipe/qcs")})
        QVERIFY2(!RuntimePaths::isAbsoluteFilesystemPath(path), qPrintable(path));
    const QString absolute = QStringLiteral("C:/root");
#else
    const QString absolute = QStringLiteral("/root");
    QVERIFY(RuntimePaths::isAbsoluteFilesystemPath(absolute));
    QVERIFY(RuntimePaths::isAbsoluteFilesystemPath(QStringLiteral("/")));
    QVERIFY(!RuntimePaths::isAbsoluteFilesystemPath(QStringLiteral("C:/root")));
#endif
    QVERIFY(!RuntimePaths::isAbsoluteFilesystemPath(absolute + QChar::Null + QStringLiteral("tail")));
}

void CoreTest::legacyProfileEnvironmentRejected()
{
    TestEnvironment env("legacy-profile-environment");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    ScopedEnvironment profile("QCS_HOME", {});
    ScopedEnvironment legacy("QCOPILOTS_SEARCH_PROFILE_DIR", env.path("legacy-profile").toUtf8());
    const auto paths = RuntimePaths::resolve();
    QVERIFY(paths.resolutionError.contains("QCS_HOME"));
    QVERIFY(paths.resolutionError.contains("QCOPILOTS_SEARCH_PROFILE_DIR"));
    QVERIFY(paths.profileDir.isEmpty());
    QVERIFY(paths.modelDir.isEmpty());
    QVERIFY(paths.configFile.isEmpty());
    QVERIFY(paths.ipcName.isEmpty());
    QString error;
    QVERIFY(!paths.validateProfileLayout(&error));
    QCOMPARE(error, paths.resolutionError);
    QVERIFY(!paths.ensure(&error));
    QCOMPARE(error, paths.resolutionError);
    QVERIFY(!paths.ensureIpcDirectory(&error));
    QCOMPARE(error, paths.resolutionError);
    QVERIFY(!paths.validateIpcDirectory(&error));
    QCOMPARE(error, paths.resolutionError);
    QVERIFY(!QFileInfo::exists(env.path("legacy-profile")));
    const auto explicitProfile = RuntimePaths::resolve(env.path("explicit-profile"));
    QVERIFY(explicitProfile.resolutionError.isEmpty());
    QCOMPARE(explicitProfile.profileDir, env.path("explicit-profile"));
}

void CoreTest::invalidProfileSelection_data()
{
    QTest::addColumn<bool>("fromEnvironment");
    QTest::addColumn<QString>("kind");
    for (bool fromEnvironment : {false, true}) {
        for (const char *kind : {"relative", "file", "child-of-file"
#ifdef Q_OS_WIN
                                , "drive-relative", "drive-only", "current-drive-root",
                                "current-drive-backslash", "resource", "unc-server-only",
                                "device", "incomplete-extended"
#endif
                               }) {
            const QByteArray name = QByteArray(fromEnvironment ? "environment-" : "argument-") + kind;
            QTest::newRow(name.constData()) << fromEnvironment << QString::fromLatin1(kind);
        }
    }
    QTest::newRow("argument-nul") << false << QStringLiteral("nul");
}

void CoreTest::invalidProfileSelection()
{
    QFETCH(bool, fromEnvironment);
    QFETCH(QString, kind);
    TestEnvironment env("invalid-profile");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    QVERIFY(env.writeFile("occupied", "original data"));
    QString selected;
    if (kind == "relative") selected = QStringLiteral("relative-profile");
    else if (kind == "file") selected = env.path("occupied");
    else if (kind == "child-of-file") selected = env.path("occupied/profile");
    else if (kind == "drive-relative") selected = QStringLiteral("C:relative");
    else if (kind == "drive-only") selected = QStringLiteral("C:");
    else if (kind == "current-drive-root") selected = QStringLiteral("/root");
    else if (kind == "current-drive-backslash") selected = QStringLiteral("\\root");
    else if (kind == "resource") selected = QStringLiteral(":/root");
    else if (kind == "unc-server-only") selected = QStringLiteral("//server");
    else if (kind == "device") selected = QStringLiteral("\\\\.\\pipe\\qcs");
    else if (kind == "incomplete-extended") selected = QStringLiteral("\\\\?\\C:relative");
    else selected = env.path("bad") + QChar::Null + QStringLiteral("profile");
    ScopedEnvironment profile("QCS_HOME", fromEnvironment ? selected.toUtf8() : env.path("unused-profile").toUtf8());
    ScopedEnvironment legacy("QCOPILOTS_SEARCH_PROFILE_DIR", {});
    ScopedEnvironment model("QCOPILOTS_SEARCH_MODEL_DIR", env.path("unused-models").toUtf8());
    const QString previous = QDir::currentPath();
    const auto restoreDirectory = qScopeGuard([&] { QDir::setCurrent(previous); });
    QVERIFY(QDir::setCurrent(env.root()));
    const auto paths = RuntimePaths::resolve(fromEnvironment ? QString{} : selected);
    QString error;
    QVERIFY(!paths.validateProfileLayout(&error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!paths.ensure(&error));
    QVERIFY(!error.isEmpty());
    if (kind != "file" && kind != "child-of-file") {
        QVERIFY(!paths.resolutionError.isEmpty());
        QCOMPARE(error, paths.resolutionError);
        QVERIFY(paths.profileDir.isEmpty());
        QVERIFY(paths.configFile.isEmpty());
        QVERIFY(paths.ipcName.isEmpty());
    } else {
        QVERIFY(error.contains("not a directory"));
        QCOMPARE(paths.profileDir, selected);
    }
    QCOMPARE(QDir(env.root()).entryList(QDir::AllEntries | QDir::Hidden | QDir::NoDotAndDotDot),
             QStringList{QStringLiteral("occupied")});
    QFile original(env.path("occupied"));
    QVERIFY(original.open(QIODevice::ReadOnly));
    QCOMPARE(original.readAll(), QByteArray("original data"));
}

#ifdef Q_OS_WIN
void CoreTest::absoluteWindowsProfilePaths()
{
    TestEnvironment env("absolute-profile");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const QString profile = env.path("absolute profile");
    const auto expected = RuntimePaths::resolve(profile);
    for (const QString &value : {profile, QDir::toNativeSeparators(profile),
                                 nativeLockFilePath(profile)}) {
        ScopedEnvironment home("QCS_HOME", value.toUtf8());
        const auto selected = RuntimePaths::resolve();
        QVERIFY2(selected.resolutionError.isEmpty(), qPrintable(selected.resolutionError));
        QCOMPARE(selected.profileDir, expected.profileDir);
        QCOMPARE(selected.ipcName, expected.ipcName);
    }
    QVERIFY(!QFileInfo::exists(profile));
}
#endif

void CoreTest::workingDirectoryIndependence()
{
    TestEnvironment env(QStringLiteral("working-directory"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto before = env.runtimePaths();
    const QString previous = QDir::currentPath();
    struct RestoreDirectory {
        QString previous;
        ~RestoreDirectory() { QDir::setCurrent(previous); }
    } restore{previous};
    QVERIFY(QDir::setCurrent(env.root()));
    const auto after = env.runtimePaths();
    QCOMPARE(after.profileDir, before.profileDir);
    QCOMPARE(after.configFile, before.configFile);
    QCOMPARE(after.ipcName, before.ipcName);
    QString error;
    QVERIFY2(after.ensure(&error), qPrintable(error));
    AppSettings settings;
    QVERIFY2(settings.save(after.configFile, &error), qPrintable(error));
    QVERIFY(QDir::setCurrent(previous));
    AppSettings loaded;
    QVERIFY2(loaded.load(after.configFile, &error), qPrintable(error));
    QCOMPARE(loaded.toJson(), settings.toJson());
}

void CoreTest::longAndUnicodePaths()
{
    TestEnvironment env(QStringLiteral("long-unicode"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    // Keep below legacy Windows MAX_PATH while exercising multiple long
    // components. Extended-length OS capability is tested by platform jobs.
    const QString suffix = QStringLiteral("中文 目录/") + QString(32, u'a') + u'/'
                           + QString(32, u'b');
    const auto paths = RuntimePaths::resolve(env.path(suffix), env.path("models"));
    QString error;
    QVERIFY2(paths.ensure(&error), qPrintable(error));
    AppSettings settings;
    settings.roots = {env.path(QStringLiteral("资料 文件"))};
    QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
    AppSettings loaded;
    QVERIFY2(loaded.load(paths.configFile, &error), qPrintable(error));
    QCOMPARE(loaded.roots, settings.roots);
}

void CoreTest::runtimeCreationFailure()
{
    TestEnvironment env(QStringLiteral("cannot-create"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    QVERIFY(env.writeFile("blocked", "file"));
    const auto paths = RuntimePaths::resolve(env.path("blocked/profile"), env.path("models"));
    QString error;
    QVERIFY(!paths.ensure(&error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!QFileInfo::exists(paths.databaseFile()));
    QVERIFY(!RuntimePaths{}.ensure(&error));
}

void CoreTest::readOnlyModelDirectory()
{
    TestEnvironment env(QStringLiteral("readonly-models"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = env.runtimePaths();
    QString error;
    QVERIFY2(paths.ensure(&error), qPrintable(error));
    QVERIFY(QFile::setPermissions(paths.modelDir, QFileDevice::ReadOwner | QFileDevice::ExeOwner));
    const bool created = paths.ensure(&error);
    // Restore before any assertion exits so cleanup also works on Unix.
    QVERIFY(QFile::setPermissions(paths.modelDir, QFileDevice::ReadOwner | QFileDevice::WriteOwner
                                                | QFileDevice::ExeOwner));
    QVERIFY2(created, qPrintable(error));
}

void CoreTest::temporaryRootsUnique()
{
    TestEnvironment first(QStringLiteral("parallel"));
    TestEnvironment second(QStringLiteral("parallel"));
    QVERIFY2(first.isValid(), qPrintable(first.error()));
    QVERIFY2(second.isValid(), qPrintable(second.error()));
    QVERIFY(first.root() != second.root());
    QVERIFY(first.runtimePaths().ipcName != second.runtimePaths().ipcName);
    QVERIFY(first.path("../escape").isEmpty());
    QVERIFY(first.path(QDir::rootPath()).isEmpty());
    TestEnvironment longLabel(QString(100, u'x'));
    QVERIFY2(longLabel.isValid(), qPrintable(longLabel.error()));
    QCOMPARE(QFileInfo(longLabel.root()).fileName().size(), qsizetype(6 + 16 + 1 + 6));
}

void CoreTest::customTemporaryRoot()
{
    TestEnvironment parent(QStringLiteral("custom-root"));
    QVERIFY2(parent.isValid(), qPrintable(parent.error()));
    const QString base = parent.path(QStringLiteral("中文 自定义临时路径"));
    ScopedEnvironment variable("QCOPILOTS_SEARCH_TEST_ROOT", base.toUtf8());
    TestEnvironment child(QStringLiteral("custom"));
    QVERIFY2(child.isValid(), qPrintable(child.error()));
    QCOMPARE(QFileInfo(child.root()).absolutePath(), base);
}

void CoreTest::temporaryFailureDoesNotFallback()
{
    TestEnvironment env(QStringLiteral("blocked-root"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    QVERIFY(env.writeFile("file", "not a directory"));
    ScopedEnvironment variable("QCOPILOTS_SEARCH_TEST_ROOT", env.path("file/child").toUtf8());
    TestEnvironment failed;
    QVERIFY(!failed.isValid());
    QVERIFY(!failed.error().isEmpty());
    QVERIFY(failed.root().isEmpty());
    QVERIFY(failed.path("file").isEmpty());
    QVERIFY(failed.runtimePaths().profileDir.isEmpty());
}

void CoreTest::cleanupBoundaries()
{
    TestEnvironment env(QStringLiteral("cleanup"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const QString root = env.root();
    QVERIFY(env.writeFile("nested/fixture.txt", "fixture"));
    QString error;
    QVERIFY2(env.cleanup(&error), qPrintable(error));
    QVERIFY(!QFileInfo::exists(root));
    QVERIFY(env.cleanup(&error));
}

void CoreTest::cleanupRejectsMovedRoot()
{
    TestEnvironment env(QStringLiteral("moved-root"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const QString original = env.root();
    const QString moved = original + QStringLiteral("-moved");
    QVERIFY(env.writeFile("keep.txt", "must survive the refused cleanup"));
    QVERIFY(QDir().rename(original, moved));
    QString error;
    const bool removed = env.cleanup(&error);
    // Always restore ownership before an assertion can leave the scope.
    const bool stillPresent = QFileInfo::exists(QDir(moved).filePath("keep.txt"));
    const bool restored = QDir().rename(moved, original);
    QVERIFY(restored);
    QVERIFY(!removed);
    QVERIFY(!error.isEmpty());
    QVERIFY(stillPresent);
    QVERIFY2(env.cleanup(&error), qPrintable(error));
}

void CoreTest::cleanupRejectsReplacedRoot()
{
    TestEnvironment parent("replaced-parent");
    QVERIFY2(parent.isValid(), qPrintable(parent.error()));
    ScopedEnvironment root("QCOPILOTS_SEARCH_TEST_ROOT", parent.root().toUtf8());
    TestEnvironment env("replaced");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto original = env.root();
    const auto moved = original + "-saved";
    QVERIFY(QDir().rename(original, moved));
    QVERIFY(QDir().mkpath(original));
    QFile marker(QDir(original).filePath("unowned.txt"));
    QVERIFY(marker.open(QIODevice::WriteOnly));
    QCOMPARE(marker.write("keep"), qint64(4));
    marker.close();
    QString error;
    const bool cleaned = env.cleanup(&error);
    const bool preserved = QFileInfo::exists(marker.fileName());
    // Restore the owned directory before reporting any assertion failure.
    const bool removedReplacement = QFile::remove(marker.fileName()) && QDir().rmdir(original);
    const bool restored = removedReplacement && QDir().rename(moved, original);
    QVERIFY(restored);
    QVERIFY(!cleaned);
    QVERIFY(error.contains("replaced"));
    QVERIFY(preserved);
    QVERIFY2(env.cleanup(&error), qPrintable(error));
}

void CoreTest::temporaryWriteFailure()
{
    TestEnvironment env("write-failure");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    QVERIFY(env.writeFile("blocked", "original"));
    QString error;
    QVERIFY(!env.writeFile("blocked/child", "must fail", &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(QDir().mkpath(env.path("directory")));
    error.clear();
    QVERIFY(!env.writeFile("directory", "must fail", &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!env.writeFile("../escape", "must fail", &error));
    QFile original(env.path("blocked"));
    QVERIFY(original.open(QIODevice::ReadOnly));
    QCOMPARE(original.readAll(), QByteArray("original"));
}

void CoreTest::destructorCleanupFailuresAreObservable()
{
    TestEnvironment parent("cleanup-report");
    QVERIFY2(parent.isValid(), qPrintable(parent.error()));
    QString original;
    QString moved;
    {
        TestTemporaryDir child(parent.path("qcs-t-case-XXXXXX"));
        QVERIFY2(child.isValid(), qPrintable(child.errorString()));
        original = child.path();
        moved = original + "-moved";
        QVERIFY(QDir().rename(original, moved));
        // Destruction must record the refused cleanup, rather than warn only.
    }
    const auto error = takeCleanupErrors();
    const bool present = QFileInfo(moved).isDir();
    QVERIFY(QDir().rename(moved, original));
    QVERIFY(error.contains("Refusing to clean"));
    QVERIFY(error.contains(original));
    QVERIFY(present);
    QVERIFY(takeCleanupErrors().isEmpty());
}

#ifdef Q_OS_WIN
void CoreTest::cleanupRequiresClosedWindowsHandles()
{
    TestEnvironment env("open-handle");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    QVERIFY(env.writeFile("held.txt", "held by a native handle"));
    const auto native = nativeLockFilePath(env.path("held.txt"));
    HANDLE handle = CreateFileW(reinterpret_cast<LPCWSTR>(native.utf16()), GENERIC_READ,
        FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    QVERIFY(handle != INVALID_HANDLE_VALUE);
    QString error;
    const bool cleaned = env.cleanup(&error);
    const bool preserved = QFileInfo::exists(env.path("held.txt"));
    CloseHandle(handle);
    QVERIFY(!cleaned);
    QVERIFY(!error.isEmpty());
    QVERIFY(preserved);
    QVERIFY2(env.cleanup(&error), qPrintable(error));
    QVERIFY(env.cleanup(&error));
}
#endif

void CoreTest::legacyProfileRejected_data()
{
    QTest::addColumn<QString>("relative");
    for (const auto &path : {"qcopilots-search-settings.json", "qcopilots-search-service.lock",
                            "qcopilots-search-ipc-token", "index/qcopilots-search.sqlite3",
                            "index/qcopilots-search.sqlite3-wal", "index/qcopilots-search.sqlite3-shm",
                            "index/qcopilots-search-progress.json", "index/qcopilots-search-ann-orphan"})
        QTest::newRow(path) << QString::fromLatin1(path);
}

void CoreTest::legacyProfileRejected()
{
    QFETCH(QString, relative);
    TestEnvironment env("legacy-profile");
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = env.runtimePaths();
    QVERIFY(env.writeFile("profile/" + relative, "old-owned-data"));
    QString error;
    QVERIFY(!paths.validateProfileLayout(&error));
    QVERIFY(error.startsWith("legacy_profile_layout:"));
    QVERIFY(!paths.ensure(&error));
    QVERIFY(error.startsWith("legacy_profile_layout:"));
    QVERIFY(!QFileInfo::exists(paths.configFile));
    QVERIFY(!QFileInfo::exists(paths.databaseFile()));
    QVERIFY(!QFileInfo::exists(paths.cacheDir));
    QVERIFY(!QFileInfo::exists(paths.tempDir));
    QVERIFY(!QFileInfo::exists(paths.logDir));
    QVERIFY(!QFileInfo::exists(paths.modelDir));
    QFile old(env.path("profile/" + relative));
    QVERIFY(old.open(QIODevice::ReadOnly));
    QCOMPARE(old.readAll(), QByteArray("old-owned-data"));
}

void CoreTest::settingsRoundTrip()
{
    TestEnvironment env(QStringLiteral("settings"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto paths = env.runtimePaths();
    QString error;
    QVERIFY2(paths.ensure(&error), qPrintable(error));
    AppSettings first;
    QVERIFY(first.load(paths.configFile, &error));
    QVERIFY(first.roots.isEmpty());
    first.roots = {env.path(QStringLiteral("未挂载 目录"))};
    first.excludes = {env.path(QStringLiteral("未挂载 目录/排除"))};
    first.codeRoots = {env.path(QStringLiteral("独立 代码"))};
    first.codeExcludes = {env.path(QStringLiteral("独立 代码/build"))};
    first.language = QStringLiteral("zh_CN");
    first.includeCode = true;
    first.indexingPaused = true;
    first.editorCommand = QStringLiteral("editor --line {line} {path}");
    first.pdfOpener.preset = QStringLiteral("custom");
    first.pdfOpener.program = env.path(QStringLiteral("PDF 阅读器/viewer"));
    first.pdfOpener.arguments = {QStringLiteral("--page"), QStringLiteral("{page}"),
                                 QStringLiteral("{path}"), QString{}};
    first.mediaOpener.preset = QStringLiteral("mpv");
    first.mediaOpener.program = env.path(QStringLiteral("播放器/mpv"));
    first.mediaOpener.arguments = {QStringLiteral("--start={seconds}"), QStringLiteral("{path}")};
    QCOMPARE(first.toJson().value(QStringLiteral("version")).toInt(), 2);
    QVERIFY2(first.save(paths.configFile, &error), qPrintable(error));
    AppSettings second;
    QVERIFY2(second.load(paths.configFile, &error), qPrintable(error));
    QCOMPARE(second.toJson(), first.toJson());
    QCOMPARE(second.codeRoots, first.codeRoots);
    QCOMPARE(second.codeExcludes, first.codeExcludes);
    QVERIFY(second.codeRoots != second.roots);
    QCOMPARE(second.pdfOpener.arguments.last(), QString{});
    QVERIFY(!second.needsMigration());
    QVERIFY(!QFileInfo::exists(env.path("registry")));
}

void CoreTest::generalFileRulesDefaults()
{
    const AppSettings settings;
    const auto rules = settings.toJson().value(QStringLiteral("generalFileRules")).toObject();
    QCOMPARE(rules.size(), 10);
    for (const auto &key : {"excludeHidden", "excludeProjects", "excludeNamedDirectories", "excludeMediaDatasets"}) {
        QVERIFY(rules.value(QLatin1StringView(key)).isBool());
        QVERIFY(!rules.value(QLatin1StringView(key)).toBool());
    }
    QCOMPARE(settings.generalFileRules.mediaDatasetMinimumFiles, 300);
    QCOMPARE(settings.generalFileRules.mediaDatasetMachineNamedPercent, 70);
    QVERIFY(settings.generalFileRules.projectFileNames.contains(QStringLiteral(".git")));
    QVERIFY(settings.generalFileRules.projectFileNames.contains(QStringLiteral("CMakeLists.txt")));
    QVERIFY(settings.generalFileRules.directoryNames.contains(QStringLiteral("node_modules")));
    QVERIFY(settings.generalFileRules.directorySuffixes.contains(QStringLiteral("noindex")));
}

void CoreTest::generalFileRulesUpgrade()
{
    TestEnvironment env(QStringLiteral("general-rules-upgrade"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    AppSettings original;
    original.roots = {env.path("documents")};
    original.codeRoots = {env.path("separate-code")};
    original.codeExcludes = {env.path("separate-code/build")};
    original.includeCode = true;
    const auto settingsPath = env.path("settings.json");
    QString error;
    for (const bool partial : {false, true}) {
        auto json = original.toJson();
        if (partial) {
            json[QStringLiteral("generalFileRules")] = QJsonObject{{"excludeHidden", true}};
        } else {
            json.remove(QStringLiteral("generalFileRules"));
        }
        const auto bytes = QJsonDocument(json).toJson();
        QVERIFY(env.writeFile("settings.json", bytes));
        AppSettings loaded;
        QVERIFY2(loaded.load(settingsPath, &error), qPrintable(error));
        QVERIFY(loaded.needsMigration());
        QCOMPARE(loaded.codeRoots, original.codeRoots);
        QCOMPARE(loaded.codeExcludes, original.codeExcludes);
        QCOMPARE(loaded.generalFileRules.excludeHidden, partial);
        QVERIFY(!loaded.generalFileRules.excludeProjects);
        QCOMPARE(loaded.generalFileRules.mediaDatasetMinimumFiles, 300);
        QFile file(settingsPath);
        QVERIFY(file.open(QIODevice::ReadOnly));
        QCOMPARE(file.readAll(), bytes); // Load never writes before the profile lock.
        file.close();
        QVERIFY2(loaded.save(settingsPath, &error), qPrintable(error));
        AppSettings reloaded;
        QVERIFY2(reloaded.load(settingsPath, &error), qPrintable(error));
        QVERIFY(!reloaded.needsMigration());
        QCOMPARE(reloaded.toJson(), loaded.toJson());
        QCOMPARE(reloaded.toJson().value("version").toInt(), 2);
        QCOMPARE(reloaded.toJson().value("generalFileRules").toObject().size(), 10);
    }
}

void CoreTest::generalFileRulesRoundTrip()
{
    TestEnvironment env(QStringLiteral("general-rules-round-trip"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    AppSettings original;
    auto &rules = original.generalFileRules;
    rules.excludeHidden = true;
    rules.excludeProjects = true;
    rules.excludeNamedDirectories = true;
    rules.excludeMediaDatasets = true;
    rules.projectFileNames = {QStringLiteral("自定义 标记.json")};
    rules.projectDirectorySuffixes = {QStringLiteral("myproject")};
    rules.directoryNames = {QStringLiteral("custom-cache"), QStringLiteral("缓存 文件")};
    rules.directorySuffixes = {};
    rules.mediaDatasetMinimumFiles = 47;
    rules.mediaDatasetMachineNamedPercent = 100;
    const auto path = env.path("settings.json");
    QString error;
    QVERIFY2(original.save(path, &error), qPrintable(error));
    AppSettings loaded;
    QVERIFY2(loaded.load(path, &error), qPrintable(error));
    QCOMPARE(loaded.toJson(), original.toJson());
    QCOMPARE(loaded.fingerprint(), original.fingerprint());
    rules.mediaDatasetMinimumFiles = 1;
    rules.mediaDatasetMachineNamedPercent = 0;
    QVERIFY2(AppSettings::fromJson(original.toJson(), &loaded, &error), qPrintable(error));
    QCOMPARE(loaded.generalFileRules.toJson(), rules.toJson());
}

void CoreTest::generalFileRulesRejectInvalid_data()
{
    QTest::addColumn<QJsonValue>("value");
    QTest::newRow("not-object") << QJsonValue(true);
    QTest::newRow("null") << QJsonValue(QJsonValue::Null);
    QTest::newRow("misspelled-rule") << QJsonValue(QJsonObject{{"excludeProject", true}});
    for (const auto &key : {"excludeHidden", "excludeProjects", "excludeNamedDirectories", "excludeMediaDatasets"}) {
        QTest::newRow(key) << QJsonValue(QJsonObject{{QLatin1StringView(key), "true"}});
    }
    const QString nul = QStringLiteral("bad") + QChar::Null + QStringLiteral("name");
    for (const auto &key : {"projectFileNames", "projectDirectorySuffixes", "directoryNames", "directorySuffixes"}) {
        const QByteArray prefix(key);
        const QString field = QString::fromLatin1(key);
        QTest::newRow((prefix + "-array-required").constData()) << QJsonValue(QJsonObject{{field, "cache"}});
        QTest::newRow((prefix + "-string-required").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{true}}});
        QTest::newRow((prefix + "-empty").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{""}}});
        QTest::newRow((prefix + "-blank").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{" \t "}}});
        QTest::newRow((prefix + "-slash").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{"one/two"}}});
        QTest::newRow((prefix + "-backslash").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{"one\\two"}}});
        QTest::newRow((prefix + "-nul").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{nul}}});
        QTest::newRow((prefix + "-dot").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{"."}}});
        QTest::newRow((prefix + "-dot-dot").constData()) << QJsonValue(QJsonObject{{field, QJsonArray{".."}}});
    }
    for (const auto &key : {"projectDirectorySuffixes", "directorySuffixes"}) {
        QTest::newRow((QByteArray(key) + "-leading-dot").constData())
            << QJsonValue(QJsonObject{{QLatin1StringView(key), QJsonArray{".app"}}});
    }
    for (const auto &key : {"mediaDatasetMinimumFiles", "mediaDatasetMachineNamedPercent"}) {
        const QByteArray prefix(key);
        const QString field = QString::fromLatin1(key);
        QTest::newRow((prefix + "-integer-required").constData()) << QJsonValue(QJsonObject{{field, 1.5}});
        QTest::newRow((prefix + "-number-required").constData()) << QJsonValue(QJsonObject{{field, "10"}});
        QTest::newRow((prefix + "-negative").constData()) << QJsonValue(QJsonObject{{field, -1}});
        QTest::newRow((prefix + "-overflow").constData()) << QJsonValue(QJsonObject{{field, 2147483648.0}});
    }
    QTest::newRow("minimum-zero") << QJsonValue(QJsonObject{{"mediaDatasetMinimumFiles", 0}});
    QTest::newRow("percent-over-100") << QJsonValue(QJsonObject{{"mediaDatasetMachineNamedPercent", 101}});
}

void CoreTest::generalFileRulesRejectInvalid()
{
    QFETCH(QJsonValue, value);
    TestEnvironment env(QStringLiteral("general-rules-invalid"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    const auto path = env.path("settings.json");
    AppSettings settings;
    settings.language = QStringLiteral("zh_CN");
    settings.generalFileRules.excludeHidden = true;
    settings.generalFileRules.directoryNames = {QStringLiteral("retained")};
    const auto before = settings.toJson();
    auto invalid = AppSettings{}.toJson();
    invalid[QStringLiteral("generalFileRules")] = value;
    const auto bytes = QJsonDocument(invalid).toJson();
    QVERIFY(env.writeFile("settings.json", bytes));
    QString error;
    QVERIFY(!settings.load(path, &error));
    QVERIFY2(error.contains(QStringLiteral("generalFileRules")), qPrintable(error));
    QCOMPARE(settings.toJson(), before);
    QFile file(path);
    QVERIFY(file.open(QIODevice::ReadOnly));
    QCOMPARE(file.readAll(), bytes);
}

void CoreTest::generalFileRulesFingerprint()
{
    const AppSettings original;
    const auto fingerprint = original.fingerprint();
    const auto defaults = original.generalFileRules.toJson();
    for (auto entry = defaults.begin(); entry != defaults.end(); ++entry) {
        auto changed = defaults;
        if (entry.value().isBool()) changed[entry.key()] = true;
        else if (entry.value().isArray()) changed[entry.key()] = QJsonArray{"custom"};
        else changed[entry.key()] = entry.value().toInt() + 1;
        auto json = original.toJson();
        json[QStringLiteral("generalFileRules")] = changed;
        AppSettings settings;
        QString error;
        QVERIFY2(AppSettings::fromJson(json, &settings, &error), qPrintable(error));
        QVERIFY2(settings.fingerprint() != fingerprint, qPrintable(entry.key()));
    }
    QString error;
    QVERIFY(!GeneralFileRules::fromJson({}, nullptr, &error));
    QVERIFY(!error.isEmpty());
}

void CoreTest::settingsV1Migration_data()
{
    QTest::addColumn<bool>("includeCode");
    QTest::newRow("code-enabled") << true;
    QTest::newRow("code-disabled") << false;
}

void CoreTest::settingsV1Migration()
{
    QFETCH(bool, includeCode);
    TestEnvironment env(QStringLiteral("settings-v1"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    AppSettings original;
    original.roots = {env.path(QStringLiteral("原始 资料"))};
    original.excludes = {env.path(QStringLiteral("原始 资料/排除"))};
    original.includeCode = includeCode;
    original.language = QStringLiteral("zh_CN");
    auto legacy = original.toJson();
    legacy[QStringLiteral("version")] = 1;
    legacy.remove(QStringLiteral("codeRoots"));
    legacy.remove(QStringLiteral("codeExcludes"));
    legacy.remove(QStringLiteral("pdfOpener"));
    legacy.remove(QStringLiteral("mediaOpener"));
    const QByteArray legacyBytes = QJsonDocument(legacy).toJson(QJsonDocument::Indented);
    QVERIFY(env.writeFile(QStringLiteral("settings.json"), legacyBytes));

    AppSettings migrated;
    QString error;
    QVERIFY2(migrated.load(env.path(QStringLiteral("settings.json")), &error), qPrintable(error));
    QVERIFY(migrated.needsMigration());
    QCOMPARE(migrated.roots, original.roots);
    QCOMPARE(migrated.excludes, original.excludes);
    QCOMPARE(migrated.language, original.language);
    QCOMPARE(migrated.codeRoots, includeCode ? original.roots : QStringList{});
    QCOMPARE(migrated.codeExcludes, includeCode ? original.excludes : QStringList{});
    QCOMPARE(migrated.pdfOpener.preset, QStringLiteral("system"));
    QCOMPARE(migrated.mediaOpener.preset, QStringLiteral("system"));

    QFile file(env.path(QStringLiteral("settings.json")));
    QVERIFY(file.open(QIODevice::ReadOnly));
    QCOMPARE(file.readAll(), legacyBytes); // Loading must not write before the profile lock is held.
    file.close();
    QVERIFY2(migrated.save(file.fileName(), &error), qPrintable(error));
    QVERIFY(file.open(QIODevice::ReadOnly));
    const auto saved = QJsonDocument::fromJson(file.readAll()).object();
    file.close();
    QCOMPARE(saved.value(QStringLiteral("version")).toInt(), 2);
    AppSettings reloaded;
    QVERIFY2(reloaded.load(file.fileName(), &error), qPrintable(error));
    QVERIFY(!reloaded.needsMigration());
    QCOMPARE(reloaded.toJson(), migrated.toJson());
}

void CoreTest::settingsRejectCorruption()
{
    TestEnvironment env(QStringLiteral("bad-json"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    AppSettings settings;
    settings.roots = {env.path("original")};
    QVERIFY(env.writeFile("settings.json", "{broken"));
    QString error;
    QVERIFY(!settings.load(env.path("settings.json"), &error));
    QVERIFY(!error.isEmpty());
    QCOMPARE(settings.roots, QStringList{env.path("original")});
    QVERIFY(env.writeFile("settings.json", QByteArray(1024 * 1024 + 1, ' ')));
    QVERIFY(!settings.load(env.path("settings.json"), &error));
    QVERIFY(error.contains("1 MiB"));
}

void CoreTest::settingsRejectUnsupportedTypes()
{
    AppSettings original;
    AppSettings parsed;
    parsed.language = QStringLiteral("zh_CN");
    const auto before = parsed.toJson();
    QString error;
    auto json = original.toJson();
    json["version"] = 3;
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    QCOMPARE(parsed.toJson(), before);
    json = original.toJson();
    json["includeCode"] = "true";
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    json = original.toJson();
    json["roots"] = QJsonArray{"relative/directory"};
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    for (const auto &key : {QStringLiteral("codeRoots"), QStringLiteral("codeExcludes")}) {
        json = original.toJson();
        json[key] = QJsonArray{"relative/directory"};
        QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
        QCOMPARE(parsed.toJson(), before);
        json[key] = true;
        QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
        QCOMPARE(parsed.toJson(), before);
    }
    json = original.toJson();
    json["language"] = "unknown";
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    json = original.toJson();
    json["editorCommand"] = QStringLiteral("editor") + QChar::Null + QStringLiteral("{path}");
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    QCOMPARE(parsed.toJson(), before);
    QVERIFY(!AppSettings::fromJson(original.toJson(), nullptr, &error));
}

void CoreTest::settingsRejectInvalidOpeners_data()
{
    QTest::addColumn<QString>("opener");
    QTest::addColumn<QJsonValue>("value");
    const QString nul = QStringLiteral("prefix") + QChar::Null + QStringLiteral("suffix");
    for (const QString &opener : {QStringLiteral("pdfOpener"), QStringLiteral("mediaOpener")}) {
        const QByteArray prefix = opener.toLatin1();
        QTest::newRow((prefix + "-not-object").constData()) << opener << QJsonValue(true);
        QTest::newRow((prefix + "-unknown-preset").constData())
            << opener << QJsonValue(QJsonObject{{"preset", "unknown"}});
        QTest::newRow((prefix + "-preset-type").constData())
            << opener << QJsonValue(QJsonObject{{"preset", true}});
        QTest::newRow((prefix + "-program-type").constData())
            << opener << QJsonValue(QJsonObject{{"program", 42}});
        QTest::newRow((prefix + "-program-nul").constData())
            << opener << QJsonValue(QJsonObject{{"program", nul}});
        QTest::newRow((prefix + "-custom-without-program").constData())
            << opener << QJsonValue(QJsonObject{{"preset", "custom"}});
        QTest::newRow((prefix + "-custom-blank-program").constData())
            << opener << QJsonValue(QJsonObject{{"preset", "custom"}, {"program", " \t "}});
        QTest::newRow((prefix + "-arguments-type").constData())
            << opener << QJsonValue(QJsonObject{{"arguments", "--page {page}"}});
        QTest::newRow((prefix + "-argument-type").constData())
            << opener << QJsonValue(QJsonObject{{"arguments", QJsonArray{"{path}", 42}}});
        QTest::newRow((prefix + "-argument-nul").constData())
            << opener << QJsonValue(QJsonObject{{"arguments", QJsonArray{nul}}});
    }
    QTest::newRow("pdf-rejects-media-preset")
        << QStringLiteral("pdfOpener") << QJsonValue(QJsonObject{{"preset", "mpv"}});
    QTest::newRow("media-rejects-pdf-preset")
        << QStringLiteral("mediaOpener") << QJsonValue(QJsonObject{{"preset", "sumatra"}});
}

void CoreTest::settingsRejectInvalidOpeners()
{
    QFETCH(QString, opener);
    QFETCH(QJsonValue, value);
    AppSettings parsed;
    parsed.language = QStringLiteral("zh_CN");
    parsed.pdfOpener.preset = QStringLiteral("sumatra");
    parsed.mediaOpener.preset = QStringLiteral("mpv");
    auto legacy = parsed.toJson();
    legacy[QStringLiteral("version")] = 1;
    QString error;
    QVERIFY2(AppSettings::fromJson(legacy, &parsed, &error), qPrintable(error));
    QVERIFY(parsed.needsMigration());
    const auto before = parsed.toJson();
    auto invalid = AppSettings{}.toJson();
    invalid[opener] = value;
    QVERIFY(!AppSettings::fromJson(invalid, &parsed, &error));
    QVERIFY(!error.isEmpty());
    QCOMPARE(parsed.toJson(), before);
    QVERIFY(parsed.needsMigration());
}

void CoreTest::settingsOpenerPresets()
{
    for (const QString &pdf : {QStringLiteral("system"), QStringLiteral("sumatra"),
                               QStringLiteral("okular"), QStringLiteral("skim"),
                               QStringLiteral("custom")}) {
        for (const QString &media : {QStringLiteral("system"), QStringLiteral("mpv"),
                                     QStringLiteral("custom")}) {
            AppSettings source;
            source.pdfOpener.preset = pdf;
            source.pdfOpener.program = QStringLiteral("pdf-viewer");
            source.pdfOpener.arguments = {QString{}, QStringLiteral("{path}")};
            source.mediaOpener.preset = media;
            source.mediaOpener.program = QStringLiteral("media-viewer");
            source.mediaOpener.arguments = {QStringLiteral("{path}"), QString{}};
            AppSettings parsed;
            QString error;
            QVERIFY2(AppSettings::fromJson(source.toJson(), &parsed, &error), qPrintable(error));
            QCOMPARE(parsed.toJson(), source.toJson());
        }
    }
}

void CoreTest::settingsAtomicFailure()
{
    TestEnvironment env(QStringLiteral("settings-write"));
    QVERIFY2(env.isValid(), qPrintable(env.error()));
    AppSettings settings;
    QString error;
    QVERIFY(!settings.save(env.path("missing-parent/settings.json"), &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(settings.save(env.path("settings.json"), &error));
    QFile before(env.path("settings.json"));
    QVERIFY(before.open(QIODevice::ReadOnly));
    const QByteArray bytes = before.readAll();
    before.close();
    settings.language = "unsupported";
    QVERIFY(!settings.save(env.path("settings.json"), &error));
    QVERIFY(before.open(QIODevice::ReadOnly));
    QCOMPARE(before.readAll(), bytes);
}

void CoreTest::settingsFingerprint()
{
    AppSettings settings;
    const QString original = settings.fingerprint();
    settings.language = "zh_CN";
    settings.minimizeToTray = false;
    settings.pdfOpener.preset = QStringLiteral("sumatra");
    settings.pdfOpener.program = QStringLiteral("pdf-viewer");
    settings.pdfOpener.arguments = {QStringLiteral("{path}")};
    settings.mediaOpener.preset = QStringLiteral("mpv");
    settings.mediaOpener.program = QStringLiteral("media-viewer");
    settings.mediaOpener.arguments = {QStringLiteral("{path}")};
    QCOMPARE(settings.fingerprint(), original);
    settings.codeRoots = {QDir::rootPath()};
    const QString withCodeRoots = settings.fingerprint();
    QVERIFY(withCodeRoots != original);
    settings.codeExcludes = {QDir::rootPath()};
    QVERIFY(settings.fingerprint() != withCodeRoots);
    settings.codeRoots.clear();
    settings.codeExcludes.clear();
    QCOMPARE(settings.fingerprint(), original);
    settings.includeCode = true;
    QVERIFY(settings.fingerprint() != original);
}

void CoreTest::segmentSerialization()
{
    Segment segment;
    segment.id = 9007199254740993LL;
    segment.path = QStringLiteral("路径/文档.pdf");
    segment.title = QStringLiteral("中文标题");
    segment.kind = "pdf";
    segment.modality = "text";
    segment.imageBudget = 280;
    segment.text = QStringLiteral("中文和 English");
    segment.page = 3;
    segment.lineStart = 12;
    segment.lineEnd = 25;
    segment.cell = 2;
    segment.startMs = 1234567890;
    segment.endMs = 1234567990;
    segment.embedding = {0.5F, -0.25F};
    const auto transferred = QJsonDocument::fromJson(QJsonDocument(segment.toJson()).toJson()).object();
    const Segment result = Segment::fromJson(transferred);
    QCOMPARE(result.toJson(), segment.toJson());
    QCOMPARE(result.id, segment.id);
    QVERIFY(!segment.toJson(false).contains("embedding"));
}

void CoreTest::invalidVectors()
{
    QJsonObject object;
    object["embedding"] = QJsonArray{1.0, "bad", 2.0};
    QVERIFY(Segment::fromJson(object).embedding.isEmpty());
    QJsonArray tooMany;
    for (int i = 0; i <= embeddingDimensions; ++i)
        tooMany.append(0.0);
    object["embedding"] = tooMany;
    QVERIFY(Segment::fromJson(object).embedding.isEmpty());
    object["embedding"] = QJsonArray{std::numeric_limits<double>::max()};
    QVERIFY(Segment::fromJson(object).embedding.isEmpty());
}

void CoreTest::searchHitSerialization()
{
    SearchHit hit;
    hit.segmentId = 9007199254740993LL;
    hit.sourceVersion = QStringLiteral("captured-version-at-query-snapshot");
    hit.path = QStringLiteral("资料/视频.mp4");
    hit.title = "video";
    hit.kind = "video";
    hit.text = "caption";
    hit.matchSource = "semantic";
    hit.score = 0.625;
    hit.startMs = 91000;
    hit.endMs = 103000;
    hit.page = 4;
    hit.cell = 2;
    hit.lineStart = 13;
    hit.lineEnd = 16;
    QCOMPARE(SearchHit::fromJson(hit.toJson()).toJson(), hit.toJson());
}

void CoreTest::progressSerialization()
{
    IndexProgress progress;
    progress.state = "indexing";
    progress.currentPath = QStringLiteral("中文/文件.txt");
    progress.total = 100000;
    progress.completed = 42;
    progress.failed = 3;
    progress.segments = 1000000;
    progress.error = "example";
    const auto json = progress.toJson();
    QCOMPARE(json.value("total").toInteger(), progress.total);
    QCOMPARE(json.value("completed").toInteger(), progress.completed);
    QCOMPARE(json.value("segments").toInteger(), progress.segments);
    QCOMPARE(json.value("error").toString(), progress.error);
}

void CoreTest::pipelineContracts()
{
    const auto manifest = ModelFiles::manifest();
    const auto embedding = embeddingPipelineContract();
    QCOMPARE(embedding.value("textSha256").toString().toLatin1(), manifest.at(0).sha256);
    QCOMPARE(embedding.value("mediaSha256").toString().toLatin1(), manifest.at(1).sha256);
    QVERIFY(manifest.at(0).url.toString().contains(embedding.value("modelRevision").toString()));
    QVERIFY(embeddingPipelineFingerprint().startsWith("qcopilots-search-embedding-v1:"));
    QCOMPARE(embeddingPipelineFingerprint(), QStringLiteral("qcopilots-search-embedding-v1:e2eb31c724adc082a252eede5996a4d1edf435ad72538a2e9d9372a3b8f9560e"));
    QCOMPARE(extractionPipelineFingerprint(), QStringLiteral("qcopilots-search-extraction-v7:ee56376c71b1fa8a085547d7d4a77da6a55efbfa121c0a1c1dd576040b5393af"));
    auto changed = embedding;
    changed["queryPrompt"] = "different prompt";
    QVERIFY(pipelineContractFingerprint("qcopilots-search-embedding-v1", changed)
            != embeddingPipelineFingerprint());
    QVERIFY(extractionPipelineContract().contains("tessdataFast"));
    QVERIFY(extractionPipelineContract().contains("legacyDoc"));
    QVERIFY(extractionPipelineFingerprint() != embeddingPipelineFingerprint());
}

QTEST_GUILESS_MAIN(CoreTest)
#include "core_test.moc"
