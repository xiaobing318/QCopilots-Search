#pragma once

#include <QByteArray>
#include <QString>
#include "core/runtime_paths.h"

namespace qcopilots_search {

enum class LoginStartupStatus { Disabled, Enabled, ApprovalRequired, Unavailable, Error };

struct LoginStartupState {
    LoginStartupStatus status = LoginStartupStatus::Unavailable;
    QString message;
    QString profileDir;
    QString modelDir;
};

// Explicit development paths and Qt test mode never access user login settings.
bool loginStartupSessionAllowed();
bool loginStartupSupported();
bool loginStartupSetEnabled(bool enabled, QString *error = nullptr);
bool loginStartupSetEnabled(bool enabled, const QString &profileDir,
                            const QString &modelDir, QString *error = nullptr);
// Resolve only after native launch detection is complete. A sign-in launch
// loads the persisted target, while manual and isolated launches use arguments
// and environment. Failure is returned without creating runtime directories.
RuntimePaths resolveLoginStartupPaths(const QString &profileDir, const QString &modelDir,
                                     bool loginLaunch);
LoginStartupState loginStartupState();

// Call before constructing QApplication so the macOS launch notification can be
// observed without taking over Qt's Apple event handler.
void prepareLoginStartupDetection();
bool isLoginStartupLaunch();
// Call from the event loop before initially showing the main window. A false
// return means native launch notification delivery is still pending. Queue a
// retry and recompute isLoginStartupLaunch() before deciding visibility again.
// On Windows, Linux and non-bundle macOS executables this is an immediate no-op.
bool completeLoginStartupDetection(bool showForeground);

// Pure XDG desktop-file serializer. An empty result means that the executable
// path cannot be represented safely in an Exec key.
QByteArray desktopAutostartEntry(const QString &executable, bool enabled);
// Parse a supplied desktop entry without accessing native startup settings.
LoginStartupState desktopAutostartState(const QByteArray &entry, const QString &executable,
                                       const QByteArray &currentDesktops = {});
#ifdef Q_OS_WIN
// Return an empty command and an error if the executable cannot be registered.
QString windowsLoginStartupCommand(const QString &executable, QString *error = nullptr);
#endif

} // namespace qcopilots_search
