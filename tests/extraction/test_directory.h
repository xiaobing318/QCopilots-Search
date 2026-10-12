#pragma once
#include "../support/test_environment.h"
#include <QtTest>

namespace qcopilots_search::test {
// Preserve the case's own files on failure or explicit request. QTemporaryDir
// supplies uniqueness and removes only its own directory on a successful run.
class ExtractionDirectory final : public TestTemporaryDir {
public:
    using TestTemporaryDir::TestTemporaryDir;
    ~ExtractionDirectory()
    {
        if (isValid() && (QTest::currentTestFailed()
            || qEnvironmentVariableIntValue("QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS") == 1)) {
            setAutoRemove(false);
            qInfo().noquote() << "Preserved extraction test artifacts:" << path();
        }
    }
};
}
