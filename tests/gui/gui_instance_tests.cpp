#include "app/gui_instance.h"
#include "core/native_lock_path.h"
#include "support/test_environment.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QLocalSocket>
#include <QLockFile>
#include <QProcess>
#include <QTimer>
#include <QtTest>
#include <cstdio>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

namespace {
void output(const QByteArray &message)
{
    std::fwrite(message.constData(), 1, size_t(message.size()), stdout);
    std::fflush(stdout);
}

// The same executable supplies a real, isolated GUI-instance process without
// loading a model, opening a window or connecting to any indexing service.
int childMain(QCoreApplication &application)
{
    const auto arguments = application.arguments();
    if (arguments.size() != 4) return 2;
    const auto paths = RuntimePaths::resolve(arguments.at(2));
    GuiInstance instance(paths);
    QObject::connect(&instance, &GuiInstance::showRequested, &application, [] { output("shown\n"); });
    QString error;
    const auto result = instance.start(&error);
    if (result == GuiInstance::Result::ActivatedExisting) { output("forwarded\n"); return 0; }
    if (result != GuiInstance::Result::Primary) { output("error: " + error.toUtf8() + '\n'); return 2; }
    output("ready\n");
    QTimer stop;
    QObject::connect(&stop, &QTimer::timeout, &application, [&] {
        if (QFileInfo::exists(arguments.at(3))) application.quit();
    });
    stop.start(20);
    QTimer::singleShot(20000, &application, [&] { application.exit(3); });
    return application.exec();
}

class Child {
public:
    Child(const TestEnvironment &environment, const QString &profile, const QString &label)
        : stopFile(environment.path(label + QStringLiteral(".stop")))
    {
        process.setProgram(QCoreApplication::applicationFilePath());
        process.setArguments({QStringLiteral("--instance-child"), profile, stopFile});
        process.setWorkingDirectory(environment.root());
    }
    ~Child()
    {
        if (process.state() == QProcess::NotRunning) return;
        QFile stop(stopFile);
        if (!stop.open(QIODevice::WriteOnly))
            qcopilots_search::test::recordCleanupFailure(QStringLiteral("Cannot request GUI fixture shutdown: %1").arg(stop.errorString()));
        stop.close();
        if (!process.waitForFinished(3000)) {
            process.kill();
            process.waitForFinished(3000);
            qcopilots_search::test::recordCleanupFailure(QStringLiteral("GUI fixture did not stop normally."));
        }
    }
    bool start()
    {
        process.start();
        return process.waitForStarted(5000);
    }
    void readStandardOutput()
    {
        standardOutput += process.readAllStandardOutput();
        // The child's C stdio stream translates LF to CRLF on Windows. Normalize
        // the accumulated buffer so a CRLF split across reads is handled too.
        standardOutput.replace("\r\n", "\n");
    }
    bool waitFor(const QByteArray &message, int timeout = 5000)
    {
        QElapsedTimer elapsed;
        elapsed.start();
        do {
            readStandardOutput();
            if (standardOutput.contains(message)) return true;
            if (process.state() == QProcess::NotRunning) return false;
            process.waitForReadyRead(qMax(1, timeout - int(elapsed.elapsed())));
        } while (elapsed.elapsed() < timeout);
        readStandardOutput();
        return standardOutput.contains(message);
    }
    QString diagnostics()
    {
        readStandardOutput();
        return QStringLiteral("%1\nstdout: %2\nstderr: %3")
            .arg(process.errorString(), QString::fromUtf8(standardOutput), QString::fromUtf8(process.readAllStandardError()));
    }
    QProcess process;
    QString stopFile;
    QByteArray standardOutput;
};

QString tokenPath(const RuntimePaths &paths, const GuiInstance &instance)
{
#ifdef Q_OS_WIN
    Q_UNUSED(instance);
    return QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui-token"));
#else
    Q_UNUSED(paths);
    return instance.endpointName() + QStringLiteral(".token");
#endif
}
}

