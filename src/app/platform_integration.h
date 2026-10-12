#pragma once
#include "core/types.h"
#include <QAbstractNativeEventFilter>
#include <QObject>
#include <QString>

namespace qcopilots_search {
class PlatformIntegration final : public QObject, public QAbstractNativeEventFilter {
    Q_OBJECT
public:
    explicit PlatformIntegration(QObject *parent = nullptr);
    ~PlatformIntegration() override;
    bool registerGlobalShortcut();
    QString shortcutStatus() const;
    bool nativeEventFilter(const QByteArray &, void *message, qintptr *result) override;
    static bool openHit(const SearchHit &hit, const QString &editorCommand, QString *error = nullptr);
    static bool revealFile(const QString &path, QString *error = nullptr);
    static bool setStartAtLogin(bool enabled, QString *error = nullptr);
    static bool supportsStartAtLogin();
signals:
    void activated();
private:
    bool registered_ = false;
    QString status_;
    void *hotkey_ = nullptr;
    void *handler_ = nullptr;
};
}
