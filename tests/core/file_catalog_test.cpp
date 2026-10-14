#include "core/file_catalog.h"
#include "support/test_environment.h"

#include <QDir>
#include <QDateTime>
#include <QFile>
#include <QFileInfo>
#include <QSet>
#include <QStandardPaths>
#include <QtTest>
#include <git2.h>
#include <memory>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <winioctl.h>
#include <cstring>
#endif
#ifdef Q_OS_MACOS
#include <sys/xattr.h>
#endif

using namespace qcopilots_search;
using namespace qcopilots_search::test;

namespace {
#ifdef Q_OS_WIN
// Attribute changes are confined to fixture files and restored before their
// temporary directory is removed, including when an assertion returns early.
class TestWindowsAttributes {
public:
    explicit TestWindowsAttributes(QString path) : path_(QDir::toNativeSeparators(path))
    {
        original_ = GetFileAttributesW(reinterpret_cast<LPCWSTR>(path_.utf16()));
    }
    ~TestWindowsAttributes()
    {
        if (changed_ && !restore())
            recordCleanupFailure(QStringLiteral("Cannot restore fixture attributes: %1 (Windows error %2)")
                                     .arg(path_).arg(GetLastError()));
    }
    bool add(DWORD attributes)
    {
        if (original_ == INVALID_FILE_ATTRIBUTES) return false;
        changed_ = SetFileAttributesW(reinterpret_cast<LPCWSTR>(path_.utf16()),
                                     (original_ & ~FILE_ATTRIBUTE_NORMAL) | attributes);
        return changed_;
    }
    bool restore()
    {
        if (!changed_) return true;
        if (!SetFileAttributesW(reinterpret_cast<LPCWSTR>(path_.utf16()), original_)) return false;
        changed_ = false;
        return true;
    }
private:
    QString path_;
    DWORD original_ = INVALID_FILE_ATTRIBUTES;
    bool changed_ = false;
};

// A junction is an actual Windows directory reparse point, but unlike a
// symbolic link it does not need SeCreateSymbolicLinkPrivilege. Every endpoint
// is contained in this fixture's root and cleanup removes only the link itself.
class TestJunctions {
public:
    explicit TestJunctions(QString ownedRoot) : ownedRoot_(normalizePath(ownedRoot)) {}
    ~TestJunctions()
    {
        QString error;
        if (!removeAll(&error)) recordCleanupFailure(error);
    }

