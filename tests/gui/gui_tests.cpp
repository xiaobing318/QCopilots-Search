#include "app/app_backend.h"
#include "app/main_window.h"
#include "app/model_dialog.h"
#include "app/platform_integration.h"
#include "app/preview_panel.h"
#include "app/settings_dialog.h"
#include "core/app_settings.h"
#include "support/test_environment.h"
#include <QAbstractItemView>
#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QImage>
#include <QInputMethodEvent>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMediaPlayer>
#include <QPainter>
#include <QPdfPageNavigator>
#include <QPdfView>
#include <QPdfWriter>
#include <QPersistentModelIndex>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProxyStyle>
#include <QPushButton>
#include <QScrollBar>
#include <QSignalSpy>
#include <QSlider>
#include <QSpinBox>
#include <QStandardPaths>
#include <QTabWidget>
#include <QTextBrowser>
#include <QtTest>
#include <QtEndian>

using namespace qcopilots_search;
using qcopilots_search::test::TestEnvironment;

namespace {
QPixmap imagePreviewPixmap(const QWidget &panel) {
    const auto *view = panel.findChild<QGraphicsView *>("imagePreview");
    if (view && view->scene()) {
        for (auto *item : view->scene()->items()) {
            if (auto *image = qgraphicsitem_cast<QGraphicsPixmapItem *>(item)) return image->pixmap();
        }
    }
    return {};
}

class WidgetPopupStyle final : public QProxyStyle {
public:
    int styleHint(StyleHint hint, const QStyleOption *option = nullptr,
                  const QWidget *widget = nullptr, QStyleHintReturn *returnData = nullptr) const override {
        if (hint == QStyle::SH_ComboBox_UseNativePopup) return 0;
        return QProxyStyle::styleHint(hint, option, widget, returnData);
    }
};

QByteArray silentPcmWave(int seconds) {
    QByteArray wav("RIFF");
    const auto append32 = [&wav](quint32 value) { const quint32 le = qToLittleEndian(value); wav.append(reinterpret_cast<const char *>(&le), sizeof(le)); };
    const auto append16 = [&wav](quint16 value) { const quint16 le = qToLittleEndian(value); wav.append(reinterpret_cast<const char *>(&le), sizeof(le)); };
    const int samples = 16000 * seconds;
    append32(36 + samples * 2); wav.append("WAVEfmt ", 8); append32(16);
    append16(1); append16(1); append32(16000); append32(32000);
    append16(2); append16(16); wav.append("data", 4); append32(samples * 2);
    wav.append(QByteArray(samples * 2, '\0'));
    return wav;
}
}

