add_executable(qcs-tests-client test_client.cpp)
target_link_libraries(qcs-tests-client PRIVATE
    qcs-client qcs-test-support Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-client COMMAND "$<TARGET_FILE:qcs-tests-client>")
set_tests_properties(qcs-tests-client PROPERTIES
    TIMEOUT 60
    LABELS "unit;integration;ipc")
