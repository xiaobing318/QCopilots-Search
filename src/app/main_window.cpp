#include "main_window.h"
#include "app_backend.h"
#include "model_dialog.h"
#include "platform_integration.h"
#include "preview_panel.h"
#include "settings_dialog.h"
#include <QApplication>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialog>
#include <QDir>
#include <QFileInfo>
#include <QInputMethodEvent>
#include <QJsonArray>
#include <QJsonDocument>
#include <QKeyEvent>
#include <QLabel>
#include <QListWidget>
#include <QLocale>
#include <QMenu>
#include <QMessageBox>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QPainter>
#include <QProgressBar>
#include <QPushButton>
#include <QScreen>
#include <QScrollBar>
#include <QShortcut>
#include <QSignalBlocker>
#include <QSplitter>
#include <QStandardPaths>
#include <QStatusBar>
#include <QSystemTrayIcon>
#include <QVersionNumber>
#include <QVBoxLayout>

namespace qcopilots_search {
void SearchEdit::inputMethodEvent(QInputMethodEvent *event) {
    const bool wasComposing = composing_;
    composing_ = !event->preeditString().isEmpty();
    QLineEdit::inputMethodEvent(event);
    if (wasComposing && !composing_) emit compositionFinished();
}
QIcon MainWindow::applicationIcon() {
    QPixmap pixmap(64, 64);
    pixmap.fill(Qt::transparent);
    QPainter painter(&pixmap);
    painter.setRenderHint(QPainter::Antialiasing);
    painter.setBrush(QColor("#2856cf"));
    painter.setPen(Qt::NoPen);
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 15, 15);
    painter.setBrush(Qt::NoBrush);
    painter.setPen(QPen(Qt::white, 5, Qt::SolidLine, Qt::RoundCap));
    painter.drawEllipse(QPointF(28, 28), 14, 14);
    painter.drawLine(QPointF(39, 39), QPointF(49, 49));
    return QIcon(pixmap);
}
MainWindow::MainWindow(const RuntimePaths &paths, AppBackend *backend, bool useTray, bool onboarding, QWidget *parent)
    : QMainWindow(parent), paths_(paths), backend_(backend), platform_(new PlatformIntegration(this)),
      language_(QLocale::system().name()), useTray_(useTray), onboarding_(onboarding), network_(new QNetworkAccessManager(this)) {
    setObjectName("qcopilots-search-main-window");
    setWindowTitle("QCopilots Search");
    setWindowIcon(applicationIcon());
    resize(1180, 770);
    setMinimumSize(820, 540);
    buildUi();
    applyLanguage();
    debounce_.setSingleShot(true);
    debounce_.setInterval(250);
    popupDebounce_.setSingleShot(true);
    popupDebounce_.setInterval(180);
    connect(&debounce_, &QTimer::timeout, this, [this] { sendSearch(); });
    connect(&popupDebounce_, &QTimer::timeout, this, [this] { sendSearch(true); });
    connect(query_, &QLineEdit::textChanged, this, [this] { if (!query_->isComposing()) debounce_.start(); });
    connect(query_, &SearchEdit::compositionFinished, &debounce_, qOverload<>(&QTimer::start));
    connect(query_, &QLineEdit::returnPressed, this, [this] { if (!query_->isComposing()) { debounce_.stop(); sendSearch(); } });
    connect(kind_, &QComboBox::currentIndexChanged, this, [this] { debounce_.start(); });
    connect(root_, &QComboBox::currentIndexChanged, this, [this] { debounce_.start(); });
    connect(code_, &QCheckBox::toggled, this, [this](bool code) { kind_->setEnabled(!code); debounce_.start(); });
    connect(results_, &QListWidget::currentRowChanged, this, &MainWindow::selectResult);
    connect(results_, &QListWidget::itemActivated, this, [this] { openCurrent(); });
    connect(preview_, &PreviewPanel::openRequested, this, [this](const SearchHit &hit) { QString error; if (!PlatformIntegration::openHit(hit, settings_.editorCommand, &error)) showError(error); });
    connect(preview_, &PreviewPanel::revealRequested, this, [this](const QString &path) { QString error; if (!PlatformIntegration::revealFile(path, &error)) showError(error); });
    connect(preview_, &PreviewPanel::documentRequested, this, [this](const QString &path, bool code, int start) {
        if (!documentRequest_.isEmpty()) { backend_->cancel(documentRequest_); pending_.remove(documentRequest_); }
        documentPath_ = path;
        documentStart_ = start;
        documentRequest_ = send("document.preview", {{"path", path}, {"code", code}, {"start", start}, {"maxSegments", 100}}, 100000);
    });
    connect(settingsButton_, &QPushButton::clicked, this, [this] { showSettings(); });
    connect(modelButton_, &QPushButton::clicked, this, &MainWindow::showModels);
    connect(indexButton_, &QPushButton::clicked, this, [this] { send("index.reconcile"); });
    connect(pauseButton_, &QPushButton::clicked, this, [this] { send(indexState_ == "paused" ? "index.resume" : "index.pause"); });
    connect(retryButton_, &QPushButton::clicked, this, [this] { send("index.retry"); });
    connect(quickButton_, &QPushButton::clicked, this, &MainWindow::showQuickSearch);
    connect(updateButton_, &QPushButton::clicked, this, [this] { checkForUpdates(true); });
    connect(backend_, &AppBackend::connected, this, [this] {
        connected_ = true;
        error_->hide();
        indexButton_->setEnabled(true);
        send("status");
        send("settings.get");
    });
    connect(backend_, &AppBackend::disconnected, this, [this] {
        connected_ = false;
        if (quitting_) { QApplication::quit(); return; }
        indexButton_->setEnabled(false);
        showError(label("The local service disconnected. Restart the application to reconnect.", "本地服务已断开，请重新启动应用以连接。"));
    });
    connect(backend_, &AppBackend::response, this, &MainWindow::handleResponse);
    connect(backend_, &AppBackend::failed, this, &MainWindow::handleFailure);
    connect(backend_, &AppBackend::eventReceived, this, [this](const QString &event, const QJsonObject &data) {
        if (event == "progress") updateProgress(data);
        else if (event == "settings") applySettings(data);
        else if (event == "indexChanged") debounce_.start();
    });
    refresh_.setInterval(2500);
    connect(&refresh_, &QTimer::timeout, this, [this] { if (connected_ && !pending_.values().contains("status")) send("status"); });
    refresh_.start();
    auto *find = new QShortcut(QKeySequence::Find, this);
    connect(find, &QShortcut::activated, this, &MainWindow::showSearch);
    auto *quit = new QShortcut(QKeySequence::Quit, this);
    connect(quit, &QShortcut::activated, this, &MainWindow::quitApplication);
    query_->installEventFilter(this);
    results_->installEventFilter(this);
    if (useTray_) {
        createTray();
        platform_->registerGlobalShortcut();
        connect(platform_, &PlatformIntegration::activated, this, &MainWindow::showQuickSearch);
    }
    applyLanguage();
}
MainWindow::~MainWindow() { if (tray_) tray_->hide(); }
void MainWindow::start() { backend_->start(); }
QString MainWindow::label(const char *en, const char *zh) const { return QString::fromUtf8(language_.startsWith("zh") ? zh : en); }
void MainWindow::buildUi() {
    auto *central = new QWidget(this);
    auto *layout = new QVBoxLayout(central);
    layout->setContentsMargins(24, 20, 24, 16);
    layout->setSpacing(14);
    auto *top = new QHBoxLayout;
    auto *brand = new QVBoxLayout;
    heading_ = new QLabel("QCopilots Search", central);
    heading_->setStyleSheet("font-size:24px;font-weight:650");
    subtitle_ = new QLabel(central);
    brand->addWidget(heading_);
    brand->addWidget(subtitle_);
    top->addLayout(brand, 1);
    quickButton_ = new QPushButton(central);
    quickButton_->setObjectName("quickSearchButton");
    modelButton_ = new QPushButton(central);
    modelButton_->setObjectName("modelsButton");
    settingsButton_ = new QPushButton(central);
    settingsButton_->setObjectName("settingsButton");
    top->addWidget(quickButton_);
    top->addWidget(modelButton_);
    top->addWidget(settingsButton_);
    layout->addLayout(top);
    query_ = new SearchEdit(central);
    query_->setObjectName("searchInput");
    query_->setClearButtonEnabled(true);
    query_->setMinimumHeight(48);
    query_->setStyleSheet("QLineEdit{font-size:17px;border:1px solid #849acb;border-radius:9px;padding:8px 12px} QLineEdit:focus{border:2px solid #2856cf}");
    layout->addWidget(query_);
    auto *filters = new QHBoxLayout;
    kind_ = new QComboBox(central);
    kind_->setObjectName("kindFilter");
    kind_->setMinimumWidth(150);
    root_ = new QComboBox(central);
    root_->setObjectName("rootFilter");
    root_->setMinimumWidth(220);
    root_->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    code_ = new QCheckBox(central);
    code_->setObjectName("codeSearchCheck");
    summary_ = new QLabel(central);
    summary_->setObjectName("searchSummary");
    filters->addWidget(kind_);
    filters->addWidget(root_);
    filters->addWidget(code_);
    filters->addStretch();
    filters->addWidget(summary_);
    layout->addLayout(filters);
    error_ = new QLabel(central);
    error_->setObjectName("applicationError");
    error_->setWordWrap(true);
    error_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    error_->setStyleSheet("background:#fff0e9;color:#8b3527;padding:9px;border-radius:5px");
    error_->hide();
    layout->addWidget(error_);
    auto *splitter = new QSplitter(Qt::Horizontal, central);
    splitter->setChildrenCollapsible(false);
    results_ = new QListWidget(splitter);
    results_->setObjectName("searchResults");
    results_->setWordWrap(true);
    results_->setSpacing(5);
    results_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    results_->setStyleSheet("QListWidget{border:1px solid #cbd1de;border-radius:7px} QListWidget::item{padding:12px;border-radius:5px} QListWidget::item:selected{background:#e4ecff;color:#102957}");
    results_->setMinimumWidth(240);
    preview_ = new PreviewPanel(splitter);
    preview_->setMinimumWidth(310);
    splitter->addWidget(results_);
    splitter->addWidget(preview_);
    splitter->setStretchFactor(0, 4);
    splitter->setStretchFactor(1, 6);
    layout->addWidget(splitter, 1);
    auto *indexRow = new QHBoxLayout;
    progressText_ = new QLabel(central);
    progressText_->setObjectName("indexProgressText");
    progressText_->setMinimumWidth(160);
    progress_ = new QProgressBar(central);
    progress_->setObjectName("indexProgress");
    progress_->setMaximumWidth(220);
    progress_->setValue(0);
    indexButton_ = new QPushButton(central);
    indexButton_->setObjectName("reconcileButton");
    indexButton_->setEnabled(false);
    pauseButton_ = new QPushButton(central);
    pauseButton_->setObjectName("pauseIndexButton");
    retryButton_ = new QPushButton(central);
    retryButton_->setObjectName("retryIndexButton");
    indexRow->addWidget(progressText_, 1);
    indexRow->addWidget(progress_);
    indexRow->addWidget(indexButton_);
    indexRow->addWidget(pauseButton_);
    indexRow->addWidget(retryButton_);
    layout->addLayout(indexRow);
    auto *footer = new QHBoxLayout;
    capability_ = new QLabel(central);
    capability_->setWordWrap(true);
    capability_->setObjectName("shortcutCapability");
    updateButton_ = new QPushButton(central);
    updateButton_->setFlat(true);
    updateButton_->setObjectName("updatesButton");
    footer->addWidget(capability_, 1);
    footer->addWidget(updateButton_);
    layout->addLayout(footer);
    setCentralWidget(central);
}
void MainWindow::applyLanguage() {
    subtitle_->setText(label("Find the meaning in your files. Local, private, multilingual.", "从本机文件中找到所需内容，支持中英文与多种媒体。"));
    query_->setPlaceholderText(label("Search documents, images, sounds and moments…", "搜索文档、图片、声音与视频片段…"));
    query_->setAccessibleName(label("Search files", "搜索文件"));
    const QSignalBlocker kindBlocker(kind_);
    const QString currentKind = kind_->currentData().toString();
    kind_->clear();
    kind_->addItem(label("All file types", "所有类型"), "");
    kind_->addItem(label("Documents", "文档"), "document");
    kind_->addItem("PDF", "pdf");
    kind_->addItem(label("Images", "图片"), "image");
    kind_->addItem(label("Audio", "音频"), "audio");
    kind_->addItem(label("Video", "视频"), "video");
    kind_->setCurrentIndex(qMax(0, kind_->findData(currentKind)));
    code_->setText(label("Code search", "代码搜索"));
    code_->setToolTip(label("Enable code indexing in Settings before searching source code and notebook cells.", "请先在设置中启用代码索引，再搜索源代码与 Notebook 单元格。"));
    quickButton_->setText(label("Quick search", "快速搜索"));
    modelButton_->setText(modelAvailable_ ? label("Model ready", "模型已就绪") : label("Add model", "添加模型"));
    settingsButton_->setText(label("Settings", "设置"));
    indexButton_->setText(label("Rescan", "重新扫描"));
    pauseButton_->setText(indexState_ == "paused" ? label("Resume", "继续") : label("Pause", "暂停"));
    retryButton_->setText(label("Retry failed", "重试失败项"));
    updateButton_->setText(label("Check for updates", "检查更新"));
    if (root_->count() == 0) root_->addItem(label("All folders", "所有文件夹"), ""); else root_->setItemText(0, label("All folders", "所有文件夹"));
    if (summary_->text().isEmpty()) summary_->setText(label("Start with a question or keyword", "输入问题或关键词开始搜索"));
    if (progressText_->text().isEmpty()) progressText_->setText(label("Connecting to local service…", "正在连接本地服务…"));
    preview_->setLanguage(language_);
    if (useTray_) {
        const QString shortcut = platform_->shortcutStatus();
        if (shortcut == "Ctrl+Shift+Space") capability_->setText(label("Quick search: Ctrl+Shift+Space · Search: Ctrl+F", "快速搜索：Ctrl+Shift+Space · 搜索：Ctrl+F"));
        else if (!shortcut.isEmpty()) capability_->setText(label("Global shortcut unavailable. Use Quick search, the tray or Ctrl+F.", "全局快捷键暂不可用，请使用快速搜索、托盘或 Ctrl+F。"));
    } else capability_->setText(label("Search: Ctrl+F", "搜索：Ctrl+F"));
    if (useTray_ && !tray_) capability_->setText(capability_->text() + label(" · Tray unavailable, closing the window exits the app.", " · 系统托盘不可用，关闭窗口将退出应用。"));
    if (tray_) {
        traySearch_->setText(label("Open search", "打开搜索"));
        trayQuick_->setText(label("Quick search", "快速搜索"));
        trayPause_->setText(indexState_ == "paused" ? label("Resume indexing", "继续索引") : label("Pause indexing", "暂停索引"));
        trayQuit_->setText(label("Quit QCopilots Search", "退出 QCopilots Search"));
    }
}
void MainWindow::createTray() {
    if (!QSystemTrayIcon::isSystemTrayAvailable()) return;
    tray_ = new QSystemTrayIcon(windowIcon(), this);
    tray_->setToolTip("QCopilots Search");
    auto *menu = new QMenu(this);
    traySearch_ = menu->addAction("Open search", this, &MainWindow::showSearch);
    trayQuick_ = menu->addAction("Quick search", this, &MainWindow::showQuickSearch);
    trayPause_ = menu->addAction("Pause indexing", this, [this] { send(indexState_ == "paused" ? "index.resume" : "index.pause"); });
    menu->addSeparator();
    trayQuit_ = menu->addAction("Quit", this, &MainWindow::quitApplication);
    tray_->setContextMenu(menu);
    connect(tray_, &QSystemTrayIcon::activated, this, [this](QSystemTrayIcon::ActivationReason reason) { if (reason == QSystemTrayIcon::Trigger || reason == QSystemTrayIcon::DoubleClick) showSearch(); });
    tray_->show();
    applyLanguage();
}
QString MainWindow::send(const QString &method, const QJsonObject &params, int timeoutMs) {
    const QString id = backend_->request(method, params, timeoutMs);
    pending_.insert(id, method);
    return id;
}
void MainWindow::sendSearch(bool popup) {
    if (!connected_) return;
    SearchEdit *input = popup ? popupQuery_ : query_;
    if (!input || input->isComposing()) return;
    QString &previous = popup ? popupSearch_ : currentSearch_;
    if (!previous.isEmpty()) { backend_->cancel(previous); pending_.remove(previous); }
    QJsonObject parameters{{"query", input->text().trimmed()}, {"limit", popup ? 12 : 100}, {"semantic", true}, {"code", popup ? false : code_->isChecked()}};
    if (!popup) { parameters.insert("kind", code_->isChecked() ? "" : kind_->currentData().toString()); parameters.insert("root", root_->currentData().toString()); }
    previous = send("search", parameters, 120000);
    if (popup) popupRequestedQuery_ = input->text().trimmed();
    if (popup) popupStatus_->setText(label("Searching…", "正在搜索…")); else summary_->setText(label("Searching…", "正在搜索…"));
}
void MainWindow::handleResponse(const QString &id, const QJsonObject &data) {
    const QString method = pending_.take(id);
    if (method == "search") {
        const bool popup = id == popupSearch_;
        if (!popup && id != currentSearch_) return;
        if (popup) popupResultQuery_ = popupRequestedQuery_;
        QList<SearchHit> &hits = popup ? popupHits_ : hits_;
        QListWidget *list = popup ? popupResults_ : results_;
        QLabel *summary = popup ? popupStatus_ : summary_;
        const SearchHit previous = preview_->currentHit();
        const QSignalBlocker resultBlocker(list);
        hits.clear();
        list->clear();
        for (const auto &value : data.value("hits").toArray()) {
            const auto hit = SearchHit::fromJson(value.toObject());
            hits.append(hit);
            QString excerpt = hit.text.simplified().left(popup ? 110 : 180);
            QString description = hit.title.isEmpty() ? QFileInfo(hit.path).fileName() : hit.title;
            description += '\n' + excerpt;
            description += '\n' + QFileInfo(hit.path).absolutePath();
            if (!popup && !hit.matchSource.isEmpty()) {
                const QString source = hit.matchSource == "semantic" ? label("Semantic", "语义匹配")
                    : hit.matchSource == "hybrid" ? label("Semantic + keywords", "语义与关键词")
                    : hit.matchSource == "keyword" ? label("Keywords", "关键词") : hit.matchSource;
                description += " · " + source;
            }
            auto *item = new QListWidgetItem(description, list);
            item->setToolTip(hit.path);
            item->setData(Qt::UserRole, hit.path);
        }
        summary->setText(hits.isEmpty() ? label("No matching files", "没有匹配的文件") : label("Results: ", "结果：") + QString::number(hits.size()) + (data.value("semantic").toBool() ? label(" · semantic + keywords", " · 语义与关键词") : label(" · keywords", " · 关键词")));
        int selection = hits.isEmpty() ? -1 : 0;
        bool sameHit = false;
        if (!popup) {
            for (int row = 0; row < hits.size(); ++row) {
                const auto &hit = hits.at(row);
                if (hit.path == previous.path && hit.segmentId == previous.segmentId && hit.text == previous.text && hit.page == previous.page && hit.startMs == previous.startMs) {
                    selection = row;
                    sameHit = true;
                    break;
                }
            }
        }
        list->setCurrentRow(selection);
        if (!popup && !sameHit) selectResult(selection);
        const QString warning = data.value("warning").toString(data.value("error").toString());
        if (popup) {
            if (!warning.isEmpty()) popupStatus_->setText(popupStatus_->text() + '\n' + warning);
        } else {
            if (!warning.isEmpty()) showError(warning);
            else if (!lastSearchWarning_.isEmpty() && error_->text() == lastSearchWarning_) error_->hide();
            lastSearchWarning_ = warning;
        }
    } else if (method == "document.preview") {
        if (id == documentRequest_ && preview_->currentHit().path == documentPath_) {
            const bool append = documentStart_ > 0;
            documentRequest_.clear();
            preview_->setDocumentPreview(data, append);
        }
    } else if (method == "settings.get" || method == "settings.set") {
        applySettings(data.contains("settings") ? data.value("settings").toObject() : data);
        if (!shownOnboarding_ && onboarding_) {
            shownOnboarding_ = true;
            if (settings_.roots.isEmpty()) QTimer::singleShot(0, this, [this] { showSettings(true); });
            else if (settings_.checkUpdates) QTimer::singleShot(5000, this, [this] { checkForUpdates(false); });
        }
    } else if (method == "status") {
        modelAvailable_ = data.value("modelAvailable").toBool();
        if (data.contains("progress")) updateProgress(data.value("progress").toObject());
        const QString watcherWarning = data.value("watcherWarning").toString();
        if (!watcherWarning.isEmpty()) showError(watcherWarning);
        applyLanguage();
    } else if (method.startsWith("index.")) {
        updateProgress(data.contains("progress") ? data.value("progress").toObject() : data);
    } else if (method == "models.reload") {
        modelReloadAttempts_ = 0;
        modelAvailable_ = data.value("modelAvailable").toBool();
        applyLanguage();
        send("index.start");
        debounce_.start();
    }
}
void MainWindow::handleFailure(const QString &id, const QString &code, const QString &message) {
    const QString method = pending_.take(id);
    if (code.compare("cancelled", Qt::CaseInsensitive) == 0 || code.compare("canceled", Qt::CaseInsensitive) == 0) return;
    if (method == "models.reload" && code == "busy" && modelReloadAttempts_++ < 20 && !quitting_) {
        statusBar()->showMessage(label("Waiting for the current index update before loading the model…", "正在等待当前索引更新完成，然后加载模型…"), 5000);
        QTimer::singleShot(500, this, [this] { if (!quitting_) send("models.reload", {}, 120000); });
        return;
    }
    if (method == "search" && id != currentSearch_ && id != popupSearch_) return;
    if (method == "document.preview") {
        if (id == documentRequest_ && preview_->currentHit().path == documentPath_) { preview_->documentPreviewFailed(message); documentRequest_.clear(); }
        return;
    }
    showError(message.isEmpty() ? code : message);
    if (id == currentSearch_) summary_->setText(label("Search failed", "搜索失败"));
    if (id == popupSearch_ && popupStatus_) popupStatus_->setText(message);
    if (method == "shutdown") QApplication::quit();
}
void MainWindow::applySettings(const QJsonObject &json) {
    AppSettings parsed;
    QString error;
    if (!AppSettings::fromJson(json, &parsed, &error)) { showError(error); return; }
    settings_ = parsed;
    language_ = settings_.language == "system" ? QLocale::system().name() : settings_.language;
    const QSignalBlocker rootBlocker(root_);
    const QString previousRoot = root_->currentData().toString();
    root_->clear();
    root_->addItem(label("All folders", "所有文件夹"), "");
    for (const auto &path : settings_.roots) root_->addItem(path, path);
    root_->setCurrentIndex(qMax(0, root_->findData(previousRoot)));
    code_->setEnabled(settings_.includeCode);
    if (!settings_.includeCode) code_->setChecked(false);
    applyLanguage();
    debounce_.start();
}
void MainWindow::updateProgress(const QJsonObject &value) {
    indexState_ = value.value("state").toString("idle");
    const qint64 total = value.value("total").toInteger();
    const qint64 completed = value.value("completed").toInteger();
    const qint64 failed = value.value("failed").toInteger();
    QString state = indexState_;
    if (state == "idle" || state == "complete" || state == "completed") state = label("Index ready", "索引已就绪");
    else if (state == "paused") state = label("Index paused", "索引已暂停");
    else if (state == "scanning") state = label("Scanning folders", "正在扫描文件夹");
    else if (state == "indexing" || state == "running") state = label("Indexing", "正在索引");
    progressText_->setText(state + QStringLiteral("  %1 / %2").arg(completed).arg(total) + (failed > 0 ? label(" · failed: ", " · 失败：") + QString::number(failed) : QString()));
    progressText_->setToolTip(value.value("currentPath").toString());
    progress_->setRange(0, indexState_ == "scanning" && total == 0 ? 0 : 1000);
    progress_->setValue(total > 0 ? int(qMin<qint64>(1000, completed * 1000 / total)) : 0);
    retryButton_->setEnabled(failed > 0);
    pauseButton_->setEnabled(connected_);
    pauseButton_->setText(indexState_ == "paused" ? label("Resume", "继续") : label("Pause", "暂停"));
    if (trayPause_) trayPause_->setText(indexState_ == "paused" ? label("Resume indexing", "继续索引") : label("Pause indexing", "暂停索引"));
    const QString error = value.value("error").toString();
    if (!error.isEmpty()) showError(error);
}
void MainWindow::showSettings(bool firstRun) {
    if (auto *existing = findChild<SettingsDialog *>()) { existing->raise(); existing->activateWindow(); return; }
    const QString platformDefault = QDir(QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)).filePath(QStringLiteral("qcopilots-search"));
    const bool defaultProfile = !QStandardPaths::isTestModeEnabled()
        && !qEnvironmentVariableIsSet("QCOPILOTS_SEARCH_PROFILE_DIR")
        && paths_.profileDir == RuntimePaths::resolve(platformDefault).profileDir;
    auto *dialog = new SettingsDialog(settings_, firstRun, defaultProfile, this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    connect(dialog, &QDialog::accepted, this, [this, dialog, defaultProfile] {
        const auto settings = dialog->settings();
        if (defaultProfile && settings.startAtLogin != settings_.startAtLogin) {
            QString error;
            if (!PlatformIntegration::setStartAtLogin(settings.startAtLogin, &error)) { showError(error); return; }
        }
        auto changes = settings.toJson();
        // Pausing belongs to the service's live task state. A preferences dialog
        // may stay open while that state changes and must never overwrite it.
        changes.remove("indexingPaused");
        send("settings.set", changes);
    });
    dialog->open();
}
void MainWindow::showModels() {
    if (auto *existing = findChild<ModelDialog *>()) { existing->raise(); existing->activateWindow(); return; }
    auto *dialog = new ModelDialog(paths_.modelDir, language_, this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    connect(dialog, &ModelDialog::modelsReady, this, [this] { if (!modelAvailable_) { modelReloadAttempts_ = 0; send("models.reload", {}, 120000); } });
    dialog->open();
}
void MainWindow::showError(const QString &message) { error_->setText(message); error_->show(); }
void MainWindow::openCurrent() {
    const int row = results_->currentRow();
    if (row < 0 || row >= hits_.size()) return;
    QString error;
    if (!PlatformIntegration::openHit(hits_.at(row), settings_.editorCommand, &error)) showError(error);
}
void MainWindow::selectResult(int row) {
    if (!documentRequest_.isEmpty()) { backend_->cancel(documentRequest_); pending_.remove(documentRequest_); documentRequest_.clear(); }
    if (row >= 0 && row < hits_.size()) preview_->showHit(hits_.at(row)); else preview_->clear();
}
void MainWindow::showSearch() { showNormal(); raise(); activateWindow(); query_->setFocus(); query_->selectAll(); }
void MainWindow::showQuickSearch() {
    if (!popup_) {
        popup_ = new QDialog(this, Qt::Tool);
        popup_->setObjectName("quickSearchWindow");
        popup_->setWindowTitle("QCopilots Search");
        popup_->resize(740, 450);
        auto *layout = new QVBoxLayout(popup_);
        popupQuery_ = new SearchEdit(popup_);
        popupQuery_->setObjectName("quickSearchInput");
        popupQuery_->setMinimumHeight(44);
        popupQuery_->setClearButtonEnabled(true);
        popupResults_ = new QListWidget(popup_);
        popupResults_->setObjectName("quickSearchResults");
        popupResults_->setWordWrap(true);
        popupStatus_ = new QLabel(popup_);
        popupStatus_->setWordWrap(true);
        layout->addWidget(popupQuery_);
        layout->addWidget(popupResults_, 1);
        layout->addWidget(popupStatus_);
        auto *full = new QPushButton(label("Open full search", "打开完整搜索"), popup_);
        full->setObjectName("openFullSearchButton");
        layout->addWidget(full);
        connect(full, &QPushButton::clicked, this, [this] { query_->setText(popupQuery_->text()); popup_->hide(); showSearch(); });
        connect(popupQuery_, &QLineEdit::textChanged, this, [this] {
            if (!popupQuery_->isComposing()) {
                popupHits_.clear();
                popupResults_->clear();
                popupResultQuery_.clear();
                popupDebounce_.start();
            }
        });
        connect(popupQuery_, &SearchEdit::compositionFinished, &popupDebounce_, qOverload<>(&QTimer::start));
        const auto open = [this] {
            if (popupQuery_->text().trimmed() != popupResultQuery_) { popupDebounce_.stop(); sendSearch(true); return; }
            const int row = popupResults_->currentRow();
            if (row < 0 || row >= popupHits_.size()) { sendSearch(true); return; }
            QString error;
            if (PlatformIntegration::openHit(popupHits_.at(row), settings_.editorCommand, &error)) popup_->hide(); else popupStatus_->setText(error);
        };
        connect(popupQuery_, &QLineEdit::returnPressed, this, [this, open] { if (!popupQuery_->isComposing()) open(); });
        connect(popupResults_, &QListWidget::itemActivated, this, open);
        popupQuery_->installEventFilter(this);
        popupResults_->installEventFilter(this);
    }
    popupQuery_->setPlaceholderText(label("Describe what you want to find…", "描述您想查找的内容…"));
    popup_->move(screen()->availableGeometry().center() - QPoint(popup_->width() / 2, popup_->height() / 2));
    popup_->show();
    popup_->raise();
    popup_->activateWindow();
    popupQuery_->setFocus();
    popupQuery_->selectAll();
    sendSearch(true);
}
bool MainWindow::eventFilter(QObject *watched, QEvent *event) {
    if (event->type() == QEvent::KeyPress) {
        const auto *key = static_cast<QKeyEvent *>(event);
        if ((watched == query_ || watched == popupQuery_) && key->key() == Qt::Key_Down) {
            auto *edit = static_cast<SearchEdit *>(watched);
            if (edit->isComposing()) return false;
            QListWidget *list = watched == query_ ? results_ : popupResults_;
            if (list->count()) { list->setFocus(); list->setCurrentRow(qMax(0, list->currentRow())); return true; }
        }
        if ((watched == results_ || watched == popupResults_) && key->key() == Qt::Key_Escape) {
            if (watched == popupResults_) popup_->hide(); else query_->setFocus();
            return true;
        }
    }
    return QMainWindow::eventFilter(watched, event);
}
void MainWindow::checkForUpdates(bool manual) {
    QNetworkRequest request(QUrl("https://api.github.com/repos/xiaobing318/QCopilots-search/releases/latest"));
    request.setRawHeader("User-Agent", "qcopilots-search/" + QCoreApplication::applicationVersion().toUtf8());
    request.setRawHeader("Accept", "application/vnd.github+json");
    request.setTransferTimeout(15000);
    auto *reply = network_->get(request);
    updateButton_->setEnabled(false);
    connect(reply, &QNetworkReply::finished, this, [this, reply, manual] {
        updateButton_->setEnabled(true);
        reply->deleteLater();
        if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() == 404) {
            if (manual) QMessageBox::information(this, label("Updates", "更新"), label("No public release is available yet.", "目前尚无公开发布版本。"));
            return;
        }
        if (reply->error() != QNetworkReply::NoError) { if (manual) showError(label("Update check failed: ", "检查更新失败：") + reply->errorString()); return; }
        const QJsonObject release = QJsonDocument::fromJson(reply->readAll()).object();
        QString version = release.value("tag_name").toString();
        if (version.startsWith('v')) version.remove(0, 1);
        const QVersionNumber latest = QVersionNumber::fromString(version);
        const QVersionNumber installed = QVersionNumber::fromString(QCoreApplication::applicationVersion());
        if (latest.isNull() || latest <= installed) {
            if (manual) QMessageBox::information(this, label("Updates", "更新"), label("You are using the latest available version.", "当前已是最新可用版本。"));
            return;
        }
        const QUrl url(release.value("html_url").toString());
        if (url.scheme() != "https" || url.host() != "github.com" || !url.path().startsWith("/xiaobing318/QCopilots-search/releases/")) { if (manual) showError(label("The update response contains an unexpected download address.", "更新响应中的下载地址不符合预期。")); return; }
        QMessageBox prompt(QMessageBox::Information, label("Update available", "有可用更新"), label("Version ", "版本 ") + version + label(" is available. Open the release page to download it?", " 已发布，是否打开发布页面下载？"), QMessageBox::Open | QMessageBox::Cancel, this);
        if (prompt.exec() == QMessageBox::Open) QDesktopServices::openUrl(url);
    });
}
void MainWindow::quitApplication() {
    quitting_ = true;
    refresh_.stop();
    if (tray_) tray_->hide();
    if (popup_) popup_->hide();
    preview_->clear();
    hide();
    // The server drains workers and releases its profile lock before exiting.
    if (connected_) { send("shutdown", {}, 10000); QTimer::singleShot(11000, qApp, &QCoreApplication::quit); }
    else QApplication::quit();
}
void MainWindow::closeEvent(QCloseEvent *event) {
    if (!quitting_ && settings_.minimizeToTray && tray_ && tray_->isVisible()) { hide(); event->ignore(); return; }
    if (!quitting_) { quitApplication(); event->ignore(); return; }
    event->accept();
}
}