    bool create(const QString &link, const QString &target, QString *error)
    {
        const QString normalizedLink = normalizePath(link);
        const QString normalizedTarget = normalizePath(target);
        if (ownedRoot_.isEmpty() || !QDir::isAbsolutePath(normalizedLink)
            || !isPathWithin(normalizedLink, ownedRoot_) || !isPathWithin(normalizedTarget, ownedRoot_)
            || normalizedLink == ownedRoot_ || QFileInfo::exists(normalizedLink)
            || !QFileInfo(normalizedTarget).isDir()) {
            *error = QStringLiteral("Invalid owned junction endpoints: %1 -> %2").arg(link, target);
            return false;
        }
        const QString display = QDir::toNativeSeparators(normalizedTarget);
        const QString substitute = display.startsWith(QStringLiteral("\\\\"))
            ? QStringLiteral("\\??\\UNC\\") + display.mid(2)
            : QStringLiteral("\\??\\") + display;
        struct MountPointHeader {
            DWORD tag;
            WORD dataLength;
            WORD reserved;
            WORD substituteOffset;
            WORD substituteLength;
            WORD printOffset;
            WORD printLength;
        };
        static_assert(sizeof(MountPointHeader) == 16);
        const qsizetype substituteBytes = substitute.size() * sizeof(wchar_t);
        const qsizetype displayBytes = display.size() * sizeof(wchar_t);
        const qsizetype payloadBytes = substituteBytes + displayBytes + 2 * sizeof(wchar_t);
        if (sizeof(MountPointHeader) + payloadBytes > MAXIMUM_REPARSE_DATA_BUFFER_SIZE) {
            *error = QStringLiteral("Junction target exceeds the reparse buffer limit");
            return false;
        }
        const MountPointHeader header{IO_REPARSE_TAG_MOUNT_POINT, WORD(8 + payloadBytes), 0,
                                      0, WORD(substituteBytes), WORD(substituteBytes + sizeof(wchar_t)),
                                      WORD(displayBytes)};
        QByteArray buffer(sizeof(MountPointHeader) + payloadBytes, '\0');
        std::memcpy(buffer.data(), &header, sizeof(header));
        std::memcpy(buffer.data() + sizeof(header), substitute.utf16(), size_t(substituteBytes));
        std::memcpy(buffer.data() + sizeof(header) + substituteBytes + sizeof(wchar_t),
                    display.utf16(), size_t(displayBytes));
        const QString nativeLink = extendedPath(normalizedLink);
        if (!CreateDirectoryW(reinterpret_cast<LPCWSTR>(nativeLink.utf16()), nullptr)) {
            *error = QStringLiteral("CreateDirectoryW failed for owned junction: %1").arg(GetLastError());
            return false;
        }
        HANDLE handle = CreateFileW(reinterpret_cast<LPCWSTR>(nativeLink.utf16()), GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, nullptr);
        DWORD returned = 0;
        const bool created = handle != INVALID_HANDLE_VALUE
            && DeviceIoControl(handle, FSCTL_SET_REPARSE_POINT, buffer.data(), DWORD(buffer.size()),
                               nullptr, 0, &returned, nullptr);
        const DWORD failure = created ? ERROR_SUCCESS : GetLastError();
        if (handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
        if (!created) {
            // This path was created immediately above inside the owned root.
            // RemoveDirectoryW never recursively follows a junction target.
            RemoveDirectoryW(reinterpret_cast<LPCWSTR>(nativeLink.utf16()));
            *error = QStringLiteral("FSCTL_SET_REPARSE_POINT failed: %1").arg(failure);
            return false;
        }
        links_.append(normalizedLink);
        return true;
    }

    bool removeAll(QString *error)
    {
        bool removed = true;
        for (auto it = links_.rbegin(); it != links_.rend(); ++it) {
            const QString parent = QFileInfo(*it).absolutePath();
            const QString nativeLink = extendedPath(*it);
            const DWORD attributes = GetFileAttributesW(reinterpret_cast<LPCWSTR>(nativeLink.utf16()));
            if (attributes == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_FILE_NOT_FOUND) continue;
            if (!QDir::isAbsolutePath(*it) || !isPathWithin(parent, ownedRoot_)
                || attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
                || !RemoveDirectoryW(reinterpret_cast<LPCWSTR>(nativeLink.utf16()))) {
                if (error) *error = QStringLiteral("Cannot remove owned junction %1 (Windows error %2)")
                                       .arg(*it).arg(GetLastError());
                removed = false;
            }
        }
        if (removed) links_.clear();
        return removed;
    }
private:
    static QString extendedPath(const QString &path)
    {
        const QString native = QDir::toNativeSeparators(path);
        return native.startsWith(QStringLiteral("\\\\"))
            ? QStringLiteral("\\\\?\\UNC\\") + native.mid(2) : QStringLiteral("\\\\?\\") + native;
    }
    QString ownedRoot_;
    QStringList links_;
};
#endif

bool writeFile(const QString &path, const QByteArray &contents = "catalog test")
{
    if (!QDir().mkpath(QFileInfo(path).absolutePath())) return false;
    QFile file(path);
    return file.open(QIODevice::WriteOnly) && file.write(contents) == contents.size();
}

QSet<QString> fileNames(const ScanResult &result)
{
    QSet<QString> names;
    for (const auto &file : result.files) names.insert(QFileInfo(file.path).fileName());
    return names;
}

bool createRepository(const QString &directory)
{
    git_repository *repository = nullptr;
    const bool result = git_repository_init(&repository, directory.toUtf8().constData(), 0) == 0;
    git_repository_free(repository);
    return result;
}

bool trackFile(const QString &directory, const QByteArray &relative)
{
    git_repository *rawRepository = nullptr;
    if (git_repository_open(&rawRepository, directory.toUtf8().constData()) < 0) return false;
    std::unique_ptr<git_repository, decltype(&git_repository_free)> repository(rawRepository, git_repository_free);
    git_index *rawIndex = nullptr;
    if (git_repository_index(&rawIndex, repository.get()) < 0) return false;
    std::unique_ptr<git_index, decltype(&git_index_free)> index(rawIndex, git_index_free);
    return git_index_add_bypath(index.get(), relative.constData()) == 0 && git_index_write(index.get()) == 0;
}
}

class FileCatalogTest : public QObject {
    Q_OBJECT
    std::unique_ptr<TestEnvironment> gitConfiguration_;
private slots:
    void cleanup()
    {
        const auto error = takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }
    void initTestCase()
    {
        gitConfiguration_ = std::make_unique<TestEnvironment>("git-config");
        QVERIFY2(gitConfiguration_->isValid(), qPrintable(gitConfiguration_->error()));
        QVERIFY(git_libgit2_init() >= 0);
        // The fixture must not read the real user's global/system Git settings.
        const QByteArray configurationRoot = gitConfiguration_->root().toUtf8();
        for (const int level : {GIT_CONFIG_LEVEL_SYSTEM, GIT_CONFIG_LEVEL_XDG, GIT_CONFIG_LEVEL_GLOBAL, GIT_CONFIG_LEVEL_PROGRAMDATA})
            QCOMPARE(git_libgit2_opts(GIT_OPT_SET_SEARCH_PATH, level, configurationRoot.constData()), 0);
    }
    void cleanupTestCase()
    {
        git_libgit2_shutdown();
        gitConfiguration_.reset();
        const auto error = takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }
    void normalizedPathsHaveComponentBoundaries()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = QDir(temporary.root()).filePath(QString::fromUtf8("中文 root"));
        QVERIFY(QDir().mkpath(root + "/nested"));
        QVERIFY(writeFile(root + "/nested/note.txt"));
        QCOMPARE(normalizePath({}), QString{});
        QCOMPARE(normalizePath(root + "/nested/../nested/note.txt"), normalizePath(root + "/nested/note.txt"));
        QVERIFY(isPathWithin(root, root));
        QVERIFY(isPathWithin(root + "/nested/note.txt", root + "/"));
        QVERIFY(!isPathWithin(root + "-sibling/note.txt", root));
        QVERIFY(!isPathWithin({}, root));
        QVERIFY(!isPathWithin(root, {}));
#ifdef Q_OS_WIN
        QVERIFY(isPathWithin(root.toUpper() + "/nested/note.txt", root.toLower()));
#endif
    }

