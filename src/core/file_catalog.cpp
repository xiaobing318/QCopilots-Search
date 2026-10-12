#include "file_catalog.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QSet>
#include <algorithm>
#include <git2.h>
#include <memory>
#include <string>

#ifdef Q_OS_WIN
#include <git2/sys/config.h>
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

const QSet<QString> &sourceExtensions()
{
    static const QSet<QString> extensions = {
        "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm", "swift", "py", "pyi", "pyx",
        "ipynb", "ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "kts",
        "scala", "groovy", "rb", "php", "cs", "fs", "sh", "bash", "zsh", "fish", "ps1", "lua", "pl", "pm",
        "r", "jl", "dart", "sql", "vue", "svelte", "astro", "css", "scss", "sass", "less", "gradle", "cmake",
        "mk", "proto", "graphql", "gql", "tf", "hcl", "nix", "zig", "hs", "ex", "exs", "erl", "clj", "cljs",
        "elm", "ml", "mli", "vim", "el", "lisp", "asm", "s", "metal", "glsl", "hlsl", "wgsl", "cu", "cuh",
        "ino", "v", "sv", "tex", "yaml", "yml", "toml", "rst", "adoc", "org"
    };
    return extensions;
}

bool secretName(const QString &name)
{
    // Rules adapted from DigUp (MIT), Sources/DigUpKit/Rules.swift.
    static const QStringList prefixes = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env", "secret",
        "credential", ".netrc", ".npmrc", ".pypirc", "htpasswd", ".htpasswd"};
    const QString lower = name.toLower();
    return std::any_of(prefixes.cbegin(), prefixes.cend(), [&lower](const auto &prefix) { return lower.startsWith(prefix); });
}

bool transportStream(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return false;
    const QByteArray bytes = file.read(600);
    return bytes.size() >= 400
        && ((bytes[0] == 0x47 && bytes[188] == 0x47 && bytes[376] == 0x47)
            || (bytes[4] == 0x47 && bytes[196] == 0x47 && bytes[388] == 0x47));
}

QString classify(const QFileInfo &info, bool includeCode)
{
    const QString extension = info.suffix().toLower();
    const QString name = info.fileName();
    static const QSet<QString> texts = {"txt", "text", "md", "markdown", "rtf", "docx", "doc", "odt", "html", "htm"};
    static const QSet<QString> images = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "ico", "ppm",
        "pgm", "pbm", "heic", "heif", "avif", "dng", "cr2", "cr3", "nef", "nrw", "arw", "rw2", "raf", "orf", "pef", "raw"};
    static const QSet<QString> audio = {"wav", "mp3", "flac", "ogg", "oga", "opus", "m4a", "aac", "aiff", "aif", "wma"};
    static const QSet<QString> video = {"mp4", "m4v", "mov", "mkv", "avi", "webm", "wmv", "mpg", "mpeg", "3gp", "flv"};
    static const QSet<QString> sourceNames = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
        "Justfile", "justfile", "Rakefile", "Gemfile", "Podfile", "Brewfile", "Procfile", "Vagrantfile", "Jenkinsfile",
        "CMakeLists.txt"};
    if (secretName(name))
        return {};
    if (extension == "ts" || extension == "mts" || extension == "m2ts") {
        if (transportStream(info.absoluteFilePath()))
            return "video";
        if (extension == "m2ts")
            return {};
    }
    if (includeCode && (sourceExtensions().contains(extension) || sourceNames.contains(name) || name.startsWith("Dockerfile."))) {
        if (name.endsWith(".min.js", Qt::CaseInsensitive) || name.endsWith(".min.mjs", Qt::CaseInsensitive)
            || name.endsWith(".min.css", Qt::CaseInsensitive))
            return {};
        return "code";
    }
    if (texts.contains(extension)) return "text";
    if (extension == "pdf") return "pdf";
    if (images.contains(extension)) return "image";
    if (audio.contains(extension)) return "audio";
    if (video.contains(extension)) return "video";
    return {};
}

