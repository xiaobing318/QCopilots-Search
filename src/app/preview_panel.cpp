#include "preview_panel.h"
#include "extraction/image_extractor.h"
#include <QAudioOutput>
#include <QEvent>
#include <QFileInfo>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QJsonArray>
#include <QFutureWatcher>
#include <QHideEvent>
#include <QLabel>
#include <QMediaPlayer>
#include <QPainter>
#include <QPdfDocument>
#include <QPdfPageNavigator>
#include <QPdfView>
#include <QPushButton>
#include <QScrollBar>
#include <QSignalBlocker>
#include <QSlider>
#include <QSpinBox>
#include <QStackedWidget>
#include <QTextBrowser>
#include <QTextCursor>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>
#include <QVideoWidget>
#include <QtConcurrent>
#include <climits>

namespace qcopilots_search {
PreviewPanel::PreviewPanel(QWidget *parent) : QWidget(parent) {
    imagePool_.setMaxThreadCount(1);
    imagePool_.setExpiryTimeout(1000);
    setObjectName(QStringLiteral("previewPanel"));
    auto *layout = new QVBoxLayout(this);
    layout->setContentsMargins(20, 16, 20, 16);
    title_ = new QLabel(this);
    title_->setObjectName("previewTitle");
    title_->setWordWrap(true);
    title_->setStyleSheet("font-size:17px;font-weight:600");
    location_ = new QLabel(this);
    location_->setObjectName("previewLocation");
    location_->setWordWrap(true);
    location_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    error_ = new QLabel(this);
    error_->setObjectName("previewError");
    error_->setWordWrap(true);
    error_->setStyleSheet("color:#ba3c32");
    layout->addWidget(title_);
    layout->addWidget(location_);
    layout->addWidget(error_);
    pages_ = new QStackedWidget(this);
    empty_ = new QLabel(this);
    empty_->setAlignment(Qt::AlignCenter);
    empty_->setWordWrap(true);
    pages_->addWidget(empty_);
    text_ = new QTextBrowser(this);
    text_->setObjectName("textPreview");
    text_->setOpenExternalLinks(false);
    text_->setOpenLinks(false);
    pages_->addWidget(text_);
    imagePage_ = new QWidget(this);
    auto *imageLayout = new QVBoxLayout(imagePage_);
    auto *imageControls = new QHBoxLayout;
    imageZoomOut_ = new QPushButton(imagePage_);
    imageZoomOut_->setObjectName("imageZoomOutButton");
    imageZoomIn_ = new QPushButton(imagePage_);
    imageZoomIn_->setObjectName("imageZoomInButton");
    imageActualSize_ = new QPushButton(QStringLiteral("100%"), imagePage_);
    imageActualSize_->setObjectName("imageActualSizeButton");
    imageFitButton_ = new QPushButton(imagePage_);
    imageFitButton_->setObjectName("imageFitButton");
    imageFitButton_->setCheckable(true);
    imageZoom_ = new QLabel(imagePage_);
    imageZoom_->setObjectName("imageZoomLabel");
    imageControls->addWidget(imageZoomOut_);
    imageControls->addWidget(imageZoomIn_);
    imageControls->addWidget(imageActualSize_);
    imageControls->addWidget(imageFitButton_);
    imageControls->addWidget(imageZoom_);
    imageControls->addStretch();
    imageLayout->addLayout(imageControls);
    imageState_ = new QLabel(imagePage_);
    imageState_->setObjectName("imagePreviewState");
    imageState_->setWordWrap(true);
    imageLayout->addWidget(imageState_);
    image_ = new QGraphicsView(imagePage_);
    image_->setObjectName("imagePreview");
    image_->setAlignment(Qt::AlignCenter);
    image_->setMinimumSize(1, 1);
    image_->setRenderHint(QPainter::SmoothPixmapTransform);
    image_->setDragMode(QGraphicsView::ScrollHandDrag);
    image_->setTransformationAnchor(QGraphicsView::NoAnchor);
    image_->setResizeAnchor(QGraphicsView::AnchorViewCenter);
    image_->setScene(new QGraphicsScene(image_));
    imageItem_ = image_->scene()->addPixmap({});
    imageItem_->setShapeMode(QGraphicsPixmapItem::BoundingRectShape);
    imageItem_->setTransformationMode(Qt::SmoothTransformation);
    image_->viewport()->installEventFilter(this);
    imageLayout->addWidget(image_, 1);
    connect(imageZoomOut_, &QPushButton::clicked, this, [this] { setImageZoom(image_->transform().m11() / 1.25); });
    connect(imageZoomIn_, &QPushButton::clicked, this, [this] { setImageZoom(image_->transform().m11() * 1.25); });
    connect(imageActualSize_, &QPushButton::clicked, this, [this] { setImageZoom(1.0); });
    connect(imageFitButton_, &QPushButton::clicked, this, [this] { imageFit_ = true; updateImage(); });
    pages_->addWidget(imagePage_);
    pdfPage_ = new QWidget(this);
    auto *pdfLayout = new QVBoxLayout(pdfPage_);
    pdf_ = new QPdfDocument(this);
    pdfView_ = new QPdfView(pdfPage_);
    pdfView_->setObjectName("pdfPreview");
    pdfView_->setDocument(pdf_);
    pdfView_->setZoomMode(QPdfView::ZoomMode::FitToWidth);
    pdfView_->setPageMode(QPdfView::PageMode::SinglePage);
    page_ = new QSpinBox(pdfPage_);
    page_->setObjectName("pdfPageNumber");
    pdfLayout->addWidget(page_);
    pdfLayout->addWidget(pdfView_, 1);
    connect(page_, &QSpinBox::valueChanged, this, [this](int page) { pdfView_->pageNavigator()->jump(page - 1, QPointF()); });
    pages_->addWidget(pdfPage_);
    mediaPage_ = new QWidget(this);
    auto *mediaLayout = new QVBoxLayout(mediaPage_);
    player_ = new QMediaPlayer(this);
    player_->setObjectName("previewMediaPlayer");
    audio_ = new QAudioOutput(this);
    audio_->setVolume(0.7f);
    video_ = new QVideoWidget(mediaPage_);
    video_->setMinimumSize(160, 100);
    player_->setAudioOutput(audio_);
    player_->setVideoOutput(video_);
    mediaLayout->addWidget(video_, 1);
    auto *controls = new QHBoxLayout;
    play_ = new QPushButton(mediaPage_);
    play_->setObjectName("mediaPlayButton");
    position_ = new QSlider(Qt::Horizontal, mediaPage_);
    position_->setObjectName("mediaPosition");
    time_ = new QLabel(mediaPage_);
    time_->setObjectName("mediaTimeLabel");
    controls->addWidget(play_);
    controls->addWidget(position_, 1);
    controls->addWidget(time_);
    mediaLayout->addLayout(controls);
    pages_->addWidget(mediaPage_);
    connect(play_, &QPushButton::clicked, this, [this] {
        if (player_->playbackState() == QMediaPlayer::PlayingState) player_->pause(); else player_->play();
    });
    connect(player_, &QMediaPlayer::playbackStateChanged, this, &PreviewPanel::updatePlaybackButton);
    connect(player_, &QMediaPlayer::durationChanged, this, [this](qint64 duration) { updateMediaTime(player_->position(), duration); });
    connect(player_, &QMediaPlayer::positionChanged, this, [this](qint64 position) { updateMediaTime(position, player_->duration()); });
    connect(position_, &QSlider::sliderMoved, player_, &QMediaPlayer::setPosition);
    connect(player_, &QMediaPlayer::mediaStatusChanged, this, [this](QMediaPlayer::MediaStatus status) {
        if ((status == QMediaPlayer::LoadedMedia || status == QMediaPlayer::BufferedMedia) && pendingPosition_ >= 0) { player_->setPosition(pendingPosition_); pendingPosition_ = -1; }
    });
    connect(player_, &QMediaPlayer::errorOccurred, this, [this](QMediaPlayer::Error, const QString &message) { if (pages_->currentWidget() == mediaPage_) { error_->setText(message); error_->show(); } });
    layout->addWidget(pages_, 1);
    auto *actions = new QHBoxLayout;
    document_ = new QPushButton(this);
    document_->setObjectName("loadDocumentPreviewButton");
    actions->addWidget(document_);
    open_ = new QPushButton(this);
    open_->setObjectName("openResultButton");
    reveal_ = new QPushButton(this);
    reveal_->setObjectName("revealResultButton");
    actions->addWidget(open_);
    actions->addWidget(reveal_);
    actions->addStretch();
    layout->addLayout(actions);
    connect(open_, &QPushButton::clicked, this, [this] { emit openRequested(hit_); });
    connect(reveal_, &QPushButton::clicked, this, [this] { emit revealRequested(hit_.path); });
    connect(document_, &QPushButton::clicked, this, &PreviewPanel::loadDocumentText);
    setLanguage("en");
    clear();
}
void PreviewPanel::loadDocumentText() {
    if (document_->isHidden() || !document_->isEnabled() || hit_.path.isEmpty()) return;
    documentAutoLocate_ = !documentHitLocated_ && (!hit_.text.isEmpty() || hit_.lineStart > 0 || hit_.cell > 0);
    documentBatches_ = 0;
    documentState_ = 1;
    document_->setEnabled(false);
    updateDocumentButton();
    emit documentRequested(hit_.path, hit_.kind == "code" || hit_.lineStart > 0, documentNext_);
}
PreviewPanel::~PreviewPanel() {
    if (imageCancelled_) imageCancelled_->store(true);
    imagePool_.clear();
    imagePool_.waitForDone();
    player_->stop();
    player_->setSource({});
    pdf_->close();
}
QString PreviewPanel::label(const char *en, const char *zh) const { return QString::fromUtf8(language_.startsWith("zh") ? zh : en); }
void PreviewPanel::setLanguage(const QString &language) {
    language_ = language;
    empty_->setText(label("Select a result to preview its matching passage, page or moment.", "选择搜索结果，预览命中的文字、页面或时刻。"));
    open_->setText(label("Open file", "打开文件"));
    reveal_->setText(label("Show in folder", "在文件夹中显示"));
    imageZoomOut_->setText(label("Zoom out", "缩小"));
    imageZoomOut_->setToolTip(imageZoomOut_->text());
    imageZoomOut_->setAccessibleName(imageZoomOut_->toolTip());
    imageZoomIn_->setText(label("Zoom in", "放大"));
    imageZoomIn_->setToolTip(imageZoomIn_->text());
    imageZoomIn_->setAccessibleName(imageZoomIn_->toolTip());
    imageActualSize_->setToolTip(label("Show current preview pixels at 1:1 (follows system display scaling)", "按当前预览像素 1:1 显示（跟随系统显示缩放）"));
    imageActualSize_->setAccessibleName(imageActualSize_->toolTip());
    imageFitButton_->setText(label("Fit", "适配"));
    imageFitButton_->setToolTip(label("Fit image to preview", "适配预览区域"));
    updateImageControls();
    updatePlaybackButton();
    const QString pagePrefix = label("Page ", "页码 ");
    if (page_->prefix() != pagePrefix) page_->setPrefix(pagePrefix);
    updateDocumentButton();
}
void PreviewPanel::updatePlaybackButton() {
    play_->setText(player_->playbackState() == QMediaPlayer::PlayingState ? label("Pause", "暂停") : label("Play", "播放"));
}
void PreviewPanel::updateMediaTime(qint64 position, qint64 duration) {
    position = qMax<qint64>(0, position);
    duration = qMax<qint64>(0, duration);
    position_->setRange(0, int(qMin<qint64>(duration, INT_MAX)));
    if (!position_->isSliderDown()) position_->setValue(int(qMin<qint64>(position, INT_MAX)));
    time_->setText(QStringLiteral("%1:%2 / %3:%4")
        .arg(position / 60000).arg(position / 1000 % 60, 2, 10, QLatin1Char('0'))
        .arg(duration / 60000).arg(duration / 1000 % 60, 2, 10, QLatin1Char('0')));
}
void PreviewPanel::clear() {
    if (imageCancelled_) imageCancelled_->store(true);
    imagePool_.clear();
    ++generation_;
    documentNext_ = 0;
    documentSourceVersion_.clear();
    documentState_ = 0;
    documentHitLocated_ = false;
    documentAutoLocate_ = false;
    documentBatches_ = 0;
    document_->hide();
    document_->setEnabled(true);
    updateDocumentButton();
    player_->stop();
    player_->setSource({});
    updateMediaTime(0, 0);
    pendingPosition_ = -1;
    // QPdfDocument's filename overload retains its QFile until destruction.
    // Detach the view before releasing it so Windows can rename/remove the old
    // source immediately after selecting another result.
    pdfView_->setDocument(nullptr);
    delete pdf_;
    pdf_ = new QPdfDocument(this);
    pdfView_->setDocument(pdf_);
    imageItem_->setPixmap({});
    image_->scene()->setSceneRect(QRectF(0, 0, 1, 1));
    image_->resetTransform();
    imageFit_ = true;
    image_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    image_->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    image_->horizontalScrollBar()->setValue(0);
    image_->verticalScrollBar()->setValue(0);
    imageState_->clear();
    imageState_->hide();
    updateImageControls();
    hit_ = {};
    title_->clear();
    location_->clear();
    error_->clear();
    error_->hide();
    text_->clear();
    pages_->setCurrentWidget(empty_);
    open_->setEnabled(false);
    reveal_->setEnabled(false);
}
void PreviewPanel::refreshHitMetadata(const SearchHit &hit) {
    hit_ = hit;
    title_->setText(hit.title.isEmpty() ? QFileInfo(hit.path).fileName() : hit.title);
    QString location = hit.path;
    if (hit.page > 0) location += label("\nPage ", "\n页码 ") + QString::number(hit.page);
    if (hit.lineStart > 0) location += label("\nLine ", "\n行号 ") + QString::number(hit.lineStart);
    if (hit.cell > 0) location += label(" · Cell ", " · 单元格 ") + QString::number(hit.cell);
    if (hit.startMs > 0) location += QStringLiteral("\n%1.%2 s").arg(hit.startMs / 1000).arg(hit.startMs % 1000, 3, 10, QLatin1Char('0'));
    location_->setText(location);
    const bool exists = QFileInfo::exists(hit.path);
    open_->setEnabled(exists);
    reveal_->setEnabled(exists);
}
void PreviewPanel::showHit(const SearchHit &hit) {
    clear();
    refreshHitMetadata(hit);
    text_->setPlainText(hit.text);
    pages_->setCurrentWidget(text_);
    if (!QFileInfo::exists(hit.path)) { error_->setText(label("The source file moved or was removed. Reconcile the index to refresh this result.", "源文件已移动或删除。重新扫描后将更新此结果。")); error_->show(); return; }
    const QString suffix = QFileInfo(hit.path).suffix().toLower();
    document_->setVisible(hit.kind != "image" && hit.kind != "pdf" && hit.kind != "audio" && hit.kind != "video" && suffix != "pdf");
    if (suffix == "pdf") {
        if (pdf_->load(hit.path) != QPdfDocument::Error::None) { error_->setText(label("Unable to render this PDF. The indexed text is shown below.", "无法渲染此 PDF，以下显示已索引的文字。")); error_->show(); return; }
        page_->setRange(1, qMax(1, pdf_->pageCount()));
        page_->setValue(qBound(1, hit.page, qMax(1, pdf_->pageCount())));
        pdfView_->pageNavigator()->jump(page_->value() - 1, QPointF());
        pages_->setCurrentWidget(pdfPage_);
    } else if (hit.kind == "image" || QStringList{"png","jpg","jpeg","webp","bmp","gif","tiff","tif","heic","heif","avif"}.contains(suffix)) {
        pages_->setCurrentWidget(imagePage_);
        imageState_->setText(label("Loading image…", "正在加载图片…"));
        imageState_->show();
        const quint64 generation = generation_;
        imageCancelled_ = std::make_shared<std::atomic_bool>(false);
        const auto cancelled = imageCancelled_;
        auto *watcher = new QFutureWatcher<QPair<QImage, QString>>(this);
        connect(watcher, &QFutureWatcherBase::finished, this, [this, watcher, generation] {
            if (watcher->isCanceled()) { watcher->deleteLater(); return; }
            const auto result = watcher->result();
            watcher->deleteLater();
            if (generation != generation_) return;
            if (result.first.isNull()) { error_->setText(result.second); error_->show(); pages_->setCurrentWidget(text_); return; }
            auto pixels = QPixmap::fromImage(result.first);
            pixels.setDevicePixelRatio(1.0);
            imageItem_->setPixmap(pixels);
            image_->scene()->setSceneRect(imageItem_->boundingRect());
            imageState_->hide();
            updateImage();
        });
        const QString path = hit.path;
        watcher->setFuture(QtConcurrent::run(&imagePool_, [path, cancelled] {
            if (cancelled->load()) return qMakePair(QImage(), QStringLiteral("Preview cancelled"));
            QString error;
            QImage image = loadPreviewImage(path, 4096, &error, [cancelled] { return cancelled->load(); });
            return qMakePair(image, error);
        }));
    } else if (hit.kind == "audio" || hit.kind == "video") {
        video_->setVisible(hit.kind == "video");
        pendingPosition_ = qMax<qint64>(0, hit.startMs);
        player_->setSource(QUrl::fromLocalFile(hit.path));
        pages_->setCurrentWidget(mediaPage_);
    }
}
void PreviewPanel::setDocumentPreview(const QJsonObject &result, bool append) {
    const QString sourceVersion = result.value("sourceVersion").toString();
    if (sourceVersion.isEmpty() || (append && sourceVersion != documentSourceVersion_)) {
        // Extraction cursors refer to one source version. Do not combine an
        // earlier batch with text read after the source changed between requests.
        ++generation_;
        documentNext_ = 0;
        documentSourceVersion_.clear();
        documentHitLocated_ = false;
        documentAutoLocate_ = false;
        documentBatches_ = 0;
        documentPreviewFailed(sourceVersion.isEmpty()
            ? label("Unable to verify the document version. Read the document again from the beginning.", "无法确认正文版本，请从头重新读取文档。")
            : label("The source file changed. Read the document again from the beginning.", "源文件已变化，请从头重新读取文档。"));
        return;
    }
    documentSourceVersion_ = sourceVersion;
    error_->clear();
    error_->hide();
    const auto segments = result.value("segments").toArray();
    QString body;
    int matchingOffset = -1;
    for (const auto &value : segments) {
        const auto segment = value.toObject();
        const QString text = segment.value("text").toString();
        if (!body.isEmpty()) body += QStringLiteral("\n\n");
        const int segmentOffset = int(body.size());
        const int cell = segment.value("cell").toInt();
        const int lineStart = segment.value("lineStart").toInt();
        const int lineEnd = segment.value("lineEnd").toInt(lineStart);
        const bool cellMatches = hit_.cell <= 0 || cell == hit_.cell;
        const bool lineMatches = hit_.lineStart <= 0 || (lineStart > 0 && lineStart <= hit_.lineStart && lineEnd >= hit_.lineStart);
        if (!documentHitLocated_ && matchingOffset < 0 && cellMatches && lineMatches) {
            if (hit_.lineStart > 0 || hit_.cell > 0) {
                int offset = 0;
                for (int line = lineStart; lineStart > 0 && line < hit_.lineStart; ++line) {
                    const int newline = int(text.indexOf(QLatin1Char('\n'), offset));
                    if (newline < 0) break;
                    offset = newline + 1;
                }
                matchingOffset = segmentOffset + offset;
            } else if (!hit_.text.isEmpty()) {
                const int offset = int(text.indexOf(hit_.text.left(80)));
                if (offset >= 0) matchingOffset = segmentOffset + offset;
            }
        }
        body += text;
    }
    // Legacy responses without structured segments still provide a body, but
    // must not guess a code or notebook location from repeated text alone.
    if (segments.isEmpty()) {
        body = result.value("text").toString();
        if (!documentHitLocated_ && hit_.lineStart <= 0 && hit_.cell <= 0 && !hit_.text.isEmpty()) matchingOffset = int(body.indexOf(hit_.text.left(80)));
    }
    const int previousPosition = text_->textCursor().position();
    const QString previousText = append ? text_->toPlainText() : QString();
    const int appendedOffset = append ? int(previousText.size()) + 1 : 0;
    if (previousText.size() + body.size() > 16 * 1024 * 1024) { documentPreviewFailed(label("Preview text limit reached. Open the original file to continue.", "正文预览已达到容量上限，请打开源文件继续阅读。")); return; }
    if (!body.isEmpty() || !append) {
        if (append) text_->setPlainText(previousText + QLatin1Char('\n') + body); else text_->setPlainText(body);
        pages_->setCurrentWidget(text_);
        if (matchingOffset >= 0) {
            QTextCursor cursor(text_->document());
            cursor.setPosition(appendedOffset + matchingOffset);
            text_->setTextCursor(cursor);
            text_->ensureCursorVisible();
            documentHitLocated_ = true;
        } else if (append && documentHitLocated_) {
            QTextCursor cursor(text_->document());
            cursor.setPosition(qMin(previousPosition, int(text_->toPlainText().size())));
            text_->setTextCursor(cursor);
            text_->ensureCursorVisible();
        }
    }
    const int previousNext = documentNext_;
    documentNext_ = result.value("next").toInt();
    const bool partial = result.value("partial").toBool();
    ++documentBatches_;
    if (partial && documentNext_ <= previousNext) { documentPreviewFailed(label("The preview continuation cursor did not advance.", "正文续读游标未前进。")); return; }
    documentState_ = partial ? 2 : 3;
    document_->setEnabled(partial);
    updateDocumentButton();
    const QString error = result.value("error").toString();
    if (!error.isEmpty()) { documentPreviewFailed(error); return; }
    const bool hasLocation = hit_.lineStart > 0 || hit_.cell > 0 || !hit_.text.isEmpty();
    if (documentAutoLocate_ && !documentHitLocated_ && hasLocation && partial && documentBatches_ < 50) {
        documentState_ = 1;
        document_->setEnabled(false);
        updateDocumentButton();
        const quint64 generation = generation_;
        QTimer::singleShot(0, this, [this, generation] {
            if (generation != generation_ || documentState_ != 1) return;
            emit documentRequested(hit_.path, hit_.kind == "code" || hit_.lineStart > 0, documentNext_);
        });
    } else if (documentAutoLocate_ && !documentHitLocated_ && hasLocation) {
        error_->setText(partial ? label("The matching passage is beyond the automatic preview limit. Continue reading or open the source file.", "命中位置超出自动预览范围，可继续读取或打开源文件。") : label("The indexed location is no longer present in the current source. Rescan the file to refresh the result.", "当前源文件中已找不到索引位置，请重新扫描以更新结果。"));
        error_->show();
    }
}
void PreviewPanel::documentPreviewFailed(const QString &message) {
    error_->setText(message);
    error_->show();
    documentState_ = 4;
    document_->setEnabled(true);
    updateDocumentButton();
}
void PreviewPanel::updateDocumentButton() {
    switch (documentState_) {
    case 1: document_->setText(label("Loading text…", "正在加载正文…")); break;
    case 2: document_->setText(label("Continue reading", "继续读取正文")); break;
    case 3: document_->setText(label("Document loaded", "正文已读取")); break;
    case 4: document_->setText(label("Retry reading", "重试读取正文")); break;
    default: document_->setText(label("Read document text", "读取文档正文")); break;
    }
}
void PreviewPanel::updateImage() {
    if (!imageItem_->pixmap().isNull() && imageFit_) {
        // Fit uses the viewport, never the scaled image's size hint. Keeping
        // scrollbars off in this mode avoids a resize/scrollbar feedback loop.
        image_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        image_->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        const QSize available = image_->viewport()->size() - QSize(2, 2);
        const QSize pixels = imageItem_->pixmap().size();
        const qreal scale = qMin<qreal>(1.0, qMin(qMax(1, available.width()) / qreal(pixels.width()),
                                                qMax(1, available.height()) / qreal(pixels.height())));
        image_->setTransform(QTransform::fromScale(scale, scale));
        image_->centerOn(imageItem_);
    }
    updateImageControls();
}
void PreviewPanel::setImageZoom(qreal scale) {
    if (imageItem_->pixmap().isNull()) return;
    const QPointF center = image_->mapToScene(image_->viewport()->rect().center());
    imageFit_ = false;
    image_->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    image_->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    // Transform the bounded decoded pixmap instead of allocating a whole
    // enlarged raster. Only the visible part is painted at the chosen scale.
    scale = qBound<qreal>(0.05, scale, 4.0);
    image_->setTransform(QTransform::fromScale(scale, scale));
    image_->centerOn(center);
    updateImageControls();
}
void PreviewPanel::updateImageControls() {
    const bool loaded = !imageItem_->pixmap().isNull();
    const qreal scale = image_->transform().m11();
    imageZoomOut_->setEnabled(loaded && scale > 0.050001);
    imageZoomIn_->setEnabled(loaded && scale < 3.999999);
    imageActualSize_->setEnabled(loaded);
    imageFitButton_->setEnabled(loaded);
    imageFitButton_->setChecked(imageFit_);
    imageZoom_->setText(!loaded ? QString() : (imageFit_ ? label("Fit · %1%", "适配 · %1%") : QStringLiteral("%1%")).arg(qRound(scale * 100)));
}
bool PreviewPanel::eventFilter(QObject *watched, QEvent *event) {
    if (watched == image_->viewport() && event->type() == QEvent::Resize && imageFit_) {
        // The viewport's final geometry includes toolbar/font/layout changes.
        QTimer::singleShot(0, this, [this] { updateImage(); });
    }
    return QWidget::eventFilter(watched, event);
}
void PreviewPanel::hideEvent(QHideEvent *event) {
    if (player_->playbackState() == QMediaPlayer::PlayingState) player_->pause();
    QWidget::hideEvent(event);
}
}