    void scansUnicodeAndDeduplicatesOverlappingRoots()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = temporary.root();
        QVERIFY(writeFile(root + QString::fromUtf8("/中文 空格/笔记.txt")));
        QVERIFY(writeFile(root + "/photo.PNG"));
        QVERIFY(writeFile(root + "/document.PDF"));
        QVERIFY(writeFile(root + "/recording.wav"));
        QVERIFY(writeFile(root + "/movie.mp4"));
        QVERIFY(writeFile(root + "/unknown.binary"));
        QVERIFY(writeFile(root + "/source.cpp"));
        QVERIFY(QDir().mkpath(root + "/empty"));
        const auto result = FileCatalog::scan({{root, root + QString::fromUtf8("/中文 空格")}, {}, false});
        QVERIFY2(result.errors.isEmpty(), qPrintable(result.errors.join('\n')));
        QCOMPARE(result.files.size(), 5);
        QSet<QString> kinds;
        for (const auto &record : result.files) {
            QVERIFY(QDir::isAbsolutePath(record.path));
            QVERIFY(!record.identity.isEmpty());
            QCOMPARE(record.fingerprint.size(), 64);
            QVERIFY(record.size > 0);
            QVERIFY(record.modifiedMs > 0);
            kinds.insert(record.kind);
        }
        QCOMPARE(kinds, QSet<QString>({"text", "image", "pdf", "audio", "video"}));
        QStringList expectedDirectories{normalizePath(root), normalizePath(root + "/empty"),
            normalizePath(root + QString::fromUtf8("/中文 空格"))};
        expectedDirectories.sort();
        QCOMPARE(result.directories, expectedDirectories);
    }

    void exclusionsUseBoundariesAndChosenNestedRootOverridesAncestor()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = temporary.root();
        QVERIFY(writeFile(root + "/private/omitted.txt"));
        QVERIFY(writeFile(root + "/private/chosen/kept.txt"));
        QVERIFY(writeFile(root + "/private-sibling/visible.txt"));
        const auto result = FileCatalog::scan({{root, root + "/private/chosen"}, {root + "/private"}, false});
        QVERIFY(result.errors.isEmpty());
        QCOMPARE(fileNames(result), QSet<QString>({"kept.txt", "visible.txt"}));
        QStringList expectedDirectories{normalizePath(root), normalizePath(root + "/private/chosen"),
            normalizePath(root + "/private-sibling")};
        expectedDirectories.sort();
        QCOMPARE(result.directories, expectedDirectories);
    }

    void excludesGeneratedDirectoriesAndSecrets()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = temporary.root();
        QVERIFY(writeFile(root + "/node_modules/package/readme.txt"));
        QVERIFY(writeFile(root + "/folder.noindex/note.txt"));
        QVERIFY(writeFile(root + "/.svn/internal.txt"));
        QVERIFY(writeFile(root + "/secret-passwords.txt"));
        QVERIFY(writeFile(root + "/credentials.yaml"));
        QVERIFY(writeFile(root + "/notes.txt"));
        const auto result = FileCatalog::scan({{root}, {}, false});
        QVERIFY(result.errors.isEmpty());
        QCOMPARE(fileNames(result), QSet<QString>({"notes.txt"}));
        QCOMPARE(result.directories, QStringList{normalizePath(root)});
    }

    void independentScopesAllowSharedTextAndRejectCodeData()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = temporary.root();
        const QStringList names{"shared.txt", "README.md", "guide.markdown", "page.html", "main.cpp",
            "script.py", "Makefile", "LICENSE.txt", "NOTICE.md", "bundle.min.js", "theme.min.css",
            "data.json", "records.csv", "photo.png", "recording.wav", "document.pdf", "rich.rtf"};
        for (const auto &name : names) QVERIFY(writeFile(QDir(root).filePath(name)));
        const auto general = FileCatalog::scan({{root}, {}, false, FolderRules::Scope::General});
        QVERIFY2(general.errors.isEmpty(), qPrintable(general.errors.join('\n')));
        QCOMPARE(fileNames(general), QSet<QString>({"shared.txt", "README.md", "guide.markdown", "page.html",
            "LICENSE.txt", "NOTICE.md", "photo.png", "recording.wav", "document.pdf", "rich.rtf"}));
        const auto code = FileCatalog::scan({{root}, {}, false, FolderRules::Scope::Code});
        QVERIFY2(code.errors.isEmpty(), qPrintable(code.errors.join('\n')));
        QCOMPARE(fileNames(code), QSet<QString>({"shared.txt", "README.md", "guide.markdown", "page.html",
            "main.cpp", "script.py", "Makefile"}));
        for (const auto &record : code.files) QCOMPARE(record.kind, QStringLiteral("code"));
        const auto sharedIn = [](const ScanResult &result) {
            for (const auto &record : result.files)
                if (QFileInfo(record.path).fileName() == "shared.txt") return record;
            return FileRecord{};
        };
        const auto generalShared = sharedIn(general);
        const auto codeShared = sharedIn(code);
        QCOMPARE(generalShared.kind, QStringLiteral("text"));
        QCOMPARE(codeShared.kind, QStringLiteral("code"));
        QCOMPARE(generalShared.path, codeShared.path);
        QCOMPARE(generalShared.identity, codeShared.identity);
        QCOMPARE(generalShared.fingerprint, codeShared.fingerprint);
    }

    void projectContentIsLimitedToScreenshotsEvenWhenChosen()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString project = temporary.path("project");
        QVERIFY(writeFile(project + "/package.json", "{}"));
        QVERIFY(writeFile(project + "/notes.txt"));
        QVERIFY(writeFile(project + "/photo.png"));
        QVERIFY(writeFile(project + "/nested/recording.wav"));
        QVERIFY(writeFile(project + "/nested/source.cpp"));
        const QStringList screenshots{"Screenshot 2026-10-14.png", "Screen Shot example.jpg",
            "CleanShot result.webp", "SCR-example.PNG"};
        for (const auto &name : screenshots) QVERIFY(writeFile(project + "/nested/" + name));
        // A screenshot-like name on a document must not bypass project rules.
        QVERIFY(writeFile(project + "/Screenshot notes.txt"));
        QVERIFY(writeFile(temporary.path("outside.txt")));
        const auto selectedProject = FileCatalog::scan({{project}, {}, false});
        QVERIFY2(selectedProject.errors.isEmpty(), qPrintable(selectedProject.errors.join('\n')));
        QCOMPARE(fileNames(selectedProject), QSet<QString>(screenshots.cbegin(), screenshots.cend()));
        for (const auto &record : selectedProject.files) {
            QVERIFY(record.screenshot);
            QCOMPARE(record.kind, QStringLiteral("image"));
        }
        auto expected = fileNames(selectedProject);
        expected.insert("outside.txt");
        const auto ancestor = FileCatalog::scan({{temporary.root()}, {}, false});
        QVERIFY2(ancestor.errors.isEmpty(), qPrintable(ancestor.errors.join('\n')));
        QCOMPARE(fileNames(ancestor), expected);
        const auto code = FileCatalog::scan({{project}, {}, false, FolderRules::Scope::Code});
        QVERIFY2(code.errors.isEmpty(), qPrintable(code.errors.join('\n')));
        QCOMPARE(fileNames(code), QSet<QString>({"notes.txt", "source.cpp", "Screenshot notes.txt"}));
        for (const auto &record : code.files) QVERIFY(!record.screenshot);
    }

    void explicitlyChosenExcludedDirectoriesStillExcludeDescendants_data()
    {
        QTest::addColumn<QString>("directory");
        QTest::addColumn<bool>("code");
        for (const auto &directory : {".hidden", "node_modules", "Example.app"}) {
            QTest::newRow(qPrintable(QString::fromLatin1(directory) + "-general")) << QString::fromLatin1(directory) << false;
            QTest::newRow(qPrintable(QString::fromLatin1(directory) + "-code")) << QString::fromLatin1(directory) << true;
        }
    }

    void explicitlyChosenExcludedDirectoriesStillExcludeDescendants()
    {
        QFETCH(QString, directory);
        QFETCH(bool, code);
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString selected = QDir(temporary.root()).filePath(directory);
        QVERIFY(writeFile(selected + "/kept.txt"));
        QVERIFY(writeFile(selected + "/nested/visible.txt"));
        QVERIFY(writeFile(selected + "/.hidden.txt"));
        QVERIFY(writeFile(selected + "/.hidden/hidden.txt"));
        QVERIFY(writeFile(selected + "/node_modules/cached.txt"));
        QVERIFY(writeFile(selected + "/Nested.app/bundled.txt"));
        const auto scope = code ? FolderRules::Scope::Code : FolderRules::Scope::General;
        const auto notChosen = FileCatalog::scan({{temporary.root()}, {}, false, scope});
        QVERIFY2(notChosen.errors.isEmpty(), qPrintable(notChosen.errors.join('\n')));
        QVERIFY(notChosen.files.isEmpty());
        const auto chosen = FileCatalog::scan({{temporary.root(), selected}, {}, false, scope});
        QVERIFY2(chosen.errors.isEmpty(), qPrintable(chosen.errors.join('\n')));
        QCOMPARE(fileNames(chosen), QSet<QString>({"kept.txt", "visible.txt"}));
        QVERIFY(!chosen.directories.contains(normalizePath(selected + "/.hidden")));
        QVERIFY(!chosen.directories.contains(normalizePath(selected + "/node_modules")));
        QVERIFY(!chosen.directories.contains(normalizePath(selected + "/Nested.app")));
    }

    void mediaDatasetDetectionUsesCountRatioAndCameraExceptions_data()
    {
        QTest::addColumn<int>("total");
        QTest::addColumn<int>("machine");
        QTest::addColumn<bool>("camera");
        QTest::addColumn<bool>("excluded");
        QTest::newRow("below-minimum-count") << 299 << 299 << false << false;
        QTest::newRow("below-seventy-percent") << 300 << 209 << false << false;
        QTest::newRow("at-seventy-percent") << 300 << 210 << false << true;
        QTest::newRow("camera-names-are-personal-media") << 300 << 300 << true << false;
    }

    void mediaDatasetDetectionUsesCountRatioAndCameraExceptions()
    {
        QFETCH(int, total);
        QFETCH(int, machine);
        QFETCH(bool, camera);
        QFETCH(bool, excluded);
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString dataset = temporary.path("media");
        const QStringList cameraPrefixes{"IMG_", "DSC_", "PXL_", "VID_", "DJI_"};
        for (int index = 0; index < total; ++index) {
            const QString prefix = index < machine
                ? (camera ? cameraPrefixes[index % cameraPrefixes.size()] : QStringLiteral("sample-"))
                : QStringLiteral("summer-holiday-view-");
            QVERIFY(writeFile(QDir(dataset).filePath(prefix + QString::number(index).rightJustified(4, u'0') + ".jpg")));
        }
        QVERIFY(writeFile(dataset + "/Screenshot retained.png"));
        QVERIFY(writeFile(dataset + "/personal-recording.wav"));
        QVERIFY(writeFile(dataset + "/notes.txt"));
        const auto result = FileCatalog::scan({{temporary.root()}, {}, false});
        QVERIFY2(result.errors.isEmpty(), qPrintable(result.errors.join('\n')));
        if (excluded) {
            QCOMPARE(fileNames(result), QSet<QString>({"Screenshot retained.png", "notes.txt"}));
        } else {
            QCOMPARE(result.files.size(), total + 3);
        }
        // Explicitly selecting the media directory opts in, also when an
        // ancestor was scanned first and excluded this directory's dataset.
        const auto selected = FileCatalog::scan({{temporary.root(), dataset}, {}, false});
        QVERIFY2(selected.errors.isEmpty(), qPrintable(selected.errors.join('\n')));
        QCOMPARE(selected.files.size(), total + 3);
    }

