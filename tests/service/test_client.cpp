#include "service/service_client.h"
#include "service/ipc_protocol.h"
#include "core/types.h"
#include "support/test_environment.h"
#include <QtTest>
#include <QLocalServer>
#include <QTemporaryDir>
#include <QFile>
#include <QDir>
#include <QPointer>
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
        QFile f(QDir(paths.profileDir).filePath("qcopilots-search-ipc-token"));
        return f.open(QIODevice::WriteOnly | QIODevice::Truncate)
            && f.write(QByteArray(64, letter)) == 64;
    }
    bool start() {
        if (!root.isValid() || !paths.ensure() || !token()) return false;
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
        QCOMPARE(failure[0][1].toString(), "connection_failed");
        QVERIFY(!QFileInfo::exists(paths.profileDir));
    }
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
        QFile token(QDir(f.paths.profileDir).filePath("qcopilots-search-ipc-token"));
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
