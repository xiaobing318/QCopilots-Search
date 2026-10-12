#include "settings_dialog.h"
#include "platform_integration.h"
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
#include <QPushButton>
#include <QTabWidget>
#include <QVBoxLayout>

namespace qcopilots_search {
SettingsDialog::SettingsDialog(const AppSettings &settings, bool firstRun, bool allowStartup, QWidget *parent)
    : QDialog(parent), original_(settings) {
    setObjectName("settingsDialog");
    const bool chinese = (settings.language == "system" ? QLocale::system().name() : settings.language).startsWith("zh");
    const auto text = [chinese](const char *en, const char *zh) { return QString::fromUtf8(chinese ? zh : en); };
    setWindowTitle(firstRun ? text("Welcome to QCopilots Search", "欢迎使用 QCopilots Search") : text("Settings", "设置"));
    resize(680, 580);
    auto *layout = new QVBoxLayout(this);
    auto *intro = new QLabel(firstRun ? text("Choose the folders you want to search. Your files stay on this device. You can use keyword search immediately and add the semantic model when ready.", "选择需要搜索的文件夹。文件保留在本机，可立即使用关键词搜索，也可随后添加语义模型。") : text("Only selected folders are read. Excluded folders apply to every source folder.", "只读取您选择的文件夹。排除文件夹对所有索引目录生效。"), this);
    intro->setWordWrap(true);
    layout->addWidget(intro);
    auto *tabs = new QTabWidget(this);
    auto *folders = new QWidget(tabs);
    auto *folderLayout = new QVBoxLayout(folders);
    const auto folderGroup = [&](const QString &title, const char *name, const QStringList &values, QListWidget **list) {
        auto *group = new QGroupBox(title, folders);
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
            const QString canonical = QFileInfo(path).canonicalFilePath();
            if (target->findItems(canonical, Qt::MatchExactly).isEmpty()) target->addItem(canonical);
        });
        connect(remove, &QPushButton::clicked, this, [target] { delete target->takeItem(target->currentRow()); });
        folderLayout->addWidget(group);
    };
    folderGroup(text("Search folders", "搜索文件夹"), "rootFolders", settings.roots, &roots_);
    folderGroup(text("Excluded folders", "排除文件夹"), "excludedFolders", settings.excludes, &excludes_);
    code_ = new QCheckBox(text("Index source code and notebook cells in a separate code search", "在独立的代码搜索中索引源代码和 Notebook 单元格"), folders);
    code_->setObjectName("includeCodeCheck");
    code_->setChecked(settings.includeCode);
    folderLayout->addWidget(code_);
    tabs->addTab(folders, text("Folders", "文件夹"));
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
    startup_->setEnabled(allowStartup && PlatformIntegration::supportsStartAtLogin());
    startup_->setObjectName("startAtLoginCheck");
    if (!startup_->isEnabled()) startup_->setToolTip(text("Available on Windows with the default application profile. Configure other platforms in the system startup settings.", "仅在 Windows 默认应用配置中可用。其他平台请在系统启动项中配置。"));
    form->addRow(startup_);
    updates_ = new QCheckBox(text("Check for updates after startup", "启动后检查新版本"), preferences);
    updates_->setChecked(settings.checkUpdates);
    updates_->setObjectName("checkUpdatesCheck");
    form->addRow(updates_);
    tabs->addTab(preferences, text("Preferences", "偏好设置"));
    layout->addWidget(tabs, 1);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, this);
    buttons->button(QDialogButtonBox::Save)->setObjectName("saveSettingsButton");
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
    for (int i = 0; i < roots_->count(); ++i) result.roots << QDir::cleanPath(roots_->item(i)->text());
    for (int i = 0; i < excludes_->count(); ++i) result.excludes << QDir::cleanPath(excludes_->item(i)->text());
    result.language = language_->currentData().toString();
    result.includeCode = code_->isChecked();
    result.editorCommand = editor_->text().trimmed();
    result.minimizeToTray = tray_->isChecked();
    result.checkUpdates = updates_->isChecked();
    if (startup_->isEnabled()) result.startAtLogin = startup_->isChecked();
    return result;
}
}