#ifdef Q_OS_WIN
    void offlineFilesAreRejectedBeforeContentInspection()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString document = temporary.path("offline.txt");
        const QString stream = temporary.path("offline.ts");
        QVERIFY(writeFile(document));
        QByteArray transport(600, '\0');
        transport[0] = transport[188] = transport[376] = 0x47;
        QVERIFY(writeFile(stream, transport));
        TestWindowsAttributes documentAttributes(document), streamAttributes(stream);
        QVERIFY(documentAttributes.add(FILE_ATTRIBUTE_OFFLINE));
        QVERIFY(streamAttributes.add(FILE_ATTRIBUTE_OFFLINE));
        for (const auto &path : {document, stream}) {
            FileRecord record;
            QString error;
            QVERIFY(!FileCatalog::describe(path, &record, &error));
            QVERIFY2(error.contains("cloud"), qPrintable(error));
            QVERIFY(record.path.isEmpty());
        }
        const auto offline = FileCatalog::scan({{temporary.root()}, {}, true});
        QVERIFY2(offline.errors.isEmpty(), qPrintable(offline.errors.join('\n')));
        QVERIFY(offline.files.isEmpty());
        QVERIFY(documentAttributes.restore());
        QVERIFY(streamAttributes.restore());
        const auto downloaded = FileCatalog::scan({{temporary.root()}, {}, false});
        QVERIFY2(downloaded.errors.isEmpty(), qPrintable(downloaded.errors.join('\n')));
        QCOMPARE(fileNames(downloaded), QSet<QString>({"offline.txt", "offline.ts"}));
    }
