#include "support/test_environment.h"

#include <QDir>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalServer>
#include <QLocalSocket>
#include <QProcess>
#include <QSet>
#include <QStandardPaths>
#include <QTimer>
#include <QtEndian>
#include <QtTest>

using namespace qcopilots_search::test;

namespace {
struct Invocation {
    bool started = false;
    bool finished = false;
    int exitCode = -1;
    QByteArray output;
    QByteArray errors;
    QJsonObject json() const { return QJsonDocument::fromJson(output).object(); }
};

Invocation invoke(const TestEnvironment &environment, const QStringList &arguments, bool processEvents = false)
{
    QProcess process;
    process.setProgram(QStringLiteral(QCOPILOTS_SEARCH_TEST_CLI_PATH));
    process.setWorkingDirectory(environment.root());
    process.setArguments(QStringList{QStringLiteral("--profile-dir"), environment.path(QStringLiteral("用户 profile")),
                                     QStringLiteral("--model-dir"), environment.path(QStringLiteral("模型 cache"))} + arguments);
    process.start();
    Invocation result;
    result.started = process.waitForStarted(5000);
    if (!result.started) {
        result.errors = process.errorString().toUtf8();
        return result;
    }
    if (processEvents) {
        // A hermetic IPC fixture runs in this process. Keep its socket and
        // timers responsive while waiting for the real CLI child to finish.
        QEventLoop loop;
        QTimer deadline;
        deadline.setSingleShot(true);
        QObject::connect(&process, &QProcess::finished, &loop, &QEventLoop::quit);
        QObject::connect(&deadline, &QTimer::timeout, &loop, &QEventLoop::quit);
        deadline.start(15000);
        if (process.state() != QProcess::NotRunning)
            loop.exec();
        result.finished = process.state() == QProcess::NotRunning;
    } else {
        result.finished = process.waitForFinished(15000);
    }
    if (!result.finished) {
        // Only the child created above is stopped, never another application.
        process.kill();
        process.waitForFinished(5000);
    }
    result.exitCode = process.exitCode();
    result.output = process.readAllStandardOutput();
    result.errors = process.readAllStandardError();
    return result;
}

class OwnedService final {
public:
    explicit OwnedService(const TestEnvironment &environment) : environment_(environment) {}
    ~OwnedService()
    {
        if (process.state() == QProcess::NotRunning) return;
        (void)invoke(environment_, {QStringLiteral("shutdown"), QStringLiteral("--no-start"),
                                    QStringLiteral("--timeout"), QStringLiteral("3000")});
        if (!process.waitForFinished(5000) && process.state() != QProcess::NotRunning) {
            process.kill();
            process.waitForFinished(5000);
        }
    }
    bool start()
    {
        process.setWorkingDirectory(environment_.root());
        process.start(QStringLiteral(QCOPILOTS_SEARCH_TEST_SERVICE_PATH),
            {QStringLiteral("--profile-dir"), environment_.path(QStringLiteral("用户 profile")),
             QStringLiteral("--model-dir"), environment_.path(QStringLiteral("模型 cache"))});
        if (!process.waitForStarted(5000)) return false;
        QElapsedTimer timer;
        timer.start();
        while (timer.elapsed() < 5000 && process.state() != QProcess::NotRunning) {
            const auto status = invoke(environment_, {QStringLiteral("status"), QStringLiteral("--no-start"),
                                                       QStringLiteral("--timeout"), QStringLiteral("250")});
            if (status.finished && status.exitCode == 0) return true;
            QTest::qWait(50);
        }
        return false;
    }
    QProcess process;
private:
    const TestEnvironment &environment_;
};

class ModelSelectionService final {
public:
    bool start(const TestEnvironment &environment)
    {
        const auto paths = qcopilots_search::RuntimePaths::resolve(
            environment.path(QStringLiteral("用户 profile")), environment.path(QStringLiteral("模型 cache")));
        if (!paths.ensure(&error)
            || !environment.writeFile(QStringLiteral("用户 profile/qcs-ipc-token"), token, &error))
            return false;
        server.setSocketOptions(QLocalServer::UserAccessOption);
        QObject::connect(&server, &QLocalServer::newConnection, &server, [this] {
            while (auto *socket = server.nextPendingConnection()) {
                auto buffer = std::make_shared<QByteArray>();
                QObject::connect(socket, &QLocalSocket::readyRead, socket, [this, socket, buffer] {
                    buffer->append(socket->readAll());
                    while (buffer->size() >= 4) {
                        const quint32 length = qFromBigEndian<quint32>(buffer->constData());
                        if (length > 16 * 1024 * 1024 || length == 0) {
                            error = QStringLiteral("Invalid CLI frame length");
                            socket->abort();
                            return;
                        }
                        if (buffer->size() < qsizetype(length) + 4)
                            return;
                        const auto request = QJsonDocument::fromJson(buffer->mid(4, length)).object();
                        buffer->remove(0, length + 4);
                        if (request.value(QStringLiteral("protocol")).toInt() != 1
                            || request.value(QStringLiteral("token")).toString().toUtf8() != token) {
                            error = QStringLiteral("Invalid CLI authentication or protocol");
                            socket->abort();
                            return;
                        }
                        const QString method = request.value(QStringLiteral("method")).toString();
                        methods.append(method);
                        if (method == QStringLiteral("cancel"))
                            continue;
                        QJsonObject reply{{QStringLiteral("protocol"), 1},
                            {QStringLiteral("id"), request.value(QStringLiteral("id"))}, {QStringLiteral("ok"), true}};
                        int delay = 0;
                        if (method == QStringLiteral("status")) {
                            ++statusRequests;
                            reply.insert(QStringLiteral("result"), QJsonObject{{QStringLiteral("modelLoading"),
                                loadingReplies < 0 || statusRequests <= loadingReplies}});
                        } else if (method == QStringLiteral("models.select")) {
                            ++selectRequests;
                            selectedId = request.value(QStringLiteral("params")).toObject().value(QStringLiteral("modelId")).toString();
                            delay = selectionDelay;
                            if (selectionBusy) {
                                reply.insert(QStringLiteral("ok"), false);
                                reply.insert(QStringLiteral("error"), QJsonObject{{QStringLiteral("code"), QStringLiteral("busy")},
                                    {QStringLiteral("message"), QStringLiteral("A different transition is in progress")}});
                            } else {
                                reply.insert(QStringLiteral("result"), QJsonObject{{QStringLiteral("modelId"), selectedId}});
                            }
                        } else {
                            error = QStringLiteral("Unexpected CLI method: %1").arg(method);
                            socket->abort();
                            return;
                        }
                        const QByteArray payload = QJsonDocument(reply).toJson(QJsonDocument::Compact);
                        QByteArray frame(4, '\0');
                        qToBigEndian<quint32>(quint32(payload.size()), frame.data());
                        frame.append(payload);
                        QTimer::singleShot(delay, socket, [socket, frame] { socket->write(frame); });
                    }
                });
            }
        });
        if (server.listen(paths.ipcName))
            return true;
        error = server.errorString();
        return false;
    }

