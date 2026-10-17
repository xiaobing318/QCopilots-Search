#include "model_dialog.h"
#include "core/model_catalog.h"
#include "core/model_files.h"
#include <QDialogButtonBox>
#include <QDir>
#include <QFileDialog>
#include <QFutureWatcher>
#include <QLabel>
#include <QProgressBar>
#include <QPushButton>
#include <QStringList>
#include <QThread>
#include <QVBoxLayout>
#include <QtConcurrent>

namespace qcopilots_search {
ModelDialog::ModelDialog(const QString &modelDir, const QString &language, QWidget *parent)
    : ModelDialog(modelDir, language, defaultModelId(), parent) {}
ModelDialog::ModelDialog(const QString &modelDir, const QString &language, const QString &modelId, QWidget *parent)
    : QDialog(parent), modelDir_(modelDir), language_(language), modelId_(modelId), files_(new ModelFiles(modelDir, modelId)), downloadThread_(new QThread(this)) {
    files_->moveToThread(downloadThread_);
    connect(downloadThread_, &QThread::finished, files_, &QObject::deleteLater);
    downloadThread_->start();
    setObjectName("modelDialog");
    setWindowTitle(label("Semantic model", "语义模型"));
    resize(680, 380);
    auto *layout = new QVBoxLayout(this);
    auto *description = new QLabel(this);
    description->setObjectName("modelDescription");
    const auto *model = findModel(modelId_);
    if (model) {
        qint64 downloadBytes = 0;
        for (const auto &artifact : model->artifacts) downloadBytes += artifact.size;
        QStringList capabilities{label("Text", "文本")};
        if (model->supportsVision) capabilities << label("images", "图像") << label("video frames", "视频画面");
        if (model->supportsAudio) capabilities << label("audio", "音频");
        description->setText(label("%1\n%2 dimensions · %3 GiB download\nSupported: %4. Downloaded and imported files are verified with SHA-256. Apply settings to load this model after verification.", "%1\n%2 维向量 · 下载量 %3 GiB\n支持：%4。下载与导入文件均通过 SHA-256 校验，完成后请点击设置页的“应用”加载此模型。")
            .arg(model->name).arg(model->dimensions).arg(double(downloadBytes) / (1024.0 * 1024.0 * 1024.0), 0, 'f', 2).arg(capabilities.join(label(", ", "、"))));
        if (!model->supportsAudio) description->setText(description->text() + label("\nAudio files retain keyword search. This model does not support audio semantic search.", "\n音频文件保留关键词搜索，此模型不支持音频语义搜索。"));
    } else {
        description->setText(label("Unknown model: ", "未知模型：") + modelId_);
    }
    description->setWordWrap(true);
    layout->addWidget(description);
    auto *path = new QLabel(QDir::toNativeSeparators(files_->modelDirectory()), this);
    path->setObjectName("modelDirectory");
    path->setWordWrap(true);
    path->setTextInteractionFlags(Qt::TextSelectableByMouse);
    layout->addWidget(path);
    status_ = new QLabel(this);
    status_->setObjectName("modelStatus");
    status_->setWordWrap(true);
    progress_ = new QProgressBar(this);
    progress_->setObjectName("modelProgress");
    layout->addWidget(status_);
    layout->addWidget(progress_);
    auto *actions = new QHBoxLayout;
    download_ = new QPushButton(label("Download / retry", "下载 / 重试"), this);
    download_->setObjectName("downloadModelButton");
    import_ = new QPushButton(label("Import from folder…", "从文件夹导入…"), this);
    import_->setObjectName("importModelButton");
    verify_ = new QPushButton(label("Verify files", "校验文件"), this);
    verify_->setObjectName("verifyModelButton");
    cancel_ = new QPushButton(label("Cancel download", "取消下载"), this);
    cancel_->setObjectName("cancelModelButton");
    actions->addWidget(download_);
    actions->addWidget(import_);
    actions->addWidget(verify_);
    actions->addWidget(cancel_);
    layout->addLayout(actions);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Close, this);
    buttons->button(QDialogButtonBox::Close)->setObjectName("closeModelButton");
    buttons->button(QDialogButtonBox::Close)->setText(label("Close", "关闭"));
    connect(buttons, &QDialogButtonBox::rejected, this, &ModelDialog::reject);
    layout->addWidget(buttons);
    connect(download_, &QPushButton::clicked, this, [this] { downloading_ = true; setBusy(true); status_->setText(label("Downloading and verifying…", "正在下载与校验…")); QMetaObject::invokeMethod(files_, &ModelFiles::downloadMissing, Qt::QueuedConnection); });
    connect(import_, &QPushButton::clicked, this, &ModelDialog::importModels);
    connect(verify_, &QPushButton::clicked, this, &ModelDialog::verifyModels);
    connect(cancel_, &QPushButton::clicked, this, [this] { status_->setText(label("Cancelling after the current verification step…", "当前校验步骤结束后取消…")); QMetaObject::invokeMethod(files_, &ModelFiles::cancel, Qt::QueuedConnection); });
    connect(files_, &ModelFiles::progress, this, [this](qint64 received, qint64 total) {
        progress_->setRange(0, total > 0 ? 1000 : 0);
        if (total > 0) progress_->setValue(int(received * 1000 / total));
        status_->setText(label("Downloaded ", "已下载 ") + QString::number(received / 1048576) + " / " + QString::number(total / 1048576) + " MiB");
    });
    connect(files_, &ModelFiles::finished, this, &ModelDialog::finishOperation);
    verifyModels();
}
ModelDialog::~ModelDialog() {
    if (downloadThread_->isRunning()) {
        QMetaObject::invokeMethod(files_, &ModelFiles::cancel, Qt::BlockingQueuedConnection);
        downloadThread_->quit();
        downloadThread_->wait();
    }
}
QString ModelDialog::label(const char *en, const char *zh) const { return QString::fromUtf8(language_.startsWith("zh") ? zh : en); }
void ModelDialog::setBusy(bool busy) {
    download_->setEnabled(!busy);
    import_->setEnabled(!busy);
    verify_->setEnabled(!busy);
    cancel_->setEnabled(busy && downloading_);
    if (busy) progress_->setRange(0, 0);
}
void ModelDialog::verifyModels() {
    importing_ = true;
    setBusy(true);
    status_->setText(label("Checking installed model files…", "正在校验已安装的模型文件…"));
    auto *watcher = new QFutureWatcher<QPair<bool, QString>>(this);
    connect(watcher, &QFutureWatcherBase::finished, this, [this, watcher] {
        const auto result = watcher->result();
        watcher->deleteLater();
        importing_ = false;
        setBusy(false);
        progress_->setRange(0, 100);
        progress_->setValue(result.first ? 100 : 0);
        status_->setText(result.first ? label("Model files are verified. Apply settings to load this model.", "模型文件已校验，请点击设置页的“应用”加载此模型。") : label("Keyword search is ready. Download or import this semantic model.\n", "关键词搜索已就绪，可下载或导入此语义模型。\n") + result.second);
        if (result.first) emit modelsReady();
    });
    const QString directory = modelDir_;
    const QString modelId = modelId_;
    watcher->setFuture(QtConcurrent::run([directory, modelId] { ModelFiles files(directory, modelId); QString error; const bool ok = files.verify(&error); return qMakePair(ok, error); }));
}
void ModelDialog::importModels() {
    const QString source = QFileDialog::getExistingDirectory(this, label("Select the folder containing both GGUF files", "选择包含两个 GGUF 文件的文件夹"));
    if (source.isEmpty()) return;
    importing_ = true;
    setBusy(true);
    status_->setText(label("Importing and verifying model files…", "正在导入与校验模型文件…"));
    auto *watcher = new QFutureWatcher<QPair<bool, QString>>(this);
    connect(watcher, &QFutureWatcherBase::finished, this, [this, watcher] {
        const auto result = watcher->result();
        watcher->deleteLater();
        importing_ = false;
        finishOperation(result.first, result.second);
    });
    const QString destination = modelDir_;
    const QString modelId = modelId_;
    watcher->setFuture(QtConcurrent::run([source, destination, modelId] {
        ModelFiles files(destination, modelId);
        QString error;
        for (const auto &artifact : ModelFiles::manifest(modelId)) if (!files.importFile(QDir(source).filePath(artifact.name), artifact.name, &error)) return qMakePair(false, error);
        return qMakePair(files.verify(&error), error);
    }));
}
void ModelDialog::finishOperation(bool success, const QString &error) {
    downloading_ = false;
    setBusy(false);
    progress_->setRange(0, 100);
    progress_->setValue(success ? 100 : 0);
    status_->setText(success ? label("Model files are verified. Apply settings to load this model.", "模型文件已校验，请点击设置页的“应用”加载此模型。") : label("Operation did not finish: ", "操作未完成：") + error);
    if (success) emit modelsReady();
    if (closePending_) QDialog::reject();
}
void ModelDialog::reject() {
    if (importing_) { status_->setText(label("Please wait for file verification to finish before closing.", "请等待文件校验完成后再关闭。")); return; }
    if (downloading_) {
        closePending_ = true;
        status_->setText(label("Cancelling the download before closing…", "正在取消下载，完成后关闭…"));
        QMetaObject::invokeMethod(files_, &ModelFiles::cancel, Qt::QueuedConnection);
        return;
    }
    QDialog::reject();
}
}
