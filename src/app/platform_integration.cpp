#include "platform_integration.h"
#include <QCoreApplication>
#include <QDesktopServices>
#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QSettings>
#include <QStandardPaths>
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
    QCoreApplication::instance()->removeNativeEventFilter(this);
#ifdef Q_OS_WIN
    if (registered_) UnregisterHotKey(nullptr, hotkeyId);
#elif defined(Q_OS_MACOS)
    if (hotkey_) UnregisterEventHotKey(static_cast<EventHotKeyRef>(hotkey_));
    if (handler_) RemoveEventHandler(static_cast<EventHandlerRef>(handler_));
#endif
}
bool PlatformIntegration::registerGlobalShortcut() {
    if (registered_) return true;
#ifdef Q_OS_WIN
    registered_ = RegisterHotKey(nullptr, hotkeyId, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE);
    if (registered_) QCoreApplication::instance()->installNativeEventFilter(this);
    status_ = registered_ ? QStringLiteral("Ctrl+Shift+Space") : QStringLiteral("Global shortcut is already in use. Use the tray or main window.");
#elif defined(Q_OS_MACOS)
    EventTypeSpec type{kEventClassKeyboard, kEventHotKeyPressed};
    EventHandlerRef handler = nullptr;
    EventHotKeyRef key = nullptr;
    const OSStatus result = InstallApplicationEventHandler(&macHotkey, 1, &type, this, &handler);
    EventHotKeyID id{'QCPS', hotkeyId};
    registered_ = result == noErr && RegisterEventHotKey(kVK_Space, controlKey | shiftKey, id, GetApplicationEventTarget(), 0, &key) == noErr;
    hotkey_ = key;
    handler_ = handler;
    status_ = registered_ ? QStringLiteral("Ctrl+Shift+Space") : QStringLiteral("Global shortcut unavailable. Use the tray or main window.");
#else
    status_ = QStringLiteral("Global shortcut is unavailable on this desktop. Use the tray or main window.");
#endif
    return registered_;
}
QString PlatformIntegration::shortcutStatus() const { return status_; }
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
    if (!QFileInfo::exists(hit.path)) return failure(error, QStringLiteral("The source file no longer exists."));
    if (hit.lineStart > 0 && !editorCommand.trimmed().isEmpty()) {
        QStringList command = QProcess::splitCommand(editorCommand);
        if (command.isEmpty()) return failure(error, QStringLiteral("The editor command is empty."));
        QString program = command.takeFirst();
        bool substituted = false;
        for (auto &argument : command) {
            substituted |= argument.contains(QStringLiteral("{file}"));
            argument.replace(QStringLiteral("{file}"), hit.path);
            argument.replace(QStringLiteral("{line}"), QString::number(hit.lineStart));
            argument.replace(QStringLiteral("{cell}"), QString::number(hit.cell));
        }
        if (!substituted) {
            const QString executable = QFileInfo(program).completeBaseName().toLower();
            if (executable == "code" || executable == "code-insiders" || executable == "codium") command << "--goto" << hit.path + ':' + QString::number(hit.lineStart);
            else if (executable == "subl" || executable == "sublime_text") command << hit.path + ':' + QString::number(hit.lineStart);
            else command << hit.path;
        }
        if (!QProcess::startDetached(program, command)) return failure(error, QStringLiteral("Could not start the configured editor."));
        return true;
    }
    if (!QDesktopServices::openUrl(QUrl::fromLocalFile(hit.path))) return failure(error, QStringLiteral("No application could open this file."));
    return true;
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
#ifdef Q_OS_WIN
    return true;
#else
    return false;
#endif
}
bool PlatformIntegration::setStartAtLogin(bool enabled, QString *error) {
#ifdef Q_OS_WIN
    QSettings registry(QStringLiteral("HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"), QSettings::NativeFormat);
    const QString name = QStringLiteral("qcopilots-search");
    if (enabled) registry.setValue(name, '"' + QDir::toNativeSeparators(QCoreApplication::applicationFilePath()) + QStringLiteral("\" --background"));
    else registry.remove(name);
    registry.sync();
    if (registry.status() != QSettings::NoError) return failure(error, QStringLiteral("Could not change the Windows sign-in setting."));
    return true;
#else
    if (!enabled) return true;
    return failure(error, QStringLiteral("Automatic sign-in launch is unavailable on this platform. Use the system startup application settings."));
#endif
}
}
