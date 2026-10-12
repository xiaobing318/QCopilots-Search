#include "app/app_backend.h"
#include "app/main_window.h"
#include "core/app_settings.h"
#include "service/service_client.h"
#include "support/test_environment.h"
#include <QFileInfo>
#include <QComboBox>
#include <QDir>
#include <QEventLoop>
#include <QLabel>
#include <QListWidget>
#include <QLocalSocket>
#include <QPainter>
#include <QPdfWriter>
#include <QProcess>
#include <QPushButton>
#include <QSignalSpy>
#include <QStandardPaths>
#include <QtTest>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

// Own only the process created by this test. Cleanup never uses names or a
// production profile and therefore cannot terminate another user's service.
struct ScopedService {
    explicit ScopedService(RuntimePaths paths) : paths(std::move(paths)) {}
    RuntimePaths paths;
    QProcess process;
    ~ScopedService() {
        if (process.state() == QProcess::NotRunning) return;
        ServiceClient client(paths);
        client.setAutoStart(false);
        QEventLoop loop;
        QTimer deadline;
        deadline.setSingleShot(true);
        QObject::connect(&deadline, &QTimer::timeout, &loop, &QEventLoop::quit);
        QObject::connect(&client, &ServiceClient::response, &loop, &QEventLoop::quit);
        QObject::connect(&client, &ServiceClient::failed, &loop, &QEventLoop::quit);
        client.request("shutdown", {}, 2000);
        deadline.start(2500);
        loop.exec();
        if (!process.waitForFinished(3000)) { process.terminate(); if (!process.waitForFinished(3000)) { process.kill(); process.waitForFinished(3000); } }
    }
};
class ServiceGuiTests final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void actualServiceIndexesAndSearchesInIsolatedProfile() {
        TestEnvironment env("gui-service-中文");
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        QVERIFY(env.writeFile("selected folder/contract 中文.txt", QString::fromUtf8("orchidcontract identifies this document. 中文项目合同。sharedcategorycanary also identifies this text file.\n").toUtf8()));
        const QString pdfPath = env.path("selected folder/category 中文.pdf");
        {
            QPdfWriter writer(pdfPath);
            QPainter painter(&writer);
            QVERIFY(painter.isActive());
            painter.drawText(100, 200, "sharedcategorycanary identifies this portable document with enough text for native extraction.");
        }
        const auto paths = env.runtimePaths();
        QString error;
        QVERIFY2(paths.ensure(&error), qPrintable(error));
        AppSettings settings;
        settings.roots << env.path("selected folder");
        settings.language = "zh_CN";
        settings.checkUpdates = false;
        settings.minimizeToTray = false;
        QVERIFY2(settings.save(paths.configFile, &error), qPrintable(error));
        ScopedService service(paths);
        service.process.setProgram(QStringLiteral(QCOPILOTS_SEARCH_SERVICE_EXECUTABLE));
        service.process.setArguments({"--profile-dir", paths.profileDir, "--model-dir", paths.modelDir});
        service.process.setWorkingDirectory(env.root());
        service.process.start();
        QVERIFY2(service.process.waitForStarted(10000), qPrintable(service.process.errorString()));
        QTRY_VERIFY_WITH_TIMEOUT(QFileInfo::exists(QDir(paths.profileDir).filePath("qcopilots-search-ipc-token")), 10000);
        ServiceClient probe(paths);
        probe.setAutoStart(false);
        QTRY_VERIFY_WITH_TIMEOUT(([&] { if (!probe.isConnected()) probe.connectToService(); return probe.isConnected(); })(), 10000);
        QSignalSpy probeResponses(&probe, &ServiceClient::response);
        const auto probeId = probe.request("status");
        QTRY_VERIFY_WITH_TIMEOUT(probeResponses.count() == 1, 10000);
        QCOMPARE(probeResponses.first().at(0).toString(), probeId);
        QVERIFY(probeResponses.first().at(1).toJsonObject().contains("progress"));
        probe.disconnectFromService();
        QVERIFY(service.process.state() == QProcess::Running);
        ServiceBackend backend(paths);
        backend.setAutoStart(false);
        QSignalSpy ready(&backend, &AppBackend::connected);
        MainWindow window(paths, &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY_WITH_TIMEOUT(ready.count() == 1, 15000);
        backend.request("index.start");
        auto *input = window.findChild<SearchEdit *>("searchInput");
        input->setText("orchidcontract");
        auto *results = window.findChild<QListWidget *>("searchResults");
        QTRY_VERIFY_WITH_TIMEOUT(results->count() > 0, 90000);
        QVERIFY(results->item(0)->data(Qt::UserRole).toString().endsWith(QString::fromUtf8("contract 中文.txt")));
        QVERIFY(window.findChild<QLabel *>("searchSummary")->text().contains(QString::fromUtf8("关键词")));
        auto *document = window.findChild<QPushButton *>("loadDocumentPreviewButton");
        QVERIFY(document->isEnabled());
        document->click();
        QTRY_COMPARE_WITH_TIMEOUT(document->text(), QString::fromUtf8("正文已读取"), 90000);
        // Exercise the real GUI -> IPC -> worker -> database kind contract.
        // The user-facing Documents label must select stored kind=text.
        input->setText("sharedcategorycanary");
        QTRY_COMPARE_WITH_TIMEOUT(results->count(), 2, 90000);
        auto *kind = window.findChild<QComboBox *>("kindFilter"); QVERIFY(kind);
        kind->setCurrentIndex(1);
        QTRY_COMPARE_WITH_TIMEOUT(results->count(), 1, 10000);
        QVERIFY(results->item(0)->data(Qt::UserRole).toString().endsWith(QString::fromUtf8("contract 中文.txt")));
        kind->setCurrentIndex(kind->findData("pdf"));
        QTRY_VERIFY_WITH_TIMEOUT(results->count() == 1 && results->item(0)->data(Qt::UserRole).toString() == pdfPath, 10000);
        kind->setCurrentIndex(kind->findData("image"));
        QTRY_COMPARE_WITH_TIMEOUT(results->count(), 0, 10000);
        kind->setCurrentIndex(0);
        QTRY_COMPARE_WITH_TIMEOUT(results->count(), 2, 10000);
        backend.request("shutdown");
        QTRY_VERIFY_WITH_TIMEOUT(service.process.state() == QProcess::NotRunning, 15000);
        QCOMPARE(service.process.exitCode(), 0);
        QLocalSocket closedEndpoint;
        closedEndpoint.connectToServer(paths.ipcName);
        QVERIFY(!closedEndpoint.waitForConnected(300));
        QVERIFY(!QFileInfo::exists(paths.lockFile));
    }
};
QTEST_MAIN(ServiceGuiTests)
#include "service_gui_tests.moc"
