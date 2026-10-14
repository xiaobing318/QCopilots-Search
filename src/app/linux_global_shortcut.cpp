#include "linux_global_shortcut.h"

#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusError>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusObjectPath>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDBusServiceWatcher>
#include <QGuiApplication>
#include <QSet>
#include <QSocketNotifier>
#include <QTimer>
#include <QUuid>
#include <QVariantMap>
#include <cstdlib>

#ifdef QCOPILOTS_SEARCH_HAS_XCB
#include <xcb/xcb.h>
#endif

namespace qcopilots_search {
struct PortalShortcut {
    QString id;
    QVariantMap properties;
};
using PortalShortcuts = QList<PortalShortcut>;

QDBusArgument &operator<<(QDBusArgument &argument, const PortalShortcut &shortcut) {
    argument.beginStructure();
    argument << shortcut.id << shortcut.properties;
    argument.endStructure();
    return argument;
}
const QDBusArgument &operator>>(const QDBusArgument &argument, PortalShortcut &shortcut) {
    argument.beginStructure();
    argument >> shortcut.id >> shortcut.properties;
    argument.endStructure();
    return argument;
}
}
Q_DECLARE_METATYPE(qcopilots_search::PortalShortcut)
Q_DECLARE_METATYPE(qcopilots_search::PortalShortcuts)

namespace qcopilots_search {
namespace {
const QString portalService = QStringLiteral("org.freedesktop.portal.Desktop");
const QString portalPath = QStringLiteral("/org/freedesktop/portal/desktop");
const QString portalInterface = QStringLiteral("org.freedesktop.portal.GlobalShortcuts");
const QString requestInterface = QStringLiteral("org.freedesktop.portal.Request");
const QString sessionInterface = QStringLiteral("org.freedesktop.portal.Session");
const QString shortcutId = QStringLiteral("show-search");

QString newToken() {
    return QStringLiteral("qcs_") + QUuid::createUuid().toString(QUuid::Id128);
}
bool portalUnavailable(const QDBusError &error) {
    return error.type() == QDBusError::ServiceUnknown || error.type() == QDBusError::UnknownMethod ||
        error.type() == QDBusError::UnknownInterface || error.type() == QDBusError::Disconnected ||
        error.type() == QDBusError::NotSupported || error.type() == QDBusError::NoServer ||
        error.type() == QDBusError::NoNetwork || error.type() == QDBusError::NoReply ||
        error.type() == QDBusError::Timeout || error.type() == QDBusError::TimedOut ||
        error.name() == QStringLiteral("org.freedesktop.portal.Error.NotSupported");
}
bool permissionDenied(const QDBusError &error) {
    return error.type() == QDBusError::AccessDenied ||
        error.name() == QStringLiteral("org.freedesktop.portal.Error.NotAllowed");
}
QString sessionPath(const QVariant &value) {
    if (value.metaType() == QMetaType::fromType<QDBusObjectPath>()) return value.value<QDBusObjectPath>().path();
    return value.toString(); // CreateSession returns a string, not an object path.
}
}

struct LinuxGlobalShortcut::Impl {
    enum class Request { None, CreateSession, BindShortcuts };
    LinuxGlobalShortcut *q;
    QDBusConnection bus = QDBusConnection::sessionBus();
    QDBusServiceWatcher *serviceWatcher;
    QTimer *requestTimeout;
    QString parentWindow;
    QString backend = QStringLiteral("none");
    QString session;
    QString requestPath;
    Request request = Request::None;
    quint64 generation = 0;
    bool enabled = false;
    bool permissionStopped = false;
    bool portalLost = false;
    bool portalSignals = false;
    LinuxShortcutState state = LinuxShortcutState::Unavailable;
#ifdef QCOPILOTS_SEARCH_HAS_XCB
    xcb_connection_t *xcb = nullptr;
    QSocketNotifier *notifier = nullptr;
    QTimer *releaseTimer = nullptr;
    bool keyHeld = false;
    QSet<xcb_keycode_t> spaceKeys;
#endif

