#include "app_backend.h"
#include "main_window.h"
#include "login_startup.h"
#include "core/runtime_paths.h"
#include <QApplication>
#include <QCommandLineParser>
#include <QLocale>
#include <QMessageBox>
#include <QPushButton>
#include <QSystemTrayIcon>
#include <QTimer>
#include <cstdio>
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
    parser.addOption({QStringLiteral("profile-dir"), QStringLiteral("Isolated configuration and index directory."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("model-dir"), QStringLiteral("Model directory."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("no-tray"), QStringLiteral("Disable tray and global shortcut integration.")});
    parser.addOption({QStringLiteral("no-onboarding"), QStringLiteral("Do not open first-run setup automatically.")});
    parser.addOption({QStringLiteral("background"), QStringLiteral("Start in the tray when a system tray is available.")});
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
    const auto paths = qcopilots_search::RuntimePaths::resolve(parser.value("profile-dir"), parser.value("model-dir"));
    QString error;
    if (!paths.ensure(&error)) {
        std::fprintf(stderr, "%s\n", error.toUtf8().constData());
        QMessageBox prompt(QMessageBox::Critical, QStringLiteral("QCopilots Search"), error, QMessageBox::Ok);
        prompt.button(QMessageBox::Ok)->setText(QLocale::system().language() == QLocale::Chinese ? QStringLiteral("确定") : QStringLiteral("OK"));
        prompt.exec();
        return 2;
    }
    qcopilots_search::ServiceBackend backend(paths);
    qcopilots_search::MainWindow window(paths, &backend, !parser.isSet("no-tray"), !parser.isSet("no-onboarding"));
    application.setQuitOnLastWindowClosed(false);
    const bool explicitBackground = parser.isSet("background");
    const bool disableTray = parser.isSet("no-tray");
    auto *startup = new QTimer(&window);
    startup->setInterval(10);
    QObject::connect(startup, &QTimer::timeout, &window, [&window, startup, explicitBackground, disableTray] {
        const bool background = explicitBackground || qcopilots_search::isLoginStartupLaunch();
        const bool showForeground = !background || disableTray || !QSystemTrayIcon::isSystemTrayAvailable();
        if (!qcopilots_search::completeLoginStartupDetection(showForeground)) return;
        startup->stop();
        startup->deleteLater();
        if (showForeground) window.show();
        window.start();
    });
    startup->start();
    return application.exec();
}
