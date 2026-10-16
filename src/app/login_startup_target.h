#pragma once

#include <QString>
#include <functional>

namespace qcopilots_search {

struct LoginStartupTarget {
    QString profileDir;
    QString modelDir;
};

// The locator is explicit so callers can isolate tests and choose the platform
// data root. Reading never creates the locator or any runtime directory. It
// takes the same operation lock as updates and reports a busy target rather
// than exposing a target whose native registration is still in progress.
// Saved directories must exist, with a writable profile and readable models.
// An interrupted update marker blocks reads until an explicit enable repairs it.
bool readLoginStartupTarget(const QString &locatorFile, LoginStartupTarget *target,
                            QString *error = nullptr);

// Serialize changes to the single per-user target. A successful nativeSet
// includes a registration that is waiting for OS approval. Native failures
// restore the previous locator bytes. Disabling requires ownership of the
// current canonical profile and model paths. A .pending marker makes crashes
// and failed rollbacks fail closed and is removed only after a known outcome.
bool updateLoginStartupTarget(const QString &locatorFile, const LoginStartupTarget &target,
                              bool enabled,
                              const std::function<bool(bool, QString *)> &nativeSet,
                              QString *error = nullptr);

} // namespace qcopilots_search
