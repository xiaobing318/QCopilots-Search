set(_qcopilots_runtime_tests
  "image-sizing|image_sizing_test.cpp|unit"
  "runtime|runtime_unit_test.cpp|unit"
  "reference-vectors|reference_vectors_test.cpp|real-model")
add_executable(qcs-tests-backend-layout backend_layout_test.cpp)
target_include_directories(qcs-tests-backend-layout PRIVATE "${PROJECT_SOURCE_DIR}/src")
target_link_libraries(qcs-tests-backend-layout PRIVATE qcs-test-support Qt6::Core Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-backend-layout
  COMMAND "$<TARGET_FILE:qcs-tests-backend-layout>" -o "-,txt")
set_tests_properties(qcs-tests-backend-layout PROPERTIES LABELS "unit;runtime;package" TIMEOUT 60)
find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
qcopilots_search_add_test(NAME qcs-tests-inference-install-layout
  COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/install_layout_test.py"
    --cmake "${CMAKE_COMMAND}" --generator "${CMAKE_GENERATOR}" --compiler "${CMAKE_C_COMPILER}"
    --qt-cmake-dir "${Qt6_DIR}")
set_tests_properties(qcs-tests-inference-install-layout PROPERTIES LABELS "integration;runtime;package" TIMEOUT 180)
foreach(_entry IN LISTS _qcopilots_runtime_tests)
  string(REPLACE "|" ";" _parts "${_entry}")
  list(GET _parts 0 _name)
  list(GET _parts 1 _source)
  list(GET _parts 2 _label)
  set(_target "qcs-tests-${_name}")
  add_executable("${_target}" "${_source}")
  target_link_libraries("${_target}" PRIVATE qcs-runtime qcs-test-support Qt6::Test)
  target_compile_definitions("${_target}" PRIVATE
    QCOPILOTS_SEARCH_RUNTIME_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")
  qcopilots_search_add_test(NAME "${_target}" COMMAND "$<TARGET_FILE:${_target}>" -o "-,txt")
  set_tests_properties("${_target}" PROPERTIES LABELS "${_label};runtime" TIMEOUT 1800)
  if(_label STREQUAL "real-model")
    set_tests_properties("${_target}" PROPERTIES RUN_SERIAL TRUE)
    if(QCOPILOTS_SEARCH_MODEL_DIR)
      set_tests_properties("${_target}" PROPERTIES ENVIRONMENT
        "QCOPILOTS_SEARCH_MODEL_DIR=${QCOPILOTS_SEARCH_MODEL_DIR}")
    endif()
  endif()
endforeach()
if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(AMD64|amd64|x86_64|X86_64)$")
  qcopilots_search_add_test(NAME qcs-tests-reference-vectors-baseline
    COMMAND "$<TARGET_FILE:qcs-tests-reference-vectors>" vectorsMatchReference -o "-,txt")
  set_tests_properties(qcs-tests-reference-vectors-baseline PROPERTIES
    LABELS "real-model;runtime;cpu-baseline" TIMEOUT 1800 RUN_SERIAL TRUE
    ENVIRONMENT "QCOPILOTS_SEARCH_CPU_BASELINE=1;QCOPILOTS_SEARCH_MODEL_DIR=${QCOPILOTS_SEARCH_MODEL_DIR}")
endif()
