add_executable(qcopilots-search-tests-service test_service.cpp)
target_link_libraries(qcopilots-search-tests-service PRIVATE qcopilots-search-service-core qcopilots-search-test-support Qt6::Test)
add_dependencies(qcopilots-search-tests-service qcopilots-search-service qcopilots-search-worker)
add_test(NAME qcopilots-search-tests-service COMMAND qcopilots-search-tests-service -o "-,txt")
set_tests_properties(qcopilots-search-tests-service PROPERTIES
  WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
  LABELS "integration;service;process;portability"
  TIMEOUT 180
  ENVIRONMENT "QCOPILOTS_SEARCH_TEST_SERVICE=$<TARGET_FILE:qcopilots-search-service>")
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/client-tests.cmake")
  include("${CMAKE_CURRENT_SOURCE_DIR}/client-tests.cmake")
endif()
