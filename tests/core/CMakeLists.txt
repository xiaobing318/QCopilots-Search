foreach(test_name IN ITEMS core file_catalog model_files)
    string(REPLACE "_" "-" target_suffix "${test_name}")
    set(target_name "qcopilots-search-tests-${target_suffix}")
    add_executable("${target_name}" "${test_name}_test.cpp")
    target_link_libraries("${target_name}" PRIVATE
        qcopilots-search-core qcopilots-search-test-support Qt6::Test Qt6::Network)
    if(test_name STREQUAL "file_catalog")
        target_link_libraries("${target_name}" PRIVATE qcopilots-search::git2)
    endif()
    add_test(NAME "${target_name}" COMMAND "$<TARGET_FILE:${target_name}>" -o "-,txt")
    set_tests_properties("${target_name}" PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
        TIMEOUT 180
        LABELS "unit;core")
endforeach()