bool skipDirectory(const QString &name, bool includeCode)
{
    static const QSet<QString> common = {".git", ".svn", ".hg", "node_modules", "bower_components", "venv", ".venv",
        "site-packages", "__pycache__", "DerivedData", "Pods", "Carthage"};
    static const QSet<QString> code = {"vendor", "Vendor", "vendored", "third_party", "third-party", "thirdparty",
        "3rdparty", "external", "extern", "deps", "build", "Build", "dist", "out", "target", "obj", "coverage",
        "htmlcov", "generated", "__generated__", "wandb", "mlruns", "lightning_logs"};
    return common.contains(name) || name.endsWith(".noindex") || (includeCode && code.contains(name));
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
    if (!ok || !resolved || (metadata.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
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
    const int descriptor = ::open(QFile::encodeName(record.path).constData(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    const bool valid = descriptor >= 0 && ::fstat(descriptor, &metadata) == 0 && S_ISREG(metadata.st_mode);
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
        error = QStringLiteral("Cannot open Git repository %1: %2").arg(repository, gitError());
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
    static constexpr char longPaths[] = "[core]\nlongpaths = true\n";
    git_config_backend *rawBackend = nullptr;
    if (git_config_backend_from_string(&rawBackend, longPaths, sizeof(longPaths) - 1, nullptr) < 0) {
        error = QStringLiteral("Cannot create Git path configuration for %1: %2").arg(repository, gitError());
        return false;
    }
    const auto freeBackend = [](git_config_backend *backend) { if (backend) backend->free(backend); };
    std::unique_ptr<git_config_backend, decltype(freeBackend)> backend(rawBackend, freeBackend);
    if (git_config_add_backend(config.get(), backend.get(), GIT_CONFIG_LEVEL_APP, repo.get(), 0) < 0) {
        error = QStringLiteral("Cannot apply Git path configuration for %1: %2").arg(repository, gitError());
        return false;
    }
    backend.release(); // The config owns an added backend.
    // libgit2 1.9.1's memory backend does not retain entries returned by get.
    // Freeze via its deep-copy iterator before any lookup can release that
    // backend's list. Only snapshot backends with safe entry ownership are
    // exposed to repository config lookups and their later destruction.
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
    described.kind = classify(info, true);
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
    GitLibrary git(rules.includeCode);
    if (rules.includeCode && !git.initialized) {
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
        QStringList pending{root};
        while (!pending.isEmpty()) {
            if (isCancelled(cancel)) { result.cancelled = true; break; }
            const QString directory = pending.takeLast();
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
            if (!seenDirectories.contains(pathKey(actualDirectory))) {
                seenDirectories.insert(pathKey(actualDirectory));
                result.directories.append(actualDirectory);
            }
            for (const auto &info : entries) {
                if (isCancelled(cancel)) { result.cancelled = true; break; }
                // Symlinks and Windows junctions are never followed, including file links.
                if (info.isSymLink()
#ifdef Q_OS_WIN
                    || info.isJunction()
#endif
                ) continue;
                const QString path = normalizePath(info.absoluteFilePath());
                if (!containsNormalized(path, root)) continue;
                if (std::any_of(activeExcludes.cbegin(), activeExcludes.cend(), [&path](const auto &exclude) {
                        return containsNormalized(path, exclude);
                    })) continue;
                if (info.isDir()) {
                    if (!skipDirectory(info.fileName(), rules.includeCode)) pending.append(path);
                    continue;
                }
                if (!info.isFile() || seenFiles.contains(pathKey(path))) continue;
                const QString kind = classify(info, rules.includeCode);
                if (kind.isEmpty()) continue;
                FileRecord record;
                record.kind = kind;
                QString error;
                if (!describeFile(info, record, error)) { result.errors.append(error); continue; }
                // Validate the actual handle-resolved path after any ancestor changes.
                if (!containsNormalized(record.path, root)
                    || std::any_of(activeExcludes.cbegin(), activeExcludes.cend(), [&record](const auto &exclude) {
                        return containsNormalized(record.path, exclude);
                    })) continue;
                seenFiles.insert(pathKey(record.path));
                const QString repository = rules.includeCode ? findRepository(info.absolutePath(), repositoryCache) : QString{};
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
