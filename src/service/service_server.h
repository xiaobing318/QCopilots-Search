#pragma once
#include "core/runtime_paths.h"
#include "storage/index_repository.h"
#include <QObject>
#include <QLocalServer>
#include <QProcess>
#include <functional>
#include <memory>

namespace qcopilots_search {

struct ServiceDependencies {
    StorageOptions storage;
    // Empty selects real ModelFiles verification. Injection is restricted to the
    // C++ construction interface, never enabled by environment or IPC requests.
    std::function<bool(QString *)> verifyModels;
    // Empty uses QLocalServer::listen. Construction-only injection lets tests
    // deliver a real connection before the listen operation returns.
    std::function<bool(QLocalServer &, const QString &)> listen;
    // Construction-only seams for deterministic lifecycle tests. Production
    // callers use these defaults and the installed worker executable.
    std::function<void(QProcess &, bool query)> configureWorker;
    int indexIdleMs = 60000;
    int queryIdleMs = 600000;
    int workerShutdownGraceMs = 1500;
    int reconcileMs = 30000;
};

class ServiceServer final : public QObject {
    Q_OBJECT
public:
    explicit ServiceServer(RuntimePaths paths, QObject *parent = nullptr);
    ServiceServer(RuntimePaths paths, ServiceDependencies dependencies, QObject *parent = nullptr);
    ~ServiceServer() override;
    bool start(QString *error = nullptr);
    void stop();
private:
    class Private;
    std::unique_ptr<Private> d;
};

} // namespace qcopilots_search
