add_library(qcs-client STATIC service_client.cpp service_client.h ipc_protocol.cpp ipc_protocol.h)
target_link_libraries(qcs-client PUBLIC qcs-core Qt6::Network)
target_include_directories(qcs-client PUBLIC "${PROJECT_SOURCE_DIR}/src")

add_library(qcs-service-core STATIC service_server.cpp service_server.h)
target_link_libraries(qcs-service-core PUBLIC qcs-client qcs-storage Qt6::Concurrent)
add_executable(qcs-service main.cpp)
target_link_libraries(qcs-service PRIVATE qcs-service-core)
target_compile_definitions(qcs-service PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
