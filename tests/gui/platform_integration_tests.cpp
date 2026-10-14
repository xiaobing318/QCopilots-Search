#include "app/platform_external_open.h"
#include "app/platform_integration.h"
#include "support/test_environment.h"

#include <QDir>
#include <QApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcess>
#include <QSaveFile>
#include <QStandardPaths>
#include <QUrl>
#include <QtTest>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

class PlatformIntegrationTests final : public QObject {
    Q_OBJECT
private slots:
    void cleanup() {
        const QString error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }
    void templateExpansionDoesNotReinterpretInsertedPath() {
        SearchHit hit;
        hit.path = QStringLiteral("/中文 空格/{line}/{page}/percent%20{file}.pdf");
        hit.lineStart = 19;
        hit.page = 8;
        hit.cell = 3;
        hit.startMs = 12345;
        QCOMPARE(expandOpenArgument(QStringLiteral("{file}:{line}:{page}:{cell}:{startMs}:{startSeconds}"), hit),
                 hit.path + QStringLiteral(":19:8:3:12345:12.345"));
        QCOMPARE(expandOpenArgument(QStringLiteral("{unknown}"), hit), QStringLiteral("{unknown}"));
    }
    void editorArgumentsRemainSeparate() {
        SearchHit hit;
        hit.path = QStringLiteral("/space folder/{line} example.cpp");
        hit.lineStart = 21;
        AppSettings settings;
        settings.editorCommand = QStringLiteral("code --reuse-window");
        const auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QCOMPARE(command->program, QStringLiteral("code"));
        QCOMPARE(command->arguments, (QStringList{QStringLiteral("--reuse-window"), QStringLiteral("--goto"), hit.path + QStringLiteral(":21")}));
    }
    void customApplicationUsesExactArgumentsAndAppendsMissingFile() {
        AppSettings settings;
        settings.pdfOpener.preset = QStringLiteral("custom");
        settings.pdfOpener.program = QStringLiteral("viewer");
        settings.pdfOpener.arguments = {QStringLiteral("--page={page}"), QStringLiteral("literal text && not a shell")};
        SearchHit hit;
        hit.kind = QStringLiteral("pdf");
        hit.path = QStringLiteral("/file {page}.pdf");
        hit.page = 7;
        auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QCOMPARE(command->arguments, (QStringList{QStringLiteral("--page=7"), QStringLiteral("literal text && not a shell"), hit.path}));
        settings.pdfOpener.arguments = {QStringLiteral("{file}"), QStringLiteral("{page}")};
        command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QCOMPARE(command->arguments, (QStringList{hit.path, QStringLiteral("7")}));
        settings.pdfOpener.program.clear();
        QString error;
        QVERIFY(!buildOpenCommand(hit, settings, &error));
        QVERIFY(!error.isEmpty());
    }
    void mediaStartUsesExactMilliseconds() {
        SearchHit hit;
        hit.kind = QStringLiteral("audio");
        hit.path = QStringLiteral("/music with space.wav");
        hit.startMs = 9876;
        AppSettings settings;
        settings.mediaOpener.preset = QStringLiteral("mpv");
        const auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QCOMPARE(command->program, QStringLiteral("mpv"));
        QCOMPARE(command->arguments, (QStringList{QStringLiteral("--start=9.876"), QStringLiteral("--"), hit.path}));
    }
    void platformPdfPresetUsesPhysicalPage() {
        SearchHit hit;
        hit.kind = QStringLiteral("pdf");
        hit.path = QStringLiteral("/中文 空格/report#section.pdf");
        hit.page = 11;
        AppSettings settings;
#ifdef Q_OS_WIN
        settings.pdfOpener.preset = QStringLiteral("sumatra");
        const auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QCOMPARE(command->arguments, (QStringList{QStringLiteral("-reuse-instance"), QStringLiteral("-page"), QStringLiteral("11"), hit.path}));
#elif defined(Q_OS_LINUX)
        settings.pdfOpener.preset = QStringLiteral("okular");
        const auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QCOMPARE(command->arguments.mid(0, 3), (QStringList{QStringLiteral("--unique"), QStringLiteral("--page"), QStringLiteral("11")}));
        QCOMPARE(QUrl(command->arguments.last()).toLocalFile(), hit.path);
#elif defined(Q_OS_MACOS)
        settings.pdfOpener.preset = QStringLiteral("skim");
        const auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QVERIFY(command->waitForLauncher);
        const QUrl link(command->arguments.last());
        QCOMPARE(link.scheme(), QStringLiteral("skim"));
        QCOMPARE(link.path(), hit.path);
        QCOMPARE(link.fragment(), QStringLiteral("page=11"));
#endif
    }
    void ordinaryOpenExplicitlyReportsLocationLimit() {
        AppSettings settings;
        SearchHit hit;
        hit.kind = QStringLiteral("pdf");
        hit.page = 4;
        const auto command = buildOpenCommand(hit, settings);
        QVERIFY(command);
        QVERIFY(command->systemOpen);
        QVERIFY(command->notice.contains(QStringLiteral("not guaranteed")));
    }
    void missingApplicationNeverSilentlyFallsBack() {
        TestEnvironment environment(QStringLiteral("platform-open"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("中文 file.pdf"), "example"));
        SearchHit hit;
        hit.path = environment.path(QStringLiteral("中文 file.pdf"));
        hit.kind = QStringLiteral("pdf");
        AppSettings settings;
        settings.pdfOpener.preset = QStringLiteral("custom");
        settings.pdfOpener.program = environment.path(QStringLiteral("missing-viewer"));
        QString error;
        QVERIFY(!PlatformIntegration::openHit(hit, settings, &error));
        QVERIFY(error.contains(QStringLiteral("not found")));
    }
    void actualProcessReceivesLiteralFileAndLocationArguments() {
        TestEnvironment environment(QStringLiteral("platform-argv"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString filename = QStringLiteral("中文 space & percent% {line}.pdf");
        QVERIFY(environment.writeFile(filename, "example"));
        SearchHit hit;
        hit.path = environment.path(filename);
        hit.kind = QStringLiteral("pdf");
        hit.page = 17;
        AppSettings settings;
        settings.pdfOpener.preset = QStringLiteral("custom");
        settings.pdfOpener.program = QCoreApplication::applicationFilePath();
        const QString capturePath = environment.path(QStringLiteral("captured.json"));
        settings.pdfOpener.arguments = {QStringLiteral("--qcs-platform-capture"), capturePath,
                                        QStringLiteral("--page={page}"), QStringLiteral("{file}")};
        QString error;
        const auto command = buildOpenCommand(hit, settings, &error);
        QVERIFY2(command.has_value(), qPrintable(error));
        QProcess process;
        process.start(command->program, command->arguments);
        const bool finished = process.waitForFinished(5000);
        if (!finished) { process.kill(); process.waitForFinished(1000); }
        QVERIFY2(finished, qPrintable(process.errorString()));
        QCOMPARE(process.exitStatus(), QProcess::NormalExit);
        QCOMPARE(process.exitCode(), 0);
        QVERIFY(QFileInfo::exists(capturePath));
        QFile capture(capturePath);
        QVERIFY(capture.open(QIODevice::ReadOnly));
        const auto arguments = QJsonDocument::fromJson(capture.readAll()).array();
        QCOMPARE(arguments, (QJsonArray{QStringLiteral("--page=17"), hit.path}));
    }
    void isolatedProfilesCannotChangeLoginStartup() {
        TestEnvironment environment(QStringLiteral("platform-startup"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto paths = environment.runtimePaths();
        QVERIFY(!PlatformIntegration::canSetStartAtLogin(paths.profileDir, paths.modelDir));
        QString error;
        QVERIFY(!PlatformIntegration::setStartAtLogin(true, paths.profileDir, paths.modelDir, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!PlatformIntegration::setStartAtLogin(false, paths.profileDir, paths.modelDir, &error));
    }
#ifdef Q_OS_WIN
    void windowsBatchWrappersCannotReinterpretFileArguments() {
        TestEnvironment environment(QStringLiteral("platform-batch"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("bin/code.cmd"), "@echo off\r\n"));
        QVERIFY(environment.writeFile(QStringLiteral("Code.exe"), "test-only executable path"));
        QString error;
        QCOMPARE(resolveExternalExecutable(environment.path(QStringLiteral("bin/code.cmd")), &error),
                 environment.path(QStringLiteral("Code.exe")));
        QVERIFY(error.isEmpty());
        QVERIFY(environment.writeFile(QStringLiteral("viewer.cmd"), "@echo off\r\n"));
        QVERIFY(resolveExternalExecutable(environment.path(QStringLiteral("viewer.cmd")), &error).isEmpty());
        QVERIFY(error.contains(QStringLiteral(".exe")));
    }
#endif
};

int main(int argc, char **argv) {
    if (argc >= 3 && QByteArray(argv[1]) == "--qcs-platform-capture") {
        QCoreApplication application(argc, argv);
        const QStringList arguments = application.arguments();
        QSaveFile capture(arguments.at(2));
        const QByteArray content = QJsonDocument(QJsonArray::fromStringList(arguments.mid(3))).toJson(QJsonDocument::Compact);
        if (!capture.open(QIODevice::WriteOnly) || capture.write(content) != content.size() || !capture.commit()) return 2;
        return 0;
    }
    QApplication application(argc, argv);
    PlatformIntegrationTests tests;
    return QTest::qExec(&tests, argc, argv);
}
#include "platform_integration_tests.moc"
