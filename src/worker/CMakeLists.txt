qt_add_executable(qcs-worker main.cpp)
target_link_libraries(qcs-worker PRIVATE qcs-core
    qcs-extraction qcs-runtime Qt6::Concurrent)
target_compile_definitions(qcs-worker PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
if(WIN32)
    target_compile_definitions(qcs-worker PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