#endif

#ifdef Q_OS_MACOS
    void macScreenCaptureAttributePreservesProjectScreenshot()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString screenshot = temporary.path("renamed-capture.png");
        QVERIFY(writeFile(temporary.path("package.json"), "{}"));
        QVERIFY(writeFile(screenshot));
        const QByteArray name = QFile::encodeName(screenshot);
        const char capture[] = "true";
        QCOMPARE(::setxattr(name.constData(), "com.apple.metadata:kMDItemIsScreenCapture",
                            capture, sizeof(capture) - 1, 0, XATTR_NOFOLLOW), 0);
        const auto result = FileCatalog::scan({{temporary.root()}, {}, false});
        QVERIFY2(result.errors.isEmpty(), qPrintable(result.errors.join('\n')));
        QCOMPARE(result.files.size(), 1);
        QVERIFY(result.files.constFirst().screenshot);
        QCOMPARE(QFileInfo(result.files.constFirst().path).fileName(), QStringLiteral("renamed-capture.png"));
        QCOMPARE(::removexattr(name.constData(), "com.apple.metadata:kMDItemIsScreenCapture", XATTR_NOFOLLOW), 0);
    }
#endif

    void missingRootIsExplicitError()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const auto result = FileCatalog::scan({{temporary.root() + "/absent", ""}, {}, false});
        QVERIFY(result.files.isEmpty());
        QVERIFY(result.directories.isEmpty());
        QCOMPARE(result.errors.size(), 2);
        QVERIFY(!result.cancelled);
    }

    void longPathsUseNativeMetadata()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        QString directory = temporary.root();
        for (int index = 0; index < 6; ++index)
            directory += "/" + QString(45, QChar('a' + index));
        const QString path = directory + QString::fromUtf8("/较长路径 note.txt");
        QVERIFY(path.size() > 260);
        QVERIFY(writeFile(path));
        FileRecord described;
        QString error;
        QVERIFY2(FileCatalog::describe(path, &described, &error), qPrintable(error));
        QCOMPARE(described.path, normalizePath(path));
        const auto result = FileCatalog::scan({{temporary.root()}, {}, false});
        QVERIFY2(result.errors.isEmpty(), qPrintable(result.errors.join('\n')));
        QCOMPARE(result.files.size(), 1);
        QCOMPARE(result.files[0].identity, described.identity);
    }

    void describeRejectsMissingDirectoryAndUnknownKind()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        FileRecord record;
        QString error;
        QVERIFY(!FileCatalog::describe(temporary.root() + "/absent.txt", &record, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!FileCatalog::describe(temporary.root(), &record, &error));
        QVERIFY(writeFile(temporary.root() + "/unknown.binary"));
        QVERIFY(!FileCatalog::describe(temporary.root() + "/unknown.binary", &record, &error));
        QVERIFY(writeFile(temporary.root() + "/valid.txt"));
        QVERIFY(!FileCatalog::describe(temporary.root() + "/valid.txt", nullptr, &error));
        QVERIFY(FileCatalog::describe(temporary.root() + "/valid.txt", &record, &error));
        QVERIFY(error.isEmpty());
    }

    void cancellationDoesNotScan()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        QVERIFY(writeFile(temporary.root() + "/note.txt"));
        std::atomic_bool cancelled{true};
        const auto result = FileCatalog::scan({{temporary.root()}, {}, false}, &cancelled);
        QVERIFY(result.cancelled);
        QVERIFY(result.files.isEmpty());
        QVERIFY(result.directories.isEmpty());
    }

    void cancellationDuringDiscovery_data()
    {
        QTest::addColumn<bool>("repository");
        QTest::newRow("ordinary-files") << false;
        QTest::newRow("repository-files") << true;
    }

    void cancellationDuringDiscovery()
    {
        QFETCH(bool, repository);
        TestEnvironment temporary(QStringLiteral("active-discovery-cancel"));
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        if (repository) QVERIFY(createRepository(temporary.root()));
        constexpr qsizetype total = 100;
        for (qsizetype index = 0; index < total; ++index)
            QVERIFY(writeFile(temporary.path(QStringLiteral("file-%1.txt").arg(index))));
        const FolderRules rules{{temporary.root()}, {}, repository};
        std::atomic_bool cancelled{false};
        QList<qsizetype> counts;
        const auto partial = FileCatalog::scan(rules, &cancelled, [&](qsizetype discovered) {
            counts.append(discovered);
            if (discovered == 25) cancelled.store(true, std::memory_order_relaxed);
        });
        QVERIFY2(partial.errors.isEmpty(), qPrintable(partial.errors.join('\n')));
        QVERIFY(partial.cancelled);
        QVERIFY(partial.files.size() > 0 && partial.files.size() < total);
        QCOMPARE(partial.files.size(), 25);
        QCOMPARE(counts.size(), partial.files.size());
        for (qsizetype index = 0; index < counts.size(); ++index) QCOMPARE(counts[index], index + 1);
        cancelled.store(false, std::memory_order_relaxed);
        const auto complete = FileCatalog::scan(rules, &cancelled);
        QVERIFY2(complete.errors.isEmpty(), qPrintable(complete.errors.join('\n')));
        QVERIFY(!complete.cancelled);
        QCOMPARE(complete.files.size(), total);
    }

    void identitySurvivesRenameAndFingerprintChangesWithContents()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString first = temporary.root() + "/first.txt";
        const QString second = temporary.root() + "/renamed.txt";
        QVERIFY(writeFile(first, "one"));
        const auto before = FileCatalog::scan({{temporary.root()}, {}, false});
        QCOMPARE(before.files.size(), 1);
        QVERIFY(QFile::rename(first, second));
        const auto renamed = FileCatalog::scan({{temporary.root()}, {}, false});
        QCOMPARE(renamed.files.size(), 1);
        QCOMPARE(renamed.files[0].identity, before.files[0].identity);
        QVERIFY(writeFile(second, "longer changed contents"));
        const auto changed = FileCatalog::scan({{temporary.root()}, {}, false});
        QCOMPARE(changed.files.size(), 1);
        QCOMPARE(changed.files[0].identity, before.files[0].identity);
        QVERIFY(changed.files[0].fingerprint != before.files[0].fingerprint);
    }

    void sameSizeEditWithRestoredMtimeChangesFingerprint()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString path = temporary.root() + "/version.txt";
        QVERIFY(writeFile(path, "one"));
        const auto before = FileCatalog::scan({{temporary.root()}, {}, false});
        QCOMPARE(before.files.size(), 1);
        const QDateTime timestamp = QFileInfo(path).lastModified();
        QTest::qWait(20);
        QVERIFY(writeFile(path, "two"));
        QFile file(path);
        QVERIFY(file.open(QIODevice::ReadWrite));
        QVERIFY(file.setFileTime(timestamp, QFileDevice::FileModificationTime));
        file.close();
        const auto after = FileCatalog::scan({{temporary.root()}, {}, false});
        QCOMPARE(after.files.size(), 1);
        QCOMPARE(after.files[0].size, before.files[0].size);
        QVERIFY(after.files[0].fingerprint != before.files[0].fingerprint);
    }

    void nativeLinksCannotEscapeOrLoop()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = temporary.root() + "/chosen";
        const QString outside = temporary.root() + "/outside";
        QVERIFY(writeFile(root + "/inside.txt"));
        QVERIFY(writeFile(outside + "/outside.txt"));
