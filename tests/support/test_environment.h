#pragma once

#include "core/runtime_paths.h"

#include <QByteArray>
#include <QString>
#include <QTemporaryDir>
#include <memory>

namespace qcopilots_search::test {

// QtTest cleanup slots drain these errors after all case-local fixtures have
// been destroyed, so a failed RAII cleanup cannot turn into a passing test.
void recordCleanupFailure(const QString &error);
QString takeCleanupErrors();

#ifdef Q_OS_UNIX
// Remove only the link created by this fixture, including on early QtTest
// assertion returns. Retained artifact directories must not retain links.
class TestSymbolicLink {
public:
    TestSymbolicLink() = default;
    ~TestSymbolicLink();
    TestSymbolicLink(const TestSymbolicLink &) = delete;
    TestSymbolicLink &operator=(const TestSymbolicLink &) = delete;
    bool create(const QString &link, const QString &target, QString *error = nullptr);
    bool remove(QString *error = nullptr);
private:
    QString link_;
    QString parent_;
    QByteArray parentIdentity_;
    QByteArray identity_;
};
#endif

class TestTemporaryDir : public QTemporaryDir {
public:
    explicit TestTemporaryDir(const QString &pathTemplate);
    ~TestTemporaryDir();
    bool remove(QString *error = nullptr);
private:
    QString canonicalParent_;
    QByteArray identity_;
    bool removed_ = false;
};

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
    std::unique_ptr<TestTemporaryDir> temporary_;
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
