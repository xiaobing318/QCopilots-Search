#include "service/service_client.h"
#include "service/ipc_protocol.h"
#include "service/ipc_security.h"
#include "core/types.h"
#include "support/test_environment.h"
#include <QtTest>
#include <QLocalServer>
#include <QTemporaryDir>
#include <QFile>
#include <QDir>
#include <QPointer>
#ifndef Q_OS_WIN
#include <sys/un.h>
#endif
using namespace qcopilots_search;

struct Fixture {
    test::TestEnvironment root{QStringLiteral("ipc-client")};
    RuntimePaths paths;
    QLocalServer server;
    QPointer<QLocalSocket> peer;
    QByteArray incoming;
    QList<QJsonObject> requests;
    QString parseError;
    Fixture() : paths(root.runtimePaths()) {}
    bool token(char letter = 'a') {
        QFile f(QDir(paths.profileDir).filePath("qcs-ipc-token"));
        return f.open(QIODevice::WriteOnly | QIODevice::Truncate)
            && f.write(QByteArray(64, letter)) == 64;
    }
    bool start() {
        if (!root.isValid() || !paths.ensure() || !paths.ensureIpcDirectory() || !token()) return false;
        QObject::connect(&server, &QLocalServer::newConnection, &server, [this] {
            peer = server.nextPendingConnection();
            QObject::connect(peer, &QLocalSocket::readyRead, &server, [this] {
                if (!peer) return;
                incoming += peer->readAll();
                takeIpcFrames(incoming, requests, &parseError);
            });
        });
        server.setSocketOptions(QLocalServer::UserAccessOption);
        return server.listen(paths.ipcName);
    }
    void reply(const QString &id) {
        peer->write(encodeIpcFrame({{"protocol", 1}, {"id", id}, {"ok", true},
                                  {"result", QJsonObject{{"value", 42}}}}));
    }
};

