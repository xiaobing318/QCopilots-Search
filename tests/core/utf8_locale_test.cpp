#include "core/utf8_locale.h"
#include "support/test_environment.h"

#include <QDir>
#include <QtTest>
#include <clocale>
#include <fstream>
#include <future>
#include <string>

using namespace qcopilots_search;

class Utf8LocaleTest final : public QObject {
    Q_OBJECT
private slots:
    void cleanup()
    {
        const auto error = test::takeCleanupErrors();
        QVERIFY2(error.isEmpty(), qPrintable(error));
    }
    void restoresThreadLocale()
    {
        const std::string before = std::setlocale(LC_CTYPE, nullptr);
        const std::string numeric = std::setlocale(LC_NUMERIC, nullptr);
        const auto otherThread = [] {
            return std::async(std::launch::async, [] {
                return std::string(std::setlocale(LC_CTYPE, nullptr));
            }).get();
        };
        const std::string otherBefore = otherThread();
        {
            ScopedUtf8Locale utf8;
            QVERIFY(utf8.isValid());
            QCOMPARE(std::string(std::setlocale(LC_NUMERIC, nullptr)), numeric);
            QCOMPARE(otherThread(), otherBefore);
            {
                ScopedUtf8Locale nested;
                QVERIFY(nested.isValid());
            }
        }
        QCOMPARE(std::string(std::setlocale(LC_CTYPE, nullptr)), before);
        QCOMPARE(std::string(std::setlocale(LC_NUMERIC, nullptr)), numeric);
        QCOMPARE(otherThread(), otherBefore);
    }

    void opensUnicodePath()
    {
        test::TestEnvironment environment(QStringLiteral("utf8-locale"));
        QVERIFY2(environment.isValid(), qPrintable(environment.error()));
        const QString name = QString::fromUtf8("中文 العربية 🔎.txt");
        QString error;
        QVERIFY2(environment.writeFile(name, "upstream narrow path", &error), qPrintable(error));
        {
            ScopedUtf8Locale utf8;
            QVERIFY(utf8.isValid());
            const auto path = QDir::toNativeSeparators(environment.path(name)).toUtf8();
            std::ifstream input(path.constData(), std::ios::binary);
            QVERIFY(input.is_open());
            const std::string content((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
            QCOMPARE(content, std::string("upstream narrow path"));
        }
        QVERIFY2(environment.cleanup(&error), qPrintable(error));
    }
};

QTEST_GUILESS_MAIN(Utf8LocaleTest)
#include "utf8_locale_test.moc"
