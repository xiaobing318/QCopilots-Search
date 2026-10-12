add_executable(qcopilots-search-tests-semantic semantic_acceptance.cpp)
target_link_libraries(qcopilots-search-tests-semantic PRIVATE
  qcopilots-search-test-support qcopilots-search-runtime
  qcopilots-search-extraction qcopilots-search-storage Qt6::Gui Qt6::Pdf)
target_compile_definitions(qcopilots-search-tests-semantic PRIVATE
  QCOPILOTS_SEARCH_SEMANTIC_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}"
  QCOPILOTS_SEARCH_SEMANTIC_TESSDATA="${QCOPILOTS_SEARCH_TESSDATA_DIR}")
if(MSVC)
  target_compile_options(qcopilots-search-tests-semantic PRIVATE /utf-8)
endif()
add_test(NAME qcopilots-search-tests-semantic-manifest
  COMMAND "$<TARGET_FILE:qcopilots-search-tests-semantic>" --validate-only)
if(WIN32)
  set(_qcopilots_semantic_platform windows)
elseif(APPLE)
  set(_qcopilots_semantic_platform cocoa)
else()
  set(_qcopilots_semantic_platform offscreen)
endif()
set_tests_properties(qcopilots-search-tests-semantic-manifest PROPERTIES
  LABELS "unit;semantic" ENVIRONMENT "QT_QPA_PLATFORM=${_qcopilots_semantic_platform}"
  WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}")
add_test(NAME qcopilots-search-tests-semantic
  COMMAND "$<TARGET_FILE:qcopilots-search-tests-semantic>")
set_tests_properties(qcopilots-search-tests-semantic PROPERTIES
  LABELS "real-model;semantic;release-acceptance" RUN_SERIAL TRUE TIMEOUT 21600
  WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
  ENVIRONMENT "QT_QPA_PLATFORM=${_qcopilots_semantic_platform}")