class ClientTests : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void fragmentedAndCombinedFrames() {
        const auto frame = encodeIpcFrame({{"protocol", 1}, {"text", QString::fromUtf8("中文 path")}});
        QByteArray buffer;
        QList<QJsonObject> objects;
        for (qsizetype n = 0; n < frame.size(); ++n) {
            buffer += frame[n];
            QVERIFY(takeIpcFrames(buffer, objects));
            QCOMPARE(objects.size(), n == frame.size() - 1 ? 1 : 0);
        }
        QCOMPARE(objects[0].value("text").toString(), QString::fromUtf8("中文 path"));
        buffer = frame + frame;
        QVERIFY(takeIpcFrames(buffer, objects));
        QCOMPARE(objects.size(), 3);
        QVERIFY(buffer.isEmpty());
    }
    void invalidFrames_data() {
        QTest::addColumn<QByteArray>("bytes");
        QTest::newRow("zero") << QByteArray(4, '\0');
        QTest::newRow("oversize") << QByteArray::fromHex("01000001");
        QTest::newRow("invalid-json") << QByteArray::fromHex("00000001") + "?";
        QTest::newRow("non-object") << QByteArray::fromHex("00000002") + "[]";
        const QByteArray wrongVersion("{\"protocol\":2}");
        QByteArray header(4, '\0');
        header[3] = static_cast<char>(wrongVersion.size());
        QTest::newRow("protocol-version") << header + wrongVersion;
        const QByteArray invalidUtf8("{\"protocol\":1,\"x\":\"\xff\"}");
        header[3] = static_cast<char>(invalidUtf8.size());
        QTest::newRow("invalid-utf8") << header + invalidUtf8;
    }
    void invalidFrames() {
        QFETCH(QByteArray, bytes);
        QList<QJsonObject> messages;
        QString error;
        QVERIFY(!takeIpcFrames(bytes, messages, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(bytes.isEmpty());
    }
    void disabledAutoStartDoesNotCreateProfile() {
        test::TestEnvironment environment(QStringLiteral("ipc-no-start"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        ServiceClient client(paths);
        client.setAutoStart(false);
        QSignalSpy failure(&client, &ServiceClient::failed);
        const auto id = client.request("status");
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure[0][0].toString(), id);
        const auto code = failure[0][1].toString();
        QVERIFY(code == QStringLiteral("connection_failed")
                || code == QStringLiteral("ipc_directory_unavailable"));
        QVERIFY(!QFileInfo::exists(paths.profileDir));
    }
    void failedEnsureConnectionDoesNotCreateBusinessDirectories() {
        test::TestEnvironment environment(QStringLiteral("ipc-failed-ensure"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = RuntimePaths::resolve(environment.path(QStringLiteral("data base 中文")),
                                                 environment.path(QStringLiteral("models")));
        QVERIFY(QDir().mkpath(paths.profileDir));
        QVERIFY(paths.ensureIpcDirectory());
        QFile token(QDir(paths.profileDir).filePath(QStringLiteral("qcs-ipc-token")));
        QVERIFY(token.open(QIODevice::WriteOnly | QIODevice::NewOnly));
        QCOMPARE(token.write("invalid"), qint64(7)); token.close();
        const auto entries = QDir(paths.profileDir).entryList(QDir::AllEntries | QDir::Hidden | QDir::NoDotAndDotDot);
        // A listening peer with an invalid token causes ensureService to fail
        // authentication without spawning any detached service process.
        QLocalServer server;
        server.setSocketOptions(QLocalServer::UserAccessOption);
        QVERIFY(server.listen(paths.ipcName));
        ServiceClient client(paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        const auto id = client.request(QStringLiteral("status"), {}, 100);
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure.first().first().toString(), id);
        QVERIFY(!client.isConnected());
        client.disconnectFromService();
        QCOMPARE(QDir(paths.profileDir).entryList(QDir::AllEntries | QDir::Hidden | QDir::NoDotAndDotDot), entries);
        for (const auto &directory : {paths.indexDir, paths.modelDir, paths.cacheDir, paths.logDir, paths.tempDir})
            QVERIFY2(!QFileInfo::exists(directory), qPrintable(directory));
    }
    void coldStartReportsInvalidConfigurationWithoutCreatingState_data() {
        QTest::addColumn<QByteArray>("bytes");
        QTest::addColumn<QString>("diagnostic");
        QTest::newRow("invalid-json") << QByteArray("{not json") << QStringLiteral("Invalid settings JSON");
        QTest::newRow("old-version") << QByteArray("{\"version\":3}") << QStringLiteral("requires version 4");
        QTest::newRow("future-version") << QByteArray("{\"version\":5}") << QStringLiteral("requires version 4");
        QTest::newRow("removed-selection") << QByteArray("{\"version\":4,\"modelId\":\"embeddinggemma-2\"}")
            << QStringLiteral("modelId");
    }
    void coldStartReportsInvalidConfigurationWithoutCreatingState() {
        QFETCH(QByteArray, bytes);
        QFETCH(QString, diagnostic);
        test::TestEnvironment environment(QStringLiteral("ipc-invalid-cold-start"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = RuntimePaths::resolve(environment.path(QStringLiteral("data base 中文")),
                                                 environment.path(QStringLiteral("models")));
        QVERIFY(QDir().mkpath(paths.profileDir));
        QFile configuration(paths.configFile);
        QVERIFY(configuration.open(QIODevice::WriteOnly | QIODevice::NewOnly));
        QCOMPARE(configuration.write(bytes), qint64(bytes.size())); configuration.close();
        const auto entries = QDir(paths.profileDir).entryList(QDir::AllEntries | QDir::Hidden | QDir::NoDotAndDotDot);
        ServiceClient client(paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        QSignalSpy response(&client, &ServiceClient::response);
        const auto id = client.request(QStringLiteral("status"));
        QTRY_COMPARE_WITH_TIMEOUT(failure.size(), 1, 2000);
        QCOMPARE(failure.first().at(0).toString(), id);
        QCOMPARE(failure.first().at(1).toString(), QStringLiteral("service_start_failed"));
        QVERIFY2(failure.first().at(2).toString().contains(diagnostic), qPrintable(failure.first().at(2).toString()));
        QVERIFY(response.isEmpty());
        QVERIFY(!client.isConnected());
        client.disconnectFromService();
        QCOMPARE(QDir(paths.profileDir).entryList(QDir::AllEntries | QDir::Hidden | QDir::NoDotAndDotDot), entries);
        for (const auto &directory : {paths.indexDir, paths.modelDir, paths.cacheDir, paths.logDir, paths.tempDir})
            QVERIFY2(!QFileInfo::exists(directory), qPrintable(directory));
        QVERIFY(!QFileInfo::exists(paths.lockFile));
        QVERIFY(configuration.open(QIODevice::ReadOnly));
        QCOMPARE(configuration.readAll(), bytes);
    }
    void liveServiceRemainsReachableWithInvalidConfiguration_data() {
        coldStartReportsInvalidConfigurationWithoutCreatingState_data();
    }
    void liveServiceRemainsReachableWithInvalidConfiguration() {
        QFETCH(QByteArray, bytes);
        Fixture fixture;
        QVERIFY(fixture.start());
        QFile configuration(fixture.paths.configFile);
        QVERIFY(configuration.open(QIODevice::WriteOnly | QIODevice::NewOnly));
        QCOMPARE(configuration.write(bytes), qint64(bytes.size())); configuration.close();
        // Active services keep their last valid settings. A bad pending edit
        // must not prevent connecting to report or recover from that edit.
        ServiceClient client(fixture.paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        QSignalSpy response(&client, &ServiceClient::response);
        const auto id = client.request(QStringLiteral("status"));
        QTRY_COMPARE(fixture.requests.size(), 1);
        QVERIFY(client.isConnected());
        fixture.reply(id);
        QTRY_COMPARE(response.size(), 1);
        QCOMPARE(response.first().first().toString(), id);
        QVERIFY(failure.isEmpty());
        QVERIFY(configuration.open(QIODevice::ReadOnly));
        QCOMPARE(configuration.readAll(), bytes);
    }
    void legacyProfileIsRejectedBeforeConnectingToExistingEndpoint() {
        Fixture fixture;
        QVERIFY2(fixture.root.isValid(), qPrintable(fixture.root.error()));
        QVERIFY(fixture.start());
        // A live endpoint must not let an explicitly selected legacy profile
        // bypass layout validation, including when service autostart is off.
        const QString markerPath = QDir(fixture.paths.profileDir)
            .filePath(QStringLiteral("qcopilots-search-ipc-token"));
        const QByteArray legacyContents("legacy-user-data-must-not-change");
        QFile marker(markerPath);
        QVERIFY(marker.open(QIODevice::WriteOnly | QIODevice::NewOnly));
        QCOMPARE(marker.write(legacyContents), legacyContents.size());
        marker.close();
        const auto originalEntries = QDir(fixture.paths.profileDir).entryList(QDir::AllEntries | QDir::Hidden);
        QSignalSpy connections(&fixture.server, &QLocalServer::newConnection);
        ServiceClient client(fixture.paths);
        client.setAutoStart(false);
        QSignalSpy failure(&client, &ServiceClient::failed);
        QSignalSpy response(&client, &ServiceClient::response);
        const QString id = client.request(QStringLiteral("status"));
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure.first().at(0).toString(), id);
        QCOMPARE(failure.first().at(1).toString(), QStringLiteral("profile_unavailable"));
        QVERIFY(failure.first().at(2).toString().contains(QStringLiteral("legacy_profile_layout:")));
        QCOMPARE(response.size(), 0);
        QCOMPARE(connections.size(), 0);
        QVERIFY(fixture.peer.isNull());
        QVERIFY(fixture.requests.isEmpty());
        QVERIFY(!fixture.server.hasPendingConnections());
        QCOMPARE(QDir(fixture.paths.profileDir).entryList(QDir::AllEntries | QDir::Hidden), originalEntries);
        QVERIFY(marker.open(QIODevice::ReadOnly));
        QCOMPARE(marker.readAll(), legacyContents);
    }
#ifndef Q_OS_WIN
    void unixPeerCredentialsAreVerified() {
        struct SocketPair {
            int descriptors[2]{-1, -1};
            ~SocketPair() { for (const int fd : descriptors) if (fd >= 0) ::close(fd); }
        } pair;
        QCOMPARE(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair.descriptors), 0);
        QString error;
        QVERIFY2(ipcPeerIsCurrentUser(pair.descriptors[0], &error), qPrintable(error));
        QVERIFY2(ipcPeerIsCurrentUser(pair.descriptors[1], &error), qPrintable(error));
        QVERIFY(!ipcPeerIsCurrentUser(-1, &error));
        QVERIFY(!error.isEmpty());
        // Exercise the UID rejection decision without needing a privileged
        // process or modifying another account's credentials in a unit test.
        QVERIFY(!ipc_security_detail::matchesUser(100, 101, &error));
        QVERIFY(error.contains(QStringLiteral("different")));
    }
    void unixLongTemporaryDirectoryStillConnects() {
        test::TestEnvironment environment(QStringLiteral("ipc-long-tmpdir"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto longTemporary = environment.path(QStringLiteral("temporary/") + QString(120, u't'));
        QVERIFY(QDir().mkpath(longTemporary));
        test::ScopedEnvironment temporary("TMPDIR", longTemporary.toUtf8());
        Fixture fixture;
        QVERIFY(fixture.start());
        QVERIFY(QDir::isAbsolutePath(fixture.paths.ipcName));
        sockaddr_un address{};
        QVERIFY(QFile::encodeName(fixture.paths.ipcName).size() + 1 <= qsizetype(sizeof(address.sun_path)));
        {
            test::ScopedEnvironment differentTemporary("TMPDIR", environment.root().toUtf8());
            QCOMPARE(RuntimePaths::resolve(fixture.paths.profileBaseDir, fixture.paths.modelDir).ipcName,
                     fixture.paths.ipcName);
        }
        ServiceClient client(fixture.paths);
        client.setAutoStart(false);
        QSignalSpy response(&client, &ServiceClient::response);
        const auto id = client.request(QStringLiteral("status"));
        QTRY_COMPARE(fixture.requests.size(), 1);
        fixture.reply(id);
        QTRY_COMPARE(response.size(), 1);
        QCOMPARE(response.first().first().toString(), id);
    }
#endif
    void roundTripAndEvent() {
        Fixture f;
        QVERIFY(f.start());
        ServiceClient client(f.paths);
        QSignalSpy connected(&client, &ServiceClient::connected);
        QSignalSpy response(&client, &ServiceClient::response);
        QSignalSpy event(&client, &ServiceClient::eventReceived);
        const auto id = client.request("status");
        QTRY_COMPARE(connected.size(), 1);
        QTRY_COMPARE(f.requests.size(), 1);
        QCOMPARE(f.requests[0].value("token").toString(), QString(64, 'a'));
        QCOMPARE(f.requests[0].value("id").toString(), id);
        f.reply(id);
        f.peer->write(encodeIpcFrame({{"protocol", 1}, {"event", "progress"},
                                     {"data", QJsonObject{{"completed", 1}}}}));
        QTRY_COMPARE(response.size(), 1);
        QTRY_COMPARE(event.size(), 1);
        QCOMPARE(response[0][0].toString(), id);
        QCOMPARE(response[0][1].toJsonObject().value("value").toInt(), 42);
    }
    void timeoutSendsCancellationAndIgnoresLateResponse() {
        Fixture f;
        QVERIFY(f.start());
        ServiceClient client(f.paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        QSignalSpy response(&client, &ServiceClient::response);
        client.ensureService();
        QTRY_VERIFY(client.isConnected());
        const auto id = client.request("search", {}, 100);
        QTRY_COMPARE(f.requests.size(), 1);
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure[0][0].toString(), id);
        QCOMPARE(failure[0][1].toString(), "request_timeout");
        QTRY_COMPARE(f.requests.size(), 2);
        QCOMPARE(f.requests[1].value("method").toString(), "cancel");
        QCOMPARE(f.requests[1].value("params").toObject().value("id").toString(), id);
        f.reply(id);
        QTest::qWait(30);
        QCOMPARE(response.size(), 0);
    }
    void manualCancellation() {
        Fixture f;
        QVERIFY(f.start());
        ServiceClient client(f.paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        const auto id = client.request("status");
        client.cancel(id);
        QCOMPARE(failure.size(), 1);
        QCOMPARE(failure[0][0].toString(), id);
        QCOMPARE(failure[0][1].toString(), "cancelled");
        QTest::qWait(20);
        QCOMPARE(f.requests.size(), 0);
    }
    void reconnectReadsNewToken() {
        Fixture f;
        QVERIFY(f.start());
        ServiceClient client(f.paths);
        client.ensureService();
        QTRY_VERIFY(client.isConnected());
        client.disconnectFromService();
        QVERIFY(!client.isConnected());
        QVERIFY(f.token('b'));
        client.connectToService();
        QTRY_VERIFY(client.isConnected());
        client.request("status");
        QTRY_COMPARE(f.requests.size(), 1);
        QCOMPARE(f.requests[0].value("token").toString(), QString(64, 'b'));
    }
    void malformedResponseClosesConnection() {
        Fixture f;
        QVERIFY(f.start());
        ServiceClient client(f.paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        client.request("status");
        QTRY_COMPARE(f.requests.size(), 1);
        f.peer->write(QByteArray(4, '\0'));
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure[0][1].toString(), "protocol_error");
        QVERIFY(!client.isConnected());
    }
    void disconnectedRequestFails() {
        Fixture f;
        QVERIFY(f.start());
        ServiceClient client(f.paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        const auto id = client.request("status");
        QTRY_COMPARE(f.requests.size(), 1);
        f.peer->abort();
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure[0][0].toString(), id);
        QCOMPARE(failure[0][1].toString(), "connection_closed");
    }
    void oversizedTokenFailsClosed() {
        Fixture f;
        QVERIFY(f.start());
        QFile token(QDir(f.paths.profileDir).filePath("qcs-ipc-token"));
        QVERIFY(token.open(QIODevice::WriteOnly));
        token.write(QByteArray(10000, 'a'));
        token.close();
        ServiceClient client(f.paths);
        QSignalSpy failure(&client, &ServiceClient::failed);
        client.connectToService();
        QTRY_COMPARE(failure.size(), 1);
        QCOMPARE(failure[0][1].toString(), "authentication_unavailable");
        QVERIFY(!client.isConnected());
    }
};
QTEST_GUILESS_MAIN(ClientTests)
#include "test_client.moc"
