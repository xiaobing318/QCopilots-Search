#include "app/app_backend.h"
#include "app/main_window.h"
#include "app/model_dialog.h"
#include "app/platform_integration.h"
#include "app/preview_panel.h"
#include "app/settings_dialog.h"
#include "core/app_settings.h"
#include "support/test_environment.h"
#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileInfo>
#include <QImage>
#include <QInputMethodEvent>
#include <QJsonArray>
#include <QLabel>
#include <QListWidget>
#include <QMediaPlayer>
#include <QPainter>
#include <QPdfWriter>
#include <QProcess>
#include <QPushButton>
#include <QScrollBar>
#include <QSignalSpy>
#include <QSpinBox>
#include <QStandardPaths>
#include <QTextBrowser>
#include <QtTest>
#include <QtEndian>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

class FakeBackend final : public AppBackend {
    Q_OBJECT
public:
    struct Request { QString id, method; QJsonObject params; };
    QList<Request> requests;
    QStringList cancelled;
    AppSettings settings;
    QList<SearchHit> hits;
    bool autoSearch = true;
    int next = 1;
    FakeBackend() { settings.language = "en"; settings.checkUpdates = false; settings.minimizeToTray = false; }
    void start() override { QTimer::singleShot(0, this, [this] { emit connected(); }); }
    QString request(const QString &method, const QJsonObject &params = {}, int = 30000) override {
        const QString id = QString::number(next++);
        requests.append({id, method, params});
        QTimer::singleShot(0, this, [this, id, method, params] {
            if (method == "settings.get") emit response(id, settings.toJson());
            else if (method == "settings.set") {
                auto updated = settings.toJson();
                for (auto it = params.begin(); it != params.end(); ++it) updated.insert(it.key(), it.value());
                QString error; AppSettings::fromJson(updated, &settings, &error); emit response(id, settings.toJson());
            }
            else if (method == "status") emit response(id, {{"modelAvailable", false}, {"progress", QJsonObject{{"state", "idle"}, {"total", 2}, {"completed", 2}}}});
            else if (method == "search" && autoSearch) {
                QJsonArray values;
                for (const auto &hit : hits) values.append(hit.toJson());
                emit response(id, {{"hits", values}, {"semantic", false}});
            } else if (method == "index.pause") { settings.indexingPaused = true; emit response(id, {{"state", "paused"}}); }
            else if (method == "index.resume") { settings.indexingPaused = false; emit response(id, {{"state", "indexing"}}); }
            else if (method.startsWith("index.")) emit response(id, {{"state", "indexing"}});
        });
        return id;
    }
    void cancel(const QString &id) override { cancelled << id; }
    int count(const QString &method) const { int n = 0; for (const auto &request : requests) if (request.method == method) ++n; return n; }
    Request last(const QString &method) const { for (auto it = requests.crbegin(); it != requests.crend(); ++it) if (it->method == method) return *it; return {}; }
};

