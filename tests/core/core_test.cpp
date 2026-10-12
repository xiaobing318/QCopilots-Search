#include "core/app_settings.h"
#include "core/runtime_paths.h"
#include "core/ipc_directory.h"
#include "core/pipeline_manifest.h"
#include "core/model_files.h"
#include "core/types.h"
#include "support/test_environment.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QLocalServer>
#include <QLocalSocket>
#include <QtTest>
#include <limits>
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
    void explicitRuntimePaths();
    void runtimePathIdentity();
    void ipcIgnoresLongTemporaryDirectory();
#ifdef Q_OS_UNIX
    void privateIpcDirectoryRejectsUnsafeEntries();
#endif
    void environmentOverrides();
    void workingDirectoryIndependence();
    void longAndUnicodePaths();
    void runtimeCreationFailure();
    void readOnlyModelDirectory();
    void temporaryRootsUnique();
    void customTemporaryRoot();
    void temporaryFailureDoesNotFallback();
    void cleanupBoundaries();
    void cleanupRejectsMovedRoot();
    void settingsRoundTrip();
    void settingsRejectCorruption();
    void settingsRejectUnsupportedTypes();
    void settingsAtomicFailure();
    void settingsFingerprint();
    void segmentSerialization();
    void invalidVectors();
    void searchHitSerialization();
    void progressSerialization();
    void pipelineContracts();
};

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
    QVERIFY(QFileInfo(paths.ipcName).fileName().startsWith(QStringLiteral("qcopilots-search-")));
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
    const auto link = env.path("private-link");
    QVERIFY(::symlink(QFile::encodeName(directory).constData(), QFile::encodeName(link).constData()) == 0);
    QVERIFY(!detail::preparePrivateIpcDirectory(link, false, &error));
    QVERIFY(!detail::preparePrivateIpcDirectory(link, true, &error));
    QVERIFY(QFileInfo(link).isSymLink());
    QVERIFY(env.writeFile("not-directory", "preserve existing file"));
    QVERIFY(!detail::preparePrivateIpcDirectory(env.path("not-directory"), true, &error));
    QFile regular(env.path("not-directory"));
    QVERIFY(regular.open(QIODevice::ReadOnly));
    QCOMPARE(regular.readAll(), QByteArray("preserve existing file"));
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
    ScopedEnvironment profile("QCOPILOTS_SEARCH_PROFILE_DIR", env.path("env profile").toUtf8());
    ScopedEnvironment model("QCOPILOTS_SEARCH_MODEL_DIR", env.path("env model").toUtf8());
    const auto fromEnvironment = RuntimePaths::resolve();
    QCOMPARE(fromEnvironment.profileDir, env.path("env profile"));
    QCOMPARE(fromEnvironment.modelDir, env.path("env model"));
    const auto fromArguments = RuntimePaths::resolve(env.path("explicit profile"), env.path("explicit model"));
    QCOMPARE(fromArguments.profileDir, env.path("explicit profile"));
    QCOMPARE(fromArguments.modelDir, env.path("explicit model"));
}

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
    first.language = QStringLiteral("zh_CN");
    first.includeCode = true;
    first.indexingPaused = true;
    first.editorCommand = QStringLiteral("editor --line {line} {path}");
    QVERIFY2(first.save(paths.configFile, &error), qPrintable(error));
    AppSettings second;
    QVERIFY2(second.load(paths.configFile, &error), qPrintable(error));
    QCOMPARE(second.toJson(), first.toJson());
    QVERIFY(!QFileInfo::exists(env.path("registry")));
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
    QString error;
    auto json = original.toJson();
    json["version"] = 2;
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    json = original.toJson();
    json["includeCode"] = "true";
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    json = original.toJson();
    json["roots"] = QJsonArray{"relative/directory"};
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    json = original.toJson();
    json["language"] = "unknown";
    QVERIFY(!AppSettings::fromJson(json, &parsed, &error));
    QVERIFY(!AppSettings::fromJson(original.toJson(), nullptr, &error));
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
    QCOMPARE(embeddingPipelineFingerprint(), embeddingPipelineFingerprint());
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
