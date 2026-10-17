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
#include <QResource>
#include <QProgressBar>
#include <QPushButton>
#include <QRegularExpression>
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

// Force the icon collection into programs that link the static app component
// library, including GUI tests. Resource initialization must be at global scope.
static void initializeQcsIcons() {
    Q_INIT_RESOURCE(qcs_icons);
}

namespace qcopilots_search {
namespace {
bool preservesSourceVersion(const QString &previous, const QString &current) {
    if (previous == current) return true;
    // Publishing another extraction batch changes the index version, but not
    // the source or pipeline. Only forward partial publication is equivalent.
    static const QRegularExpression partialVersion(QStringLiteral("\\A([^|\\r\\n]+\\|[^|\\r\\n]+)\\|partial:([1-9][0-9]*)\\z"));
    const auto oldPartial = partialVersion.match(previous);
    if (!oldPartial.hasMatch()) return false;
    bool oldOk = false;
    const auto oldOffset = oldPartial.captured(2).toLongLong(&oldOk);
    if (!oldOk) return false;
    const QString base = oldPartial.captured(1);
    if (current == base) return true;
    const auto newPartial = partialVersion.match(current);
    bool newOk = false;
    const auto newOffset = newPartial.captured(2).toLongLong(&newOk);
    return newPartial.hasMatch() && newOk && newPartial.captured(1) == base && newOffset >= oldOffset;
}

bool preservesPreview(const SearchHit &previous, const SearchHit &current) {
    // Segment IDs are database row identities and change when an indexed file
    // is replaced. Preview identity instead follows its source and full locator.
    return current.path == previous.path && current.kind == previous.kind && current.text == previous.text
        && current.page == previous.page && current.lineStart == previous.lineStart && current.lineEnd == previous.lineEnd
        && current.cell == previous.cell && current.startMs == previous.startMs && current.endMs == previous.endMs
        && preservesSourceVersion(previous.sourceVersion, current.sourceVersion);
}
}

void SearchEdit::inputMethodEvent(QInputMethodEvent *event) {
    const bool wasComposing = composing_;
    composing_ = !event->preeditString().isEmpty();
    QLineEdit::inputMethodEvent(event);
    if (wasComposing && !composing_) emit compositionFinished();
}
QIcon MainWindow::applicationIcon() {
    static const QIcon icon = [] {
        initializeQcsIcons();
        QIcon result;
        for (const int size : {16, 20, 24, 32, 40, 48, 64, 128, 256, 512, 1024}) {
            result.addFile(QStringLiteral(":/qcs/icons/qcs-%1.png").arg(size), QSize(size, size));
        }
        return result;
    }();
    return icon;
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
    connect(query_, &QLineEdit::textChanged, this, [this] { updateBrowseControls(); if (!query_->isComposing()) debounce_.start(); });
    connect(query_, &SearchEdit::compositionFinished, &debounce_, qOverload<>(&QTimer::start));
    connect(query_, &QLineEdit::returnPressed, this, [this] { if (!query_->isComposing()) { debounce_.stop(); sendSearch(); } });
    connect(kind_, &QComboBox::currentIndexChanged, this, [this] { updateBrowseControls(); debounce_.start(); });
    connect(root_, &QComboBox::currentIndexChanged, this, [this] { updateBrowseControls(); debounce_.start(); });
    connect(code_, &QCheckBox::toggled, this, [this](bool code) { kind_->setEnabled(!code); refreshRoots(); debounce_.start(); });
    connect(results_, &QListWidget::currentRowChanged, this, &MainWindow::selectResult);
    connect(results_, &QListWidget::itemActivated, this, [this] { openCurrent(); });
    connect(preview_, &PreviewPanel::openRequested, this, &MainWindow::openHit);
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
        clearError(errorSource_);
        indexButton_->setEnabled(true);
        send("status");
        send("settings.get");
    });
    connect(backend_, &AppBackend::disconnected, this, [this] {
        connected_ = false;
        runtime_.insert("loaded", false);
        updateRuntimeStatus();
        if (quitting_) { QApplication::quit(); return; }
        indexButton_->setEnabled(false);
        showError(label("The local service disconnected. Restart the application to reconnect.", "本地服务已断开，请重新启动应用以连接。"));
    });
    connect(backend_, &AppBackend::response, this, &MainWindow::handleResponse);
    connect(backend_, &AppBackend::failed, this, &MainWindow::handleFailure);
    connect(backend_, &AppBackend::eventReceived, this, [this](const QString &event, const QJsonObject &data) {
        if (event == "progress") updateProgress(data);
        else if (event == "settings") applySettings(data);
        else if (event == "runtime") {
            runtime_ = data.value("runtime").toObject();
            runtime_.insert("indexDevices", data.value("indexDevices"));
            runtime_.insert("indexDeviceCounts", data.value("indexDeviceCounts"));
            updateRuntimeStatus();
        }
        else if (event == "indexChanged") {
            const QString generation = data.value("browseGeneration").toString();
            const QString visibilityEpoch = data.value("visibilityEpoch").toString();
            const auto changed = [&](const BrowseState &state) {
                return (!generation.isEmpty() && generation != state.generation)
                    || (!visibilityEpoch.isEmpty() && visibilityEpoch != state.visibilityEpoch);
            };
            if (query_->text().trimmed().isEmpty() && !browse_.generation.isEmpty() && !browse_.pending
                && displayedSearchContext_ == searchContext(false)) {
                if (changed(browse_)) {
                    markFilesStale();
                    // An empty page has no selection or preview to preserve.
                    // Coalesce publications until the first files become visible.
                    if (hits_.isEmpty() && !debounce_.isActive()) debounce_.start();
                }
            }
            else refreshSearchForIndexChange();
            if (popup_ && popup_->isVisible()) {
                if (popupQuery_->text().trimmed().isEmpty() && !popupBrowse_.generation.isEmpty() && !popupBrowse_.pending
                    && popupDisplayedContext_ == searchContext(true)) {
                    if (changed(popupBrowse_)) {
                        markFilesStale(true);
                        if (popupHits_.isEmpty() && !popupDebounce_.isActive()) popupDebounce_.start();
                    }
                }
                else refreshSearchForIndexChange(true);
            }
        }
    });
    refresh_.setInterval(2500);
    connect(&refresh_, &QTimer::timeout, this, [this] {
        if (!connected_) return;
        if (!pending_.values().contains("status")) send("status");
        if (auto *dialog = findChild<SettingsDialog *>(); dialog && dialog->isVisible()
            && !pending_.values().contains("models.status"))
            modelStatusRequests_.insert(send("models.status", {{"modelId", dialog->selectedModelId()}}), dialog);
    });
    refresh_.start();
    auto *find = new QShortcut(QKeySequence::Find, this);
    connect(find, &QShortcut::activated, this, &MainWindow::showSearch);
    auto *quit = new QShortcut(QKeySequence::Quit, this);
    connect(quit, &QShortcut::activated, this, &MainWindow::quitApplication);
    query_->installEventFilter(this);
    results_->installEventFilter(this);
    if (useTray_) {
        createTray();
        connect(platform_, &PlatformIntegration::shortcutStatusChanged, this, &MainWindow::applyLanguage);
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
    results_->setWordWrap(false);
    results_->setTextElideMode(Qt::ElideMiddle);
    results_->setSpacing(2);
    results_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    results_->setStyleSheet("QListWidget{border:1px solid #cbd1de;border-radius:7px} QListWidget::item{padding:8px;border-radius:5px} QListWidget::item:selected{background:#e4ecff;color:#102957}");
    results_->setMinimumWidth(240);
    preview_ = new PreviewPanel(splitter);
    preview_->setMinimumWidth(310);
    splitter->addWidget(results_);
    splitter->addWidget(preview_);
    splitter->setStretchFactor(0, 4);
    splitter->setStretchFactor(1, 6);
    layout->addWidget(splitter, 1);
    auto *browseActions = new QHBoxLayout;
    refreshFiles_ = new QPushButton(central);
    refreshFiles_->setObjectName("refreshFilesButton");
    loadMore_ = new QPushButton(central);
    loadMore_->setObjectName("loadMoreFilesButton");
    browseActions->addWidget(refreshFiles_);
    browseActions->addWidget(loadMore_);
    browseActions->addStretch();
    layout->addLayout(browseActions);
    connect(refreshFiles_, &QPushButton::clicked, this, [this] { debounce_.stop(); sendSearch(); });
    connect(loadMore_, &QPushButton::clicked, this, [this] { loadMoreFiles(); });
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
    runtimeStatus_ = new QLabel(central);
    runtimeStatus_->setObjectName("inferenceRuntimeStatus");
    runtimeStatus_->setWordWrap(true);
    runtimeStatus_->setTextFormat(Qt::PlainText);
    runtimeStatus_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    layout->addWidget(runtimeStatus_);
    auto *footer = new QHBoxLayout;
    capability_ = new QLabel(central);
    capability_->setWordWrap(true);
    capability_->setObjectName("shortcutCapability");
    retryShortcut_ = new QPushButton(central);
    retryShortcut_->setObjectName("retryShortcutButton");
    connect(retryShortcut_, &QPushButton::clicked, platform_, &PlatformIntegration::registerGlobalShortcut);
    updateButton_ = new QPushButton(central);
    updateButton_->setFlat(true);
    updateButton_->setObjectName("updatesButton");
    footer->addWidget(capability_, 1);
    footer->addWidget(retryShortcut_);
    footer->addWidget(updateButton_);
    layout->addLayout(footer);
    setCentralWidget(central);
}
void MainWindow::applyLanguage() {
    subtitle_->setText(label("Find the meaning in your files. Local, private, multilingual.", "从本机文件中找到所需内容，支持中英文与多种媒体。"));
    query_->setPlaceholderText(label("Search documents, images, sounds and moments…", "搜索文档、图片、声音与视频片段…"));
    query_->setAccessibleName(label("Search files", "搜索文件"));
    const QSignalBlocker kindBlocker(kind_);
    const QStringList kindValues{"", "text", "pdf", "image", "audio", "video"};
    const QStringList kindLabels{label("All file types", "所有类型"), label("Documents", "文档"),
        QStringLiteral("PDF"), label("Images", "图片"), label("Audio", "音频"), label("Video", "视频")};
    const bool initializeKinds = kind_->count() == 0;
    for (int row = 0; row < kindValues.size(); ++row) {
        if (initializeKinds) kind_->addItem(kindLabels.at(row), kindValues.at(row));
        else {
            const int index = kind_->findData(kindValues.at(row));
            if (index >= 0 && kind_->itemText(index) != kindLabels.at(row))
                kind_->setItemText(index, kindLabels.at(row));
        }
    }
    code_->setText(label("Code search", "代码搜索"));
    code_->setToolTip(label("Enable code indexing in Settings before searching source code and notebook cells.", "请先在设置中启用代码索引，再搜索源代码与 Notebook 单元格。"));
    quickButton_->setText(label("Quick search", "快速搜索"));
    if (popupQuery_) popupQuery_->setPlaceholderText(label("Describe what you want to find…", "描述您想查找的内容…"));
    if (popupFullSearch_) popupFullSearch_->setText(label("Open full search", "打开完整搜索"));
    refreshFiles_->setText(label("Refresh files", "刷新文件列表"));
    loadMore_->setText(label("Load more files", "加载更多文件"));
    if (popupRefreshFiles_) popupRefreshFiles_->setText(refreshFiles_->text());
    modelButton_->setText(label("Manage models", "管理模型"));
    modelButton_->setToolTip(modelAvailable_ ? label("The current model passed loading preflight. Manage or switch models.", "当前模型已通过加载预检。可管理或切换模型。") : label("Keyword search is available. Download and apply a model for semantic search.", "关键词搜索可用。下载并应用模型以启用语义搜索。"));
    settingsButton_->setText(label("Settings", "设置"));
    indexButton_->setText(label("Rescan", "重新扫描"));
    pauseButton_->setText(indexState_ == "paused" ? label("Resume", "继续") : label("Pause", "暂停"));
    retryButton_->setText(label("Retry failed", "重试失败项"));
    updateButton_->setText(label("Check for updates", "检查更新"));
    if (root_->count() == 0) root_->addItem(label("All folders", "所有文件夹"), ""); else root_->setItemText(0, label("All folders", "所有文件夹"));
    if (summary_->text().isEmpty()) summary_->setText(label("Start with a question or keyword", "输入问题或关键词开始搜索"));
    if (progressText_->text().isEmpty()) progressText_->setText(label("Connecting to local service…", "正在连接本地服务…"));
    preview_->setLanguage(language_);
    retryShortcut_->setText(label("Retry shortcut", "重试快捷键"));
    retryShortcut_->setVisible(useTray_);
    if (useTray_) {
        const auto shortcut = platform_->shortcutInfo();
        if (shortcut.state == GlobalShortcutState::Active) capability_->setText(label("Quick search: ", "快速搜索：") + shortcut.binding + label(" · Search: Ctrl+F", " · 搜索：Ctrl+F"));
        else if (shortcut.state == GlobalShortcutState::Pending) capability_->setText(label("Waiting for the desktop shortcut permission…", "正在等待系统快捷键授权…"));
        else capability_->setText(label("Global shortcut unavailable. Use Quick search, the tray or Ctrl+F.", "全局快捷键暂不可用，请使用快速搜索、托盘或 Ctrl+F。"));
        capability_->setToolTip(shortcut.message);
        retryShortcut_->setEnabled(shortcut.state != GlobalShortcutState::Active && shortcut.state != GlobalShortcutState::Pending);
    } else capability_->setText(label("Search: Ctrl+F", "搜索：Ctrl+F"));
    if (useTray_ && !tray_) capability_->setText(capability_->text() + label(" · Tray unavailable, closing the window exits the app.", " · 系统托盘不可用，关闭窗口将退出应用。"));
    updateRuntimeStatus();
    if (tray_) {
        traySearch_->setText(label("Open search", "打开搜索"));
        trayQuick_->setText(label("Quick search", "快速搜索"));
        trayPause_->setText(indexState_ == "paused" ? label("Resume indexing", "继续索引") : label("Pause indexing", "暂停索引"));
        trayQuit_->setText(label("Quit QCopilots Search", "退出 QCopilots Search"));
    }
    updateBrowseControls();
    if (popup_) updateBrowseControls(true);
}
void MainWindow::updateRuntimeStatus() {
    const QString variant = runtime_.value("buildVariant").toString();
    const QString backend = runtime_.value("selectedBackend").toString();
    const QString device = runtime_.value("deviceName").toString();
    const QString fallback = runtime_.value("fallbackReason").toString();
    const QString error = runtime_.value("error").toString();
    const QJsonArray indexDevices = runtime_.value("indexDevices").toArray();
    runtimeStatus_->setVisible(!variant.isEmpty() || !backend.isEmpty() || !error.isEmpty() || !indexDevices.isEmpty());
    QStringList details;
    if (!backend.isEmpty()) details.append(backend.toUpper());
    if (!device.isEmpty()) details.append(device);
    if (!variant.isEmpty()) details.append(variant);
    QString text = runtime_.value("loaded").toBool()
        ? label("Inference: ", "推理：")
        : error.isEmpty() ? label("Last inference (idle): ", "上次推理（空闲）：")
                          : label("Inference unavailable: ", "推理不可用：");
    text += details.join(QStringLiteral(" · "));
    if (!fallback.isEmpty()) text += '\n' + label("CPU fallback: ", "CPU 回退：") + fallback;
    if (!error.isEmpty()) text += '\n' + error;
    QStringList deviceDetails;
    const auto counts = runtime_.value("indexDeviceCounts").toObject();
    const int discovered = counts.value("discovered").toInt();
    if (discovered > 0) {
        QStringList backends;
        for (const auto &entry : indexDevices) {
            const auto item = entry.toObject();
            const QString name = item.value("backend").toString().toUpper();
            if (!name.isEmpty() && name != "CPU" && !backends.contains(name)) backends.append(name);
        }
        text += '\n' + label("Indexing: ", "索引：") + backends.join('/') + QStringLiteral(" · ")
            + label("%1/%2 GPU available", "%1/%2 GPU 可用")
                  .arg(connected_ ? counts.value("ready").toInt() : 0).arg(discovered)
            + label(" · %1 busy · %2 failed", " · %1 忙碌 · %2 故障")
                  .arg(connected_ ? counts.value("busy").toInt() : 0).arg(counts.value("failed").toInt());
    }
    for (const auto &entry : indexDevices) {
        const auto item = entry.toObject();
        const QString state = connected_ ? item.value("state").toString() : QStringLiteral("disconnected");
        const QString stateText = state == "busy" ? label("busy", "忙碌")
            : state == "ready" ? label("ready", "可用")
            : state == "failed" ? label("failed", "故障")
            : state == "idle" ? label("idle", "空闲")
            : state == "disconnected" ? label("disconnected", "已断开")
            : label("initializing", "初始化中");
        QString detail = item.value("name").toString() + QStringLiteral(" · ") + stateText
            + label(" · %1 cumulative batches completed", " · 累计完成 %1 批次").arg(item.value("completedTasks").toInteger());
        const QString activePath = item.value("activePath").toString();
        if (connected_ && !activePath.isEmpty()) detail += QStringLiteral(" · ") + QFileInfo(activePath).fileName();
        const QString deviceError = item.value("error").toString();
        if (!deviceError.isEmpty()) detail += QStringLiteral(" · ") + deviceError;
        text += '\n' + detail;
        QString fullDetail = detail + '\n' + item.value("description").toString();
        const QString id = item.value("deviceId").toString();
        if (!id.isEmpty()) fullDetail += '\n' + label("Device ID: ", "设备 ID：") + id;
        if (connected_ && !activePath.isEmpty()) fullDetail += '\n' + activePath;
        deviceDetails.append(fullDetail);
    }
    runtimeStatus_->setText(text);
    QString tooltip = runtime_.value("deviceDescription").toString();
    if (!deviceDetails.isEmpty()) {
        if (!tooltip.isEmpty()) tooltip += '\n';
        tooltip += label("Batch counts are cumulative, not file counts. A file can require multiple batches, and indexing a changed file adds more batches.\n\n",
                         "批次数是累计处理量，不是文件数。一个文件可能分多批处理，文件修改后重新索引也会增加批次。\n\n");
        tooltip += deviceDetails.join(QStringLiteral("\n\n"));
    }
    runtimeStatus_->setToolTip(tooltip);
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
QJsonObject MainWindow::searchContext(bool popup) const {
    const bool code = !popup && code_->isChecked();
    return {{"query", (popup ? popupQuery_ : query_)->text().trimmed()},
        {"code", code}, {"kind", popup || code ? "" : kind_->currentData().toString()},
        {"root", popup ? "" : root_->currentData().toString()},
        {"modelId", settings_.modelId},
        {"configuredRoots", QJsonArray::fromStringList(code ? settings_.codeRoots : settings_.roots)},
        {"configuredExcludes", QJsonArray::fromStringList(code ? settings_.codeExcludes : settings_.excludes)}};
}
void MainWindow::sendSearch(bool popup) {
    if (!connected_ || quitting_ || (popup && (!popup_ || !popup_->isVisible()))) return;
    SearchEdit *input = popup ? popupQuery_ : query_;
    if (!input || input->isComposing()) return;
    (popup ? popupDebounce_ : debounce_).stop();
    QString &previous = popup ? popupSearch_ : currentSearch_;
    if (!previous.isEmpty()) { backend_->cancel(previous); pending_.remove(previous); resultRequests_.remove(previous); }
    const auto context = searchContext(popup);
    const bool browse = context.value("query").toString().isEmpty();
    auto parameters = context;
    parameters.remove("configuredRoots"); parameters.remove("configuredExcludes"); parameters.remove("modelId");
    parameters.insert("limit", popup ? 12 : 100);
    if (browse) parameters.remove("query"); else parameters.insert("semantic", true);
    auto &state = popup ? popupBrowse_ : browse_;
    if (context != (popup ? popupDisplayedContext_ : displayedSearchContext_)) state = {};
    state.loading = browse;
    state.pending = false;
    state.failure.clear();
    previous = send(browse ? "files.list" : "search", parameters, 120000);
    resultRequests_.insert(previous, {popup, browse, false, context});
    (popup ? popupStatus_ : summary_)->setText(browse ? label("Loading files…", "正在加载文件…") : label("Searching…", "正在搜索…"));
    updateBrowseControls(popup);
}
void MainWindow::refreshSearchForIndexChange(bool popup) {
    const auto id = popup ? popupSearch_ : currentSearch_;
    auto request = resultRequests_.find(id);
    if (request != resultRequests_.end() && !request->browse
        && request->context == searchContext(popup)) {
        // Reconciliation can publish while a cold query model is still loading.
        // Keep that request alive and coalesce publications into one refresh.
        request->refreshAfterResponse = true;
        return;
    }
    (popup ? popupDebounce_ : debounce_).start();
}
void MainWindow::loadMoreFiles(bool popup) {
    auto &state = popup ? popupBrowse_ : browse_;
    const auto context = searchContext(popup);
    if (!connected_ || !context.value("query").toString().isEmpty() || state.loading || state.stale || state.cursor.isEmpty()
        || context != (popup ? popupDisplayedContext_ : displayedSearchContext_)) return;
    auto parameters = context;
    parameters.remove("query"); parameters.remove("configuredRoots"); parameters.remove("configuredExcludes"); parameters.remove("modelId");
    parameters.insert("limit", popup ? 12 : 100);
    parameters.insert("cursor", state.cursor);
    QString &request = popup ? popupSearch_ : currentSearch_;
    request = send("files.list", parameters, 120000);
    resultRequests_.insert(request, {popup, true, true, context});
    state.loading = true;
    state.failure.clear();
    updateBrowseControls(popup);
}
void MainWindow::updateBrowseControls(bool popup) {
    if (popup && !popupQuery_) return;
    const auto context = searchContext(popup);
    const bool browse = context.value("query").toString().isEmpty();
    const auto &state = popup ? popupBrowse_ : browse_;
    auto *more = popup ? nullptr : loadMore_;
    auto *refresh = popup ? popupRefreshFiles_ : refreshFiles_;
    if (!refresh) return;
    refresh->setVisible(browse);
    if (more) {
        more->setVisible(browse);
        more->setEnabled(browse && connected_ && !state.loading && !state.stale && !state.pending && !state.cursor.isEmpty()
            && context == (popup ? popupDisplayedContext_ : displayedSearchContext_));
    }
    refresh->setEnabled(browse && connected_ && !state.loading);
    if (!browse) return;
    QString message;
    if (state.loading) message = label("Loading files…", "正在加载文件…");
    else if (state.pending) message = label("Checking folders. Refresh after indexing finishes.", "正在核对文件目录，完成后请刷新。");
    else if (state.stale) message = label("Files changed. Refresh to continue browsing.", "文件列表已变化，请刷新后继续浏览。");
    else if (!state.failure.isEmpty()) message = state.failure;
    else if (context == (popup ? popupDisplayedContext_ : displayedSearchContext_))
        message = label("Files: ", "文件：") + QString::number((popup ? popupHits_ : hits_).size())
            + (state.cursor.isEmpty() ? label(" · all loaded", " · 已全部加载")
                : popup ? label(" · open full search for more", " · 打开完整搜索查看更多") : label(" · more available", " · 还有更多"));
    if (!message.isEmpty()) (popup ? popupStatus_ : summary_)->setText(message);
}
void MainWindow::markFilesStale(bool popup) {
    if (!(popup ? popupHits_ : hits_).isEmpty()) (popup ? popupDebounce_ : debounce_).stop();
    auto &state = popup ? popupBrowse_ : browse_;
    state.stale = true;
    state.loading = false;
    QString &request = popup ? popupSearch_ : currentSearch_;
    if (!request.isEmpty()) { backend_->cancel(request); pending_.remove(request); resultRequests_.remove(request); request.clear(); }
    updateBrowseControls(popup);
}
void MainWindow::handleResponse(const QString &id, const QJsonObject &data) {
    const QString method = pending_.take(id);
    if (method == "search" || method == "files.list") {
        if (!resultRequests_.contains(id)) return;
        const auto request = resultRequests_.take(id);
        const bool popup = request.popup;
        if (id != (popup ? popupSearch_ : currentSearch_) || request.context != searchContext(popup)) return;
        (popup ? popupSearch_ : currentSearch_).clear();
        auto &state = popup ? popupBrowse_ : browse_;
        state.loading = false;
        if (popup) popupResultQuery_ = request.context.value("query").toString();
        QList<SearchHit> &hits = popup ? popupHits_ : hits_;
        QListWidget *list = popup ? popupResults_ : results_;
        QLabel *summary = popup ? popupStatus_ : summary_;
        const int previousRow = list->currentRow();
        const int scroll = list->verticalScrollBar()->value();
        const SearchHit previous = previousRow >= 0 && previousRow < hits.size() ? hits.at(previousRow) : SearchHit{};
        auto &displayedContext = popup ? popupDisplayedContext_ : displayedSearchContext_;
        const bool sameContext = request.context == displayedContext;
        displayedContext = request.context;
        const QSignalBlocker resultBlocker(list);
        if (!request.append) { hits.clear(); list->clear(); }
        for (const auto &value : data.value(request.browse ? "files" : "hits").toArray()) {
            SearchHit hit;
            if (request.browse) {
                const auto file = FileListEntry::fromJson(value.toObject());
                hit.path = file.path; hit.title = file.title; hit.kind = file.code ? "code" : file.kind; hit.sourceVersion = file.sourceVersion;
            } else hit = SearchHit::fromJson(value.toObject());
            hits.append(hit);
            QString description = (hit.title.isEmpty() ? QFileInfo(hit.path).fileName() : hit.title).simplified();
            description += '\n' + QFileInfo(hit.path).absolutePath().simplified();
            if (!hit.kind.isEmpty()) description += " · " + hit.kind;
            if (hit.page > 0) description += label(" · Page ", " · 页码 ") + QString::number(hit.page);
            if (hit.lineStart > 0) description += label(" · Line ", " · 行号 ") + QString::number(hit.lineStart);
            if (hit.cell > 0) description += label(" · Cell ", " · 单元格 ") + QString::number(hit.cell);
            if (hit.startMs > 0) description += QStringLiteral(" · %1 s").arg(hit.startMs / 1000.0, 0, 'f', 1);
            if (!popup && !hit.matchSource.isEmpty()) {
                const QString source = hit.matchSource == "semantic" ? label("Semantic", "语义匹配")
                    : hit.matchSource == "hybrid" ? label("Semantic + keywords", "语义与关键词")
                    : hit.matchSource == "keyword" ? label("Keywords", "关键词") : hit.matchSource;
                description += " · " + source;
            }
            auto *item = new QListWidgetItem(description, list);
            item->setToolTip(hit.path);
            item->setData(Qt::UserRole, hit.path);
            if (request.browse) item->setData(Qt::UserRole + 1, value.toObject().value("fileId").toString());
        }
        if (request.browse) {
            // A successful first page consumes any queued empty-page refresh.
            // Populated pages follow the normal explicit refresh policy.
            if (!hits.isEmpty()) (popup ? popupDebounce_ : debounce_).stop();
            state.cursor = data.value("hasMore").toBool() ? data.value("nextCursor").toString() : QString();
            state.generation = data.value("browseGeneration").toString();
            state.visibilityEpoch = data.value("visibilityEpoch").toString();
            state.stale = false;
            state.pending = data.value("metadataPending").toBool();
            state.failure.clear();
        } else summary->setText(hits.isEmpty() ? label("No matching files", "没有匹配的文件") : label("Results: ", "结果：") + QString::number(hits.size()) + (data.value("semantic").toBool() ? label(" · semantic + keywords", " · 语义与关键词") : label(" · keywords", " · 关键词")));
        int selection = hits.isEmpty() ? -1 : 0;
        bool sameHit = false;
        if (sameContext) {
            for (int row = 0; row < hits.size(); ++row) {
                const auto &hit = hits.at(row);
                if (preservesPreview(previous, hit)) {
                    selection = row;
                    sameHit = true;
                    break;
                }
            }
        }
        list->setCurrentRow(selection);
        if (sameContext) list->verticalScrollBar()->setValue(scroll); else list->scrollToTop();
        if (!popup) {
            if (sameHit) preview_->refreshHitMetadata(hits.at(selection));
            else selectResult(selection);
        }
        updateBrowseControls(popup);
        const QString warning = data.value("warning").toString(data.value("error").toString());
        if (popup) {
            if (!warning.isEmpty()) popupStatus_->setText(popupStatus_->text() + '\n' + warning);
        } else {
            if (!warning.isEmpty()) showError(warning, ErrorSource::SearchWarning);
            else clearError(ErrorSource::SearchWarning);
        }
        if (request.refreshAfterResponse && (!popup || popup_->isVisible()))
            (popup ? popupDebounce_ : debounce_).start();
    } else if (method == "document.preview") {
        if (id == documentRequest_ && preview_->currentHit().path == documentPath_) {
            const bool append = documentStart_ > 0;
            documentRequest_.clear();
            preview_->setDocumentPreview(data, append);
        }
    } else if (method == "models.status") {
        if (auto dialog = modelStatusRequests_.take(id)) dialog->setModelStatus(data);
    } else if (method == "settings.get" || method == "settings.set" || method == "settings.reload" || method == "models.select") {
        applySettings(data.contains("settings") ? data.value("settings").toObject() : data);
        if (auto dialog = settingsOperations_.take(id)) {
            dialog->setModelOperationPending(false);
            if (method == "settings.reload") {
                connect(dialog.data(), &QObject::destroyed, this, [this] { showSettings(false, true); });
                dialog->reject();
            } else {
                const auto request = send("models.status", {{"modelId", dialog->selectedModelId()}});
                modelStatusRequests_.insert(request, dialog);
            }
        }
        if (method != "settings.get") send("status");
        if (!shownOnboarding_ && onboarding_) {
            shownOnboarding_ = true;
            if (settings_.roots.isEmpty()) QTimer::singleShot(0, this, [this] { showSettings(true); });
            else if (settings_.checkUpdates) QTimer::singleShot(5000, this, [this] { checkForUpdates(false); });
        }
    } else if (method == "status") {
        modelAvailable_ = data.value("modelAvailable").toBool();
        runtime_ = data.value("runtime").toObject();
        runtime_.insert("indexDevices", data.value("indexDevices"));
        runtime_.insert("indexDeviceCounts", data.value("indexDeviceCounts"));
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
    if (method.isEmpty()) return;
    if (auto dialog = modelStatusRequests_.take(id)) { dialog->setModelStatusError(message.isEmpty() ? code : message); return; }
    if (auto dialog = settingsOperations_.take(id)) dialog->setModelError(message.isEmpty() ? code : message);
    if (method == "search" || method == "files.list") {
        if (!resultRequests_.contains(id)) return;
        const auto request = resultRequests_.take(id);
        if (id != (request.popup ? popupSearch_ : currentSearch_) || request.context != searchContext(request.popup)) return;
        (request.popup ? popupSearch_ : currentSearch_).clear();
        auto &state = request.popup ? popupBrowse_ : browse_;
        state.loading = false;
        if (code == "cursor_stale" || code == "invalid_cursor") {
            markFilesStale(request.popup);
            return;
        }
        if (code == "metadata_pending") {
            state.pending = true;
            (request.popup ? popupStatus_ : summary_)->setText(label("Checking folders. Results will refresh when indexing finishes.", "正在核对文件目录，完成后将刷新结果。"));
            updateBrowseControls(request.popup);
            return;
        }
        if (code != "cancelled" && code != "canceled") {
            state.failure = request.browse && code == "unknown_method"
                ? label("The application and local service use incompatible versions. Restart QCopilots Search and update both components if this continues.", "应用与本地服务版本不兼容，请重新启动 QCopilots Search，如仍有问题请同步更新应用和服务。")
                : message.isEmpty() ? code : message;
            if (request.popup) popupStatus_->setText(state.failure);
            else { showError(state.failure); summary_->setText(label("Could not load results", "无法加载结果")); }
        }
        // A failed append retains its cursor, rows, selection and preview for retry.
        updateBrowseControls(request.popup);
        return;
    }
    if (code.compare("cancelled", Qt::CaseInsensitive) == 0 || code.compare("canceled", Qt::CaseInsensitive) == 0) return;
    if (method == "models.reload" && code == "busy" && modelReloadAttempts_++ < 20 && !quitting_) {
        statusBar()->showMessage(label("Waiting for the current index update before loading the model…", "正在等待当前索引更新完成，然后加载模型…"), 5000);
        QTimer::singleShot(500, this, [this] { if (!quitting_) send("models.reload", {}, 120000); });
        return;
    }
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
    const auto previousContext = searchContext(false);
    const auto previousPopupContext = popup_ ? searchContext(true) : QJsonObject{};
    const bool modelChanged = settingsLoaded_ && settings_.modelId != parsed.modelId;
    settings_ = parsed;
    if (modelChanged) {
        for (const auto &request : {currentSearch_, popupSearch_, documentRequest_}) {
            if (!request.isEmpty()) { backend_->cancel(request); pending_.remove(request); resultRequests_.remove(request); }
        }
        currentSearch_.clear(); popupSearch_.clear(); documentRequest_.clear();
        hits_.clear(); results_->clear(); selectResult(-1);
        popupHits_.clear();
        if (popupResults_) popupResults_->clear();
        browse_ = {}; popupBrowse_ = {};
        displayedSearchContext_ = {}; popupDisplayedContext_ = {};
        modelAvailable_ = false;
        runtime_.insert("loaded", false);
    }
    language_ = settings_.language == "system" ? QLocale::system().name() : settings_.language;
    code_->setEnabled(settings_.includeCode);
    if (!settings_.includeCode) code_->setChecked(false);
    refreshRoots();
    applyLanguage();
    if (!settingsLoaded_ || previousContext != searchContext(false)) debounce_.start();
    if (popup_ && popup_->isVisible() && (!settingsLoaded_ || previousPopupContext != searchContext(true))) popupDebounce_.start();
    settingsLoaded_ = true;
    if (auto *dialog = findChild<SettingsDialog *>()) dialog->updateConfiguredModel(settings_.modelId);
}
void MainWindow::refreshRoots() {
    const QSignalBlocker rootBlocker(root_);
    const auto &paths = code_->isChecked() ? settings_.codeRoots : settings_.roots;
    bool unchanged = root_->count() == paths.size() + 1;
    for (int row = 0; unchanged && row < paths.size(); ++row) unchanged = root_->itemData(row + 1).toString() == paths.at(row);
    if (unchanged) { root_->setItemText(0, label("All folders", "所有文件夹")); return; }
    const QString previousRoot = root_->currentData().toString();
    root_->clear();
    root_->addItem(label("All folders", "所有文件夹"), "");
    for (const auto &path : paths) root_->addItem(path, path);
    root_->setCurrentIndex(qMax(0, root_->findData(previousRoot)));
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
    if (!error.isEmpty()) showError(error, ErrorSource::IndexProgress);
    else clearError(ErrorSource::IndexProgress);
}
void MainWindow::showSettings(bool firstRun, bool modelsPage) {
    if (auto *existing = findChild<SettingsDialog *>()) { if (modelsPage) existing->showModelsPage(); existing->raise(); existing->activateWindow(); return; }
    const bool allowStartup = PlatformIntegration::canSetStartAtLogin(paths_.profileDir, paths_.modelDir);
    SettingsDialog::StartupOptions startup;
    startup.allowed = allowStartup;
    startup.profileDir = paths_.profileDir;
    startup.modelDir = paths_.modelDir;
    if (allowStartup) startup.state = PlatformIntegration::startAtLoginState();
    auto *dialog = new SettingsDialog(settings_, firstRun, startup, this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    connect(dialog, &QDialog::accepted, this, [this, dialog, allowStartup] {
        const auto settings = dialog->settings();
        if (allowStartup && dialog->startupChangeRequested()) {
            QString error;
            if (!PlatformIntegration::setStartAtLogin(settings.startAtLogin, paths_.profileDir, paths_.modelDir, &error)) { showError(error); return; }
        }
        auto changes = settings.toJson();
        // Pausing belongs to the service's live task state. A preferences dialog
        // may stay open while that state changes and must never overwrite it.
        changes.remove("indexingPaused");
        // These JSON-only rules have no controls here. Preserve the service's
        // current values, including updates made while this dialog was open.
        changes.remove("generalFileRules");
        // A model switch made by another client must survive a preferences-only
        // save from a dialog opened before that switch.
        if (!dialog->modelChangeRequested()) changes.remove("modelId");
        send("settings.set", changes, 600000);
    });
    const auto requestModelStatus = [this, dialog](const QString &modelId) {
        modelStatusRequests_.insert(send("models.status", {{"modelId", modelId}}), dialog);
    };
    connect(dialog, &SettingsDialog::modelStatusRequested, this, requestModelStatus);
    connect(dialog, &SettingsDialog::manageModelRequested, this, [this, dialog, requestModelStatus](const QString &modelId) {
        auto *models = new ModelDialog(paths_.modelDir, language_, modelId, dialog);
        models->setAttribute(Qt::WA_DeleteOnClose);
        connect(models, &ModelDialog::modelsReady, dialog, [dialog, requestModelStatus, modelId] {
            dialog->noteVerifiedModel(modelId);
            requestModelStatus(modelId);
        });
        models->open();
    });
    connect(dialog, &SettingsDialog::applyModelRequested, this, [this, dialog](const QString &modelId) {
        settingsOperations_.insert(send("models.select", {{"modelId", modelId}}, 600000), dialog);
    });
    connect(dialog, &SettingsDialog::reloadSettingsRequested, this, [this, dialog] {
        settingsOperations_.insert(send("settings.reload", {}, 600000), dialog);
    });
    if (modelsPage) dialog->showModelsPage();
    dialog->open();
    requestModelStatus(dialog->selectedModelId());
}
void MainWindow::showModels() {
    showSettings(false, true);
}
void MainWindow::showError(const QString &message, ErrorSource source) {
    errorSource_ = source;
    error_->setText(message);
    error_->show();
}
void MainWindow::clearError(ErrorSource source) {
    // A successful status or search only clears its own message. An unrelated
    // failure may have replaced it, even with exactly the same wording.
    if (errorSource_ != source) return;
    error_->clear();
    error_->hide();
    errorSource_ = ErrorSource::Other;
}
void MainWindow::openCurrent() {
    const int row = results_->currentRow();
    if (row < 0 || row >= hits_.size()) return;
    openHit(hits_.at(row));
}
void MainWindow::openHit(const SearchHit &hit) {
    QString error;
    if (!PlatformIntegration::openHit(hit, settings_, &error)) showError(error);
    else if (!error.isEmpty()) statusBar()->showMessage(error, 15000);
}
void MainWindow::selectResult(int row) {
    if (!documentRequest_.isEmpty()) { backend_->cancel(documentRequest_); pending_.remove(documentRequest_); documentRequest_.clear(); }
    if (row >= 0 && row < hits_.size()) {
        preview_->showHit(hits_.at(row));
        if (displayedSearchContext_.value("query").toString().isEmpty()) preview_->loadDocumentText();
    } else preview_->clear();
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
        popupResults_->setWordWrap(false);
        popupResults_->setTextElideMode(Qt::ElideMiddle);
        popupResults_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        popupStatus_ = new QLabel(popup_);
        popupStatus_->setObjectName("quickSearchStatus");
        popupStatus_->setWordWrap(true);
        layout->addWidget(popupQuery_);
        layout->addWidget(popupResults_, 1);
        layout->addWidget(popupStatus_);
        auto *browseActions = new QHBoxLayout;
        popupRefreshFiles_ = new QPushButton(popup_);
        popupRefreshFiles_->setObjectName("quickRefreshFilesButton");
        browseActions->addWidget(popupRefreshFiles_);
        browseActions->addStretch();
        layout->addLayout(browseActions);
        connect(popupRefreshFiles_, &QPushButton::clicked, this, [this] { popupDebounce_.stop(); sendSearch(true); });
        popupFullSearch_ = new QPushButton(label("Open full search", "打开完整搜索"), popup_);
        popupFullSearch_->setObjectName("openFullSearchButton");
        layout->addWidget(popupFullSearch_);
        connect(popupFullSearch_, &QPushButton::clicked, this, [this] {
            popupDebounce_.stop();
            if (!popupSearch_.isEmpty()) {
                backend_->cancel(popupSearch_);
                pending_.remove(popupSearch_);
                resultRequests_.remove(popupSearch_);
                popupSearch_.clear();
            }
            // Quick search always covers ordinary files of every type and folder.
            code_->setChecked(false);
            kind_->setCurrentIndex(0);
            root_->setCurrentIndex(0);
            query_->setText(popupQuery_->text());
            popup_->hide();
            showSearch();
            sendSearch();
        });
        connect(popupQuery_, &QLineEdit::textChanged, this, [this] {
            if (!popupQuery_->isComposing()) {
                popupHits_.clear();
                popupResults_->clear();
                popupResultQuery_.clear();
                updateBrowseControls(true);
                popupDebounce_.start();
            }
        });
        connect(popupQuery_, &SearchEdit::compositionFinished, &popupDebounce_, qOverload<>(&QTimer::start));
        const auto open = [this] {
            if (popupQuery_->text().trimmed() != popupResultQuery_) { popupDebounce_.stop(); sendSearch(true); return; }
            const int row = popupResults_->currentRow();
            if (row < 0 || row >= popupHits_.size()) { sendSearch(true); return; }
            QString error;
            if (PlatformIntegration::openHit(popupHits_.at(row), settings_, &error)) {
                if (error.isEmpty()) popup_->hide(); else popupStatus_->setText(error);
            } else popupStatus_->setText(error);
        };
        connect(popupQuery_, &QLineEdit::returnPressed, this, [this, open] { if (!popupQuery_->isComposing()) open(); });
        connect(popupResults_, &QListWidget::itemActivated, this, open);
        popupQuery_->installEventFilter(this);
        popupResults_->installEventFilter(this);
    }
    applyLanguage();
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
    QNetworkRequest request(QUrl("https://api.github.com/repos/xiaobing318/QCopilots-Search/releases/latest"));
    request.setRawHeader("User-Agent", "qcopilots-search/" + QCoreApplication::applicationVersion().toUtf8());
    request.setRawHeader("Accept", "application/vnd.github+json");
    request.setTransferTimeout(15000);
    auto *reply = network_->get(request);
    updateButton_->setEnabled(false);
    connect(reply, &QNetworkReply::finished, this, [this, reply, manual] {
        updateButton_->setEnabled(true);
        reply->deleteLater();
        const auto showInformation = [this](const QString &message) {
            QMessageBox prompt(QMessageBox::Information, label("Updates", "更新"), message, QMessageBox::Ok, this);
            prompt.button(QMessageBox::Ok)->setText(label("OK", "确定"));
            prompt.exec();
        };
        if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() == 404) {
            if (manual) showInformation(label("No public release is available yet.", "目前尚无公开发布版本。"));
            return;
        }
        if (reply->error() != QNetworkReply::NoError) { if (manual) showError(label("Update check failed: ", "检查更新失败：") + reply->errorString()); return; }
        const QJsonObject release = QJsonDocument::fromJson(reply->readAll()).object();
        QString version = release.value("tag_name").toString();
        if (version.startsWith('v')) version.remove(0, 1);
        const QVersionNumber latest = QVersionNumber::fromString(version);
        const QVersionNumber installed = QVersionNumber::fromString(QCoreApplication::applicationVersion());
        if (latest.isNull() || latest <= installed) {
            if (manual) showInformation(label("You are using the latest available version.", "当前已是最新可用版本。"));
            return;
        }
        const QUrl url(release.value("html_url").toString());
        if (url.scheme() != "https" || url.host() != "github.com" || !url.path().startsWith("/xiaobing318/QCopilots-Search/releases/")) { if (manual) showError(label("The update response contains an unexpected download address.", "更新响应中的下载地址不符合预期。")); return; }
        QMessageBox prompt(QMessageBox::Information, label("Update available", "有可用更新"), label("Version ", "版本 ") + version + label(" is available. Open the release page to download it?", " 已发布，是否打开发布页面下载？"), QMessageBox::Open | QMessageBox::Cancel, this);
        prompt.button(QMessageBox::Open)->setText(label("Open", "打开"));
        prompt.button(QMessageBox::Cancel)->setText(label("Cancel", "取消"));
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
