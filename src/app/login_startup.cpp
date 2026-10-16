#include "login_startup.h"
#include "login_startup_target.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QSaveFile>
#include <QSettings>
#include <QStandardPaths>
#include <QStringList>

namespace qcopilots_search {
#ifdef Q_OS_MACOS
bool macLoginStartupSupported();
bool macLoginStartupSetEnabled(bool enabled, QString *error);
LoginStartupState macLoginStartupState();
void macPrepareLoginStartupDetection();
bool macIsLoginStartupLaunch();
bool macCompleteLoginStartupDetection(bool showForeground);
#endif

namespace {
bool fail(QString *error, const QString &message) {
    if (error) *error = message;
    return false;
}

#ifdef Q_OS_WIN
QString windowsStartupCommand() {
    return windowsLoginStartupCommand(QCoreApplication::applicationFilePath());
}
#elif defined(Q_OS_LINUX)
const QString desktopFileName = QStringLiteral("com.qcopilots.qcopilots-search.desktop");

QString configHome() {
    const QString configured = qEnvironmentVariable("XDG_CONFIG_HOME");
    // The XDG specification requires absolute paths. Relative values are ignored.
    if (QDir::isAbsolutePath(configured)) return QDir::cleanPath(configured);
    const QString home = QDir::homePath();
    return QDir::isAbsolutePath(home) ? QDir(home).filePath(QStringLiteral(".config")) : QString{};
}

QString personalDesktopFile() {
    const QString root = configHome();
    return root.isEmpty() ? QString{} : QDir(root).filePath(QStringLiteral("autostart/") + desktopFileName);
}

QString effectiveDesktopFile() {
    const QString personal = personalDesktopFile();
    if (!personal.isEmpty() && QFileInfo::exists(personal)) return personal;
    const QString configured = qEnvironmentVariable("XDG_CONFIG_DIRS");
    const QStringList directories = configured.isEmpty() ? QStringList{QStringLiteral("/etc/xdg")}
        : configured.split(':', Qt::SkipEmptyParts);
    for (const QString &directory : directories) {
        if (!QDir::isAbsolutePath(directory)) continue;
        const QString path = QDir(directory).filePath(QStringLiteral("autostart/") + desktopFileName);
        if (QFileInfo::exists(path)) return path;
    }
    return {};
}

LoginStartupState desktopFileState(const QString &path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return {LoginStartupStatus::Error, QStringLiteral("Could not read the login startup entry: %1").arg(file.errorString())};
    const QByteArray entry = file.readAll();
    if (file.error() != QFileDevice::NoError)
        return {LoginStartupStatus::Error, QStringLiteral("Could not read the login startup entry: %1").arg(file.errorString())};
    return desktopAutostartState(entry, QCoreApplication::applicationFilePath(),
                                qgetenv("XDG_CURRENT_DESKTOP"));
}
#endif
} // namespace

QByteArray desktopAutostartEntry(const QString &executable, bool enabled) {
    if (executable.isEmpty() || executable.contains('=') || !QDir::isAbsolutePath(executable)) return {};
    QString quoted;
    for (const QChar character : executable) {
        if (character.unicode() < 0x20 || character.unicode() == 0x7f) return {};
        if (character == '"' || character == '`' || character == '$' || character == '\\') quoted += '\\';
        quoted += character;
        if (character == '%') quoted += '%';
    }
    // String-value unescaping happens before Exec argument unquoting. In
    // particular, a literal backslash therefore needs four backslashes here.
    quoted.replace(QStringLiteral("\\"), QStringLiteral("\\\\"));
    return (QStringLiteral("[Desktop Entry]\nType=Application\nVersion=1.0\nName=QCopilots Search\nExec=\"")
        + quoted + QStringLiteral("\" --login-startup\nIcon=com.qcopilots.qcopilots-search\nTerminal=false\nHidden=")
        + (enabled ? QStringLiteral("false\n") : QStringLiteral("true\n"))).toUtf8();
}

LoginStartupState desktopAutostartState(const QByteArray &entry, const QString &executable,
                                       const QByteArray &currentDesktops) {
    QHash<QByteArray, QByteArray> values;
    bool desktopGroup = false;
    for (const QByteArray &rawLine : entry.split('\n')) {
        const QByteArray line = rawLine.trimmed();
        if (line.startsWith('[')) desktopGroup = line == "[Desktop Entry]";
        else if (desktopGroup && !line.startsWith('#')) {
            const auto separator = line.indexOf('=');
            if (separator > 0) values.insert(line.left(separator).trimmed(), line.mid(separator + 1));
        }
    }
    if (values.value("Hidden") == "true" || values.value("X-GNOME-Autostart-enabled") == "false")
        return {LoginStartupStatus::Disabled, {}};
    if (values.value("Type") != "Application" || values.value("Exec").isEmpty())
        return {LoginStartupStatus::Error, QStringLiteral("The login startup entry is invalid. Save the setting again to repair it.")};
    QByteArray expectedExec;
    for (const QByteArray &line : desktopAutostartEntry(executable, true).split('\n'))
        if (line.startsWith("Exec=")) expectedExec = line.mid(5);
    if (expectedExec.isEmpty() || values.value("Exec") != expectedExec)
        return {LoginStartupStatus::Error, QStringLiteral("The login startup entry points to a different command. "
            "Use current directories at sign-in in Settings to repair it.")};
    const auto desktops = currentDesktops.split(':');
    const auto matchesDesktop = [&desktops](const QByteArray &list) {
        for (const QByteArray &desktop : desktops)
            if (!desktop.isEmpty() && list.split(';').contains(desktop)) return true;
        return false;
    };
    if ((values.contains("OnlyShowIn") && !matchesDesktop(values.value("OnlyShowIn")))
        || matchesDesktop(values.value("NotShowIn")))
        return {LoginStartupStatus::Disabled, QStringLiteral("The login entry does not apply to this desktop session.")};
    return {LoginStartupStatus::Enabled, {}};
}

#ifdef Q_OS_WIN
QString windowsLoginStartupCommand(const QString &executable, QString *error) {
    if (error) error->clear();
    if (!RuntimePaths::isAbsoluteFilesystemPath(executable) || executable.contains('"')
        || executable.contains('\n') || executable.contains('\r')) {
        fail(error, QStringLiteral("A fully qualified executable path is required for Windows sign-in launch."));
        return {};
    }
    const QString command = '"' + QDir::toNativeSeparators(executable) + QStringLiteral("\" --login-startup");
    // The Windows Run registry value supports at most 260 command characters.
    if (command.size() > 260) {
        fail(error, QStringLiteral("The application path is too long for Windows sign-in launch. "
            "Install QCopilots Search in a shorter directory and apply the setting again."));
        return {};
    }
    return command;
}
#endif

bool loginStartupSessionAllowed() {
    if (QStandardPaths::isTestModeEnabled()) return false;
    for (const QString &argument : QCoreApplication::arguments()) {
        if (argument == QStringLiteral("--profile-dir") || argument.startsWith(QStringLiteral("--profile-dir="))
            || argument == QStringLiteral("--model-dir") || argument.startsWith(QStringLiteral("--model-dir=")))
            return false;
    }
    return true;
}

bool loginStartupSupported() {
    if (!loginStartupSessionAllowed()) return false;
#ifdef Q_OS_WIN
    return true;
#elif defined(Q_OS_MACOS)
    return macLoginStartupSupported();
#elif defined(Q_OS_LINUX)
    return !configHome().isEmpty();
#else
    return false;
#endif
}

static LoginStartupState nativeLoginStartupState() {
    if (!loginStartupSessionAllowed())
        return {LoginStartupStatus::Unavailable, QStringLiteral("Login startup is unavailable for a test profile.")};
#ifdef Q_OS_WIN
    QSettings registry(QStringLiteral("HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"), QSettings::NativeFormat);
    const QString configured = registry.value(QStringLiteral("qcopilots-search")).toString();
    if (registry.status() != QSettings::NoError)
        return {LoginStartupStatus::Error, QStringLiteral("Could not read the Windows sign-in setting.")};
    if (configured.isEmpty()) return {LoginStartupStatus::Disabled, {}};
    const QString command = windowsStartupCommand();
    if (command.isEmpty())
        return {LoginStartupStatus::Error, QStringLiteral("The application path is too long or invalid for Windows sign-in launch.")};
    if (configured != command)
        return {LoginStartupStatus::Error, QStringLiteral("The Windows sign-in entry points to a different command. Save the setting again to repair it.")};
    return {LoginStartupStatus::Enabled, {}};
#elif defined(Q_OS_MACOS)
    return macLoginStartupState();
#elif defined(Q_OS_LINUX)
    if (!loginStartupSupported()) return {LoginStartupStatus::Unavailable, QStringLiteral("A user configuration directory is unavailable.")};
    const QString path = effectiveDesktopFile();
    return path.isEmpty() ? LoginStartupState{LoginStartupStatus::Disabled, {}} : desktopFileState(path);
#else
    return {LoginStartupStatus::Unavailable, QStringLiteral("Automatic sign-in launch is unavailable on this platform.")};
#endif
}

static bool nativeLoginStartupSetEnabled(bool enabled, QString *error) {
    if (error) error->clear();
    if (!loginStartupSessionAllowed())
        return fail(error, QStringLiteral("Login startup cannot be changed from an isolated development or test session."));
#ifdef Q_OS_WIN
    QSettings registry(QStringLiteral("HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"), QSettings::NativeFormat);
    if (enabled) {
        const QString command = windowsLoginStartupCommand(QCoreApplication::applicationFilePath(), error);
        if (command.isEmpty()) return false;
        registry.setValue(QStringLiteral("qcopilots-search"), command);
    } else registry.remove(QStringLiteral("qcopilots-search"));
    registry.sync();
    if (registry.status() != QSettings::NoError) return fail(error, QStringLiteral("Could not change the Windows sign-in setting."));
    return true;
#elif defined(Q_OS_MACOS)
    return macLoginStartupSetEnabled(enabled, error);
#elif defined(Q_OS_LINUX)
    const QString path = personalDesktopFile();
    if (path.isEmpty()) return fail(error, QStringLiteral("A user configuration directory is unavailable."));
    const QByteArray entry = desktopAutostartEntry(QCoreApplication::applicationFilePath(), enabled);
    if (entry.isEmpty()) return fail(error, QStringLiteral("This application path cannot be represented in a desktop startup entry."));
    if (!QDir().mkpath(QFileInfo(path).absolutePath()))
        return fail(error, QStringLiteral("Could not create the user autostart directory."));
    // Keep a Hidden=true override when disabled. Deleting the user's file could
    // accidentally re-enable a same-named system-wide entry.
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly) || file.write(entry) != entry.size() || !file.commit())
        return fail(error, QStringLiteral("Could not save the login startup entry: %1").arg(file.errorString()));
    return true;
