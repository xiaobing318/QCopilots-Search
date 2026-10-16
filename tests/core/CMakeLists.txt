foreach(test_name IN ITEMS core file_catalog model_files utf8_locale)
    string(REPLACE "_" "-" target_suffix "${test_name}")
    set(target_name "qcs-tests-${target_suffix}")
    add_executable("${target_name}" "${test_name}_test.cpp")
    target_link_libraries("${target_name}" PRIVATE
        qcs-core qcs-test-support Qt6::Test Qt6::Network)
    if(test_name STREQUAL "file_catalog")
        target_link_libraries("${target_name}" PRIVATE qcs::git2)
    endif()
    qcopilots_search_add_test(NAME "${target_name}" COMMAND "$<TARGET_FILE:${target_name}>" -o "-,txt")
    set_tests_properties("${target_name}" PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
        TIMEOUT 180
        LABELS "unit;core")
endforeach()
