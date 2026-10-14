#include "app/login_startup.h"

#include <QDir>
#include <QStandardPaths>
#include <QtTest>

using namespace qcopilots_search;

namespace {
// Decode just the serialized executable token, using the two distinct escaping
// stages specified by XDG. A round trip detects argument splitting or accidental
// expansion of executable paths containing quotes, dollars, or field codes.
QString decodeExecutable(const QByteArray &entry) {
    QByteArray encoded;
    for (const auto &line : entry.split('\n')) if (line.startsWith("Exec=")) encoded = line.mid(5);
    if (!encoded.endsWith(" --background")) return {};
    encoded.chop(qsizetype(sizeof(" --background") - 1));
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

    void testProfileCannotInspectOrModifyRealLoginState() {
        QVERIFY(QStandardPaths::isTestModeEnabled());
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

QTEST_GUILESS_MAIN(LoginStartupTests)
#include "login_startup_tests.moc"
