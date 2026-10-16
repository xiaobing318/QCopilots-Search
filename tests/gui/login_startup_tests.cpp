#include "app/login_startup.h"
#include "support/test_environment.h"

#include <QDir>
#include <QStandardPaths>
#include <QProcess>
#include <QtTest>

using namespace qcopilots_search;

namespace {
// Decode just the serialized executable token, using the two distinct escaping
// stages specified by XDG. A round trip detects argument splitting or accidental
// expansion of executable paths containing quotes, dollars, or field codes.
QString decodeExecutable(const QByteArray &entry) {
    QByteArray encoded;
    for (const auto &line : entry.split('\n')) if (line.startsWith("Exec=")) encoded = line.mid(5);
    if (!encoded.endsWith(" --login-startup")) return {};
    encoded.chop(qsizetype(sizeof(" --login-startup") - 1));
    QByteArray value;
    for (qsizetype i = 0; i < encoded.size(); ++i) {
        if (encoded[i] == '\\') {
            if (++i >= encoded.size() || encoded[i] != '\\') return {};
        }
        value += encoded[i];
    }
    if (value.size() < 2 || value.front() != '"' || value.back() != '"') return {};
    value = value.mid(1, value.size() - 2);
    QByteArray token;
    for (qsizetype i = 0; i < value.size(); ++i) {
        const char character = value[i];
        if (character == '\\') {
            if (++i >= value.size() || !QByteArray("\"`$\\").contains(value[i])) return {};
            token += value[i];
        } else if (character == '%') {
            if (++i >= value.size() || value[i] != '%') return {};
            token += '%';
        } else {
            if (character == '"' || character == '`' || character == '$') return {};
            token += character;
        }
    }
    return QString::fromUtf8(token);
}
}

class LoginStartupTests final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }

    void desktopEntryPreservesExecutable_data() {
        QTest::addColumn<QString>("suffix");
        QTest::newRow("plain") << QStringLiteral("Apps/qcs-gui");
        QTest::newRow("spaces-unicode") << QStringLiteral("应用 Search/qcs-gui");
        QTest::newRow("exec-reserved") << QStringLiteral("a \"quoted\" $HOME `command`/qcs-gui");
        QTest::newRow("percent-field-codes") << QStringLiteral("100% %f %U/qcs-gui");
        QTest::newRow("backslash") << QStringLiteral("a\\b\\\"c/qcs-gui");
        QTest::newRow("shell-characters") << QStringLiteral("a;&|()'< >#*?/qcs-gui");
    }

    void desktopEntryPreservesExecutable() {
        QFETCH(QString, suffix);
        const QString path = QDir::tempPath() + '/' + suffix;
        const QByteArray entry = desktopAutostartEntry(path, true);
        QVERIFY(!entry.isEmpty());
        QCOMPARE(decodeExecutable(entry), path);
        QVERIFY(entry.contains("\nHidden=false\n"));
        QVERIFY(entry.contains("\nIcon=com.qcopilots.qcopilots-search\n"));
        QCOMPARE(entry.count("\nExec="), 1);
    }

    void disabledEntryOverridesSystemEntry() {
        const QString path = QDir::tempPath() + QStringLiteral("/qcs-gui");
        const QByteArray entry = desktopAutostartEntry(path, false);
        QVERIFY(entry.startsWith("[Desktop Entry]\nType=Application\n"));
        QVERIFY(entry.contains("\nHidden=true\n"));
        QCOMPARE(decodeExecutable(entry), path);
    }

    void invalidPathCannotInjectDesktopFields() {
        QVERIFY(desktopAutostartEntry({}, true).isEmpty());
        QVERIFY(desktopAutostartEntry(QStringLiteral("relative/qcs-gui"), true).isEmpty());
        const QString prefix = QDir::tempPath() + '/';
        QVERIFY(desktopAutostartEntry(prefix + "bad=name", true).isEmpty());
        QVERIFY(desktopAutostartEntry(prefix + "bad\nExec=another", true).isEmpty());
        QVERIFY(desktopAutostartEntry(prefix + "bad\rName=another", true).isEmpty());
        QVERIFY(desktopAutostartEntry(prefix + "bad\tname", true).isEmpty());
        QVERIFY(desktopAutostartEntry(prefix + QChar(0), true).isEmpty());
    }

    void desktopStateRejectsAnotherCommand() {
        const QString executable = QDir::tempPath() + QStringLiteral("/QCS/qcs-gui");
        const QByteArray expected = desktopAutostartEntry(executable, true);
        QCOMPARE(desktopAutostartState(expected, executable).status, LoginStartupStatus::Enabled);
        QCOMPARE(desktopAutostartState(desktopAutostartEntry(executable, false), executable).status,
                 LoginStartupStatus::Disabled);
        auto oldArguments = expected;
        oldArguments.replace(" --login-startup", " --background");
        QCOMPARE(desktopAutostartState(oldArguments, executable).status, LoginStartupStatus::Error);
        QCOMPARE(desktopAutostartState(expected, executable + QStringLiteral("-other")).status,
                 LoginStartupStatus::Error);
        QCOMPARE(desktopAutostartState("[Desktop Entry]\nType=Application\nExec=another\n", executable).status,
                 LoginStartupStatus::Error);
    }

    void desktopStateRespectsSessionFilters() {
        const QString executable = QDir::tempPath() + QStringLiteral("/QCS/qcs-gui");
        const QByteArray entry = desktopAutostartEntry(executable, true) + "OnlyShowIn=GNOME;KDE;\n";
        QCOMPARE(desktopAutostartState(entry, executable, "GNOME").status, LoginStartupStatus::Enabled);
        QCOMPARE(desktopAutostartState(entry, executable, "XFCE").status, LoginStartupStatus::Disabled);
        QCOMPARE(desktopAutostartState(entry + "NotShowIn=KDE;\n", executable, "KDE").status,
                 LoginStartupStatus::Disabled);
    }

