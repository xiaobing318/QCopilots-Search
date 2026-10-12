set(_qcopilots_runtime_tests
  "image-sizing|image_sizing_test.cpp|unit"
  "runtime|runtime_unit_test.cpp|unit"
  "reference-vectors|reference_vectors_test.cpp|real-model")
foreach(_entry IN LISTS _qcopilots_runtime_tests)
  string(REPLACE "|" ";" _parts "${_entry}")
  list(GET _parts 0 _name)
  list(GET _parts 1 _source)
  list(GET _parts 2 _label)
  set(_target "qcopilots-search-tests-${_name}")
  add_executable("${_target}" "${_source}")
  target_link_libraries("${_target}" PRIVATE qcopilots-search-runtime Qt6::Test)
  target_compile_definitions("${_target}" PRIVATE
    QCOPILOTS_SEARCH_RUNTIME_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")
  add_test(NAME "${_target}" COMMAND "$<TARGET_FILE:${_target}>" -o "-,txt")
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
  add_test(NAME qcopilots-search-tests-reference-vectors-baseline
    COMMAND "$<TARGET_FILE:qcopilots-search-tests-reference-vectors>" vectorsMatchReference -o "-,txt")
  set_tests_properties(qcopilots-search-tests-reference-vectors-baseline PROPERTIES
    LABELS "real-model;runtime;cpu-baseline" TIMEOUT 1800 RUN_SERIAL TRUE
    ENVIRONMENT "QCOPILOTS_SEARCH_CPU_BASELINE=1;QCOPILOTS_SEARCH_MODEL_DIR=${QCOPILOTS_SEARCH_MODEL_DIR}")
endif()
