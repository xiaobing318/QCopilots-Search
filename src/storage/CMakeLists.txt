add_library(qcs-storage STATIC
    index_repository.cpp index_repository.h
    text_index.cpp text_index.h
    vector_index.cpp vector_index.h)
target_compile_features(qcs-storage PUBLIC cxx_std_20)
target_include_directories(qcs-storage PUBLIC "${PROJECT_SOURCE_DIR}/src")
target_link_libraries(qcs-storage PUBLIC qcs-core Qt6::Core
    PRIVATE SQLite::SQLite3 hnswlib::hnswlib cppjieba::cppjieba)
if(QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR)
    # Match the installed layout while running from a build tree. No build-host
    # source path is compiled into a release executable as a resource fallback.
    if(CMAKE_CONFIGURATION_TYPES)
        set(_qcopilots_dict_destination "${CMAKE_RUNTIME_OUTPUT_DIRECTORY}/share/qcs/dict")
    else()
        get_filename_component(_qcopilots_runtime_parent "${CMAKE_RUNTIME_OUTPUT_DIRECTORY}" DIRECTORY)
        set(_qcopilots_dict_destination "${_qcopilots_runtime_parent}/share/qcs/dict")
    endif()
    file(COPY "${QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR}/" DESTINATION "${_qcopilots_dict_destination}")
endif()

add_executable(qcs-benchmark "${PROJECT_SOURCE_DIR}/tools/qcs-benchmark.cpp")
target_link_libraries(qcs-benchmark PRIVATE qcs-storage SQLite::SQLite3)
install(TARGETS qcs-benchmark RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