    int loadingReplies = 2;
    int selectionDelay = 0;
    bool selectionBusy = false;
    int statusRequests = 0;
    int selectRequests = 0;
    QString selectedId;
    QStringList methods;
    QString error;
private:
    const QByteArray token = QByteArray(64, 'f');
    QLocalServer server;
};
}

class CliTests final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = qcopilots_search::test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
        QVERIFY2(QFileInfo::exists(QStringLiteral(QCOPILOTS_SEARCH_TEST_CLI_PATH)), QCOPILOTS_SEARCH_TEST_CLI_PATH);
    }

    void helpDoesNotCreateProfile()
    {
        TestEnvironment environment(QStringLiteral("cli-help"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("--help")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 0);
        QVERIFY(result.output.contains("qcs-cli"));
        QVERIFY(result.output.contains("model"));
        QVERIFY(result.output.contains("--profile-dir"));
        QVERIFY(result.output.contains("(1..500)"));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile"))));
    }

    void usageErrorsAreMachineReadable_data()
    {
        QTest::addColumn<QStringList>("arguments");
        QTest::newRow("unknown-command") << QStringList{QStringLiteral("does-not-exist")};
        QTest::newRow("unknown-option") << QStringList{QStringLiteral("--does-not-exist")};
        QTest::newRow("missing-command") << QStringList{};
        QTest::newRow("invalid-timeout") << QStringList{QStringLiteral("status"), QStringLiteral("--timeout"), QStringLiteral("zero")};
        QTest::newRow("negative-timeout") << QStringList{QStringLiteral("status"), QStringLiteral("--timeout=-1")};
        QTest::newRow("empty-query") << QStringList{QStringLiteral("search"), QStringLiteral(" ")};
        QTest::newRow("invalid-limit") << QStringList{QStringLiteral("search"), QStringLiteral("中文"), QStringLiteral("--limit"), QStringLiteral("0")};
        QTest::newRow("over-maximum-limit") << QStringList{QStringLiteral("search"), QStringLiteral("中文"), QStringLiteral("--limit"), QStringLiteral("501")};
        QTest::newRow("invalid-config") << QStringList{QStringLiteral("config"), QStringLiteral("set"), QStringLiteral("{invalid}")};
        QTest::newRow("array-config") << QStringList{QStringLiteral("config"), QStringLiteral("set"), QStringLiteral("[]")};
        QTest::newRow("missing-import-source") << QStringList{QStringLiteral("model"), QStringLiteral("import")};
        QTest::newRow("unknown-model") << QStringList{QStringLiteral("model"), QStringLiteral("manifest"), QStringLiteral("--model"), QStringLiteral("unknown")};
        QTest::newRow("missing-model-selection") << QStringList{QStringLiteral("model"), QStringLiteral("select")};
        QTest::newRow("unknown-model-selection") << QStringList{QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("unknown")};
        QTest::newRow("extra-model-selection") << QStringList{QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("embeddinggemma-2"), QStringLiteral("extra")};
        QTest::newRow("model-option-on-selection") << QStringList{QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("embeddinggemma-2"), QStringLiteral("--model"), QStringLiteral("qwen3-vl-embedding-2b")};
        QTest::newRow("model-option-on-reload") << QStringList{QStringLiteral("model"), QStringLiteral("reload"), QStringLiteral("--model"), QStringLiteral("qwen3-vl-embedding-2b")};
        QTest::newRow("model-option-on-search") << QStringList{QStringLiteral("search"), QStringLiteral("query"), QStringLiteral("--model"), QStringLiteral("qwen3-vl-embedding-2b")};
        QTest::newRow("extra-config-reload") << QStringList{QStringLiteral("config"), QStringLiteral("reload"), QStringLiteral("extra")};
        QTest::newRow("file-config-reload") << QStringList{QStringLiteral("config"), QStringLiteral("reload"), QStringLiteral("--file"), QStringLiteral("settings.json")};
    }

    void usageErrorsAreMachineReadable()
    {
        QFETCH(QStringList, arguments);
        TestEnvironment environment(QStringLiteral("cli-usage"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, arguments);
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 2);
        QCOMPARE(result.json().value(QStringLiteral("ok")).toBool(true), false);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("usage"));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile/qcs-ipc-token"))));
    }

    void manifestIsOfflineAndDoesNotCreateProfile_data()
    {
        QTest::addColumn<QString>("modelId");
        QTest::addColumn<int>("dimensions");
        QTest::newRow("default-gemma") << QString{} << 768;
        QTest::newRow("explicit-gemma") << QStringLiteral("embeddinggemma-2") << 768;
        QTest::newRow("qwen-2b") << QStringLiteral("qwen3-vl-embedding-2b") << 2048;
        QTest::newRow("qwen-8b") << QStringLiteral("qwen3-vl-embedding-8b") << 4096;
    }

    void manifestIsOfflineAndDoesNotCreateProfile()
    {
        QFETCH(QString, modelId);
        QFETCH(int, dimensions);
        TestEnvironment environment(QStringLiteral("cli-manifest"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QStringList arguments{QStringLiteral("models"), QStringLiteral("manifest")};
        if (!modelId.isEmpty()) arguments.append({QStringLiteral("--model"), modelId});
        const auto result = invoke(environment, arguments);
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 0);
        QVERIFY(result.json().value(QStringLiteral("ok")).toBool());
        const auto manifest = result.json().value(QStringLiteral("result")).toObject();
        QCOMPARE(manifest.value(QStringLiteral("modelId")).toString(), modelId.isEmpty() ? QStringLiteral("embeddinggemma-2") : modelId);
        QCOMPARE(manifest.value(QStringLiteral("dimensions")).toInt(), dimensions);
        const auto artifacts = manifest.value(QStringLiteral("artifacts")).toArray();
        QCOMPARE(artifacts.size(), 2);
        for (const auto &value : artifacts) {
            const auto artifact = value.toObject();
            QCOMPARE(artifact.value(QStringLiteral("sha256")).toString().size(), 64);
            QVERIFY(artifact.value(QStringLiteral("size")).toInteger() > 0);
            QVERIFY(artifact.value(QStringLiteral("url")).toString().startsWith(QStringLiteral("https://")));
        }
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile"))));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("模型 cache"))));
    }

    void modelListIsOfflineAndDoesNotCreateProfile()
    {
        TestEnvironment environment(QStringLiteral("cli-model-list"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("list")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 0);
        const auto catalog = result.json().value(QStringLiteral("result")).toObject();
        QCOMPARE(catalog.value(QStringLiteral("defaultModelId")).toString(), QStringLiteral("embeddinggemma-2"));
        const auto models = catalog.value(QStringLiteral("models")).toArray();
        QCOMPARE(models.size(), 3);
        QSet<QString> identities;
        QSet<int> dimensions;
        for (const auto &entry : models) {
            const auto model = entry.toObject();
            identities.insert(model.value(QStringLiteral("id")).toString());
            dimensions.insert(model.value(QStringLiteral("dimensions")).toInt());
        }
        QCOMPARE(identities, (QSet<QString>{QStringLiteral("embeddinggemma-2"), QStringLiteral("qwen3-vl-embedding-2b"), QStringLiteral("qwen3-vl-embedding-8b")}));
        QCOMPARE(dimensions, (QSet<int>{768, 2048, 4096}));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile"))));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("模型 cache"))));
    }

    void modelStatusUsesUnicodeOverride_data()
    {
        QTest::addColumn<QString>("modelId");
        QTest::addColumn<int>("dimensions");
        QTest::newRow("gemma") << QStringLiteral("embeddinggemma-2") << 768;
        QTest::newRow("qwen-2b") << QStringLiteral("qwen3-vl-embedding-2b") << 2048;
        QTest::newRow("qwen-8b") << QStringLiteral("qwen3-vl-embedding-8b") << 4096;
    }

    void modelStatusUsesUnicodeOverride()
    {
        QFETCH(QString, modelId);
        QFETCH(int, dimensions);
        TestEnvironment environment(QStringLiteral("cli-model-status"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("models"), QStringLiteral("status"), QStringLiteral("--pretty"), QStringLiteral("--model"), modelId});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 0);
        const auto model = result.json().value(QStringLiteral("result")).toObject();
        QCOMPARE(model.value(QStringLiteral("valid")).toBool(true), false);
        QCOMPARE(model.value(QStringLiteral("filesVerified")).toBool(true), false);
        QCOMPARE(model.value(QStringLiteral("modelId")).toString(), modelId);
        QCOMPARE(model.value(QStringLiteral("dimensions")).toInt(), dimensions);
        QVERIFY(!model.contains(QStringLiteral("modelAvailable")));
        const QString directory = modelId == QStringLiteral("embeddinggemma-2")
            ? environment.path(QStringLiteral("模型 cache")) : environment.path(QStringLiteral("模型 cache/") + modelId);
        QCOMPARE(QDir::cleanPath(model.value(QStringLiteral("directory")).toString()),
                 QDir::cleanPath(directory));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile/qcs-ipc-token"))));
    }

    void incompleteModelVerificationFails_data()
    {
        QTest::addColumn<QString>("modelId");
        QTest::newRow("gemma") << QStringLiteral("embeddinggemma-2");
        QTest::newRow("qwen-2b") << QStringLiteral("qwen3-vl-embedding-2b");
        QTest::newRow("qwen-8b") << QStringLiteral("qwen3-vl-embedding-8b");
    }

    void incompleteModelVerificationFails()
    {
        QFETCH(QString, modelId);
        TestEnvironment environment(QStringLiteral("cli-model-verify"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("verify"), QStringLiteral("--model"), modelId});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("model_invalid"));
    }

    void corruptedModelCannotPublish()
    {
        TestEnvironment environment(QStringLiteral("cli-corrupt-import"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("not a model.gguf"), "not a model"));
        const auto result = invoke(environment, {QStringLiteral("import"), environment.path(QStringLiteral("not a model.gguf")),
                                                QStringLiteral("--name"), QStringLiteral("embeddinggemma-2-Q8_0.gguf")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("model_import_failed"));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("模型 cache/embeddinggemma-2-Q8_0.gguf"))));
    }

    void importCannotUseAnotherModelsProjector()
    {
        TestEnvironment environment(QStringLiteral("cli-wrong-projector"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString fileName = QStringLiteral("Qwen3-VL-Embedding-2B.mmproj-f16.gguf");
        QVERIFY(environment.writeFile(fileName, "not the selected model's projector"));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("import"), environment.path(fileName),
            QStringLiteral("--model"), QStringLiteral("qwen3-vl-embedding-8b")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("model_import_failed"));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("模型 cache/qwen3-vl-embedding-8b/") + fileName)));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile/qcs-settings.json"))));
    }

    void blockedProfileFailsWithoutFallback()
    {
        TestEnvironment environment(QStringLiteral("cli-blocked-profile"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("用户 profile"), "ordinary file"));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("status")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("path_error"));
        QVERIFY(QFileInfo(environment.path(QStringLiteral("用户 profile"))).isFile());
    }

    void connectOnlyNeverStartsService()
    {
        TestEnvironment environment(QStringLiteral("cli-no-start"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("status"), QStringLiteral("--no-start"), QStringLiteral("--timeout"), QStringLiteral("250")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("ok")).toBool(true), false);
        QVERIFY(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString()
                != QStringLiteral("service_start_failed"));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile/qcs-ipc-token"))));
    }

    void modelSelectWaitsForStartupProbe()
    {
        TestEnvironment environment(QStringLiteral("cli-model-startup"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ModelSelectionService service;
        QVERIFY2(service.start(environment), qPrintable(service.error));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("qwen3-vl-embedding-2b"),
            QStringLiteral("--timeout"), QStringLiteral("3000")}, true);
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QVERIFY2(service.error.isEmpty(), qPrintable(service.error));
        QCOMPARE(result.exitCode, 0);
        QCOMPARE(service.methods, (QStringList{QStringLiteral("status"), QStringLiteral("status"),
            QStringLiteral("status"), QStringLiteral("models.select")}));
        QCOMPARE(service.selectRequests, 1);
        QCOMPARE(service.selectedId, QStringLiteral("qwen3-vl-embedding-2b"));
        QCOMPARE(result.json().value(QStringLiteral("result")).toObject().value(QStringLiteral("modelId")).toString(), service.selectedId);
    }

    void modelSelectStartupTimeoutDoesNotSelect()
    {
        TestEnvironment environment(QStringLiteral("cli-model-startup-timeout"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ModelSelectionService service;
        service.loadingReplies = -1;
        QVERIFY2(service.start(environment), qPrintable(service.error));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("qwen3-vl-embedding-2b"),
            QStringLiteral("--timeout"), QStringLiteral("350")}, true);
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QVERIFY2(service.error.isEmpty(), qPrintable(service.error));
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("timeout"));
        QVERIFY(service.statusRequests >= 1);
        QCOMPARE(service.selectRequests, 0);
    }

    void modelSelectSharesStartupAndSelectionDeadline()
    {
        TestEnvironment environment(QStringLiteral("cli-model-selection-deadline"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ModelSelectionService service;
        service.selectionDelay = 2700;
        QVERIFY2(service.start(environment), qPrintable(service.error));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("qwen3-vl-embedding-2b"),
            QStringLiteral("--timeout"), QStringLiteral("3000")}, true);
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QVERIFY2(service.error.isEmpty(), qPrintable(service.error));
        QCOMPARE(result.exitCode, 1);
        const QString code = result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString();
        QVERIFY2(code == QStringLiteral("timeout") || code == QStringLiteral("request_timeout"), result.output.constData());
        QCOMPARE(service.statusRequests, 3);
        QCOMPARE(service.selectRequests, 1);
    }

    void modelSelectDoesNotRetryOtherBusyFailures()
    {
        TestEnvironment environment(QStringLiteral("cli-model-selection-busy"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        ModelSelectionService service;
        service.loadingReplies = 0;
        service.selectionBusy = true;
        QVERIFY2(service.start(environment), qPrintable(service.error));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("qwen3-vl-embedding-2b"),
            QStringLiteral("--timeout"), QStringLiteral("3000")}, true);
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QVERIFY2(service.error.isEmpty(), qPrintable(service.error));
        QCOMPARE(result.exitCode, 1);
        QCOMPARE(result.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString(), QStringLiteral("busy"));
        QCOMPARE(service.statusRequests, 1);
        QCOMPARE(service.selectRequests, 1);
    }

    void unavailableModelSelectionKeepsCurrentSettings()
    {
        TestEnvironment environment(QStringLiteral("cli-model-select"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        OwnedService service(environment);
        QVERIFY2(service.start(), service.process.readAllStandardError().constData());
        const auto before = invoke(environment, {QStringLiteral("config"), QStringLiteral("get"), QStringLiteral("--no-start")});
        QCOMPARE(before.exitCode, 0);
        const auto original = before.json().value(QStringLiteral("result")).toObject();
        QCOMPARE(original.value(QStringLiteral("modelId")).toString(), QStringLiteral("embeddinggemma-2"));
        const auto selected = invoke(environment, {QStringLiteral("model"), QStringLiteral("select"), QStringLiteral("qwen3-vl-embedding-2b"),
            QStringLiteral("--no-start"), QStringLiteral("--timeout"), QStringLiteral("3000")});
        QVERIFY2(selected.started && selected.finished, selected.errors.constData());
        QCOMPARE(selected.exitCode, 1);
        const auto errorCode = selected.json().value(QStringLiteral("error")).toObject().value(QStringLiteral("code")).toString();
        QCOMPARE(errorCode, QStringLiteral("model_unavailable"));
        const auto after = invoke(environment, {QStringLiteral("config"), QStringLiteral("get"), QStringLiteral("--no-start")});
        QCOMPARE(after.exitCode, 0);
        QCOMPARE(after.json().value(QStringLiteral("result")).toObject(), original);
    }

    void configReloadReadsDiskAndRejectsInvalidJson()
    {
        TestEnvironment environment(QStringLiteral("cli-config-reload"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        OwnedService service(environment);
        QVERIFY2(service.start(), service.process.readAllStandardError().constData());
        const auto initial = invoke(environment, {QStringLiteral("config"), QStringLiteral("get"), QStringLiteral("--no-start")});
        QCOMPARE(initial.exitCode, 0);
        auto edited = initial.json().value(QStringLiteral("result")).toObject();
        edited.insert(QStringLiteral("indexingPaused"), true);
        QVERIFY(environment.writeFile(QStringLiteral("用户 profile/qcs-settings.json"), QJsonDocument(edited).toJson()));
        const auto reloaded = invoke(environment, {QStringLiteral("config"), QStringLiteral("reload"), QStringLiteral("--no-start"),
            QStringLiteral("--timeout"), QStringLiteral("3000")});
        QCOMPARE(reloaded.exitCode, 0);
        QCOMPARE(reloaded.json().value(QStringLiteral("result")).toObject(), edited);
        QVERIFY(environment.writeFile(QStringLiteral("用户 profile/qcs-settings.json"), "{invalid-json"));
        const auto invalid = invoke(environment, {QStringLiteral("config"), QStringLiteral("reload"), QStringLiteral("--no-start"),
            QStringLiteral("--timeout"), QStringLiteral("3000")});
        QCOMPARE(invalid.exitCode, 1);
        const auto unchanged = invoke(environment, {QStringLiteral("config"), QStringLiteral("get"), QStringLiteral("--no-start")});
        QCOMPARE(unchanged.exitCode, 0);
        QCOMPARE(unchanged.json().value(QStringLiteral("result")).toObject(), edited);
    }

    void newProfileCodeIndexUsesIndependentRoots_data()
    {
        QTest::addColumn<bool>("rootOption");
        QTest::newRow("positional-root") << false;
        QTest::newRow("root-option") << true;
    }

    void newProfileCodeIndexUsesIndependentRoots()
    {
        QFETCH(bool, rootOption);
        TestEnvironment environment(QStringLiteral("cli-code-index"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("代码 root/source.cpp"), "int clifreshcodekeyword = 1;\n"));
        QVERIFY(environment.writeFile(QStringLiteral("代码 root/skip/ignored.cpp"), "int cliexcludedcodekeyword = 1;\n"));
        const QString codeRoot = environment.path(QStringLiteral("代码 root"));
        const QString codeExclude = environment.path(QStringLiteral("代码 root/skip"));
        OwnedService service(environment);
        QVERIFY2(service.start(), service.process.readAllStandardError().constData());
        QStringList arguments{QStringLiteral("index"), QStringLiteral("--code")};
        if (rootOption) arguments.append(QStringLiteral("--root"));
        arguments.append(codeRoot);
        arguments.append({QStringLiteral("--exclude"), codeExclude, QStringLiteral("--wait"),
            QStringLiteral("--no-start"), QStringLiteral("--timeout"), QStringLiteral("10000")});
        const auto indexed = invoke(environment, arguments);
        QVERIFY2(indexed.started && indexed.finished, indexed.errors.constData());
        QCOMPARE(indexed.exitCode, 0);
        QVERIFY2(indexed.json().value(QStringLiteral("ok")).toBool(), indexed.output.constData());
        const auto config = invoke(environment, {QStringLiteral("config"), QStringLiteral("get"), QStringLiteral("--no-start")});
        QCOMPARE(config.exitCode, 0);
        const auto settings = config.json().value(QStringLiteral("result")).toObject();
        QVERIFY(settings.value(QStringLiteral("includeCode")).toBool());
        QCOMPARE(settings.value(QStringLiteral("codeRoots")).toArray(), QJsonArray{codeRoot});
        QCOMPARE(settings.value(QStringLiteral("codeExcludes")).toArray(), QJsonArray{codeExclude});
        QVERIFY(settings.value(QStringLiteral("roots")).toArray().isEmpty());
        QVERIFY(settings.value(QStringLiteral("excludes")).toArray().isEmpty());
        const auto search = invoke(environment, {QStringLiteral("search"), QStringLiteral("clifreshcodekeyword"),
            QStringLiteral("--code"), QStringLiteral("--keyword-only"), QStringLiteral("--no-start"),
            QStringLiteral("--limit"), QStringLiteral("500")});
        QCOMPARE(search.exitCode, 0);
        const auto hits = search.json().value(QStringLiteral("result")).toObject().value(QStringLiteral("hits")).toArray();
        QCOMPARE(hits.size(), 1);
        QCOMPARE(hits.first().toObject().value(QStringLiteral("path")).toString(), environment.path(QStringLiteral("代码 root/source.cpp")));
        const auto excluded = invoke(environment, {QStringLiteral("search"), QStringLiteral("cliexcludedcodekeyword"),
            QStringLiteral("--code"), QStringLiteral("--keyword-only"), QStringLiteral("--no-start")});
        QCOMPARE(excluded.exitCode, 0);
        QVERIFY(excluded.json().value(QStringLiteral("result")).toObject().value(QStringLiteral("hits")).toArray().isEmpty());
    }

    void configSetCodePreservesOrdinaryRootsAndExcludes()
    {
        TestEnvironment environment(QStringLiteral("cli-code-config"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString ordinaryRoot = environment.path(QStringLiteral("ordinary"));
        const QString ordinaryExclude = environment.path(QStringLiteral("ordinary/skip"));
        const QString codeRoot = environment.path(QStringLiteral("code-one"));
        const QString secondCodeRoot = environment.path(QStringLiteral("code-two"));
        const QString codeExclude = environment.path(QStringLiteral("code-one/skip"));
        const QString secondCodeExclude = environment.path(QStringLiteral("code-two/skip"));
        for (const auto &directory : {ordinaryExclude, codeExclude, secondCodeExclude})
            QVERIFY(QDir().mkpath(directory));
        OwnedService service(environment);
        QVERIFY2(service.start(), service.process.readAllStandardError().constData());
        const QString initialJson = QString::fromUtf8(QJsonDocument(QJsonObject{
            {QStringLiteral("roots"), QJsonArray{ordinaryRoot}}, {QStringLiteral("excludes"), QJsonArray{ordinaryExclude}},
            {QStringLiteral("indexingPaused"), true}}).toJson(QJsonDocument::Compact));
        const auto initial = invoke(environment, {QStringLiteral("config"), QStringLiteral("set"), initialJson, QStringLiteral("--no-start")});
        QCOMPARE(initial.exitCode, 0);
        const auto changed = invoke(environment, {QStringLiteral("config"), QStringLiteral("set"), QStringLiteral("--code"),
            QStringLiteral("--root"), codeRoot, QStringLiteral("--root"), secondCodeRoot,
            QStringLiteral("--exclude"), codeExclude, QStringLiteral("--exclude"), secondCodeExclude, QStringLiteral("--no-start")});
        QVERIFY2(changed.started && changed.finished, changed.errors.constData());
        QCOMPARE(changed.exitCode, 0);
        const auto settings = changed.json().value(QStringLiteral("result")).toObject();
        QCOMPARE(settings.value(QStringLiteral("roots")).toArray(), QJsonArray{ordinaryRoot});
        QCOMPARE(settings.value(QStringLiteral("excludes")).toArray(), QJsonArray{ordinaryExclude});
        QCOMPARE(settings.value(QStringLiteral("codeRoots")).toArray(), (QJsonArray{codeRoot, secondCodeRoot}));
        QCOMPARE(settings.value(QStringLiteral("codeExcludes")).toArray(), (QJsonArray{codeExclude, secondCodeExclude}));
        QVERIFY(settings.value(QStringLiteral("includeCode")).toBool());
        QVERIFY(settings.value(QStringLiteral("indexingPaused")).toBool());
        const auto persisted = invoke(environment, {QStringLiteral("config"), QStringLiteral("get"), QStringLiteral("--no-start")});
        QCOMPARE(persisted.exitCode, 0);
        QCOMPARE(persisted.json().value(QStringLiteral("result")).toObject(), settings);
        // Updating ordinary folder options must also preserve the code selection.
        const auto ordinary = invoke(environment, {QStringLiteral("config"), QStringLiteral("set"),
            QStringLiteral("--root"), ordinaryRoot, QStringLiteral("--exclude"), ordinaryExclude, QStringLiteral("--no-start")});
        QCOMPARE(ordinary.exitCode, 0);
        QCOMPARE(ordinary.json().value(QStringLiteral("result")).toObject(), settings);
    }

    void indexWaitReportsRealCorruptFileAsPartialFailure()
    {
        TestEnvironment environment(QStringLiteral("cli-partial-index"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QVERIFY(environment.writeFile(QStringLiteral("selected 文档/good.txt"), "The searchable aurora expedition record."));
        QVERIFY(environment.writeFile(QStringLiteral("selected 文档/broken.docx"), "This is a damaged document, not a ZIP container."));
        OwnedService service(environment);
        QVERIFY2(service.start(), service.process.readAllStandardError().constData());
        const auto result = invoke(environment, {QStringLiteral("index"), environment.path(QStringLiteral("selected 文档")),
            QStringLiteral("--wait"), QStringLiteral("--no-start"), QStringLiteral("--timeout"), QStringLiteral("10000")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 1);
        const auto failure = result.json().value(QStringLiteral("error")).toObject();
        QCOMPARE(failure.value(QStringLiteral("code")).toString(), QStringLiteral("partial_failure"));
        const auto details = failure.value(QStringLiteral("details")).toObject();
        const auto progress = details.value(QStringLiteral("progress")).toObject();
        QCOMPARE(progress.value(QStringLiteral("state")).toString(), QStringLiteral("partial"));
        QVERIFY(progress.value(QStringLiteral("failed")).toInteger() >= 1);
        QVERIFY(QJsonDocument(details).toJson().contains("broken.docx"));
        const auto search = invoke(environment, {QStringLiteral("search"), QStringLiteral("aurora"),
            QStringLiteral("--keyword-only"), QStringLiteral("--no-start"), QStringLiteral("--timeout"), QStringLiteral("3000")});
        QCOMPARE(search.exitCode, 0);
        QVERIFY2(search.output.contains("good.txt"), search.output.constData());
    }
};

QTEST_GUILESS_MAIN(CliTests)
#include "cli_tests.moc"
