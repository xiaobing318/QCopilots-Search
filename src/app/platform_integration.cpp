#include "platform_integration.h"
#include "platform_external_open.h"
#include "core/runtime_paths.h"
#ifdef Q_OS_LINUX
#include "linux_global_shortcut.h"
#endif
#include <QCoreApplication>
#include <QDesktopServices>
#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QGuiApplication>
#include <QUrl>
#ifdef Q_OS_WIN
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#elif defined(Q_OS_MACOS)
#include <Carbon/Carbon.h>
#endif

namespace qcopilots_search {
namespace {
constexpr int hotkeyId = 0x5143;
#ifdef Q_OS_MACOS
OSStatus macHotkey(EventHandlerCallRef, EventRef event, void *context) {
    EventHotKeyID id{};
    GetEventParameter(event, kEventParamDirectObject, typeEventHotKeyID, nullptr, sizeof(id), nullptr, &id);
    if (id.id == hotkeyId) emit static_cast<PlatformIntegration *>(context)->activated();
    return noErr;
}
#endif
bool failure(QString *error, const QString &message) { if (error) *error = message; return false; }
}
PlatformIntegration::PlatformIntegration(QObject *parent) : QObject(parent) {}
PlatformIntegration::~PlatformIntegration() {
    if (QCoreApplication::instance()) QCoreApplication::instance()->removeNativeEventFilter(this);
#ifdef Q_OS_WIN
    if (registered_) UnregisterHotKey(nullptr, hotkeyId);
#elif defined(Q_OS_MACOS)
    if (hotkey_) UnregisterEventHotKey(static_cast<EventHotKeyRef>(hotkey_));
    if (handler_) RemoveEventHandler(static_cast<EventHandlerRef>(handler_));
#endif
}
bool PlatformIntegration::registerGlobalShortcut() {
    if (registered_ || shortcutInfo_.state == GlobalShortcutState::Pending) return registered_;
#ifdef Q_OS_WIN
    shortcutInfo_.backend = QStringLiteral("win32");
    registered_ = RegisterHotKey(nullptr, hotkeyId, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE);
    if (registered_) QCoreApplication::instance()->installNativeEventFilter(this);
    status_ = registered_ ? QStringLiteral("Ctrl+Shift+Space") : QStringLiteral("Global shortcut is already in use. Use the tray or main window.");
#elif defined(Q_OS_MACOS)
    shortcutInfo_.backend = QStringLiteral("carbon");
    if (handler_) { RemoveEventHandler(static_cast<EventHandlerRef>(handler_)); handler_ = nullptr; }
    EventTypeSpec type{kEventClassKeyboard, kEventHotKeyPressed};
    EventHandlerRef handler = nullptr;
    EventHotKeyRef key = nullptr;
    const OSStatus result = InstallApplicationEventHandler(&macHotkey, 1, &type, this, &handler);
    EventHotKeyID id{'QCPS', hotkeyId};
    registered_ = result == noErr && RegisterEventHotKey(kVK_Space, controlKey | shiftKey, id, GetApplicationEventTarget(), 0, &key) == noErr;
    hotkey_ = key;
    handler_ = handler;
    status_ = registered_ ? QStringLiteral("Ctrl+Shift+Space") : QStringLiteral("Global shortcut unavailable. Use the tray or main window.");
#elif defined(Q_OS_LINUX)
    if (!linuxShortcut_) {
        linuxShortcut_ = new LinuxGlobalShortcut(this);
        connect(linuxShortcut_, &LinuxGlobalShortcut::statusChanged, this,
                [this](LinuxShortcutState state, const QString &binding, const QString &message) {
            switch (state) {
            case LinuxShortcutState::Unavailable: shortcutInfo_.state = GlobalShortcutState::Unavailable; break;
            case LinuxShortcutState::Pending: shortcutInfo_.state = GlobalShortcutState::Pending; break;
            case LinuxShortcutState::Active: shortcutInfo_.state = GlobalShortcutState::Active; break;
            case LinuxShortcutState::Denied: shortcutInfo_.state = GlobalShortcutState::Denied; break;
            case LinuxShortcutState::Failed: shortcutInfo_.state = GlobalShortcutState::Failed; break;
            }
            shortcutInfo_.backend = linuxShortcut_->backend();
            shortcutInfo_.binding = binding;
            shortcutInfo_.message = message;
            registered_ = state == LinuxShortcutState::Active;
            status_ = message.isEmpty() ? binding : message;
            emit shortcutStatusChanged();
        });
        connect(linuxShortcut_, &LinuxGlobalShortcut::activated, this, [this](const QString &token) {
            // Keep the compositor token scoped to this user-triggered activation.
            // Forward the standard token to the platform during activation.
            // Compositor focus still requires native Qt/Wayland acceptance tests.
            const bool hadToken = qEnvironmentVariableIsSet("XDG_ACTIVATION_TOKEN");
            const QByteArray previous = qgetenv("XDG_ACTIVATION_TOKEN");
            if (!token.isEmpty()) qputenv("XDG_ACTIVATION_TOKEN", token.toUtf8());
            emit activated();
            if (!token.isEmpty()) {
                if (hadToken) qputenv("XDG_ACTIVATION_TOKEN", previous);
                else qunsetenv("XDG_ACTIVATION_TOKEN");
            }
        });
    }
    linuxShortcut_->start();
    return registered_; // Portal permission and binding complete asynchronously.
#else
    status_ = QStringLiteral("Global shortcut is unavailable on this desktop. Use the tray or main window.");
#endif
    shortcutInfo_.state = registered_ ? GlobalShortcutState::Active : GlobalShortcutState::Unavailable;
    shortcutInfo_.binding = registered_ ? QStringLiteral("Ctrl+Shift+Space") : QString();
    shortcutInfo_.message = status_;
    emit shortcutStatusChanged();
    return registered_;
}
QString PlatformIntegration::shortcutStatus() const { return status_; }
GlobalShortcutInfo PlatformIntegration::shortcutInfo() const { return shortcutInfo_; }
bool PlatformIntegration::nativeEventFilter(const QByteArray &, void *message, qintptr *result) {
#ifdef Q_OS_WIN
    const auto *msg = static_cast<MSG *>(message);
    if (msg->message == WM_HOTKEY && msg->wParam == hotkeyId) { emit activated(); if (result) *result = 0; return true; }
#else
    Q_UNUSED(message)
    Q_UNUSED(result)
#endif
    return false;
}
bool PlatformIntegration::openHit(const SearchHit &hit, const QString &editorCommand, QString *error) {
    AppSettings settings;
    settings.editorCommand = editorCommand;
    return openHit(hit, settings, error);
}
bool PlatformIntegration::openHit(const SearchHit &hit, const AppSettings &settings, QString *error) {
    if (!QFileInfo(hit.path).isFile()) return failure(error, QStringLiteral("The source file no longer exists."));
    const auto command = buildOpenCommand(hit, settings, error);
    return command && executeOpenCommand(hit, *command, error);
}
bool PlatformIntegration::revealFile(const QString &path, QString *error) {
    if (!QFileInfo::exists(path)) return failure(error, QStringLiteral("The source file no longer exists."));
#ifdef Q_OS_WIN
    if (QProcess::startDetached(QStringLiteral("explorer.exe"), {QStringLiteral("/select,"), QDir::toNativeSeparators(path)})) return true;
#elif defined(Q_OS_MACOS)
    if (QProcess::startDetached(QStringLiteral("/usr/bin/open"), {QStringLiteral("-R"), path})) return true;
#else
    if (QDesktopServices::openUrl(QUrl::fromLocalFile(QFileInfo(path).absolutePath()))) return true;
#endif
    return failure(error, QStringLiteral("Could not reveal the file in the file manager."));
}
bool PlatformIntegration::supportsStartAtLogin() {
    return loginStartupSessionAllowed() && loginStartupSupported();
}
bool PlatformIntegration::setStartAtLogin(bool enabled, QString *error) {
    if (!loginStartupSessionAllowed())
        return failure(error, QStringLiteral("Login startup cannot be changed from a test session or a session with explicit directory arguments."));
    const auto paths = RuntimePaths::resolve();
    if (!paths.validateProfileLayout(error)) return false;
    return setStartAtLogin(enabled, paths.profileBaseDir, paths.modelDir, error);
}
bool PlatformIntegration::canSetStartAtLogin(const QString &profileBaseDirectory, const QString &modelsDirectory) {
    return RuntimePaths::isAbsoluteFilesystemPath(profileBaseDirectory)
        && RuntimePaths::isAbsoluteFilesystemPath(modelsDirectory) && supportsStartAtLogin();
}
bool PlatformIntegration::setStartAtLogin(bool enabled, const QString &profileBaseDirectory,
                                         const QString &modelsDirectory, QString *error) {
    if (!canSetStartAtLogin(profileBaseDirectory, modelsDirectory))
        return failure(error, QStringLiteral("Login startup requires a supported normal session and absolute profile and model directories."));
    return loginStartupSetEnabled(enabled, profileBaseDirectory, modelsDirectory, error);
}
LoginStartupState PlatformIntegration::startAtLoginState() {
    if (!loginStartupSessionAllowed())
        return {LoginStartupStatus::Unavailable, QStringLiteral("Login startup is unavailable for test sessions and explicit directory arguments.")};
    return loginStartupState();
}
}
