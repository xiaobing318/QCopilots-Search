qt_add_executable(qcs-cli main.cpp)
target_link_libraries(qcs-cli PRIVATE qcs-core qcs-client Qt6::Network)
target_compile_definitions(qcs-cli PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
