qt_add_executable(qcopilots-search-cli main.cpp)
target_link_libraries(qcopilots-search-cli PRIVATE qcopilots-search-core qcopilots-search-client Qt6::Network)
target_compile_definitions(qcopilots-search-cli PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
