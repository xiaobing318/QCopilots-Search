#pragma once

#include "core/runtime_paths.h"
#include <QObject>
#include <memory>

namespace qcopilots_search {

// One GUI owns each normalized profile. A second launch only asks that GUI to
// show its window, and never constructs a backend or shuts down its service.
class GuiInstance final : public QObject {
    Q_OBJECT
public:
    enum class Result { Primary, ActivatedExisting, Failed };
    explicit GuiInstance(const RuntimePaths &paths, QObject *parent = nullptr);
    ~GuiInstance() override;
    Result start(QString *error = nullptr, int timeoutMs = 5000);
    QString endpointName() const;

signals:
    void showRequested();

private:
    class Private;
    std::unique_ptr<Private> d;
};

} // namespace qcopilots_search
