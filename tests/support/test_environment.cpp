#include "test_environment.h"

#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QStandardPaths>

#ifdef Q_OS_WIN
#include <cstdlib>
#endif

namespace qcopilots_search::test {
TestEnvironment::TestEnvironment(const QString &label)
{
    QStandardPaths::setTestModeEnabled(true);
    QString base = qEnvironmentVariable("QCOPILOTS_SEARCH_TEST_ROOT");
    if (base.isEmpty())
        base = QDir::tempPath();
    base = QFileInfo(base).absoluteFilePath();
    if (!QDir().mkpath(base) || !QFileInfo(base).isDir()) {
        error_ = QStringLiteral("Cannot create explicit test root: %1").arg(base);
        return;
    }
    canonicalBase_ = QFileInfo(base).canonicalFilePath();
    if (canonicalBase_.isEmpty()) {
        error_ = QStringLiteral("Cannot resolve isolated test root: %1").arg(base);
        return;
    }
    // macOS commonly exposes /var through /private/var. Use one spelling for
    // test fixtures, runtime IPC identities, and cleanup ownership checks.
    base = canonicalBase_;
    QString safeLabel = label;
    safeLabel.replace(QRegularExpression(QStringLiteral("[^a-zA-Z0-9_-]")), QStringLiteral("_"));
    safeLabel = safeLabel.left(48);
    temporary_ = std::make_unique<QTemporaryDir>(QDir(base).filePath(
        QStringLiteral("qcopilots-search-tests-%1-XXXXXX").arg(safeLabel)));
    if (!temporary_->isValid())
        error_ = QStringLiteral("Cannot create isolated test directory under %1: %2")
                     .arg(base, temporary_->errorString());
    else
        canonicalRoot_ = QFileInfo(temporary_->path()).canonicalFilePath();
    if (qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") == 1)
        preserve();
}

TestEnvironment::~TestEnvironment()
{
    if (!cleaned_ && temporary_ && temporary_->autoRemove() && temporary_->isValid()) {
        QString message;
        if (!cleanup(&message))
            qWarning().noquote() << message;
    }
}

bool TestEnvironment::isValid() const
{
    return !cleaned_ && temporary_ && temporary_->isValid() && error_.isEmpty();
}
QString TestEnvironment::error() const { return error_; }
QString TestEnvironment::root() const { return isValid() ? temporary_->path() : QString{}; }
QString TestEnvironment::path(const QString &relative) const
{
    if (!isValid() || QDir::isAbsolutePath(relative))
        return {};
    const QString normalized = QDir::cleanPath(QDir::fromNativeSeparators(relative));
    if (normalized == QStringLiteral("..") || normalized.startsWith(QStringLiteral("../")))
        return {};
    return QDir(root()).filePath(normalized);
}
RuntimePaths TestEnvironment::runtimePaths() const
{
    if (!isValid())
        return {}; // Do not resolve an empty path into the real user profile.
    return RuntimePaths::resolve(path(QStringLiteral("profile")), path(QStringLiteral("models")));
}
bool TestEnvironment::writeFile(const QString &relative, const QByteArray &data, QString *error) const
{
    const QString destination = path(relative);
    if (destination.isEmpty() || !QDir().mkpath(QFileInfo(destination).absolutePath())) {
        if (error)
            *error = QStringLiteral("Invalid fixture destination: %1").arg(relative);
        return false;
    }
    QFile file(destination);
    if (!file.open(QIODevice::WriteOnly) || file.write(data) != data.size()) {
        if (error)
            *error = QStringLiteral("Cannot write fixture %1: %2").arg(destination, file.errorString());
        return false;
    }
    return true;
}
void TestEnvironment::preserve()
{
    if (temporary_ && temporary_->isValid()) {
        temporary_->setAutoRemove(false);
        qInfo().noquote() << "Retaining test artifacts:" << temporary_->path();
    }
}
bool TestEnvironment::cleanup(QString *error)
{
    if (cleaned_ || !temporary_ || !temporary_->isValid())
        return true;
    const QFileInfo target(temporary_->path());
    const QString canonicalParent = QFileInfo(target.absolutePath()).canonicalFilePath();
    const QString canonicalTarget = target.canonicalFilePath();
    if (!target.isAbsolute() || !target.fileName().startsWith(QStringLiteral("qcopilots-search-tests-"))
        || target.isSymLink() || canonicalRoot_.isEmpty() || canonicalBase_.isEmpty()
        || canonicalParent != canonicalBase_ || canonicalTarget != canonicalRoot_
        || QFileInfo(canonicalTarget).absolutePath() != canonicalBase_) {
        if (error)
            *error = QStringLiteral("Refusing to clean unowned test directory: %1").arg(target.filePath());
        temporary_->setAutoRemove(false);
        return false;
    }
    if (!temporary_->remove()) {
        if (error)
            *error = QStringLiteral("Cannot clean test directory (close files and child processes): %1")
                         .arg(target.filePath());
        temporary_->setAutoRemove(false);
        return false;
    }
    temporary_->setAutoRemove(false);
    cleaned_ = true;
    return true;
}

ScopedEnvironment::ScopedEnvironment(const char *key, const QByteArray &value)
    : key_(key), previous_(qgetenv(key)), existed_(qEnvironmentVariableIsSet(key))
{
#ifdef Q_OS_WIN
    previousWide_ = qEnvironmentVariable(key);
    const QString wideKey = QString::fromLatin1(key);
    const QString wideValue = QString::fromUtf8(value);
    _wputenv_s(reinterpret_cast<const wchar_t *>(wideKey.utf16()),
               reinterpret_cast<const wchar_t *>(wideValue.utf16()));
#else
    qputenv(key_.constData(), value);
#endif
}
ScopedEnvironment::~ScopedEnvironment()
{
#ifdef Q_OS_WIN
    const QString key = QString::fromLatin1(key_);
    const QString value = existed_ ? previousWide_ : QString{};
    _wputenv_s(reinterpret_cast<const wchar_t *>(key.utf16()),
               reinterpret_cast<const wchar_t *>(value.utf16()));
#else
    if (existed_)
        qputenv(key_.constData(), previous_);
    else
        qunsetenv(key_.constData());
#endif
}
} // namespace qcopilots_search::test
