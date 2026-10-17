// Model artifact metadata adapted from DigUp (MIT), Copyright (c) ARahim3.
#pragma once

#include <QByteArray>
#include <QFile>
#include <QList>
#include <QObject>
#include <QPointer>
#include <QString>
#include <QUrl>
#include <memory>

class QLockFile;
class QNetworkAccessManager;
class QNetworkReply;

namespace qcopilots_search {

struct ModelArtifact {
    QString name;
    QUrl url;
    qint64 size = 0;
    QByteArray sha256; // Lowercase hexadecimal SHA-256, not binary digest bytes.
};

// Use on its owning Qt event-loop thread. The manifest overload supports an
// alternate, explicitly trusted mirror and hermetic HTTP integration tests.
class ModelFiles final : public QObject {
    Q_OBJECT
public:
    explicit ModelFiles(QString modelDir, QObject* parent = nullptr);
    ModelFiles(QString modelRoot, const QString& modelId, QObject* parent = nullptr);
    ModelFiles(QString modelDir, QList<ModelArtifact> artifacts, QObject* parent = nullptr);
    ~ModelFiles() override;

    static QList<ModelArtifact> manifest();
    static QList<ModelArtifact> manifest(const QString& modelId);
    bool verify(QString* error = nullptr) const;
    bool importFile(const QString& source, const QString& fileName, QString* error = nullptr);
    bool isBusy() const { return busy_; }
    QString modelDirectory() const { return modelDir_; }

public slots:
    void downloadMissing();
    void cancel();

signals:
    void progress(qint64 received, qint64 total);
    void finished(bool success, QString error);

private:
    bool validateManifest(QString* error) const;
    bool acquireLock(QString* error);
    bool verifyArtifact(const QString& path, const ModelArtifact& artifact, QString* error) const;
    bool publish(const QString& source, const ModelArtifact& artifact, QString* error);
    bool checkSpace(qint64 required, QString* error) const;
    bool acceptResponse(bool finalResponse = false);
    void startNext();
    void readAvailable();
    void replyFinished();
    void fail(const QString& error);
    void complete(bool success, const QString& error = {});

    QString modelDir_;
    QList<ModelArtifact> artifacts_;
    QNetworkAccessManager* network_ = nullptr;
    QPointer<QNetworkReply> reply_;
    std::unique_ptr<QLockFile> lock_;
    QFile partial_;
    int current_ = 0;
    qint64 completedBytes_ = 0;
    qint64 totalBytes_ = 0;
    qint64 offset_ = 0;
    quint64 operation_ = 0;
    bool responseAccepted_ = false;
    bool busy_ = false;
};

} // namespace qcopilots_search
