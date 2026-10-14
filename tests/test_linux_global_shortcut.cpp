#include "app/linux_global_shortcut.h"

#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusError>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusObjectPath>
#include <QDBusVirtualObject>
#include <QGuiApplication>
#include <QSignalSpy>
#include <QTimer>
#include <QUuid>
#include <QtTest>
#include <memory>

namespace fake_portal {
struct Shortcut {
    QString id;
    QVariantMap properties;
};
using Shortcuts = QList<Shortcut>;

QDBusArgument &operator<<(QDBusArgument &argument, const Shortcut &shortcut) {
    argument.beginStructure();
    argument << shortcut.id << shortcut.properties;
    argument.endStructure();
    return argument;
}
const QDBusArgument &operator>>(const QDBusArgument &argument, Shortcut &shortcut) {
    argument.beginStructure();
    argument >> shortcut.id >> shortcut.properties;
    argument.endStructure();
    return argument;
}
}
Q_DECLARE_METATYPE(fake_portal::Shortcut)
Q_DECLARE_METATYPE(fake_portal::Shortcuts)

namespace {
using qcopilots_search::LinuxGlobalShortcut;
using qcopilots_search::LinuxShortcutState;

const QString service = QStringLiteral("org.freedesktop.portal.Desktop");
const QString desktopPath = QStringLiteral("/org/freedesktop/portal/desktop");
const QString shortcutInterface = QStringLiteral("org.freedesktop.portal.GlobalShortcuts");
const QString requestInterface = QStringLiteral("org.freedesktop.portal.Request");
const QString sessionInterface = QStringLiteral("org.freedesktop.portal.Session");
const QString shortcutId = QStringLiteral("show-search");

QString callerPath(QString sender) {
    sender.remove(0, 1);
    sender.replace('.', '_');
    return sender;
}

class FakePortal final : public QDBusVirtualObject {
public:
    enum class BindResult { Success, Empty, Cancelled, Denied, FailedResponse };

    explicit FakePortal(QDBusConnection connection) : bus(std::move(connection)) {}

    QDBusConnection bus;
    BindResult bindResult = BindResult::Success;
    bool earlyResponse = false;
    bool alternateRequestPath = false;
    bool unsupported = false;
    int createCalls = 0;
    int bindCalls = 0;
    int closeCalls = 0;
    QString currentSession;
    QString currentCaller;
    QString createSignature;
    QString bindSignature;
    QString requestedParent;
    fake_portal::Shortcuts requestedShortcuts;
    QString binding = QStringLiteral("Super+Alt+Space");

    QString introspect(const QString &) const override { return {}; }

    QVariant shortcuts(const QString &trigger) const {
        fake_portal::Shortcuts result;
        if (!trigger.isEmpty()) {
            result.append({shortcutId,
                {{QStringLiteral("description"), QStringLiteral("Show search")},
                 {QStringLiteral("trigger_description"), trigger}}});
        }
        return QVariant::fromValue(result);
    }

    bool handleMessage(const QDBusMessage &message, const QDBusConnection &connection) override {
        if (message.member() == QStringLiteral("Close") &&
            (message.interface() == requestInterface || message.interface() == sessionInterface)) {
            ++closeCalls;
            connection.send(message.createReply());
            return true;
        }
        if (message.interface() != shortcutInterface || message.path() != desktopPath) return false;
        const auto arguments = message.arguments();
        QVariantMap results;
        QVariantMap options;
        uint response = 0;
        if (message.member() == QStringLiteral("CreateSession")) {
            ++createCalls;
            createSignature = message.signature();
            if (createSignature != QStringLiteral("a{sv}") || arguments.size() != 1) return invalidArguments(message);
            if (unsupported) {
                connection.send(message.createErrorReply(QDBusError::UnknownMethod, QStringLiteral("Portal is unsupported")));
                return true;
            }
            options = qdbus_cast<QVariantMap>(arguments[0]);
            currentCaller = message.service();
            currentSession = desktopPath + QStringLiteral("/session/") + callerPath(currentCaller) + '/' +
                options.value(QStringLiteral("session_handle_token")).toString();
            // The portal specification intentionally uses legacy type 's' here.
            results.insert(QStringLiteral("session_handle"), currentSession);
        } else if (message.member() == QStringLiteral("BindShortcuts")) {
            ++bindCalls;
            bindSignature = message.signature();
            if (bindSignature != QStringLiteral("oa(sa{sv})sa{sv}") || arguments.size() != 4 ||
                arguments[0].value<QDBusObjectPath>().path() != currentSession) return invalidArguments(message);
            requestedShortcuts = qdbus_cast<fake_portal::Shortcuts>(arguments[1]);
            requestedParent = arguments[2].toString();
            options = qdbus_cast<QVariantMap>(arguments[3]);
            if (bindResult == BindResult::Denied) {
                connection.send(message.createErrorReply(QStringLiteral("org.freedesktop.portal.Error.NotAllowed"),
                                                        QStringLiteral("Denied by test desktop")));
                return true;
            }
            if (bindResult == BindResult::Cancelled) response = 1;
            if (bindResult == BindResult::FailedResponse) response = 2;
            results.insert(QStringLiteral("shortcuts"), shortcuts(bindResult == BindResult::Empty ? QString{} : binding));
        } else {
            return false;
        }
        QString path = desktopPath + QStringLiteral("/request/") + callerPath(message.service()) + '/' +
            options.value(QStringLiteral("handle_token")).toString();
        if (alternateRequestPath) path += QStringLiteral("_returned");
        auto responseMessage = QDBusMessage::createTargetedSignal(message.service(), path, requestInterface,
                                                                 QStringLiteral("Response"));
        responseMessage.setArguments({response, results});
        const auto methodReply = message.createReply(QVariant::fromValue(QDBusObjectPath(path)));
        message.setDelayedReply(true);
        if (earlyResponse) {
            connection.send(responseMessage);
            // Force Response to reach the client before its pending-call watcher.
            QTimer::singleShot(20, this, [connection, methodReply] { connection.send(methodReply); });
        } else {
            connection.send(methodReply);
            // A different returned Request path needs time for its subscription.
            QTimer::singleShot(20, this, [connection, responseMessage] { connection.send(responseMessage); });
        }
        return true;
    }