class GuiTests final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void keywordSearchAndTextPreview() {
        TestEnvironment env("gui-search-中文");
        QVERIFY2(env.isValid(), qPrintable(env.error()));
        QVERIFY(env.writeFile("sources/中文 contract.txt", "Local contract details"));
        FakeBackend backend;
        backend.settings.roots << env.path("sources");
        SearchHit hit;
        hit.path = env.path("sources/中文 contract.txt"); hit.title = "Contract"; hit.text = "Local contract details"; hit.kind = "document"; hit.matchSource = "keyword";
        backend.hits << hit;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("settings.get") == 1);
        auto *query = window.findChild<SearchEdit *>("searchInput");
        query->setText("contract");
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString("contract"));
        auto *results = window.findChild<QListWidget *>("searchResults");
        QTRY_COMPARE(results->count(), 1);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), hit.text);
        QVERIFY(window.findChild<QPushButton *>("openResultButton")->isEnabled());
        QVERIFY(backend.last("search").params.value("semantic").toBool());
    }
    void filtersAndSeparateCodeSearch() {
        TestEnvironment env("gui-filters"); QVERIFY(env.isValid());
        FakeBackend backend; backend.settings.includeCode = true; backend.settings.roots << env.root();
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        auto *code = window.findChild<QCheckBox *>("codeSearchCheck");
        QTRY_VERIFY(code->isEnabled());
        auto *kind = window.findChild<QComboBox *>("kindFilter");
        kind->setCurrentIndex(kind->findData("pdf"));
        auto *root = window.findChild<QComboBox *>("rootFilter");
        QTRY_COMPARE(root->count(), 2);
        root->setCurrentIndex(1);
        QTRY_COMPARE(backend.last("search").params.value("kind").toString(), QString("pdf"));
        QCOMPARE(backend.last("search").params.value("root").toString(), env.root());
        code->setChecked(true);
        QTRY_VERIFY(backend.last("search").params.value("code").toBool());
        QVERIFY(!kind->isEnabled());
        QCOMPARE(backend.last("search").params.value("kind").toString(), QString());
    }
    void inputMethodPreeditDoesNotSearch() {
        TestEnvironment env("gui-ime"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("search") > 0);
        auto *query = window.findChild<SearchEdit *>("searchInput");
        query->setFocus();
        const int before = backend.count("search");
        QInputMethodEvent preedit(QString::fromUtf8("中文"), {});
        QApplication::sendEvent(query, &preedit);
        QVERIFY(query->isComposing());
        QTest::qWait(350);
        QCOMPARE(backend.count("search"), before);
        QInputMethodEvent commit;
        commit.setCommitString(QString::fromUtf8("中文"));
        QApplication::sendEvent(query, &commit);
        QVERIFY(!query->isComposing());
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString::fromUtf8("中文"));
    }
    void staleRepliesNeverReplaceCurrentResults() {
        TestEnvironment env("gui-cancel"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoSearch = false;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        auto *query = window.findChild<SearchEdit *>("searchInput");
        query->setText("old");
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString("old"));
        const QString old = backend.last("search").id;
        query->setText("new");
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString("new"));
        const QString current = backend.last("search").id;
        SearchHit hit; hit.path = env.path("new.txt"); hit.title = "new match"; hit.text = "current";
        emit backend.response(current, {{"hits", QJsonArray{hit.toJson()}}});
        hit.title = "stale match";
        emit backend.response(old, {{"hits", QJsonArray{hit.toJson()}}});
        QVERIFY(backend.cancelled.contains(old));
        auto *results = window.findChild<QListWidget *>("searchResults");
        QCOMPARE(results->count(), 1);
        QVERIFY(results->item(0)->text().contains("new match"));
    }
    void changedSearchContextSelectsFirstResult_data() {
        QTest::addColumn<QString>("change");
        for (const auto *change : {"query", "clear-query", "kind", "root", "code"}) QTest::newRow(change) << QString::fromLatin1(change);
    }
    void changedSearchContextSelectsFirstResult() {
        QFETCH(QString, change);
        TestEnvironment env("gui-new-search-selection"); QVERIFY(env.isValid());
        FakeBackend backend; backend.settings.includeCode = true; backend.settings.roots << env.root();
        for (int i = 0; i < 30; ++i) {
            const QString name = QStringLiteral("result-%1.txt").arg(i);
            const QString text = QStringLiteral("Distinct passage %1").arg(i);
            QVERIFY(env.writeFile(name, text.toUtf8()));
            SearchHit hit; hit.path = env.path(name); hit.title = name; hit.text = text; hit.kind = "document"; hit.segmentId = i + 1;
            backend.hits.append(hit);
        }
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.resize(850, 540); window.show(); window.start();
        auto *query = window.findChild<SearchEdit *>("searchInput");
        query->setText("initial query");
        auto *results = window.findChild<QListWidget *>("searchResults");
        QTRY_COMPARE(results->count(), 30);
        results->setCurrentRow(20);
        results->scrollToItem(results->currentItem(), QAbstractItemView::PositionAtTop);
        QTRY_VERIFY(results->verticalScrollBar()->value() > 0);
        const QString previousPath = backend.hits.at(20).path;
        QCOMPARE(window.findChild<PreviewPanel *>()->currentHit().path, previousPath);
        backend.hits.move(29, 0);
        const auto expected = backend.hits.first();
        const int before = backend.count("search");
        if (change == "query") query->setText(QString::fromUtf8("兰花港口"));
        else if (change == "clear-query") query->clear();
        else if (change == "kind") window.findChild<QComboBox *>("kindFilter")->setCurrentIndex(1);
        else if (change == "root") window.findChild<QComboBox *>("rootFilter")->setCurrentIndex(1);
        else window.findChild<QCheckBox *>("codeSearchCheck")->setChecked(true);
        QTRY_VERIFY(backend.count("search") > before);
        QTRY_COMPARE(results->item(0)->data(Qt::UserRole).toString(), expected.path);
        QCOMPARE(results->currentRow(), 0);
        QCOMPARE(results->verticalScrollBar()->value(), results->verticalScrollBar()->minimum());
        QCOMPARE(window.findChild<PreviewPanel *>()->currentHit().path, expected.path);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), expected.text);
        QVERIFY(results->item(21)->data(Qt::UserRole).toString() == previousPath);
    }
    void sameSearchRefreshPreservesSelectedPreview() {
        TestEnvironment env("gui-refresh-selection"); QVERIFY(env.isValid());
        FakeBackend backend;
        for (int i = 0; i < 30; ++i) {
            const QString name = QStringLiteral("result-%1.txt").arg(i);
            const QString text = QStringLiteral("Distinct passage %1").arg(i);
            QVERIFY(env.writeFile(name, text.toUtf8()));
            SearchHit hit; hit.path = env.path(name); hit.title = name; hit.text = text; hit.kind = "document"; hit.segmentId = i + 1;
            backend.hits.append(hit);
        }
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.resize(850, 540); window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("unchanged query");
        auto *results = window.findChild<QListWidget *>("searchResults");
        QTRY_COMPARE(results->count(), 30);
        results->setCurrentRow(20);
        auto *preview = window.findChild<PreviewPanel *>();
        const auto selected = backend.hits.at(20);
        const QString expanded = selected.text + "\nAlready loaded full document body";
        preview->setDocumentPreview({{"text", expanded}, {"partial", false}, {"next", 0}}, false);
        backend.hits.move(20, 25);
        const int before = backend.count("search");
        emit backend.eventReceived("indexChanged", {});
        QTRY_VERIFY(backend.count("search") > before);
        QTRY_COMPARE(results->currentRow(), 25);
        QCOMPARE(results->currentItem()->data(Qt::UserRole).toString(), selected.path);
        QCOMPARE(preview->currentHit().path, selected.path);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), expanded);
        QVERIFY(results->verticalScrollBar()->value() > 0);
    }
    void chineseDialogButtonsUseApplicationLanguage() {
        TestEnvironment env("gui-chinese-buttons"); QVERIFY(env.isValid());
        AppSettings settings; settings.language = "zh_CN";
        SettingsDialog preferences(settings, false, false);
        auto *preferenceButtons = preferences.findChild<QDialogButtonBox *>(); QVERIFY(preferenceButtons);
        QCOMPARE(preferenceButtons->button(QDialogButtonBox::Save)->text(), QString::fromUtf8("保存"));
        QCOMPARE(preferenceButtons->button(QDialogButtonBox::Cancel)->text(), QString::fromUtf8("取消"));
        ModelDialog models(env.runtimePaths().modelDir, "zh_CN");
        auto *close = models.findChild<QPushButton *>("closeModelButton"); QVERIFY(close);
        QCOMPARE(close->text(), QString::fromUtf8("关闭"));
        QTRY_VERIFY(models.findChild<QPushButton *>("importModelButton")->isEnabled());
    }
    void semanticFailureShowsKeywordFallbackWarning() {
        TestEnvironment env("gui-semantic-warning"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoSearch = false;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("search") > 0);
        emit backend.response(backend.last("search").id, {{"hits", QJsonArray()}, {"semantic", false}, {"warning", "Semantic encoder failed. Keyword results remain available."}});
        auto *warning = window.findChild<QLabel *>("applicationError");
        QVERIFY(warning->isVisible());
        QVERIFY(warning->text().contains("Semantic encoder failed"));
        const int searches = backend.count("search");
        window.findChild<SearchEdit *>("searchInput")->setText("retry");
        QTRY_VERIFY(backend.count("search") > searches);
        emit backend.response(backend.last("search").id, {{"hits", QJsonArray()}, {"semantic", true}});
        QVERIFY(!warning->isVisible());
    }
    void settingsPersistThroughServiceAndSwitchLanguage() {
        TestEnvironment env("gui-settings"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("settings.get") == 1);
        QTest::mouseClick(window.findChild<QPushButton *>("settingsButton"), Qt::LeftButton);
        auto *dialog = window.findChild<SettingsDialog *>(); QVERIFY(dialog);
        dialog->findChild<QListWidget *>("rootFolders")->addItem(env.root());
        dialog->findChild<QComboBox *>("languageCombo")->setCurrentIndex(2);
        dialog->findChild<QCheckBox *>("includeCodeCheck")->setChecked(true);
        QVERIFY(!dialog->findChild<QCheckBox *>("startAtLoginCheck")->isEnabled());
        QTest::mouseClick(dialog->findChild<QPushButton *>("saveSettingsButton"), Qt::LeftButton);
        QTRY_COMPARE(backend.settings.language, QString("zh_CN"));
        QCOMPARE(backend.settings.roots, QStringList{env.root()});
        QVERIFY(backend.settings.includeCode);
        QTRY_COMPARE(window.findChild<QPushButton *>("settingsButton")->text(), QString::fromUtf8("设置"));
        QCOMPARE(backend.count("index.resume"), 0);
        QVERIFY(!backend.last("settings.set").params.contains("indexingPaused"));
    }
    void firstRunCanBeDeferred() {
        TestEnvironment env("gui-onboarding"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, true);
        window.show(); window.start();
        QTRY_VERIFY(window.findChild<SettingsDialog *>() != nullptr);
        auto *dialog = window.findChild<SettingsDialog *>();
        QVERIFY(dialog->windowTitle().contains("Welcome"));
        dialog->reject();
        QTRY_VERIFY(window.findChild<SettingsDialog *>() == nullptr);
        QCOMPARE(backend.count("settings.set"), 0);
        QVERIFY(window.findChild<SearchEdit *>("searchInput")->isEnabled());
    }
    void changingPreferencesPreservesLivePauseState() {
        TestEnvironment env("gui-preserve-pause"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("settings.get") == 1);
        QTest::mouseClick(window.findChild<QPushButton *>("pauseIndexButton"), Qt::LeftButton);
        QTRY_VERIFY(backend.settings.indexingPaused);
        QTest::mouseClick(window.findChild<QPushButton *>("settingsButton"), Qt::LeftButton);
        auto *dialog = window.findChild<SettingsDialog *>(); QVERIFY(dialog);
        dialog->findChild<QComboBox *>("languageCombo")->setCurrentIndex(2);
        QTest::mouseClick(dialog->findChild<QPushButton *>("saveSettingsButton"), Qt::LeftButton);
        QTRY_COMPARE(backend.settings.language, QString("zh_CN"));
        QVERIFY(backend.settings.indexingPaused);
        QVERIFY(!backend.last("settings.set").params.contains("indexingPaused"));
        QCOMPARE(backend.count("index.resume"), 0);
        QCOMPARE(backend.count("index.start"), 0);
    }
    void environmentProfileCannotEnableOperatingSystemStartup() {
        TestEnvironment env("gui-startup-profile"); QVERIFY(env.isValid());
        qcopilots_search::test::ScopedEnvironment overrideProfile("QCOPILOTS_SEARCH_PROFILE_DIR", env.path("environment profile").toUtf8());
        struct RestoreTestMode { ~RestoreTestMode() { QStandardPaths::setTestModeEnabled(true); } } restore;
        QStandardPaths::setTestModeEnabled(false);
        FakeBackend backend;
        MainWindow window(RuntimePaths::resolve(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("settings.get") == 1);
        QTest::mouseClick(window.findChild<QPushButton *>("settingsButton"), Qt::LeftButton);
        auto *dialog = window.findChild<SettingsDialog *>(); QVERIFY(dialog);
        QVERIFY(!dialog->findChild<QCheckBox *>("startAtLoginCheck")->isEnabled());
        dialog->reject();
    }
    void pauseResumeRetryAndProgressErrors() {
        TestEnvironment env("gui-progress"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("status") > 0);
        QTest::mouseClick(window.findChild<QPushButton *>("pauseIndexButton"), Qt::LeftButton);
        QTRY_COMPARE(window.findChild<QPushButton *>("pauseIndexButton")->text(), QString("Resume"));
        QTest::mouseClick(window.findChild<QPushButton *>("pauseIndexButton"), Qt::LeftButton);
        QTRY_VERIFY(backend.count("index.resume") == 1);
        emit backend.eventReceived("progress", {{"state", "indexing"}, {"total", 7}, {"completed", 3}, {"failed", 1}, {"error", "Unreadable source file"}});
        QVERIFY(window.findChild<QPushButton *>("retryIndexButton")->isEnabled());
        QVERIFY(window.findChild<QLabel *>("indexProgressText")->text().contains("3 / 7"));
        QCOMPARE(window.findChild<QLabel *>("applicationError")->text(), QString("Unreadable source file"));
        QTest::mouseClick(window.findChild<QPushButton *>("retryIndexButton"), Qt::LeftButton);
        QTRY_COMPARE(backend.count("index.retry"), 1);
    }
    void keyboardNavigationAndQuickSearch() {
        TestEnvironment env("gui-keyboard"); QVERIFY(env.isValid());
        FakeBackend backend; SearchHit hit; hit.path = env.path("example.txt"); hit.text = "example"; backend.hits << hit;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        auto *results = window.findChild<QListWidget *>("searchResults");
        QTRY_COMPARE(results->count(), 1);
        auto *input = window.findChild<SearchEdit *>("searchInput");
        input->setFocus();
        QTest::keyClick(input, Qt::Key_Down);
        QCOMPARE(window.focusWidget(), results);
        QTest::keyClick(results, Qt::Key_Escape);
        QCOMPARE(window.focusWidget(), input);
        window.showQuickSearch();
        auto *quick = window.findChild<SearchEdit *>("quickSearchInput"); QVERIFY(quick);
        quick->setText("quick query");
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString("quick query"));
        QCOMPARE(backend.last("search").params.value("limit").toInt(), 12);
        QTest::mouseClick(window.findChild<QPushButton *>("openFullSearchButton"), Qt::LeftButton);
        QCOMPARE(input->text(), QString("quick query"));
    }
    void textPreviewDoesNotExecuteHtml() {
        TestEnvironment env("gui-html"); QVERIFY(env.isValid()); QVERIFY(env.writeFile("sample.html", "<h1>Heading</h1>"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("sample.html"); hit.text = "<script>alert('x')</script><img src='https://example.invalid/tracker'>";
        preview.showHit(hit);
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), hit.text);
    }
    void documentPreviewCanContinueAndKeepsCompletionLabel() {
        TestEnvironment env("gui-document-preview"); QVERIFY(env.isValid()); QVERIFY(env.writeFile("document.txt", "contents"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("document.txt"); hit.kind = "document"; hit.text = "first body";
        preview.showHit(hit);
        QSignalSpy requests(&preview, &PreviewPanel::documentRequested);
        auto *button = preview.findChild<QPushButton *>("loadDocumentPreviewButton");
        button->click();
        QCOMPARE(requests.count(), 1);
        QCOMPARE(requests.first().at(2).toInt(), 0);
        QVERIFY(!button->isEnabled());
        preview.setDocumentPreview({{"text", "first body"}, {"partial", true}, {"next", 100}}, false);
        QVERIFY(button->isEnabled());
        button->click();
        QCOMPARE(requests.last().at(2).toInt(), 100);
        preview.setDocumentPreview({{"text", "second body"}, {"partial", false}, {"next", 0}}, true);
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("first body\nsecond body"));
        QVERIFY(!button->isEnabled());
        preview.setLanguage("zh_CN");
        QCOMPARE(button->text(), QString::fromUtf8("正文已读取"));
    }
    void repeatedCodeUsesCellAndLineLocation() {
        TestEnvironment env("gui-code-location"); QVERIFY(env.isValid()); QVERIFY(env.writeFile("notebook.ipynb", "{}"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("notebook.ipynb"); hit.kind = "code"; hit.text = "repeat();"; hit.cell = 3; hit.lineStart = 90; hit.lineEnd = 91;
        preview.showHit(hit);
        preview.findChild<QPushButton *>("loadDocumentPreviewButton")->click();
        const QJsonArray segments{
            QJsonObject{{"text", "repeat();"}, {"cell", 1}, {"lineStart", 90}, {"lineEnd", 91}},
            QJsonObject{{"text", "repeat();"}, {"cell", 3}, {"lineStart", 1}, {"lineEnd", 2}},
            QJsonObject{{"text", "repeat();"}, {"cell", 3}, {"lineStart", 90}, {"lineEnd", 91}}
        };
        preview.setDocumentPreview({{"segments", segments}, {"partial", false}, {"next", 0}}, false);
        auto *body = preview.findChild<QTextBrowser *>("textPreview");
        QCOMPARE(body->textCursor().position(), int(body->toPlainText().lastIndexOf("repeat();")));
        QVERIFY(body->textCursor().position() > 0);
    }
    void laterBatchAutomaticallyLoadsAndLocatesHit() {
        TestEnvironment env("gui-later-location"); QVERIFY(env.isValid()); QVERIFY(env.writeFile("code.cpp", "source"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("code.cpp"); hit.kind = "code"; hit.text = "target();"; hit.lineStart = 5005; hit.lineEnd = 5006;
        preview.showHit(hit);
        QSignalSpy requests(&preview, &PreviewPanel::documentRequested);
        auto *button = preview.findChild<QPushButton *>("loadDocumentPreviewButton");
        button->click();
        preview.setDocumentPreview({{"segments", QJsonArray{QJsonObject{{"text", "first batch"}, {"lineStart", 1}, {"lineEnd", 3}}}}, {"partial", true}, {"next", 100}}, false);
        QVERIFY(!button->isEnabled());
        QTRY_COMPARE(requests.count(), 2);
        QCOMPARE(requests.last().at(2).toInt(), 100);
        preview.setDocumentPreview({{"segments", QJsonArray{QJsonObject{{"text", "target();"}, {"lineStart", 5005}, {"lineEnd", 5006}}}}, {"partial", false}, {"next", 0}}, true);
        auto *body = preview.findChild<QTextBrowser *>("textPreview");
        QCOMPARE(body->textCursor().position(), int(body->toPlainText().indexOf("target();")));
        QVERIFY(body->textCursor().position() > 0);
        QCOMPARE(button->text(), QString("Document loaded"));
    }
    void pdfHitPageIsSelected() {
        TestEnvironment env("gui-pdf"); QVERIFY(env.isValid());
        const QString path = env.path("two pages 中文.pdf");
        { QPdfWriter writer(path); QPainter painter(&writer); painter.drawText(100, 200, "first page"); writer.newPage(); painter.drawText(100, 200, "second page"); }
        PreviewPanel preview; SearchHit hit; hit.path = path; hit.page = 2; hit.text = "second page"; hit.kind = "pdf";
        preview.showHit(hit);
        QCOMPARE(preview.findChild<QSpinBox *>("pdfPageNumber")->value(), 2);
        QCOMPARE(preview.findChild<QSpinBox *>("pdfPageNumber")->maximum(), 2);
        preview.clear();
        QVERIFY(QFile::remove(path));
    }
    void imageAndMissingFileReleaseResources() {
        TestEnvironment env("gui-image"); QVERIFY(env.isValid());
        const QString path = env.path("picture 中文.png"); QImage image(64, 64, QImage::Format_RGB32); image.fill(Qt::blue); QVERIFY(image.save(path));
        PreviewPanel preview; SearchHit hit; hit.path = path; hit.kind = "image";
        preview.showHit(hit); QVERIFY(preview.findChild<QPushButton *>("openResultButton")->isEnabled());
        QTRY_VERIFY(!preview.findChild<QLabel *>("imagePreview")->pixmap().isNull());
        preview.clear(); QVERIFY(QFile::remove(path));
        preview.showHit(hit); QVERIFY(!preview.findChild<QPushButton *>("openResultButton")->isEnabled());
        QVERIFY(!preview.findChild<QLabel *>("previewError")->text().isEmpty());
    }
    void pdfImageAndTextSwitchingReleasesResources() {
        TestEnvironment env("gui-preview-switching"); QVERIFY(env.isValid());
        const QString pdfPath = env.path("two pages 中文.pdf");
        { QPdfWriter writer(pdfPath); QPainter painter(&writer); painter.drawText(100, 200, "first page"); writer.newPage(); painter.drawText(100, 200, "second page"); }
        const QString imagePath = env.path("green image 中文.png");
        QImage pixels(240, 120, QImage::Format_RGB32); pixels.fill(Qt::green); QVERIFY(pixels.save(imagePath));
        QVERIFY(env.writeFile("plain text.txt", "The currently selected document"));
        SearchHit pdf; pdf.path = pdfPath; pdf.kind = "pdf"; pdf.page = 2;
        SearchHit image; image.path = imagePath; image.kind = "image";
        SearchHit text; text.path = env.path("plain text.txt"); text.kind = "text"; text.text = "The currently selected document";
        PreviewPanel preview; preview.resize(640, 480); preview.show();
        auto *imageLabel = preview.findChild<QLabel *>("imagePreview");
        auto *textPreview = preview.findChild<QTextBrowser *>("textPreview");
        for (int pass = 0; pass < 4; ++pass) {
            preview.showHit(pdf);
            QCOMPARE(preview.findChild<QSpinBox *>("pdfPageNumber")->value(), 2);
            // Let the visible PDF submit rendering work before changing its source.
            QCoreApplication::processEvents();
            preview.showHit(image);
            preview.showHit(text);
            QCoreApplication::processEvents();
            QCOMPARE(preview.currentHit().path, text.path);
            QCOMPARE(textPreview->toPlainText(), text.text);
            preview.showHit(image);
            QTRY_VERIFY_WITH_TIMEOUT(!imageLabel->pixmap().isNull(), 10000);
            QCOMPARE(imageLabel->pixmap().toImage().pixelColor(0, 0), QColor(Qt::green));
            QCOMPARE(preview.currentHit().path, imagePath);
            preview.clear();
        }
        QVERIFY(QFile::remove(pdfPath));
        QVERIFY(QFile::remove(imagePath));
        QVERIFY(QFile::remove(text.path));
    }
    void imagePreviewResizeRemainsBounded() {
        TestEnvironment env("gui-image-resize"); QVERIFY(env.isValid());
        const QString path = env.path("wide image.png");
        QImage pixels(1200, 300, QImage::Format_RGB32); pixels.fill(Qt::blue); QVERIFY(pixels.save(path));
        PreviewPanel preview; preview.resize(760, 560); preview.show();
        SearchHit hit; hit.path = path; hit.kind = "image"; preview.showHit(hit);
        auto *image = preview.findChild<QLabel *>("imagePreview");
        QTRY_VERIFY_WITH_TIMEOUT(!image->pixmap().isNull(), 10000);
        for (const QSize size : {QSize(760, 560), QSize(600, 460), QSize(900, 620), QSize(620, 480)}) {
            preview.resize(size);
            QCoreApplication::processEvents();
            const QSize settled = preview.size();
            for (int pass = 0; pass < 3; ++pass) QCoreApplication::processEvents();
            QCOMPARE(preview.size(), settled);
            QVERIFY(!image->pixmap().isNull());
            QVERIFY(image->pixmap().width() <= image->width());
            QVERIFY(image->pixmap().height() <= image->height());
        }
    }
    void audioHitSeeksAndSwitchingReleasesFile() {
        TestEnvironment env("gui-audio"); QVERIFY(env.isValid());
        QByteArray wav("RIFF");
        const auto append32 = [&wav](quint32 value) { const quint32 le = qToLittleEndian(value); wav.append(reinterpret_cast<const char *>(&le), sizeof(le)); };
        const auto append16 = [&wav](quint16 value) { const quint16 le = qToLittleEndian(value); wav.append(reinterpret_cast<const char *>(&le), sizeof(le)); };
        constexpr int samples = 32000;
        append32(36 + samples * 2); wav.append("WAVEfmt ", 8); append32(16); append16(1); append16(1); append32(16000); append32(32000); append16(2); append16(16); wav.append("data", 4); append32(samples * 2); wav.append(QByteArray(samples * 2, '\0'));
        QVERIFY(env.writeFile("audio 中文.wav", wav));
        PreviewPanel preview; preview.show(); SearchHit audio; audio.path = env.path("audio 中文.wav"); audio.kind = "audio"; audio.startMs = 700;
        preview.showHit(audio);
        auto *player = preview.findChild<QMediaPlayer *>("previewMediaPlayer"); QVERIFY(player);
        QTRY_VERIFY_WITH_TIMEOUT(player->duration() >= 1900, 15000);
        QTRY_COMPARE_WITH_TIMEOUT(player->position(), 700, 15000);
        player->play();
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        preview.hide();
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PausedState);
        QVERIFY(env.writeFile("document.txt", "document"));
        SearchHit document; document.path = env.path("document.txt"); document.kind = "document"; document.text = "document";
        preview.showHit(document);
        QCOMPARE(player->source(), QUrl());
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
        QTRY_VERIFY(!QFileInfo::exists(audio.path) || QFile::remove(audio.path));
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("document"));
    }
    void rapidImageSelectionsPublishOnlyLatestAndReleaseHandles() {
        TestEnvironment env("gui-image-cancellation"); QVERIFY(env.isValid());
        QStringList paths;
        for (int n = 0; n < 12; ++n) {
            QImage image(1500, 1200, QImage::Format_RGB32);
            image.fill(n == 11 ? Qt::green : Qt::red);
            const QString path = env.path(QStringLiteral("image-%1.png").arg(n));
            QVERIFY(image.save(path)); paths << path;
        }
        {
            PreviewPanel preview;
            preview.resize(600, 400);
            preview.show();
            for (const auto &path : paths) { SearchHit hit; hit.path = path; hit.kind = "image"; preview.showHit(hit); }
            auto *image = preview.findChild<QLabel *>("imagePreview");
            QTRY_VERIFY_WITH_TIMEOUT(!image->pixmap().isNull(), 10000);
            QCOMPARE(image->pixmap().toImage().pixelColor(0, 0), QColor(Qt::green));
            QCOMPARE(preview.currentHit().path, paths.last());
        }
        for (const auto &path : paths) QVERIFY(QFile::remove(path));
    }
    void missingModelsExposeImportAndRetry() {
        TestEnvironment env("gui-models"); QVERIFY(env.isValid());
        ModelDialog dialog(env.runtimePaths().modelDir, "en");
        QTRY_VERIFY(dialog.findChild<QPushButton *>("importModelButton")->isEnabled());
        QVERIFY(dialog.findChild<QPushButton *>("downloadModelButton")->isEnabled());
        QVERIFY(dialog.findChild<QLabel *>("modelStatus")->text().contains("Keyword search"));
    }
    void verifiedModelRetriesPublicationBusy() {
        TestEnvironment env("gui-model-reload"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("settings.get") == 1);
        QTest::mouseClick(window.findChild<QPushButton *>("modelsButton"), Qt::LeftButton);
        auto *dialog = window.findChild<ModelDialog *>(); QVERIFY(dialog);
        QTRY_VERIFY(dialog->findChild<QPushButton *>("importModelButton")->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(dialog, "modelsReady"));
        QCOMPARE(backend.count("models.reload"), 1);
        emit backend.failed(backend.last("models.reload").id, "busy", "Publication in progress");
        QTRY_COMPARE(backend.count("models.reload"), 2);
        emit backend.response(backend.last("models.reload").id, {{"modelAvailable", true}});
        QCOMPARE(window.findChild<QPushButton *>("modelsButton")->text(), QString("Model ready"));
        QTRY_VERIFY(backend.count("index.start") == 1);
    }
    void editorAndRevealRejectMissingFiles() {
        TestEnvironment env("gui-platform"); QVERIFY(env.isValid()); SearchHit hit; hit.path = env.path("missing file.cpp"); hit.lineStart = 7;
        QString error;
        QVERIFY(!PlatformIntegration::openHit(hit, "not-a-real-editor --goto {file}:{line}", &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!PlatformIntegration::revealFile(hit.path, &error));
    }
};
QTEST_MAIN(GuiTests)
#include "gui_tests.moc"