    explicit Impl(LinuxGlobalShortcut *owner) : q(owner),
        serviceWatcher(new QDBusServiceWatcher(portalService, bus, QDBusServiceWatcher::WatchForOwnerChange, owner)),
        requestTimeout(new QTimer(owner)) {
        requestTimeout->setSingleShot(true);
        requestTimeout->setInterval(120000);
        QObject::connect(requestTimeout, &QTimer::timeout, q, [this] {
            clearPortal(true);
            setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("The desktop did not finish the shortcut request. Use Retry shortcut in the main window."));
        });
        QObject::connect(serviceWatcher, &QDBusServiceWatcher::serviceOwnerChanged, q,
                         [this](const QString &, const QString &oldOwner, const QString &newOwner) {
            if (!enabled) return;
            if (!oldOwner.isEmpty()) {
                portalLost = true;
                clearPortal(false);
                if (!permissionStopped) fallback(LinuxGlobalShortcut::tr("The desktop shortcut service disconnected."));
                else setState(LinuxShortcutState::Unavailable, {}, LinuxGlobalShortcut::tr("The desktop shortcut service disconnected."));
            }
            if (!permissionStopped && !newOwner.isEmpty() && (portalLost || state == LinuxShortcutState::Unavailable)) {
                portalLost = false;
                closeXcb();
                createSession();
            }
        });
    }