    bool emitActivated(const QString &token, const QString &session = {}, const QString &id = {}) {
        auto message = QDBusMessage::createTargetedSignal(currentCaller, desktopPath, shortcutInterface,
                                                         QStringLiteral("Activated"));
        message.setArguments({QVariant::fromValue(QDBusObjectPath(session.isEmpty() ? currentSession : session)),
                              id.isEmpty() ? shortcutId : id, QVariant::fromValue<qulonglong>(1234),
                              QVariantMap{{QStringLiteral("activation_token"), token}}});
        return bus.send(message);
    }

    bool emitChanged(const QString &trigger) {
        auto message = QDBusMessage::createTargetedSignal(currentCaller, desktopPath, shortcutInterface,
                                                         QStringLiteral("ShortcutsChanged"));
        message.setArguments({QVariant::fromValue(QDBusObjectPath(currentSession)), shortcuts(trigger)});
        return bus.send(message);
    }

    bool emitClosed() {
        auto message = QDBusMessage::createTargetedSignal(currentCaller, currentSession, sessionInterface,
                                                         QStringLiteral("Closed"));
        message.setArguments({QVariantMap{}});
        return bus.send(message);
    }

private:
    bool invalidArguments(const QDBusMessage &message) {
        bus.send(message.createErrorReply(QDBusError::InvalidArgs, QStringLiteral("Unexpected portal wire arguments")));
        return true;
    }
};

LinuxShortcutState lastState(const QSignalSpy &spy) {
    return spy.isEmpty() ? LinuxShortcutState::Failed : spy.last().at(0).value<LinuxShortcutState>();
}
QString lastBinding(const QSignalSpy &spy) { return spy.isEmpty() ? QString{} : spy.last().at(1).toString(); }
}

