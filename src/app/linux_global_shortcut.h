#pragma once

#include <QObject>
#include <QString>

class QDBusMessage;

namespace qcopilots_search {

enum class LinuxShortcutState { Unavailable, Pending, Active, Denied, Failed };

// Uses the desktop portal first. The independent XCB connection is only a
// fallback for native X11 when the portal is unsupported or unreachable.
class LinuxGlobalShortcut final : public QObject {
    Q_OBJECT
public:
    explicit LinuxGlobalShortcut(QObject *parent = nullptr);
    ~LinuxGlobalShortcut() override;
    void start(const QString &parentWindow = {});
    void stop();
    QString backend() const;

signals:
    void statusChanged(qcopilots_search::LinuxShortcutState state,
                       const QString &binding, const QString &message);
    void activated(const QString &activationToken);

private slots:
    void portalResponse(const QDBusMessage &message);
    void portalActivated(const QDBusMessage &message);
    void portalShortcutsChanged(const QDBusMessage &message);
    void portalClosed(const QDBusMessage &message);

private:
    struct Impl;
    Impl *d_;
};

}

Q_DECLARE_METATYPE(qcopilots_search::LinuxShortcutState)