#ifdef Q_OS_WIN
    void windowsRunCommandLengthAndQuoting() {
        QString error;
        const QString executable = QStringLiteral("C:/Program Files/QCS/qcs-gui.exe");
        QCOMPARE(windowsLoginStartupCommand(executable, &error),
                 QStringLiteral("\"C:\\Program Files\\QCS\\qcs-gui.exe\" --login-startup"));
        QVERIFY(error.isEmpty());
        const QString suffix = QStringLiteral(" --login-startup");
        const QString longest = QStringLiteral("C:/") + QString(260 - 3 - 2 - suffix.size(), u'a');
        QCOMPARE(windowsLoginStartupCommand(longest, &error).size(), 260);
        QVERIFY(error.isEmpty());
        QVERIFY(windowsLoginStartupCommand(longest + u'a', &error).isEmpty());
        QVERIFY(error.contains(QStringLiteral("too long")));
        QVERIFY(windowsLoginStartupCommand(QStringLiteral("C:relative"), &error).isEmpty());
        QVERIFY(windowsLoginStartupCommand(QStringLiteral("C:/bad\"name"), &error).isEmpty());
    }
#endif

    void testProfileCannotInspectOrModifyRealLoginState() {
        QVERIFY(QStandardPaths::isTestModeEnabled());
        QVERIFY(!loginStartupSessionAllowed());
        QVERIFY(!loginStartupSupported());
        const auto state = loginStartupState();
        QCOMPARE(state.status, LoginStartupStatus::Unavailable);
        QVERIFY(!state.message.isEmpty());
        // The explicit test-mode guard exits before any native storage access.
        QString error;
        QVERIFY(!loginStartupSetEnabled(true, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!loginStartupSetEnabled(false, &error));
    }

    void explicitDirectoryArgumentsIsolateStartup_data() {
        QTest::addColumn<QStringList>("options");
        QTest::newRow("profile-separated") << QStringList{"--profile-dir", "profile"};
        QTest::newRow("profile-equals") << QStringList{"--profile-dir=profile"};
        QTest::newRow("models-separated") << QStringList{"--model-dir", "models"};
        QTest::newRow("models-equals") << QStringList{"--model-dir=models"};
    }

    void explicitDirectoryArgumentsIsolateStartup() {
        QFETCH(QStringList, options);
        test::TestEnvironment environment(QStringLiteral("login-session"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QProcess child;
        child.setWorkingDirectory(environment.root());
        child.setProgram(QCoreApplication::applicationFilePath());
        child.setArguments(QStringList{QStringLiteral("--probe-login-isolation")} + options);
        child.start();
        QVERIFY2(child.waitForStarted(5000), qPrintable(child.errorString()));
        if (!child.waitForFinished(10000)) {
            child.kill();
            child.waitForFinished(5000);
            QFAIL("The isolated startup probe did not finish.");
        }
        QCOMPARE(child.exitStatus(), QProcess::NormalExit);
        QCOMPARE(child.exitCode(), 0);
        QVERIFY2(environment.cleanup(), qPrintable(environment.error()));
    }

    void manualAndIsolatedLaunchesResolveWithoutLocator() {
        test::TestEnvironment environment(QStringLiteral("login-resolution"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        test::ScopedEnvironment home("QCS_HOME", environment.path("manual").toUtf8());
        test::ScopedEnvironment models("QCOPILOTS_SEARCH_MODEL_DIR", environment.path("manual-models").toUtf8());
        const auto manual = resolveLoginStartupPaths({}, {}, false);
        QCOMPARE(manual.profileDir, RuntimePaths::resolve().profileDir);
        QCOMPARE(manual.modelDir, RuntimePaths::resolve().modelDir);
        const auto explicitPaths = resolveLoginStartupPaths(environment.path("isolated"),
                                                            environment.path("isolated-models"), true);
        QCOMPARE(explicitPaths.profileDir, RuntimePaths::resolve(environment.path("isolated")).profileDir);
        QCOMPARE(explicitPaths.modelDir, RuntimePaths::resolve(environment.path("isolated"), environment.path("isolated-models")).modelDir);
        QVERIFY(!QFileInfo::exists(manual.profileDir));
        QVERIFY(!QFileInfo::exists(explicitPaths.profileDir));
        QVERIFY(!QFileInfo::exists(explicitPaths.modelDir));
        QVERIFY2(environment.cleanup(), qPrintable(environment.error()));
    }

    void testProfileStartupDetectionDoesNotChangeActivationPolicy() {
        const char *key = "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM";
        const bool wasSet = qEnvironmentVariableIsSet(key);
        const QByteArray previous = qgetenv(key);
        prepareLoginStartupDetection();
        prepareLoginStartupDetection();
        QVERIFY(completeLoginStartupDetection(false));
        QVERIFY(completeLoginStartupDetection(true));
        QCOMPARE(qEnvironmentVariableIsSet(key), wasSet);
        QCOMPARE(qgetenv(key), previous);
    }
};

int main(int argc, char **argv) {
    QCoreApplication application(argc, argv);
    if (application.arguments().contains(QStringLiteral("--probe-login-isolation"))) {
        // Exercise the argument guard in a real, non-test-mode child process.
        // Exit immediately if it fails before attempting any native operation.
        if (QStandardPaths::isTestModeEnabled() || loginStartupSessionAllowed()) return 10;
        if (loginStartupSupported() || loginStartupState().status != LoginStartupStatus::Unavailable) return 11;
        QString error;
        if (loginStartupSetEnabled(true, &error) || error.isEmpty()) return 12;
        if (loginStartupSetEnabled(false, &error) || error.isEmpty()) return 13;
        if (loginStartupSetEnabled(true, QStringLiteral("relative"), QStringLiteral("relative"), &error)) return 14;
        return 0;
    }
    LoginStartupTests tests;
    return QTest::qExec(&tests, argc, argv);
}
#include "login_startup_tests.moc"
