#pragma once
#include "core/app_settings.h"
#include "core/runtime_paths.h"
#include "core/types.h"
#include <QHash>
#include <QJsonObject>
#include <QLineEdit>
#include <QMainWindow>
#include <QTimer>
class QAction;
class QCheckBox;
class QCloseEvent;
class QComboBox;
class QDialog;
class QLabel;
class QListWidget;
class QNetworkAccessManager;
class QProgressBar;
class QPushButton;
class QSystemTrayIcon;
namespace qcopilots_search {
class AppBackend;
class PlatformIntegration;
class PreviewPanel;

class SearchEdit final : public QLineEdit {
    Q_OBJECT
public:
    using QLineEdit::QLineEdit;
    bool isComposing() const { return composing_; }
signals:
    void compositionFinished();
protected:
    void inputMethodEvent(QInputMethodEvent *event) override;
private:
    bool composing_ = false;
};

class MainWindow final : public QMainWindow {
    Q_OBJECT
public:
    MainWindow(const RuntimePaths &paths, AppBackend *backend, bool useTray = true,
               bool onboarding = true, QWidget *parent = nullptr);
    ~MainWindow() override;
    void start();
    void showSearch();
    void showQuickSearch();
    static QIcon applicationIcon();
protected:
    void closeEvent(QCloseEvent *event) override;
    bool eventFilter(QObject *watched, QEvent *event) override;
private:
    QString label(const char *en, const char *zh) const;
    void buildUi();
    void applyLanguage();
    void createTray();
    void sendSearch(bool popup = false);
    void loadMoreFiles(bool popup = false);
    void updateBrowseControls(bool popup = false);
    void markFilesStale(bool popup = false);
    void refreshRoots();
    QJsonObject searchContext(bool popup) const;
    void openHit(const SearchHit &hit);
    QString send(const QString &method, const QJsonObject &params = {}, int timeoutMs = 30000);
    void handleResponse(const QString &id, const QJsonObject &data);
    void handleFailure(const QString &id, const QString &code, const QString &message);
    void applySettings(const QJsonObject &json);
    void updateProgress(const QJsonObject &progress);
    void showSettings(bool firstRun = false);
    void showModels();
    void openCurrent();
    void selectResult(int row);
    void showError(const QString &message);
    void checkForUpdates(bool manual);
    void quitApplication();
    RuntimePaths paths_;
    AppBackend *backend_;
    AppSettings settings_;
    PlatformIntegration *platform_;
    QString language_;
    bool useTray_, onboarding_, shownOnboarding_ = false, quitting_ = false, connected_ = false, modelAvailable_ = false, settingsLoaded_ = false;
    SearchEdit *query_;
    QComboBox *kind_, *root_;
    QCheckBox *code_;
    QListWidget *results_;
    PreviewPanel *preview_;
    QLabel *heading_, *subtitle_, *summary_, *error_, *progressText_, *capability_;
    QPushButton *settingsButton_, *modelButton_, *indexButton_, *pauseButton_, *retryButton_, *quickButton_, *updateButton_;
    QPushButton *loadMore_, *refreshFiles_, *retryShortcut_;
    QProgressBar *progress_;
    QSystemTrayIcon *tray_ = nullptr;
    QAction *traySearch_ = nullptr, *trayQuick_ = nullptr, *trayPause_ = nullptr, *trayQuit_ = nullptr;
    QDialog *popup_ = nullptr;
    SearchEdit *popupQuery_ = nullptr;
    QListWidget *popupResults_ = nullptr;
    QLabel *popupStatus_ = nullptr;
    QPushButton *popupFullSearch_ = nullptr;
    QPushButton *popupRefreshFiles_ = nullptr;
    QTimer debounce_, popupDebounce_, refresh_;
    QString currentSearch_, popupSearch_, indexState_;
    struct ResultRequest {
        bool popup = false, browse = false, append = false;
        QJsonObject context;
    };
    struct BrowseState {
        QString cursor, generation, visibilityEpoch, failure;
        bool stale = false, pending = false, loading = false;
    };
    QHash<QString, ResultRequest> resultRequests_;
    QJsonObject displayedSearchContext_, popupDisplayedContext_;
    BrowseState browse_, popupBrowse_;
    QString popupResultQuery_;
    QString lastSearchWarning_;
    QString documentRequest_, documentPath_;
    int documentStart_ = 0;
    int modelReloadAttempts_ = 0;
    QList<SearchHit> hits_, popupHits_;
    QHash<QString, QString> pending_;
    QNetworkAccessManager *network_;
};
}
