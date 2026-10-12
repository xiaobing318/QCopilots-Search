qt_add_executable(qcs-tests-file-capacity file_capacity_test.cpp)
target_link_libraries(qcs-tests-file-capacity PRIVATE qcs-core qcs-test-support Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-file-capacity COMMAND $<TARGET_FILE:qcs-tests-file-capacity> -o "-,txt")
set_tests_properties(qcs-tests-file-capacity PROPERTIES LABELS "capacity;filesystem;portable" TIMEOUT 1800)
