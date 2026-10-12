// Adapted from DigUp LlamaRuntime, Copyright (c) 2026 Abdur Rahim, MIT.
#include "EmbeddingRuntime.h"
#include "BackendPaths.h"
#include "ImageSizing.h"
#include "core/pipeline_manifest.h"

#include <QCryptographicHash>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QThread>
#include <llama.h>
#include <mtmd.h>
#include <mtmd-helper.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <stdexcept>

namespace qcopilots_search {
namespace {
constexpr auto TextName = "embeddinggemma-2-Q8_0.gguf";
constexpr auto MediaName = "mmproj-embeddinggemma-2-Q8_0.gguf";
constexpr auto TextHash = embeddingTextSha256;
constexpr auto MediaHash = embeddingMediaSha256;

void logCallback(ggml_log_level level, const char* text, void*) {
    static std::mutex mutex;
    static ggml_log_level previous = GGML_LOG_LEVEL_INFO;
    std::lock_guard lock(mutex);
    if (level != GGML_LOG_LEVEL_CONT) previous = level;
    if (previous >= GGML_LOG_LEVEL_WARN && text) std::fputs(text, stderr);
}

int threadCount() {
    bool ok = false;
    const int requested = qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_THREADS", &ok);
    const int available = std::max(1, QThread::idealThreadCount());
    return std::clamp(ok ? requested : available / 2, 1, available);
}

void initializeBackend() {
#ifdef QCOPILOTS_SEARCH_CPU_DISPATCH
    // Load only an explicitly known CPU backend from the installation. Upstream's
    // generic loader also consults cwd and GGML_BACKEND_PATH, which we don't use.
    const bool baseline = qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_CPU_BASELINE") == 1;
    QStringList variants = baseline ? QStringList{QStringLiteral("x64")} :
        QStringList{QStringLiteral("sapphirerapids"), QStringLiteral("zen4"), QStringLiteral("cooperlake"),
                    QStringLiteral("icelake"), QStringLiteral("cascadelake"), QStringLiteral("cannonlake"),
                    QStringLiteral("skylakex"), QStringLiteral("alderlake"), QStringLiteral("haswell"),
                    QStringLiteral("piledriver"), QStringLiteral("ivybridge"), QStringLiteral("sandybridge"),
                    QStringLiteral("sse42"), QStringLiteral("x64")};
#ifdef Q_OS_MACOS
    constexpr auto platform = detail::BackendPlatform::MacOS;
#elif defined(Q_OS_WIN)
    constexpr auto platform = detail::BackendPlatform::Windows;
#else
    constexpr auto platform = detail::BackendPlatform::Unix;
#endif
    const auto roots = detail::cpuBackendSearchRoots(QCoreApplication::applicationDirPath(), platform);
    bool loaded = false;
    for (const auto& variant : variants) {
#ifdef Q_OS_WIN
        const QString name = QStringLiteral("ggml-cpu-%1.dll").arg(variant);
#elif defined(Q_OS_MACOS)
        const QString name = QStringLiteral("libggml-cpu-%1.so").arg(variant);
#else
        const QString name = QStringLiteral("libggml-cpu-%1.so").arg(variant);
#endif
        for (const auto& root : roots) {
            const auto path = QDir(root).absoluteFilePath(name);
            if (QFileInfo::exists(path) && ggml_backend_load(path.toUtf8().constData())) {
                loaded = true;
                break;
            }
        }
        if (loaded) break;
    }
    if (!loaded) throw std::runtime_error("No compatible packaged CPU backend was found");
#endif
    llama_backend_init();
}

bool verify(const QString& path, qint64 bytes, const char* expected, QString* error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        if (error) *error = QStringLiteral("Cannot open model %1: %2").arg(path, file.errorString());
        return false;
    }
    if (file.size() != bytes) {
        if (error) *error = QStringLiteral("Model size mismatch: %1 (expected %2, got %3)")
                               .arg(path).arg(bytes).arg(file.size());
        return false;
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file) || hash.result().toHex() != expected) {
        if (error) *error = QStringLiteral("Model SHA-256 mismatch: %1").arg(path);
        return false;
    }
    return true;
}

