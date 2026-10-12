add_library(qcopilots-search-storage STATIC
    index_repository.cpp index_repository.h
    text_index.cpp text_index.h
    vector_index.cpp vector_index.h)
target_compile_features(qcopilots-search-storage PUBLIC cxx_std_20)
target_include_directories(qcopilots-search-storage PUBLIC "${PROJECT_SOURCE_DIR}/src")
target_link_libraries(qcopilots-search-storage PUBLIC qcopilots-search-core Qt6::Core
    PRIVATE SQLite::SQLite3 hnswlib::hnswlib cppjieba::cppjieba)
if(QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR)
    # Match the installed layout while running from a build tree. No build-host
    # source path is compiled into a release executable as a resource fallback.
    if(CMAKE_CONFIGURATION_TYPES)
        set(_qcopilots_dict_destination "${CMAKE_RUNTIME_OUTPUT_DIRECTORY}/share/qcopilots-search/dict")
    else()
        get_filename_component(_qcopilots_runtime_parent "${CMAKE_RUNTIME_OUTPUT_DIRECTORY}" DIRECTORY)
        set(_qcopilots_dict_destination "${_qcopilots_runtime_parent}/share/qcopilots-search/dict")
    endif()
    file(COPY "${QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR}/" DESTINATION "${_qcopilots_dict_destination}")
endif()

add_executable(qcopilots-search-benchmark "${PROJECT_SOURCE_DIR}/tools/qcopilots-search-benchmark.cpp")
target_link_libraries(qcopilots-search-benchmark PRIVATE qcopilots-search-storage SQLite::SQLite3)
install(TARGETS qcopilots-search-benchmark RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
