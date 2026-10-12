#pragma once
#include "core/app_settings.h"
#include <QDialog>
class QCheckBox;
class QComboBox;
class QLineEdit;
class QListWidget;
namespace qcopilots_search {
class SettingsDialog final : public QDialog {
    Q_OBJECT
public:
    SettingsDialog(const AppSettings &settings, bool firstRun, bool allowStartup, QWidget *parent = nullptr);
    AppSettings settings() const;
private:
    AppSettings original_;
    QListWidget *roots_, *excludes_;
    QComboBox *language_;
    QCheckBox *code_, *tray_, *updates_, *startup_;
    QLineEdit *editor_;
};
}
