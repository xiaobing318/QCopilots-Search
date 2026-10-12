add_library(qcopilots-search-test-support STATIC
    test_environment.cpp test_environment.h
)
target_compile_features(qcopilots-search-test-support PUBLIC cxx_std_20)
target_include_directories(qcopilots-search-test-support PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/..")
target_link_libraries(qcopilots-search-test-support PUBLIC qcopilots-search-core Qt6::Core)
