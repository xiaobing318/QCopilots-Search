#pragma once

#include "core/runtime_paths.h"

#include <QHash>
#include <QJsonObject>
#include <QLocalSocket>
#include <QObject>
#include <QTimer>

namespace qcopilots_search {

// Asynchronous local IPC client. All calls belong on this object's Qt thread.
// Requests queued during connection keep their own deadlines. A request on a
// disconnected client starts ensureService automatically.
class ServiceClient final : public QObject {
    Q_OBJECT
public:
    explicit ServiceClient(const RuntimePaths &paths, QObject *parent = nullptr);
    ~ServiceClient() override;

    bool isConnected() const;
    const RuntimePaths &paths() const { return paths_; }
    void setAutoStart(bool enabled) { autoStart_ = enabled; if (!enabled) ensureMode_ = false; }
    bool autoStart() const { return autoStart_; }
    void connectToService();
    void ensureService(int timeoutMs = 10000);
    QString request(const QString &method, const QJsonObject &params = {},
                    int timeoutMs = 30000);
    void cancel(const QString &requestId);
    void disconnectFromService();

signals:
    void connected();
    void disconnected();
    void response(const QString &id, const QJsonObject &result);
    void failed(const QString &id, const QString &code, const QString &message);
    void eventReceived(const QString &name, const QJsonObject &data);

private:
    struct PendingRequest {
        QJsonObject message;
        QTimer *timer = nullptr;
        bool sent = false;
    };
    void attemptConnection();
    void socketConnected();
    void socketDisconnected();
    void socketError(QLocalSocket::LocalSocketError error);
    void readAvailable();
    void sendQueuedRequests();
    bool sendMessage(QJsonObject message, QString *error = nullptr);
    bool launchService(QString *error);
    bool readToken(QString *error);
    void failPending(const QString &code, const QString &message);
    void finishFailure(const QString &id, const QString &code, const QString &message);
    void sendCancellation(const QString &id);

    RuntimePaths paths_;
    QLocalSocket socket_;
    QTimer connectionDeadline_;
    QTimer retryTimer_;
    QHash<QString, PendingRequest> pending_;
    QByteArray incoming_;
    QString token_;
    QString lastConnectionError_;
    bool ensureMode_ = false;
    bool autoStart_ = true;
    bool launching_ = false;
    bool ready_ = false;
    bool intentionalDisconnect_ = false;
};

} // namespace qcopilots_search