#else
    Q_UNUSED(enabled)
    return fail(error, QStringLiteral("Automatic sign-in launch is unavailable on this platform."));
#endif
}

namespace {
QString loginTargetFile() {
    const QString root = RuntimePaths::defaultProfileDirectory();
    return root.isEmpty() ? QString{} : QDir(root).filePath(QStringLiteral("qcs-login-startup.json"));
}
}

LoginStartupState loginStartupState() {
    if (!loginStartupSessionAllowed())
        return {LoginStartupStatus::Unavailable, QStringLiteral("Login startup is unavailable for an isolated development or test session.")};
    auto state = nativeLoginStartupState();
    const QFileInfo pending(loginTargetFile() + QStringLiteral(".pending"));
    if (!loginTargetFile().isEmpty() && (pending.exists() || pending.isSymbolicLink()))
        return {LoginStartupStatus::Error, QStringLiteral("A login startup update did not finish. "
            "Use current directories at sign-in in Settings and save to repair the target.")};
    if (state.status != LoginStartupStatus::Enabled && state.status != LoginStartupStatus::ApprovalRequired)
        return state;
    LoginStartupTarget target;
    QString error;
    if (!readLoginStartupTarget(loginTargetFile(), &target, &error))
        return {LoginStartupStatus::Error, error};
    state.profileDir = target.profileDir;
    state.modelDir = target.modelDir;
    return state;
}

