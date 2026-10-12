add_executable(qcs-tests-semantic semantic_acceptance.cpp)
target_link_libraries(qcs-tests-semantic PRIVATE
  qcs-test-support qcs-runtime
  qcs-extraction qcs-storage Qt6::Gui Qt6::Pdf)
target_compile_definitions(qcs-tests-semantic PRIVATE
  QCOPILOTS_SEARCH_SEMANTIC_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}"
  QCOPILOTS_SEARCH_SEMANTIC_TESSDATA="${QCOPILOTS_SEARCH_TESSDATA_DIR}")
if(MSVC)
  target_compile_options(qcs-tests-semantic PRIVATE /utf-8)
endif()
qcopilots_search_add_test(NAME qcs-tests-semantic-manifest
  COMMAND "$<TARGET_FILE:qcs-tests-semantic>" --validate-only)
if(WIN32)
  set(_qcopilots_semantic_platform windows)
elseif(APPLE)
  set(_qcopilots_semantic_platform cocoa)
else()
  set(_qcopilots_semantic_platform offscreen)
endif()
set_tests_properties(qcs-tests-semantic-manifest PROPERTIES
  LABELS "unit;semantic" ENVIRONMENT "QT_QPA_PLATFORM=${_qcopilots_semantic_platform}")
qcopilots_search_add_test(NAME qcs-tests-semantic KEEP_SUCCESS_ARTIFACTS
  COMMAND "$<TARGET_FILE:qcs-tests-semantic>")
set_tests_properties(qcs-tests-semantic PROPERTIES
  LABELS "real-model;semantic;release-acceptance" RUN_SERIAL TRUE TIMEOUT 21600
  ENVIRONMENT "QT_QPA_PLATFORM=${_qcopilots_semantic_platform}")
