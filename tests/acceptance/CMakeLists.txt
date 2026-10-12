qt_add_executable(qcopilots-search-tests-file-capacity file_capacity_test.cpp)
target_link_libraries(qcopilots-search-tests-file-capacity PRIVATE qcopilots-search-core qcopilots-search-test-support Qt6::Test)
add_test(NAME qcopilots-search-tests-file-capacity COMMAND $<TARGET_FILE:qcopilots-search-tests-file-capacity> -o "-,txt")
set_tests_properties(qcopilots-search-tests-file-capacity PROPERTIES LABELS "capacity;filesystem;portable" TIMEOUT 1800)
