#include "core/model_files.h"
#include "support/test_environment.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QSignalSpy>
#include <QStandardPaths>
#include <QTcpServer>
#include <QTcpSocket>
#include <QtTest>
#include <filesystem>
#include <limits>
#ifdef Q_OS_MACOS
#include <QStorageInfo>
#include <sys/mount.h>
#endif

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

namespace {
bool writeFile(const QString& path, const QByteArray& data)
{
    QFile file(path);
    return file.open(QIODevice::WriteOnly) && file.write(data) == data.size();
}

QByteArray readFile(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    return file.readAll();
}

class ModelServer : public QTcpServer {
public:
    enum Mode { Normal, IgnoreRange, WrongRange, WrongLength, Corrupt, Denied, Slow, Truncated,
                NotModified, MultipleChoices, Redirect };
    QByteArray body = QByteArrayLiteral("a realistic test model payload with bytes \0 and UTF-8 \xe4\xb8\xad");
    QByteArray lastRequest;
    Mode mode = Normal;
    int requests = 0;

    ModelServer()
    {
        connect(this, &QTcpServer::newConnection, this, [this] {
            while (auto* socket = nextPendingConnection()) {
                auto request = std::make_shared<QByteArray>();
                connect(socket, &QTcpSocket::readyRead, this, [this, socket, request] {
                    request->append(socket->readAll());
                    if (!request->contains("\r\n\r\n")) return;
                    disconnect(socket, &QTcpSocket::readyRead, this, nullptr);
                    lastRequest = *request;
                    ++requests;
                    qint64 start = 0;
                    const auto range = request->indexOf("Range: bytes=");
                    if (range >= 0) {
                        const auto beginning = range + qint64(QByteArrayLiteral("Range: bytes=").size());
                        const auto end = request->indexOf('-', beginning);
                        start = request->mid(beginning, end - beginning).toLongLong();
                    }
                    if (mode == Denied) {
                        socket->write("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                        socket->disconnectFromHost();
                        return;
                    }
                    if (mode == NotModified || mode == MultipleChoices) {
                        const QByteArray status = mode == NotModified
                            ? QByteArrayLiteral("304 Not Modified")
                            : QByteArrayLiteral("300 Multiple Choices");
                        socket->write("HTTP/1.1 " + status
                                      + "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                        socket->disconnectFromHost();
                        return;
                    }
                    if (mode == Redirect && request->startsWith("GET /model ")) {
                        socket->write("HTTP/1.1 302 Found\r\nLocation: /payload\r\n"
                                      "Content-Length: 0\r\nConnection: close\r\n\r\n");
                        socket->disconnectFromHost();
                        return;
                    }
                    const bool partial = (start > 0 && mode != IgnoreRange) || mode == WrongRange;
                    if (mode == IgnoreRange) start = 0;
                    QByteArray payload = body.mid(start);
                    if (mode == Corrupt) payload.fill('x');
                    QByteArray header = partial ? "HTTP/1.1 206 Partial Content\r\n" : "HTTP/1.1 200 OK\r\n";
                    if (partial) {
                        header += "Content-Range: bytes " + QByteArray::number(mode == WrongRange ? start + 1 : start) +
                                  '-' + QByteArray::number(body.size() - 1) + '/' + QByteArray::number(body.size()) + "\r\n";
                    }
                    header += "Content-Length: " + QByteArray::number(payload.size() + (mode == WrongLength ? 1 : 0)) +
                              "\r\nConnection: close\r\n\r\n";
                    socket->write(header);
                    socket->write(mode == Slow || mode == Truncated ? payload.left(7) : payload);
                    if (mode != Slow) socket->disconnectFromHost();
                });
                connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
            }
        });
    }

    ModelArtifact artifact() const
    {
        return {QStringLiteral("test-model.gguf"),
                QUrl(QStringLiteral("http://127.0.0.1:%1/model").arg(serverPort())), body.size(),
                QCryptographicHash::hash(body, QCryptographicHash::Sha256).toHex()};
    }
};
}

class ModelFilesTest : public QObject {
    Q_OBJECT
private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
        QCoreApplication::setApplicationName(QStringLiteral("qcopilots-search-tests-model-files"));
    }

    void pinnedManifest()
    {
        const auto files = ModelFiles::manifest();
        QCOMPARE(files.size(), 2);
        QCOMPARE(files.at(0).size + files.at(1).size, qint64(864676480));
        QCOMPARE(files.at(0).sha256, QByteArrayLiteral("2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135"));
        QCOMPARE(files.at(1).sha256, QByteArrayLiteral("c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f"));
        for (const auto& file : files) {
            QCOMPARE(file.url.scheme(), QStringLiteral("https"));
            QVERIFY(file.url.path().contains(QStringLiteral("bfcd298762cc34d0357ece5ebdd31791a3a374d8")));
            QVERIFY(file.name.endsWith(QStringLiteral("Q8_0.gguf")));
        }
    }