#ifdef Q_OS_WIN
        TestJunctions junctions(temporary.root());
        QString junctionError;
        QVERIFY2(junctions.create(root + "/outside-link", outside, &junctionError), qPrintable(junctionError));
        QVERIFY2(junctions.create(root + "/loop", root, &junctionError), qPrintable(junctionError));
        QVERIFY(QFileInfo(root + "/outside-link").isJunction());
        QVERIFY(QFileInfo(root + "/loop").isJunction());
        qInfo("Native link coverage: Windows directory junctions (outside target and loop)");
#else
        TestSymbolicLink outsideLink, loopLink, fileLink;
        QString linkError;
        QVERIFY2(outsideLink.create(root + "/outside-link", outside, &linkError), qPrintable(linkError));
        QVERIFY2(loopLink.create(root + "/loop", root, &linkError), qPrintable(linkError));
        QVERIFY2(fileLink.create(root + "/file-link.txt", outside + "/outside.txt", &linkError), qPrintable(linkError));
        qInfo("Native link coverage: POSIX directory and file symbolic links");
#endif
        QVERIFY(!isPathWithin(root + "/outside-link/does-not-exist.txt", root));
        QCOMPARE(normalizePath(root + "/outside-link/does-not-exist.txt"), normalizePath(outside + "/does-not-exist.txt"));
        const auto result = FileCatalog::scan({{root}, {}, false});
        QVERIFY(result.errors.isEmpty());
        QCOMPARE(fileNames(result), QSet<QString>({"inside.txt"}));
        QCOMPARE(result.directories, QStringList{normalizePath(root)});
        const auto linkedRoot = FileCatalog::scan({{root + "/outside-link"}, {}, false});
        QVERIFY(linkedRoot.files.isEmpty());
        QVERIFY(linkedRoot.directories.isEmpty());
        QCOMPARE(linkedRoot.errors.size(), 1);
