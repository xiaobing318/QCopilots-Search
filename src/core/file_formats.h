#pragma once

#include <QByteArray>
#include <QFileInfo>
#include <QSet>
#include <QString>
#include <QStringList>

namespace qcopilots_search::file_formats {

// Discovery and extraction share these formats. File access, scope restrictions
// and content validation remain the responsibility of their callers.
inline const QSet<QString> &documentExtensions()
{
    static const QSet<QString> values{"txt", "text", "md", "markdown", "rtf", "docx", "doc", "odt", "html", "htm"};
    return values;
}

inline const QSet<QString> &sourceExtensions()
{
    static const QSet<QString> values{
        "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm", "swift", "py", "pyi", "pyx",
        "ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "kts",
        "scala", "groovy", "rb", "php", "cs", "fs", "sh", "bash", "zsh", "fish", "ps1", "lua", "pl", "pm",
        "r", "jl", "dart", "sql", "vue", "svelte", "astro", "html", "htm", "css", "scss", "sass", "less", "gradle", "cmake",
        "mk", "proto", "graphql", "gql", "tf", "hcl", "nix", "zig", "hs", "ex", "exs", "erl", "clj", "cljs",
        "elm", "ml", "mli", "vim", "el", "lisp", "asm", "s", "metal", "glsl", "hlsl", "wgsl", "cu", "cuh",
        "ino", "v", "sv", "tex", "qml"
    };
    return values;
}

inline const QSet<QString> &codeTextExtensions()
{
    static const QSet<QString> values{"md", "markdown", "rst", "adoc", "org", "txt", "yaml", "yml", "toml"};
    return values;
}

inline bool isCodeFilename(const QString &filename)
{
    static const QSet<QString> names{"makefile", "gnumakefile", "dockerfile", "containerfile", "justfile",
        "rakefile", "gemfile", "podfile", "brewfile", "procfile", "vagrantfile", "jenkinsfile", "cmakelists.txt"};
    const QString lower = filename.toLower();
    return names.contains(lower) || lower.startsWith(QStringLiteral("dockerfile."));
}

inline bool isCode(const QString &suffix, const QString &filename)
{
    const QString lower = suffix.toLower();
    return sourceExtensions().contains(lower) || codeTextExtensions().contains(lower)
        || lower == QStringLiteral("ipynb") || isCodeFilename(filename);
}

inline const QSet<QString> &rawImageExtensions()
{
    static const QSet<QString> values{"raw", "dng", "cr2", "cr3", "nef", "nrw", "arw", "sr2", "srf", "orf", "rw2",
        "raf", "pef", "srw", "kdc", "mrw", "erf", "mos", "iiq"};
    return values;
}

inline bool isImage(const QString &suffix)
{
    static const QSet<QString> common{"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "ico", "ppm",
        "pgm", "pbm", "heic", "heif", "avif"};
    const QString lower = suffix.toLower();
    return common.contains(lower) || rawImageExtensions().contains(lower);
}

inline bool isAudio(const QString &suffix)
{
    static const QSet<QString> values{"wav", "mp3", "flac", "ogg", "oga", "opus", "m4a", "aac", "aiff", "aif",
        "aifc", "wma", "caf", "mka", "m4b"};
    return values.contains(suffix.toLower());
}

inline bool isVideo(const QString &suffix)
{
    static const QSet<QString> values{"mp4", "m4v", "mov", "mkv", "avi", "webm", "wmv", "mpg", "mpeg", "3gp", "flv",
        "ts", "mts", "m2ts"};
    return values.contains(suffix.toLower());
}

inline bool isTransportStream(const QByteArray &head)
{
    for (const int stride : {188, 192}) {
        for (int offset = 0; offset <= 4; ++offset) {
            if (head.size() > offset + 2 * stride && uchar(head[offset]) == 0x47
                && uchar(head[offset + stride]) == 0x47 && uchar(head[offset + 2 * stride]) == 0x47)
                return true;
        }
    }
    return false;
}

inline bool isSensitiveFilename(const QString &filename)
{
    // Rules adapted from DigUp (MIT), Sources/DigUpKit/Rules.swift.
    static const QStringList prefixes{"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env", "secret",
        "credential", ".netrc", ".npmrc", ".pypirc", "htpasswd", ".htpasswd"};
    static const QSet<QString> extensions{"pem", "key", "p12", "pfx", "keystore", "jks", "ppk",
        "kdbx", "gpg", "asc", "crt", "cer", "der", "p8", "mobileprovision"};
    const QString lower = filename.toLower();
    if (extensions.contains(QFileInfo(lower).suffix())) return true;
    for (const auto &prefix : prefixes)
        if (lower.startsWith(prefix)) return true;
    return false;
}

} // namespace qcopilots_search::file_formats