    void importsVerifyAndPreserveDestination()
    {
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        const auto artifact = server.artifact();
        const QString folder = root.path(QString::fromUtf8("中文 models with spaces"));
        ModelFiles models(folder, {artifact});
        QString error;
        QVERIFY(!models.verify(&error));
        QVERIFY(!error.isEmpty());
        const QString source = root.path(QStringLiteral("offline.gguf"));
        QVERIFY(writeFile(source, server.body));
        QVERIFY2(models.importFile(source, artifact.name, &error), qPrintable(error));
        QVERIFY2(models.verify(&error), qPrintable(error));
        QVERIFY(error.isEmpty());
        QCOMPARE(readFile(QDir(folder).filePath(artifact.name)), server.body);
        QVERIFY2(models.importFile(QDir(folder).filePath(artifact.name), artifact.name, &error), qPrintable(error));
        QVERIFY(writeFile(source, QByteArray(server.body.size(), 'z')));
        QVERIFY(!models.importFile(source, artifact.name, &error));
        QVERIFY(error.contains(QStringLiteral("SHA-256")));
        QCOMPARE(readFile(QDir(folder).filePath(artifact.name)), server.body);
        QVERIFY(!models.importFile(source, QStringLiteral("../outside.gguf"), &error));
        QVERIFY(!QFile::exists(root.path(QStringLiteral("outside.gguf"))));
    }

    void downloadedModelsAreVerifiedAndReused()
    {
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
#ifdef Q_OS_MACOS
        // Preserve native runner evidence of the System/Data volume distinction
        // without assuming any machine-specific mount or temporary path.
        const QStorageInfo qtStorage(root.root());
        struct statfs nativeStorage = {};
        QVERIFY(::statfs(QFile::encodeName(root.root()).constData(), &nativeStorage) == 0);
        qInfo().noquote() << "Model storage target:" << root.root()
                         << "Qt volume:" << qtStorage.rootPath()
                         << "Qt valid/ready/readonly:" << qtStorage.isValid()
                         << qtStorage.isReady() << qtStorage.isReadOnly()
                         << "native mount:" << nativeStorage.f_mntonname
                         << "native readonly:" << ((nativeStorage.f_flags & MNT_RDONLY) != 0)
                         << "native available blocks/block size:" << nativeStorage.f_bavail << nativeStorage.f_bsize;
#endif
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        ModelFiles models(root.root(), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        QSignalSpy progress(&models, &ModelFiles::progress);
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 1, 5000);
        QVERIFY2(completed.at(0).at(0).toBool(), qPrintable(completed.at(0).at(1).toString()));
        QVERIFY(models.verify());
        QCOMPARE(server.requests, 1);
        QVERIFY(!QFile::exists(root.path(QStringLiteral("test-model.gguf.download"))));
        QVERIFY(!progress.isEmpty());
        QCOMPARE(progress.last().at(0).toLongLong(), qint64(server.body.size()));
        QCOMPARE(progress.last().at(1).toLongLong(), qint64(server.body.size()));
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 2, 5000);
        QVERIFY(completed.at(1).at(0).toBool());
        QCOMPARE(server.requests, 1);
    }