#ifdef Q_OS_WIN
        QVERIFY2(junctions.removeAll(&junctionError), qPrintable(junctionError));
#else
        QVERIFY2(fileLink.remove(&linkError), qPrintable(linkError));
        QVERIFY2(loopLink.remove(&linkError), qPrintable(linkError));
        QVERIFY2(outsideLink.remove(&linkError), qPrintable(linkError));
        QVERIFY(!QFileInfo(root + "/file-link.txt").isSymLink());
        QVERIFY(!QFileInfo::exists(root + "/file-link.txt"));
#endif
        QVERIFY(!QFileInfo(root + "/outside-link").isSymLink());
        QVERIFY(!QFileInfo(root + "/loop").isSymLink());
        QVERIFY(!QFileInfo::exists(root + "/outside-link"));
        QVERIFY(!QFileInfo::exists(root + "/loop"));
        for (const auto &target : {root + "/inside.txt", outside + "/outside.txt"}) {
            QFile preserved(target);
            QVERIFY(preserved.open(QIODevice::ReadOnly));
            QCOMPARE(preserved.readAll(), QByteArray("catalog test"));
        }
    }

    void typescriptIsNotMistakenForVideo()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        QVERIFY(writeFile(temporary.root() + "/source.ts", "export const answer = 42;"));
        QByteArray video(600, '\0');
        video[0] = video[188] = video[376] = 0x47;
        QVERIFY(writeFile(temporary.root() + "/transport.ts", video));
        const auto ordinary = FileCatalog::scan({{temporary.root()}, {}, false});
        QCOMPARE(fileNames(ordinary), QSet<QString>({"transport.ts"}));
        const auto withCode = FileCatalog::scan({{temporary.root()}, {}, true});
        QCOMPARE(withCode.files.size(), 2);
        for (const auto &file : withCode.files)
            QCOMPARE(file.kind, QFileInfo(file.path).fileName() == "source.ts" ? QString("code") : QString("video"));
    }

    void gitIgnoreKeepsTrackedAndUntrackedSource()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString root = temporary.root();
        QVERIFY(createRepository(root));
        QVERIFY(writeFile(root + "/tracked.cpp"));
        QVERIFY(trackFile(root, "tracked.cpp"));
        QVERIFY(writeFile(root + "/.gitignore", "ignored/\ntracked.cpp\n*.generated.cpp\n"));
        QVERIFY(writeFile(root + "/ignored/hidden.cpp"));
        QVERIFY(writeFile(root + "/new.cpp"));
        QVERIFY(writeFile(root + QString::fromUtf8("/中文 source.cpp")));
        QVERIFY(writeFile(root + "/auto.generated.cpp"));
        QVERIFY(writeFile(root + "/README.md"));
        const auto result = FileCatalog::scan({{root}, {}, true});
        QVERIFY2(result.errors.isEmpty(), qPrintable(result.errors.join('\n')));
        QCOMPARE(fileNames(result), QSet<QString>({"tracked.cpp", "new.cpp", QString::fromUtf8("中文 source.cpp"), "README.md"}));
        QStringList expectedDirectories{normalizePath(root), normalizePath(root + "/ignored")};
        expectedDirectories.sort();
        QCOMPARE(result.directories, expectedDirectories);
        for (const auto &file : result.files) QCOMPARE(file.kind, QString("code"));
        const auto subtree = FileCatalog::scan({{root + "/ignored"}, {}, true});
        QVERIFY2(subtree.errors.isEmpty(), qPrintable(subtree.errors.join('\n')));
        QVERIFY(subtree.files.isEmpty());
    }

