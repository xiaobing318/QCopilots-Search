qt_add_executable(qcopilots-search-worker main.cpp)
target_link_libraries(qcopilots-search-worker PRIVATE qcopilots-search-core
    qcopilots-search-extraction qcopilots-search-runtime Qt6::Concurrent)
target_compile_definitions(qcopilots-search-worker PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
if(WIN32)
    target_compile_definitions(qcopilots-search-worker PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
