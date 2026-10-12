#pragma once

#include "core/runtime_paths.h"
#include <QJsonObject>
#include <QObject>

namespace qcopilots_search {
class ServiceClient;

// The only GUI boundary to authoritative application state. UI tests replace
// this transport, while integration tests use the real local service.
class AppBackend : public QObject {
    Q_OBJECT
public:
    using QObject::QObject;
    virtual void start() = 0;
    virtual QString request(const QString &method, const QJsonObject &params = {}, int timeoutMs = 30000) = 0;
    virtual void cancel(const QString &id) = 0;
signals:
    void connected();
    void disconnected();
    void response(QString id, QJsonObject result);
    void failed(QString id, QString code, QString message);
    void eventReceived(QString name, QJsonObject data);
};

class ServiceBackend final : public AppBackend {
    Q_OBJECT
public:
    explicit ServiceBackend(const RuntimePaths &paths, QObject *parent = nullptr);
    void setAutoStart(bool enabled);
    void start() override;
    QString request(const QString &method, const QJsonObject &params = {}, int timeoutMs = 30000) override;
    void cancel(const QString &id) override;
private:
    ServiceClient *client_;
};
}
