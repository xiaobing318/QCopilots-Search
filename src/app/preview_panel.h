#pragma once
#include "core/types.h"
#include <QImage>
#include <QThreadPool>
#include <QWidget>
#include <atomic>
#include <memory>
class QAudioOutput;
class QLabel;
class QMediaPlayer;
class QPdfDocument;
class QPdfView;
class QPushButton;
class QSlider;
class QSpinBox;
class QStackedWidget;
class QTextBrowser;
class QVideoWidget;
namespace qcopilots_search {
class PreviewPanel final : public QWidget {
    Q_OBJECT
public:
    explicit PreviewPanel(QWidget *parent = nullptr);
    ~PreviewPanel() override;
    void showHit(const SearchHit &hit);
    void loadDocumentText();
    void clear();
    void setLanguage(const QString &language);
    void setDocumentPreview(const QJsonObject &result, bool append);
    void documentPreviewFailed(const QString &message);
    SearchHit currentHit() const { return hit_; }
signals:
    void openRequested(qcopilots_search::SearchHit hit);
    void revealRequested(QString path);
    void documentRequested(QString path, bool code, int start);
private:
    QString label(const char *en, const char *zh) const;
    void updateImage();
    void updatePlaybackButton();
    void updateMediaTime(qint64 position, qint64 duration);
    void updateDocumentButton();
    void resizeEvent(QResizeEvent *event) override;
    void hideEvent(QHideEvent *event) override;
    SearchHit hit_;
    QString language_;
    QLabel *title_, *location_, *error_, *image_, *empty_, *time_;
    QStackedWidget *pages_;
    QTextBrowser *text_;
    QWidget *imagePage_, *pdfPage_, *mediaPage_;
    QPdfDocument *pdf_;
    QPdfView *pdfView_;
    QSpinBox *page_;
    QMediaPlayer *player_;
    QAudioOutput *audio_;
    QVideoWidget *video_;
    QPushButton *play_, *open_, *reveal_, *document_;
    QSlider *position_;
    QImage originalImage_;
    qint64 pendingPosition_ = -1;
    quint64 generation_ = 0;
    int documentNext_ = 0;
    int documentState_ = 0;
    bool documentHitLocated_ = false;
    bool documentAutoLocate_ = false;
    int documentBatches_ = 0;
    QThreadPool imagePool_;
    std::shared_ptr<std::atomic_bool> imageCancelled_;
};
}
