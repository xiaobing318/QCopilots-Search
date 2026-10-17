#include "settings_dialog.h"
#include "core/model_catalog.h"
#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QGroupBox>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QScrollArea>
#include <QSignalBlocker>
#include <QTabWidget>
#include <QVBoxLayout>

namespace qcopilots_search {
SettingsDialog::SettingsDialog(const AppSettings &settings, bool firstRun, const StartupOptions &startup, QWidget *parent)
    : QDialog(parent), original_(settings) {
    setObjectName("settingsDialog");
    const bool chinese = (settings.language == "system" ? QLocale::system().name() : settings.language).startsWith("zh");
    const auto text = [chinese](const char *en, const char *zh) { return QString::fromUtf8(chinese ? zh : en); };
    setWindowTitle(firstRun ? text("Welcome to QCopilots Search", "欢迎使用 QCopilots Search") : text("Settings", "设置"));
    resize(740, 640);
    auto *layout = new QVBoxLayout(this);
    auto *intro = new QLabel(firstRun ? text("Choose the folders you want to search. Your files stay on this device. You can use keyword search immediately and add the semantic model when ready.", "选择需要搜索的文件夹。文件保留在本机，可立即使用关键词搜索，也可随后添加语义模型。") : text("General files and source code use separate folders and exclusions. Only selected folders are read.", "普通文件与代码分别设置搜索目录和排除目录，只读取您选择的文件夹。"), this);
    intro->setWordWrap(true);
    layout->addWidget(intro);
    auto *tabs = new QTabWidget(this);
    tabs_ = tabs;
    tabs->setObjectName("settingsTabs");
    auto *folders = new QWidget(tabs);
    auto *folderLayout = new QVBoxLayout(folders);
    const auto folderGroup = [&](QWidget *page, QVBoxLayout *pageLayout, const QString &title, const char *name, const QStringList &values, QListWidget **list) {
        auto *group = new QGroupBox(title, page);
        auto *groupLayout = new QVBoxLayout(group);
        *list = new QListWidget(group);
        (*list)->setObjectName(name);
        (*list)->addItems(values);
        groupLayout->addWidget(*list);
        auto *buttons = new QHBoxLayout;
        auto *add = new QPushButton(text("Add folder…", "添加文件夹…"), group);
        add->setObjectName(QString::fromUtf8(name) + "Add");
        auto *remove = new QPushButton(text("Remove selection", "移除所选项"), group);
        remove->setObjectName(QString::fromUtf8(name) + "Remove");
        buttons->addWidget(add);
        buttons->addWidget(remove);
        buttons->addStretch();
        groupLayout->addLayout(buttons);
        auto *target = *list;
        connect(add, &QPushButton::clicked, this, [this, target, title] {
            const QString path = QFileDialog::getExistingDirectory(this, title);
            if (path.isEmpty()) return;
            const QFileInfo info(path);
            const QString canonical = QDir::cleanPath(info.canonicalFilePath().isEmpty() ? info.absoluteFilePath() : info.canonicalFilePath());
            for (int i = 0; i < target->count(); ++i)
                if (QDir::cleanPath(target->item(i)->text()) == canonical) return;
            target->addItem(canonical);
        });
        connect(remove, &QPushButton::clicked, this, [target] { delete target->takeItem(target->currentRow()); });
        pageLayout->addWidget(group);
        return group;
    };
    folderGroup(folders, folderLayout, text("Search folders", "搜索文件夹"), "rootFolders", settings.roots, &roots_);
    folderGroup(folders, folderLayout, text("Excluded folders", "排除文件夹"), "excludedFolders", settings.excludes, &excludes_);
    tabs->addTab(folders, text("General files", "普通文件"));
    auto *codeFolders = new QWidget(tabs);
    auto *codeLayout = new QVBoxLayout(codeFolders);
    code_ = new QCheckBox(text("Index source code and notebook cells in a separate code search", "在独立的代码搜索中索引源代码和 Notebook 单元格"), codeFolders);
    code_->setObjectName("includeCodeCheck");
    code_->setChecked(settings.includeCode);
    codeLayout->addWidget(code_);
    auto *codeHelp = new QLabel(text("Code search reads only the folders below. General search folders and exclusions do not apply here. Turning code indexing off preserves these choices.", "代码搜索只读取下方目录，普通文件的搜索和排除目录不影响这里。关闭代码索引会保留这些设置。"), codeFolders);
    codeHelp->setWordWrap(true);
    codeLayout->addWidget(codeHelp);
    auto *codeRootsGroup = folderGroup(codeFolders, codeLayout, text("Code folders", "代码文件夹"), "codeRootFolders", settings.codeRoots, &codeRoots_);
    auto *codeExcludesGroup = folderGroup(codeFolders, codeLayout, text("Excluded code folders", "排除代码文件夹"), "codeExcludedFolders", settings.codeExcludes, &codeExcludes_);
    codeRootsGroup->setEnabled(settings.includeCode);
    codeExcludesGroup->setEnabled(settings.includeCode);
    connect(code_, &QCheckBox::toggled, codeRootsGroup, &QWidget::setEnabled);
    connect(code_, &QCheckBox::toggled, codeExcludesGroup, &QWidget::setEnabled);
    tabs->addTab(codeFolders, text("Source code", "源代码"));
    modelsPage_ = new QWidget(tabs);
    auto *modelLayout = new QVBoxLayout(modelsPage_);
    auto *modelForm = new QFormLayout;
    model_ = new QComboBox(modelsPage_);
    model_->setObjectName("modelCombo");
    for (const auto &model : modelCatalog()) model_->addItem(model.name, model.id);
    model_->setCurrentIndex(qMax(0, model_->findData(settings.modelId)));
    modelForm->addRow(text("Semantic model", "语义模型"), model_);
    modelLayout->addLayout(modelForm);
    modelDescription_ = new QLabel(modelsPage_);
    modelDescription_->setObjectName("selectedModelDescription");
    modelDescription_->setWordWrap(true);
    modelLayout->addWidget(modelDescription_);
    modelStatus_ = new QLabel(text("Checking model availability…", "正在查看模型可用性…"), modelsPage_);
    modelStatus_->setObjectName("selectedModelStatus");
    modelStatus_->setWordWrap(true);
    modelStatus_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    modelLayout->addWidget(modelStatus_);
    auto *modelActions = new QHBoxLayout;
    manageModel_ = new QPushButton(text("Download / import / verify…", "下载 / 导入 / 校验…"), modelsPage_);
    manageModel_->setObjectName("manageSelectedModelButton");
    applyModel_ = new QPushButton(text("Apply selected model", "应用所选模型"), modelsPage_);
    applyModel_->setObjectName("applySelectedModelButton");
    modelActions->addWidget(manageModel_);
    modelActions->addWidget(applyModel_);
    modelActions->addStretch();
    modelLayout->addLayout(modelActions);
    auto *modelHelp = new QLabel(text("Downloading or importing a model does not change the active model. Apply verifies and loads the selected model before switching. Each model keeps an independent index and uses additional disk space. Its first use builds an index, and switching back reuses unchanged files after checking your folders.", "下载或导入模型不会更改当前使用的模型。应用时会先校验并加载所选模型，成功后再切换。每个模型保存独立索引并占用额外磁盘空间，首次使用需要建立索引，切回时会先核对文件夹并复用未变化的文件。"), modelsPage_);
    modelHelp->setObjectName("modelIndexHelp");
    modelHelp->setWordWrap(true);
    modelLayout->addWidget(modelHelp);
    reloadSettings_ = new QPushButton(text("Reload configuration file", "重新读取配置文件"), modelsPage_);
    reloadSettings_->setObjectName("reloadSettingsButton");
    reloadSettings_->setToolTip(text("Apply edits saved in the configuration file. This replaces unsaved changes in this dialog after a successful reload.", "应用已保存到配置文件的修改。重新读取成功后，会替换此窗口尚未保存的修改。"));
    modelLayout->addWidget(reloadSettings_, 0, Qt::AlignLeft);
    modelLayout->addStretch();
    tabs->addTab(modelsPage_, text("Models", "模型"));
    connect(model_, &QComboBox::currentIndexChanged, this, [this] {
        updateModelDescription();
        modelStatus_->setText(label("Checking model availability…", "正在查看模型可用性…"));
        emit modelStatusRequested(selectedModelId());
    });
    connect(manageModel_, &QPushButton::clicked, this, [this] { emit manageModelRequested(selectedModelId()); });
    connect(applyModel_, &QPushButton::clicked, this, [this] { setModelOperationPending(true); emit applyModelRequested(selectedModelId()); });
    connect(reloadSettings_, &QPushButton::clicked, this, [this] { setModelOperationPending(true); emit reloadSettingsRequested(); });
    updateModelDescription();
    auto *externalScroll = new QScrollArea(tabs);
    externalScroll->setWidgetResizable(true);
    externalScroll->setFrameShape(QFrame::NoFrame);
    auto *external = new QWidget(externalScroll);
    auto *externalLayout = new QVBoxLayout(external);
    auto *externalHelp = new QLabel(text("The system default opens the file without guaranteed page or time positioning. Choose a supported application to request positioning. Install external applications yourself. QCopilots Search does not install them and reports an error if the chosen application cannot be started.", "系统默认应用只打开文件，不保证定位到命中页或时刻。选择受支持的应用可请求定位。外部应用需要自行安装，QCopilots Search 不会自动安装，所选应用无法启动时会明确提示。"), external);
    externalHelp->setWordWrap(true);
    externalHelp->setObjectName("externalApplicationsHelp");
    externalLayout->addWidget(externalHelp);
    const auto openerGroup = [&](bool pdf, const ExternalOpenerSettings &opener, QComboBox **preset, QLineEdit **program, QPlainTextEdit **arguments) {
        const QString prefix = pdf ? QStringLiteral("pdfOpener") : QStringLiteral("mediaOpener");
        auto *group = new QGroupBox(pdf ? text("PDF reader", "PDF 阅读器") : text("Audio and video player", "音视频播放器"), external);
        auto *openerForm = new QFormLayout(group);
        *preset = new QComboBox(group);
        auto *presetBox = *preset;
        presetBox->setObjectName(prefix + "Preset");
        presetBox->addItem(text("System default (open file only)", "系统默认（只打开文件）"), "system");
        if (pdf) {
#ifdef Q_OS_WIN
            presetBox->addItem(QStringLiteral("SumatraPDF"), "sumatra");
#elif defined(Q_OS_MACOS)
            presetBox->addItem(QStringLiteral("Skim"), "skim");
#else
            presetBox->addItem(QStringLiteral("Okular"), "okular");
#endif
        } else {
            presetBox->addItem(QStringLiteral("mpv"), "mpv");
        }
        presetBox->addItem(text("Custom command", "自定义命令"), "custom");
        if (presetBox->findData(opener.preset) < 0) {
            presetBox->addItem(text("Unavailable on this platform: ", "当前平台不可用：") + opener.preset, opener.preset);
            presetBox->setItemData(presetBox->count() - 1, true, Qt::UserRole + 1);
        }
        presetBox->setCurrentIndex(presetBox->findData(opener.preset));
        openerForm->addRow(text("Application", "应用"), presetBox);
        auto *programRow = new QWidget(group);
        auto *programLayout = new QHBoxLayout(programRow);
        programLayout->setContentsMargins(0, 0, 0, 0);
        *program = new QLineEdit(opener.program, programRow);
        auto *programEdit = *program;
        programEdit->setObjectName(prefix + "Program");
        programLayout->addWidget(programEdit, 1);
        auto *browse = new QPushButton(text("Browse…", "浏览…"), programRow);
        browse->setObjectName(prefix + "Browse");
        programLayout->addWidget(browse);
        openerForm->addRow(text("Application path", "应用路径"), programRow);
        *arguments = new QPlainTextEdit(group);
        auto *argumentsEdit = *arguments;
        argumentsEdit->setObjectName(prefix + "Arguments");
        argumentsEdit->setPlainText(opener.arguments.join(QLatin1Char('\n')));
        argumentsEdit->setProperty("initialArgumentText", argumentsEdit->toPlainText());
        argumentsEdit->setTabChangesFocus(true);
        argumentsEdit->setLineWrapMode(QPlainTextEdit::NoWrap);
        argumentsEdit->setMaximumHeight(110);
        argumentsEdit->setPlaceholderText(pdf ? QStringLiteral("--page\n{page}\n{file}") : QStringLiteral("--start=+{startSeconds}\n--\n{file}"));
        auto *argumentsLabel = new QLabel(text("Arguments, one per line", "参数，每行一项"), group);
        openerForm->addRow(argumentsLabel, argumentsEdit);
        auto *help = new QLabel(group);
        help->setWordWrap(true);
        help->setObjectName(prefix + "Help");
        openerForm->addRow(help);
        const auto updateOpener = [=] {
            const QString selected = presetBox->currentData().toString();
            const bool unavailable = presetBox->currentData(Qt::UserRole + 1).toBool();
            const bool custom = selected == "custom";
            programRow->setEnabled(selected != "system" && !unavailable);
            argumentsEdit->setVisible(custom);
            argumentsLabel->setVisible(custom);
            programEdit->setPlaceholderText(custom ? text("Executable path or name", "可执行程序路径或名称") : selected == "skim" ? text("Optional Skim.app path", "可选的 Skim.app 路径") : text("Leave empty to find the installed application", "留空以查找已安装的应用"));
            if (unavailable) {
                help->setText(text("This saved application is unavailable on this platform. Choose a supported application or the system default.", "已保存的应用在当前平台不可用，请选择受支持的应用或系统默认。"));
            } else if (selected == "system") {
                help->setText(text("Uses your system file association. The matching page or time remains available in the preview panel.", "使用系统文件关联。命中页或时刻仍可在应用内预览框中查看。"));
            } else if (custom) {
                help->setText(text("One argument per line. Do not add quotes around arguments containing spaces. Supported placeholders: {file}, {page}, {line}, {cell}, {startMs}, {startSeconds}. The file path is appended if {file} is absent. Commands run directly without a shell.", "每行填写一个参数，含空格的参数不需要额外引号。支持 {file}、{page}、{line}、{cell}、{startMs}、{startSeconds}。未填写 {file} 时会追加文件路径。命令通过独立参数直接执行，不经过 shell。"));
            } else if (selected == "skim") {
                help->setText(text("Requests the matching PDF page using Skim's deep link. Leave the path empty for the installed Skim, or choose its .app bundle.", "通过 Skim 深链接请求定位 PDF 命中页。路径留空时使用已安装的 Skim，也可选择其 .app 应用包。"));
            } else if (selected == "sumatra" || selected == "okular") {
                help->setText(text("Requests the matching physical PDF page, starting at page 1. You can override the application path. Choose Custom command to edit arguments.", "请求定位到 PDF 的实际页码，从第 1 页开始。可指定应用路径，需要修改参数时请选择自定义命令。"));
            } else if (selected == "mpv") {
                help->setText(text("Requests playback from the matching time in seconds. You can override the mpv executable path. Choose Custom command to edit arguments.", "请求从命中时刻开始播放，时间以秒为单位。可指定 mpv 可执行程序路径，需要修改参数时请选择自定义命令。"));
            } else {
                help->setText(text("This saved application is unavailable on this platform. Choose a supported application or the system default.", "已保存的应用在当前平台不可用，请选择受支持的应用或系统默认。"));
            }
        };
        connect(presetBox, &QComboBox::currentIndexChanged, this, [updateOpener](int) { updateOpener(); });
        connect(browse, &QPushButton::clicked, this, [this, presetBox, programEdit, text] {
            const QString path = presetBox->currentData().toString() == "skim"
                ? QFileDialog::getExistingDirectory(this, text("Choose Skim.app", "选择 Skim.app"), programEdit->text())
                : QFileDialog::getOpenFileName(this, text("Choose executable", "选择可执行程序"), programEdit->text());
            if (!path.isEmpty()) programEdit->setText(QDir::cleanPath(path));
        });
        updateOpener();
        externalLayout->addWidget(group);
    };
    openerGroup(true, settings.pdfOpener, &pdfPreset_, &pdfProgram_, &pdfArguments_);
    openerGroup(false, settings.mediaOpener, &mediaPreset_, &mediaProgram_, &mediaArguments_);
    externalLayout->addStretch();
    externalScroll->setWidget(external);
    tabs->addTab(externalScroll, text("External applications", "外部应用"));
    auto *preferences = new QWidget(tabs);
    auto *form = new QFormLayout(preferences);
    language_ = new QComboBox(preferences);
    language_->setObjectName("languageCombo");
    language_->addItem(text("System language", "跟随系统"), "system");
    language_->addItem("English", "en");
    language_->addItem(QString::fromUtf8("简体中文"), "zh_CN");
    language_->setCurrentIndex(qMax(0, language_->findData(settings.language)));
    form->addRow(text("Language", "界面语言"), language_);
    editor_ = new QLineEdit(settings.editorCommand, preferences);
    editor_->setObjectName("editorCommand");
    editor_->setPlaceholderText(QStringLiteral("code --goto \"{file}:{line}\""));
    form->addRow(text("Code editor", "代码编辑器"), editor_);
    auto *editorHelp = new QLabel(text("Quote paths containing spaces. Supported placeholders: {file}, {line}, {cell}. The command runs directly without a shell.", "路径含空格时请加引号。支持 {file}、{line}、{cell} 占位符，使用参数直接启动编辑器。"), preferences);
    editorHelp->setWordWrap(true);
    form->addRow(editorHelp);
    tray_ = new QCheckBox(text("Keep running in the tray when the window closes", "关闭窗口时保留托盘运行"), preferences);
    tray_->setChecked(settings.minimizeToTray);
    tray_->setObjectName("minimizeToTrayCheck");
    form->addRow(tray_);
    startup_ = new QCheckBox(text("Start when I sign in", "登录系统时启动"), preferences);
    startup_->setChecked(settings.startAtLogin);
    startup_->setEnabled(startup.allowed && startup.state.status != LoginStartupStatus::Unavailable);
    startup_->setObjectName("startAtLoginCheck");
    form->addRow(startup_);
    auto *startupStatus = new QLabel(preferences);
    startupStatus->setObjectName("startAtLoginStatus");
    startupStatus->setWordWrap(true);
    auto *useCurrentStartup = new QPushButton(text("Use current directories at sign-in", "使用当前目录登录启动"), preferences);
    useCurrentStartup->setObjectName("useCurrentStartupDirectoriesButton");
    useCurrentStartup->setToolTip(text("Save the current data and model directories as the single startup target for this user.", "保存当前数据和模型目录，作为此用户唯一的登录启动目标。"));
    useCurrentStartup->hide();
    bool registeredDifferentTarget = false;
    if (!startup.allowed) {
        startupStatus->setText(text("Login startup cannot be managed in test sessions or when --profile-dir or --model-dir is supplied. Start normally with QCS_HOME to manage it.", "测试会话或使用 --profile-dir、--model-dir 参数时不能管理登录启动。请使用 QCS_HOME 正常启动应用后设置。"));
    } else if (!startup_->isEnabled()) {
        startupStatus->setText(text("Login startup management is unavailable here. Use the system login or startup settings.", "当前环境无法在应用内管理登录启动，请使用系统的登录项或启动项设置。"));
    } else {
        const auto &state = startup.state;
        startupStatusKnown_ = state.status == LoginStartupStatus::Disabled
            || state.status == LoginStartupStatus::Enabled || state.status == LoginStartupStatus::ApprovalRequired;
        switch (state.status) {
        case LoginStartupStatus::Disabled:
            startup_->setChecked(false);
            startupStatus->setText(text("System status: disabled. Changes take effect when you save.", "系统状态：未启用。更改将在保存后生效。"));
            break;
        case LoginStartupStatus::Enabled:
            startup_->setChecked(true);
            startupStatus->setText(text("System status: enabled. Changes take effect when you save.", "系统状态：已启用。更改将在保存后生效。"));
            break;
        case LoginStartupStatus::ApprovalRequired:
            startup_->setChecked(true);
            startupStatus->setText(text("System status: approval required. Allow QCopilots Search in the system login settings.", "系统状态：等待授权。请在系统登录项设置中允许 QCopilots Search。"));
            break;
        case LoginStartupStatus::Unavailable:
            startup_->setEnabled(false);
            startupStatus->setText(text("System status: unavailable. Use the system login or startup settings.", "系统状态：不可用。请使用系统的登录项或启动项设置。"));
            break;
        case LoginStartupStatus::Error:
            startup_->setChecked(false);
            startup_->setEnabled(false);
            startupStatus->setText(text("Could not read the system login startup status.", "无法读取系统登录启动状态。"));
            useCurrentStartup->show();
            break;
        }
        const auto sameDirectory = [](const QString &left, const QString &right) {
            const auto normalized = [](const QString &path) {
                const QString canonical = QFileInfo(path).canonicalFilePath();
                return QDir::cleanPath(canonical.isEmpty() ? path : canonical);
            };
#ifdef Q_OS_WIN
            constexpr Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
            constexpr Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif
            return normalized(left).compare(normalized(right), pathCase) == 0;
        };
        const bool registered = state.status == LoginStartupStatus::Enabled
            || state.status == LoginStartupStatus::ApprovalRequired;
        if (registered) {
            const QString target = text("Registered data directory: %1\nRegistered model directory: %2", "已注册的数据目录：%1\n已注册的模型目录：%2")
                .arg(QDir::toNativeSeparators(state.profileDir), QDir::toNativeSeparators(state.modelDir));
            registeredDifferentTarget = !sameDirectory(state.profileDir, startup.profileDir)
                || !sameDirectory(state.modelDir, startup.modelDir);
            if (registeredDifferentTarget) {
                startup_->setChecked(false);
                startup_->setEnabled(false);
                startupStatus->setText(text("Another data or model directory is registered for sign-in. Use the button below to switch the startup target when you save.", "登录启动已注册其他数据或模型目录。点击下方按钮后保存，可切换启动目标。") + QLatin1Char('\n') + target);
                useCurrentStartup->show();
            } else {
                startupStatus->setText(startupStatus->text() + QLatin1Char('\n') + target);
            }
        }
        if (!state.message.isEmpty()) startupStatus->setToolTip(state.message);
    }
    if (!startup_->isEnabled()) startup_->setToolTip(startupStatus->text());
    form->addRow(startupStatus);
    form->addRow(useCurrentStartup);
    const QString initialStartupStatus = startupStatus->text();
    connect(startup_, &QCheckBox::toggled, this, [this, registeredDifferentTarget, startupStatus, useCurrentStartup, initialStartupStatus](bool checked) {
        if (!startup_->isEnabled()) return;
        if (registeredDifferentTarget && !checked) {
            // Cancelling a pending takeover must not disable the other profile.
            startupChangeRequested_ = false;
            startup_->setEnabled(false);
            startupStatus->setText(initialStartupStatus);
            useCurrentStartup->setEnabled(true);
            return;
        }
        startupChangeRequested_ = true;
    });
    connect(useCurrentStartup, &QPushButton::clicked, this, [this, startupStatus, useCurrentStartup, startup, text] {
        startup_->setEnabled(true);
        startup_->setChecked(true);
        startup_->setToolTip({});
        startupChangeRequested_ = true;
        startupStatus->setText(text("These directories will be used at sign-in after you save.\nData: %1\nModels: %2", "保存后将使用以下目录登录启动。\n数据：%1\n模型：%2")
            .arg(QDir::toNativeSeparators(startup.profileDir), QDir::toNativeSeparators(startup.modelDir)));
        useCurrentStartup->setEnabled(false);
    });
    updates_ = new QCheckBox(text("Check for updates after startup", "启动后检查新版本"), preferences);
    updates_->setChecked(settings.checkUpdates);
    updates_->setObjectName("checkUpdatesCheck");
    form->addRow(updates_);
    tabs->addTab(preferences, text("Preferences", "偏好设置"));
    layout->addWidget(tabs, 1);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, this);
    buttons->button(QDialogButtonBox::Save)->setObjectName("saveSettingsButton");
    save_ = buttons->button(QDialogButtonBox::Save);
    buttons->button(QDialogButtonBox::Save)->setText(firstRun ? text("Start indexing", "开始索引") : text("Save", "保存"));
    buttons->button(QDialogButtonBox::Cancel)->setText(firstRun ? text("Set up later", "稍后设置") : text("Cancel", "取消"));
    connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
}
AppSettings SettingsDialog::settings() const {
    auto result = original_;
    result.roots.clear();
    result.excludes.clear();
    result.codeRoots.clear();
    result.codeExcludes.clear();
    // Unchanged paths must retain their spelling so preference-only saves do not
    // change the indexing fingerprint, selected folder, or preview context.
    for (int i = 0; i < roots_->count(); ++i) result.roots << roots_->item(i)->text();
    for (int i = 0; i < excludes_->count(); ++i) result.excludes << excludes_->item(i)->text();
    for (int i = 0; i < codeRoots_->count(); ++i) result.codeRoots << codeRoots_->item(i)->text();
    for (int i = 0; i < codeExcludes_->count(); ++i) result.codeExcludes << codeExcludes_->item(i)->text();
    result.language = language_->currentData().toString();
    result.modelId = selectedModelId();
    result.includeCode = code_->isChecked();
    result.editorCommand = editor_->text().trimmed();
    const auto readOpener = [](const ExternalOpenerSettings &original, const QComboBox *preset, const QLineEdit *program, const QPlainTextEdit *arguments) {
        auto opener = original;
        opener.preset = preset->currentData().toString();
        opener.program = program->text().trimmed();
        const QString argumentText = arguments->toPlainText();
        if (argumentText != arguments->property("initialArgumentText").toString()) {
            opener.arguments = argumentText.isEmpty() ? QStringList{} : argumentText.split(QLatin1Char('\n'), Qt::KeepEmptyParts);
        }
        return opener;
    };
    result.pdfOpener = readOpener(original_.pdfOpener, pdfPreset_, pdfProgram_, pdfArguments_);
    result.mediaOpener = readOpener(original_.mediaOpener, mediaPreset_, mediaProgram_, mediaArguments_);
    result.minimizeToTray = tray_->isChecked();
    result.checkUpdates = updates_->isChecked();
    if (startupStatusKnown_ || startupChangeRequested_) result.startAtLogin = startup_->isChecked();
    return result;
}
QString SettingsDialog::label(const char *en, const char *zh) const {
    const QString language = original_.language == "system" ? QLocale::system().name() : original_.language;
    return QString::fromUtf8(language.startsWith("zh") ? zh : en);
}
QString SettingsDialog::selectedModelId() const { return model_->currentData().toString(); }
bool SettingsDialog::modelChangeRequested() const { return selectedModelId() != original_.modelId; }
void SettingsDialog::updateConfiguredModel(const QString &modelId) {
    const bool userChangedModel = modelChangeRequested();
    original_.modelId = modelId;
    if (!userChangedModel) {
        const QSignalBlocker blocker(model_);
        model_->setCurrentIndex(model_->findData(modelId));
        updateModelDescription();
    }
    // Keep other unsaved preferences and an explicit pending model choice.
    // Refresh the actual service state even when the selected model stays put.
    if (!modelOperationPending_) modelStatus_->setText(label("Checking model availability…", "正在查看模型可用性…"));
    emit modelStatusRequested(selectedModelId());
}
void SettingsDialog::showModelsPage() { tabs_->setCurrentWidget(modelsPage_); }
void SettingsDialog::updateModelDescription() {
    const auto *model = findModel(selectedModelId());
    if (!model) return;
    qint64 bytes = 0;
    for (const auto &artifact : model->artifacts) bytes += artifact.size;
    const QString capabilities = model->supportsAudio
        ? label("Text, images, audio and video", "文字、图像、音频与视频")
        : label("Text, images and video frames. Audio supports keyword search only.", "文字、图像与视频画面。音频仅支持关键词搜索。");
    modelDescription_->setText(label("%1\n%2 dimensions · Download: %3 GiB\n%4", "%1\n%2 维向量 · 下载量：%3 GiB\n%4")
        .arg(model->name).arg(model->dimensions).arg(double(bytes) / (1024.0 * 1024 * 1024), 0, 'f', 2).arg(capabilities));
}
void SettingsDialog::setModelStatus(const QJsonObject &status) {
    if (modelOperationPending_ || status.value("modelId").toString() != selectedModelId()) return;
    const QString configured = status.value("configuredModelId").toString(original_.modelId);
    if (configured != original_.modelId) return;
    const QString active = status.value("activeModelId").toString();
    const auto modelName = [](const QString &id) { const auto *model = findModel(id); return model ? model->name : id; };
    QString state;
    if (status.value("loading").toBool())
        state = label("Verifying files and checking model loading…", "正在校验文件并进行模型加载预检…");
    else if (status.value("modelAvailable").toBool() && active == selectedModelId())
        state = label("Loading preflight passed. Available for search.", "加载预检通过，可以使用。");
    else if (status.value("filesVerified").toBool()
        || (status.value("filesPresent").toBool() && verifiedModels_.contains(selectedModelId())))
        state = label("Files verified. Apply to check loading.", "文件已校验。请应用以检查是否能加载。");
    else if (status.value("filesPresent").toBool())
        state = label("Files present. Verification and loading have not completed.", "文件已存在，尚未完成校验和加载。");
    else state = label("Model files missing. Download or import them first.", "缺少模型文件，请先下载或导入。");
    QString text = label("Configured: %1\nCurrent model: %2\nSelected model: %3", "配置目标：%1\n当前使用：%2\n所选模型：%3")
        .arg(modelName(configured), active.isEmpty() ? label("None (keyword search)", "无（关键词搜索）") : modelName(active), state);
    const QString indexState = status.value("indexState").toString();
    QString indexStatus;
    if (indexState == "not-built") indexStatus = label("Not built yet", "尚未建立");
    else if (indexState == "cached") indexStatus = label("Saved cache, checked when selected", "已有缓存，切回时核对");
    else if (indexState == "reconciling") indexStatus = label("Checking folders", "正在核对文件夹");
    else if (indexState == "empty") indexStatus = label("No files in selected folders", "所选目录中没有文件");
    else if (indexState == "pending") indexStatus = label("Paused with pending files", "已暂停，仍有文件待处理");
    else if (indexState == "indexing") indexStatus = label("Indexing", "正在建立索引");
    else if (indexState == "partial") indexStatus = label("Some files failed", "部分文件处理失败");
    else if (indexState == "ready") indexStatus = label("Ready", "已就绪");
    if (!indexStatus.isEmpty()) text += label("\nIndex status: ", "\n索引状态：") + indexStatus;
    const QString directory = status.value("indexDirectory").toString();
    if (!directory.isEmpty()) text += label("\nIndependent index: ", "\n独立索引：") + QDir::toNativeSeparators(directory);
    const QString error = status.value("error").toString();
    if (!error.isEmpty()) text += QLatin1Char('\n') + error;
    modelStatus_->setText(text);
}
void SettingsDialog::setModelOperationPending(bool pending) {
    modelOperationPending_ = pending;
    model_->setEnabled(!pending);
    manageModel_->setEnabled(!pending);
    applyModel_->setEnabled(!pending);
    reloadSettings_->setEnabled(!pending);
    save_->setEnabled(!pending);
    if (pending) modelStatus_->setText(label("Verifying and loading the model. Large models may take several minutes. The current model remains active until the switch succeeds.", "正在校验并加载模型。大模型可能需要几分钟，切换成功前保留当前模型。"));
}
void SettingsDialog::setModelError(const QString &error) {
    setModelOperationPending(false);
    modelStatus_->setText(error);
}
void SettingsDialog::noteVerifiedModel(const QString &modelId) { verifiedModels_.insert(modelId); }
void SettingsDialog::setModelStatusError(const QString &error) {
    if (!modelOperationPending_) modelStatus_->setText(error);
}
}