    void setState(LinuxShortcutState value, const QString &binding, const QString &message) {
        state = value;
        emit q->statusChanged(value, binding, message);
    }
    void sendClose(const QString &path, const QString &interface) {
        if (path.isEmpty() || !bus.isConnected()) return;
        auto message = QDBusMessage::createMethodCall(portalService, path, interface, QStringLiteral("Close"));
        bus.send(message);
    }
    void clearRequest(bool close) {
        requestTimeout->stop();
        if (!requestPath.isEmpty()) {
            bus.disconnect(portalService, requestPath, requestInterface, QStringLiteral("Response"),
                           q, SLOT(portalResponse(QDBusMessage)));
            if (close) sendClose(requestPath, requestInterface);
        }
        requestPath.clear();
        request = Request::None;
    }
    void clearPortal(bool close) {
        ++generation;
        clearRequest(close);
        if (!session.isEmpty()) {
            bus.disconnect(portalService, session, sessionInterface, QStringLiteral("Closed"),
                           q, SLOT(portalClosed(QDBusMessage)));
            if (close) sendClose(session, sessionInterface);
        }
        session.clear();
        if (portalSignals) {
            bus.disconnect(portalService, portalPath, portalInterface, QStringLiteral("Activated"),
                           q, SLOT(portalActivated(QDBusMessage)));
            bus.disconnect(portalService, portalPath, portalInterface, QStringLiteral("ShortcutsChanged"),
                           q, SLOT(portalShortcutsChanged(QDBusMessage)));
            portalSignals = false;
        }
    }
    bool connectPortalSignals() {
        portalSignals = true; // Also disconnect a partially successful subscription.
        return bus.connect(portalService, portalPath, portalInterface, QStringLiteral("Activated"),
                           q, SLOT(portalActivated(QDBusMessage))) &&
            bus.connect(portalService, portalPath, portalInterface, QStringLiteral("ShortcutsChanged"),
                        q, SLOT(portalShortcutsChanged(QDBusMessage)));
    }
    void call(Request kind, const QString &method, QVariantList arguments, QVariantMap options) {
        const QString token = newToken();
        options.insert(QStringLiteral("handle_token"), token);
        arguments.append(options);
        QString sender = bus.baseService();
        sender.remove(0, 1);
        sender.replace('.', '_');
        requestPath = portalPath + QStringLiteral("/request/") + sender + '/' + token;
        request = kind;
        if (!bus.connect(portalService, requestPath, requestInterface, QStringLiteral("Response"),
                         q, SLOT(portalResponse(QDBusMessage)))) {
            clearPortal(true);
            setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("Could not listen for the desktop shortcut response."));
            return;
        }
        auto message = QDBusMessage::createMethodCall(portalService, portalPath, portalInterface, method);
        message.setArguments(arguments);
        const auto currentGeneration = generation;
        const QString expectedPath = requestPath;
        requestTimeout->start();
        auto *watcher = new QDBusPendingCallWatcher(bus.asyncCall(message), q);
        QObject::connect(watcher, &QDBusPendingCallWatcher::finished, q,
                         [this, watcher, currentGeneration, expectedPath] {
            const QDBusPendingReply<QDBusObjectPath> reply = *watcher;
            watcher->deleteLater();
            // A Response may arrive before the method reply, or stop/restart may
            // have replaced this request. Neither may mutate the new session.
            if (!enabled || currentGeneration != generation || requestPath != expectedPath) return;
            if (reply.isError()) {
                const auto error = reply.error();
                clearPortal(true);
                if (permissionDenied(error)) {
                    permissionStopped = true;
                    setState(LinuxShortcutState::Denied, {}, LinuxGlobalShortcut::tr("The desktop denied the global shortcut request."));
                } else if (portalUnavailable(error)) {
                    portalLost = true;
                    fallback(LinuxGlobalShortcut::tr("The desktop does not provide the global shortcut portal."));
                } else {
                    setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("The desktop shortcut request failed: %1").arg(error.message()));
                }
                return;
            }
            const QString returnedPath = reply.value().path();
            if (returnedPath.isEmpty() || returnedPath == QStringLiteral("/")) {
                clearPortal(true);
                setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("The desktop returned an invalid shortcut request."));
                return;
            }
            if (returnedPath != requestPath) {
                bus.disconnect(portalService, requestPath, requestInterface, QStringLiteral("Response"),
                               q, SLOT(portalResponse(QDBusMessage)));
                requestPath = returnedPath;
                if (!bus.connect(portalService, requestPath, requestInterface, QStringLiteral("Response"),
                                 q, SLOT(portalResponse(QDBusMessage)))) {
                    clearPortal(true);
                    setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("Could not listen for the desktop shortcut response."));
                }
            }
        });
    }
    void createSession() {
        clearPortal(true);
        backend = QStringLiteral("portal");
        setState(LinuxShortcutState::Pending, {}, LinuxGlobalShortcut::tr("Waiting for the desktop to configure the global shortcut…"));
        if (!bus.isConnected()) {
            portalLost = true;
            fallback(LinuxGlobalShortcut::tr("The desktop session bus is unavailable."));
            return;
        }
        if (!connectPortalSignals()) {
            clearPortal(true);
            setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("Could not listen for desktop shortcut events."));
            return;
        }
        call(Request::CreateSession, QStringLiteral("CreateSession"), {},
             {{QStringLiteral("session_handle_token"), newToken()}});
    }
    void bindShortcuts() {
        const PortalShortcuts shortcuts{{shortcutId,
            {{QStringLiteral("description"), LinuxGlobalShortcut::tr("Show QCopilots Search")},
             {QStringLiteral("preferred_trigger"), QStringLiteral("CTRL+SHIFT+space")}}}};
        call(Request::BindShortcuts, QStringLiteral("BindShortcuts"),
             {QVariant::fromValue(QDBusObjectPath(session)), QVariant::fromValue(shortcuts), parentWindow}, {});
    }
    void updateBinding(const QVariant &value) {
        const auto shortcuts = qdbus_cast<PortalShortcuts>(value);
        for (const auto &shortcut : shortcuts) {
            if (shortcut.id != shortcutId) continue;
            const QString binding = shortcut.properties.value(QStringLiteral("trigger_description")).toString().trimmed();
            if (!binding.isEmpty()) {
                permissionStopped = false;
                setState(LinuxShortcutState::Active, binding, binding);
                return;
            }
        }
        // An empty successful result is not a binding and must never cause an
        // X11 grab that bypasses the user's desktop shortcut choice.
        permissionStopped = true;
        setState(LinuxShortcutState::Unavailable, {}, LinuxGlobalShortcut::tr("No global shortcut is assigned. Use the main window or tray, or configure it in desktop settings."));
    }

    void closeXcb() {
#ifdef QCOPILOTS_SEARCH_HAS_XCB
        if (notifier) { delete notifier; notifier = nullptr; }
        if (releaseTimer) { delete releaseTimer; releaseTimer = nullptr; }
        if (xcb) { xcb_disconnect(xcb); xcb = nullptr; }
        spaceKeys.clear();
        keyHeld = false;
#endif
    }
    void fallback(const QString &reason) {
        closeXcb();
        backend = QStringLiteral("none");
        if (QGuiApplication::platformName() != QStringLiteral("xcb") ||
            qEnvironmentVariable("XDG_SESSION_TYPE").compare(QStringLiteral("wayland"), Qt::CaseInsensitive) == 0 ||
            !qEnvironmentVariableIsEmpty("WAYLAND_DISPLAY")) {
            setState(LinuxShortcutState::Unavailable, {}, reason + LinuxGlobalShortcut::tr(" Use the main window or tray."));
            return;
        }
#ifdef QCOPILOTS_SEARCH_HAS_XCB
        xcb = xcb_connect(nullptr, nullptr);
        if (!xcb || xcb_connection_has_error(xcb)) {
            closeXcb();
            setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("Could not connect to the X11 server for the global shortcut."));
            return;
        }
        const auto *setup = xcb_get_setup(xcb);
        const auto mappingCookie = xcb_get_keyboard_mapping(xcb, setup->min_keycode,
            static_cast<uint8_t>(setup->max_keycode - setup->min_keycode + 1));
        auto *mapping = xcb_get_keyboard_mapping_reply(xcb, mappingCookie, nullptr);
        auto *modifiers = xcb_get_modifier_mapping_reply(xcb, xcb_get_modifier_mapping(xcb), nullptr);
        if (!mapping || !modifiers) {
            std::free(mapping);
            std::free(modifiers);
            closeXcb();
            setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("Could not read the X11 keyboard map."));
            return;
        }
        QSet<xcb_keycode_t> numLockKeys;
        const auto *keysyms = xcb_get_keyboard_mapping_keysyms(mapping);
        for (int key = setup->min_keycode; key <= setup->max_keycode; ++key) {
            for (int column = 0; column < mapping->keysyms_per_keycode; ++column) {
                const auto sym = keysyms[(key - setup->min_keycode) * mapping->keysyms_per_keycode + column];
                if (sym == 0x20 && column < 2) spaceKeys.insert(static_cast<xcb_keycode_t>(key));
                if (sym == 0xff7f) numLockKeys.insert(static_cast<xcb_keycode_t>(key));
            }
        }
        uint16_t numLockMask = 0;
        const auto *modifierKeys = xcb_get_modifier_mapping_keycodes(modifiers);
        for (int modifier = 0; modifier < 8; ++modifier)
            for (int i = 0; i < modifiers->keycodes_per_modifier; ++i)
                if (numLockKeys.contains(modifierKeys[modifier * modifiers->keycodes_per_modifier + i]))
                    numLockMask |= static_cast<uint16_t>(1u << modifier);
        std::free(mapping);
        std::free(modifiers);
        bool grabbed = !spaceKeys.isEmpty();
        const QSet<uint16_t> ignoredMasks{0, XCB_MOD_MASK_LOCK, numLockMask,
                                        static_cast<uint16_t>(XCB_MOD_MASK_LOCK | numLockMask)};
        for (auto screen = xcb_setup_roots_iterator(setup); screen.rem && grabbed; xcb_screen_next(&screen)) {
            for (const auto key : spaceKeys) {
                for (const auto ignored : ignoredMasks) {
                    const auto cookie = xcb_grab_key_checked(xcb, 0, screen.data->root,
                        static_cast<uint16_t>(XCB_MOD_MASK_CONTROL | XCB_MOD_MASK_SHIFT | ignored),
                        key, XCB_GRAB_MODE_ASYNC, XCB_GRAB_MODE_ASYNC);
                    if (auto *error = xcb_request_check(xcb, cookie)) {
                        std::free(error);
                        grabbed = false;
                    }
                }
            }
        }
        if (!grabbed) {
            closeXcb(); // Disconnect also releases any partial set of grabs.
            setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("Ctrl+Shift+Space could not be registered on X11. It may already be in use."));
            return;
        }
        xcb_flush(xcb);
        releaseTimer = new QTimer(q);
        releaseTimer->setSingleShot(true);
        releaseTimer->setInterval(20);
        QObject::connect(releaseTimer, &QTimer::timeout, q, [this] { keyHeld = false; });
        notifier = new QSocketNotifier(xcb_get_file_descriptor(xcb), QSocketNotifier::Read, q);
        QObject::connect(notifier, &QSocketNotifier::activated, q, [this] {
            while (auto *event = xcb_poll_for_event(xcb)) {
                const auto kind = event->response_type & 0x7f;
                if (kind == XCB_KEY_PRESS || kind == XCB_KEY_RELEASE) {
                    const auto *key = reinterpret_cast<xcb_key_press_event_t *>(event);
                    if (spaceKeys.contains(key->detail)) {
                        if (kind == XCB_KEY_RELEASE) releaseTimer->start();
                        else {
                            releaseTimer->stop();
                            if (!keyHeld) { keyHeld = true; emit q->activated({}); }
                        }
                    }
                }
                std::free(event);
            }
            if (xcb_connection_has_error(xcb)) {
                closeXcb();
                backend = QStringLiteral("none");
                setState(LinuxShortcutState::Failed, {}, LinuxGlobalShortcut::tr("The X11 shortcut connection was lost."));
            }
        });
        backend = QStringLiteral("x11");
        setState(LinuxShortcutState::Active, QStringLiteral("Ctrl+Shift+Space"),
                 LinuxGlobalShortcut::tr("Ctrl+Shift+Space (X11)"));