using Bitmap = std::unique_ptr<mtmd_bitmap, decltype(&mtmd_bitmap_free)>;
using Chunks = std::unique_ptr<mtmd_input_chunks, decltype(&mtmd_input_chunks_free)>;
}

class EmbeddingRuntime::Private {
public:
    llama_model* model = nullptr;
    llama_context* context = nullptr;
    mtmd_context* media = nullptr;
    llama_batch_ext* batch = nullptr;
    const llama_vocab* vocab = nullptr;
    int maxTokens = 2048;
    int rowSize = 0;
    int outputSize = 0;
    int position = 0;
    bool textOnly = true;
    QString error;
    int threads = threadCount();
    std::function<bool()> cancelled;
    static bool abortCallback(void* data) {
        auto* self = static_cast<Private*>(data);
        return self->cancelled && self->cancelled();
    }
    static bool loadProgress(float, void* data) { return !abortCallback(data); }
    void checkCancellation() {
        if (abortCallback(this)) throw std::runtime_error("Embedding cancelled");
    }

    void reset() {
        if (batch) llama_batch_ext_clear(batch);
        position = 0;
    }
    void close() {
        if (batch) llama_batch_ext_free(batch);
        if (media) mtmd_free(media);
        if (context) llama_free(context);
        if (model) llama_model_free(model);
        batch = nullptr; media = nullptr; context = nullptr; model = nullptr; vocab = nullptr;
    }
    void place(int32_t index) {
        if (index < 0 || position >= maxTokens) throw std::runtime_error("Embedding batch is full");
        llama_pos pos[] = {position, 0, 0, 0};
        if (!llama_batch_ext_set_pos(batch, index, pos)
            || !llama_batch_ext_set_output_embd(batch, index, true))
            throw std::runtime_error("Cannot place embedding input token");
        ++position;
    }
    void token(llama_token id) { place(llama_batch_ext_add_token(batch, 0, id)); }
    void rows(const float* data, size_t count) {
        if (!data || count > size_t(maxTokens - position))
            throw std::runtime_error("Media embedding exceeds context token capacity");
        for (size_t row = 0; row < count; ++row) {
            llama_embd embd{data + row * size_t(rowSize), 1, size_t(rowSize)};
            place(llama_batch_ext_add_embd(batch, 0, embd));
        }
    }
    QVector<float> run() {
        checkCancellation();
        const int status = llama_process(context, LLAMA_PROCESS_TYPE_ENCODE, batch);
        checkCancellation();
        if (status != 0) throw std::runtime_error("llama_process failed: " + std::to_string(status));
        const float* pooled = llama_get_embeddings_seq(context, 0);
        if (!pooled) throw std::runtime_error("Model returned no pooled embedding");
        double square = 0;
        for (int i = 0; i < outputSize; ++i) {
            if (!std::isfinite(pooled[i])) throw std::runtime_error("Model returned a non-finite embedding");
            square += double(pooled[i]) * double(pooled[i]);
        }
        if (!(square > 0)) throw std::runtime_error("Model returned a zero embedding");
        const float scale = float(1.0 / std::sqrt(square));
        QVector<float> result(outputSize);
        for (int i = 0; i < outputSize; ++i) result[i] = pooled[i] * scale;
        return result;
    }
    QVector<float> text(const QString& input) {
        const auto utf8 = input.toUtf8();
        if (utf8.size() > std::numeric_limits<int32_t>::max() - 8)
            throw std::runtime_error("Text input is too large");
        QVector<llama_token> tokens(utf8.size() + 8);
        int32_t count = llama_tokenize(vocab, utf8.constData(), int32_t(utf8.size()),
                                      tokens.data(), int32_t(tokens.size()), true, true);
        if (count < 0) {
            if (count == std::numeric_limits<int32_t>::min()) throw std::runtime_error("Tokenizer failed");
            tokens.resize(-count);
            count = llama_tokenize(vocab, utf8.constData(), int32_t(utf8.size()),
                                   tokens.data(), int32_t(tokens.size()), true, true);
        }
        if (count <= 0) throw std::runtime_error("Tokenizer returned no input tokens");
        tokens.resize(std::min(count, maxTokens));
        if (count > maxTokens) tokens[maxTokens - 1] = llama_vocab_eos(vocab);
        for (auto id : tokens) token(id);
        return run();
    }

