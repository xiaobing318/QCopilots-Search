#pragma once
#include "core/app_settings.h"
#include "login_startup.h"
#include <QDialog>
#include <QSet>
class QCheckBox;
class QComboBox;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QLabel;
class QPushButton;
class QTabWidget;
namespace qcopilots_search {
class SettingsDialog final : public QDialog {
    Q_OBJECT
public:
    struct StartupOptions {
        bool allowed = false;
        QString profileDir;
        QString modelDir;
        LoginStartupState state;
    };
    SettingsDialog(const AppSettings &settings, bool firstRun, const StartupOptions &startup, QWidget *parent = nullptr);
    AppSettings settings() const;
    bool startupChangeRequested() const { return startupChangeRequested_; }
    QString selectedModelId() const;
    bool modelChangeRequested() const;
    void updateConfiguredModel(const QString &modelId);
    void showModelsPage();
    void setModelStatus(const QJsonObject &status);
    void setModelOperationPending(bool pending);
    void setModelError(const QString &error);
    void setModelStatusError(const QString &error);
    void noteVerifiedModel(const QString &modelId);
signals:
    void modelStatusRequested(QString modelId);
    void manageModelRequested(QString modelId);
    void applyModelRequested(QString modelId);
    void reloadSettingsRequested();
private:
    void updateModelDescription();
    QString label(const char *en, const char *zh) const;
    AppSettings original_;
    bool startupChangeRequested_ = false;
    bool startupStatusKnown_ = false;
    QListWidget *roots_, *excludes_, *codeRoots_, *codeExcludes_;
    QComboBox *language_, *pdfPreset_, *mediaPreset_;
    QCheckBox *code_, *tray_, *updates_, *startup_;
    QLineEdit *editor_, *pdfProgram_, *mediaProgram_;
    QPlainTextEdit *pdfArguments_, *mediaArguments_;
    QTabWidget *tabs_;
    QWidget *modelsPage_;
    QComboBox *model_;
    QLabel *modelDescription_, *modelStatus_;
    QPushButton *manageModel_, *applyModel_, *reloadSettings_, *save_;
    bool modelOperationPending_ = false;
    QSet<QString> verifiedModels_;
};
}
