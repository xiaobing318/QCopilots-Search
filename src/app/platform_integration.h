#pragma once
#include "login_startup.h"
#include "core/app_settings.h"
#include "core/types.h"
#include <QAbstractNativeEventFilter>
#include <QObject>
#include <QString>

namespace qcopilots_search {
enum class GlobalShortcutState { Unavailable, Pending, Active, Denied, Failed };
struct GlobalShortcutInfo {
    GlobalShortcutState state = GlobalShortcutState::Unavailable;
    QString backend;
    QString binding;
    QString message;
};
#ifdef Q_OS_LINUX
class LinuxGlobalShortcut;
#endif
class PlatformIntegration final : public QObject, public QAbstractNativeEventFilter {
    Q_OBJECT
public:
    explicit PlatformIntegration(QObject *parent = nullptr);
    ~PlatformIntegration() override;
    bool registerGlobalShortcut();
    QString shortcutStatus() const;
    GlobalShortcutInfo shortcutInfo() const;
    bool nativeEventFilter(const QByteArray &, void *message, qintptr *result) override;
    static bool openHit(const SearchHit &hit, const QString &editorCommand, QString *error = nullptr);
    // On success a nonempty message describes the dispatch/positioning limit.
    static bool openHit(const SearchHit &hit, const AppSettings &settings, QString *error = nullptr);
    static bool revealFile(const QString &path, QString *error = nullptr);
    static bool setStartAtLogin(bool enabled, QString *error = nullptr);
    // The saved target receives the user-selected base, not its profiles/vN child.
    static bool setStartAtLogin(bool enabled, const QString &profileBaseDirectory,
                                const QString &modelsDirectory, QString *error = nullptr);
    static bool canSetStartAtLogin(const QString &profileBaseDirectory, const QString &modelsDirectory);
    static LoginStartupState startAtLoginState();
    static bool supportsStartAtLogin();
signals:
    void activated();
    void shortcutStatusChanged();
private:
    bool registered_ = false;
    QString status_;
    GlobalShortcutInfo shortcutInfo_;
    void *hotkey_ = nullptr;
    void *handler_ = nullptr;
#ifdef Q_OS_LINUX
    LinuxGlobalShortcut *linuxShortcut_ = nullptr;
#endif
};
}
