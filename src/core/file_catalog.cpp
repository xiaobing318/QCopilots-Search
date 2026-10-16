#include "file_catalog.h"
#include "file_formats.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QRegularExpression>
#include <QSet>
#include <QTemporaryFile>
#include <algorithm>
#include <git2.h>
#include <memory>
#include <string>

#ifdef Q_OS_WIN
#include <git2/sys/repository.h>
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#ifdef Q_OS_MACOS
#include <sys/xattr.h>
#endif
#endif

namespace qcopilots_search {
namespace {
struct GitLibrary {
    bool initialized = false;
    explicit GitLibrary(bool needed) : initialized(needed && git_libgit2_init() >= 0) {}
    ~GitLibrary() { if (initialized) git_libgit2_shutdown(); }
};

QString gitError()
{
    const git_error *error = git_error_last();
    return error && error->message ? QString::fromUtf8(error->message) : QStringLiteral("Unknown libgit2 error");
}

#ifdef Q_OS_WIN
constexpr Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
constexpr Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif

QString pathKey(const QString &path)
{
#ifdef Q_OS_WIN
    return path.toCaseFolded();
#else
    return path;
#endif
}

bool containsNormalized(const QString &path, const QString &root)
{
    if (path.isEmpty() || root.isEmpty())
        return false;
    return path.compare(root, pathCase) == 0
        || path.startsWith(root.endsWith(u'/') ? root : root + u'/', pathCase);
}

bool isCancelled(std::atomic_bool *cancel)
{
    return cancel && cancel->load(std::memory_order_relaxed);
}

// This check precedes even transport-stream sniffing. Reading a placeholder can
// ask its provider to download it, so metadata is inspected without file data.
bool unavailablePlaceholder(const QFileInfo &info)
{
#ifdef Q_OS_WIN
    QString native = QDir::toNativeSeparators(info.absoluteFilePath());
    if (!native.startsWith(QStringLiteral("\\\\?\\")))
        native = native.startsWith(QStringLiteral("\\\\"))
            ? QStringLiteral("\\\\?\\UNC\\") + native.mid(2) : QStringLiteral("\\\\?\\") + native;
    const DWORD attributes = GetFileAttributesW(reinterpret_cast<LPCWSTR>(native.utf16()));
    // FILE_ATTRIBUTE_RECALL_ON_OPEN / RECALL_ON_DATA_ACCESS are available on
    // modern Windows SDKs. Use their ABI values for older build SDK headers.
    constexpr DWORD recallOnOpen = 0x00040000, recallOnDataAccess = 0x00400000;
    return attributes != INVALID_FILE_ATTRIBUTES
        && (attributes & (FILE_ATTRIBUTE_OFFLINE | recallOnOpen | recallOnDataAccess));
#elif defined(Q_OS_MACOS)
    struct stat state{};
    constexpr uint32_t dataless = 0x40000000; // SF_DATALESS, iCloud and File Provider.
    return (::lstat(QFile::encodeName(info.absoluteFilePath()).constData(), &state) == 0
            && (state.st_flags & dataless)) || info.fileName().endsWith(QStringLiteral(".icloud"));
#else
    Q_UNUSED(info);
    return false;
#endif
}

bool isScreenshot(const QFileInfo &info)
{
#ifdef Q_OS_MACOS
    if (::getxattr(QFile::encodeName(info.absoluteFilePath()).constData(),
                  "com.apple.metadata:kMDItemIsScreenCapture", nullptr, 0, 0, XATTR_NOFOLLOW) > 0)
        return true;
#endif
    const auto name = info.fileName();
    for (const auto &prefix : {"Screenshot", "Screen Shot", "CleanShot", "SCR-"})
        if (name.startsWith(QLatin1StringView(prefix), Qt::CaseInsensitive)) return true;
    return false;
}

bool transportStream(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return false;
    return file_formats::isTransportStream(file.read(600));
}

QString classify(const QFileInfo &info, bool includeCode, bool codeOnly = false)
{
    const QString extension = info.suffix().toLower();
    const QString name = info.fileName();
    if (file_formats::isSensitiveFilename(name))
        return {};
    if (extension == "ts" || extension == "mts" || extension == "m2ts") {
        if (transportStream(info.absoluteFilePath()))
            return codeOnly ? QString{} : QStringLiteral("video");
        if (extension == "m2ts" || !includeCode)
            return {};
    }
    // Mixed discovery keeps ordinary documents as text. Explicit Code scope
    // also indexes repository documentation, while named build files are code.
    if (includeCode && file_formats::isCode(extension, name)
        && (codeOnly || !file_formats::documentExtensions().contains(extension) || file_formats::isCodeFilename(name))) {
        if (name.endsWith(".min.js", Qt::CaseInsensitive) || name.endsWith(".min.mjs", Qt::CaseInsensitive)
            || name.endsWith(".min.css", Qt::CaseInsensitive))
            return {};
        const auto lower = name.toLower();
        if (codeOnly && (lower.startsWith("license") || lower.startsWith("licence")
            || lower.startsWith("copying") || lower.startsWith("notice"))) return {};
        return "code";
    }
    if (codeOnly) return {};
    if (file_formats::documentExtensions().contains(extension)) return "text";
    if (extension == "pdf") return "pdf";
    if (file_formats::isImage(extension)) return "image";
    if (file_formats::isAudio(extension)) return "audio";
    if (file_formats::isVideo(extension)) return "video";
    return {};
}

bool skipDirectory(const QFileInfo &info, bool includeCode)
{
    const auto name = info.fileName();
    static const QSet<QString> common = {".git", ".svn", ".hg", "node_modules", "bower_components", "venv", ".venv",
        "site-packages", "__pycache__", "DerivedData", "Pods", "Carthage"};
    static const QSet<QString> code = {"vendor", "Vendor", "vendored", "third_party", "third-party", "thirdparty",
        "3rdparty", "external", "extern", "deps", "build", "Build", "dist", "out", "target", "obj", "coverage",
        "htmlcov", "generated", "__generated__", "wandb", "mlruns", "lightning_logs"};
    static const QSet<QString> bundles = {"app", "bundle", "framework", "plugin", "kext", "prefpane",
        "appex", "xcodeproj", "xcworkspace", "pkg", "photoslibrary"};
    return info.isHidden() || name.startsWith(u'.') || common.contains(name) || name.endsWith(".noindex")
        || bundles.contains(info.suffix().toLower()) || (includeCode && code.contains(name))
#ifdef Q_OS_MACOS
        || info.absoluteFilePath() == QDir::homePath() + QStringLiteral("/Library")
#endif
        ;
}

bool projectDirectory(const QFileInfoList &entries)
{
    static const QSet<QString> markers = {".git", "package.json", "Package.swift", "pyproject.toml", "setup.py",
        "requirements.txt", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile",
        "composer.json", "CMakeLists.txt"};
    return std::any_of(entries.cbegin(), entries.cend(), [](const auto &entry) {
        return markers.contains(entry.fileName()) || entry.suffix() == "xcodeproj" || entry.suffix() == "xcworkspace";
    });
}

bool machineNamed(const QString &stem)
{
    const auto lower = stem.toLower();
    for (const auto &prefix : {"img", "dsc", "pxl", "mvimg", "vid", "gopr", "dji", "photo", "whatsapp", "screenshot"})
        if (lower.startsWith(QLatin1StringView(prefix))) return false;
    static const QRegularExpression hex(QStringLiteral("^[0-9a-f]{8,}([-_][0-9a-f]{4,})*$"));
    static const QRegularExpression numbered(QStringLiteral("^[a-z]{0,16}[-_. ]?[0-9]{3,}([-_.][0-9]+)*$"));
    return hex.match(lower).hasMatch() || numbered.match(lower).hasMatch();
}

bool describeFile(const QFileInfo &info, FileRecord &record, QString &error)
{
    record.path = QDir::cleanPath(QDir::fromNativeSeparators(info.absoluteFilePath()));
    QByteArray changeTime;
#ifdef Q_OS_WIN
    QString native = QDir::toNativeSeparators(record.path);
    if (!native.startsWith(QStringLiteral("\\\\?\\"))) {
        if (native.startsWith(QStringLiteral("\\\\"))) native = QStringLiteral("\\\\?\\UNC\\") + native.mid(2);
        else native = QStringLiteral("\\\\?\\") + native;
    }
    HANDLE handle = CreateFileW(reinterpret_cast<LPCWSTR>(native.utf16()), FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        error = QStringLiteral("Cannot read file identity: %1 (Windows error %2)").arg(record.path).arg(GetLastError());
        return false;
    }
    BY_HANDLE_FILE_INFORMATION metadata{};
    FILE_BASIC_INFO basic{};
    const bool ok = GetFileInformationByHandle(handle, &metadata)
        && GetFileInformationByHandleEx(handle, FileBasicInfo, &basic, sizeof(basic));
    DWORD failure = ok ? ERROR_SUCCESS : GetLastError();
    const DWORD finalLength = GetFinalPathNameByHandleW(handle, nullptr, 0, FILE_NAME_NORMALIZED);
    bool resolved = false;
    if (finalLength) {
        std::wstring finalName(finalLength, L'\0');
        const DWORD copied = GetFinalPathNameByHandleW(handle, finalName.data(), finalLength, FILE_NAME_NORMALIZED);
        if (copied > 0 && copied < finalLength) {
            QString finalPath = QDir::fromNativeSeparators(QString::fromWCharArray(finalName.data(), int(copied)));
            if (finalPath.startsWith("//?/UNC/")) finalPath = "//" + finalPath.mid(8);
            else if (finalPath.startsWith("//?/")) finalPath = finalPath.mid(4);
            record.path = QDir::cleanPath(finalPath);
            resolved = true;
        }
    }
    if (!resolved && ok) failure = GetLastError();
    CloseHandle(handle);
    constexpr DWORD unavailableAttributes = FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT
        | FILE_ATTRIBUTE_OFFLINE | 0x00040000 | 0x00400000;
    if (!ok || !resolved || (metadata.dwFileAttributes & unavailableAttributes)) {
        error = QStringLiteral("File changed or identity is unavailable: %1 (Windows error %2)").arg(record.path).arg(failure);
        return false;
    }
    const quint64 fileId = (quint64(metadata.nFileIndexHigh) << 32) | metadata.nFileIndexLow;
    const quint64 ticks = (quint64(metadata.ftLastWriteTime.dwHighDateTime) << 32) | metadata.ftLastWriteTime.dwLowDateTime;
    record.identity = QStringLiteral("win:%1:%2").arg(metadata.dwVolumeSerialNumber, 0, 16).arg(fileId, 0, 16);
    record.size = qint64((quint64(metadata.nFileSizeHigh) << 32) | metadata.nFileSizeLow);
    record.modifiedMs = qint64(ticks / 10000) - Q_INT64_C(11644473600000);
    changeTime = QByteArray::number(ticks) + ':' + QByteArray::number(basic.ChangeTime.QuadPart);
#else
    struct stat metadata{};
    const int descriptor = ::open(QFile::encodeName(record.path).constData(),
#ifdef Q_OS_MACOS
        O_EVTONLY | O_NOFOLLOW | O_CLOEXEC);
#else
        O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
#endif
    const bool valid = descriptor >= 0 && ::fstat(descriptor, &metadata) == 0 && S_ISREG(metadata.st_mode)
#ifdef Q_OS_MACOS
        && !(metadata.st_flags & 0x40000000)
#endif
        ;
    if (!valid) {
        const int failure = errno;
        if (descriptor >= 0) ::close(descriptor);
        error = QStringLiteral("Cannot read regular file identity: %1 (%2)").arg(record.path, QString::fromLocal8Bit(std::strerror(failure)));
        return false;
    }
    QByteArray resolved(4096, '\0');
#ifdef Q_OS_MACOS
    const bool hasPath = ::fcntl(descriptor, F_GETPATH, resolved.data()) == 0;
    if (hasPath) resolved.resize(qsizetype(std::strlen(resolved.constData())));
#else
    const QByteArray descriptorPath = QByteArray("/proc/self/fd/") + QByteArray::number(descriptor);
    const auto resolvedLength = ::readlink(descriptorPath.constData(), resolved.data(), size_t(resolved.size() - 1));
    const bool hasPath = resolvedLength >= 0 && resolvedLength < resolved.size() - 1;
    if (hasPath) resolved.resize(resolvedLength);
#endif
    ::close(descriptor);
    if (!hasPath) {
        error = QStringLiteral("Cannot read regular file identity: %1 (%2)").arg(record.path, QString::fromLocal8Bit(std::strerror(errno)));
        return false;
    }
    record.path = QDir::cleanPath(QFile::decodeName(resolved));
    record.identity = QStringLiteral("unix:%1:%2").arg(quint64(metadata.st_dev)).arg(quint64(metadata.st_ino));
    record.size = metadata.st_size;
#ifdef Q_OS_MACOS
    const auto modified = metadata.st_mtimespec;
    const auto changed = metadata.st_ctimespec;
#else
    const auto modified = metadata.st_mtim;
    const auto changed = metadata.st_ctim;
#endif
    record.modifiedMs = qint64(modified.tv_sec) * 1000 + modified.tv_nsec / 1000000;
    changeTime = QByteArray::number(qint64(modified.tv_sec)) + ':' + QByteArray::number(qint64(modified.tv_nsec));
    changeTime += ':' + QByteArray::number(qint64(changed.tv_sec)) + ':' + QByteArray::number(qint64(changed.tv_nsec));
#endif
    record.fingerprint = QString::fromLatin1(QCryptographicHash::hash(record.identity.toUtf8() + ':'
        + QByteArray::number(record.size) + ':' + changeTime, QCryptographicHash::Sha256).toHex());
    return true;
}

QString findRepository(QString directory, QHash<QString, QString> &cache)
{
    QStringList visited;
    QString result;
    while (!directory.isEmpty()) {
        if (cache.contains(directory)) {
            result = cache.value(directory);
            break;
        }
        visited.append(directory);
        const QFileInfo marker(QDir(directory).filePath(".git"));
        if (marker.exists() || marker.isSymLink()) {
            result = directory;
            break;
        }
        const QString parent = QFileInfo(directory).absolutePath();
        if (parent == directory) break;
        directory = parent;
    }
    for (const auto &path : visited) cache.insert(path, result);
    return result;
}

// Ignore rules do not hide tracked files. Bundled libgit2 avoids a runtime Git dependency.
bool gitIgnored(const QString &repository, const QList<FileRecord> &files,
    QSet<QString> &ignored, QString &error, std::atomic_bool *cancel)
{
    git_repository *rawRepository = nullptr;
    if (git_repository_open_ext(&rawRepository, repository.toUtf8().constData(), GIT_REPOSITORY_OPEN_NO_SEARCH, nullptr) < 0) {
        const QString details = gitError();
        error = QStringLiteral("Cannot open Git repository %1 with upstream libgit2: %2").arg(repository, details);
#ifdef Q_OS_WIN
        if (details.contains(QStringLiteral("too long"), Qt::CaseInsensitive))
            error += QStringLiteral(" Move the repository metadata to a shorter path.");
#endif
        return false;
    }
    std::unique_ptr<git_repository, decltype(&git_repository_free)> repo(rawRepository, git_repository_free);
#ifdef Q_OS_WIN
    // Scanning can read long working-tree paths even when the user's Git
    // client keeps core.longpaths disabled. Overlay a readonly snapshot for
    // this repository instance without writing either source or user config.
    git_config *rawConfig = nullptr;
    if (git_repository_config_snapshot(&rawConfig, repo.get()) < 0) {
        error = QStringLiteral("Cannot read Git configuration for %1: %2").arg(repository, gitError());
        return false;
    }
    std::unique_ptr<git_config, decltype(&git_config_free)> config(rawConfig, git_config_free);
    QTemporaryFile applicationConfiguration(QDir::temp().filePath(QStringLiteral("qcs-git-config-XXXXXX")));
    static constexpr char longPaths[] = "[core]\nlongpaths = true\n";
    if (!applicationConfiguration.open()
        || applicationConfiguration.write(longPaths, sizeof(longPaths) - 1) != sizeof(longPaths) - 1
        || !applicationConfiguration.flush()) {
        error = QStringLiteral("Cannot create Git path configuration for %1: %2")
            .arg(repository, applicationConfiguration.errorString());
        return false;
    }
    applicationConfiguration.close();
    if (git_config_add_file_ondisk(config.get(), applicationConfiguration.fileName().toUtf8().constData(),
            GIT_CONFIG_LEVEL_APP, repo.get(), 0) < 0) {
        error = QStringLiteral("Cannot apply Git path configuration for %1: %2").arg(repository, gitError());
        return false;
    }
    // Repository lookups use an immutable snapshot. The temporary APP file
    // never changes repository or user settings and is removed on return.
    git_config *rawSnapshot = nullptr;
    if (git_config_snapshot(&rawSnapshot, config.get()) < 0) {
        error = QStringLiteral("Cannot snapshot Git path configuration for %1: %2").arg(repository, gitError());
        return false;
    }
    std::unique_ptr<git_config, decltype(&git_config_free)> snapshot(rawSnapshot, git_config_free);
    if (git_repository_set_config(repo.get(), snapshot.get()) < 0) {
        error = QStringLiteral("Cannot activate Git path configuration for %1: %2").arg(repository, gitError());
        return false;
    }
#endif
    git_index *rawIndex = nullptr;
    if (git_repository_index(&rawIndex, repo.get()) < 0) {
        error = QStringLiteral("Cannot read Git index for %1: %2").arg(repository, gitError());
        return false;
    }
    std::unique_ptr<git_index, decltype(&git_index_free)> index(rawIndex, git_index_free);
    for (const auto &file : files) {
        if (isCancelled(cancel)) return false;
        const QByteArray relative = QDir(repository).relativeFilePath(file.path).toUtf8();
        size_t position = 0;
        const int found = git_index_find(&position, index.get(), relative.constData());
        if (found == 0) continue;
        if (found != GIT_ENOTFOUND) {
            error = QStringLiteral("Cannot inspect Git index for %1: %2").arg(file.path, gitError());
            return false;
        }
        int isIgnored = 0;
        if (git_ignore_path_is_ignored(&isIgnored, repo.get(), relative.constData()) < 0) {
            error = QStringLiteral("Cannot evaluate Git ignore rules for %1: %2").arg(file.path, gitError());
            return false;
        }
        if (isIgnored) ignored.insert(pathKey(file.path));
    }
    return true;
}
} // namespace

QString normalizePath(const QString &path)
{
    if (path.isEmpty()) return {};
    QString current = QDir::cleanPath(QFileInfo(QDir::fromNativeSeparators(path)).absoluteFilePath());
    QStringList tail;
    while (!QFileInfo::exists(current)) {
        const QFileInfo info(current);
        // Resolve an explicit dangling link as well as links to existing targets.
        if (info.isSymLink() && !info.symLinkTarget().isEmpty()) {
            current = info.symLinkTarget();
            break;
        }
        const QString parent = info.absolutePath();
        if (parent == current) break;
        tail.prepend(info.fileName());
        current = parent;
    }
#ifdef Q_OS_WIN
    // QFileInfo::canonicalFilePath() can retain Windows junction components.
    // Follow the existing ancestor with a native handle before appending the
    // missing tail, so a junction cannot make containment checks lexical-only.
    QString native = QDir::toNativeSeparators(current);
    if (!native.startsWith(QStringLiteral("\\\\?\\"))) {
        native = native.startsWith(QStringLiteral("\\\\"))
            ? QStringLiteral("\\\\?\\UNC\\") + native.mid(2) : QStringLiteral("\\\\?\\") + native;
    }
    HANDLE handle = CreateFileW(reinterpret_cast<LPCWSTR>(native.utf16()), FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return {};
    const DWORD length = GetFinalPathNameByHandleW(handle, nullptr, 0, FILE_NAME_NORMALIZED);
    std::wstring resolved(length, L'\0');
    const DWORD copied = length
        ? GetFinalPathNameByHandleW(handle, resolved.data(), length, FILE_NAME_NORMALIZED) : 0;
    CloseHandle(handle);
    if (copied == 0 || copied >= length) return {};
    current = QDir::fromNativeSeparators(QString::fromWCharArray(resolved.data(), int(copied)));
    if (current.startsWith(QStringLiteral("//?/UNC/"))) current = QStringLiteral("//") + current.mid(8);
    else if (current.startsWith(QStringLiteral("//?/"))) current = current.mid(4);
#else
    const QString canonical = QFileInfo(current).canonicalFilePath();
    if (!canonical.isEmpty()) current = canonical;
#endif
    for (const auto &component : tail) current = QDir(current).filePath(component);
    return QDir::cleanPath(QDir::fromNativeSeparators(current));
}

bool isPathWithin(const QString &path, const QString &root)
{
    return containsNormalized(normalizePath(path), normalizePath(root));
}

bool FileCatalog::describe(const QString &path, FileRecord *record, QString *error)
{
    const QFileInfo info(path);
    if (!record || path.isEmpty() || !info.isFile() || info.isSymLink()
#ifdef Q_OS_WIN
        || info.isJunction()
#endif
    ) {
        if (error) *error = QStringLiteral("Not a regular file: %1").arg(path);
        return false;
    }
    FileRecord described;
    if (unavailablePlaceholder(info)) {
        if (error) *error = QStringLiteral("File is in the cloud and has not been downloaded: %1").arg(path);
        return false;
    }
    described.kind = classify(info, true);
    described.screenshot = described.kind == QStringLiteral("image") && isScreenshot(info);
    if (described.kind.isEmpty()) {
        if (error) *error = QStringLiteral("Unsupported file type: %1").arg(path);
        return false;
    }
    QString failure;
    if (!describeFile(info, described, failure)) {
        if (error) *error = failure;
        return false;
    }
    *record = described;
    if (error) error->clear();
    return true;
}

ScanResult FileCatalog::scan(const FolderRules &rules, std::atomic_bool *cancel,
                             const std::function<void(qsizetype)> &onDiscovered)
{
    ScanResult result;
    const bool codeOnly = rules.scope == FolderRules::Scope::Code;
    const bool includeCode = rules.includeCode || codeOnly;
    GitLibrary git(includeCode);
    if (includeCode && !git.initialized) {
        result.errors.append(QStringLiteral("Cannot initialize bundled libgit2: %1").arg(gitError()));
        return result;
    }
    QStringList excludes;
    for (const auto &path : rules.excludes)
        if (!path.isEmpty()) excludes.append(normalizePath(path));
    QSet<QString> seenFiles;
    QSet<QString> seenDirectories;
    QHash<QString, QString> repositoryCache;
    QHash<QString, QList<FileRecord>> repositoryFiles;
    QSet<QString> explicitRoots;
    for (const auto &root : rules.roots) {
        const QFileInfo info(root);
        if (!info.isSymLink()
#ifdef Q_OS_WIN
            && !info.isJunction()
#endif
        ) explicitRoots.insert(pathKey(normalizePath(root)));
    }
    for (const auto &rawRoot : rules.roots) {
        if (isCancelled(cancel)) { result.cancelled = true; break; }
        const QFileInfo rawInfo(rawRoot);
        if (rawInfo.isSymLink()
#ifdef Q_OS_WIN
            || rawInfo.isJunction()
#endif
        ) {
            result.errors.append(QStringLiteral("Search root must be a real directory, not a link: %1").arg(rawRoot));
            continue;
        }
        const QString root = normalizePath(rawRoot);
        const QFileInfo rootInfo(root);
        if (root.isEmpty() || !rootInfo.isDir() || !rootInfo.isReadable()) {
            result.errors.append(QStringLiteral("Search root is unavailable or unreadable: %1").arg(rawRoot));
            continue;
        }
        // A chosen nested root overrides exclusions at or above that root.
        QStringList activeExcludes;
        for (const auto &exclude : excludes)
            if (containsNormalized(exclude, root) && exclude.compare(root, pathCase) != 0) activeExcludes.append(exclude);
        struct PendingDirectory { QString path; bool inProject; };
        QList<PendingDirectory> pending{{root, false}};
        while (!pending.isEmpty()) {
            if (isCancelled(cancel)) { result.cancelled = true; break; }
            const auto next = pending.takeLast();
            const QString directory = next.path;
            const QFileInfo current(directory);
            if (current.isSymLink()
#ifdef Q_OS_WIN
                || current.isJunction()
#endif
            ) continue;
            if (!current.isDir() || !current.isReadable()) {
                result.errors.append(QStringLiteral("Directory became unavailable or unreadable: %1").arg(directory));
                continue;
            }
            const QString actualDirectory = normalizePath(directory);
            if (!containsNormalized(actualDirectory, root)
                || std::any_of(activeExcludes.cbegin(), activeExcludes.cend(), [&actualDirectory](const auto &exclude) {
                    return containsNormalized(actualDirectory, exclude);
                })) continue;
            const auto entries = QDir(directory).entryInfoList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System, QDir::NoSort);
            const bool inProject = next.inProject || (!includeCode && projectDirectory(entries));
            if (!seenDirectories.contains(pathKey(actualDirectory))) {
                seenDirectories.insert(pathKey(actualDirectory));
                result.directories.append(actualDirectory);
            }
            QList<FileRecord> directoryFiles;
            for (const auto &info : entries) {
                if (isCancelled(cancel)) { result.cancelled = true; break; }
                // Symlinks and Windows junctions are never followed, including file links.
                if (info.isSymLink() || info.isHidden() || info.fileName().startsWith(u'.')
#ifdef Q_OS_WIN
                    || info.isJunction()
#endif
                ) continue;
                if (unavailablePlaceholder(info)) continue;
                const QString path = normalizePath(info.absoluteFilePath());
                if (!containsNormalized(path, root)) continue;
                if (std::any_of(activeExcludes.cbegin(), activeExcludes.cend(), [&path](const auto &exclude) {
                        return containsNormalized(path, exclude);
                    })) continue;
                if (info.isDir()) {
                    const QFileInfo gitMarker(QDir(path).filePath(QStringLiteral(".git")));
                    // A selected worktree root is valid, but nested worktrees
                    // and submodules are excluded from a code walk.
                    if (!skipDirectory(info, includeCode) && !(codeOnly && gitMarker.isFile()))
                        pending.append({path, inProject});
                    continue;
                }
                if (!info.isFile() || seenFiles.contains(pathKey(path))) continue;
                const QString kind = classify(info, includeCode, codeOnly);
                if (kind.isEmpty()) continue;
                FileRecord record;
                record.kind = kind;
                record.screenshot = kind == QStringLiteral("image") && isScreenshot(info);
                if (inProject && !record.screenshot) continue;
                QString error;
                if (!describeFile(info, record, error)) { result.errors.append(error); continue; }
                // Validate the actual handle-resolved path after any ancestor changes.
                if (!containsNormalized(record.path, root)
                    || std::any_of(activeExcludes.cbegin(), activeExcludes.cend(), [&record](const auto &exclude) {
                        return containsNormalized(record.path, exclude);
                    })) continue;
                directoryFiles.append(record);
            }
            // DigUp excludes a whole folder's media when one media kind has
            // >=300 entries and >=70% machine names. Explicit roots opt in.
            bool dataset = false;
            if (!includeCode && !explicitRoots.contains(pathKey(actualDirectory))) {
                QHash<QString, QPair<qsizetype, qsizetype>> counts;
                for (const auto &record : directoryFiles) {
                    if (record.screenshot || (record.kind != "image" && record.kind != "audio" && record.kind != "video"))
                        continue;
                    auto &count = counts[record.kind];
                    ++count.first;
                    if (machineNamed(QFileInfo(record.path).completeBaseName())) ++count.second;
                }
                for (const auto &count : counts) {
                    if (count.first >= 300 && count.second * 10 >= count.first * 7) { dataset = true; break; }
                }
            }
            for (const auto &record : directoryFiles) {
                if (isCancelled(cancel)) { result.cancelled = true; break; }
                if (dataset && !record.screenshot && (record.kind == "image" || record.kind == "audio" || record.kind == "video"))
                    continue;
                if (seenFiles.contains(pathKey(record.path))) continue;
                seenFiles.insert(pathKey(record.path));
                const QString repository = includeCode ? findRepository(QFileInfo(record.path).absolutePath(), repositoryCache) : QString{};
                if (repository.isEmpty()) {
                    result.files.append(record);
                    if (onDiscovered) onDiscovered(result.files.size());
                } else repositoryFiles[repository].append(record);
            }
        }
        if (result.cancelled) break;
    }
    for (auto it = repositoryFiles.cbegin(); it != repositoryFiles.cend(); ++it) {
        if (isCancelled(cancel)) { result.cancelled = true; break; }
        QSet<QString> ignored;
        QString error;
        if (!gitIgnored(it.key(), it.value(), ignored, error, cancel)) {
            if (isCancelled(cancel)) result.cancelled = true;
            if (!error.isEmpty()) result.errors.append(error);
            // Fail closed: files from a repository with unknown ignore state are omitted.
            continue;
        }
        for (auto record : it.value()) {
            if (isCancelled(cancel)) { result.cancelled = true; break; }
            if (ignored.contains(pathKey(record.path))) continue;
            // Repository documentation participates in the independent code index.
            if (record.kind == "text" && (QFileInfo(record.path).suffix().toLower() == "md"
                || QFileInfo(record.path).suffix().toLower() == "markdown" || QFileInfo(record.path).suffix().toLower() == "txt"))
                record.kind = "code";
            result.files.append(record);
            if (onDiscovered) onDiscovered(result.files.size());
        }
    }
    result.cancelled = result.cancelled || isCancelled(cancel);
    std::sort(result.files.begin(), result.files.end(), [](const auto &left, const auto &right) { return left.path < right.path; });
    std::sort(result.directories.begin(), result.directories.end());
    return result;
}

} // namespace qcopilots_search
