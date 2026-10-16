#include "core/model_files.h"
#include "core/runtime_paths.h"
#include "core/types.h"
#include "service/service_client.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDir>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTextStream>
#include <QTimer>
#include <QVersionNumber>
#include <cstdio>

using namespace qcopilots_search;

namespace {
struct Outcome {
    QJsonObject result;
    QString code;
    QString message;
    QJsonObject details;
    bool ok() const { return code.isEmpty(); }
};

Outcome failure(const QString &code, const QString &message, const QJsonObject &details = {})
{
    return {{}, code, message, details};
}

int printOutcome(const Outcome &value, bool pretty)
{
    QJsonObject envelope{{QStringLiteral("ok"), value.ok()}};
    if (value.ok())
        envelope.insert(QStringLiteral("result"), value.result);
    else {
        QJsonObject error{{QStringLiteral("code"), value.code}, {QStringLiteral("message"), value.message}};
        if (!value.details.isEmpty()) error.insert(QStringLiteral("details"), value.details);
        envelope.insert(QStringLiteral("error"), error);
    }
    const QByteArray bytes = QJsonDocument(envelope).toJson(pretty ? QJsonDocument::Indented : QJsonDocument::Compact);
    // stdout contains exactly one JSON document, including on errors.
    fwrite(bytes.constData(), 1, size_t(bytes.size()), stdout);
    if (!bytes.endsWith('\n'))
        fputc('\n', stdout);
    fflush(stdout);
    return value.ok() ? 0 : (value.code == QStringLiteral("usage") ? 2 : 1);
}

Outcome call(ServiceClient &client, const QString &method, const QJsonObject &params, int timeout)
{
    QEventLoop loop;
    QTimer deadline;
    deadline.setSingleShot(true);
    Outcome result;
    QString pendingId;
    bool completed = false;
    const auto response = QObject::connect(&client, &ServiceClient::response, &loop,
        [&](const QString &id, const QJsonObject &object) {
            if (id != pendingId)
                return;
            result.result = object;
            completed = true;
            loop.quit();
        });
    const auto failed = QObject::connect(&client, &ServiceClient::failed, &loop,
        [&](const QString &id, const QString &code, const QString &message) {
            if (!id.isEmpty() && id != pendingId)
                return;
            result = failure(code, message);
            completed = true;
            loop.quit();
        });
    QObject::connect(&deadline, &QTimer::timeout, &loop, [&] {
        client.cancel(pendingId);
        result = failure(QStringLiteral("timeout"), QStringLiteral("The service request exceeded the configured timeout"));
        completed = true;
        loop.quit();
    });
    pendingId = client.request(method, params, timeout);
    deadline.start(timeout);
    if (!completed)
        loop.exec();
    QObject::disconnect(response);
    QObject::disconnect(failed);
    return result;
}

Outcome checkUpdate(int timeout)
{
    QNetworkAccessManager network;
    QNetworkRequest request(QUrl(QStringLiteral("https://api.github.com/repos/xiaobing318/QCopilots-Search/releases/latest")));
    request.setRawHeader("Accept", "application/vnd.github+json");
    request.setRawHeader("User-Agent", "qcopilots-search-cli");
    request.setTransferTimeout(timeout);
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    auto *reply = network.get(request);
    QEventLoop loop;
    QTimer timer;
    timer.setSingleShot(true);
    QByteArray bytes;
    bool tooLarge = false;
    bool timedOut = false;
    QObject::connect(reply, &QNetworkReply::readyRead, &loop, [&] {
        bytes += reply->readAll();
        if (bytes.size() > 2 * 1024 * 1024) {
            tooLarge = true;
            reply->abort();
        }
    });
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QObject::connect(&timer, &QTimer::timeout, &loop, [&] { timedOut = true; reply->abort(); });
    timer.start(timeout);
    if (!reply->isFinished())
        loop.exec();
    if (timedOut)
        return failure(QStringLiteral("timeout"), QStringLiteral("Update check timed out"));
    if (tooLarge)
        return failure(QStringLiteral("invalid_response"), QStringLiteral("Update response exceeds 2 MiB"));
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    if (status == 404)
        return {{{QStringLiteral("available"), false}, {QStringLiteral("reason"), QStringLiteral("No public release is available")},
                 {QStringLiteral("currentVersion"), QCoreApplication::applicationVersion()}}, {}, {}};
    if (reply->error() != QNetworkReply::NoError)
        return failure(QStringLiteral("network_error"), reply->errorString());
    bytes += reply->readAll();
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(bytes, &error);
    if (!document.isObject() || error.error != QJsonParseError::NoError)
        return failure(QStringLiteral("invalid_response"), QStringLiteral("The release server returned invalid JSON"));
    const auto object = document.object();
    QString tag = object.value(QStringLiteral("tag_name")).toString();
    if (tag.startsWith(u'v'))
        tag.remove(0, 1);
    const auto latest = QVersionNumber::fromString(tag);
    const auto current = QVersionNumber::fromString(QCoreApplication::applicationVersion());
    const QUrl url(object.value(QStringLiteral("html_url")).toString());
    if (latest.isNull() || current.isNull() || url.scheme() != QStringLiteral("https")
        || url.host() != QStringLiteral("github.com"))
        return failure(QStringLiteral("invalid_response"), QStringLiteral("The release server returned invalid version or download information"));
    return {{{QStringLiteral("available"), latest > current}, {QStringLiteral("currentVersion"), current.toString()},
             {QStringLiteral("latestVersion"), latest.toString()}, {QStringLiteral("downloadUrl"), url.toString()}}, {}, {}};
}

Outcome modelOperation(const QStringList &arguments, const QCommandLineParser &parser,
                       const RuntimePaths &paths, int timeout)
{
    const QString action = arguments.value(0, QStringLiteral("status"));
    ModelFiles files(paths.modelDir);
    QString error;
    if (action == QStringLiteral("status") || action == QStringLiteral("verify")) {
        if (arguments.size() > 1)
            return failure(QStringLiteral("usage"), QStringLiteral("model status/verify does not accept extra arguments"));
        const bool valid = files.verify(&error);
        QJsonArray artifacts;
        for (const auto &artifact : ModelFiles::manifest()) {
            const QFileInfo info(QDir(paths.modelDir).filePath(artifact.name));
            artifacts.append(QJsonObject{{QStringLiteral("name"), artifact.name}, {QStringLiteral("present"), info.isFile()},
                                         {QStringLiteral("bytes"), info.size()}, {QStringLiteral("expectedBytes"), artifact.size},
                                         {QStringLiteral("sha256"), QString::fromLatin1(artifact.sha256)}});
        }
        if (action == QStringLiteral("verify") && !valid)
            return failure(QStringLiteral("model_invalid"), error);
        return {{{QStringLiteral("directory"), paths.modelDir}, {QStringLiteral("valid"), valid},
                 {QStringLiteral("artifacts"), artifacts}, {QStringLiteral("detail"), error}}, {}, {}};
    }
    if (action == QStringLiteral("import")) {
        if (arguments.size() != 2)
            return failure(QStringLiteral("usage"), QStringLiteral("model import requires one file or directory"));
        const QFileInfo source(arguments.at(1));
        QJsonArray imported;
        if (source.isDir()) {
            if (parser.isSet(QStringLiteral("name")))
                return failure(QStringLiteral("usage"), QStringLiteral("--name applies only when importing a file"));
            for (const auto &artifact : ModelFiles::manifest()) {
                if (!files.importFile(QDir(source.absoluteFilePath()).filePath(artifact.name), artifact.name, &error))
                    return failure(QStringLiteral("model_import_failed"), error);
                imported.append(artifact.name);
            }
        } else {
            const QString name = parser.isSet(QStringLiteral("name")) ? parser.value(QStringLiteral("name")) : source.fileName();
            if (!files.importFile(source.absoluteFilePath(), name, &error))
                return failure(QStringLiteral("model_import_failed"), error);
            imported.append(name);
        }
        const bool complete = files.verify(&error);
        return {{{QStringLiteral("imported"), imported}, {QStringLiteral("complete"), complete},
                 {QStringLiteral("directory"), paths.modelDir}}, {}, {}};
    }
    if (action == QStringLiteral("download")) {
        if (arguments.size() != 1)
            return failure(QStringLiteral("usage"), QStringLiteral("model download does not accept extra arguments"));
        QEventLoop loop;
        QTimer timer;
        timer.setSingleShot(true);
        bool completed = false;
        bool success = false;
        bool timedOut = false;
        QObject::connect(&files, &ModelFiles::finished, &loop, [&](bool ok, const QString &detail) {
            completed = true;
            success = ok;
            error = detail;
            loop.quit();
        });
        QObject::connect(&files, &ModelFiles::progress, &loop, [&](qint64 received, qint64 total) {
            if (parser.isSet(QStringLiteral("progress"))) {
                const auto bytes = QJsonDocument(QJsonObject{{QStringLiteral("received"), received},
                                                            {QStringLiteral("total"), total}}).toJson(QJsonDocument::Compact);
                fwrite(bytes.constData(), 1, size_t(bytes.size()), stderr);
                fputc('\n', stderr);
                fflush(stderr);
            }
        });
        QObject::connect(&timer, &QTimer::timeout, &loop, [&] { timedOut = true; files.cancel(); });
        timer.start(timeout);
        files.downloadMissing();
        if (!completed)
            loop.exec();
        if (!success)
            return failure(timedOut ? QStringLiteral("timeout") : QStringLiteral("model_download_failed"), error);
        return {{{QStringLiteral("complete"), true}, {QStringLiteral("directory"), paths.modelDir}}, {}, {}};
    }
    return failure(QStringLiteral("usage"), QStringLiteral("Unknown model operation: %1").arg(action));
}
} // namespace