    Bitmap readBitmap(const QString& path, bool image, int budget) {
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly)) throw std::runtime_error("Cannot read media file: " + path.toStdString());
        // Extraction produces bounded frame/window files. Never load an unbounded source video here.
        if (file.size() > 256 * 1024 * 1024 || file.size() == 0)
            throw std::runtime_error("Media input must be a nonempty image or audio window below 256 MiB");
        const auto encoded = file.readAll();
        if (encoded.size() != file.size()) throw std::runtime_error("Incomplete media read");
        auto wrapper = mtmd_helper_bitmap_init_from_buf(media,
            reinterpret_cast<const unsigned char*>(encoded.constData()), size_t(encoded.size()),
            false, mtmd_helper_init_opt_default());
        Bitmap bitmap(wrapper.bitmap, &mtmd_bitmap_free);
        if (wrapper.video_ctx) {
            mtmd_helper_video_free(wrapper.video_ctx);
            throw std::runtime_error("Video must be extracted into frames and audio windows before encoding");
        }
        if (!bitmap) throw std::runtime_error("Cannot decode media input");
        if (mtmd_bitmap_is_audio(bitmap.get()) == image)
            throw std::runtime_error(image ? "Input is not an image" : "Input is not audio");
        if (image) {
            const int width = int(mtmd_bitmap_get_nx(bitmap.get()));
            const int height = int(mtmd_bitmap_get_ny(bitmap.get()));
            const auto target = ImageSizing::targetSize(height, width, budget);
            if (!target) throw std::runtime_error("Image dimensions cannot fit the token budget");
            if (target->width() == width && target->height() == height) return bitmap;
            const auto pixels = ImageSizing::resizeBicubic(mtmd_bitmap_get_data(bitmap.get()), width, height,
                                                          target->width(), target->height());
            if (pixels.isEmpty()) throw std::runtime_error("Cannot resize image");
            Bitmap resized(mtmd_bitmap_init(uint32_t(target->width()), uint32_t(target->height()),
                reinterpret_cast<const unsigned char*>(pixels.constData())), &mtmd_bitmap_free);
            if (!resized) throw std::runtime_error("Cannot allocate resized image bitmap");
            return resized;
        }
        const auto samples = mtmd_bitmap_get_n_bytes(bitmap.get()) / sizeof(float);
        const size_t limit = size_t(30) * size_t(mtmd_get_audio_sample_rate(media));
        if (samples <= limit) return bitmap;
        Bitmap prefix(mtmd_bitmap_init_from_audio(limit,
            reinterpret_cast<const float*>(mtmd_bitmap_get_data(bitmap.get()))), &mtmd_bitmap_free);
        if (!prefix) throw std::runtime_error("Cannot truncate audio to 30 seconds");
        return prefix;
    }

    QVector<float> mediaEmbedding(const QString& path, bool image, int budget) {
        if (!media) throw std::runtime_error("This runtime was loaded in text-only mode");
        if (image && budget != 70 && budget != 140 && budget != 280 && budget != 560 && budget != 1120)
            throw std::runtime_error("Image budget must be 70, 140, 280, 560 or 1120");
        auto bitmap = readBitmap(path, image, budget);
        Chunks chunks(mtmd_input_chunks_init(), &mtmd_input_chunks_free);
        if (!chunks) throw std::runtime_error("Cannot allocate media chunks");
        const char* marker = mtmd_default_marker();
        mtmd_input_text text{marker, std::strlen(marker), true, true};
        const mtmd_bitmap* bitmaps[] = {bitmap.get()};
        const auto status = mtmd_tokenize(media, chunks.get(), &text, bitmaps, 1);
        if (status != 0) throw std::runtime_error("Cannot tokenize media: " + std::to_string(status));
        for (size_t index = 0; index < mtmd_input_chunks_size(chunks.get()); ++index) {
            checkCancellation();
            const auto* chunk = mtmd_input_chunks_get(chunks.get(), index);
            if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
                size_t count = 0;
                const auto* ids = mtmd_input_chunk_get_tokens_text(chunk, &count);
                for (size_t i = 0; ids && i < count; ++i) token(ids[i]);
            } else {
                if (mtmd_encode_chunk(media, chunk) != 0) throw std::runtime_error("Media encoder failed");
                checkCancellation();
                rows(mtmd_get_output_embd(media), mtmd_input_chunk_get_n_tokens(chunk));
            }
        }
        return run();
    }

    template<class F> QVector<float> guarded(F&& fn, QString* out) {
        error.clear();
        if (out) out->clear();
        reset();
        try {
            if (!context || !batch) throw std::runtime_error("Embedding runtime is not loaded");
            checkCancellation();
            auto result = fn();
            reset();
            return result;
        } catch (const std::exception& e) {
            reset(); error = QString::fromUtf8(e.what());
            if (out) *out = error;
            return {};
        }
    }
};

