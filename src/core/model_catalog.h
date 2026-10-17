#pragma once

#include "model_files.h"
#include <QJsonObject>

namespace qcopilots_search {

struct ModelDescriptor {
    QString id;
    QString name;
    QString repository;
    QString revision;
    QString adapter;
    QString textFile;
    QString mediaFile;
    int dimensions = 0;
    bool supportsVision = false;
    bool supportsAudio = false;
    QList<ModelArtifact> artifacts;
    QJsonObject toJson() const;
};

QString defaultModelId();
const QList<ModelDescriptor> &modelCatalog();
const ModelDescriptor *findModel(const QString &id);
// The default model keeps its original flat cache layout. Unknown IDs fail closed.
QString modelDirectory(const QString &root, const QString &id);
QJsonObject modelPipelineContract(const QString &id);
QString modelPipelineFingerprint(const QString &id);

} // namespace qcopilots_search