class GuiInstanceTests final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void secondProcessActivatesNormalizedProfile()
    {
        TestEnvironment env(QStringLiteral("gui-instance-中文"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        Child first(env, paths.profileDir, QStringLiteral("first"));
        QVERIFY(first.start());
        QVERIFY2(first.waitFor("ready\n"), qPrintable(first.diagnostics()));
        // Relative components and Windows case aliases address the same profile.
        auto alias = QDir(paths.profileDir).filePath(QStringLiteral("../profile/./"));
        QCOMPARE(RuntimePaths::resolve(alias).ipcName, paths.ipcName);
#ifdef Q_OS_WIN
        alias = alias.toUpper();
        QCOMPARE(RuntimePaths::resolve(alias).ipcName, paths.ipcName);
#endif
        Child second(env, alias, QStringLiteral("second"));
        QVERIFY(second.start());
        QVERIFY2(second.process.waitForFinished(6000), qPrintable(second.diagnostics()));
        QCOMPARE(second.process.exitStatus(), QProcess::NormalExit);
        QCOMPARE(second.process.exitCode(), 0);
        QVERIFY2(second.waitFor("forwarded\n"), qPrintable(second.diagnostics()));
        QVERIFY2(first.waitFor("shown\n"), qPrintable(first.diagnostics()));
        QCOMPARE(first.process.state(), QProcess::Running);
        // An additional secondary's destruction must retain the owner's state.
        {
            GuiInstance third(paths);
            QString error;
            QCOMPARE(third.start(&error), GuiInstance::Result::ActivatedExisting);
        }
        GuiInstance fourth(paths);
        QString error;
        QCOMPARE(fourth.start(&error), GuiInstance::Result::ActivatedExisting);
    }

    void distinctProfilesRunConcurrently()
    {
        TestEnvironment env(QStringLiteral("gui-profiles"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto a = RuntimePaths::resolve(env.path(QStringLiteral("one")));
        const auto b = RuntimePaths::resolve(env.path(QStringLiteral("two")));
        Child first(env, a.profileDir, QStringLiteral("first"));
        Child second(env, b.profileDir, QStringLiteral("second"));
        QVERIFY(first.start());
        QVERIFY(second.start());
        QVERIFY2(first.waitFor("ready\n"), qPrintable(first.diagnostics()));
        QVERIFY2(second.waitFor("ready\n"), qPrintable(second.diagnostics()));
        GuiInstance activationA(a), activationB(b);
        QVERIFY(activationA.endpointName() != activationB.endpointName());
        QString error;
        QCOMPARE(activationA.start(&error), GuiInstance::Result::ActivatedExisting);
        QCOMPARE(activationB.start(&error), GuiInstance::Result::ActivatedExisting);
        QVERIFY(first.waitFor("shown\n"));
        QVERIFY(second.waitFor("shown\n"));
    }

    void racingLaunchesElectOneOwner()
    {
        TestEnvironment env(QStringLiteral("gui-race"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        Child first(env, paths.profileDir, QStringLiteral("first"));
        Child second(env, paths.profileDir, QStringLiteral("second"));
        QVERIFY(first.start());
        QVERIFY(second.start());
        QTRY_VERIFY_WITH_TIMEOUT(first.process.state() == QProcess::NotRunning
            || second.process.state() == QProcess::NotRunning, 7000);
        auto &owner = first.process.state() == QProcess::NotRunning ? second : first;
        auto &secondary = first.process.state() == QProcess::NotRunning ? first : second;
        QVERIFY2(owner.waitFor("ready\n"), qPrintable(owner.diagnostics()));
        QVERIFY2(owner.waitFor("shown\n"), qPrintable(owner.diagnostics()));
        QVERIFY2(secondary.process.exitCode() == 0, qPrintable(secondary.diagnostics()));
        QVERIFY2(secondary.waitFor("forwarded\n"), qPrintable(secondary.diagnostics()));
    }

    void activationReplyRemainsAvailableUntilClientReadsIt()
    {
        TestEnvironment env(QStringLiteral("gui-reply"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        GuiInstance primary(paths);
        QString error;
        QCOMPARE(primary.start(&error), GuiInstance::Result::Primary);
        QSignalSpy shown(&primary, &GuiInstance::showRequested);
        QFile secret(tokenPath(paths, primary));
        QVERIFY(secret.open(QIODevice::ReadOnly));
        const auto token = secret.readAll();
        secret.close();
        QCOMPARE(token.size(), 64);
        const auto request = QByteArrayLiteral("qcs-gui/1 show ") + token + '\n';
        const auto expectedReply = QByteArrayLiteral("qcs-gui/1 shown\n");

        // Hold all but the first reply byte in the OS pipe while the owner
        // finishes handling show. The owner must leave the connection open
        // until the client consumes its ACK and closes the connection.
        QLocalSocket socket;
        socket.setReadBufferSize(1);
        socket.connectToServer(primary.endpointName());
        QVERIFY(socket.waitForConnected(1000));
        QCOMPARE(socket.write(request), request.size());
        QTRY_COMPARE_WITH_TIMEOUT(shown.count(), 1, 1000);
        QTRY_VERIFY_WITH_TIMEOUT(socket.bytesAvailable() > 0, 1000);

        QByteArray reply;
        QObject::connect(&socket, &QLocalSocket::readyRead, &socket,
                         [&] { reply += socket.readAll(); });
        reply += socket.readAll();
        QTRY_COMPARE_WITH_TIMEOUT(reply, expectedReply, 1000);
        QCOMPARE(socket.state(), QLocalSocket::ConnectedState);
        socket.disconnectFromServer();
        QTRY_COMPARE_WITH_TIMEOUT(socket.state(), QLocalSocket::UnconnectedState, 1000);
        QCOMPARE(shown.count(), 1);
    }

    void liveLockWithoutEndpointFailsExplicitly()
    {
        TestEnvironment env(QStringLiteral("gui-unready"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        QLockFile lock(nativeLockFilePath(QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui.lock"))));
        lock.setStaleLockTime(0);
        QVERIFY(lock.tryLock());
        {
            GuiInstance blocked(paths);
            QCOMPARE(blocked.start(&error, 150), GuiInstance::Result::Failed);
            QVERIFY2(error.contains(QStringLiteral("did not become ready")), qPrintable(error));
        }
        QVERIFY(lock.isLocked());
        QVERIFY(QFileInfo::exists(QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui.lock"))));
    }

    void crashReleasesProfileForRecovery()
    {
        TestEnvironment env(QStringLiteral("gui-crash"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        Child first(env, paths.profileDir, QStringLiteral("first"));
        QVERIFY(first.start());
        QVERIFY2(first.waitFor("ready\n"), qPrintable(first.diagnostics()));
        GuiInstance probe(paths);
        const auto secret = tokenPath(paths, probe);
        QVERIFY(QFileInfo::exists(secret));
        first.process.kill();
        QVERIFY(first.process.waitForFinished(5000));
        {
            GuiInstance replacement(paths);
            QString error;
            QCOMPARE(replacement.start(&error), GuiInstance::Result::Primary);
            QVERIFY(QFileInfo::exists(secret));
        }
        QVERIFY(!QFileInfo::exists(secret));
        QVERIFY(!QFileInfo::exists(QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui.lock"))));
#ifndef Q_OS_WIN
        QVERIFY(!QFileInfo::exists(probe.endpointName()));
#endif
    }

    void normalExitRemovesOnlyItsOwnResources()
    {
        TestEnvironment env(QStringLiteral("gui-clean-exit"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        QString secret, endpoint, error;
        {
            GuiInstance primary(paths);
            QCOMPARE(primary.start(&error), GuiInstance::Result::Primary);
            secret = tokenPath(paths, primary);
            endpoint = primary.endpointName();
            QVERIFY(QFileInfo::exists(secret));
        }
        QVERIFY(!QFileInfo::exists(secret));
        QVERIFY(!QFileInfo::exists(QDir(paths.profileDir).filePath(QStringLiteral("qcs-gui.lock"))));
#ifndef Q_OS_WIN
        QVERIFY(!QFileInfo::exists(endpoint));
#endif
        GuiInstance replacement(paths);
        QCOMPARE(replacement.start(&error), GuiInstance::Result::Primary);
    }

    void invalidTokenFileFailsWithoutDisturbingOwner()
    {
        TestEnvironment env(QStringLiteral("gui-token"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        Child owner(env, paths.profileDir, QStringLiteral("owner"));
        QVERIFY(owner.start());
        QVERIFY2(owner.waitFor("ready\n"), qPrintable(owner.diagnostics()));
        GuiInstance probe(paths);
        QFile secret(tokenPath(paths, probe));
        QVERIFY(secret.open(QIODevice::ReadWrite));
        const auto original = secret.readAll();
        QVERIFY(secret.resize(0));
        QCOMPARE(secret.write("invalid"), 7);
        secret.close();
        QString error;
        const auto result = probe.start(&error);
        // Restore the owned fixture before asserting to keep its cleanup valid.
        QVERIFY(secret.open(QIODevice::WriteOnly | QIODevice::Truncate));
        QCOMPARE(secret.write(original), original.size());
        secret.close();
        QCOMPARE(result, GuiInstance::Result::Failed);
        QVERIFY2(error.contains(QStringLiteral("token is invalid")), qPrintable(error));
        QCOMPARE(owner.process.state(), QProcess::Running);
        GuiInstance valid(paths);
        QCOMPARE(valid.start(&error), GuiInstance::Result::ActivatedExisting);
        QVERIFY(owner.waitFor("shown\n"));
    }

    void invalidRequestsCannotActivateOrBreakOwner()
    {
        TestEnvironment env(QStringLiteral("gui-invalid"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        Child owner(env, paths.profileDir, QStringLiteral("owner"));
        QVERIFY(owner.start());
        QVERIFY2(owner.waitFor("ready\n"), qPrintable(owner.diagnostics()));
        GuiInstance probe(paths);
        QFile secret(tokenPath(paths, probe));
        QVERIFY(secret.open(QIODevice::ReadOnly));
        const auto token = secret.readAll();
        secret.close();
        QCOMPARE(token.size(), 64);
        const QList<QByteArray> invalid{
            "qcs-gui/1 show " + QByteArray(64, '0') + '\n',
            "qcs-gui/1 shutdown " + token + '\n',
            "qcs-gui/1 show " + token + "\nextra\n",
            QByteArray(129, 'x'), QByteArrayLiteral("qcs-gui/1 show ")};
        for (const auto &request : invalid) {
            QLocalSocket socket;
            socket.connectToServer(probe.endpointName());
            QVERIFY(socket.waitForConnected(1000));
            QCOMPARE(socket.write(request), request.size());
            if (socket.bytesToWrite()) QVERIFY(socket.waitForBytesWritten(1000));
            if (socket.state() != QLocalSocket::UnconnectedState)
                QVERIFY(socket.waitForDisconnected(3000));
            QVERIFY(socket.readAll().isEmpty());
        }
        owner.readStandardOutput();
        QVERIFY(!owner.standardOutput.contains("shown\n"));
        QString error;
        QCOMPARE(probe.start(&error), GuiInstance::Result::ActivatedExisting);
        QVERIFY2(owner.waitFor("shown\n"), qPrintable(owner.diagnostics()));
    }

#ifdef Q_OS_UNIX
    void symbolicProfileAliasUsesSameInstance()
    {
        TestEnvironment env(QStringLiteral("gui-alias"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        qcopilots_search::test::TestSymbolicLink link;
        QVERIFY2(link.create(env.path(QStringLiteral("alias")), paths.profileDir, &error), qPrintable(error));
        Child first(env, paths.profileDir, QStringLiteral("first"));
        QVERIFY(first.start());
        QVERIFY2(first.waitFor("ready\n"), qPrintable(first.diagnostics()));
        GuiInstance second(RuntimePaths::resolve(env.path(QStringLiteral("alias"))));
        QCOMPARE(second.start(&error), GuiInstance::Result::ActivatedExisting);
        QVERIFY(first.waitFor("shown\n"));
    }

    void unexpectedEndpointIsNeverDeleted()
    {
        TestEnvironment env(QStringLiteral("gui-endpoint"));
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        const auto paths = env.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        GuiInstance instance(paths);
        QFile unexpected(instance.endpointName());
        QVERIFY(unexpected.open(QIODevice::WriteOnly | QIODevice::NewOnly));
        QCOMPARE(unexpected.write("keep"), 4);
        unexpected.close();
        const auto result = instance.start(&error);
        const bool readable = unexpected.open(QIODevice::ReadOnly);
        const auto contents = unexpected.readAll();
        unexpected.close();
        // The fixture only removes the ordinary file it created itself.
        QVERIFY(unexpected.remove());
        QCOMPARE(result, GuiInstance::Result::Failed);
        QVERIFY2(error.contains(QStringLiteral("not a socket owned")), qPrintable(error));
        QVERIFY(readable);
        QCOMPARE(contents, QByteArrayLiteral("keep"));
    }
#endif
};

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    if (application.arguments().contains(QStringLiteral("--instance-child"))) return childMain(application);
    GuiInstanceTests tests;
    return QTest::qExec(&tests, argc, argv);
}

#include "gui_instance_tests.moc"
