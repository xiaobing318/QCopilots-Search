#pragma once

#include <QByteArray>
#include <QString>

namespace qcopilots_search {

enum class LoginStartupStatus { Disabled, Enabled, ApprovalRequired, Unavailable, Error };

struct LoginStartupState {
    LoginStartupStatus status = LoginStartupStatus::Unavailable;
    QString message;
};

bool loginStartupSupported();
bool loginStartupSetEnabled(bool enabled, QString *error = nullptr);
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

} // namespace qcopilots_search
