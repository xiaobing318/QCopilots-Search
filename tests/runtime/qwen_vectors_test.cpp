#include "runtime/EmbeddingRuntime.h"
#include "runtime/ImageSizing.h"
#include "support/test_environment.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMap>
#include <QRegularExpression>
#include <QSet>
#include <QStandardPaths>
#include <QStringList>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <stdexcept>

#ifndef QCOPILOTS_SEARCH_QWEN_FIXTURES
#error The build must provide QCOPILOTS_SEARCH_QWEN_FIXTURES as an absolute fixture directory.
#endif

using namespace qcopilots_search;

namespace {
struct Input {
    QString id;
    QString kind;
    QString text;
    QString title;
    QString imagePath;
    int budget = 0;
    bool textOnly = false;
    QVector<float> expected;
};

struct Ranking {
    QString query;
    QString preferred;
    QString other;
};

struct Manifest {
    QString modelId;
    int dimensions = 0;
    double minimumCosine = 0;
    QVector<Input> inputs;
    QVector<Ranking> rankings;
};

void require(bool condition, const QString& message)
{
    if (!condition) throw std::runtime_error(message.toUtf8().constData());
}

int modelDimensions(const QString& modelId)
{
    if (modelId == QStringLiteral("qwen3-vl-embedding-2b")) return 2048;
    if (modelId == QStringLiteral("qwen3-vl-embedding-8b")) return 4096;
    throw std::runtime_error(QStringLiteral("Unknown Qwen fixture model: %1").arg(modelId).toStdString());
}

void validateVector(const QVector<float>& vector, int dimensions, const QString& label)
{
    require(vector.size() == dimensions, QStringLiteral("%1: expected %2 dimensions, received %3")
        .arg(label).arg(dimensions).arg(vector.size()));
    double squaredNorm = 0;
    for (float value : vector) {
        require(std::isfinite(value), label + QStringLiteral(": non-finite vector value"));
        squaredNorm += double(value) * value;
    }
    const double norm = std::sqrt(squaredNorm);
    require(std::abs(norm - 1.0) <= 0.0001,
        QStringLiteral("%1: vector is not normalized, norm=%2").arg(label).arg(norm, 0, 'g', 12));
}

double cosine(const QVector<float>& first, const QVector<float>& second)
{
    require(!first.isEmpty() && first.size() == second.size(), QStringLiteral("Cosine dimension mismatch"));
    double dot = 0, firstSquared = 0, secondSquared = 0;
    for (qsizetype index = 0; index < first.size(); ++index) {
        dot += double(first[index]) * second[index];
        firstSquared += double(first[index]) * first[index];
        secondSquared += double(second[index]) * second[index];
    }
    return dot / std::sqrt(firstSquared * secondSquared);
}

QByteArray readFile(const QString& path, qint64 maximumBytes)
{
    QFile file(path);
    const bool opened = file.open(QIODevice::ReadOnly);
    require(opened, QStringLiteral("Cannot read fixture %1: %2").arg(path, file.errorString()));
    require(file.size() > 0 && file.size() <= maximumBytes, QStringLiteral("Fixture size is invalid: %1").arg(path));
    const auto bytes = file.readAll();
    require(bytes.size() == file.size(), QStringLiteral("Incomplete fixture read: %1").arg(path));
    return bytes;
}

void verifyHash(const QJsonValue& value, const QByteArray& bytes, const QString& label)
{
    static const QRegularExpression hashPattern(QStringLiteral("^[0-9a-f]{64}$"));
    require(value.isString() && hashPattern.match(value.toString()).hasMatch(),
        label + QStringLiteral(": sha256 must contain 64 lowercase hexadecimal characters"));
    require(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex() == value.toString().toLatin1(),
        label + QStringLiteral(": SHA-256 mismatch"));
}

void validateProvenance(const QJsonObject& provenance, const QString& modelId)
{
    const bool small = modelId == QStringLiteral("qwen3-vl-embedding-2b");
    const QMap<QString, QString> expected{
        {QStringLiteral("sourceRepo"), QStringLiteral("https://github.com/QwenLM/Qwen3-VL-Embedding")},
        {QStringLiteral("revision"), QStringLiteral("393e2978d27852b0d0230d6994f37f9c15bed73c")},
        {QStringLiteral("scriptPath"), QStringLiteral("src/models/qwen3_vl_embedding.py")},
        {QStringLiteral("scriptSha256"), QStringLiteral("0e447b6a80e05527f3fd437ab4f1617a6e7d0df102b8164348bbce787a41b267")},
        {QStringLiteral("modelRepo"), small ? QStringLiteral("Qwen/Qwen3-VL-Embedding-2B") : QStringLiteral("Qwen/Qwen3-VL-Embedding-8B")},
        {QStringLiteral("modelRevision"), small ? QStringLiteral("9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda")
                                               : QStringLiteral("2c4565515e0f265c6511776e7193b22c0968ddc7")},
        {QStringLiteral("dtype"), small ? QStringLiteral("float32") : QStringLiteral("bfloat16")},
        {QStringLiteral("normalizationDtype"), QStringLiteral("float32")},
        {QStringLiteral("device"), QStringLiteral("cpu")},
        {QStringLiteral("attention"), QStringLiteral("sdpa")}
    };
    for (auto it = expected.cbegin(); it != expected.cend(); ++it)
        require(provenance.value(it.key()).toString() == it.value(),
            QStringLiteral("%1: oracle provenance differs from the locked %2").arg(modelId, it.key()));
    require(provenance.value(QStringLiteral("batchSize")).toDouble(-1) == 1
            && provenance.value(QStringLiteral("useCache")).isBool()
            && !provenance.value(QStringLiteral("useCache")).toBool(),
        modelId + QStringLiteral(": oracle must use batch size 1 with KV cache disabled"));
    const QMap<QString, QString> dependencies{
        {QStringLiteral("torch"), QStringLiteral("2.8.0")}, {QStringLiteral("torchvision"), QStringLiteral("0.23.0")},
        {QStringLiteral("transformers"), QStringLiteral("4.57.1")}, {QStringLiteral("qwen-vl-utils"), QStringLiteral("0.0.14")},
        {QStringLiteral("accelerate"), QStringLiteral("1.11.0")}, {QStringLiteral("huggingface-hub"), QStringLiteral("0.36.0")},
        {QStringLiteral("safetensors"), QStringLiteral("0.6.2")}, {QStringLiteral("numpy"), QStringLiteral("2.2.6")},
        {QStringLiteral("pillow"), QStringLiteral("11.3.0")}
    };
    const auto actualDependencies = provenance.value(QStringLiteral("dependencies")).toObject();
    for (auto it = dependencies.cbegin(); it != dependencies.cend(); ++it)
        require(actualDependencies.value(it.key()).toString().section(QLatin1Char('+'), 0, 0) == it.value(),
            QStringLiteral("%1: oracle dependency %2 differs from its lock").arg(modelId, it.key()));
    struct Weight { QString name; qint64 bytes; QString sha256; };
    const QList<Weight> weights = small
        ? QList<Weight>{{QStringLiteral("model.safetensors"), 4255140312,
                         QStringLiteral("c73fa9caeddeb3ff831d46c085a7a5708343248ca777e90f2d486964464509c1")}}
        : QList<Weight>{
            {QStringLiteral("model-00001-of-00004.safetensors"), 4998056552, QStringLiteral("79ef275ec5f751d5fb59357c00d473268f9fd74abf5e38aa30137d268e7733c4")},
            {QStringLiteral("model-00002-of-00004.safetensors"), 4915962464, QStringLiteral("a4da61f512e84fc0f0b80bcb7bcc5137eb3bf25b658a7d55f84f4056078545f0")},
            {QStringLiteral("model-00003-of-00004.safetensors"), 4915962496, QStringLiteral("7fb17cf8f06d6fe5aaacf114e85c4e6d8318799f24b517f50d1ec154a8d47007")},
            {QStringLiteral("model-00004-of-00004.safetensors"), 1459698112, QStringLiteral("000213b6d1d03ed9023fac23716da51ec4c5be221a04526c2f732d31d8fed1f5")}
        };
    const auto actualWeights = provenance.value(QStringLiteral("weights")).toArray();
    require(actualWeights.size() == weights.size(), modelId + QStringLiteral(": oracle weight list differs from its lock"));
    for (qsizetype index = 0; index < weights.size(); ++index) {
        const auto actual = actualWeights[index].toObject();
        const auto& weight = weights[index];
        require(actual.value(QStringLiteral("file")).toString() == weight.name
                && actual.value(QStringLiteral("bytes")).toDouble(-1) == double(weight.bytes)
                && actual.value(QStringLiteral("sha256")).toString() == weight.sha256,
            QStringLiteral("%1: oracle weight %2 differs from its lock").arg(modelId, weight.name));
    }
}

void validateTokens(const QJsonObject& entry, const Input& input, const QString& label)
{
    const int context = input.textOnly ? 512 : 2048;
    const auto tokens = entry.value(QStringLiteral("tokenIds")).toArray();
    require(entry.value(QStringLiteral("contextLength")).toDouble(-1) == context
            && !tokens.isEmpty() && tokens.size() <= context,
        label + QStringLiteral(": token sequence must fit its declared application context"));
    for (const auto& value : tokens) {
        const double token = value.toDouble(-1);
        require(std::isfinite(token) && token >= 0 && token <= 2147483647 && token == std::floor(token),
            label + QStringLiteral(": invalid token ID"));
    }
    // Verified against the locked official tokenizers for both model sizes.
    const QList<int> suffix{151645, 198, 151644, 77091, 198, 151643};
    if (input.kind != QStringLiteral("raw")) {
        require(tokens.size() >= suffix.size(), label + QStringLiteral(": missing assistant suffix and terminal token"));
        for (qsizetype index = 0; index < suffix.size(); ++index)
            require(tokens[tokens.size() - suffix.size() + index].toInt(-1) == suffix[index],
                label + QStringLiteral(": assistant suffix or terminal token was changed or truncated"));
    }
    if (input.id == QStringLiteral("long-query"))
        require(entry.value(QStringLiteral("untruncatedTokenCount")).toDouble(-1) > 512 && tokens.size() == 512,
            label + QStringLiteral(": the long query must actually exercise 512-token truncation"));
}

void validateCoverage(const Manifest& manifest)
{
    const auto required = [&](const QString& id, const QString& kind, bool textOnly) -> const Input& {
        const auto it = std::find_if(manifest.inputs.cbegin(), manifest.inputs.cend(),
            [&](const Input& input) { return input.id == id; });
        require(it != manifest.inputs.cend() && it->kind == kind && it->textOnly == textOnly,
            QStringLiteral("%1: missing required %2 case with the correct kind/context").arg(manifest.modelId, id));
        return *it;
    };
    static const QRegularExpression chinese(QStringLiteral("[\\x{3400}-\\x{9FFF}]"));
    required(QStringLiteral("query-en"), QStringLiteral("query"), true);
    const auto& queryZh = required(QStringLiteral("query-zh"), QStringLiteral("query"), true);
    require(chinese.match(queryZh.text).hasMatch(), QStringLiteral("Chinese query coverage is missing"));
    const auto& newline = required(QStringLiteral("query-leading-newline"), QStringLiteral("query"), true);
    require(newline.text.startsWith(QLatin1Char('\n')), QStringLiteral("Leading-newline query coverage is missing"));
    required(QStringLiteral("long-query"), QStringLiteral("query"), true);
    required(QStringLiteral("document-en"), QStringLiteral("document"), false);
    const auto& documentZh = required(QStringLiteral("document-zh"), QStringLiteral("document"), false);
    require(chinese.match(documentZh.text).hasMatch(), QStringLiteral("Chinese document coverage is missing"));
    required(QStringLiteral("unrelated-en"), QStringLiteral("document"), false);
    QSet<int> imageBudgets;
    QSet<QString> imageFiles;
    for (const auto& input : manifest.inputs) {
        if (input.kind != QStringLiteral("image")) continue;
        imageBudgets.insert(input.budget);
        imageFiles.insert(QFileInfo(input.imagePath).fileName());
    }
    require(imageBudgets.contains(140) && imageBudgets.contains(280) && imageBudgets.contains(560),
        QStringLiteral("Image coverage must include 320, 640 and 1280 visual token budgets"));
    require(imageFiles.contains(QStringLiteral("photo.jpg")) && imageFiles.contains(QStringLiteral("portrait.png"))
            && imageFiles.contains(QStringLiteral("screen.png")),
        QStringLiteral("Image coverage must include landscape, portrait and screenshot fixtures"));
    for (const auto& language : {QStringLiteral("en"), QStringLiteral("zh")}) {
        const auto it = std::find_if(manifest.rankings.cbegin(), manifest.rankings.cend(), [&](const Ranking& ranking) {
            return ranking.query == QStringLiteral("query-") + language
                && ranking.preferred == QStringLiteral("document-") + language
                && ranking.other == QStringLiteral("unrelated-en");
        });
        require(it != manifest.rankings.cend(), language + QStringLiteral(": required semantic ranking is missing"));
    }
}

Manifest readManifest(const QString& fixtureRoot, const QString& modelId)
{
    Manifest result;
    result.modelId = modelId;
    result.dimensions = modelDimensions(modelId);
    const QString path = QDir(fixtureRoot).filePath(modelId + QStringLiteral(".json"));
    QJsonParseError parseError;
    const auto document = QJsonDocument::fromJson(readFile(path, 16 * 1024 * 1024), &parseError);
    require(parseError.error == QJsonParseError::NoError && document.isObject(),
        QStringLiteral("Invalid fixture JSON %1: %2").arg(path, parseError.errorString()));
    const auto object = document.object();
    require(object.value(QStringLiteral("schemaVersion")).toDouble(-1) == 1,
        path + QStringLiteral(": expected schemaVersion 1"));
    require(object.value(QStringLiteral("modelId")).toString() == modelId,
        path + QStringLiteral(": modelId does not match the requested model"));
    require(object.value(QStringLiteral("dimensions")).toDouble(-1) == result.dimensions,
        path + QStringLiteral(": wrong embedding dimensions"));
    const double fixedMinimum = result.dimensions == 2048 ? 0.995 : 0.97;
    result.minimumCosine = object.value(QStringLiteral("minimumCosine")).toDouble(-1);
    require(std::isfinite(result.minimumCosine) && result.minimumCosine >= fixedMinimum
            && result.minimumCosine <= 1.0,
        QStringLiteral("%1: minimumCosine must be at least %2 and no greater than 1")
            .arg(path).arg(fixedMinimum));
    const auto provenance = object.value(QStringLiteral("provenance"));
    require(provenance.isObject() && !provenance.toObject().isEmpty(),
        path + QStringLiteral(": independent oracle provenance is required"));
    validateProvenance(provenance.toObject(), modelId);
    const auto inputs = object.value(QStringLiteral("inputs"));
    require(inputs.isArray() && !inputs.toArray().isEmpty(), path + QStringLiteral(": inputs are required"));
    QMap<QString, QVector<float>> expected;
    QMap<QString, QString> kinds;
    bool hasPng = false, hasJpeg = false, hasTextOnly = false, hasFullText = false;
    for (const auto& value : inputs.toArray()) {
        require(value.isObject(), path + QStringLiteral(": every input must be an object"));
        const auto entry = value.toObject();
        Input input;
        input.id = entry.value(QStringLiteral("id")).toString();
        const QString label = modelId + QLatin1Char('/') + input.id;
        require(!input.id.isEmpty() && !expected.contains(input.id), label + QStringLiteral(": missing or duplicate input id"));
        input.kind = entry.value(QStringLiteral("kind")).toString();
        require(QSet<QString>{QStringLiteral("query"), QStringLiteral("code"), QStringLiteral("document"),
                    QStringLiteral("raw"), QStringLiteral("image")}.contains(input.kind),
            label + QStringLiteral(": unknown input kind"));
        require(entry.value(QStringLiteral("textOnly")).isBool(), label + QStringLiteral(": textOnly must be a boolean"));
        input.textOnly = entry.value(QStringLiteral("textOnly")).toBool();
        if (input.kind == QStringLiteral("image")) {
            require(!input.textOnly, label + QStringLiteral(": an image cannot use a text-only context"));
            const QString relative = entry.value(QStringLiteral("image")).toString();
            require(!relative.isEmpty() && QDir::isRelativePath(relative),
                label + QStringLiteral(": image must be relative to the fixture directory"));
            // Existing shared fixtures deliberately use ../photo.jpg and ../screen.png.
            input.imagePath = QFileInfo(QDir(fixtureRoot).filePath(relative)).absoluteFilePath();
            const auto encoded = readFile(input.imagePath, 256 * 1024 * 1024);
            verifyHash(entry.value(QStringLiteral("sha256")), encoded, label);
            const bool png = encoded.startsWith(QByteArray::fromHex("89504e470d0a1a0a"));
            const bool jpeg = encoded.startsWith(QByteArray::fromHex("ffd8ff"));
            require(png || jpeg, label + QStringLiteral(": image fixture must contain PNG or JPEG data"));
            hasPng = hasPng || png;
            hasJpeg = hasJpeg || jpeg;
            input.budget = entry.value(QStringLiteral("budget")).toInt();
            require(entry.value(QStringLiteral("budget")).toDouble(-1) == input.budget
                    && ImageSizing::qwenTokenBudget(input.budget) > 0,
                label + QStringLiteral(": invalid application image budget"));
        } else {
            require(entry.value(QStringLiteral("text")).isString(), label + QStringLiteral(": text must be a string"));
            input.text = entry.value(QStringLiteral("text")).toString();
            require(!entry.contains(QStringLiteral("title")) || entry.value(QStringLiteral("title")).isString(),
                label + QStringLiteral(": title must be a string"));
            input.title = entry.value(QStringLiteral("title")).toString();
            verifyHash(entry.value(QStringLiteral("sha256")), input.text.toUtf8(), label);
            hasTextOnly = hasTextOnly || input.textOnly;
            hasFullText = hasFullText || !input.textOnly;
        }
        require(entry.value(QStringLiteral("vector")).isArray(), label + QStringLiteral(": vector must be an array"));
        for (const auto& number : entry.value(QStringLiteral("vector")).toArray()) {
            require(number.isDouble() && std::isfinite(number.toDouble()), label + QStringLiteral(": invalid oracle vector value"));
            input.expected.append(float(number.toDouble()));
        }
        validateVector(input.expected, result.dimensions, label);
        validateTokens(entry, input, label);
        expected.insert(input.id, input.expected);
        kinds.insert(input.id, input.kind);
        result.inputs.append(input);
    }
    require(hasPng && hasJpeg, path + QStringLiteral(": both PNG and JPEG fixtures are required"));
    require(hasTextOnly && hasFullText, path + QStringLiteral(": text fixtures must cover both context modes"));
    const auto rankings = object.value(QStringLiteral("rankings"));
    require(rankings.isArray() && !rankings.toArray().isEmpty(), path + QStringLiteral(": semantic rankings are required"));
    for (const auto& value : rankings.toArray()) {
        require(value.isObject(), path + QStringLiteral(": every ranking must be an object"));
        const auto entry = value.toObject();
        const Ranking ranking{entry.value(QStringLiteral("query")).toString(),
            entry.value(QStringLiteral("preferred")).toString(), entry.value(QStringLiteral("other")).toString()};
        require(expected.contains(ranking.query) && expected.contains(ranking.preferred) && expected.contains(ranking.other)
                && ranking.query != ranking.preferred && ranking.query != ranking.other && ranking.preferred != ranking.other
                && kinds.value(ranking.query) != QStringLiteral("image"),
            path + QStringLiteral(": ranking must refer to three distinct inputs and a text query"));
        require(cosine(expected.value(ranking.query), expected.value(ranking.preferred))
                > cosine(expected.value(ranking.query), expected.value(ranking.other)),
            path + QStringLiteral(": oracle vectors do not satisfy their declared ranking"));
        result.rankings.append(ranking);
    }
    validateCoverage(result);
    qInfo().noquote() << modelId << "manifest verified:" << result.inputs.size() << "inputs,"
                     << result.rankings.size() << "rankings, minimum cosine" << result.minimumCosine;
    return result;
}

PromptKind promptKind(const QString& kind)
{
    if (kind == QStringLiteral("query")) return PromptKind::Query;
    if (kind == QStringLiteral("code")) return PromptKind::CodeQuery;
    if (kind == QStringLiteral("document")) return PromptKind::Document;
    return PromptKind::Raw;
}

QVector<float> embed(EmbeddingRuntime& runtime, const Input& input, QString* error)
{
    return input.kind == QStringLiteral("image") ? runtime.embedImage(input.imagePath, input.budget, error)
        : runtime.embedText(input.text, promptKind(input.kind), input.title, error);
}

QVector<float> checkedEmbedding(EmbeddingRuntime& runtime, const Input& input, const Manifest& manifest)
{
    QString error;
    const auto vector = embed(runtime, input, &error);
    require(!vector.isEmpty() && error.isEmpty(),
        QStringLiteral("%1/%2: embedding failed: %3").arg(manifest.modelId, input.id, error));
    validateVector(vector, manifest.dimensions, input.id);
    const double agreement = cosine(vector, input.expected);
    qInfo().noquote() << manifest.modelId << input.id << "oracle cosine" << QString::number(agreement, 'g', 12);
    require(agreement >= manifest.minimumCosine,
        QStringLiteral("%1/%2: oracle cosine %3 is below %4. Do not lower the threshold or regenerate the oracle from the runtime.")
            .arg(manifest.modelId, input.id).arg(agreement, 0, 'g', 12).arg(manifest.minimumCosine));
    return vector;
}

void requireSame(const QVector<float>& first, const QVector<float>& second, const QString& label)
{
    const double agreement = cosine(first, second);
    require(agreement >= 0.999999,
        QStringLiteral("%1: repeated input changed, cosine=%2").arg(label).arg(agreement, 0, 'g', 12));
}

void runModel(const Manifest& manifest, const QString& modelRoot, const QString& temporaryRoot)
{
    QMap<QString, QVector<float>> actual;
    // Each context is loaded once and released before the next, including on failure.
    for (bool textOnly : {true, false}) {
        EmbeddingRuntime runtime;
        QString error;
        const bool loaded = runtime.load(modelRoot, manifest.modelId, textOnly, &error);
        require(loaded,
            QStringLiteral("Cannot load %1 (%2): %3").arg(manifest.modelId,
                textOnly ? QStringLiteral("text-only") : QStringLiteral("multimodal"), error));
        require(runtime.dimensions() == manifest.dimensions && runtime.modelId() == manifest.modelId
                && runtime.isTextOnly() == textOnly && !runtime.modelFingerprint().isEmpty(),
            QStringLiteral("Loaded runtime identity does not match the fixture"));
        require(runtime.capabilities().value(QStringLiteral("image")).toBool()
                && !runtime.capabilities().value(QStringLiteral("audio")).toBool(),
            QStringLiteral("Qwen must declare image support and no audio support"));
        const Input* firstText = nullptr;
        const Input* firstImage = nullptr;
        for (const auto& input : manifest.inputs) {
            if (input.textOnly != textOnly) continue;
            actual.insert(input.id, checkedEmbedding(runtime, input, manifest));
            if (input.kind == QStringLiteral("image")) {
                if (!firstImage) firstImage = &input;
            } else if (!firstText) {
                firstText = &input;
            }
        }
        require(firstText != nullptr, QStringLiteral("No text fixture for runtime context"));
        const auto repeatedText = checkedEmbedding(runtime, *firstText, manifest);
        requireSame(actual.value(firstText->id), repeatedText, QStringLiteral("repeated text"));
        if (!textOnly) {
            require(firstImage != nullptr, QStringLiteral("No image fixture for multimodal context"));
            const auto repeatedImage = checkedEmbedding(runtime, *firstImage, manifest);
            requireSame(actual.value(firstImage->id), repeatedImage, QStringLiteral("repeated image"));
            // The preceding repeatedText, this image and the following text form A -> image -> A.
            requireSame(repeatedText, checkedEmbedding(runtime, *firstText, manifest),
                QStringLiteral("text-image-text KV isolation"));
            error.clear();
            const auto audio = runtime.embedAudio(QDir(temporaryRoot).filePath(QStringLiteral("unsupported.wav")), &error);
            require(audio.isEmpty() && error.contains(QStringLiteral("audio"), Qt::CaseInsensitive)
                    && error.contains(QStringLiteral("support"), Qt::CaseInsensitive),
                QStringLiteral("Qwen audio must fail explicitly as unsupported: %1").arg(error));
            requireSame(repeatedText, checkedEmbedding(runtime, *firstText, manifest),
                QStringLiteral("recovery after unsupported audio"));
        }
        // Request cancellation after entering the encoding path. Backends that
        // do not poll during compute may observe it immediately after decoding.
        std::atomic<int> cancellationChecks{0};
        runtime.setCancellationCallback([&] { return cancellationChecks.fetch_add(1) >= 2; });
        error.clear();
        const auto cancelled = embed(runtime, *firstText, &error);
        runtime.setCancellationCallback({});
        require(cancelled.isEmpty() && !error.isEmpty() && cancellationChecks.load() >= 3,
            QStringLiteral("Cancellation must discard the output and report an error"));
        require(!runtime.diagnostics().value(QStringLiteral("deviceFailure")).toBool(),
            QStringLiteral("Cancellation must not mark the device as failed"));
        requireSame(repeatedText, checkedEmbedding(runtime, *firstText, manifest),
            QStringLiteral("recovery after cancellation"));
        runtime.close();
        require(!runtime.isLoaded(), QStringLiteral("Runtime remained loaded after close"));
    }
    for (const auto& ranking : manifest.rankings) {
        const double preferred = cosine(actual.value(ranking.query), actual.value(ranking.preferred));
        const double other = cosine(actual.value(ranking.query), actual.value(ranking.other));
        qInfo().noquote() << manifest.modelId << ranking.query << "ranking:" << ranking.preferred << preferred
                         << '>' << ranking.other << other;
        require(preferred > other, QStringLiteral("%1: expected %2 to rank above %3")
            .arg(ranking.query, ranking.preferred, ranking.other));
    }
    qInfo().noquote() << manifest.modelId << "real-model oracle, repetition, KV isolation and cancellation checks passed";
}
} // namespace