class LinuxGlobalShortcutTests final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() {
        QVERIFY2(qEnvironmentVariable("QCOPILOTS_SEARCH_ISOLATED_DBUS") == QStringLiteral("1"),
                 "Run this test through its dbus-run-session CTest entry. Never use the desktop session bus.");
        QVERIFY2(!qEnvironmentVariableIsEmpty("DBUS_SESSION_BUS_ADDRESS"), "The isolated session bus address is missing.");
        QVERIFY2(QGuiApplication::platformName() == QStringLiteral("offscreen"),
                 "The portal protocol suite requires QT_QPA_PLATFORM=offscreen and must not grab real keys.");
        qDBusRegisterMetaType<fake_portal::Shortcut>();
        qDBusRegisterMetaType<fake_portal::Shortcuts>();
        QVERIFY2(QDBusConnection::sessionBus().isConnected(), "The isolated test session bus is unavailable.");
    }

    void init() {
        connectionName_ = QStringLiteral("qcs-test-portal-") + QUuid::createUuid().toString(QUuid::Id128);
        auto bus = QDBusConnection::connectToBus(QDBusConnection::SessionBus, connectionName_);
        QVERIFY2(bus.isConnected(), qPrintable(bus.lastError().message()));
        portal_ = std::make_unique<FakePortal>(bus);
        QVERIFY(bus.registerVirtualObject(desktopPath, portal_.get(), QDBusConnection::SubPath));
        QVERIFY2(bus.registerService(service), qPrintable(bus.lastError().message()));
    }

    void cleanup() {
        if (portal_) {
            portal_->bus.unregisterService(service);
            portal_->bus.unregisterObject(desktopPath, QDBusConnection::UnregisterTree);
            portal_.reset();
        }
        QDBusConnection::disconnectFromBus(connectionName_);
    }

    void bindsWireContractAndUsesDesktopTrigger_data() {
        QTest::addColumn<bool>("early");
        QTest::addColumn<bool>("alternatePath");
        QTest::newRow("normal") << false << false;
        QTest::newRow("response-before-method-reply") << true << false;
        QTest::newRow("returned-request-path-differs") << false << true;
    }

    void bindsWireContractAndUsesDesktopTrigger() {
        QFETCH(bool, early);
        QFETCH(bool, alternatePath);
        portal_->earlyResponse = early;
        portal_->alternateRequestPath = alternatePath;
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        shortcut.start(QStringLiteral("x11:abc"));
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        QCOMPARE(shortcut.backend(), QStringLiteral("portal"));
        QCOMPARE(portal_->createCalls, 1);
        QCOMPARE(portal_->bindCalls, 1);
        QCOMPARE(portal_->createSignature, QStringLiteral("a{sv}"));
        QCOMPARE(portal_->bindSignature, QStringLiteral("oa(sa{sv})sa{sv}"));
        QCOMPARE(portal_->requestedParent, QStringLiteral("x11:abc"));
        QCOMPARE(portal_->requestedShortcuts.size(), 1);
        QCOMPARE(portal_->requestedShortcuts[0].id, shortcutId);
        QCOMPARE(portal_->requestedShortcuts[0].properties.value(QStringLiteral("preferred_trigger")).toString(),
                 QStringLiteral("CTRL+SHIFT+space"));
        QVERIFY(!portal_->requestedShortcuts[0].properties.value(QStringLiteral("description")).toString().isEmpty());
        QCOMPARE(lastBinding(states), portal_->binding);
        // The stale method replies must not replace the active binding or create another session.
        QTest::qWait(50);
        QCOMPARE(lastState(states), LinuxShortcutState::Active);
        QCOMPARE(portal_->createCalls, 1);
        QCOMPARE(portal_->bindCalls, 1);
    }

    void respectsUnassignedOrDeniedShortcut_data() {
        QTest::addColumn<int>("result");
        QTest::addColumn<int>("expectedState");
        QTest::newRow("empty-binding") << int(FakePortal::BindResult::Empty) << int(LinuxShortcutState::Unavailable);
        QTest::newRow("user-cancelled") << int(FakePortal::BindResult::Cancelled) << int(LinuxShortcutState::Denied);
        QTest::newRow("permission-denied") << int(FakePortal::BindResult::Denied) << int(LinuxShortcutState::Denied);
    }

    void respectsUnassignedOrDeniedShortcut() {
        QFETCH(int, result);
        QFETCH(int, expectedState);
        portal_->bindResult = static_cast<FakePortal::BindResult>(result);
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        shortcut.start();
        QTRY_COMPARE(portal_->bindCalls, 1);
        QTRY_COMPARE(int(lastState(states)), expectedState);
        QVERIFY(lastBinding(states).isEmpty());
        const int calls = portal_->createCalls;
        // Restarting the portal must not bypass a recorded desktop choice.
        QVERIFY(portal_->bus.unregisterService(service));
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Unavailable);
        QVERIFY(portal_->bus.registerService(service));
        QTest::qWait(100);
        QCOMPARE(portal_->createCalls, calls);
        for (const auto &event : states) QVERIFY(event[0].value<LinuxShortcutState>() != LinuxShortcutState::Active);
    }

    void updatesBindingAndForwardsActivationToken() {
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        QSignalSpy activations(&shortcut, &LinuxGlobalShortcut::activated);
        shortcut.start();
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        QVERIFY(portal_->emitActivated(QStringLiteral("wrong-session"), desktopPath + QStringLiteral("/session/other")));
        QVERIFY(portal_->emitActivated(QStringLiteral("wrong-id"), {}, QStringLiteral("another-shortcut")));
        QVERIFY(portal_->emitActivated(QStringLiteral("activation-token-42")));
        QTRY_COMPARE(activations.size(), 1);
        QCOMPARE(activations[0][0].toString(), QStringLiteral("activation-token-42"));
        QVERIFY(portal_->emitChanged(QStringLiteral("Meta+F9")));
        QTRY_COMPARE(lastBinding(states), QStringLiteral("Meta+F9"));
        QVERIFY(portal_->emitChanged({}));
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Unavailable);
        QVERIFY(portal_->emitActivated(QStringLiteral("disabled-token")));
        QTest::qWait(50);
        QCOMPARE(activations.size(), 1);
        QVERIFY(portal_->emitChanged(QStringLiteral("Ctrl+F10")));
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        QCOMPARE(lastBinding(states), QStringLiteral("Ctrl+F10"));
        QVERIFY(portal_->emitActivated(QStringLiteral("new-binding-token")));
        QTRY_COMPARE(activations.size(), 2);
    }

    void failedResponseDoesNotFallBack() {
        portal_->bindResult = FakePortal::BindResult::FailedResponse;
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        shortcut.start();
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Failed);
        QCOMPARE(portal_->bindCalls, 1);
        QCOMPARE(shortcut.backend(), QStringLiteral("portal"));
        QVERIFY(lastBinding(states).isEmpty());
        for (const auto &event : states) QVERIFY(event[0].value<LinuxShortcutState>() != LinuxShortcutState::Active);
    }

    void unsupportedPortalIsUnavailableOnNonX11Platform() {
        portal_->unsupported = true;
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        shortcut.start();
        QTRY_COMPARE(portal_->createCalls, 1);
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Unavailable);
        QCOMPARE(portal_->bindCalls, 0);
        QCOMPARE(shortcut.backend(), QStringLiteral("none"));
        QVERIFY(lastBinding(states).isEmpty());
    }

    void closedSessionStopsActivationUntilExplicitRetry() {
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        QSignalSpy activations(&shortcut, &LinuxGlobalShortcut::activated);
        shortcut.start();
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        QVERIFY(portal_->emitClosed());
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Unavailable);
        QVERIFY(portal_->emitActivated(QStringLiteral("closed-session-token")));
        QTest::qWait(50);
        QVERIFY(activations.isEmpty());
        QCOMPARE(portal_->createCalls, 1);
        shortcut.start();
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        QCOMPARE(portal_->createCalls, 2);
        QCOMPARE(portal_->bindCalls, 2);
    }

    void serviceRestartRespectsSessionClosure_data() {
        QTest::addColumn<bool>("gracefulClose");
        QTest::newRow("owner-disappears") << false;
        QTest::newRow("closed-before-owner-disappears") << true;
    }

    void serviceRestartRespectsSessionClosure() {
        QFETCH(bool, gracefulClose);
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        shortcut.start();
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        const QString firstSession = portal_->currentSession;
        if (gracefulClose) {
            QVERIFY(portal_->emitClosed());
            QTRY_COMPARE(lastState(states), LinuxShortcutState::Unavailable);
        }
        QVERIFY(portal_->bus.unregisterService(service));
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Unavailable);
        // Wait for owner loss even when Closed had already set Unavailable.
        QTest::qWait(50);
        QVERIFY(portal_->bus.registerService(service));
        if (gracefulClose) {
            // Closed can also mean user revocation. An owner replacement must
            // not bypass that choice, so this path requires explicit retry.
            QTest::qWait(100);
            QCOMPARE(portal_->createCalls, 1);
            QCOMPARE(lastState(states), LinuxShortcutState::Unavailable);
            shortcut.start();
        }
        QTRY_COMPARE(portal_->createCalls, 2);
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        QCOMPARE(shortcut.backend(), QStringLiteral("portal"));
        QCOMPARE(portal_->bindCalls, 2);
        QVERIFY(portal_->currentSession != firstSession);
    }

    void stopClosesSessionAndIgnoresFurtherSignals() {
        LinuxGlobalShortcut shortcut;
        QSignalSpy states(&shortcut, &LinuxGlobalShortcut::statusChanged);
        QSignalSpy activations(&shortcut, &LinuxGlobalShortcut::activated);
        shortcut.start();
        QTRY_COMPARE(lastState(states), LinuxShortcutState::Active);
        shortcut.stop();
        QCOMPARE(lastState(states), LinuxShortcutState::Unavailable);
        QCOMPARE(shortcut.backend(), QStringLiteral("none"));
        QTRY_VERIFY(portal_->closeCalls > 0);
        QVERIFY(portal_->emitActivated(QStringLiteral("stopped-token")));
        QVERIFY(portal_->emitChanged(QStringLiteral("Ctrl+Alt+A")));
        QTest::qWait(50);
        QVERIFY(activations.isEmpty());
        QCOMPARE(lastState(states), LinuxShortcutState::Unavailable);
    }

private:
    QString connectionName_;
    std::unique_ptr<FakePortal> portal_;
};

int main(int argc, char **argv) {
    QGuiApplication application(argc, argv);
    LinuxGlobalShortcutTests tests;
    return QTest::qExec(&tests, argc, argv);
}

#include "test_linux_global_shortcut.moc"