bool loginStartupSetEnabled(bool enabled, const QString &profile, const QString &models, QString *error) {
    if (error) error->clear();
    if (!loginStartupSessionAllowed())
        return fail(error, QStringLiteral("Login startup cannot be changed from an isolated development or test session."));
    if (!loginStartupSupported())
        return fail(error, QStringLiteral("Automatic sign-in launch is unavailable on this platform."));
    return updateLoginStartupTarget(loginTargetFile(), {profile, models}, enabled,
        [](bool value, QString *nativeError) {
            if (nativeLoginStartupSetEnabled(value, nativeError)) return true;
            // macOS registration can be accepted while requiring user approval.
            // Keep the selected target so approval later starts that profile.
            if (value && nativeLoginStartupState().status == LoginStartupStatus::ApprovalRequired) {
                if (nativeError) nativeError->clear();
                return true;
            }
            return false;
        }, error);
}

bool loginStartupSetEnabled(bool enabled, QString *error) {
    if (!loginStartupSessionAllowed())
        return fail(error, QStringLiteral("Login startup cannot be changed from an isolated development or test session."));
    const auto paths = RuntimePaths::resolve();
    if (!paths.validateProfileLayout(error)) return false;
    return loginStartupSetEnabled(enabled, paths.profileDir, paths.modelDir, error);
}

RuntimePaths resolveLoginStartupPaths(const QString &profile, const QString &models, bool loginLaunch) {
    if (!loginLaunch || !profile.isEmpty() || !models.isEmpty() || !loginStartupSessionAllowed())
        return RuntimePaths::resolve(profile, models);
    LoginStartupTarget target;
    QString error;
    if (!readLoginStartupTarget(loginTargetFile(), &target, &error)) {
        RuntimePaths paths;
        paths.resolutionError = error;
        return paths;
    }
    return RuntimePaths::resolve(target.profileDir, target.modelDir);
}

void prepareLoginStartupDetection() {
    if (QStandardPaths::isTestModeEnabled()) return;
#ifdef Q_OS_MACOS
    macPrepareLoginStartupDetection();
#endif
}

bool completeLoginStartupDetection(bool showForeground) {
#ifdef Q_OS_MACOS
    return macCompleteLoginStartupDetection(showForeground);
#else
    Q_UNUSED(showForeground)
    return true;
#endif
}

bool isLoginStartupLaunch() {
    if (QCoreApplication::arguments().contains(QStringLiteral("--login-startup"))) return true;
#ifdef Q_OS_MACOS
    return macIsLoginStartupLaunch();
#else
    return false;
#endif
}

} // namespace qcopilots_search
