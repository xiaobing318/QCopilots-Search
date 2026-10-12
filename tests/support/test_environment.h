#pragma once

#include "core/runtime_paths.h"

#include <QByteArray>
#include <QString>
#include <QTemporaryDir>
#include <memory>

namespace qcopilots_search::test {

// Construct once per case. Failure never falls back to a user or source path.
class TestEnvironment {
public:
    explicit TestEnvironment(const QString &label = QStringLiteral("case"));
    ~TestEnvironment();
    TestEnvironment(const TestEnvironment &) = delete;
    TestEnvironment &operator=(const TestEnvironment &) = delete;

    bool isValid() const;
    QString error() const;
    QString root() const;
    QString path(const QString &relative) const;
    RuntimePaths runtimePaths() const;
    bool writeFile(const QString &relative, const QByteArray &data, QString *error = nullptr) const;
    void preserve();
    bool cleanup(QString *error = nullptr);

private:
    std::unique_ptr<QTemporaryDir> temporary_;
    QString error_;
    bool cleaned_ = false;
    QString canonicalBase_;
    QString canonicalRoot_;
};

// Restore the environment even when a test assertion returns early.
class ScopedEnvironment {
public:
    ScopedEnvironment(const char *key, const QByteArray &value);
    ~ScopedEnvironment();
private:
    QByteArray key_;
    QByteArray previous_;
    QString previousWide_;
    bool existed_;
};
} // namespace qcopilots_search::test
