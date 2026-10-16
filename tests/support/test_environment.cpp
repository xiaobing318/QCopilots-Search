#include "test_environment.h"

#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QStringList>
#include <utility>

#ifdef Q_OS_WIN
#include <cstdlib>
#include "core/native_lock_path.h"
#include <windows.h>
#else
#include <cerrno>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace qcopilots_search::test {
namespace {
thread_local QStringList cleanupErrors;

QByteArray directoryIdentity(const QString &path)
{
#ifdef Q_OS_WIN
    const QString native = nativeLockFilePath(path);
    const HANDLE handle = CreateFileW(reinterpret_cast<LPCWSTR>(native.utf16()), 0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return {};
    BY_HANDLE_FILE_INFORMATION info{};
    const bool valid = GetFileInformationByHandle(handle, &info)
        && (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
        && !(info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT);
    CloseHandle(handle);
    if (!valid) return {};
    return QByteArray::number(info.dwVolumeSerialNumber) + ':'
        + QByteArray::number(info.nFileIndexHigh) + ':' + QByteArray::number(info.nFileIndexLow);
#else
    struct stat info{};
    if (::lstat(QFile::encodeName(path).constData(), &info) != 0 || !S_ISDIR(info.st_mode)) return {};
    return QByteArray::number(qulonglong(info.st_dev)) + ':' + QByteArray::number(qulonglong(info.st_ino));
#endif
}
}

void recordCleanupFailure(const QString &error)
{
    cleanupErrors.append(error);
}

QString takeCleanupErrors()
{
    return std::exchange(cleanupErrors, {}).join(QLatin1Char('\n'));
}

#ifdef Q_OS_UNIX
TestSymbolicLink::~TestSymbolicLink()
{
    QString error;
    if (!remove(&error)) recordCleanupFailure(error);
}

bool TestSymbolicLink::create(const QString &link, const QString &target, QString *error)
{
    const auto parent = QFileInfo(link).absolutePath();
    const auto parentIdentity = directoryIdentity(parent);
    if (!link_.isEmpty() || !QDir::isAbsolutePath(link) || parentIdentity.isEmpty()
        || ::symlink(QFile::encodeName(target).constData(), QFile::encodeName(link).constData()) != 0) {
        if (error) *error = QStringLiteral("Cannot create owned symbolic link: %1").arg(link);
        return false;
    }
    link_ = link;
    parent_ = QFileInfo(parent).canonicalFilePath();
    parentIdentity_ = parentIdentity;
    struct stat state{};
    if (::lstat(QFile::encodeName(link_).constData(), &state) != 0 || !S_ISLNK(state.st_mode)) {
        if (error) *error = QStringLiteral("Cannot establish symbolic link ownership: %1").arg(link_);
        return false;
    }
    identity_ = QByteArray::number(qulonglong(state.st_dev)) + ':' + QByteArray::number(qulonglong(state.st_ino));
    return true;
}

bool TestSymbolicLink::remove(QString *error)
{
    if (link_.isEmpty()) return true;
    const auto parent = QFileInfo(link_).absolutePath();
    if (identity_.isEmpty() || QFileInfo(parent).canonicalFilePath() != parent_
        || directoryIdentity(parent) != parentIdentity_) {
        if (error) *error = QStringLiteral("Refusing to clean replaced symbolic link parent: %1").arg(link_);
        return false;
    }
    struct stat state{};
    if (::lstat(QFile::encodeName(link_).constData(), &state) != 0) {
        if (errno == ENOENT) {
            link_.clear();
            return true;
        }
        if (error) *error = QStringLiteral("Cannot inspect owned symbolic link: %1").arg(link_);
        return false;
    }
    const auto identity = QByteArray::number(qulonglong(state.st_dev)) + ':' + QByteArray::number(qulonglong(state.st_ino));
    if (!S_ISLNK(state.st_mode) || identity != identity_) {
        if (error) *error = QStringLiteral("Refusing to clean replaced symbolic link: %1").arg(link_);
        return false;
    }
    if (::unlink(QFile::encodeName(link_).constData()) != 0) {
        if (error) *error = QStringLiteral("Cannot unlink owned symbolic link: %1").arg(link_);
        return false;
    }
    link_.clear();
    return true;
}
#endif

TestTemporaryDir::TestTemporaryDir(const QString &pathTemplate) : QTemporaryDir(pathTemplate)
{
    if (isValid()) {
        canonicalParent_ = QFileInfo(QFileInfo(path()).absolutePath()).canonicalFilePath();
        identity_ = directoryIdentity(path());
        if (identity_.isEmpty()) {
            setAutoRemove(false);
            recordCleanupFailure(QStringLiteral("Cannot establish temporary directory ownership: %1").arg(path()));
        }
    }
}

TestTemporaryDir::~TestTemporaryDir()
{
    if (autoRemove() && isValid() && !removed_) {
        QString error;
        if (!remove(&error)) recordCleanupFailure(error);
    }
    // A failed ownership check must never fall through to QTemporaryDir's
    // unchecked recursive destructor cleanup.
    setAutoRemove(false);
}

bool TestTemporaryDir::remove(QString *error)
{
    if (removed_ || !isValid()) return true;
    const QFileInfo target(path());
    if (canonicalParent_.isEmpty() || identity_.isEmpty() || target.isSymLink()
        || QFileInfo(target.absolutePath()).canonicalFilePath() != canonicalParent_
        || target.canonicalFilePath() != QDir(canonicalParent_).filePath(target.fileName())
        || directoryIdentity(path()) != identity_) {
        if (error) *error = QStringLiteral("Refusing to clean replaced or unowned temporary directory: %1").arg(path());
        setAutoRemove(false);
        return false;
    }
    if (!QTemporaryDir::remove()) {
        if (error) *error = QStringLiteral("Cannot clean temporary directory, release files and child processes: %1").arg(path());
        setAutoRemove(false);
        return false;
    }
    removed_ = true;
    setAutoRemove(false);
    return true;
}

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
    safeLabel = safeLabel.left(16);
    temporary_ = std::make_unique<TestTemporaryDir>(QDir(base).filePath(
        QStringLiteral("qcs-t-%1-XXXXXX").arg(safeLabel)));
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
            recordCleanupFailure(message);
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
    QString existingParent = QFileInfo(destination).absolutePath();
    while (!QFileInfo::exists(existingParent) && !QFileInfo(existingParent).isSymLink()) {
        const auto parent = QFileInfo(existingParent).absolutePath();
        if (parent == existingParent) break;
        existingParent = parent;
    }
    const auto resolvedParent = QFileInfo(existingParent).canonicalFilePath();
    const bool ownedParent = !canonicalRoot_.isEmpty()
        && (resolvedParent == canonicalRoot_ || resolvedParent.startsWith(canonicalRoot_ + '/'));
    if (destination.isEmpty() || !ownedParent || QFileInfo(destination).isSymLink()
        || !QDir().mkpath(QFileInfo(destination).absolutePath())) {
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
    if (!file.flush()) {
        if (error) *error = QStringLiteral("Cannot flush fixture %1: %2").arg(destination, file.errorString());
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
    if (!target.isAbsolute() || !target.fileName().startsWith(QStringLiteral("qcs-t-"))
        || target.isSymLink() || canonicalRoot_.isEmpty() || canonicalBase_.isEmpty()
        || canonicalParent != canonicalBase_ || canonicalTarget != canonicalRoot_
        || QFileInfo(canonicalTarget).absolutePath() != canonicalBase_) {
        if (error)
            *error = QStringLiteral("Refusing to clean unowned test directory: %1").arg(target.filePath());
        temporary_->setAutoRemove(false);
        return false;
    }
    if (!temporary_->remove(error)) {
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
