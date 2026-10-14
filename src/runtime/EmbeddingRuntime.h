#pragma once

#include <QVector>
#include <QString>
#include <QJsonObject>
#include <memory>
#include <functional>

namespace qcopilots_search {

enum class PromptKind { Query, CodeQuery, Document, Raw };

// One runtime instance is used by one worker thread. Media files are decoded locally.
// An empty vector always denotes an error, available via error or lastError().
class EmbeddingRuntime final {
public:
    static constexpr int Dimension = 768;
    static constexpr int VectorVersion = 1;
    EmbeddingRuntime();
    ~EmbeddingRuntime();
    EmbeddingRuntime(const EmbeddingRuntime&) = delete;
    EmbeddingRuntime& operator=(const EmbeddingRuntime&) = delete;

    bool load(const QString& modelDir, bool textOnly, QString* error = nullptr);
    void close();
    bool isLoaded() const;
    bool isTextOnly() const;
    QString lastError() const;
    // GPU mode is strict: initialization must succeed on the packaged GPU
    // backend. Auto may retry on CPU and records the reason in diagnostics().
    bool setBackendMode(const QString& mode, QString* error = nullptr);
    QJsonObject diagnostics() const;
    // Set these on the owner thread before load. The callback may read a shared atomic flag.
    void setThreadCount(int threads);
    void setCancellationCallback(std::function<bool()> cancelled);
    QVector<float> embedText(const QString& text, PromptKind kind = PromptKind::Query,
                             const QString& title = {}, QString* error = nullptr);
    QVector<float> embedImage(const QString& path, int budget = 140, QString* error = nullptr);
    QVector<float> embedAudio(const QString& path, QString* error = nullptr);
    static QString prompt(const QString& text, PromptKind kind, const QString& title = {});
    static QString fingerprint();
    static QString textModelName();
    static QString mediaModelName();
    static bool verifyModels(const QString& modelDir, bool textOnly, QString* error = nullptr);

private:
    class Private;
    std::unique_ptr<Private> d;
};

}