int main(int argc, char** argv)
{
    // Windows otherwise routes Qt diagnostics to the debugger when this
    // standalone acceptance tool has redirected output and no console.
    qputenv("QT_FORCE_STDERR_LOGGING", "1");
    QCoreApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-tests-qwen-vectors"));
    QStandardPaths::setTestModeEnabled(true);
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Validate independent Qwen oracle fixtures and optionally run real inference."));
    parser.addHelpOption();
    parser.addOption({QStringLiteral("validate-only"), QStringLiteral("Check manifests and fixtures without loading model weights.")});
    parser.addOption({QStringLiteral("model"), QStringLiteral("Qwen preset ID. Required for real inference."), QStringLiteral("id")});
    parser.addOption({QStringLiteral("model-root"), QStringLiteral("Model root containing the selected preset directory."), QStringLiteral("directory")});
    parser.addOption({QStringLiteral("fixtures"), QStringLiteral("Directory containing the oracle manifests."), QStringLiteral("directory"),
        QString::fromUtf8(QCOPILOTS_SEARCH_QWEN_FIXTURES)});
    parser.process(application);
    test::TestEnvironment environment(QStringLiteral("qwen-vectors"));
    int status = 0;
    try {
        require(environment.isValid(), environment.error());
        require(parser.positionalArguments().isEmpty(), QStringLiteral("Unexpected positional arguments"));
        const bool validateOnly = parser.isSet(QStringLiteral("validate-only"));
        const QString selected = parser.value(QStringLiteral("model"));
        require(validateOnly || (!selected.isEmpty() && !parser.value(QStringLiteral("model-root")).isEmpty()),
            QStringLiteral("Real-model inference requires --model and --model-root. Missing weights never count as a pass."));
        const QStringList modelIds = selected.isEmpty()
            ? QStringList{QStringLiteral("qwen3-vl-embedding-2b"), QStringLiteral("qwen3-vl-embedding-8b")}
            : QStringList{selected};
        const QString fixtureRoot = QFileInfo(parser.value(QStringLiteral("fixtures"))).absoluteFilePath();
        for (const auto& modelId : modelIds) {
            const auto manifest = readManifest(fixtureRoot, modelId);
            if (!validateOnly)
                runModel(manifest, QFileInfo(parser.value(QStringLiteral("model-root"))).absoluteFilePath(), environment.root());
        }
        qInfo().noquote() << (validateOnly ? "Qwen manifests passed. No model inference was performed."
                                         : "Qwen real-model verification passed.");
    } catch (const std::exception& error) {
        qCritical().noquote() << "Qwen verification failed:" << QString::fromUtf8(error.what());
        status = 1;
    }
    QString cleanupError;
    if (!environment.cleanup(&cleanupError)) {
        qCritical().noquote() << cleanupError;
        status = 1;
    }
    cleanupError = test::takeCleanupErrors();
    if (!cleanupError.isEmpty()) {
        qCritical().noquote() << cleanupError;
        status = 1;
    }
    return status;
}