EmbeddingRuntime::EmbeddingRuntime() : d(std::make_unique<Private>()) {}
EmbeddingRuntime::~EmbeddingRuntime() { close(); }
void EmbeddingRuntime::close() { d->close(); }
bool EmbeddingRuntime::isLoaded() const { return d->context && d->batch; }
bool EmbeddingRuntime::isTextOnly() const { return d->textOnly; }
QString EmbeddingRuntime::lastError() const { return d->error; }
void EmbeddingRuntime::setThreadCount(int threads) {
    d->threads = std::clamp(threads, 1, std::max(1, QThread::idealThreadCount()));
    if (d->context) llama_set_n_threads(d->context, d->threads, d->threads);
}
void EmbeddingRuntime::setCancellationCallback(std::function<bool()> cancelled) {
    d->cancelled = std::move(cancelled);
}
QString EmbeddingRuntime::textModelName() { return QString::fromLatin1(TextName); }
QString EmbeddingRuntime::mediaModelName() { return QString::fromLatin1(MediaName); }
QString EmbeddingRuntime::fingerprint() {
    return embeddingPipelineFingerprint();
}
QString EmbeddingRuntime::prompt(const QString& text, PromptKind kind, const QString& title) {
    switch (kind) {
    case PromptKind::Query: return QStringLiteral("task: search result | query: ") + text;
    case PromptKind::CodeQuery: return QStringLiteral("task: code retrieval | query: ") + text;
    case PromptKind::Document: return QStringLiteral("title: ") + (title.isEmpty() ? QStringLiteral("none") : title)
                                      + QStringLiteral(" | text: ") + text;
    case PromptKind::Raw: return text;
    }
    return text;
}
bool EmbeddingRuntime::verifyModels(const QString& modelDir, bool textOnly, QString* error) {
    if (error) error->clear();
    if (modelDir.isEmpty()) { if (error) *error = QStringLiteral("Model directory is empty"); return false; }
    const QDir root(modelDir);
    return verify(root.filePath(textModelName()), 309855456, TextHash, error)
        && (textOnly || verify(root.filePath(mediaModelName()), 554821024, MediaHash, error));
}
bool EmbeddingRuntime::load(const QString& modelDir, bool textOnly, QString* error) {
    close(); d->error.clear(); d->textOnly = textOnly;
    if (error) error->clear();
    if (!verifyModels(modelDir, textOnly, &d->error)) { if (error) *error = d->error; return false; }
    try {
        static std::once_flag backend;
        std::call_once(backend, [] {
            llama_log_set(&logCallback, nullptr);
            mtmd_helper_log_set(&logCallback, nullptr);
            initializeBackend();
        });
        auto modelParams = llama_model_default_params();
        ggml_backend_dev_t cpuOnly[] = {nullptr};
        modelParams.devices = cpuOnly;
        modelParams.n_gpu_layers = 0;
        modelParams.progress_callback = &Private::loadProgress;
        modelParams.progress_callback_user_data = d.get();
        const auto textPath = QDir(modelDir).filePath(textModelName()).toUtf8();
        d->model = llama_model_load_from_file(textPath.constData(), modelParams);
        if (!d->model) throw std::runtime_error("Cannot load text model");
        d->maxTokens = textOnly ? 512 : 2048;
        auto params = llama_context_default_params();
        params.n_ctx = uint32_t(d->maxTokens);
        params.n_batch = uint32_t(d->maxTokens);
        params.n_ubatch = uint32_t(d->maxTokens);
        params.n_seq_max = 16;
        params.kv_unified = true;
        params.embeddings = true;
        params.pooling_type = LLAMA_POOLING_TYPE_MEAN;
        params.n_threads = d->threads;
        params.n_threads_batch = d->threads;
        params.abort_callback = &Private::abortCallback;
        params.abort_callback_data = d.get();
        params.offload_kqv = false;
        params.op_offload = false;
        d->context = llama_init_from_model(d->model, params);
        if (!d->context) throw std::runtime_error("Cannot create embedding context");
        d->vocab = llama_model_get_vocab(d->model);
        d->outputSize = llama_model_n_embd_out(d->model);
        d->rowSize = llama_model_n_embd_inp(d->model);
        if (d->outputSize != Dimension) throw std::runtime_error("Unexpected embedding dimension");
        d->batch = llama_batch_ext_init(d->context);
        if (!d->batch) throw std::runtime_error("Cannot allocate embedding batch");
        if (!textOnly) {
            auto mediaParams = mtmd_context_params_default();
            mediaParams.use_gpu = false;
            mediaParams.print_timings = false;
            mediaParams.warmup = true;
            mediaParams.n_threads = d->threads;
            mediaParams.progress_callback = &Private::loadProgress;
            mediaParams.progress_callback_user_data = d.get();
            mediaParams.image_min_tokens = 1;
            mediaParams.image_max_tokens = 1120;
            const auto mediaPath = QDir(modelDir).filePath(mediaModelName()).toUtf8();
            d->media = mtmd_init_from_file(mediaPath.constData(), d->model, mediaParams);
            if (!d->media) throw std::runtime_error("Cannot load image/audio model");
        }
        for (int i = 0; i < 2; ++i) {
            d->reset();
            d->text(prompt(QStringLiteral("warm up"), PromptKind::Query));
        }
        d->reset();
        return true;
    } catch (const std::exception& e) {
        d->error = QString::fromUtf8(e.what());
        if (error) *error = d->error;
        close();
        return false;
    }
}
QVector<float> EmbeddingRuntime::embedText(const QString& text, PromptKind kind, const QString& title, QString* error) {
    return d->guarded([&] { return d->text(prompt(text, kind, title)); }, error);
}
QVector<float> EmbeddingRuntime::embedImage(const QString& path, int budget, QString* error) {
    return d->guarded([&] { return d->mediaEmbedding(path, true, budget); }, error);
}
QVector<float> EmbeddingRuntime::embedAudio(const QString& path, QString* error) {
    return d->guarded([&] { return d->mediaEmbedding(path, false, 0); }, error);
}
}
