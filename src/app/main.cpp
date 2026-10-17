#include "app_backend.h"
#include "gui_instance.h"
#include "main_window.h"
#include "login_startup.h"
#include "core/runtime_paths.h"
#include "core/app_settings.h"
#include <QApplication>
#include <QCommandLineParser>
#include <QLocale>
#include <QMessageBox>
#include <QPushButton>
#include <QSystemTrayIcon>
#include <QTimer>
#include <cstdio>
#include <memory>
#ifdef Q_OS_WIN
#include <qt_windows.h>
#endif

namespace {
void writeCommandLineOutput(const QString &text, bool error = false) {
#ifdef Q_OS_WIN
    const DWORD stream = error ? STD_ERROR_HANDLE : STD_OUTPUT_HANDLE;
    HANDLE handle = GetStdHandle(stream);
    if (!handle || handle == INVALID_HANDLE_VALUE) {
        AttachConsole(ATTACH_PARENT_PROCESS);
        handle = GetStdHandle(stream);
    }
    if (handle && handle != INVALID_HANDLE_VALUE) {
        DWORD mode = 0, written = 0;
        if (GetConsoleMode(handle, &mode)) WriteConsoleW(handle, text.utf16(), DWORD(text.size()), &written, nullptr);
        else { const auto bytes = text.toUtf8(); WriteFile(handle, bytes.constData(), DWORD(bytes.size()), &written, nullptr); }
        return;
    }
#endif
    const auto bytes = text.toUtf8();
    std::fwrite(bytes.constData(), 1, size_t(bytes.size()), error ? stderr : stdout);
    std::fflush(error ? stderr : stdout);
}
}

int main(int argc, char **argv) {
    qcopilots_search::prepareLoginStartupDetection();
    QApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-gui"));
    QCoreApplication::setOrganizationName(QStringLiteral("QCopilots"));
    QCoreApplication::setApplicationVersion(QStringLiteral(QCOPILOTS_SEARCH_VERSION));
    QApplication::setApplicationDisplayName(QStringLiteral("QCopilots Search"));
    QGuiApplication::setDesktopFileName(QStringLiteral("com.qcopilots.qcopilots-search"));
    QApplication::setWindowIcon(qcopilots_search::MainWindow::applicationIcon());
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("QCopilots Search — local multimodal file search"));
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addOption({QStringLiteral("profile-dir"), QStringLiteral("Data base directory. Configuration and index use profiles/v4 beneath it."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("model-dir"), QStringLiteral("Model directory."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("no-tray"), QStringLiteral("Disable tray and global shortcut integration.")});
    parser.addOption({QStringLiteral("no-onboarding"), QStringLiteral("Do not open first-run setup automatically.")});
    parser.addOption({QStringLiteral("background"), QStringLiteral("Start in the tray when a system tray is available.")});
    QCommandLineOption loginStartupOption(QStringLiteral("login-startup"), QStringLiteral("Launch the registered sign-in target."));
    loginStartupOption.setFlags(QCommandLineOption::HiddenFromHelp);
    parser.addOption(loginStartupOption);
    // QCommandLineParser::process uses native message boxes for Windows GUI
    // programs. Explicit output keeps CLI/package probes deterministic.
    if (!parser.parse(application.arguments())) {
        writeCommandLineOutput(parser.errorText() + QLatin1Char('\n'), true);
        return 2;
    }
    if (parser.isSet("help") || parser.isSet("help-all")) {
        writeCommandLineOutput(parser.helpText());
        return 0;
    }
    if (parser.isSet("version")) {
        writeCommandLineOutput(QCoreApplication::applicationName() + QLatin1Char(' ') + QCoreApplication::applicationVersion() + QLatin1Char('\n'));
        return 0;
    }
    // AppKit delivers the login-item source during its first event-loop turn.
    // Defer every profile lookup, lock and service until that source is known.
    application.setQuitOnLastWindowClosed(false);
    std::unique_ptr<qcopilots_search::GuiInstance> instance;
    std::unique_ptr<qcopilots_search::ServiceBackend> backend;
    std::unique_ptr<qcopilots_search::MainWindow> window;
    auto *startup = new QTimer(&application);
    startup->setInterval(10);
    QObject::connect(startup, &QTimer::timeout, &application, [&] {
        const bool loginLaunch = qcopilots_search::isLoginStartupLaunch();
        const bool background = parser.isSet("background") || loginLaunch;
        const bool showForeground = !background || parser.isSet("no-tray") || !QSystemTrayIcon::isSystemTrayAvailable();
        if (!qcopilots_search::completeLoginStartupDetection(showForeground)) return;
        startup->stop();
        startup->deleteLater();
        const auto paths = qcopilots_search::resolveLoginStartupPaths(
            parser.value("profile-dir"), parser.value("model-dir"), loginLaunch);
        QString error;
        qcopilots_search::AppSettings initialSettings;
        if (!paths.validateProfileLayout(&error) || !initialSettings.load(paths.configFile, &error) || !paths.ensure(&error)) {
            writeCommandLineOutput(error + QLatin1Char('\n'), true);
            QMessageBox prompt(QMessageBox::Critical, QStringLiteral("QCopilots Search"), error, QMessageBox::Ok);
            prompt.button(QMessageBox::Ok)->setText(QLocale::system().language() == QLocale::Chinese ? QStringLiteral("确定") : QStringLiteral("OK"));
            prompt.exec();
            application.exit(2);
            return;
        }
        instance = std::make_unique<qcopilots_search::GuiInstance>(paths);
        const auto instanceResult = instance->start(&error);
        if (instanceResult == qcopilots_search::GuiInstance::Result::ActivatedExisting) {
            application.exit(0);
            return;
        }
        if (instanceResult == qcopilots_search::GuiInstance::Result::Failed) {
            writeCommandLineOutput(error + QLatin1Char('\n'), true);
            QMessageBox::critical(nullptr, QStringLiteral("QCopilots Search"), error);
            application.exit(2);
            return;
        }
        backend = std::make_unique<qcopilots_search::ServiceBackend>(paths);
        window = std::make_unique<qcopilots_search::MainWindow>(paths, backend.get(), !parser.isSet("no-tray"), !parser.isSet("no-onboarding"));
        QObject::connect(instance.get(), &qcopilots_search::GuiInstance::showRequested,
                         window.get(), &qcopilots_search::MainWindow::showSearch);
        if (showForeground) window->show();
        window->start();
    });
    startup->start();
    return application.exec();
}
