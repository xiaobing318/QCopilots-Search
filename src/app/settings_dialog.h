#pragma once
#include "core/app_settings.h"
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
    SettingsDialog(const AppSettings &settings, bool firstRun, bool allowStartup, QWidget *parent = nullptr);
    AppSettings settings() const;
    bool startupChangeRequested() const { return startupChangeRequested_; }
private:
    AppSettings original_;
    bool startupChangeRequested_ = false;
    QListWidget *roots_, *excludes_, *codeRoots_, *codeExcludes_;
    QComboBox *language_, *pdfPreset_, *mediaPreset_;
    QCheckBox *code_, *tray_, *updates_, *startup_;
    QLineEdit *editor_, *pdfProgram_, *mediaProgram_;
    QPlainTextEdit *pdfArguments_, *mediaArguments_;
};
}
