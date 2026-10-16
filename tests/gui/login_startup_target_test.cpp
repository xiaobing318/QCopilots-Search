#include "app/login_startup_target.h"
#include "core/runtime_paths.h"
#include "support/test_environment.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLockFile>
#include <QStandardPaths>
#include <QtTest>
#ifdef Q_OS_WIN
#include <windows.h>
#endif

using namespace qcopilots_search;
using namespace qcopilots_search::test;

namespace {
LoginStartupTarget targetFor(const TestEnvironment &environment, const QString &name)
{
    return {environment.path(name + QStringLiteral("/profile")),
            environment.path(name + QStringLiteral("/models"))};
}

QByteArray storedBytes(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    return file.readAll();
}
}

class LoginStartupTargetTest final : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { QStandardPaths::setTestModeEnabled(true); }
    void cleanup()
    {
        const auto error = takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }

    void saveAndReadDoNotCreateRuntimeDirectories()
    {
        TestEnvironment environment(QStringLiteral("login-target"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto target = targetFor(environment, QStringLiteral("data"));
        const QString locator = environment.path(QStringLiteral("locator/qcs-login-startup.json"));
        QString error;
        bool called = false;
        bool hiddenDuringRegistration = false;
        QVERIFY2(updateLoginStartupTarget(locator, target, true, [&](bool enabled, QString *) {
            called = enabled;
            LoginStartupTarget saved;
            QString readError;
            hiddenDuringRegistration = !readLoginStartupTarget(locator, &saved, &readError)
                && saved.profileDir.isEmpty() && readError.contains(QStringLiteral("lock"));
            return true;
        }, &error), qPrintable(error));
        QVERIFY(called);
        QVERIFY(hiddenDuringRegistration);
        QVERIFY(!QFileInfo::exists(target.profileDir));
        QVERIFY(!QFileInfo::exists(target.modelDir));
        LoginStartupTarget saved;
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY(error.contains(QStringLiteral("unavailable")));
        QVERIFY(!QFileInfo::exists(target.profileDir));
        QVERIFY(!QFileInfo::exists(target.modelDir));
        QVERIFY(QDir().mkpath(target.profileDir));
        QVERIFY(QDir().mkpath(target.modelDir));
        QVERIFY2(readLoginStartupTarget(locator, &saved, &error), qPrintable(error));
        const auto resolved = RuntimePaths::resolve(target.profileDir, target.modelDir);
        QCOMPARE(saved.profileDir, resolved.profileDir);
        QCOMPARE(saved.modelDir, resolved.modelDir);
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".lock")));
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
    }

    void failedRegistrationRestoresExactBytes()
    {
        TestEnvironment environment(QStringLiteral("login-rollback"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const QByteArray previous = "an invalid previous locator\r\n  ";
        QString error;
        QVERIFY2(environment.writeFile(QStringLiteral("target.json"), previous, &error), qPrintable(error));
        QVERIFY(!updateLoginStartupTarget(locator, targetFor(environment, QStringLiteral("new")), true,
            [](bool, QString *reason) { *reason = QStringLiteral("registration failed"); return false; }, &error));
        QCOMPARE(error, QStringLiteral("registration failed"));
        QCOMPARE(storedBytes(locator), previous);
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
    }

    void failedRegistrationRestoresMissingLocator()
    {
        TestEnvironment environment(QStringLiteral("login-rollback-missing"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        QString error;
        QVERIFY(!updateLoginStartupTarget(locator, targetFor(environment, QStringLiteral("new")), true,
            [](bool, QString *) { return false; }, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!QFileInfo::exists(locator));
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
    }

    void failedTakeoverNeverExposesUnregisteredTarget()
    {
        TestEnvironment environment(QStringLiteral("login-takeover-rollback"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto original = targetFor(environment, QStringLiteral("original"));
        QVERIFY(QDir().mkpath(original.profileDir));
        QVERIFY(QDir().mkpath(original.modelDir));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, original, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        const auto before = storedBytes(locator);
        bool hiddenDuringRegistration = false;
        QVERIFY(!updateLoginStartupTarget(locator, targetFor(environment, QStringLiteral("replacement")), true,
            [&](bool, QString *reason) {
                LoginStartupTarget saved;
                QString readError;
                hiddenDuringRegistration = !readLoginStartupTarget(locator, &saved, &readError)
                    && saved.profileDir.isEmpty() && readError.contains(QStringLiteral("lock"));
                *reason = QStringLiteral("registration failed");
                return false;
            }, &error));
        QVERIFY(hiddenDuringRegistration);
        QCOMPARE(storedBytes(locator), before);
        LoginStartupTarget saved;
        QVERIFY2(readLoginStartupTarget(locator, &saved, &error), qPrintable(error));
        QCOMPARE(saved.profileDir, RuntimePaths::resolve(original.profileDir, original.modelDir).profileDir);
    }

    void takeoverRejectsPreviousOwner()
    {
        TestEnvironment environment(QStringLiteral("login-owner"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto first = targetFor(environment, QStringLiteral("first"));
        const auto second = targetFor(environment, QStringLiteral("second"));
        QString error;
        auto success = [](bool, QString *) { return true; };
        QVERIFY2(updateLoginStartupTarget(locator, first, true, success, &error), qPrintable(error));
        QVERIFY2(updateLoginStartupTarget(locator, second, true, success, &error), qPrintable(error));
        const auto before = storedBytes(locator);
        bool called = false;
        QVERIFY(!updateLoginStartupTarget(locator, first, false,
            [&](bool, QString *) { called = true; return true; }, &error));
        QVERIFY(error.contains(QStringLiteral("another profile")));
        QVERIFY(!called);
        QCOMPARE(storedBytes(locator), before);
        // A model-only takeover also changes the owner.
        auto modelChanged = second;
        modelChanged.modelDir = first.modelDir;
        QVERIFY2(updateLoginStartupTarget(locator, modelChanged, true, success, &error), qPrintable(error));
        QVERIFY(!updateLoginStartupTarget(locator, second, false, success, &error));
    }

    void disablingCanonicalOwnerRemovesLocator()
    {
        TestEnvironment environment(QStringLiteral("login-disable"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        auto target = targetFor(environment, QStringLiteral("data"));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        target.profileDir += QStringLiteral("/./");
        target.modelDir += QStringLiteral("/./");
        bool disabled = false;
        bool hiddenDuringDisable = false;
        QVERIFY2(updateLoginStartupTarget(locator, target, false, [&](bool enabled, QString *) {
            disabled = !enabled;
            LoginStartupTarget saved;
            hiddenDuringDisable = !readLoginStartupTarget(locator, &saved) && saved.profileDir.isEmpty();
            return true;
        }, &error), qPrintable(error));
        QVERIFY(disabled);
        QVERIFY(hiddenDuringDisable);
        QVERIFY(!QFileInfo::exists(locator));
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
    }

    void failedDisablePreservesLocator()
    {
        TestEnvironment environment(QStringLiteral("login-disable-fail"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto target = targetFor(environment, QStringLiteral("data"));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        const auto before = storedBytes(locator);
        QVERIFY(!updateLoginStartupTarget(locator, target, false,
            [](bool, QString *reason) { *reason = QStringLiteral("unregister failed"); return false; }, &error));
        QCOMPARE(error, QStringLiteral("unregister failed"));
        QCOMPARE(storedBytes(locator), before);
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
    }

    void interruptedUpdateBlocksReadAndDisableUntilEnableRepairs()
    {
        TestEnvironment environment(QStringLiteral("login-interrupted"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto target = targetFor(environment, QStringLiteral("data"));
        QVERIFY(QDir().mkpath(target.profileDir));
        QVERIFY(QDir().mkpath(target.modelDir));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        QVERIFY2(environment.writeFile(QStringLiteral("target.json.pending"), "1\n", &error), qPrintable(error));
        LoginStartupTarget saved;
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY(error.contains(QStringLiteral("Use current directories at sign-in")));
        QVERIFY(saved.profileDir.isEmpty());
        bool called = false;
        QVERIFY(!updateLoginStartupTarget(locator, target, false,
            [&](bool, QString *) { called = true; return true; }, &error));
        QVERIFY(!called);
        QVERIFY(error.contains(QStringLiteral("unfinished update")));
        // A failed repair must not remove an older transaction's marker.
        QVERIFY(!updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return false; }, &error));
        QVERIFY(QFileInfo::exists(locator + QStringLiteral(".pending")));
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
        QVERIFY2(readLoginStartupTarget(locator, &saved, &error), qPrintable(error));
        QCOMPARE(saved.profileDir, RuntimePaths::resolve(target.profileDir, target.modelDir).profileDir);
    }

#ifdef Q_OS_WIN
    void failedLocatorRemovalRestoresNativeRegistration()
    {
        TestEnvironment environment(QStringLiteral("login-remove-fail"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto target = targetFor(environment, QStringLiteral("data"));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        const auto before = storedBytes(locator);
        // An open reader without FILE_SHARE_DELETE deterministically rejects
        // removal, independent of the test account's filesystem permissions.
        struct Handle {
            HANDLE value;
            ~Handle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
        } reader{CreateFileW(reinterpret_cast<LPCWSTR>(locator.utf16()), GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                             FILE_ATTRIBUTE_NORMAL, nullptr)};
        QVERIFY(reader.value != INVALID_HANDLE_VALUE);
        QList<bool> operations;
        QVERIFY(!updateLoginStartupTarget(locator, target, false, [&](bool enabled, QString *) {
            operations.append(enabled);
            return true;
        }, &error));
        QCOMPARE(operations, (QList<bool>{false, true}));
        QVERIFY(error.contains(QStringLiteral("previous native registration was restored")));
        QCOMPARE(storedBytes(locator), before);
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
    }

    void failedRollbackKeepsTargetBlockedUntilRepair()
    {
        TestEnvironment environment(QStringLiteral("login-rollback-blocked"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto original = targetFor(environment, QStringLiteral("original"));
        const auto replacement = targetFor(environment, QStringLiteral("replacement"));
        QVERIFY(QDir().mkpath(replacement.profileDir));
        QVERIFY(QDir().mkpath(replacement.modelDir));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, original, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        struct Handle {
            HANDLE value = INVALID_HANDLE_VALUE;
            ~Handle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
        } reader;
        QVERIFY(!updateLoginStartupTarget(locator, replacement, true, [&](bool, QString *reason) {
            // Retain a reader across rollback so QSaveFile cannot replace the
            // newly written locator after native registration has failed.
            reader.value = CreateFileW(reinterpret_cast<LPCWSTR>(locator.utf16()), GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
            *reason = QStringLiteral("registration failed");
            return false;
        }, &error));
        QVERIFY(reader.value != INVALID_HANDLE_VALUE);
        QVERIFY(error.contains(QStringLiteral("Restoring the previous login startup locator failed")));
        QVERIFY(error.contains(QStringLiteral("Use current directories at sign-in")));
        QVERIFY(QFileInfo::exists(locator + QStringLiteral(".pending")));
        LoginStartupTarget saved;
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY(saved.profileDir.isEmpty());
        CloseHandle(reader.value);
        reader.value = INVALID_HANDLE_VALUE;
        QVERIFY2(updateLoginStartupTarget(locator, replacement, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        QVERIFY(!QFileInfo::exists(locator + QStringLiteral(".pending")));
        QVERIFY2(readLoginStartupTarget(locator, &saved, &error), qPrintable(error));
        QCOMPARE(saved.profileDir, RuntimePaths::resolve(replacement.profileDir, replacement.modelDir).profileDir);
    }
#endif

    void invalidLocator_data()
    {
        QTest::addColumn<QByteArray>("contents");
        QTest::newRow("invalid-json") << QByteArray("{");
        QTest::newRow("array") << QByteArray("[]");
        QTest::newRow("missing-fields") << QByteArray("{}");
        QTest::newRow("relative-profile") << QByteArray("{\"version\":1,\"profileDir\":\"relative\",\"modelDir\":\"$ABS\"}");
        QTest::newRow("relative-models") << QByteArray("{\"version\":1,\"profileDir\":\"$ABS\",\"modelDir\":\"relative\"}");
        QTest::newRow("empty-profile") << QByteArray("{\"version\":1,\"profileDir\":\"\",\"modelDir\":\"$ABS\"}");
        QTest::newRow("wrong-version") << QByteArray("{\"version\":2,\"profileDir\":\"$ABS\",\"modelDir\":\"$ABS\"}");
        QTest::newRow("fractional-version") << QByteArray("{\"version\":1.5,\"profileDir\":\"$ABS\",\"modelDir\":\"$ABS\"}");
        QTest::newRow("string-version") << QByteArray("{\"version\":\"1\",\"profileDir\":\"$ABS\",\"modelDir\":\"$ABS\"}");
        QTest::newRow("nul") << QByteArray("{\"version\":1,\"profileDir\":\"$ABS\\u0000tail\",\"modelDir\":\"$ABS\"}");
    }

    void invalidLocator()
    {
        QFETCH(QByteArray, contents);
        TestEnvironment environment(QStringLiteral("login-invalid"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        contents.replace("$ABS", QDir::fromNativeSeparators(environment.root()).toUtf8());
        QString error;
        QVERIFY2(environment.writeFile(QStringLiteral("target.json"), contents, &error), qPrintable(error));
        const QString locator = environment.path(QStringLiteral("target.json"));
        LoginStartupTarget saved;
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(saved.profileDir.isEmpty());
        bool called = false;
        const auto target = targetFor(environment, QStringLiteral("data"));
        QVERIFY(!updateLoginStartupTarget(locator, target, false,
            [&](bool, QString *) { called = true; return true; }, &error));
        QVERIFY(!called);
        QCOMPARE(storedBytes(locator), contents);
        // Enabling can repair a corrupt locator.
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        QVERIFY(QDir().mkpath(target.profileDir));
        QVERIFY(QDir().mkpath(target.modelDir));
        QVERIFY2(readLoginStartupTarget(locator, &saved, &error), qPrintable(error));
    }

    void savedTargetMustExist_data()
    {
        QTest::addColumn<bool>("createProfile");
        QTest::addColumn<bool>("createModels");
        QTest::newRow("missing-profile") << false << true;
        QTest::newRow("missing-models") << true << false;
        QTest::newRow("both-missing") << false << false;
    }

    void savedTargetMustExist()
    {
        QFETCH(bool, createProfile);
        QFETCH(bool, createModels);
        TestEnvironment environment(QStringLiteral("login-target-missing"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto target = targetFor(environment, QStringLiteral("data"));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        if (createProfile) QVERIFY(QDir().mkpath(target.profileDir));
        if (createModels) QVERIFY(QDir().mkpath(target.modelDir));
        LoginStartupTarget saved{QStringLiteral("old-value"), QStringLiteral("old-value")};
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY(error.contains(QStringLiteral("unavailable")));
        QVERIFY(saved.profileDir.isEmpty());
        QVERIFY(saved.modelDir.isEmpty());
        QCOMPARE(QFileInfo::exists(target.profileDir), createProfile);
        QCOMPARE(QFileInfo::exists(target.modelDir), createModels);
    }

#ifdef Q_OS_UNIX
    void readOnlyModelsRemainUsable()
    {
        TestEnvironment environment(QStringLiteral("login-readonly-models"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto target = targetFor(environment, QStringLiteral("data"));
        QVERIFY(QDir().mkpath(target.profileDir));
        QVERIFY(QDir().mkpath(target.modelDir));
        QString error;
        QVERIFY2(updateLoginStartupTarget(locator, target, true,
            [](bool, QString *) { return true; }, &error), qPrintable(error));
        QVERIFY(QFile::setPermissions(target.modelDir, QFileDevice::ReadOwner | QFileDevice::ExeOwner));
        LoginStartupTarget saved;
        const bool readable = readLoginStartupTarget(locator, &saved, &error);
        // Restore before asserting, including failure, to permit fixture cleanup.
        QVERIFY(QFile::setPermissions(target.modelDir,
            QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        QVERIFY2(readable, qPrintable(error));
        QCOMPARE(saved.modelDir, RuntimePaths::resolve(target.profileDir, target.modelDir).modelDir);
    }
#endif

    void heldOperationLockRejectsUpdatesAndReads()
    {
        TestEnvironment environment(QStringLiteral("login-locked"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        QLockFile lock(locator + QStringLiteral(".lock"));
        QVERIFY(lock.tryLock());
        bool called = false;
        QString error;
        QVERIFY(!updateLoginStartupTarget(locator, targetFor(environment, QStringLiteral("data")), true,
            [&](bool, QString *) { called = true; return true; }, &error));
        QVERIFY(error.contains(QStringLiteral("lock")));
        QVERIFY(!called);
        QVERIFY(!QFileInfo::exists(locator));
        LoginStartupTarget saved;
        QVERIFY(!readLoginStartupTarget(locator, &saved, &error));
        QVERIFY(error.contains(QStringLiteral("lock")));
        QVERIFY(saved.profileDir.isEmpty());
    }

    void invalidUpdatePath_data()
    {
        QTest::addColumn<QString>("path");
        QTest::newRow("empty") << QString{};
        QTest::newRow("relative") << QStringLiteral("relative/data");
        QTest::newRow("resource") << QStringLiteral(":/data");
#ifdef Q_OS_WIN
        QTest::newRow("drive-relative") << QStringLiteral("C:data");
        QTest::newRow("current-drive-root") << QStringLiteral("/data");
        QTest::newRow("current-drive-backslash") << QStringLiteral("\\data");
        QTest::newRow("incomplete-unc") << QStringLiteral("\\\\server");
#endif
    }

    void invalidUpdatePath()
    {
        QFETCH(QString, path);
        TestEnvironment environment(QStringLiteral("login-invalid-update"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString locator = environment.path(QStringLiteral("target.json"));
        const auto target = targetFor(environment, QStringLiteral("data"));
        bool called = false;
        const auto nativeSet = [&](bool, QString *) { called = true; return true; };
        QString error;
        QVERIFY(!updateLoginStartupTarget(locator, {path, target.modelDir}, true, nativeSet, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!updateLoginStartupTarget(locator, {target.profileDir, path}, true, nativeSet, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!updateLoginStartupTarget(path, target, true, nativeSet, &error));
        QVERIFY(!error.isEmpty());
        LoginStartupTarget saved;
        QVERIFY(!readLoginStartupTarget(path, &saved, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!called);
        QVERIFY(!QFileInfo::exists(locator));
    }

    void missingLocatorReadDoesNotCreateDirectories()
    {
        TestEnvironment environment(QStringLiteral("login-missing"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        LoginStartupTarget target;
        QString error;
        QVERIFY(!readLoginStartupTarget(environment.path(QStringLiteral("absent/target.json")), &target, &error));
        QVERIFY(!error.isEmpty());
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("absent"))));
    }

    void missingLocatorCannotDisableNativeState()
    {
        TestEnvironment environment(QStringLiteral("login-disable-missing"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const auto target = targetFor(environment, QStringLiteral("data"));
        bool called = false;
        const auto nativeSet = [&](bool, QString *) { called = true; return true; };
        QString error;
        QVERIFY(!updateLoginStartupTarget(environment.path(QStringLiteral("target.json")), target,
                                          false, nativeSet, &error));
        QVERIFY(error.contains(QStringLiteral("No saved")));
        QVERIFY(!updateLoginStartupTarget(environment.path(QStringLiteral("absent/target.json")), target,
                                          false, nativeSet, &error));
        QVERIFY(error.contains(QStringLiteral("No saved")));
        QVERIFY(!QFileInfo::exists(environment.path(QStringLiteral("absent"))));
        QVERIFY(!called);
    }

    void targetUnderRegularFileIsRejected()
    {
        TestEnvironment environment(QStringLiteral("login-file-parent"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        QString error;
        QVERIFY2(environment.writeFile(QStringLiteral("plain-file"), "data", &error), qPrintable(error));
        const auto target = targetFor(environment, QStringLiteral("data"));
        const QString badPath = environment.path(QStringLiteral("plain-file/child"));
        const QString locator = environment.path(QStringLiteral("target.json"));
        bool called = false;
        const auto nativeSet = [&](bool, QString *) { called = true; return true; };
        QVERIFY(!updateLoginStartupTarget(locator, {badPath, target.modelDir}, true, nativeSet, &error));
        QVERIFY(error.contains(QStringLiteral("not a directory")));
        QVERIFY(!updateLoginStartupTarget(locator, {target.profileDir, badPath}, true, nativeSet, &error));
        QVERIFY(error.contains(QStringLiteral("not a directory")));
        QVERIFY(!called);
        QVERIFY(!QFileInfo::exists(locator));
    }
};

QTEST_GUILESS_MAIN(LoginStartupTargetTest)
#include "login_startup_target_test.moc"