#else
        setState(LinuxShortcutState::Unavailable, {}, reason + LinuxGlobalShortcut::tr(" This build has no X11 fallback. Use the main window or tray."));
#endif
    }
};

LinuxGlobalShortcut::LinuxGlobalShortcut(QObject *parent) : QObject(parent), d_(new Impl(this)) {
    qRegisterMetaType<LinuxShortcutState>();
    qDBusRegisterMetaType<PortalShortcut>();
    qDBusRegisterMetaType<PortalShortcuts>();
}
LinuxGlobalShortcut::~LinuxGlobalShortcut() {
    d_->enabled = false;
    d_->clearPortal(true);
    d_->closeXcb();
    delete d_;
}
void LinuxGlobalShortcut::start(const QString &parentWindow) {
    stop();
    d_->parentWindow = parentWindow;
    d_->enabled = true;
    d_->permissionStopped = false;
    d_->portalLost = false;
    d_->createSession();
}
void LinuxGlobalShortcut::stop() {
    d_->enabled = false;
    d_->clearPortal(true);
    d_->closeXcb();
    d_->backend = QStringLiteral("none");
    d_->setState(LinuxShortcutState::Unavailable, {}, tr("Global shortcut is not registered."));
}
QString LinuxGlobalShortcut::backend() const { return d_->backend; }
void LinuxGlobalShortcut::portalResponse(const QDBusMessage &message) {
    if (!d_->enabled || message.path() != d_->requestPath || message.arguments().size() != 2) return;
    const auto request = d_->request;
    const uint response = message.arguments()[0].toUInt();
    const auto results = qdbus_cast<QVariantMap>(message.arguments()[1]);
    d_->clearRequest(false);
    if (response != 0) {
        d_->clearPortal(true);
        if (response == 1) {
            d_->permissionStopped = true;
            d_->setState(LinuxShortcutState::Denied, {}, tr("Global shortcut registration was cancelled. Use the main window or tray."));
        } else {
            d_->setState(LinuxShortcutState::Failed, {}, tr("The desktop could not register the global shortcut. Use the main window or tray."));
        }
        return;
    }
    if (request == Impl::Request::CreateSession) {
        d_->session = sessionPath(results.value(QStringLiteral("session_handle")));
        if (!d_->session.startsWith(portalPath + QStringLiteral("/session/")) ||
            QDBusObjectPath(d_->session).path().isEmpty()) {
            d_->session.clear();
            d_->clearPortal(true);
            d_->setState(LinuxShortcutState::Failed, {}, tr("The desktop returned an invalid shortcut session."));
            return;
        }
        if (!d_->bus.connect(portalService, d_->session, sessionInterface, QStringLiteral("Closed"),
                             this, SLOT(portalClosed(QDBusMessage)))) {
            d_->clearPortal(true);
            d_->setState(LinuxShortcutState::Failed, {}, tr("Could not monitor the desktop shortcut session."));
            return;
        }
        d_->bindShortcuts();
    } else if (request == Impl::Request::BindShortcuts) {
        d_->updateBinding(results.value(QStringLiteral("shortcuts")));
    }
}
void LinuxGlobalShortcut::portalActivated(const QDBusMessage &message) {
    const auto arguments = message.arguments();
    if (!d_->enabled || d_->state != LinuxShortcutState::Active || d_->session.isEmpty() || arguments.size() != 4 ||
        sessionPath(arguments[0]) != d_->session || arguments[1].toString() != shortcutId) return;
    const auto options = qdbus_cast<QVariantMap>(arguments[3]);
    emit activated(options.value(QStringLiteral("activation_token")).toString());
}
void LinuxGlobalShortcut::portalShortcutsChanged(const QDBusMessage &message) {
    const auto arguments = message.arguments();
    if (!d_->enabled || d_->session.isEmpty() || arguments.size() != 2 || sessionPath(arguments[0]) != d_->session) return;
    d_->updateBinding(arguments[1]);
}
void LinuxGlobalShortcut::portalClosed(const QDBusMessage &message) {
    if (!d_->enabled || message.path() != d_->session) return;
    d_->clearPortal(false);
    // The portal does not distinguish user revocation from graceful daemon
    // shutdown. Require an explicit retry after Closed instead of prompting
    // again or falling back to an X11 grab after a possible revocation.
    d_->permissionStopped = true;
    d_->setState(LinuxShortcutState::Unavailable, {}, tr("The desktop closed the global shortcut session. Use Retry shortcut in the main window."));
}
}
