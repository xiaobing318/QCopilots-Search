#pragma once
#include "core/runtime_paths.h"
#include "storage/index_repository.h"
#include <QObject>
#include <memory>

namespace qcopilots_search {

struct ServiceDependencies {
    StorageOptions storage;
    // Empty selects real ModelFiles verification. Injection is restricted to the
    // C++ construction interface, never enabled by environment or IPC requests.
    std::function<bool(QString *)> verifyModels;
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
