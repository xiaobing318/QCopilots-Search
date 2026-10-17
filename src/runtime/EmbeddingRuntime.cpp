// Adapted from DigUp LlamaRuntime, Copyright (c) 2026 Abdur Rahim, MIT.
#include "EmbeddingRuntime.h"
#include "BackendPaths.h"
#include "BackendSelection.h"
#include "BackendLoader.h"
#include "ImageSizing.h"
#include "core/pipeline_manifest.h"
#include "core/model_catalog.h"
#include "core/utf8_locale.h"

#include <QCryptographicHash>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QThread>
#include <llama.h>
#include <mtmd.h>
#include <mtmd-helper.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <stdexcept>

#ifndef QCOPILOTS_SEARCH_GPU_BACKEND
#define QCOPILOTS_SEARCH_GPU_BACKEND "cpu"
#endif
#ifndef QCOPILOTS_SEARCH_BUILD_VARIANT
#define QCOPILOTS_SEARCH_BUILD_VARIANT "cpu"
#endif

namespace qcopilots_search {
namespace {
constexpr auto TextName = "embeddinggemma-2-Q8_0.gguf";
constexpr auto MediaName = "mmproj-embeddinggemma-2-Q8_0.gguf";

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

QString packagedBackend() {
    return detail::canonicalBackendName(QString::fromLatin1(QCOPILOTS_SEARCH_GPU_BACKEND));
}

QStringList backendSearchRoots() {
#ifdef Q_OS_MACOS
    constexpr auto platform = detail::BackendPlatform::MacOS;
#elif defined(Q_OS_WIN)
    constexpr auto platform = detail::BackendPlatform::Windows;
#else
    constexpr auto platform = detail::BackendPlatform::Unix;
#endif
    return detail::cpuBackendSearchRoots(QCoreApplication::applicationDirPath(), platform);
}

void initializeBackend() {
    const ScopedUtf8Locale utf8Locale;
    if (!utf8Locale.isValid())
        throw std::runtime_error("Cannot initialize thread-local UTF-8 locale for backend loading");
    // Load only an explicitly known CPU backend from the installation. Upstream's
    // generic loader also consults cwd and GGML_BACKEND_PATH, which we don't use.
#ifdef QCOPILOTS_SEARCH_CPU_DISPATCH
    const bool baseline = qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_CPU_BASELINE") == 1;
    QStringList variants = baseline ? QStringList{QStringLiteral("x64")} :
        QStringList{QStringLiteral("sapphirerapids"), QStringLiteral("zen4"), QStringLiteral("cooperlake"),
                    QStringLiteral("icelake"), QStringLiteral("cascadelake"), QStringLiteral("cannonlake"),
                    QStringLiteral("skylakex"), QStringLiteral("alderlake"), QStringLiteral("haswell"),
                    QStringLiteral("piledriver"), QStringLiteral("ivybridge"), QStringLiteral("sandybridge"),
                    QStringLiteral("sse42"), QStringLiteral("x64")};
#else
    // Apple Silicon has one CPU module rather than the x64 dispatch variants.
    const QStringList variants{QString{}};
#endif
    const auto roots = backendSearchRoots();
    bool loaded = false;
    for (const auto& variant : variants) {
#ifdef Q_OS_WIN
        const QString name = variant.isEmpty() ? QStringLiteral("ggml-cpu.dll")
                                               : QStringLiteral("ggml-cpu-%1.dll").arg(variant);
#else
        const QString name = variant.isEmpty() ? QStringLiteral("libggml-cpu.so")
                                               : QStringLiteral("libggml-cpu-%1.so").arg(variant);
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
    llama_backend_init();
}

bool gpuBackendRegistered(const QString& backend) {
    for (size_t index = 0; index < ggml_backend_reg_count(); ++index) {
        const auto registry = ggml_backend_reg_get(index);
        if (detail::canonicalBackendName(QString::fromUtf8(ggml_backend_reg_name(registry))) == backend)
            return true;
    }
    return false;
}

QString initializeGpuBackend() {
    const auto backend = packagedBackend();
    if (backend != QStringLiteral("cpu")) {
        const auto error = detail::prepareGpuRuntime(QCoreApplication::applicationDirPath(), backend);
        if (!error.isEmpty()) return error;
    }
    // Metal can be linked statically. Never invoke ggml's cwd/environment loader.
    if (backend == QStringLiteral("cpu") || gpuBackendRegistered(backend)) return {};
    const ScopedUtf8Locale utf8Locale;
    if (!utf8Locale.isValid())
        return QStringLiteral("Cannot initialize thread-local UTF-8 locale for backend loading");
#ifdef Q_OS_WIN
    const auto name = QStringLiteral("ggml-%1.dll").arg(backend);
#else
    const auto name = QStringLiteral("libggml-%1.so").arg(backend);
#endif
    QStringList attempted;
    for (const auto& root : backendSearchRoots()) {
        const auto path = QDir(root).absoluteFilePath(name);
        if (!QFileInfo::exists(path)) continue;
        attempted.append(path);
        if (ggml_backend_load(path.toUtf8().constData()) && gpuBackendRegistered(backend)) return {};
    }
    if (attempted.isEmpty())
        return QStringLiteral("Packaged %1 GPU module %2 was not found in the installation.").arg(backend, name);
    return QStringLiteral("Cannot load packaged %1 GPU module (%2). Check its runtime dependencies and GPU driver.")
        .arg(backend, attempted.join(QStringLiteral(", ")));
}

struct RuntimeDevices {
    QVector<ggml_backend_dev_t> handles;
    QVector<detail::BackendDevice> descriptions;
};

RuntimeDevices enumerateDevices() {
    RuntimeDevices result;
    for (size_t index = 0; index < ggml_backend_dev_count(); ++index) {
        const auto device = ggml_backend_dev_get(index);
        const auto type = ggml_backend_dev_type(device);
        const auto registry = ggml_backend_dev_backend_reg(device);
        ggml_backend_dev_props properties{};
        ggml_backend_dev_get_props(device, &properties);
        result.handles.append(device);
        result.descriptions.append({detail::canonicalBackendName(QString::fromUtf8(ggml_backend_reg_name(registry))),
            QString::fromUtf8(ggml_backend_dev_name(device)), QString::fromUtf8(ggml_backend_dev_description(device)),
            type == GGML_BACKEND_DEVICE_TYPE_CPU,
            type == GGML_BACKEND_DEVICE_TYPE_GPU || type == GGML_BACKEND_DEVICE_TYPE_IGPU,
            {}, QString::fromUtf8(properties.device_id ? properties.device_id : ""),
            quint64(properties.memory_total), quint64(properties.memory_free)});
    }
    return result;
}

struct GpuSelfTestGraph {
    static constexpr size_t Side = 32;
    static constexpr size_t GraphSize = 8;
    std::unique_ptr<ggml_context, decltype(&ggml_free)> context;
    ggml_tensor* a = nullptr;
    ggml_tensor* b = nullptr;
    ggml_tensor* product = nullptr;
    ggml_cgraph* graph = nullptr;

    GpuSelfTestGraph() : context(ggml_init({3 * ggml_tensor_overhead()
        + ggml_graph_overhead_custom(GraphSize, false), nullptr, true}), &ggml_free) {
        if (!context) throw std::runtime_error("Cannot allocate GPU self-test metadata");
        // Capability queries and actual computation use this identical graph.
        // A full small tile avoids odd or tiny matrix support differences.
        a = ggml_new_tensor_2d(context.get(), GGML_TYPE_F32, Side, Side);
        b = ggml_new_tensor_2d(context.get(), GGML_TYPE_F32, Side, Side);
        product = ggml_mul_mat(context.get(), a, b);
        graph = ggml_new_graph_custom(context.get(), GraphSize, false);
        ggml_build_forward_expand(graph, product);
    }
};

void checkMetalCapabilities(RuntimeDevices& devices) {
    std::unique_ptr<GpuSelfTestGraph> selfTest;
    for (qsizetype index = 0; index < devices.descriptions.size(); ++index) {
        auto& device = devices.descriptions[index];
        if (!device.gpu || device.backend != QStringLiteral("metal") || device.name.isEmpty()) continue;
        if (!selfTest) selfTest = std::make_unique<GpuSelfTestGraph>();
        // Metal reads device properties and tensor metadata here. Only an
        // explicit unsupported result excludes a candidate before selection.
        if (!ggml_backend_dev_supports_op(devices.handles[index], selfTest->product))
            device.unavailableReason = QStringLiteral("metal GPU device %1 does not support required %2x%2 F32 matrix multiplication.")
                .arg(device.name).arg(GpuSelfTestGraph::Side);
    }
}

std::recursive_mutex& initializationMutex() {
    static std::recursive_mutex mutex;
    return mutex;
}

RuntimeDevices initializeDevices(detail::BackendMode mode, QString* loadError) {
    // ggml's registry is process-wide. Discovery and model initialization share
    // the same lock and once flags so a concurrent CPU runtime sees a snapshot.
    const std::lock_guard initializationLock(initializationMutex());
    static std::once_flag backend;
    std::call_once(backend, [] {
        llama_log_set(&logCallback, nullptr);
        mtmd_helper_log_set(&logCallback, nullptr);
        initializeBackend();
    });
    static std::once_flag gpuBackend;
    static QString gpuLoadError;
    if (mode != detail::BackendMode::Cpu)
        std::call_once(gpuBackend, [] { gpuLoadError = initializeGpuBackend(); });
    *loadError = mode == detail::BackendMode::Cpu ? QString{} : gpuLoadError;
    auto devices = enumerateDevices();
    if (mode != detail::BackendMode::Cpu && gpuLoadError.isEmpty() && packagedBackend() == QStringLiteral("metal"))
        checkMetalCapabilities(devices);
    return devices;
}

void verifyGpuComputation(ggml_backend_dev_t device) {
    // This intentionally bypasses the scheduler. An unsupported or failing GPU
    // operation must not be silently dispatched to CPU during this self-test.
    std::unique_ptr<ggml_backend, decltype(&ggml_backend_free)> backend(
        ggml_backend_dev_init(device, nullptr), &ggml_backend_free);
    if (!backend || ggml_backend_get_device(backend.get()) != device)
        throw std::runtime_error("Cannot initialize the selected GPU for its compute self-test");
    GpuSelfTestGraph selfTest;
    if (!ggml_backend_supports_op(backend.get(), selfTest.product))
        throw std::runtime_error("Selected GPU does not support the compute self-test operation");
    std::unique_ptr<ggml_backend_buffer, decltype(&ggml_backend_buffer_free)> buffer(
        ggml_backend_alloc_ctx_tensors(selfTest.context.get(), backend.get()), &ggml_backend_buffer_free);
    if (!buffer) throw std::runtime_error("Cannot allocate GPU self-test tensors");
    // Integer-valued operands have exact float references.
    constexpr size_t side = GpuSelfTestGraph::Side;
    std::array<float, side * side> aValues{}, bValues{}, expected{}, actual{};
    for (size_t index = 0; index < aValues.size(); ++index) {
        aValues[index] = float(index % 7 + 1);
        bValues[index] = float(index % 13 + 2);
    }
    for (size_t row = 0; row < side; ++row)
        for (size_t column = 0; column < side; ++column)
            for (size_t k = 0; k < side; ++k)
                expected[row * side + column] += aValues[column * side + k] * bValues[row * side + k];
    ggml_backend_tensor_set(selfTest.a, aValues.data(), 0, sizeof(aValues));
    ggml_backend_tensor_set(selfTest.b, bValues.data(), 0, sizeof(bValues));
    if (ggml_backend_graph_compute(backend.get(), selfTest.graph) != GGML_STATUS_SUCCESS)
        throw std::runtime_error("Selected GPU failed its compute self-test");
    ggml_backend_synchronize(backend.get());
    ggml_backend_tensor_get(selfTest.product, actual.data(), 0, sizeof(actual));
    for (size_t index = 0; index < actual.size(); ++index) {
        if (!std::isfinite(actual[index]) || std::abs(actual[index] - expected[index]) > 0.001f)
            throw std::runtime_error("Selected GPU returned an incorrect compute self-test result");
    }
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
    const ModelDescriptor* descriptor = findModel(defaultModelId());
    bool qwen() const { return descriptor && descriptor->adapter == QStringLiteral("qwen3-vl-embedding"); }
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
    QString requestedMode = qEnvironmentVariable("QCOPILOTS_SEARCH_BACKEND", QStringLiteral("auto"));
    std::optional<detail::BackendDeviceSelector> deviceSelector;
    detail::BackendSelection selection;
    detail::BackendDevice selectedDevice;
    bool gpuSelfTestPassed = false;
    bool textWarmupCompleted = false;
    bool deviceFailure = false;
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
    [[noreturn]] void computationFailed(const std::string& message) {
        deviceFailure = selection.gpu && !abortCallback(this);
        throw std::runtime_error(message);
    }

    void reset() {
        if (batch) llama_batch_ext_clear(batch);
        if (context && qwen()) {
            if (auto memory = llama_get_memory(context)) llama_memory_clear(memory, true);
        }
        position = 0;
    }
    void close() {
        if (batch) llama_batch_ext_free(batch);
        if (media) mtmd_free(media);
        if (context) llama_free(context);
        if (model) llama_model_free(model);
        batch = nullptr; media = nullptr; context = nullptr; model = nullptr; vocab = nullptr;
        textWarmupCompleted = false;
    }
    void loadModels(const QString& modelDir, ggml_backend_dev_t device) {
        checkCancellation();
        const bool gpu = selection.gpu;
        gpuSelfTestPassed = false;
        if (gpu) {
            verifyGpuComputation(device);
            checkCancellation();
            gpuSelfTestPassed = true;
        }
        auto modelParams = llama_model_default_params();
        // A terminated empty list forces CPU. Never let upstream auto-select a
        // different GPU from the one recorded in our diagnostics.
        ggml_backend_dev_t devices[] = {gpu ? device : nullptr, nullptr};
        modelParams.devices = devices;
        modelParams.n_gpu_layers = gpu ? -1 : 0;
        modelParams.split_mode = LLAMA_SPLIT_MODE_NONE;
        modelParams.main_gpu = 0;
        modelParams.progress_callback = &Private::loadProgress;
        modelParams.progress_callback_user_data = this;
        const auto textPath = QDir(modelDir).filePath(descriptor->textFile).toUtf8();
        model = llama_model_load_from_file(textPath.constData(), modelParams);
        if (!model) throw std::runtime_error("Cannot load text model");
        maxTokens = textOnly ? 512 : 2048;
        auto params = llama_context_default_params();
        params.n_ctx = uint32_t(maxTokens);
        params.n_batch = qwen() ? 256 : uint32_t(maxTokens);
        params.n_ubatch = params.n_batch;
        params.n_seq_max = qwen() ? 1 : 16;
        params.kv_unified = true;
        params.embeddings = true;
        params.pooling_type = qwen() ? LLAMA_POOLING_TYPE_LAST : LLAMA_POOLING_TYPE_MEAN;
        params.n_threads = threads;
        params.n_threads_batch = threads;
        params.abort_callback = &Private::abortCallback;
        params.abort_callback_data = this;
        params.offload_kqv = gpu;
        params.op_offload = gpu;
        context = llama_init_from_model(model, params);
        if (!context) throw std::runtime_error("Cannot create embedding context");
        vocab = llama_model_get_vocab(model);
        outputSize = llama_model_n_embd_out(model);
        rowSize = llama_model_n_embd_inp(model);
        if (outputSize != descriptor->dimensions) throw std::runtime_error("Unexpected embedding dimension for selected model");
        batch = llama_batch_ext_init(context);
        if (!batch) throw std::runtime_error("Cannot allocate embedding batch");
        if (!textOnly) {
            auto mediaParams = mtmd_context_params_default();
            mediaParams.use_gpu = gpu;
            mediaParams.device = gpu ? device : nullptr;
            mediaParams.print_timings = false;
            mediaParams.warmup = !qwen();
            mediaParams.n_threads = threads;
            mediaParams.progress_callback = &Private::loadProgress;
            mediaParams.progress_callback_user_data = this;
            mediaParams.image_min_tokens = qwen() ? 4 : 1;
            mediaParams.image_max_tokens = qwen() ? 1280 : 1120;
            const auto mediaPath = QDir(modelDir).filePath(descriptor->mediaFile).toUtf8();
            media = mtmd_init_from_file(mediaPath.constData(), model, mediaParams);
            if (!media) throw std::runtime_error("Cannot load image/audio model");
            if (mtmd_support_vision(media) != descriptor->supportsVision
                || mtmd_support_audio(media) != descriptor->supportsAudio)
                throw std::runtime_error("Loaded projector capabilities differ from selected model");
        }
        for (int i = 0; i < 2; ++i) {
            reset();
            textInput(QStringLiteral("warm up"), PromptKind::Query, {});
        }
        reset();
        textWarmupCompleted = true;
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
        if (status != 0) computationFailed("llama_process failed: " + std::to_string(status));
        return output();
    }
    QVector<float> output() {
        const float* pooled = llama_get_embeddings_seq(context, 0);
        if (!pooled) computationFailed("Model returned no pooled embedding");
        double square = 0;
        for (int i = 0; i < outputSize; ++i) {
            if (!std::isfinite(pooled[i])) computationFailed("Model returned a non-finite embedding");
            square += double(pooled[i]) * double(pooled[i]);
        }
        if (!(square > 0)) computationFailed("Model returned a zero embedding");
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

    QVector<llama_token> tokenize(const QString& input, bool special) {
        const auto utf8 = input.toUtf8();
        if (utf8.size() > std::numeric_limits<int32_t>::max() - 8)
            throw std::runtime_error("Text input is too large");
        QVector<llama_token> tokens(utf8.size() + 8);
        int count = llama_tokenize(vocab, utf8.constData(), int32_t(utf8.size()),
            tokens.data(), int32_t(tokens.size()), false, special);
        if (count < 0) {
            if (count == std::numeric_limits<int32_t>::min()) throw std::runtime_error("Tokenizer failed");
            tokens.resize(-count);
            count = llama_tokenize(vocab, utf8.constData(), int32_t(utf8.size()),
                tokens.data(), int32_t(tokens.size()), false, special);
        }
        if (count < 0) throw std::runtime_error("Tokenizer failed");
        tokens.resize(count);
        return tokens;
    }
    void decodeTokens(const QVector<llama_token>& tokens) {
        for (qsizetype start = 0; start < tokens.size(); start += 256) {
            checkCancellation();
            llama_batch_ext_clear(batch);
            const auto count = std::min<qsizetype>(256, tokens.size() - start);
            for (qsizetype index = 0; index < count; ++index) token(tokens[start + index]);
            const int status = llama_process(context, LLAMA_PROCESS_TYPE_DECODE, batch);
            if (status != 0) computationFailed("Qwen text decode failed: " + std::to_string(status));
        }
        checkCancellation();
    }
    QVector<float> textInput(const QString& input, PromptKind kind, const QString& title) {
        if (!qwen()) return text(EmbeddingRuntime::prompt(input, kind, title));
        QVector<llama_token> tokens;
        if (kind == PromptKind::Raw) {
            tokens = tokenize(input, true);
            tokens.resize(std::min<qsizetype>(tokens.size(), maxTokens));
        } else {
            // Keep the prefix/content BPE boundary identical to the official
            // template, including a user body that begins with newlines. The
            // suffix begins with a special token and can be reserved separately.
            // The official tokenizer's TemplateProcessing appends endoftext
            // after the assistant newline. It is a valid input, not padding,
            // and LAST pooling must select that terminal token.
            const auto prefix = QStringLiteral("<|im_start|>system\nRepresent the user's input.<|im_end|>\n<|im_start|>user\n");
            auto suffix = tokenize(QStringLiteral("<|im_end|>\n<|im_start|>assistant\n<|endoftext|>"), true);
            const auto content = kind == PromptKind::Document && !title.isEmpty()
                ? title + QLatin1Char('\n') + input : input;
            tokens = tokenize(prefix + content, true);
            const auto capacity = maxTokens - suffix.size();
            if (capacity < tokenize(prefix, true).size())
                throw std::runtime_error("Qwen context cannot hold the embedding prompt");
            tokens.resize(std::min(tokens.size(), capacity));
            tokens += suffix;
        }
        if (tokens.isEmpty()) throw std::runtime_error("Tokenizer returned no input tokens");
        decodeTokens(tokens);
        return output();
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
            const auto target = qwen() ? ImageSizing::qwenTargetSize(height, width, budget)
                                       : ImageSizing::targetSize(height, width, budget);
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
        if ((image && !descriptor->supportsVision) || (!image && !descriptor->supportsAudio))
            throw std::runtime_error(image ? "Selected model does not support image embeddings"
                                          : "Selected model does not support audio embeddings");
        if (image && budget != 70 && budget != 140 && budget != 280 && budget != 560 && budget != 1120)
            throw std::runtime_error("Image budget must be 70, 140, 280, 560 or 1120");
        auto bitmap = readBitmap(path, image, budget);
        Chunks chunks(mtmd_input_chunks_init(), &mtmd_input_chunks_free);
        if (!chunks) throw std::runtime_error("Cannot allocate media chunks");
        const auto prompt = qwen()
            ? EmbeddingRuntime::promptForModel(descriptor->id, QString::fromUtf8(mtmd_default_marker()), PromptKind::Document).toUtf8()
            : QByteArray(mtmd_default_marker());
        mtmd_input_text text{prompt.constData(), size_t(prompt.size()), !qwen(), true};
        const mtmd_bitmap* bitmaps[] = {bitmap.get()};
        const auto status = mtmd_tokenize(media, chunks.get(), &text, bitmaps, 1);
        if (status != 0) throw std::runtime_error("Cannot tokenize media: " + std::to_string(status));
        if (qwen()) {
            if (mtmd_helper_get_n_tokens(chunks.get()) > size_t(maxTokens))
                throw std::runtime_error("Media embedding exceeds context token capacity");
            llama_pos past = 0;
            for (size_t index = 0; index < mtmd_input_chunks_size(chunks.get()); ++index) {
                checkCancellation();
                const auto* chunk = mtmd_input_chunks_get(chunks.get(), index);
                const int result = mtmd_helper_eval_chunk_single(media, context, chunk,
                    past, 0, 256, index + 1 == mtmd_input_chunks_size(chunks.get()), &past);
                if (result != 0) computationFailed("Qwen media decode failed: " + std::to_string(result));
            }
            checkCancellation();
            return output();
        }
        for (size_t index = 0; index < mtmd_input_chunks_size(chunks.get()); ++index) {
            checkCancellation();
            const auto* chunk = mtmd_input_chunks_get(chunks.get(), index);
            if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
                size_t count = 0;
                const auto* ids = mtmd_input_chunk_get_tokens_text(chunk, &count);
                for (size_t i = 0; ids && i < count; ++i) token(ids[i]);
            } else {
                if (mtmd_encode_chunk(media, chunk) != 0) computationFailed("Media encoder failed");
                checkCancellation();
                rows(mtmd_get_output_embd(media), mtmd_input_chunk_get_n_tokens(chunk));
            }
        }
        return run();
    }

    template<class F> QVector<float> guarded(F&& fn, QString* out) {
        error.clear();
        deviceFailure = false;
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
EmbeddingRuntime::EmbeddingRuntime(const QString& modelId) : EmbeddingRuntime() {
    const auto* descriptor = findModel(modelId);
    if (!descriptor) throw std::invalid_argument("Unknown embedding model preset");
    d->descriptor = descriptor;
}
EmbeddingRuntime::~EmbeddingRuntime() { close(); }
void EmbeddingRuntime::close() { d->close(); }
bool EmbeddingRuntime::isLoaded() const { return d->context && d->batch; }
bool EmbeddingRuntime::isTextOnly() const { return d->textOnly; }
int EmbeddingRuntime::dimensions() const { return d->descriptor ? d->descriptor->dimensions : 0; }
QString EmbeddingRuntime::modelId() const { return d->descriptor ? d->descriptor->id : QString(); }
QJsonObject EmbeddingRuntime::capabilities() const {
    return {{QStringLiteral("text"), true},
            {QStringLiteral("image"), d->descriptor && d->descriptor->supportsVision},
            {QStringLiteral("videoFrames"), d->descriptor && d->descriptor->supportsVision},
            {QStringLiteral("audio"), d->descriptor && d->descriptor->supportsAudio}};
}
QString EmbeddingRuntime::modelFingerprint() const { return modelPipelineFingerprint(modelId()); }
QString EmbeddingRuntime::lastError() const { return d->error; }
bool EmbeddingRuntime::setBackendMode(const QString& mode, QString* error) {
    if (error) error->clear();
    if (isLoaded()) {
        if (error) *error = QStringLiteral("Close the embedding runtime before changing its backend mode.");
        return false;
    }
    const auto parsed = detail::parseBackendMode(mode, error);
    if (!parsed) return false;
    d->requestedMode = detail::backendModeName(*parsed);
    return true;
}
bool EmbeddingRuntime::setDevice(const QJsonObject& selector, QString* error) {
    if (error) error->clear();
    if (isLoaded()) {
        if (error) *error = QStringLiteral("Close the embedding runtime before changing its device binding.");
        return false;
    }
    if (selector.isEmpty()) {
        d->deviceSelector.reset();
        return true;
    }
    for (auto item = selector.begin(); item != selector.end(); ++item) {
        if ((item.key() != QStringLiteral("backend") && item.key() != QStringLiteral("deviceId")
                && item.key() != QStringLiteral("name")) || !item.value().isString()) {
            if (error) *error = QStringLiteral("Device selection accepts only string backend, deviceId and name fields.");
            return false;
        }
    }
    detail::BackendDeviceSelector parsed{detail::canonicalBackendName(selector.value(QStringLiteral("backend")).toString()),
        selector.value(QStringLiteral("deviceId")).toString(), selector.value(QStringLiteral("name")).toString()};
    const auto& backend = parsed.backend;
    if ((backend != QStringLiteral("cpu") && backend != QStringLiteral("cuda") && backend != QStringLiteral("hip")
         && backend != QStringLiteral("vulkan") && backend != QStringLiteral("metal"))
        || (parsed.deviceId.isEmpty() && parsed.name.isEmpty())) {
        if (error) *error = QStringLiteral("Device selection requires a supported backend and a hardware ID or unique name.");
        return false;
    }
    d->deviceSelector = std::move(parsed);
    return true;
}
QJsonObject EmbeddingRuntime::discoverDevices(const QString& requestedMode, QString* error) {
    if (error) error->clear();
    const auto mode = detail::parseBackendMode(requestedMode, error);
    if (!mode) return {};
    try {
        QString loadError;
        const auto snapshot = initializeDevices(*mode, &loadError);
        const auto installation = detail::selectBackend(detail::BackendMode::Cpu, packagedBackend(), snapshot.descriptions);
        if (!installation.error.isEmpty()) throw std::runtime_error(installation.error.toStdString());
        const auto compatible = detail::compatibleGpuDevices(packagedBackend(), snapshot.descriptions);
        QJsonArray devices;
        for (qsizetype index = 0; index < snapshot.descriptions.size(); ++index) {
            const auto& device = snapshot.descriptions[index];
            if (!device.cpu && (!device.gpu || device.backend != packagedBackend()
                || *mode == detail::BackendMode::Cpu)) continue;
            QString reason = device.unavailableReason;
            if (device.gpu && !loadError.isEmpty()) reason = loadError;
            if (reason.isEmpty()) reason = detail::deviceIdentityError(device, snapshot.descriptions);
            if (reason.isEmpty() && device.backend == QStringLiteral("metal") && device.name.isEmpty())
                reason = QStringLiteral("No physical Metal device is available.");
            const bool available = reason.isEmpty() && (device.cpu || compatible.contains(int(index)));
            devices.append(QJsonObject{{QStringLiteral("backend"), device.backend},
                {QStringLiteral("deviceId"), device.deviceId}, {QStringLiteral("name"), device.name},
                {QStringLiteral("description"), device.description}, {QStringLiteral("cpu"), device.cpu},
                {QStringLiteral("gpu"), device.gpu}, {QStringLiteral("memoryTotal"), double(device.memoryTotal)},
                {QStringLiteral("memoryFree"), double(device.memoryFree)}, {QStringLiteral("available"), available},
                {QStringLiteral("unavailableReason"), reason}});
        }
        return {{QStringLiteral("packagedBackend"), packagedBackend()},
            {QStringLiteral("requestedMode"), detail::backendModeName(*mode)},
            {QStringLiteral("gpuLoadError"), loadError}, {QStringLiteral("devices"), devices}};
    } catch (const std::exception& failure) {
        if (error) *error = QString::fromUtf8(failure.what());
        return {};
    }
}
QJsonObject EmbeddingRuntime::diagnostics() const {
    QJsonObject requestedDevice;
    if (d->deviceSelector)
        requestedDevice = {{QStringLiteral("backend"), d->deviceSelector->backend},
            {QStringLiteral("deviceId"), d->deviceSelector->deviceId}, {QStringLiteral("name"), d->deviceSelector->name}};
    return {{QStringLiteral("buildVariant"), QString::fromLatin1(QCOPILOTS_SEARCH_BUILD_VARIANT)},
            {QStringLiteral("modelId"), modelId()},
            {QStringLiteral("dimensions"), dimensions()},
            {QStringLiteral("embeddingFingerprint"), modelFingerprint()},
            {QStringLiteral("capabilities"), capabilities()},
            {QStringLiteral("requestedBackend"), d->requestedMode},
            {QStringLiteral("packagedBackend"), packagedBackend()},
            {QStringLiteral("selectedBackend"), d->selection.backend},
            {QStringLiteral("deviceName"), d->selectedDevice.name},
            {QStringLiteral("deviceDescription"), d->selectedDevice.description},
            {QStringLiteral("deviceId"), d->selectedDevice.deviceId},
            {QStringLiteral("requestedDevice"), requestedDevice},
            {QStringLiteral("deviceBindingExplicit"), d->deviceSelector.has_value()},
            {QStringLiteral("deviceFailure"), d->deviceFailure},
            {QStringLiteral("gpuSelected"), d->selection.gpu},
            {QStringLiteral("gpuSelfTestPassed"), d->gpuSelfTestPassed},
            {QStringLiteral("fallbackReason"), d->selection.fallbackReason},
            {QStringLiteral("loaded"), isLoaded()},
            {QStringLiteral("textWarmupCompleted"), d->textWarmupCompleted},
            {QStringLiteral("error"), d->error}};
}
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
    return modelPipelineFingerprint(defaultModelId());
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
QString EmbeddingRuntime::promptForModel(const QString& modelId, const QString& text, PromptKind kind, const QString& title) {
    const auto* model = findModel(modelId);
    if (!model) return {};
    if (model->adapter != QStringLiteral("qwen3-vl-embedding")) return prompt(text, kind, title);
    if (kind == PromptKind::Raw) return text;
    const auto content = kind == PromptKind::Document && !title.isEmpty()
        ? title + QLatin1Char('\n') + text : text;
    return QStringLiteral("<|im_start|>system\nRepresent the user's input.<|im_end|>\n<|im_start|>user\n")
        + content + QStringLiteral("<|im_end|>\n<|im_start|>assistant\n<|endoftext|>");
}
bool EmbeddingRuntime::verifyModels(const QString& modelDir, bool textOnly, QString* error) {
    return verifyModels(modelDir, defaultModelId(), textOnly, error);
}
bool EmbeddingRuntime::verifyModels(const QString& modelRoot, const QString& modelId, bool textOnly, QString* error) {
    if (error) error->clear();
    const auto* model = findModel(modelId);
    if (!model) { if (error) *error = QStringLiteral("Unknown embedding model: %1").arg(modelId); return false; }
    if (modelRoot.isEmpty()) { if (error) *error = QStringLiteral("Model directory is empty"); return false; }
    const QDir root(modelDirectory(modelRoot, modelId));
    for (const auto& artifact : model->artifacts) {
        if (textOnly && artifact.name != model->textFile) continue;
        if (!verify(root.filePath(artifact.name), artifact.size, artifact.sha256.constData(), error)) return false;
    }
    return true;
}
bool EmbeddingRuntime::load(const QString& modelDir, bool textOnly, QString* error) {
    return load(modelDir, defaultModelId(), textOnly, error);
}
bool EmbeddingRuntime::load(const QString& modelRoot, const QString& modelId, bool textOnly, QString* error) {
    close(); d->error.clear(); d->textOnly = textOnly;
    d->selection = {}; d->selectedDevice = {};
    d->gpuSelfTestPassed = false;
    d->deviceFailure = false;
    if (error) error->clear();
    const auto* descriptor = findModel(modelId);
    if (!descriptor) {
        d->error = QStringLiteral("Unknown embedding model: %1").arg(modelId);
        if (error) *error = d->error;
        return false;
    }
    d->descriptor = descriptor;
    const auto mode = detail::parseBackendMode(d->requestedMode, &d->error);
    if (!mode) { if (error) *error = d->error; return false; }
    d->requestedMode = detail::backendModeName(*mode);
    if (!verifyModels(modelRoot, modelId, textOnly, &d->error)) { if (error) *error = d->error; return false; }
    const auto modelDir = modelDirectory(modelRoot, modelId);
    try {
        const std::lock_guard initializationLock(initializationMutex());
        QString gpuLoadError;
        // An explicit CPU binding must not touch vendor drivers, even when the
        // caller leaves the general policy at Auto. Policy conflicts are still
        // checked against the original mode below.
        const auto initializationMode = d->deviceSelector && d->deviceSelector->backend == QStringLiteral("cpu")
            ? detail::BackendMode::Cpu : *mode;
        auto devices = initializeDevices(initializationMode, &gpuLoadError);
        d->selection = d->deviceSelector
            ? detail::selectBackendDevice(*mode, packagedBackend(), devices.descriptions, *d->deviceSelector, gpuLoadError)
            : detail::selectBackend(*mode, packagedBackend(), devices.descriptions, gpuLoadError);
        if (!d->selection.error.isEmpty()) {
            d->deviceFailure = d->deviceSelector && d->deviceSelector->backend != QStringLiteral("cpu");
            throw std::runtime_error(d->selection.error.toStdString());
        }
        d->selectedDevice = devices.descriptions[d->selection.deviceIndex];
        try {
            d->loadModels(modelDir, devices.handles[d->selection.deviceIndex]);
        } catch (const std::exception& gpuFailure) {
            d->deviceFailure = d->selection.gpu && !Private::abortCallback(d.get());
            // A cancellation is not a GPU failure and must never restart loading.
            if (d->deviceSelector || *mode != detail::BackendMode::Auto || !d->selection.gpu || Private::abortCallback(d.get())) throw;
            const auto reason = QStringLiteral("%1 GPU initialization or warmup failed: %2. Retrying on CPU.")
                .arg(packagedBackend(), QString::fromUtf8(gpuFailure.what()));
            d->close();
            d->selection = detail::selectBackend(detail::BackendMode::Cpu, packagedBackend(), devices.descriptions);
            d->selection.fallbackReason = reason;
            if (!d->selection.error.isEmpty()) throw std::runtime_error(d->selection.error.toStdString());
            d->selectedDevice = devices.descriptions[d->selection.deviceIndex];
            d->deviceFailure = false;
            d->loadModels(modelDir, devices.handles[d->selection.deviceIndex]);
        }
        return true;
    } catch (const std::exception& e) {
        d->error = QString::fromUtf8(e.what());
        if (error) *error = d->error;
        close();
        return false;
    }
}
QVector<float> EmbeddingRuntime::embedText(const QString& text, PromptKind kind, const QString& title, QString* error) {
    return d->guarded([&] { return d->textInput(text, kind, title); }, error);
}
QVector<float> EmbeddingRuntime::embedImage(const QString& path, int budget, QString* error) {
    return d->guarded([&] { return d->mediaEmbedding(path, true, budget); }, error);
}
QVector<float> EmbeddingRuntime::embedAudio(const QString& path, QString* error) {
    return d->guarded([&] { return d->mediaEmbedding(path, false, 0); }, error);
}
}
