#pragma once
#include "core/app_settings.h"
#include "login_startup.h"
#include <QDialog>
class QCheckBox;
class QComboBox;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
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
private:
    AppSettings original_;
    bool startupChangeRequested_ = false;
    bool startupStatusKnown_ = false;
    QListWidget *roots_, *excludes_, *codeRoots_, *codeExcludes_;
    QComboBox *language_, *pdfPreset_, *mediaPreset_;
    QCheckBox *code_, *tray_, *updates_, *startup_;
    QLineEdit *editor_, *pdfProgram_, *mediaProgram_;
    QPlainTextEdit *pdfArguments_, *mediaArguments_;
};
}