int main(int argc, char **argv)
{
    qputenv("QT_COMMAND_LINE_PARSER_NO_GUI_MESSAGE_BOXES", "1");
    QCoreApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-cli"));
    QCoreApplication::setOrganizationName(QStringLiteral("QCopilots"));
    QCoreApplication::setApplicationVersion(QStringLiteral(QCOPILOTS_SEARCH_VERSION));
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral(
        "QCopilots Search command line client. Results are JSON.\n\n"
        "Commands:\n"
        "  status                         Service and index status\n"
        "  search QUERY                   Search selected sources\n"
        "  index [DIRECTORY ...]          Save optional source roots and start indexing\n"
        "  pause | resume | retry | reconcile\n"
        "  config [get | set JSON]        Read or merge settings\n"
        "  model [manifest | status | verify | download | reload]\n"
        "  model import FILE_OR_DIRECTORY Import and verify locked GGUF artifacts\n"
        "  import FILE_OR_DIRECTORY       Alias for model import\n"
        "  update                         Check public releases without installing\n"
        "  shutdown                       Stop this profile's service\n\n"
        "Examples:\n"
        "  qcs-cli --profile-dir \"profile space\" index \"documents\" --wait\n"
        "  qcs-cli search \"中文 查询\" --kind pdf --limit 20\n"
        "  qcs-cli model download --timeout 1800000 --progress\n"
        "  qcs-cli config set --file settings.json\n\n"
        "Exit codes: 0 success, 1 operation failed, 2 invalid arguments.\n"
        "The service is discovered beside this executable (or in its macOS bundle)."));
    parser.addHelpOption();
    parser.addVersionOption();
    parser.addPositionalArgument(QStringLiteral("command"), QStringLiteral("Command listed above"));
    parser.addPositionalArgument(QStringLiteral("arguments"), QStringLiteral("Command arguments"), QStringLiteral("[arguments...]"));
    parser.addOptions({
        {{QStringLiteral("profile-dir")}, QStringLiteral("Explicit isolated profile directory"), QStringLiteral("path")},
        {{QStringLiteral("model-dir")}, QStringLiteral("Override shared model directory"), QStringLiteral("path")},
        {{QStringLiteral("timeout")}, QStringLiteral("Operation timeout in milliseconds (1..86400000)"), QStringLiteral("ms"), QStringLiteral("30000")},
        {{QStringLiteral("pretty")}, QStringLiteral("Indent JSON output")},
        {{QStringLiteral("json")}, QStringLiteral("JSON output (the default)")},
        {{QStringLiteral("no-start")}, QStringLiteral("Connect only, without starting a service")},
        {{QStringLiteral("kind")}, QStringLiteral("Search file kind filter"), QStringLiteral("kind")},
        {{QStringLiteral("root")}, QStringLiteral("Search root filter, or source root when indexing/configuring. Repeatable."), QStringLiteral("path")},
        {{QStringLiteral("exclude")}, QStringLiteral("Excluded directory when indexing/configuring. Repeatable."), QStringLiteral("path")},
        {{QStringLiteral("code")}, QStringLiteral("Use the independent code space for search, roots and excludes")},
        {{QStringLiteral("keyword-only")}, QStringLiteral("Do not use the semantic query encoder")},
        {{QStringLiteral("limit")}, QStringLiteral("Maximum search results (1..%1)").arg(maximumSearchResults), QStringLiteral("count"), QStringLiteral("100")},
        {{QStringLiteral("wait")}, QStringLiteral("Wait for indexing completion, bounded by --timeout")},
        {{QStringLiteral("file")}, QStringLiteral("Read config set JSON from a UTF-8 file"), QStringLiteral("path")},
        {{QStringLiteral("name")}, QStringLiteral("Locked artifact name for model file import"), QStringLiteral("name")},
        {{QStringLiteral("progress")}, QStringLiteral("Write model download progress as JSON lines to stderr")}
    });
    const bool parsed = parser.parse(application.arguments());
    const bool pretty = parser.isSet(QStringLiteral("pretty"));
    if (!parsed)
        return printOutcome(failure(QStringLiteral("usage"), parser.errorText()), pretty);
    if (parser.isSet(QStringLiteral("help")))
        parser.showHelp(0);
    if (parser.isSet(QStringLiteral("version")))
        parser.showVersion();
    QStringList arguments = parser.positionalArguments();
    if (arguments.isEmpty())
        return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("A command is required. Use --help for usage.")), pretty);
    const QString command = arguments.takeFirst();
    const QStringList commands{QStringLiteral("status"), QStringLiteral("search"), QStringLiteral("index"),
        QStringLiteral("pause"), QStringLiteral("resume"), QStringLiteral("retry"), QStringLiteral("reconcile"),
        QStringLiteral("config"), QStringLiteral("model"), QStringLiteral("models"), QStringLiteral("import"),
        QStringLiteral("update"), QStringLiteral("shutdown")};
    if (!commands.contains(command))
        return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("Unknown command: %1").arg(command)), pretty);
    bool validTimeout = false;
    const int timeout = parser.value(QStringLiteral("timeout")).toInt(&validTimeout);
    if (!validTimeout || timeout < 1 || timeout > 86400000)
        return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("--timeout must be 1..86400000 milliseconds")), pretty);
    if (command == QStringLiteral("update")) {
        if (!arguments.isEmpty())
            return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("update does not accept extra arguments")), pretty);
        return printOutcome(checkUpdate(timeout), pretty);
    }
    const bool modelCommand = command == QStringLiteral("model") || command == QStringLiteral("models") || command == QStringLiteral("import");
    if (modelCommand && arguments == QStringList{QStringLiteral("manifest")}) {
        QJsonArray artifacts;
        for (const auto &artifact : ModelFiles::manifest())
            artifacts.append(QJsonObject{{QStringLiteral("name"), artifact.name},
                                        {QStringLiteral("url"), artifact.url.toString()},
                                        {QStringLiteral("size"), artifact.size},
                                        {QStringLiteral("sha256"), QString::fromLatin1(artifact.sha256)}});
        return printOutcome({{{QStringLiteral("artifacts"), artifacts}}, {}, {}}, pretty);
    }
    const auto paths = RuntimePaths::resolve(parser.value(QStringLiteral("profile-dir")), parser.value(QStringLiteral("model-dir")));
    QString error;
    if (!paths.ensure(&error))
        return printOutcome(failure(QStringLiteral("path_error"), error), pretty);
    if (command == QStringLiteral("import"))
        arguments.prepend(QStringLiteral("import"));
    if (modelCommand && arguments.value(0) != QStringLiteral("reload")) {
        return printOutcome(modelOperation(arguments, parser, paths, timeout), pretty);
    }

    QString method;
    QJsonObject params;
    if (command == QStringLiteral("search")) {
        bool validLimit = false;
        const int limit = parser.value(QStringLiteral("limit")).toInt(&validLimit);
        if (arguments.isEmpty() || arguments.join(u' ').trimmed().isEmpty() || !validLimit || limit < 1 || limit > maximumSearchResults
            || parser.values(QStringLiteral("root")).size() > 1)
            return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("search requires a nonempty query, limit 1..%1 and at most one --root").arg(maximumSearchResults)), pretty);
        method = QStringLiteral("search");
        params = {{QStringLiteral("query"), arguments.join(u' ')}, {QStringLiteral("kind"), parser.value(QStringLiteral("kind"))},
                  {QStringLiteral("root"), parser.isSet(QStringLiteral("root")) ? QFileInfo(parser.value(QStringLiteral("root"))).absoluteFilePath() : QString()},
                  {QStringLiteral("code"), parser.isSet(QStringLiteral("code"))}, {QStringLiteral("limit"), limit},
                  {QStringLiteral("semantic"), !parser.isSet(QStringLiteral("keyword-only"))}};
    } else if (command == QStringLiteral("config")) {
        const QString action = arguments.isEmpty() ? QStringLiteral("get") : arguments.takeFirst();
        if (action == QStringLiteral("get") && arguments.isEmpty() && !parser.isSet(QStringLiteral("file")))
            method = QStringLiteral("settings.get");
        else if (action == QStringLiteral("set")) {
            QByteArray bytes;
            if (parser.isSet(QStringLiteral("file"))) {
                if (!arguments.isEmpty())
                    return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("config set accepts either JSON or --file")), pretty);
                QFile file(parser.value(QStringLiteral("file")));
                if (!file.open(QIODevice::ReadOnly))
                    return printOutcome(failure(QStringLiteral("file_error"), file.errorString()), pretty);
                if (file.size() > 1024 * 1024)
                    return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("Configuration exceeds 1 MiB")), pretty);
                bytes = file.readAll();
            } else if (!arguments.isEmpty()) {
                if (arguments.size() != 1)
                    return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("Quote the JSON settings as one argument")), pretty);
                bytes = arguments.first().toUtf8();
            }
            if (!bytes.isEmpty()) {
                QJsonParseError parseError;
                const auto document = QJsonDocument::fromJson(bytes, &parseError);
                if (!document.isObject() || parseError.error != QJsonParseError::NoError)
                    return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("Settings must be a JSON object: %1").arg(parseError.errorString())), pretty);
                params = document.object();
            }
            method = QStringLiteral("settings.set");
        } else {
            return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("Use config get or config set JSON/--file")), pretty);
        }
    } else if (command == QStringLiteral("index")) {
        method = QStringLiteral("index.start");
    } else {
        if (modelCommand) {
            if (arguments != QStringList{QStringLiteral("reload")})
                return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("model reload does not accept extra arguments")), pretty);
            method = QStringLiteral("models.reload");
        } else {
            if (!arguments.isEmpty())
                return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("%1 does not accept extra arguments").arg(command)), pretty);
            method = (command == QStringLiteral("status") || command == QStringLiteral("shutdown"))
                ? command : QStringLiteral("index.") + command;
        }
    }

    if (command == QStringLiteral("index") || method == QStringLiteral("settings.set")) {
        const bool codeSpace = parser.isSet(QStringLiteral("code"));
        QStringList roots = parser.values(QStringLiteral("root"));
        if (command == QStringLiteral("index")) roots.append(arguments);
        if (!roots.isEmpty()) {
            for (auto &root : roots) root = QFileInfo(root).absoluteFilePath();
            params.insert(codeSpace ? QStringLiteral("codeRoots") : QStringLiteral("roots"), QJsonArray::fromStringList(roots));
        }
        if (parser.isSet(QStringLiteral("exclude"))) {
            auto excludes = parser.values(QStringLiteral("exclude"));
            for (auto &exclude : excludes) exclude = QFileInfo(exclude).absoluteFilePath();
            params.insert(codeSpace ? QStringLiteral("codeExcludes") : QStringLiteral("excludes"), QJsonArray::fromStringList(excludes));
        }
        if (codeSpace) params.insert(QStringLiteral("includeCode"), true);
        if (method == QStringLiteral("settings.set") && params.isEmpty())
            return printOutcome(failure(QStringLiteral("usage"), QStringLiteral("config set requires settings JSON or --root/--exclude/--code")), pretty);
    }

    ServiceClient client(paths);
    if (parser.isSet(QStringLiteral("no-start"))) {
        client.setAutoStart(false);
        client.connectToService();
    } else {
        client.ensureService(timeout);
    }
    if (command == QStringLiteral("index") && !params.isEmpty()) {
        const auto configured = call(client, QStringLiteral("settings.set"), params, timeout);
        if (!configured.ok()) return printOutcome(configured, pretty);
        params = {};
    }
    auto result = call(client, method, params, timeout);
    if (result.ok() && parser.isSet(QStringLiteral("wait"))
        && (command == QStringLiteral("index") || command == QStringLiteral("resume") || command == QStringLiteral("retry"))) {
        QElapsedTimer elapsed;
        elapsed.start();
        do {
            const int remaining = timeout - int(elapsed.elapsed());
            if (remaining <= 0) {
                result = failure(QStringLiteral("timeout"), QStringLiteral("Indexing continues in the service. Use status or pause to manage it."));
                break;
            }
            result = call(client, QStringLiteral("status"), {}, remaining);
            if (!result.ok()) break;
            const auto progress = result.result.value(QStringLiteral("progress")).toObject();
            const QString state = progress.value(QStringLiteral("state")).toString();
            if (state == QStringLiteral("idle") || state == QStringLiteral("complete")
                || state == QStringLiteral("completed") || state == QStringLiteral("partial")) {
                if (state == QStringLiteral("partial") || progress.value(QStringLiteral("failed")).toInteger() > 0)
                    result = failure(QStringLiteral("partial_failure"),
                        QStringLiteral("Indexing completed with failures. Use retry after correcting the failed sources."),
                        {{QStringLiteral("progress"), progress},
                         {QStringLiteral("failures"), result.result.value(QStringLiteral("failures"))}});
                break;
            }
            if (state == QStringLiteral("failed") || state == QStringLiteral("error") || state == QStringLiteral("paused")) {
                result = failure(QStringLiteral("index_") + state, progress.value(QStringLiteral("error")).toString(state));
                break;
            }
            QEventLoop delay;
            QTimer::singleShot(qMin(500, remaining), &delay, &QEventLoop::quit);
            delay.exec();
        } while (true);
    }
    return printOutcome(result, pretty);
}
