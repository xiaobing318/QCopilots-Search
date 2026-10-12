#include "app_backend.h"
#include "service/service_client.h"

namespace qcopilots_search {
ServiceBackend::ServiceBackend(const RuntimePaths &paths, QObject *parent)
    : AppBackend(parent), client_(new ServiceClient(paths, this)) {
    connect(client_, &ServiceClient::connected, this, &AppBackend::connected);
    connect(client_, &ServiceClient::disconnected, this, &AppBackend::disconnected);
    connect(client_, &ServiceClient::response, this, &AppBackend::response);
    connect(client_, &ServiceClient::failed, this, &AppBackend::failed);
    connect(client_, &ServiceClient::eventReceived, this, &AppBackend::eventReceived);
}
void ServiceBackend::start() { client_->ensureService(); }
void ServiceBackend::setAutoStart(bool enabled) { client_->setAutoStart(enabled); }
QString ServiceBackend::request(const QString &method, const QJsonObject &params, int timeoutMs) {
    return client_->request(method, params, timeoutMs);
}
void ServiceBackend::cancel(const QString &id) { client_->cancel(id); }
}
