add_executable(qcs-tests-recovery-child recovery_child.cpp)
target_link_libraries(qcs-tests-recovery-child PRIVATE qcs-storage SQLite::SQLite3)

foreach(_name index_repository vector_index index_repository_regression search_cancellation recovery_process)
    add_executable(qcs-tests-${_name} "${_name}_test.cpp")
    target_link_libraries(qcs-tests-${_name} PRIVATE qcs-storage
        qcs-test-support Qt6::Test SQLite::SQLite3)
    target_include_directories(qcs-tests-${_name} PRIVATE "${PROJECT_SOURCE_DIR}/tests")
    qcopilots_search_add_test(NAME qcs-tests-${_name} COMMAND $<TARGET_FILE:qcs-tests-${_name}> -o "-,txt")
    set_tests_properties(qcs-tests-${_name} PROPERTIES LABELS "storage" TIMEOUT 180)
endforeach()

target_compile_definitions(qcs-tests-recovery_process PRIVATE
    QCOPILOTS_SEARCH_RECOVERY_CHILD="$<TARGET_FILE:qcs-tests-recovery-child>")
add_dependencies(qcs-tests-recovery_process qcs-tests-recovery-child)