#ifdef Q_OS_WIN
    void gitIgnoreWorksInLongWindowsRepositoryWithoutChangingConfiguration()
    {
        TestEnvironment temporary("long-git-path");
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        QString root = temporary.path(QString::fromUtf8("中文 repository with spaces"));
        for (int depth = 0; root.size() < 320; ++depth)
            root = QDir(root).filePath(QStringLiteral("portable path component %1").arg(depth));
        QVERIFY(root.size() >= 320);
        QVERIFY(QDir().mkpath(root));
        QVERIFY2(createRepository(root), git_error_last() ? git_error_last()->message : "Cannot create long-path repository fixture");
        QVERIFY(writeFile(root + "/tracked.cpp"));
        {
            git_repository *rawRepository = nullptr;
            QCOMPARE(git_repository_open(&rawRepository, root.toUtf8().constData()), 0);
            std::unique_ptr<git_repository, decltype(&git_repository_free)> repository(rawRepository, git_repository_free);
            git_config *rawConfig = nullptr;
            QCOMPARE(git_repository_config(&rawConfig, repository.get()), 0);
            std::unique_ptr<git_config, decltype(&git_config_free)> config(rawConfig, git_config_free);
            // Only fixture preparation writes this owned repository. Leave an
            // explicit false value to prove the scanner's override is local.
            QCOMPARE(git_config_set_bool(config.get(), "core.longpaths", 1), 0);
            QVERIFY(trackFile(root, "tracked.cpp"));
            QCOMPARE(git_config_set_bool(config.get(), "core.longpaths", 0), 0);
        }
        QVERIFY(writeFile(root + "/.gitignore", "tracked.cpp\nignored/\n*.generated.cpp\n"));
        QVERIFY(writeFile(root + "/.git/info/exclude", "info-excluded.cpp\n"));
        QVERIFY(writeFile(root + "/ignored/hidden.cpp"));
        QVERIFY(writeFile(root + "/auto.generated.cpp"));
        QVERIFY(writeFile(root + "/info-excluded.cpp"));
        QVERIFY(writeFile(root + "/new.cpp"));
        QVERIFY(writeFile(root + QString::fromUtf8("/中文 source.cpp")));
        QVERIFY(writeFile(root + "/README.md"));
        const QString nested = root + QString::fromUtf8("/更深 nested directory/") + QString(48, u'n');
        QVERIFY(writeFile(nested + "/.gitignore", "*.cpp\n!kept.cpp\n"));
        QVERIFY(writeFile(nested + "/hidden-nested.cpp"));
        QVERIFY(writeFile(nested + "/kept.cpp"));

        QFile configuration(root + "/.git/config");
        QVERIFY(configuration.open(QIODevice::ReadOnly));
        const QByteArray originalConfiguration = configuration.readAll(); configuration.close();
        QFile index(root + "/.git/index");
        QVERIFY(index.open(QIODevice::ReadOnly));
        const QByteArray originalIndex = index.readAll(); index.close();
        QVERIFY(!originalConfiguration.isEmpty()); QVERIFY(!originalIndex.isEmpty());
        const QSet<QString> expected{"tracked.cpp", "new.cpp", QString::fromUtf8("中文 source.cpp"), "README.md", "kept.cpp"};
        // Every scan opens its own repository, reads the longpaths override
        // during ignore evaluation, and destroys its configuration before it
        // returns. Repeat the full lifecycle to catch premature entry release.
        for (int repetition = 0; repetition < 4; ++repetition) {
            const auto result = FileCatalog::scan({{root}, {}, true});
            QVERIFY2(result.errors.isEmpty(), qPrintable(result.errors.join('\n')));
            QCOMPARE(fileNames(result), expected);
            for (const auto &file : result.files) {
                QCOMPARE(file.kind, QString("code"));
                QVERIFY(isPathWithin(file.path, root));
            }
            QVERIFY(configuration.open(QIODevice::ReadOnly));
            QCOMPARE(configuration.readAll(), originalConfiguration); configuration.close();
            QVERIFY(index.open(QIODevice::ReadOnly));
            QCOMPARE(index.readAll(), originalIndex); index.close();
        }
        const auto subtree = FileCatalog::scan({{nested}, {}, true});
        QVERIFY2(subtree.errors.isEmpty(), qPrintable(subtree.errors.join('\n')));
        QCOMPARE(fileNames(subtree), QSet<QString>{"kept.cpp"});
        QVERIFY(configuration.open(QIODevice::ReadOnly));
        QCOMPARE(configuration.readAll(), originalConfiguration); configuration.close();
        QVERIFY(index.open(QIODevice::ReadOnly));
        QCOMPARE(index.readAll(), originalIndex); index.close();
    }
#endif

    void codeScanningWorksWithoutGitExecutable()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        QVERIFY(writeFile(temporary.root() + "/source.cpp"));
        ScopedEnvironment path("PATH", temporary.root().toUtf8());
        const auto result = FileCatalog::scan({{temporary.root()}, {}, true});
        QCOMPARE(result.files.size(), 1);
        QVERIFY(result.errors.isEmpty());
    }

    void invalidRepositoryFailsClosed()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        QVERIFY(writeFile(temporary.root() + "/source.cpp"));
        QVERIFY(writeFile(temporary.root() + "/.git", "gitdir: ./missing-repository\n"));
        const auto result = FileCatalog::scan({{temporary.root()}, {}, true});
        QVERIFY(result.files.isEmpty());
        QCOMPARE(result.errors.size(), 1);
        QVERIFY(result.errors[0].contains("Git"));
    }

    void codeScopeSkipsNestedWorktreeUnlessExplicitlySelected()
    {
        TestEnvironment temporary;
        QVERIFY2(temporary.isValid(), qPrintable(temporary.error()));
        const QString nested = temporary.path("nested-worktree");
        QVERIFY(writeFile(temporary.path("outer.cpp")));
        QVERIFY(writeFile(nested + "/inner.cpp"));
        QVERIFY(writeFile(nested + "/.git", "gitdir: ./missing-repository\n"));
        const auto ancestor = FileCatalog::scan({{temporary.root()}, {}, false, FolderRules::Scope::Code});
        QVERIFY2(ancestor.errors.isEmpty(), qPrintable(ancestor.errors.join('\n')));
        QCOMPARE(fileNames(ancestor), QSet<QString>{"outer.cpp"});
        QVERIFY(!ancestor.directories.contains(normalizePath(nested)));
        // The explicit root is examined instead of being silently skipped.
        // Its broken Git metadata must fail closed rather than leak contents.
        const auto selected = FileCatalog::scan({{nested}, {}, false, FolderRules::Scope::Code});
        QVERIFY(selected.files.isEmpty());
        QCOMPARE(selected.errors.size(), 1);
        QVERIFY(selected.errors.constFirst().contains("Git"));
    }
};

QTEST_GUILESS_MAIN(FileCatalogTest)
#include "file_catalog_test.moc"
