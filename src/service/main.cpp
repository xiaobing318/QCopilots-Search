#include "service_server.h"
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QTextStream>

int main(int argc, char **argv)
{
    qputenv("QT_COMMAND_LINE_PARSER_NO_GUI_MESSAGE_BOXES", "1");
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcopilots-search-service"));
    QCoreApplication::setOrganizationName(QStringLiteral("qcopilots-search"));
    QCoreApplication::setApplicationVersion(QStringLiteral("0.1.0"));
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("QCopilots Search local indexing and search service"));
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addOption({QStringLiteral("profile-dir"), QStringLiteral("Isolated application profile directory"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("model-dir"), QStringLiteral("Verified model directory"), QStringLiteral("path")});
    parser.process(app);
    qcopilots_search::ServiceServer server(qcopilots_search::RuntimePaths::resolve(
        parser.value(QStringLiteral("profile-dir")), parser.value(QStringLiteral("model-dir"))));
    QString error;
    if (!server.start(&error)) {
        QTextStream(stderr) << error << Qt::endl;
        return error.startsWith(QStringLiteral("already_running:")) ? 0 : 1;
    }
    QObject::connect(&app, &QCoreApplication::aboutToQuit, &server, &qcopilots_search::ServiceServer::stop);
    return app.exec();
}