    void insufficientStorageRejectsBeforeTransfer()
    {
        TestEnvironment root(QStringLiteral("model-storage-budget"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        const QByteArray encodedPath = root.root().toUtf8();
        const auto filesystemPath = std::filesystem::u8path(encodedPath.constBegin(), encodedPath.constEnd());
        std::error_code error;
        const auto space = std::filesystem::space(filesystemPath, error);
        QVERIFY2(!error, error.message().c_str());
        QVERIFY(space.capacity > 0);
        QVERIFY(space.capacity < static_cast<std::uintmax_t>(std::numeric_limits<qint64>::max() / 2));
        auto artifact = server.artifact();
        // A new download reserves both the partial file and its publication
        // copy. No large fixture is created: the budget must reject it first.
        artifact.size = static_cast<qint64>(space.capacity / 2) + 1;
        ModelFiles models(root.root(), {artifact});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 1, 5000);
        QVERIFY(!completed.at(0).at(0).toBool());
        QVERIFY2(completed.at(0).at(1).toString().contains(QStringLiteral("Insufficient model storage")),
                 qPrintable(completed.at(0).at(1).toString()));
        QCOMPARE(server.requests, 0);
        QVERIFY(!models.isBusy());
        QVERIFY(!QFile::exists(root.path(artifact.name)));
        QVERIFY(readFile(root.path(artifact.name + QStringLiteral(".download"))).isEmpty());
    }

    void resumesRanges_data()
    {
        QTest::addColumn<int>("mode");
        QTest::newRow("correct-range") << int(ModelServer::Normal);
        QTest::newRow("server-ignores-range") << int(ModelServer::IgnoreRange);
    }

    void resumesRanges()
    {
        QFETCH(int, mode);
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        server.mode = static_cast<ModelServer::Mode>(mode);
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        QVERIFY(writeFile(root.path(QStringLiteral("test-model.gguf.download")), server.body.left(9)));
        ModelFiles models(root.root(), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 1, 5000);
        QVERIFY2(completed.at(0).at(0).toBool(), qPrintable(completed.at(0).at(1).toString()));
        QVERIFY(server.lastRequest.contains("Range: bytes=9-"));
        QVERIFY(models.verify());
    }

    void rejectsUntrustedResponse_data()
    {
        QTest::addColumn<int>("mode");
        QTest::newRow("wrong-content-range") << int(ModelServer::WrongRange);
        QTest::newRow("wrong-content-length") << int(ModelServer::WrongLength);
        QTest::newRow("corrupt-payload") << int(ModelServer::Corrupt);
        QTest::newRow("forbidden") << int(ModelServer::Denied);
        QTest::newRow("truncated") << int(ModelServer::Truncated);
        QTest::newRow("terminal-304") << int(ModelServer::NotModified);
        QTest::newRow("terminal-300") << int(ModelServer::MultipleChoices);
    }

    void rejectsUntrustedResponse()
    {
        QFETCH(int, mode);
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        server.mode = static_cast<ModelServer::Mode>(mode);
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        ModelFiles models(root.root(), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 1, 5000);
        QVERIFY(!completed.at(0).at(0).toBool());
        QVERIFY(!completed.at(0).at(1).toString().isEmpty());
        QVERIFY(!QFile::exists(root.path(QStringLiteral("test-model.gguf"))));
        QVERIFY(!models.verify());
        QVERIFY(!models.isBusy());
        if (mode == ModelServer::NotModified || mode == ModelServer::MultipleChoices) {
            QVERIFY(completed.at(0).at(1).toString().contains(
                mode == ModelServer::NotModified ? QStringLiteral("304") : QStringLiteral("300")));
            // A failed final response must release the directory lock so a new
            // downloader can immediately perform a successful retry.
            server.mode = ModelServer::Normal;
            ModelFiles retry(root.root(), {server.artifact()});
            QSignalSpy retried(&retry, &ModelFiles::finished);
            retry.downloadMissing();
            QTRY_COMPARE_WITH_TIMEOUT(retried.size(), 1, 5000);
            QVERIFY2(retried.at(0).at(0).toBool(), qPrintable(retried.at(0).at(1).toString()));
            QVERIFY(retry.verify());
            QCOMPARE(completed.size(), 1);
        }
    }

    void followsAutomaticRedirect()
    {
        TestEnvironment root(QStringLiteral("model-redirect"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        server.mode = ModelServer::Redirect;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        ModelFiles models(root.root(), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 1, 5000);
        QVERIFY2(completed.at(0).at(0).toBool(), qPrintable(completed.at(0).at(1).toString()));
        QCOMPARE(server.requests, 2);
        QVERIFY(models.verify());
        QVERIFY(!models.isBusy());
    }

    void cancelKeepsPartialAndUnlocks_data()
    {
        QTest::addColumn<bool>("longPath");
        QTest::newRow("normal") << false;
#ifdef Q_OS_WIN
        QTest::newRow("windows-long-unicode-path") << true;
#endif
    }

    void cancelKeepsPartialAndUnlocks()
    {
        QFETCH(bool, longPath);
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        QString modelDir = root.root();
        if (longPath) {
            modelDir = root.path(QStringLiteral("中文 models with spaces"));
            while (modelDir.size() < 320)
                modelDir = QDir(modelDir).filePath(QStringLiteral("中文 nested model directory with spaces"));
            QVERIFY(modelDir.size() >= 320);
        }
        ModelServer server;
        server.mode = ModelServer::Slow;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        ModelFiles models(modelDir, {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        QSignalSpy progress(&models, &ModelFiles::progress);
        models.downloadMissing();
        QTRY_VERIFY_WITH_TIMEOUT(!progress.isEmpty() && progress.last().at(0).toLongLong() == 7, 5000);
        ModelFiles contender(modelDir, {server.artifact()});
        QSignalSpy blocked(&contender, &ModelFiles::finished);
        contender.downloadMissing();
        QCOMPARE(blocked.size(), 1);
        QVERIFY(!blocked.at(0).at(0).toBool());
        models.cancel();
        QCOMPARE(completed.size(), 1);
        QVERIFY(!completed.at(0).at(0).toBool());
        QVERIFY(!QFileInfo::exists(QDir(modelDir).filePath(QStringLiteral("qcopilots-search-models.lock"))));
        QCOMPARE(readFile(QDir(modelDir).filePath(QStringLiteral("test-model.gguf.download"))), server.body.left(7));
        server.mode = ModelServer::Normal;
        contender.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(blocked.size(), 2, 5000);
        QVERIFY2(blocked.at(1).at(0).toBool(), qPrintable(blocked.at(1).at(1).toString()));
        QVERIFY(contender.verify());
        models.cancel();
        QCOMPARE(completed.size(), 1);
    }

    void completePartialPublishesWithoutNetwork()
    {
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        QVERIFY(writeFile(root.path(QStringLiteral("test-model.gguf.download")), server.body));
        ModelFiles models(root.root(), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QTRY_COMPARE(completed.size(), 1);
        QVERIFY(completed.at(0).at(0).toBool());
        QCOMPARE(server.requests, 0);
        QVERIFY(models.verify());
    }

    void immediateCancelAndRestartDoesNotReuseQueuedOperation()
    {
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        ModelFiles models(root.root(), {server.artifact()});
        bool cancelled = false;
        connect(&models, &ModelFiles::progress, this, [&](qint64 received, qint64) {
            if (!cancelled && received == 0) {
                cancelled = true;
                models.cancel();
            }
        });
        QSignalSpy completed(&models, &ModelFiles::finished);
        connect(&models, &ModelFiles::finished, this, [&](bool success, const QString&) {
            if (!success) models.downloadMissing();
        });
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 2, 5000);
        QVERIFY(!completed.at(0).at(0).toBool());
        QVERIFY2(completed.at(1).at(0).toBool(), qPrintable(completed.at(1).at(1).toString()));
        QCOMPARE(server.requests, 1);
        QVERIFY(models.verify());
    }

    void invalidCompletePartialRestarts_data()
    {
        QTest::addColumn<bool>("oversized");
        QTest::newRow("complete-with-bad-hash") << false;
        QTest::newRow("larger-than-manifest") << true;
    }

    void invalidCompletePartialRestarts()
    {
        QFETCH(bool, oversized);
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        QVERIFY(writeFile(root.path(QStringLiteral("test-model.gguf.download")),
                          QByteArray(server.body.size() + (oversized ? 5 : 0), 'x')));
        ModelFiles models(root.root(), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QTRY_COMPARE_WITH_TIMEOUT(completed.size(), 1, 5000);
        QVERIFY2(completed.at(0).at(0).toBool(), qPrintable(completed.at(0).at(1).toString()));
        QCOMPARE(server.requests, 1);
        QVERIFY(!server.lastRequest.contains("Range:"));
        QVERIFY(models.verify());
    }

    void invalidManifestCannotWriteOutsideModelDirectory()
    {
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        auto artifact = server.artifact();
        artifact.name = QStringLiteral("../outside.gguf");
        ModelFiles models(root.path(QStringLiteral("models")), {artifact});
        QString error;
        QVERIFY(!models.verify(&error));
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QCOMPARE(completed.size(), 1);
        QVERIFY(!completed.at(0).at(0).toBool());
        QCOMPARE(server.requests, 0);
        QVERIFY(!QDir(root.path(QStringLiteral("models"))).exists());
    }

    void modelDirectoryCreationFailure()
    {
        TestEnvironment root(QStringLiteral("model-files"));
        QVERIFY2(root.isValid(), qPrintable(root.error()));
        ModelServer server;
        QVERIFY(server.listen(QHostAddress::LocalHost, 0));
        const QString blocker = root.path(QStringLiteral("not-a-directory"));
        QVERIFY(writeFile(blocker, "blocked"));
        ModelFiles models(QDir(blocker).filePath(QStringLiteral("models")), {server.artifact()});
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QCOMPARE(completed.size(), 1);
        QVERIFY(!completed.at(0).at(0).toBool());
        QCOMPARE(readFile(blocker), QByteArrayLiteral("blocked"));
    }

    void emptyDirectoryCannotFallBackToWorkingDirectory()
    {
        ModelFiles models(QString{});
        QString error;
        QVERIFY(!models.verify(&error));
        QVERIFY(error.contains(QStringLiteral("directory")));
        QSignalSpy completed(&models, &ModelFiles::finished);
        models.downloadMissing();
        QCOMPARE(completed.size(), 1);
        QVERIFY(!completed.at(0).at(0).toBool());
        QVERIFY(models.modelDirectory().isEmpty());
    }
};

QTEST_GUILESS_MAIN(ModelFilesTest)
#include "model_files_test.moc"
