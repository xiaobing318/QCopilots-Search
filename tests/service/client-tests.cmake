add_executable(qcopilots-search-tests-client test_client.cpp)
target_link_libraries(qcopilots-search-tests-client PRIVATE
    qcopilots-search-client qcopilots-search-test-support Qt6::Test)
add_test(NAME qcopilots-search-tests-client COMMAND "$<TARGET_FILE:qcopilots-search-tests-client>")
set_tests_properties(qcopilots-search-tests-client PROPERTIES
    WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
    TIMEOUT 60
    LABELS "unit;integration;ipc")