class FakeBackend final : public AppBackend {
    Q_OBJECT
public:
    struct Request { QString id, method; QJsonObject params; };
    QList<Request> requests;
    QStringList cancelled;
    AppSettings settings;
    QList<SearchHit> hits;
    bool autoSearch = true;
    bool autoFiles = true;
    bool autoStatus = true;
    bool autoPreview = true;
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
            else if (method == "status" && autoStatus) emit response(id, {{"modelAvailable", false}, {"progress", QJsonObject{{"state", "idle"}, {"total", 2}, {"completed", 2}}}});
            else if (method == "search" && autoSearch) {
                QJsonArray values;
                for (const auto &hit : hits) values.append(hit.toJson());
                emit response(id, {{"hits", values}, {"semantic", false}});
            } else if (method == "files.list" && autoFiles) {
                QJsonArray files;
                for (const auto &hit : hits) {
                    FileListEntry file;
                    file.fileId = files.size() + 1; file.path = hit.path; file.title = hit.title;
                    file.kind = hit.kind; file.code = params.value("code").toBool(); file.sourceVersion = hit.sourceVersion;
                    files.append(file.toJson());
                }
                emit response(id, {{"files", files}, {"nextCursor", ""}, {"hasMore", false}, {"browseGeneration", "1"}, {"metadataPending", false}});
            } else if (method == "document.preview" && autoPreview) {
                for (const auto &hit : hits) if (hit.path == params.value("path").toString()) {
                    emit response(id, {{"sourceVersion", "preview-source-v1"}, {"text", hit.text}, {"partial", false}, {"next", 0}});
                    break;
                }
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
        QVERIFY(!results->item(0)->text().contains(hit.text));
        QCOMPARE(results->item(0)->text().count('\n'), 1);
        QCOMPARE(results->item(0)->toolTip(), hit.path);
    }
    void filtersAndSeparateCodeSearch() {
        TestEnvironment env("gui-filters"); QVERIFY(env.isValid());
        FakeBackend backend; backend.settings.includeCode = true; backend.settings.roots << env.root();
        backend.settings.codeRoots << env.path("source code");
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("filter");
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
        QCOMPARE(root->itemData(1).toString(), env.path("source code"));
        QCOMPARE(root->currentData().toString(), QString());
        QVERIFY(!kind->isEnabled());
        QCOMPARE(backend.last("search").params.value("kind").toString(), QString());
    }
    void emptyQueryBrowsesFilesAndLoadsOnlySelectedBody() {
        TestEnvironment env("gui-file-browser"); QVERIFY(env.isValid());
        FakeBackend backend;
        for (int i = 0; i < 3; ++i) {
            const QString name = QStringLiteral("file-%1.txt").arg(i);
            QVERIFY(env.writeFile(name, "fixture"));
            SearchHit hit; hit.path = env.path(name); hit.title = name; hit.kind = "text";
            hit.text = QStringLiteral("Private body content %1").arg(i); backend.hits.append(hit);
        }
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        auto *results = window.findChild<QListWidget *>("searchResults");
        QTRY_COMPARE(results->count(), 3);
        QCOMPARE(backend.count("search"), 0);
        QCOMPARE(backend.last("files.list").params.value("limit").toInt(), 100);
        QVERIFY(!backend.last("files.list").params.contains("query"));
        QCOMPARE(results->currentRow(), 0);
        QTRY_COMPARE(backend.count("document.preview"), 1);
        QTRY_COMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), backend.hits.first().text);
        for (int row = 0; row < results->count(); ++row) {
            QCOMPARE(results->item(row)->text().count('\n'), 1);
            QVERIFY(!results->item(row)->text().contains("Private body content"));
            QVERIFY(results->item(row)->icon().isNull());
        }
        results->setCurrentRow(2);
        QTRY_COMPARE(backend.count("document.preview"), 2);
        QTRY_COMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), backend.hits.last().text);
        QVERIFY(!window.findChild<QPushButton *>("loadMoreFilesButton")->isEnabled());
    }
    void browsingAppendAndStaleCursorPreserveSelectionAndPreview() {
        TestEnvironment env("gui-file-pages"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoFiles = false;
        QJsonArray first, second;
        for (int i = 0; i < 45; ++i) {
            const QString name = QStringLiteral("file-%1.txt").arg(i);
            QVERIFY(env.writeFile(name, "fixture"));
            FileListEntry entry; entry.fileId = i + 1; entry.path = env.path(name); entry.title = name; entry.kind = "text"; entry.sourceVersion = "v1";
            (i < 30 ? first : second).append(entry.toJson());
        }
        MainWindow window(env.runtimePaths(), &backend, false, false); window.resize(850, 540); window.show(); window.start();
        QTRY_COMPARE(backend.count("files.list"), 1);
        emit backend.response(backend.last("files.list").id, {{"files", first}, {"nextCursor", "cursor-1"}, {"hasMore", true}, {"browseGeneration", "7"}});
        auto *results = window.findChild<QListWidget *>("searchResults");
        auto *preview = window.findChild<PreviewPanel *>();
        auto *more = window.findChild<QPushButton *>("loadMoreFilesButton");
        QCOMPARE(results->count(), 30); QVERIFY(more->isEnabled());
        results->setCurrentRow(20); results->scrollToItem(results->currentItem(), QAbstractItemView::PositionAtTop);
        QTRY_VERIFY(results->verticalScrollBar()->value() > 0);
        const int scroll = results->verticalScrollBar()->value();
        const QString selected = results->currentItem()->data(Qt::UserRole).toString();
        preview->setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"text", "Already loaded body"}, {"partial", false}}, false);
        const int reads = backend.count("document.preview");
        more->click();
        QCOMPARE(backend.last("files.list").params.value("cursor").toString(), QString("cursor-1"));
        QVERIFY(!more->isEnabled());
        emit backend.response(backend.last("files.list").id, {{"files", second}, {"nextCursor", "cursor-2"}, {"hasMore", true}, {"browseGeneration", "7"}});
        QCOMPARE(results->count(), 45); QCOMPARE(results->currentRow(), 20);
        QCOMPARE(results->verticalScrollBar()->value(), scroll);
        QCOMPARE(preview->currentHit().path, selected);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("Already loaded body"));
        QCOMPARE(backend.count("document.preview"), reads);
        const int pageRequests = backend.count("files.list");
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "7"}});
        QVERIFY(more->isEnabled());
        backend.settings.language = "zh_CN";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QTest::qWait(300);
        QCOMPARE(backend.count("files.list"), pageRequests);
        QCOMPARE(results->currentRow(), 20);
        QCOMPARE(results->count(), 45);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("Already loaded body"));
        backend.settings.language = "en";
        emit backend.eventReceived("settings", backend.settings.toJson());
        more->click();
        emit backend.failed(backend.last("files.list").id, "cursor_stale", "The catalog changed");
        QCOMPARE(results->count(), 45); QCOMPARE(results->currentRow(), 20);
        QCOMPARE(results->verticalScrollBar()->value(), scroll);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("Already loaded body"));
        QVERIFY(!more->isEnabled());
        QVERIFY(window.findChild<QLabel *>("searchSummary")->text().contains("Refresh"));
        window.findChild<QPushButton *>("refreshFilesButton")->click();
        QVERIFY(!backend.last("files.list").params.contains("cursor"));
        emit backend.response(backend.last("files.list").id, {{"files", first}, {"nextCursor", ""}, {"hasMore", false}, {"browseGeneration", "8"}});
        QCOMPARE(results->currentRow(), 20);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("Already loaded body"));
        QVERIFY(!more->isEnabled());
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "8"}, {"visibilityEpoch", "changed-eligibility"}});
        QCOMPARE(results->currentRow(), 20);
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("Already loaded body"));
        QVERIFY(window.findChild<QLabel *>("searchSummary")->text().contains("Refresh"));
    }
    void initiallyEmptyBrowserLoadsFirstFilesAfterIndexing_data() {
        QTest::addColumn<bool>("quick");
        QTest::newRow("main") << false;
        QTest::newRow("quick") << true;
    }
    void initiallyEmptyBrowserLoadsFirstFilesAfterIndexing() {
        QFETCH(bool, quick);
        TestEnvironment env("gui-first-index-browser"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoFiles = false; backend.autoStatus = false;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(backend.count("files.list"), 1);
        const auto emptyPage = [](const char *generation) {
            return QJsonObject{{"files", QJsonArray{}}, {"hasMore", false}, {"nextCursor", ""}, {"browseGeneration", generation}};
        };
        auto initialPage = emptyPage("1");
        if (quick) {
            // Keep the main window populated so only the quick browser retries.
            FileListEntry entry; entry.fileId = 999; entry.path = env.path("main-only.txt"); entry.title = "Main result"; entry.kind = "text";
            initialPage.insert("files", QJsonArray{entry.toJson()});
        }
        emit backend.response(backend.last("files.list").id, initialPage);
        if (quick) {
            window.showQuickSearch();
            emit backend.response(backend.last("files.list").id, emptyPage("1"));
        }
        auto *results = window.findChild<QListWidget *>(quick ? "quickSearchResults" : "searchResults");
        auto *input = window.findChild<SearchEdit *>(quick ? "quickSearchInput" : "searchInput");
        auto *summary = window.findChild<QLabel *>(quick ? "quickSearchStatus" : "searchSummary");
        QCOMPARE(results->count(), 0); QCOMPARE(results->currentRow(), -1);
        // Setting a child widget's focus does not activate its top-level window,
        // particularly on X11 after another test window has been active.
        auto *targetWindow = input->window();
        targetWindow->raise();
        targetWindow->activateWindow();
        QVERIFY(QTest::qWaitForWindowActive(targetWindow));
        input->setFocus();
        QTRY_VERIFY(input->hasFocus());
        auto *focus = QApplication::focusWidget();
        const int initialRequests = backend.count("files.list");
        emit backend.eventReceived("progress", {{"state", "indexing"}, {"total", 205}, {"completed", 0}});
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "2"}});
        QTRY_VERIFY(backend.count("files.list") > initialRequests);
        // A newer publication must invalidate a pending empty snapshot too.
        const QString obsoleteRequest = backend.last("files.list").id;
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "3"}});
        QVERIFY(backend.cancelled.contains(obsoleteRequest));
        emit backend.response(obsoleteRequest, emptyPage("2"));
        const int afterInvalidation = backend.count("files.list");
        emit backend.eventReceived("progress", {{"state", "idle"}, {"total", 205}, {"completed", 205}});
        QTRY_VERIFY(backend.count("files.list") > afterInvalidation);
        const int pageSize = quick ? 12 : 100;
        QJsonArray files;
        for (int row = 0; row < pageSize; ++row) {
            const QString name = QStringLiteral("indexed-%1.txt").arg(row);
            QVERIFY(env.writeFile(name, "first indexing preview"));
            SearchHit hit; hit.path = env.path(name); hit.title = name; hit.kind = "text"; hit.text = "first indexing preview";
            backend.hits.append(hit);
            FileListEntry file; file.fileId = row + 1; file.path = hit.path; file.title = name; file.kind = hit.kind;
            files.append(file.toJson());
        }
        const QString currentRequest = backend.last("files.list").id;
        QCOMPARE(backend.last("files.list").params.value("limit").toInt(), pageSize);
        emit backend.response(currentRequest, {{"files", files}, {"hasMore", true}, {"nextCursor", "next-page"}, {"browseGeneration", "3"}});
        QCOMPARE(results->count(), pageSize); QCOMPARE(results->currentRow(), 0);
        QCOMPARE(QApplication::focusWidget(), focus);
        if (!quick) QTRY_COMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("first indexing preview"));
        const int populatedRequests = backend.count("files.list");
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "4"}});
        QTest::qWait(350);
        QCOMPARE(backend.count("files.list"), populatedRequests);
        QCOMPARE(results->count(), pageSize); QCOMPARE(results->currentRow(), 0);
        QCOMPARE(QApplication::focusWidget(), focus);
        QVERIFY(summary->text().contains("Refresh"));
    }
    void typingQueryConsumesQueuedEmptyBrowserRefresh() {
        TestEnvironment env("gui-first-index-query"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoFiles = false; backend.autoSearch = false;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(backend.count("files.list"), 1);
        emit backend.response(backend.last("files.list").id, {{"files", QJsonArray{}}, {"browseGeneration", "1"}});
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "2"}});
        window.findChild<SearchEdit *>("searchInput")->setText("new user question");
        QTRY_COMPARE(backend.count("search"), 1);
        QCOMPARE(backend.last("search").params.value("query").toString(), QString("new user question"));
        QCOMPARE(backend.count("files.list"), 1);
    }
    void indexChangeKeepsPendingBrowserFilterRequest_data() {
        QTest::addColumn<QString>("filter");
        QTest::newRow("kind") << QStringLiteral("kind");
        QTest::newRow("root") << QStringLiteral("root");
        QTest::newRow("code") << QStringLiteral("code");
    }
    void indexChangeKeepsPendingBrowserFilterRequest() {
        QFETCH(QString, filter);
        TestEnvironment env("gui-browse-filter-publication"); QVERIFY(env.isValid());
        QVERIFY(env.writeFile("selected.txt", "existing preview"));
        FakeBackend backend; backend.settings.roots = {env.root()};
        backend.settings.codeRoots = {env.path("code")}; backend.settings.includeCode = true;
        SearchHit hit; hit.path = env.path("selected.txt"); hit.title = "Selected"; hit.kind = "text"; hit.text = "existing preview";
        backend.hits = {hit};
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(window.findChild<QListWidget *>("searchResults")->count(), 1);
        const int requests = backend.count("files.list");
        if (filter == "kind") window.findChild<QComboBox *>("kindFilter")->setCurrentIndex(2);
        else if (filter == "root") window.findChild<QComboBox *>("rootFilter")->setCurrentIndex(1);
        else window.findChild<QCheckBox *>("codeSearchCheck")->setChecked(true);
        // The visible rows still belong to the old context until debounce fires.
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "2"}});
        QTRY_COMPARE(backend.count("files.list"), requests + 1);
        const auto parameters = backend.last("files.list").params;
        if (filter == "kind") QCOMPARE(parameters.value("kind").toString(), QString("pdf"));
        else if (filter == "root") QCOMPARE(parameters.value("root").toString(), env.root());
        else QVERIFY(parameters.value("code").toBool());
    }
    void lateBrowseResponseCannotReplaceTypedSearchBeforeDebounce() {
        TestEnvironment env("gui-browse-stale"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoFiles = false; backend.autoSearch = false;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(backend.count("files.list"), 1);
        const QString request = backend.last("files.list").id;
        window.findChild<SearchEdit *>("searchInput")->setText("new question");
        FileListEntry entry; entry.fileId = 1; entry.path = env.path("old.txt"); entry.title = "Old result";
        emit backend.response(request, {{"files", QJsonArray{entry.toJson()}}, {"browseGeneration", "1"}});
        QCOMPARE(window.findChild<QListWidget *>("searchResults")->count(), 0);
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString("new question"));
    }
    void metadataPendingSearchShowsProgressAndRetriesAfterIndexChange() {
        TestEnvironment env("gui-search-metadata-pending"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoSearch = false;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("catalog question");
        QTRY_COMPARE(backend.count("search"), 1);
        emit backend.failed(backend.last("search").id, "metadata_pending", "Checking folder metadata");
        QVERIFY(window.findChild<QLabel *>("searchSummary")->text().contains("Checking folders"));
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "2"}});
        QTRY_COMPARE(backend.count("search"), 2);
    }
    void quickBrowserUsesTwelveAndRefreshesLanguage() {
        TestEnvironment env("gui-quick-browser"); QVERIFY(env.isValid());
        FakeBackend backend; SearchHit hit; hit.path = env.path("summary.txt"); hit.title = "Summary"; hit.text = "Secret body"; backend.hits << hit;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(backend.count("settings.get"), 1);
        window.showQuickSearch();
        auto *results = window.findChild<QListWidget *>("quickSearchResults");
        QTRY_COMPARE(results->count(), 1);
        QCOMPARE(backend.last("files.list").params.value("limit").toInt(), 12);
        QVERIFY(!window.findChild<QPushButton *>("quickLoadMoreFilesButton"));
        QVERIFY(!results->item(0)->text().contains(hit.text));
        QCOMPARE(results->item(0)->text().count('\n'), 1);
        backend.settings.language = "zh_CN";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(window.findChild<QPushButton *>("openFullSearchButton")->text(), QString::fromUtf8("打开完整搜索"));
        QCOMPARE(window.findChild<SearchEdit *>("quickSearchInput")->placeholderText(), QString::fromUtf8("描述您想查找的内容…"));
    }
    void quickBrowseFailureRemainsVisibleAcrossStatusRefresh() {
        TestEnvironment env("gui-quick-browse-error"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoStatus = false;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(backend.count("settings.get"), 1);
        window.showQuickSearch();
        QTRY_VERIFY(backend.count("files.list") > 0);
        QTRY_VERIFY(window.findChild<QPushButton *>("quickRefreshFilesButton")->isEnabled());
        backend.autoFiles = false;
        window.findChild<QPushButton *>("quickRefreshFilesButton")->click();
        emit backend.failed(backend.last("files.list").id, "storage_error", "Unable to read the catalog");
        auto *message = window.findChild<QLabel *>("quickSearchStatus"); QVERIFY(message);
        QCOMPARE(message->text(), QString("Unable to read the catalog"));
        emit backend.response(backend.last("status").id, {{"modelAvailable", false}});
        QCOMPARE(message->text(), QString("Unable to read the catalog"));
        QVERIFY(window.findChild<QPushButton *>("quickRefreshFilesButton")->isEnabled());
        window.findChild<QPushButton *>("quickRefreshFilesButton")->click();
        emit backend.failed(backend.last("files.list").id, "unknown_method", "Unknown method: files.list");
        QVERIFY(message->text().contains("incompatible versions"));
        QCOMPARE(backend.count("search"), 0);
    }
    void inferenceStatusShowsBackendDeviceAndFallback() {
        TestEnvironment env("gui-inference-status"); QVERIFY(env.isValid());
        FakeBackend backend; backend.autoStatus = false;
        MainWindow window(env.runtimePaths(), &backend, false, false); window.show(); window.start();
        QTRY_COMPARE(backend.count("settings.get"), 1);
        QTRY_COMPARE(backend.count("status"), 1);
        const QJsonObject runtime{{"buildVariant", "windows-x64-vulkan"}, {"selectedBackend", "cpu"},
            {"deviceName", "CPU"}, {"deviceDescription", "Portable processor backend"},
            {"gpuSelected", false}, {"loaded", true}, {"fallbackReason", "No Vulkan device is available"}};
        emit backend.response(backend.last("status").id, {{"modelAvailable", true}, {"runtime", runtime}});
        auto *status = window.findChild<QLabel *>("inferenceRuntimeStatus"); QVERIFY(status);
        QVERIFY(status->isVisible());
        QVERIFY(status->text().contains("windows-x64-vulkan"));
        QVERIFY(status->text().contains("Inference: CPU"));
        QVERIFY(status->text().contains("CPU fallback: No Vulkan device is available"));
        QCOMPARE(status->toolTip(), QString("Portable processor backend"));
        auto stopped = runtime; stopped.insert("loaded", false);
        emit backend.eventReceived("runtime", {{"runtime", stopped}});
        QVERIFY(status->text().contains("Last inference (idle)"));
        stopped.insert("error", "Strict GPU initialization failed");
        stopped.insert("fallbackReason", "");
        emit backend.eventReceived("runtime", {{"runtime", stopped}});
        QVERIFY(status->text().contains("Inference unavailable"));
        QVERIFY(status->text().contains("Strict GPU initialization failed"));
        QVERIFY(!status->text().contains("CPU fallback"));
        backend.settings.language = "zh_CN";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QVERIFY(status->text().contains(QString::fromUtf8("推理不可用")));
    }
    void independentFoldersAndExternalApplicationArgumentsRoundTrip() {
        AppSettings settings; settings.language = "en"; settings.roots = {"ordinary"}; settings.excludes = {"ordinary/private"};
        settings.codeRoots = {"code"}; settings.codeExcludes = {"code/build"}; settings.includeCode = false;
        settings.pdfOpener.preset = "custom"; settings.pdfOpener.program = "custom-pdf";
        settings.pdfOpener.arguments = {"--page", "{page}", "", "{file}"};
        SettingsDialog dialog(settings, false, {});
        QVERIFY(!dialog.startupChangeRequested());
        QCOMPARE(dialog.settings().pdfOpener.arguments, settings.pdfOpener.arguments);
        QCOMPARE(dialog.settings().codeRoots, settings.codeRoots);
        auto *enable = dialog.findChild<QCheckBox *>("includeCodeCheck");
        auto *codeRoots = dialog.findChild<QListWidget *>("codeRootFolders");
        QVERIFY(!codeRoots->isEnabled()); enable->setChecked(true); QVERIFY(codeRoots->isEnabled());
        codeRoots->addItem("extra code"); enable->setChecked(false); enable->setChecked(true);
        QCOMPARE(dialog.settings().codeRoots, QStringList({"code", "extra code"}));
        QCOMPARE(dialog.settings().roots, settings.roots); QCOMPARE(dialog.settings().excludes, settings.excludes);
        auto *arguments = dialog.findChild<QPlainTextEdit *>("pdfOpenerArguments"); QVERIFY(arguments);
        arguments->setPlainText("--page\n{page}\n{file}");
        QCOMPARE(dialog.settings().pdfOpener.arguments, QStringList({"--page", "{page}", "{file}"}));
    }
    void customStartupTargetRequiresExplicitTakeover() {
        TestEnvironment env("gui-startup-target"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        AppSettings settings; settings.language = "en"; settings.startAtLogin = true;
        SettingsDialog::StartupOptions startup;
        startup.allowed = true;
        startup.profileDir = env.path("current data");
        startup.modelDir = env.path("current models");
        startup.state.status = LoginStartupStatus::Enabled;
        startup.state.profileDir = env.path("registered data");
        startup.state.modelDir = env.path("registered models");
        SettingsDialog dialog(settings, false, startup);
        auto *check = dialog.findChild<QCheckBox *>("startAtLoginCheck"); QVERIFY(check);
        auto *useCurrent = dialog.findChild<QPushButton *>("useCurrentStartupDirectoriesButton"); QVERIFY(useCurrent);
        auto *status = dialog.findChild<QLabel *>("startAtLoginStatus"); QVERIFY(status);
        QVERIFY(!check->isEnabled());
        QVERIFY(!check->isChecked());
        QVERIFY(!dialog.startupChangeRequested());
        QVERIFY(!useCurrent->isHidden());
        QVERIFY(!dialog.settings().startAtLogin);
        QVERIFY(status->text().contains(QDir::toNativeSeparators(startup.state.profileDir)));
        QVERIFY(status->text().contains(QDir::toNativeSeparators(startup.state.modelDir)));
        useCurrent->click();
        QVERIFY(check->isEnabled());
        QVERIFY(check->isChecked());
        QVERIFY(dialog.startupChangeRequested());
        QVERIFY(dialog.settings().startAtLogin);
        QVERIFY(status->text().contains(QDir::toNativeSeparators(startup.profileDir)));
        QVERIFY(status->text().contains(QDir::toNativeSeparators(startup.modelDir)));
        check->setChecked(false);
        QVERIFY(!dialog.startupChangeRequested());
        QVERIFY(!check->isEnabled());
        QVERIFY(useCurrent->isEnabled());
        QVERIFY(!dialog.settings().startAtLogin);
    }
    void matchingStartupTargetCanBeDisabled() {
        TestEnvironment env("gui-startup-owned"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        AppSettings settings; settings.language = "en";
        SettingsDialog::StartupOptions startup;
        startup.allowed = true;
        startup.profileDir = env.path("data");
        startup.modelDir = env.path("models");
        startup.state.status = LoginStartupStatus::ApprovalRequired;
        startup.state.profileDir = startup.profileDir;
        startup.state.modelDir = startup.modelDir;
        SettingsDialog dialog(settings, false, startup);
        auto *check = dialog.findChild<QCheckBox *>("startAtLoginCheck"); QVERIFY(check);
        QVERIFY(check->isEnabled());
        QVERIFY(check->isChecked());
        QVERIFY(dialog.findChild<QPushButton *>("useCurrentStartupDirectoriesButton")->isHidden());
        QVERIFY(!dialog.startupChangeRequested());
        QVERIFY(dialog.findChild<QLabel *>("startAtLoginStatus")->text().contains("approval required"));
        check->setChecked(false);
        QVERIFY(dialog.startupChangeRequested());
        QVERIFY(!dialog.settings().startAtLogin);
    }
    void startupReadErrorCanBeRepairedWithCurrentDirectories() {
        TestEnvironment env("gui-startup-repair"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        AppSettings settings; settings.language = "zh_CN"; settings.startAtLogin = true;
        SettingsDialog::StartupOptions startup;
        startup.allowed = true;
        startup.profileDir = env.path("data");
        startup.modelDir = env.path("models");
        startup.state.status = LoginStartupStatus::Error;
        startup.state.message = "Invalid startup locator";
        SettingsDialog dialog(settings, false, startup);
        auto *useCurrent = dialog.findChild<QPushButton *>("useCurrentStartupDirectoriesButton"); QVERIFY(useCurrent);
        QVERIFY(!useCurrent->isHidden());
        QCOMPARE(useCurrent->text(), QString::fromUtf8("使用当前目录登录启动"));
        QCOMPARE(dialog.findChild<QLabel *>("startAtLoginStatus")->toolTip(), startup.state.message);
        QVERIFY(!dialog.startupChangeRequested());
        QVERIFY(!dialog.findChild<QCheckBox *>("startAtLoginCheck")->isEnabled());
        QVERIFY(dialog.settings().startAtLogin);
        useCurrent->click();
        QVERIFY(dialog.startupChangeRequested());
        QVERIFY(dialog.settings().startAtLogin);
    }
    void languageChangePreservesDirectoryPathSpellings() {
        TestEnvironment env("gui-settings-path-spelling"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        AppSettings settings; settings.language = "en"; settings.includeCode = true;
        const auto originalPath = [&env](const QString &name) {
            return QDir::toNativeSeparators(env.path(name) + "/./");
        };
        for (const auto &name : {"ordinary", "excluded", "code", "code-excluded"})
            QVERIFY(QDir().mkpath(env.path(name)));
        settings.roots = {originalPath("ordinary")};
        settings.excludes = {originalPath("excluded")};
        settings.codeRoots = {originalPath("code")};
        settings.codeExcludes = {originalPath("code-excluded")};
        AppSettings parsed; QString error;
        QVERIFY2(AppSettings::fromJson(settings.toJson(), &parsed, &error), qPrintable(error));
        QCOMPARE(parsed.fingerprint(), settings.fingerprint());
        QVERIFY(settings.roots.first() != QDir::cleanPath(settings.roots.first()));

        SettingsDialog dialog(parsed, false, {}); dialog.show();
        auto *tabs = dialog.findChild<QTabWidget *>("settingsTabs"); QVERIFY(tabs);
        tabs->setCurrentIndex(tabs->count() - 1);
        auto *language = dialog.findChild<QComboBox *>("languageCombo"); QVERIFY(language);
        language->setCurrentIndex(language->findData("zh_CN"));
        QSignalSpy accepted(&dialog, &QDialog::accepted);
        QTest::mouseClick(dialog.findChild<QPushButton *>("saveSettingsButton"), Qt::LeftButton);
        QCOMPARE(accepted.count(), 1);
        const auto saved = dialog.settings();
        QCOMPARE(saved.language, QString("zh_CN"));
        QCOMPARE(saved.roots, settings.roots);
        QCOMPARE(saved.excludes, settings.excludes);
        QCOMPARE(saved.codeRoots, settings.codeRoots);
        QCOMPARE(saved.codeExcludes, settings.codeExcludes);
        QCOMPARE(saved.fingerprint(), settings.fingerprint());
        QVERIFY2(AppSettings::fromJson(saved.toJson(), &parsed, &error), qPrintable(error));
        QCOMPARE(parsed.fingerprint(), settings.fingerprint());
    }
    void folderEditsPreserveExistingSpellings_data() {
        QTest::addColumn<QString>("listName");
        QTest::addColumn<QString>("settingsKey");
        QTest::addColumn<int>("tabIndex");
        QTest::newRow("roots") << QString("rootFolders") << QString("roots") << 0;
        QTest::newRow("excludes") << QString("excludedFolders") << QString("excludes") << 0;
        QTest::newRow("code-roots") << QString("codeRootFolders") << QString("codeRoots") << 1;
        QTest::newRow("code-excludes") << QString("codeExcludedFolders") << QString("codeExcludes") << 1;
    }
    void folderEditsPreserveExistingSpellings() {
        QFETCH(QString, listName);
        QFETCH(QString, settingsKey);
        QFETCH(int, tabIndex);
        TestEnvironment env("gui-settings-folder-edits"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        const QString existing = env.path("existing");
        const QString added = env.path("added");
        QVERIFY(QDir().mkpath(existing)); QVERIFY(QDir().mkpath(added));
        const QString spelling = QDir::toNativeSeparators(QFileInfo(existing).canonicalFilePath() + "/./");
        AppSettings settings; settings.language = "en"; settings.includeCode = true;
        auto json = settings.toJson(); json.insert(settingsKey, QJsonArray{spelling});
        QString error;
        QVERIFY2(AppSettings::fromJson(json, &settings, &error), qPrintable(error));
        const QString fingerprint = settings.fingerprint();
        SettingsDialog dialog(settings, false, {}); dialog.show();
        auto *tabs = dialog.findChild<QTabWidget *>("settingsTabs"); QVERIFY(tabs);
        tabs->setCurrentIndex(tabIndex);
        auto *list = dialog.findChild<QListWidget *>(listName); QVERIFY(list);
        auto *remove = dialog.findChild<QPushButton *>(listName + "Remove"); QVERIFY(remove);
        QCOMPARE(list->count(), 1);
        QCOMPARE(list->item(0)->text(), spelling);
        QCOMPARE(dialog.settings().fingerprint(), fingerprint);

        // Model the normalized item returned by the picker without opening
        // QFileDialog, which reads and writes native QtProject user settings.
        const QString canonicalAdded = QDir::cleanPath(QFileInfo(added).canonicalFilePath());
        list->addItem(canonicalAdded);
        QCOMPARE(list->count(), 2);
        QCOMPARE(dialog.settings().toJson().value(settingsKey).toArray(), QJsonArray({spelling, canonicalAdded}));
        QVERIFY(dialog.settings().fingerprint() != fingerprint);
        list->setCurrentRow(1);
        QTest::mouseClick(remove, Qt::LeftButton);
        QCOMPARE(list->count(), 1);
        QCOMPARE(dialog.settings().toJson().value(settingsKey).toArray(), QJsonArray({spelling}));
        QCOMPARE(dialog.settings().fingerprint(), fingerprint);
        list->setCurrentRow(0);
        QTest::mouseClick(remove, Qt::LeftButton);
        QVERIFY(dialog.settings().toJson().value(settingsKey).toArray().isEmpty());
        QVERIFY(dialog.settings().fingerprint() != fingerprint);
    }
    void inputMethodPreeditDoesNotSearch() {
        TestEnvironment env("gui-ime"); QVERIFY(env.isValid());
        FakeBackend backend;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        QTRY_VERIFY(backend.count("files.list") > 0);
        auto *query = window.findChild<SearchEdit *>("searchInput");
        query->setFocus();
        const int before = backend.count("search") + backend.count("files.list");
        QInputMethodEvent preedit(QString::fromUtf8("中文"), {});
        QApplication::sendEvent(query, &preedit);
        QVERIFY(query->isComposing());
        QTest::qWait(350);
        QCOMPARE(backend.count("search") + backend.count("files.list"), before);
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
        const int before = backend.count("search") + backend.count("files.list");
        if (change == "query") query->setText(QString::fromUtf8("兰花港口"));
        else if (change == "clear-query") query->clear();
        else if (change == "kind") window.findChild<QComboBox *>("kindFilter")->setCurrentIndex(1);
        else if (change == "root") window.findChild<QComboBox *>("rootFilter")->setCurrentIndex(1);
        else window.findChild<QCheckBox *>("codeSearchCheck")->setChecked(true);
        QTRY_VERIFY(backend.count("search") + backend.count("files.list") > before);
        QTRY_COMPARE(results->item(0)->data(Qt::UserRole).toString(), expected.path);
        QCOMPARE(results->currentRow(), 0);
        QCOMPARE(results->verticalScrollBar()->value(), results->verticalScrollBar()->minimum());
        QCOMPARE(window.findChild<PreviewPanel *>()->currentHit().path, expected.path);
        QTRY_COMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), expected.text);
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
        preview->setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"text", expanded}, {"partial", false}, {"next", 0}}, false);
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
    void indexPublicationPreservesDocumentContinuation_data() {
        QTest::addColumn<QString>("initialVersion");
        QTest::addColumn<QString>("publishedVersion");
        QTest::addColumn<bool>("previewArrivesFirst");
        const QString base = "source-fingerprint|pipeline-fingerprint";
        QTest::newRow("same-partial-progress") << base + "|partial:40" << base + "|partial:40" << false;
        QTest::newRow("advanced-partial-progress") << base + "|partial:40" << base + "|partial:80" << false;
        QTest::newRow("completed-publication") << base + "|partial:40" << base << false;
        QTest::newRow("unchanged-complete-version") << base << base << false;
        QTest::newRow("preview-before-search-refresh") << base + "|partial:40" << base + "|partial:80" << true;
    }
    void indexPublicationPreservesDocumentContinuation() {
        QFETCH(QString, initialVersion);
        QFETCH(QString, publishedVersion);
        QFETCH(bool, previewArrivesFirst);
        TestEnvironment env("gui-publication-continuation"); QVERIFY(env.isValid());
        QVERIFY(env.writeFile("document.txt", "matching passage"));
        FakeBackend backend; backend.autoPreview = false;
        SearchHit hit; hit.path = env.path("document.txt"); hit.title = "Initial title";
        hit.kind = "text"; hit.text = "matching passage"; hit.segmentId = 10; hit.sourceVersion = initialVersion;
        backend.hits.append(hit);
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("matching passage");
        auto *preview = window.findChild<PreviewPanel *>();
        auto *body = window.findChild<QTextBrowser *>("textPreview");
        auto *button = window.findChild<QPushButton *>("loadDocumentPreviewButton");
        QTRY_COMPARE(preview->currentHit().segmentId, hit.segmentId);
        button->click();
        const auto first = backend.last("document.preview");
        QCOMPARE(first.params.value("start").toInt(), 0);
        const QString firstBody = hit.text + "\nFirst document batch";
        emit backend.response(first.id, {{"sourceVersion", "preview-source-v1"}, {"text", firstBody}, {"partial", true}, {"next", 100}});
        QCOMPARE(body->toPlainText(), firstBody);
        QCOMPARE(button->text(), QString("Continue reading"));
        QVERIFY(button->isEnabled());
        auto cursor = body->textCursor();
        const int readingPosition = int(firstBody.indexOf("First document batch"));
        cursor.setPosition(readingPosition);
        body->setTextCursor(cursor);

        auto &published = backend.hits.first();
        published.segmentId = 20; published.sourceVersion = publishedVersion;
        published.title = "Published title"; published.score = 0.75;
        int searches = backend.count("search");
        emit backend.eventReceived("indexChanged", {{"path", hit.path}});
        QTRY_VERIFY(backend.count("search") > searches);
        QTRY_COMPARE(preview->currentHit().toJson(), published.toJson());
        QCOMPARE(body->toPlainText(), firstBody);
        QCOMPARE(body->textCursor().position(), readingPosition);
        QCOMPARE(button->text(), QString("Continue reading"));
        QCOMPARE(backend.count("document.preview"), 1);

        button->click();
        const auto second = backend.last("document.preview");
        QCOMPARE(second.params.value("start").toInt(), 100);
        QCOMPARE(button->text(), QString("Loading text…"));
        QVERIFY(!button->isEnabled());
        published.segmentId = 30; published.sourceVersion = "source-fingerprint|pipeline-fingerprint";
        published.title = "Completed title"; published.score = 0.9;
        backend.autoSearch = false;
        searches = backend.count("search");
        emit backend.eventReceived("indexChanged", {{"path", hit.path}});
        QTRY_VERIFY(backend.count("search") > searches);
        const auto refresh = backend.last("search");
        const QJsonObject continuation{{"sourceVersion", "preview-source-v1"}, {"text", "Second document batch"}, {"partial", true}, {"next", 200}};
        const QString expanded = firstBody + "\nSecond document batch";
        if (previewArrivesFirst) emit backend.response(second.id, continuation);
        emit backend.response(refresh.id, {{"hits", QJsonArray{published.toJson()}}, {"semantic", false}});
        QCOMPARE(preview->currentHit().toJson(), published.toJson());
        QVERIFY(!backend.cancelled.contains(second.id));
        QCOMPARE(backend.count("document.preview"), 2);
        QCOMPARE(body->toPlainText(), previewArrivesFirst ? expanded : firstBody);
        QCOMPARE(body->textCursor().position(), readingPosition);
        QCOMPARE(button->text(), previewArrivesFirst ? QString("Continue reading") : QString("Loading text…"));

        if (!previewArrivesFirst) emit backend.response(second.id, continuation);
        QCOMPARE(body->toPlainText(), expanded);
        QCOMPARE(body->textCursor().position(), readingPosition);
        QCOMPARE(button->text(), QString("Continue reading"));
        QVERIFY(button->isEnabled());
        emit backend.response(second.id, continuation);
        emit backend.response(first.id, {{"sourceVersion", "preview-source-v1"}, {"text", "Late initial response"}, {"partial", false}, {"next", 0}});
        QCOMPARE(body->toPlainText(), expanded);
        button->click();
        QCOMPARE(backend.count("document.preview"), 3);
        QCOMPARE(backend.last("document.preview").params.value("start").toInt(), 200);
    }
    void changedHitResetsDocumentContinuation_data() {
        QTest::addColumn<QString>("change");
        QTest::addColumn<QString>("initialVersion");
        QTest::addColumn<QString>("publishedVersion");
        const QString base = "source-fingerprint|pipeline-fingerprint";
        const QString partial = base + "|partial:40";
        for (const auto *change : {"path", "kind", "text", "page", "lineStart", "lineEnd", "cell", "startMs", "endMs"})
            QTest::newRow(change) << QString::fromLatin1(change) << partial << partial;
        QTest::newRow("source-fingerprint") << QString() << partial << QString("new-source|pipeline-fingerprint|partial:80");
        QTest::newRow("pipeline-fingerprint") << QString() << partial << QString("source-fingerprint|new-pipeline|partial:80");
        QTest::newRow("partial-regression") << QString() << partial << base + "|partial:20";
        QTest::newRow("complete-to-partial") << QString() << base << partial;
        QTest::newRow("failed") << QString() << partial << base + "|failed:80";
        QTest::newRow("capped") << QString() << partial << base + "|capped:5000";
        QTest::newRow("unknown-suffix") << QString() << partial << base + "|unknown:80";
        QTest::newRow("zero-partial") << QString() << partial << base + "|partial:0";
        QTest::newRow("negative-partial") << QString() << partial << base + "|partial:-1";
        QTest::newRow("trailing-suffix") << QString() << partial << base + "|partial:80|unexpected";
        QTest::newRow("unknown-to-complete") << QString() << base + "|unknown:40" << base;
        QTest::newRow("failed-to-complete") << QString() << base + "|failed:40" << base;
        QTest::newRow("capped-to-complete") << QString() << base + "|capped:5000" << base;
        QTest::newRow("partial-with-newline-to-complete") << QString() << partial + "\n" << base;
        QTest::newRow("nested-failed-partial") << QString() << base + "|failed:40|partial:80" << base + "|failed:40";
    }
    void changedHitResetsDocumentContinuation() {
        QFETCH(QString, change);
        QFETCH(QString, initialVersion);
        QFETCH(QString, publishedVersion);
        TestEnvironment env("gui-changed-document-continuation"); QVERIFY(env.isValid());
        QVERIFY(env.writeFile("document.txt", "matching passage"));
        QVERIFY(env.writeFile("other.txt", "matching passage"));
        FakeBackend backend; backend.autoPreview = false;
        SearchHit hit; hit.path = env.path("document.txt"); hit.kind = "text"; hit.text = "matching passage";
        hit.segmentId = 10; hit.sourceVersion = initialVersion; backend.hits.append(hit);
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("matching passage");
        auto *preview = window.findChild<PreviewPanel *>();
        auto *body = window.findChild<QTextBrowser *>("textPreview");
        auto *button = window.findChild<QPushButton *>("loadDocumentPreviewButton");
        QTRY_COMPARE(preview->currentHit().segmentId, hit.segmentId);
        button->click();
        const auto first = backend.last("document.preview");
        emit backend.response(first.id, {{"sourceVersion", "preview-source-v1"}, {"text", hit.text + "\nFirst document batch"}, {"partial", true}, {"next", 100}});
        QCOMPARE(button->text(), QString("Continue reading"));
        button->click();
        const auto continuation = backend.last("document.preview");
        QCOMPARE(continuation.params.value("start").toInt(), 100);

        auto &published = backend.hits.first();
        published.segmentId = 20; published.sourceVersion = publishedVersion;
        if (change == "path") published.path = env.path("other.txt");
        else if (change == "kind") published.kind = "code";
        else if (change == "text") published.text = "different passage";
        else if (change == "page") published.page = 2;
        else if (change == "lineStart") published.lineStart = 12;
        else if (change == "lineEnd") published.lineEnd = 15;
        else if (change == "cell") published.cell = 3;
        else if (change == "startMs") published.startMs = 1200;
        else if (change == "endMs") published.endMs = 2400;
        const int searches = backend.count("search");
        emit backend.eventReceived("indexChanged", {{"path", published.path}});
        QTRY_VERIFY(backend.count("search") > searches);
        QTRY_COMPARE(preview->currentHit().toJson(), published.toJson());
        QVERIFY(backend.cancelled.contains(continuation.id));
        QCOMPARE(body->toPlainText(), published.text);
        QCOMPARE(button->text(), QString("Read document text"));
        QVERIFY(button->isEnabled());
        emit backend.response(continuation.id, {{"sourceVersion", "preview-source-v1"}, {"text", "Late old document batch"}, {"partial", true}, {"next", 200}});
        QCOMPARE(body->toPlainText(), published.text);
        QCOMPARE(button->text(), QString("Read document text"));
        button->click();
        QCOMPARE(backend.last("document.preview").params.value("start").toInt(), 0);
        QCOMPARE(backend.last("document.preview").params.value("path").toString(), published.path);
    }
    void chineseDialogButtonsUseApplicationLanguage() {
        TestEnvironment env("gui-chinese-buttons"); QVERIFY(env.isValid());
        AppSettings settings; settings.language = "zh_CN";
        SettingsDialog preferences(settings, false, {});
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
        window.findChild<SearchEdit *>("searchInput")->setText("semantic question");
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
    void testSessionWithCustomHomeCannotManageOperatingSystemStartup() {
        TestEnvironment env("gui-startup-profile"); QVERIFY(env.isValid());
        qcopilots_search::test::ScopedEnvironment overrideProfile("QCS_HOME", env.path("environment profile").toUtf8());
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
        backend.settings.includeCode = true;
        backend.settings.roots << env.path("documents");
        backend.settings.codeRoots << env.path("source code");
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
        backend.autoSearch = false;
        auto *kind = window.findChild<QComboBox *>("kindFilter");
        auto *root = window.findChild<QComboBox *>("rootFilter");
        auto *code = window.findChild<QCheckBox *>("codeSearchCheck");
        kind->setCurrentIndex(kind->findData("pdf"));
        code->setChecked(true);
        root->setCurrentIndex(1);
        input->setText("updated quick query");
        QTRY_COMPARE(backend.last("search").params.value("root").toString(), env.path("source code"));
        QVERIFY(backend.last("search").params.value("code").toBool());
        const auto oldMain = backend.last("search");
        window.showQuickSearch();
        auto *quick = window.findChild<SearchEdit *>("quickSearchInput"); QVERIFY(quick);
        quick->setText("quick query");
        QTRY_COMPARE(backend.last("search").params.value("query").toString(), QString("quick query"));
        QCOMPARE(backend.last("search").params.value("limit").toInt(), 12);
        const auto oldQuick = backend.last("search");
        QVERIFY(!oldQuick.params.value("code").toBool());
        QCOMPARE(oldQuick.params.value("kind").toString(), QString());
        QCOMPARE(oldQuick.params.value("root").toString(), QString());
        quick->setText("updated quick query");
        const int searchesBefore = backend.count("search");
        QTest::mouseClick(window.findChild<QPushButton *>("openFullSearchButton"), Qt::LeftButton);
        QCOMPARE(input->text(), QString("updated quick query"));
        QVERIFY(!code->isChecked());
        QVERIFY(kind->isEnabled());
        QCOMPARE(kind->currentData().toString(), QString());
        QCOMPARE(root->currentData().toString(), QString());
        QCOMPARE(root->itemData(1).toString(), env.path("documents"));
        QCOMPARE(backend.count("search"), searchesBefore + 1);
        auto expected = oldQuick.params;
        expected.insert("query", "updated quick query");
        expected.insert("limit", 100);
        const auto fullSearch = backend.last("search");
        QCOMPARE(fullSearch.params, expected);
        QVERIFY(backend.cancelled.contains(oldMain.id));
        QVERIFY(backend.cancelled.contains(oldQuick.id));
        QJsonArray freshHits;
        SearchHit fresh = hit; fresh.title = "Current full search";
        freshHits.append(fresh.toJson());
        emit backend.response(fullSearch.id, {{"hits", freshHits}, {"semantic", false}});
        QCOMPARE(results->count(), 1);
        QVERIFY(results->item(0)->text().contains(fresh.title));
        emit backend.response(oldMain.id, {{"hits", QJsonArray{}}, {"semantic", false}});
        emit backend.response(oldQuick.id, {{"hits", QJsonArray{}}, {"semantic", false}});
        QCOMPARE(results->count(), 1);
        QVERIFY(results->item(0)->text().contains(fresh.title));
        QTest::qWait(300);
        QCOMPARE(backend.count("search"), searchesBefore + 1);
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
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"text", "first body"}, {"partial", true}, {"next", 100}}, false);
        QVERIFY(button->isEnabled());
        button->click();
        QCOMPARE(requests.last().at(2).toInt(), 100);
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"text", "second body"}, {"partial", false}, {"next", 0}}, true);
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString("first body\nsecond body"));
        QVERIFY(!button->isEnabled());
        preview.setLanguage("zh_CN");
        QCOMPARE(button->text(), QString::fromUtf8("正文已读取"));
    }
    void documentContinuationRejectsChangedSource_data() {
        QTest::addColumn<QString>("returnedVersion");
        QTest::addColumn<bool>("emptyRestart");
        QTest::newRow("changed-source") << QString("preview-source-v2") << false;
        QTest::newRow("missing-source") << QString() << false;
        QTest::newRow("empty-source-after-restart") << QString("preview-source-v2") << true;
    }
    void documentContinuationRejectsChangedSource() {
        QFETCH(QString, returnedVersion);
        QFETCH(bool, emptyRestart);
        TestEnvironment env("gui-preview-source-version"); QVERIFY(env.isValid());
        QVERIFY(env.writeFile("document.txt", "matching passage"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("document.txt"); hit.kind = "text";
        hit.text = emptyRestart ? QString() : QString("matching passage");
        preview.showHit(hit);
        QSignalSpy requests(&preview, &PreviewPanel::documentRequested);
        auto *button = preview.findChild<QPushButton *>("loadDocumentPreviewButton");
        auto *body = preview.findChild<QTextBrowser *>("textPreview");
        auto *error = preview.findChild<QLabel *>("previewError");
        button->click();
        const QString firstBody = hit.text + "\nOriginal source body";
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"text", firstBody}, {"partial", true}, {"next", 100}}, false);
        button->click();
        QCOMPARE(requests.last().at(2).toInt(), 100);
        QJsonObject changed{{"text", "Do not append this unverified batch"}, {"partial", true}, {"next", 200}};
        if (!returnedVersion.isEmpty()) changed.insert("sourceVersion", returnedVersion);
        preview.setDocumentPreview(changed, true);
        QCOMPARE(body->toPlainText(), firstBody);
        QCOMPARE(button->text(), QString("Retry reading"));
        QVERIFY(button->isEnabled());
        QVERIFY(!error->isHidden());
        QVERIFY(error->text().contains("beginning"));

        button->click();
        QCOMPARE(requests.count(), 3);
        QCOMPARE(requests.last().at(2).toInt(), 0);
        const QString newBody = emptyRestart ? QString() : "New source prefix\n" + hit.text + "\nReplacement source body";
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v2"}, {"text", newBody}, {"partial", false}, {"next", 0}}, false);
        QCOMPARE(body->toPlainText(), newBody);
        QCOMPARE(body->textCursor().position(), int(newBody.indexOf(hit.text)));
        QCOMPARE(button->text(), QString("Document loaded"));
        QVERIFY(!button->isEnabled());
        QVERIFY(error->isHidden());
        QVERIFY(error->text().isEmpty());
    }
    void documentPreviewRejectsMissingInitialSourceVersion() {
        TestEnvironment env("gui-preview-missing-source-version"); QVERIFY(env.isValid());
        QVERIFY(env.writeFile("document.txt", "matching passage"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("document.txt"); hit.kind = "text"; hit.text = "matching passage";
        preview.showHit(hit);
        QSignalSpy requests(&preview, &PreviewPanel::documentRequested);
        auto *button = preview.findChild<QPushButton *>("loadDocumentPreviewButton");
        button->click();
        preview.setDocumentPreview({{"text", "Unverified initial body"}, {"partial", true}, {"next", 100}}, false);
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), hit.text);
        QCOMPARE(button->text(), QString("Retry reading"));
        QVERIFY(button->isEnabled());
        QVERIFY(!preview.findChild<QLabel *>("previewError")->isHidden());
        button->click();
        QCOMPARE(requests.count(), 2);
        QCOMPARE(requests.last().at(2).toInt(), 0);
    }
    void browsingDocumentLoadsOneBatchWithoutInventingSearchLocation() {
        TestEnvironment env("gui-browse-document-batch"); QVERIFY(env.isValid()); QVERIFY(env.writeFile("large.txt", "body"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("large.txt"); hit.kind = "text";
        preview.showHit(hit);
        QSignalSpy requests(&preview, &PreviewPanel::documentRequested);
        preview.loadDocumentText();
        QCOMPARE(requests.count(), 1);
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"segments", QJsonArray{QJsonObject{{"text", "first batch"}}}}, {"partial", true}, {"next", 100}}, false);
        QCoreApplication::processEvents();
        QCOMPARE(requests.count(), 1);
        QVERIFY(preview.findChild<QPushButton *>("loadDocumentPreviewButton")->isEnabled());
        preview.loadDocumentText();
        QCOMPARE(requests.count(), 2);
        QCOMPARE(requests.last().at(2).toInt(), 100);
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
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"segments", segments}, {"partial", false}, {"next", 0}}, false);
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
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"segments", QJsonArray{QJsonObject{{"text", "first batch"}, {"lineStart", 1}, {"lineEnd", 3}}}}, {"partial", true}, {"next", 100}}, false);
        QVERIFY(!button->isEnabled());
        QTRY_COMPARE(requests.count(), 2);
        QCOMPARE(requests.last().at(2).toInt(), 100);
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"segments", QJsonArray{QJsonObject{{"text", "target();"}, {"lineStart", 5005}, {"lineEnd", 5006}}}}, {"partial", false}, {"next", 0}}, true);
        auto *body = preview.findChild<QTextBrowser *>("textPreview");
        QCOMPARE(body->textCursor().position(), int(body->toPlainText().indexOf("target();")));
        QVERIFY(body->textCursor().position() > 0);
        QCOMPARE(button->text(), QString("Document loaded"));
    }
    void clearingPreviewCancelsQueuedAutomaticContinuation() {
        TestEnvironment env("gui-cancel-automatic-continuation"); QVERIFY(env.isValid());
        QVERIFY(env.writeFile("code.cpp", "source"));
        QVERIFY(env.writeFile("other.cpp", "other source"));
        PreviewPanel preview; SearchHit hit; hit.path = env.path("code.cpp"); hit.kind = "code";
        hit.text = "target();"; hit.lineStart = 5005; hit.lineEnd = 5006;
        preview.showHit(hit);
        QSignalSpy requests(&preview, &PreviewPanel::documentRequested);
        preview.loadDocumentText();
        QCOMPARE(requests.count(), 1);
        preview.setDocumentPreview({{"sourceVersion", "preview-source-v1"}, {"segments", QJsonArray{QJsonObject{{"text", "first batch"}, {"lineStart", 1}, {"lineEnd", 3}}}},
            {"partial", true}, {"next", 100}}, false);
        preview.clear();
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), QString());
        QVERIFY(preview.currentHit().path.isEmpty());
        SearchHit next = hit; next.path = env.path("other.cpp"); next.text = "other target();";
        preview.showHit(next);
        preview.loadDocumentText();
        QCOMPARE(requests.count(), 2);
        QCOMPARE(requests.last().at(0).toString(), next.path);
        QCoreApplication::processEvents();
        QCOMPARE(requests.count(), 2);
        QCOMPARE(preview.currentHit().path, next.path);
        QCOMPARE(preview.findChild<QTextBrowser *>("textPreview")->toPlainText(), next.text);
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
        QTRY_VERIFY(!imagePreviewPixmap(preview).isNull());
        preview.clear();
        QVERIFY(imagePreviewPixmap(preview).isNull());
        for (const char *name : {"imageZoomOutButton", "imageZoomInButton", "imageActualSizeButton", "imageFitButton"})
            QVERIFY(!preview.findChild<QPushButton *>(name)->isEnabled());
        QVERIFY(QFile::remove(path));
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
            QTRY_VERIFY_WITH_TIMEOUT(!imagePreviewPixmap(preview).isNull(), 10000);
            QCOMPARE(imagePreviewPixmap(preview).toImage().pixelColor(0, 0), QColor(Qt::green));
            QCOMPARE(preview.currentHit().path, imagePath);
            preview.clear();
        }
        QVERIFY(QFile::remove(pdfPath));
        QVERIFY(QFile::remove(imagePath));
        QVERIFY(QFile::remove(text.path));
    }
    void imagePreviewResizeRemainsBounded_data() {
        QTest::addColumn<QSize>("imageSize");
        QTest::newRow("landscape") << QSize(1200, 300);
        QTest::newRow("portrait") << QSize(300, 1200);
    }
    void imagePreviewResizeRemainsBounded() {
        QFETCH(QSize, imageSize);
        TestEnvironment env("gui-image-resize"); QVERIFY(env.isValid());
        const QString path = env.path("wide image.png");
        QImage pixels(imageSize, QImage::Format_RGB32); pixels.fill(Qt::blue); QVERIFY(pixels.save(path));
        PreviewPanel preview; preview.resize(760, 560); preview.show();
        SearchHit hit; hit.path = path; hit.kind = "image"; preview.showHit(hit);
        auto *image = preview.findChild<QGraphicsView *>("imagePreview"); QVERIFY(image);
        QTRY_VERIFY_WITH_TIMEOUT(!imagePreviewPixmap(preview).isNull(), 10000);
        for (const QSize size : {QSize(760, 560), QSize(600, 460), QSize(900, 620), QSize(620, 480)}) {
            preview.resize(size);
            QCoreApplication::processEvents();
            const QSize settled = preview.size();
            for (int pass = 0; pass < 3; ++pass) QCoreApplication::processEvents();
            QCOMPARE(preview.size(), settled);
            QVERIFY(!imagePreviewPixmap(preview).isNull());
            const QSizeF displayed = image->transform().mapRect(image->sceneRect()).size();
            QVERIFY(displayed.width() <= image->viewport()->width());
            QVERIFY(displayed.height() <= image->viewport()->height());
            QCOMPARE(image->horizontalScrollBar()->maximum(), 0);
            QCOMPARE(image->verticalScrollBar()->maximum(), 0);
        }
    }
    void imageZoomUsesScrollableViewportAndKeepsBoundedPixels() {
        TestEnvironment env("gui-image-zoom"); QVERIFY(env.isValid());
        const QString path = env.path("large image.png");
        QImage pixels(1400, 1000, QImage::Format_RGB32); pixels.fill(Qt::blue); QVERIFY(pixels.save(path));
        PreviewPanel preview; preview.resize(760, 560); preview.show();
        SearchHit hit; hit.path = path; hit.kind = "image"; preview.showHit(hit);
        auto *view = preview.findChild<QGraphicsView *>("imagePreview"); QVERIFY(view);
        auto *zoomIn = preview.findChild<QPushButton *>("imageZoomInButton"); QVERIFY(zoomIn);
        auto *zoomOut = preview.findChild<QPushButton *>("imageZoomOutButton"); QVERIFY(zoomOut);
        auto *actual = preview.findChild<QPushButton *>("imageActualSizeButton"); QVERIFY(actual);
        auto *fit = preview.findChild<QPushButton *>("imageFitButton"); QVERIFY(fit);
        QTRY_VERIFY_WITH_TIMEOUT(actual->isEnabled(), 10000);
        QVERIFY(fit->isChecked());
        QTRY_VERIFY(view->transform().m11() < 1.0);
        const qreal initialFit = view->transform().m11();
        zoomOut->click();
        QVERIFY(!fit->isChecked());
        QVERIFY(view->transform().m11() < initialFit);
        actual->click();
        QCOMPARE(view->transform().m11(), 1.0);
        QTRY_VERIFY(view->horizontalScrollBar()->maximum() > 0);
        QTRY_VERIFY(view->verticalScrollBar()->maximum() > 0);
        view->horizontalScrollBar()->setValue(100);
        view->verticalScrollBar()->setValue(120);
        QCOMPARE(view->horizontalScrollBar()->value(), 100);
        QCOMPARE(view->verticalScrollBar()->value(), 120);
        zoomIn->click();
        QCOMPARE(view->transform().m11(), 1.25);
        preview.resize(900, 620);
        QCoreApplication::processEvents();
        QCOMPARE(view->transform().m11(), 1.25);
        for (int n = 0; n < 12; ++n) zoomIn->click();
        QCOMPARE(view->transform().m11(), 4.0);
        QVERIFY(!zoomIn->isEnabled());
        QCOMPARE(imagePreviewPixmap(preview).size(), pixels.size());
        for (int n = 0; n < 25; ++n) zoomOut->click();
        QCOMPARE(view->transform().m11(), 0.05);
        QVERIFY(!zoomOut->isEnabled());
        fit->click();
        QVERIFY(fit->isChecked());
        QTRY_VERIFY(view->transform().mapRect(view->sceneRect()).width() <= view->viewport()->width());
        QTRY_VERIFY(view->transform().mapRect(view->sceneRect()).height() <= view->viewport()->height());
        QCOMPARE(view->horizontalScrollBar()->maximum(), 0);
        QCOMPARE(view->verticalScrollBar()->maximum(), 0);
    }
    void switchingImagesResetsZoomAndScrollAndReleasesPixels() {
        TestEnvironment env("gui-image-zoom-reset"); QVERIFY(env.isValid());
        QImage large(1400, 1000, QImage::Format_RGB32); large.fill(Qt::blue);
        QImage small(32, 16, QImage::Format_RGB32); small.fill(Qt::green);
        const QString largePath = env.path("large.png"), smallPath = env.path("small@2x.png");
        QVERIFY(large.save(largePath)); QVERIFY(small.save(smallPath));
        QVERIFY(env.writeFile("text.txt", "after image"));
        PreviewPanel preview; preview.resize(760, 560); preview.show();
        SearchHit hit; hit.path = largePath; hit.kind = "image"; preview.showHit(hit);
        auto *view = preview.findChild<QGraphicsView *>("imagePreview"); QVERIFY(view);
        auto *actual = preview.findChild<QPushButton *>("imageActualSizeButton"); QVERIFY(actual);
        auto *fit = preview.findChild<QPushButton *>("imageFitButton"); QVERIFY(fit);
        QTRY_VERIFY_WITH_TIMEOUT(actual->isEnabled(), 10000);
        actual->click();
        view->horizontalScrollBar()->setValue(100);
        view->verticalScrollBar()->setValue(120);
        hit.path = smallPath; preview.showHit(hit);
        QVERIFY(!actual->isEnabled());
        QTRY_VERIFY_WITH_TIMEOUT(actual->isEnabled(), 10000);
        QVERIFY(fit->isChecked());
        QCOMPARE(imagePreviewPixmap(preview).size(), small.size());
        QCOMPARE(imagePreviewPixmap(preview).devicePixelRatio(), 1.0);
        QCOMPARE(view->sceneRect().size(), QSizeF(small.size()));
        QCOMPARE(view->horizontalScrollBar()->value(), 0);
        QCOMPARE(view->verticalScrollBar()->value(), 0);
        QCOMPARE(view->horizontalScrollBar()->maximum(), 0);
        QCOMPARE(view->verticalScrollBar()->maximum(), 0);
        SearchHit text; text.path = env.path("text.txt"); text.kind = "text"; text.text = "after image";
        preview.showHit(text);
        QVERIFY(imagePreviewPixmap(preview).isNull());
        QVERIFY(!actual->isEnabled());
        QVERIFY(QFile::remove(largePath)); QVERIFY(QFile::remove(smallPath));
    }
    void imageZoomSurvivesStatusLanguageAndSameResultRefresh() {
        TestEnvironment env("gui-image-refresh"); QVERIFY(env.isValid());
        const QString path = env.path("image.png");
        QImage pixels(1600, 1200, QImage::Format_RGB32); pixels.fill(Qt::blue); QVERIFY(pixels.save(path));
        FakeBackend backend; backend.autoStatus = false; backend.settings.roots << env.root();
        SearchHit hit; hit.path = path; hit.kind = "image"; hit.sourceVersion = "same-source";
        backend.hits = {hit};
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("image");
        auto *results = window.findChild<QListWidget *>("searchResults"); QVERIFY(results);
        QTRY_COMPARE(results->count(), 1);
        QTRY_VERIFY(backend.count("status") >= 1);
        const QString heldStatus = backend.last("status").id;
        auto *view = window.findChild<QGraphicsView *>("imagePreview"); QVERIFY(view);
        auto *actual = window.findChild<QPushButton *>("imageActualSizeButton"); QVERIFY(actual);
        auto *fit = window.findChild<QPushButton *>("imageFitButton"); QVERIFY(fit);
        QTRY_VERIFY_WITH_TIMEOUT(actual->isEnabled(), 10000);
        actual->click();
        window.findChild<QPushButton *>("imageZoomInButton")->click();
        QTRY_VERIFY(view->horizontalScrollBar()->maximum() >= 100);
        QTRY_VERIFY(view->verticalScrollBar()->maximum() >= 120);
        view->horizontalScrollBar()->setValue(100);
        view->verticalScrollBar()->setValue(120);
        emit backend.response(heldStatus, {{"modelAvailable", false}});
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(view->transform().m11(), 1.25);
        QCOMPARE(view->horizontalScrollBar()->value(), 100);
        QCOMPARE(view->verticalScrollBar()->value(), 120);
        backend.settings.language = "zh_CN";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCoreApplication::processEvents();
        QCOMPARE(view->transform().m11(), 1.25);
        QVERIFY(!fit->isChecked());
        QCOMPARE(fit->text(), QString::fromUtf8("适配"));
        // Language can resize the toolbar. Preserve the resulting user viewport
        // through the subsequent same-context search response as well.
        const int horizontal = view->horizontalScrollBar()->value();
        const int vertical = view->verticalScrollBar()->value();
        backend.autoSearch = false;
        const int searches = backend.count("search");
        emit backend.eventReceived("indexChanged", {{"browseGeneration", "2"}, {"visibilityEpoch", "1"}});
        QTRY_VERIFY(backend.count("search") > searches);
        SearchHit refreshed = hit;
        refreshed.title = "Refreshed image title";
        emit backend.response(backend.last("search").id, {{"hits", QJsonArray{refreshed.toJson()}}, {"semantic", false}});
        QCOMPARE(results->count(), 1);
        QVERIFY(results->item(0)->text().contains(refreshed.title));
        QCoreApplication::processEvents();
        QCOMPARE(view->transform().m11(), 1.25);
        QCOMPARE(view->horizontalScrollBar()->value(), horizontal);
        QCOMPARE(view->verticalScrollBar()->value(), vertical);
        QVERIFY(!fit->isChecked());
    }
    void playingMediaButtonSurvivesStatusAndLanguageRefresh() {
        TestEnvironment env("gui-media-label"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        QVERIFY(env.writeFile("long audio 中文.wav", silentPcmWave(65)));
        QVERIFY(env.writeFile("document.txt", "document preview"));
        FakeBackend backend; backend.autoStatus = false; backend.settings.roots << env.root();
        SearchHit audio; audio.path = env.path("long audio 中文.wav"); audio.kind = "audio"; audio.startMs = 1000;
        SearchHit document; document.path = env.path("document.txt"); document.kind = "text"; document.text = "document preview";
        backend.hits = {audio, document};
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("media moment");
        auto *results = window.findChild<QListWidget *>("searchResults"); QVERIFY(results);
        QTRY_COMPARE(results->count(), 2);
        QTRY_VERIFY(backend.count("status") >= 1);
        const QString heldStatus = backend.last("status").id;
        QVERIFY(!heldStatus.isEmpty());
        auto *player = window.findChild<QMediaPlayer *>("previewMediaPlayer"); QVERIFY(player);
        auto *button = window.findChild<QPushButton *>("mediaPlayButton"); QVERIFY(button);
        QTRY_COMPARE_WITH_TIMEOUT(player->duration(), qint64(65000), 15000);
        QTRY_COMPARE_WITH_TIMEOUT(player->position(), qint64(1000), 15000);
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
        QCOMPARE(button->text(), QString("Play"));
        QTest::mouseClick(button, Qt::LeftButton);
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        QCOMPARE(button->text(), QString("Pause"));

        // The real startup request remains pending until playback is active.
        // This traverses the same response path as the periodic status refresh.
        emit backend.response(heldStatus, {{"modelAvailable", false}});
        QCOMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        QCOMPARE(button->text(), QString("Pause"));
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        QCOMPARE(button->text(), QString("Pause"));
        backend.settings.language = "zh_CN";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        QCOMPARE(button->text(), QString::fromUtf8("暂停"));
        QTest::mouseClick(button, Qt::LeftButton);
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PausedState);
        QCOMPARE(button->text(), QString::fromUtf8("播放"));
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(player->playbackState(), QMediaPlayer::PausedState);
        QCOMPARE(button->text(), QString::fromUtf8("播放"));
        backend.settings.language = "en";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(player->playbackState(), QMediaPlayer::PausedState);
        QCOMPARE(button->text(), QString("Play"));
        player->stop();
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
        backend.settings.language = "zh_CN";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(button->text(), QString::fromUtf8("播放"));
        backend.settings.language = "en";
        emit backend.eventReceived("settings", backend.settings.toJson());
        QCOMPARE(button->text(), QString("Play"));
        QTest::mouseClick(button, Qt::LeftButton);
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        results->setCurrentRow(1);
        QCOMPARE(player->source(), QUrl());
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
        QCOMPARE(button->text(), QString("Play"));
        QCOMPARE(window.findChild<QTextBrowser *>("textPreview")->toPlainText(), document.text);
        QVERIFY(QFileInfo::exists(audio.path));
        QTRY_VERIFY(!QFileInfo::exists(audio.path) || QFile::remove(audio.path));
    }
    void savingLanguagePreservesPausedMediaAndSelectedRoot() {
        TestEnvironment env("gui-media-settings-save"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        QVERIFY(QDir().mkpath(env.path("other")));
        QVERIFY(env.writeFile("selected/audio 中文.wav", silentPcmWave(65)));
        FakeBackend backend; backend.autoStatus = false;
        backend.settings.roots = {QDir::toNativeSeparators(env.path("other") + "/./"),
            QDir::toNativeSeparators(env.path("selected") + "/./")};
        const auto originalSettings = backend.settings;
        const QString selectedRoot = originalSettings.roots.at(1);
        SearchHit audio; audio.segmentId = 7; audio.path = env.path("selected/audio 中文.wav");
        audio.title = "Paused audio"; audio.kind = "audio"; audio.startMs = 50000;
        audio.sourceVersion = "unchanged-audio";
        backend.hits = {audio};
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        auto *root = window.findChild<QComboBox *>("rootFilter"); QVERIFY(root);
        QTRY_COMPARE(root->count(), 3);
        root->setCurrentIndex(2);
        window.findChild<SearchEdit *>("searchInput")->setText("media moment");
        QTRY_COMPARE(backend.last("search").params.value("root").toString(), selectedRoot);
        auto *results = window.findChild<QListWidget *>("searchResults"); QVERIFY(results);
        QTRY_COMPARE(results->count(), 1);
        auto *preview = window.findChild<PreviewPanel *>("previewPanel"); QVERIFY(preview);
        QTRY_COMPARE(preview->currentHit().toJson(), audio.toJson());
        auto *player = window.findChild<QMediaPlayer *>("previewMediaPlayer"); QVERIFY(player);
        auto *play = window.findChild<QPushButton *>("mediaPlayButton"); QVERIFY(play);
        QTRY_COMPARE_WITH_TIMEOUT(player->duration(), qint64(65000), 15000);
        QTRY_COMPARE_WITH_TIMEOUT(player->position(), audio.startMs, 15000);
        QTest::mouseClick(play, Qt::LeftButton);
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PlayingState);
        QTest::mouseClick(play, Qt::LeftButton);
        QTRY_COMPARE(player->playbackState(), QMediaPlayer::PausedState);
        // A reset to the hit start must be observable even when the same hit remains selected.
        player->setPosition(13824);
        QTRY_COMPARE(player->position(), qint64(13824));
        const auto source = player->source();
        const qint64 position = player->position();
        const int searches = backend.count("search");
        const int browses = backend.count("files.list");
        QSignalSpy sourceChanges(player, &QMediaPlayer::sourceChanged);
        QSignalSpy stateChanges(player, &QMediaPlayer::playbackStateChanged);
        for (const auto &language : {QString("zh_CN"), QString("en")}) {
            auto *settingsButton = window.findChild<QPushButton *>("settingsButton"); QVERIFY(settingsButton);
            QTest::mouseClick(settingsButton, Qt::LeftButton);
            auto *dialog = window.findChild<SettingsDialog *>(); QVERIFY(dialog);
            auto *tabs = dialog->findChild<QTabWidget *>("settingsTabs"); QVERIFY(tabs);
            tabs->setCurrentIndex(tabs->count() - 1);
            auto *languageCombo = dialog->findChild<QComboBox *>("languageCombo"); QVERIFY(languageCombo);
            languageCombo->setCurrentIndex(languageCombo->findData(language));
            const int saves = backend.count("settings.set");
            QTest::mouseClick(dialog->findChild<QPushButton *>("saveSettingsButton"), Qt::LeftButton);
            QTRY_COMPARE(backend.count("settings.set"), saves + 1);
            QTRY_COMPARE(backend.settings.language, language);
            QTRY_COMPARE(settingsButton->text(), language == "en" ? QString("Settings") : QString::fromUtf8("设置"));
            QTRY_VERIFY(window.findChild<SettingsDialog *>() == nullptr);
            // Observe beyond MainWindow's 250 ms debounce after the settings response.
            QTest::qWait(500);
            QCOMPARE(backend.settings.roots, originalSettings.roots);
            QCOMPARE(backend.settings.fingerprint(), originalSettings.fingerprint());
            QCOMPARE(backend.last("settings.set").params.value("roots").toArray(), QJsonArray::fromStringList(originalSettings.roots));
            QCOMPARE(root->currentIndex(), 2);
            QCOMPARE(root->currentData().toString(), selectedRoot);
            QCOMPARE(results->currentRow(), 0);
            QCOMPARE(preview->currentHit().toJson(), audio.toJson());
            QCOMPARE(player->source(), source);
            QCOMPARE(player->position(), position);
            QCOMPARE(player->playbackState(), QMediaPlayer::PausedState);
            QCOMPARE(play->text(), language == "en" ? QString("Play") : QString::fromUtf8("播放"));
            QCOMPARE(sourceChanges.count(), 0);
            QCOMPARE(stateChanges.count(), 0);
            QCOMPARE(backend.count("search"), searches);
            QCOMPARE(backend.count("files.list"), browses);
        }
        preview->clear();
        QTRY_VERIFY(!QFileInfo::exists(audio.path) || QFile::remove(audio.path));
    }
    void mediaClockTracksDurationBeforePlaybackAndClears() {
        TestEnvironment env("gui-media-clock"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        QVERIFY(env.writeFile("long 中文.wav", silentPcmWave(65)));
        QVERIFY(env.writeFile("short 中文.wav", silentPcmWave(2)));
        PreviewPanel preview; preview.show();
        auto *player = preview.findChild<QMediaPlayer *>("previewMediaPlayer"); QVERIFY(player);
        auto *button = preview.findChild<QPushButton *>("mediaPlayButton"); QVERIFY(button);
        auto *clock = preview.findChild<QLabel *>("mediaTimeLabel"); QVERIFY(clock);
        auto *slider = preview.findChild<QSlider *>("mediaPosition"); QVERIFY(slider);
        SearchHit longAudio; longAudio.path = env.path("long 中文.wav"); longAudio.kind = "audio"; longAudio.startMs = 1000;
        preview.showHit(longAudio);
        QTRY_COMPARE_WITH_TIMEOUT(player->duration(), qint64(65000), 15000);
        QTRY_COMPARE_WITH_TIMEOUT(player->position(), qint64(1000), 15000);
        QCOMPARE(clock->text(), QString("0:01 / 1:05"));
        SearchHit shortAudio; shortAudio.path = env.path("short 中文.wav"); shortAudio.kind = "audio";
        preview.showHit(shortAudio);
        QTRY_COMPARE_WITH_TIMEOUT(player->duration(), qint64(2000), 15000);
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
        QCOMPARE(player->position(), qint64(0));
        QCOMPARE(button->text(), QString("Play"));
        QCOMPARE(slider->minimum(), 0);
        QCOMPARE(slider->maximum(), 2000);
        QCOMPARE(slider->value(), 0);
        // Duration must be correct before the first playback position signal.
        QCOMPARE(clock->text(), QString("0:00 / 0:02"));
        preview.clear();
        QCOMPARE(player->source(), QUrl());
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
        QCOMPARE(slider->minimum(), 0);
        QCOMPARE(slider->maximum(), 0);
        QCOMPARE(slider->value(), 0);
        QCOMPARE(clock->text(), QString("0:00 / 0:00"));
        QVERIFY(QFileInfo::exists(longAudio.path));
        QVERIFY(QFileInfo::exists(shortAudio.path));
        QTRY_VERIFY(!QFileInfo::exists(longAudio.path) || QFile::remove(longAudio.path));
        QTRY_VERIFY(!QFileInfo::exists(shortAudio.path) || QFile::remove(shortAudio.path));
    }
    void statusRefreshPreservesUncommittedKindNavigation() {
        TestEnvironment env("gui-kind-refresh"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        FakeBackend backend; backend.autoStatus = false;
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("filter navigation");
        QTRY_VERIFY(backend.count("search") > 0);
        QTRY_VERIFY(backend.count("status") >= 1);
        const QString heldStatus = backend.last("status").id;
        QVERIFY(!heldStatus.isEmpty());
        auto *kind = window.findChild<QComboBox *>("kindFilter"); QVERIFY(kind);
        // Keep keyboard interaction observable on macOS too, without replacing
        // the application's style or taking ownership of its shared instance.
        auto *style = new WidgetPopupStyle;
        style->setParent(kind);
        kind->setStyle(style);
        QCOMPARE(kind->currentData().toString(), QString());
        QSignalSpy activated(kind, qOverload<int>(&QComboBox::activated));
        kind->showPopup();
        auto *view = kind->view(); QVERIFY(view);
        QTRY_VERIFY(view->isVisible());
        QTest::keyClick(view, Qt::Key_End);
        QCOMPARE(view->currentIndex().data(Qt::UserRole).toString(), QString("video"));
        const QPersistentModelIndex pendingIndex(view->currentIndex());
        QVERIFY(pendingIndex.isValid());
        QCOMPARE(kind->currentData().toString(), QString());
        QCOMPARE(activated.count(), 0);
        emit backend.response(heldStatus, {{"modelAvailable", false}});
        QVERIFY(view->isVisible());
        QVERIFY(pendingIndex.isValid());
        QCOMPARE(view->currentIndex().data(Qt::UserRole).toString(), QString("video"));
        QCOMPARE(kind->currentData().toString(), QString());
        QCOMPARE(activated.count(), 0);
        QTest::keyClick(view, Qt::Key_Return);
        QTRY_COMPARE(kind->currentData().toString(), QString("video"));
        QCOMPARE(activated.count(), 1);
        QTRY_COMPARE(backend.last("search").params.value("kind").toString(), QString("video"));
    }
    void statusRefreshPreservesIntermediatePdfPageEdit() {
        TestEnvironment env("gui-pdf-refresh"); QVERIFY2(env.isValid(), qPrintable(env.error()));
        const QString path = env.path("two pages 中文.pdf");
        {
            QPdfWriter writer(path); QPainter painter(&writer);
            QVERIFY(painter.isActive()); painter.drawText(100, 200, "first page");
            QVERIFY(writer.newPage()); painter.drawText(100, 200, "second page");
        }
        FakeBackend backend; backend.autoStatus = false;
        SearchHit pdf; pdf.path = path; pdf.kind = "pdf"; pdf.page = 2; pdf.text = "second page";
        backend.hits = {pdf};
        MainWindow window(env.runtimePaths(), &backend, false, false);
        window.show(); window.start();
        window.findChild<SearchEdit *>("searchInput")->setText("second page");
        auto *results = window.findChild<QListWidget *>("searchResults"); QVERIFY(results);
        QTRY_COMPARE(results->count(), 1);
        QTRY_VERIFY(backend.count("status") >= 1);
        const QString heldStatus = backend.last("status").id;
        QVERIFY(!heldStatus.isEmpty());
        auto *page = window.findChild<QSpinBox *>("pdfPageNumber"); QVERIFY(page);
        QCOMPARE(page->maximum(), 2);
        QCOMPARE(page->value(), 2);
        auto *editor = page->findChild<QLineEdit *>(); QVERIFY(editor);
        auto *view = window.findChild<QPdfView *>("pdfPreview"); QVERIFY(view);
        QSignalSpy changed(page, qOverload<int>(&QSpinBox::valueChanged));
        // A widget focus request alone does not activate its top-level window.
        window.raise();
        window.activateWindow();
        QVERIFY(QTest::qWaitForWindowActive(&window));
        QTest::mouseClick(editor, Qt::LeftButton);
        QTRY_VERIFY(editor->hasFocus());
        page->selectAll();
        QTest::keyClick(editor, Qt::Key_Backspace);
        QVERIFY(page->cleanText().isEmpty());
        QVERIFY(!page->hasAcceptableInput());
        QCOMPARE(page->value(), 2);
        QCOMPARE(changed.count(), 0);
        const QString intermediate = editor->text();
        const int cursor = editor->cursorPosition();
        QVERIFY(editor->hasFocus());
        emit backend.response(heldStatus, {{"modelAvailable", false}});
        QCOMPARE(editor->text(), intermediate);
        QCOMPARE(editor->cursorPosition(), cursor);
        QVERIFY(editor->hasFocus());
        QVERIFY(page->cleanText().isEmpty());
        QCOMPARE(page->value(), 2);
        QCOMPARE(changed.count(), 0);
        QTest::keyClicks(editor, "1");
        QTest::keyClick(editor, Qt::Key_Return);
        QCOMPARE(page->value(), 1);
        QCOMPARE(view->pageNavigator()->currentPage(), 0);
        window.findChild<PreviewPanel *>("previewPanel")->clear();
        QVERIFY(QFile::remove(path));
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
            QTRY_VERIFY_WITH_TIMEOUT(!imagePreviewPixmap(preview).isNull(), 10000);
            QCOMPARE(imagePreviewPixmap(preview).toImage().pixelColor(0, 0), QColor(Qt::green));
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
