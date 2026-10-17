#include "model_catalog.h"
#include "pipeline_manifest.h"

#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QResource>
#include <QSet>
#include <stdexcept>

static void initializeModelResources()
{
    Q_INIT_RESOURCE(qcs_models);
}

namespace qcopilots_search {

QString defaultModelId() { return QStringLiteral("embeddinggemma-2"); }

const QList<ModelDescriptor> &modelCatalog()
{
    static const QList<ModelDescriptor> catalog = [] {
        initializeModelResources();
        QFile file(QStringLiteral(":/qcs/qcs-models.lock.json"));
        if (!file.open(QIODevice::ReadOnly))
            throw std::logic_error("Embedded model catalog is missing");
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(file.readAll(), &parse);
        const auto root = document.object();
        if (parse.error != QJsonParseError::NoError || root.value("schemaVersion").toInt() != 1
            || root.value("defaultModelId").toString() != defaultModelId())
            throw std::logic_error("Invalid embedded model catalog");
        const QRegularExpression idPattern(QStringLiteral("^[a-z0-9]+(?:-[a-z0-9]+)*$"));
        const QRegularExpression hashPattern(QStringLiteral("^[0-9a-f]{64}$"));
        const QRegularExpression revisionPattern(QStringLiteral("^[0-9a-f]{40}$"));
        QList<ModelDescriptor> models;
        QSet<QString> ids;
        for (const auto &value : root.value("models").toArray()) {
            const auto object = value.toObject();
            ModelDescriptor model;
            model.id = object.value("id").toString();
            model.name = object.value("name").toString();
            model.repository = object.value("repository").toString();
            model.revision = object.value("revision").toString();
            model.adapter = object.value("adapter").toString();
            model.textFile = object.value("textFile").toString();
            model.mediaFile = object.value("mediaFile").toString();
            model.dimensions = object.value("dimensions").toInt();
            model.supportsVision = object.value("supportsVision").toBool();
            model.supportsAudio = object.value("supportsAudio").toBool();
            if (!idPattern.match(model.id).hasMatch() || ids.contains(model.id)
                || model.name.isEmpty() || !revisionPattern.match(model.revision).hasMatch()
                || model.dimensions <= 0 || model.dimensions > 4096
                || (model.adapter != "embeddinggemma-2" && model.adapter != "qwen3-vl-embedding"))
                throw std::logic_error("Invalid embedded model descriptor");
            QSet<QString> names;
            for (const auto &entry : object.value("artifacts").toArray()) {
                const auto artifact = entry.toObject();
                ModelArtifact item{artifact.value("name").toString(), QUrl(artifact.value("url").toString()),
                                   artifact.value("size").toInteger(), artifact.value("sha256").toString().toLatin1()};
                if (item.name.isEmpty() || item.name.contains('/') || item.name.contains('\\')
                    || !item.name.endsWith(".gguf") || names.contains(item.name)
                    || item.size <= 0 || !hashPattern.match(QString::fromLatin1(item.sha256)).hasMatch()
                    || item.url.scheme() != "https"
                    || !item.url.toString().startsWith("https://huggingface.co/" + model.repository
                                                      + "/resolve/" + model.revision + "/"))
                    throw std::logic_error("Invalid embedded model artifact");
                names.insert(item.name);
                model.artifacts.append(item);
            }
            if (model.artifacts.size() != 2 || !names.contains(model.textFile) || !names.contains(model.mediaFile)
                || model.textFile == model.mediaFile)
                throw std::logic_error("Incomplete model artifact pair");
            ids.insert(model.id);
            models.append(model);
        }
        if (models.isEmpty() || !ids.contains(defaultModelId()))
            throw std::logic_error("Incomplete embedded model catalog");
        return models;
    }();
    return catalog;
}

const ModelDescriptor *findModel(const QString &id)
{
    for (const auto &model : modelCatalog()) if (model.id == id) return &model;
    return nullptr;
}

QString modelDirectory(const QString &root, const QString &id)
{
    if (root.trimmed().isEmpty() || !findModel(id)) return {};
    return id == defaultModelId() ? QDir(root).absolutePath() : QDir(root).absoluteFilePath(id);
}

QJsonObject ModelDescriptor::toJson() const
{
    QJsonArray files;
    qint64 total = 0;
    for (const auto &artifact : artifacts) {
        files.append(QJsonObject{{"name", artifact.name}, {"url", artifact.url.toString()},
                                 {"size", artifact.size}, {"sha256", QString::fromLatin1(artifact.sha256)}});
        total += artifact.size;
    }
    return {{"id", id}, {"name", name}, {"repository", repository}, {"revision", revision},
            {"adapter", adapter}, {"textFile", textFile}, {"mediaFile", mediaFile},
            {"dimensions", dimensions}, {"supportsVision", supportsVision}, {"supportsAudio", supportsAudio},
            {"artifacts", files}, {"totalBytes", total}, {"default", id == defaultModelId()},
            {"capabilities", QJsonObject{{"text", true}, {"image", supportsVision},
                                         {"videoFrames", supportsVision}, {"audio", supportsAudio}}}};
}

QJsonObject modelPipelineContract(const QString &id)
{
    const auto *model = findModel(id);
    if (!model) return {};
    if (id == defaultModelId()) return embeddingPipelineContract();
    return {{"revision", 1}, {"model", model->id}, {"modelRevision", model->revision},
            {"artifacts", model->toJson().value("artifacts")},
            {"llamaCpp", QStringLiteral("620d2225cc436ab0b001e3200e590e3b286ef9ab")},
            {"adapter", model->adapter}, {"dimensions", model->dimensions},
            {"instruction", QStringLiteral("Represent the user's input.")},
            {"template", QStringLiteral("ChatML-system-user-assistant-generation-prefix,last-token-tokenizer-postprocessor-endoftext")},
            {"tokenization", QStringLiteral("UTF-8,special-tokens,truncate-user-reserving-template-suffix-and-endoftext")},
            {"context", QStringLiteral("text-query=512,full-multimodal=2048,batch=256,ubatch=256,single-sequence")},
            {"output", QStringLiteral("decoder,last-pooling,L2-normalized,full-dimension")},
            {"image", QStringLiteral("Qwen-smart-resize-grid=32,ties-to-even,Pillow-bicubic-RGB,budgets=160/320/640/1280/1280,min=4,max=1280,mRoPE,deepstack")},
            {"audio", QStringLiteral("unsupported,keyword-only,video-frames-only")}};
}

QString modelPipelineFingerprint(const QString &id)
{
    if (!findModel(id)) return {};
    if (id == defaultModelId()) return embeddingPipelineFingerprint();
    return pipelineContractFingerprint(QStringLiteral("qcs-embedding-v2"), modelPipelineContract(id));
}

} // namespace qcopilots_search
