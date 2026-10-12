add_library(qcopilots-search-client STATIC service_client.cpp service_client.h ipc_protocol.cpp ipc_protocol.h)
target_link_libraries(qcopilots-search-client PUBLIC qcopilots-search-core Qt6::Network)
target_include_directories(qcopilots-search-client PUBLIC "${PROJECT_SOURCE_DIR}/src")

add_library(qcopilots-search-service-core STATIC service_server.cpp service_server.h)
target_link_libraries(qcopilots-search-service-core PUBLIC qcopilots-search-client qcopilots-search-storage Qt6::Concurrent)
add_executable(qcopilots-search-service main.cpp)
target_link_libraries(qcopilots-search-service PRIVATE qcopilots-search-service-core)
