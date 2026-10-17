#pragma once
#include <QDialog>
class QLabel;
class QProgressBar;
class QPushButton;
class QThread;
namespace qcopilots_search {
class ModelFiles;
class ModelDialog final : public QDialog {
    Q_OBJECT
public:
    ModelDialog(const QString &modelDir, const QString &language, QWidget *parent = nullptr);
    ModelDialog(const QString &modelDir, const QString &language, const QString &modelId, QWidget *parent = nullptr);
    ~ModelDialog() override;
signals:
    void modelsReady();
protected:
    void reject() override;
private:
    QString label(const char *en, const char *zh) const;
    void importModels();
    void verifyModels();
    void finishOperation(bool success, const QString &error);
    void setBusy(bool busy);
    QString modelDir_, language_, modelId_;
    ModelFiles *files_;
    QThread *downloadThread_;
    QLabel *status_;
    QProgressBar *progress_;
    QPushButton *download_, *import_, *verify_, *cancel_;
    bool importing_ = false;
    bool downloading_ = false;
    bool closePending_ = false;
};
}
