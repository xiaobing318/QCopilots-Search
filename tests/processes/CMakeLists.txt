qt_add_executable(qcopilots-search-tests-worker worker_tests.cpp)
target_link_libraries(qcopilots-search-tests-worker PRIVATE qcopilots-search-test-support Qt6::Test)
target_compile_definitions(qcopilots-search-tests-worker PRIVATE
    QCOPILOTS_SEARCH_TEST_WORKER_PATH="$<TARGET_FILE:qcopilots-search-worker>")
add_dependencies(qcopilots-search-tests-worker qcopilots-search-worker)
add_test(NAME qcopilots-search-tests-worker COMMAND qcopilots-search-tests-worker -o "-,txt")
set_tests_properties(qcopilots-search-tests-worker PROPERTIES
    WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}" LABELS "process;portable" TIMEOUT 180)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/cli_tests.cpp")
    qt_add_executable(qcopilots-search-tests-cli cli_tests.cpp)
    target_link_libraries(qcopilots-search-tests-cli PRIVATE qcopilots-search-test-support Qt6::Test)
    target_compile_definitions(qcopilots-search-tests-cli PRIVATE
        QCOPILOTS_SEARCH_TEST_CLI_PATH="$<TARGET_FILE:qcopilots-search-cli>"
        QCOPILOTS_SEARCH_TEST_SERVICE_PATH="$<TARGET_FILE:qcopilots-search-service>")
    add_dependencies(qcopilots-search-tests-cli qcopilots-search-cli qcopilots-search-service qcopilots-search-worker)
    add_test(NAME qcopilots-search-tests-cli COMMAND qcopilots-search-tests-cli -o "-,txt")
    set_tests_properties(qcopilots-search-tests-cli PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}" LABELS "process;portable" TIMEOUT 180)
endif()
