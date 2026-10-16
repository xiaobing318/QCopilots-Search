qt_add_executable(qcs-tests-worker worker_tests.cpp)
target_link_libraries(qcs-tests-worker PRIVATE qcs-test-support Qt6::Test)
target_compile_definitions(qcs-tests-worker PRIVATE
    QCOPILOTS_SEARCH_TEST_WORKER_PATH="$<TARGET_FILE:qcs-worker>")
add_dependencies(qcs-tests-worker qcs-worker)
qcopilots_search_add_test(NAME qcs-tests-worker COMMAND $<TARGET_FILE:qcs-tests-worker> -o "-,txt")
set_tests_properties(qcs-tests-worker PROPERTIES
    LABELS "process;portable" TIMEOUT 180)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/cli_tests.cpp")
    qt_add_executable(qcs-tests-cli cli_tests.cpp)
    target_link_libraries(qcs-tests-cli PRIVATE qcs-test-support Qt6::Test)
    target_compile_definitions(qcs-tests-cli PRIVATE
        QCOPILOTS_SEARCH_TEST_CLI_PATH="$<TARGET_FILE:qcs-cli>"
        QCOPILOTS_SEARCH_TEST_SERVICE_PATH="$<TARGET_FILE:qcs-service>")
    add_dependencies(qcs-tests-cli qcs-cli qcs-service qcs-worker)
    qcopilots_search_add_test(NAME qcs-tests-cli COMMAND $<TARGET_FILE:qcs-tests-cli> -o "-,txt")
    set_tests_properties(qcs-tests-cli PROPERTIES
        LABELS "process;portable" TIMEOUT 180)
endif()
