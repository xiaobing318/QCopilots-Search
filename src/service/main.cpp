#include "service_server.h"
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QJsonDocument>
#include <QTextStream>

int main(int argc, char **argv)
{
    qputenv("QT_COMMAND_LINE_PARSER_NO_GUI_MESSAGE_BOXES", "1");
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-service"));
    QCoreApplication::setOrganizationName(QStringLiteral("qcopilots-search"));
    QCoreApplication::setApplicationVersion(QStringLiteral(QCOPILOTS_SEARCH_VERSION));
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("QCopilots Search local indexing and search service"));
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addOption({QStringLiteral("profile-dir"), QStringLiteral("Application data base directory (uses profiles/v4 below it)"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("model-dir"), QStringLiteral("Verified model directory"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("index-device"), QStringLiteral("Index GPU selector JSON with backend, deviceId and name. Repeat to select multiple GPUs. Default uses all compatible GPUs"), QStringLiteral("json")});
    parser.process(app);
    qcopilots_search::ServiceDependencies dependencies;
    for (const auto &text : parser.values(QStringLiteral("index-device"))) {
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(text.toUtf8(), &parse);
        const auto selector = document.object();
        if (parse.error != QJsonParseError::NoError || !document.isObject()
            || selector.value("backend").toString().trimmed().isEmpty()
            || (selector.value("deviceId").toString().isEmpty() && selector.value("name").toString().isEmpty())) {
            QTextStream(stderr) << "--index-device requires a JSON object with backend and deviceId or a unique name" << Qt::endl;
            return 1;
        }
        dependencies.indexDevices.append(selector);
    }
    qcopilots_search::ServiceServer server(qcopilots_search::RuntimePaths::resolve(
        parser.value(QStringLiteral("profile-dir")), parser.value(QStringLiteral("model-dir"))), std::move(dependencies));
    QString error;
    if (!server.start(&error)) {
        QTextStream(stderr) << error << Qt::endl;
        return error.startsWith(QStringLiteral("already_running:")) ? 0 : 1;
    }
    QObject::connect(&app, &QCoreApplication::aboutToQuit, &server, &qcopilots_search::ServiceServer::stop);
    return app.exec();
}
