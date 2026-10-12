add_executable(qcs-tests-service test_service.cpp)
target_link_libraries(qcs-tests-service PRIVATE qcs-service-core qcs-test-support Qt6::Test)
add_dependencies(qcs-tests-service qcs-service qcs-worker)
qcopilots_search_add_test(NAME qcs-tests-service COMMAND $<TARGET_FILE:qcs-tests-service> -o "-,txt")
set_tests_properties(qcs-tests-service PROPERTIES
  LABELS "integration;service;process;portability"
  TIMEOUT 180
  ENVIRONMENT "QCOPILOTS_SEARCH_TEST_SERVICE=$<TARGET_FILE:qcs-service>")
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/client-tests.cmake")
  include("${CMAKE_CURRENT_SOURCE_DIR}/client-tests.cmake")
endif()
