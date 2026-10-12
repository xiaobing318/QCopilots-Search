#include "support/test_environment.h"

#include <QDir>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QStandardPaths>
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

Invocation invoke(const TestEnvironment &environment, const QStringList &arguments)
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
    result.finished = process.waitForFinished(15000);
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
        QTest::newRow("invalid-config") << QStringList{QStringLiteral("config"), QStringLiteral("set"), QStringLiteral("{invalid}")};
        QTest::newRow("array-config") << QStringList{QStringLiteral("config"), QStringLiteral("set"), QStringLiteral("[]")};
        QTest::newRow("missing-import-source") << QStringList{QStringLiteral("model"), QStringLiteral("import")};
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

    void manifestIsOfflineAndDoesNotCreateProfile()
    {
        TestEnvironment environment(QStringLiteral("cli-manifest"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("models"), QStringLiteral("manifest")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 0);
        QVERIFY(result.json().value(QStringLiteral("ok")).toBool());
        const auto artifacts = result.json().value(QStringLiteral("result")).toObject().value(QStringLiteral("artifacts")).toArray();
        QCOMPARE(artifacts.size(), 2);
        for (const auto &value : artifacts) {
            const auto artifact = value.toObject();
            QCOMPARE(artifact.value(QStringLiteral("sha256")).toString().size(), 64);
            QVERIFY(artifact.value(QStringLiteral("size")).toInteger() > 0);
            QVERIFY(artifact.value(QStringLiteral("url")).toString().startsWith(QStringLiteral("https://")));
        }
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile"))));
    }

    void modelStatusUsesUnicodeOverride()
    {
        TestEnvironment environment(QStringLiteral("cli-model-status"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("models"), QStringLiteral("status"), QStringLiteral("--pretty")});
        QVERIFY2(result.started && result.finished, result.errors.constData());
        QCOMPARE(result.exitCode, 0);
        const auto model = result.json().value(QStringLiteral("result")).toObject();
        QCOMPARE(model.value(QStringLiteral("valid")).toBool(true), false);
        QCOMPARE(QDir::cleanPath(model.value(QStringLiteral("directory")).toString()),
                 QDir::cleanPath(environment.path(QStringLiteral("模型 cache"))));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("用户 profile/qcs-ipc-token"))));
    }

    void incompleteModelVerificationFails()
    {
        TestEnvironment environment(QStringLiteral("cli-model-verify"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto result = invoke(environment, {QStringLiteral("model"), QStringLiteral("verify")});
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
