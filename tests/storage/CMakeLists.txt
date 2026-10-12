add_executable(qcopilots-search-tests-recovery-child recovery_child.cpp)
target_link_libraries(qcopilots-search-tests-recovery-child PRIVATE qcopilots-search-storage SQLite::SQLite3)

foreach(_name index_repository vector_index index_repository_regression search_cancellation recovery_process)
    add_executable(qcopilots-search-tests-${_name} "${_name}_test.cpp")
    target_link_libraries(qcopilots-search-tests-${_name} PRIVATE qcopilots-search-storage
        qcopilots-search-test-support Qt6::Test SQLite::SQLite3)
    target_include_directories(qcopilots-search-tests-${_name} PRIVATE "${PROJECT_SOURCE_DIR}/tests")
    add_test(NAME qcopilots-search-tests-${_name} COMMAND $<TARGET_FILE:qcopilots-search-tests-${_name}> -o "-,txt")
    set_tests_properties(qcopilots-search-tests-${_name} PROPERTIES LABELS "storage" TIMEOUT 180)
endforeach()

target_compile_definitions(qcopilots-search-tests-recovery_process PRIVATE
    QCOPILOTS_SEARCH_RECOVERY_CHILD="$<TARGET_FILE:qcopilots-search-tests-recovery-child>")
add_dependencies(qcopilots-search-tests-recovery_process qcopilots-search-tests-recovery-child)
