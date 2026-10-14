#include "platform_external_open.h"

#include <QDesktopServices>
#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QRegularExpression>
#include <QSettings>
#include <QStandardPaths>
#include <QUrl>

namespace qcopilots_search {
namespace {
QString millisecondsAsSeconds(qint64 milliseconds) {
    const qint64 value = qMax<qint64>(0, milliseconds);
    return QString::number(value / 1000) + QLatin1Char('.')
        + QString::number(value % 1000).rightJustified(3, QLatin1Char('0'));
}

std::optional<ExternalOpenCommand> invalid(QString *error, const QString &message) {
    if (error) *error = message;
    return std::nullopt;
}

QString findExecutable(const QString &program) {
    QString resolved = QFileInfo(program).isAbsolute()
        ? (QFileInfo(program).isExecutable() ? program : QString())
        : QStandardPaths::findExecutable(program);
#ifdef Q_OS_WIN
    if (resolved.isEmpty() && program.compare(QStringLiteral("SumatraPDF.exe"), Qt::CaseInsensitive) == 0) {
        for (const QString &hive : {QStringLiteral("HKEY_CURRENT_USER"), QStringLiteral("HKEY_LOCAL_MACHINE")}) {
            const QSettings appPaths(hive + QStringLiteral("\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\SumatraPDF.exe"), QSettings::NativeFormat);
            const QString path = appPaths.value(QStringLiteral(".")).toString();
            if (QFileInfo(path).isExecutable()) return path;
        }
        for (const QString &root : {qEnvironmentVariable("LOCALAPPDATA"), qEnvironmentVariable("ProgramFiles"), qEnvironmentVariable("ProgramFiles(x86)")}) {
            if (root.isEmpty()) continue;
            const QString path = QDir(root).filePath(QStringLiteral("SumatraPDF/SumatraPDF.exe"));
            if (QFileInfo(path).isExecutable()) return path;
        }
    }
#endif
    return resolved;
}

ExternalOpenCommand customCommand(const SearchHit &hit, const QString &program,
                                  const QStringList &arguments) {
    ExternalOpenCommand command;
    command.program = program;
    bool includesFile = false;
    for (const QString &argument : arguments) {
        includesFile |= argument.contains(QStringLiteral("{file}"));
        command.arguments.append(expandOpenArgument(argument, hit));
    }
    if (!includesFile) command.arguments.append(hit.path);
    command.notice = QStringLiteral("The external application received the location request. Confirm the page, line or playback position there.");
    return command;
}
}

QString resolveExternalExecutable(const QString &program, QString *error) {
    if (error) error->clear();
    const QString resolved = findExecutable(program);
#ifdef Q_OS_WIN
    const QString extension = QFileInfo(resolved.isEmpty() ? program : resolved).suffix().toLower();
    if (extension == QStringLiteral("cmd") || extension == QStringLiteral("bat")) {
        // The common VS Code CLI wrappers invoke cmd.exe, whose expansion rules
        // cannot preserve arbitrary source filenames through QProcess argv.
        // These applications also accept --goto directly on their real binary.
        const QString name = QFileInfo(resolved).completeBaseName().toLower();
        QString nativeName;
        if (name == QStringLiteral("code")) nativeName = QStringLiteral("Code.exe");
        else if (name == QStringLiteral("code-insiders")) nativeName = QStringLiteral("Code - Insiders.exe");
        else if (name == QStringLiteral("codium")) nativeName = QStringLiteral("VSCodium.exe");
        if (!nativeName.isEmpty()) {
            const QString nativePath = QDir(QFileInfo(resolved).absolutePath()).absoluteFilePath(QStringLiteral("../") + nativeName);
            if (QFileInfo(nativePath).isFile() && QFileInfo(nativePath).isExecutable()) return QDir::cleanPath(nativePath);
        }
        if (error) *error = QStringLiteral("Choose the application's .exe file instead of a .cmd or .bat wrapper so file paths are passed literally.");
        return {};
    }
#endif
    if (resolved.isEmpty() && error)
        *error = QStringLiteral("External application was not found: %1. Choose its executable in Settings, use ordinary Open, or use the built-in preview.").arg(program);
    return resolved;
}

QString expandOpenArgument(const QString &argument, const SearchHit &hit) {
    // Match against the original template only. A path containing literal
    // {line}, {page}, or another placeholder is never expanded a second time.
    static const QRegularExpression placeholder(QStringLiteral("\\{(file|page|line|cell|startMs|startSeconds)\\}"));
    QString expanded;
    qsizetype offset = 0;
    auto matches = placeholder.globalMatch(argument);
    while (matches.hasNext()) {
        const auto match = matches.next();
        expanded += argument.mid(offset, match.capturedStart() - offset);
        const QString token = match.captured(1);
        if (token == QStringLiteral("file")) expanded += hit.path;
        else if (token == QStringLiteral("page")) expanded += QString::number(qMax(1, hit.page));
        else if (token == QStringLiteral("line")) expanded += QString::number(qMax(1, hit.lineStart));
        else if (token == QStringLiteral("cell")) expanded += QString::number(hit.cell);
        else if (token == QStringLiteral("startMs")) expanded += QString::number(qMax<qint64>(0, hit.startMs));
        else expanded += millisecondsAsSeconds(hit.startMs);
        offset = match.capturedEnd();
    }
    expanded += argument.mid(offset);
    return expanded;
}

std::optional<ExternalOpenCommand> buildOpenCommand(const SearchHit &hit,
                                                   const AppSettings &settings,
                                                   QString *error) {
    if (error) error->clear();
    const bool pdf = hit.kind == QStringLiteral("pdf")
        || QFileInfo(hit.path).suffix().compare(QStringLiteral("pdf"), Qt::CaseInsensitive) == 0;
    const bool media = hit.kind == QStringLiteral("audio") || hit.kind == QStringLiteral("video");
    if (!pdf && !media && hit.lineStart > 0 && !settings.editorCommand.trimmed().isEmpty()) {
        QStringList arguments = QProcess::splitCommand(settings.editorCommand);
        if (arguments.isEmpty()) return invalid(error, QStringLiteral("The editor command is empty."));
        const QString program = arguments.takeFirst();
        bool includesFile = false;
        for (const QString &argument : arguments) includesFile |= argument.contains(QStringLiteral("{file}"));
        if (!includesFile) {
            const QString executable = QFileInfo(program).completeBaseName().toLower();
            if (executable == "code" || executable == "code-insiders" || executable == "codium")
                arguments << QStringLiteral("--goto") << QStringLiteral("{file}:{line}");
            else if (executable == "subl" || executable == "sublime_text")
                arguments << QStringLiteral("{file}:{line}");
        }
        return customCommand(hit, program, arguments);
    }
    const ExternalOpenerSettings opener = pdf ? settings.pdfOpener : media ? settings.mediaOpener : ExternalOpenerSettings{};
    if (opener.preset == QStringLiteral("system")) {
        ExternalOpenCommand command;
        command.systemOpen = true;
        if (pdf || media || hit.lineStart > 0)
            command.notice = QStringLiteral("Opened with the system application. Exact location is not guaranteed. Use the built-in preview or configure an external application for positioning.");
        return command;
    }
    if (opener.preset == QStringLiteral("custom")) {
        if (opener.program.trimmed().isEmpty()) return invalid(error, QStringLiteral("Choose an executable for the custom external application."));
        return customCommand(hit, opener.program, opener.arguments);
    }
    ExternalOpenCommand command;
    command.notice = QStringLiteral("The external application received the location request. Confirm the page or playback position there.");
    if (media && opener.preset == QStringLiteral("mpv")) {
        command.program = opener.program.isEmpty() ? QStringLiteral("mpv") : opener.program;
        command.arguments = {QStringLiteral("--start=") + millisecondsAsSeconds(hit.startMs),
                             QStringLiteral("--"), hit.path};
        return command;
    }
    if (pdf && opener.preset == QStringLiteral("sumatra")) {
#ifdef Q_OS_WIN
        command.program = opener.program.isEmpty() ? QStringLiteral("SumatraPDF.exe") : opener.program;
        command.arguments = {QStringLiteral("-reuse-instance"), QStringLiteral("-page"),
                             QString::number(qMax(1, hit.page)), hit.path};
        return command;
#else
        return invalid(error, QStringLiteral("The SumatraPDF preset is only supported on Windows. Choose a PDF application for this platform."));
#endif
    }
    if (pdf && opener.preset == QStringLiteral("okular")) {
#ifdef Q_OS_LINUX
        command.program = opener.program.isEmpty() ? QStringLiteral("okular") : opener.program;
        command.arguments = {QStringLiteral("--unique"), QStringLiteral("--page"),
                             QString::number(qMax(1, hit.page)), QUrl::fromLocalFile(hit.path).toString(QUrl::FullyEncoded)};
        return command;
#else
        return invalid(error, QStringLiteral("The Okular preset is only supported on Linux. Choose a PDF application for this platform."));
#endif
    }
    if (pdf && opener.preset == QStringLiteral("skim")) {
#ifdef Q_OS_MACOS
        QUrl url = QUrl::fromLocalFile(hit.path);
        url.setScheme(QStringLiteral("skim"));
        url.setFragment(QStringLiteral("page=") + QString::number(qMax(1, hit.page)));
        command.program = QStringLiteral("/usr/bin/open");
        command.arguments = opener.program.isEmpty()
            ? QStringList{QStringLiteral("-b"), QStringLiteral("net.sourceforge.skim-app.skim")}
            : QStringList{QStringLiteral("-a"), opener.program};
        command.arguments.append(url.toString(QUrl::FullyEncoded));
        command.waitForLauncher = true;
        return command;
#else
        return invalid(error, QStringLiteral("The Skim preset is only supported on macOS. Choose a PDF application for this platform."));
#endif
    }
    return invalid(error, QStringLiteral("The selected external application preset is not supported for this file."));
}

bool executeOpenCommand(const SearchHit &hit, const ExternalOpenCommand &command, QString *error) {
    if (error) error->clear();
    if (!QFileInfo(hit.path).isFile()) {
        if (error) *error = QStringLiteral("The source file no longer exists.");
        return false;
    }
    if (command.systemOpen) {
        if (!QDesktopServices::openUrl(QUrl::fromLocalFile(hit.path))) {
            if (error) *error = QStringLiteral("No application could open this file.");
            return false;
        }
    } else {
        const QString program = resolveExternalExecutable(command.program, error);
        if (program.isEmpty()) {
            return false;
        }
        if (command.waitForLauncher) {
            // macOS open is a short-lived dispatcher. Read its actual exit code
            // so an absent Skim installation is not reported as a successful launch.
            QProcess launcher;
            launcher.start(program, command.arguments);
            if (!launcher.waitForFinished(5000)) {
                launcher.kill();
                launcher.waitForFinished(1000);
                if (error) *error = QStringLiteral("The external application launcher did not complete: %1").arg(launcher.errorString());
                return false;
            }
            if (launcher.exitStatus() != QProcess::NormalExit || launcher.exitCode() != 0) {
                if (error) *error = QStringLiteral("Could not open the selected external application: %1").arg(QString::fromLocal8Bit(launcher.readAllStandardError()).trimmed());
                return false;
            }
        } else if (!QProcess::startDetached(program, command.arguments)) {
            if (error) *error = QStringLiteral("Could not start the configured external application. Use ordinary Open or the built-in preview.");
            return false;
        }
    }
    if (error) *error = command.notice;
    return true;
}

} // namespace qcopilots_search
