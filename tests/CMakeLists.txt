# Each actual CTest invocation gets an atomically created working directory.
# Configuration-time directories named only after a test are shared by two
# concurrent invocations and must never hold writable test state.
find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
function(qcopilots_search_add_test)
  cmake_parse_arguments(PARSE_ARGV 0 _test "COMMAND_EXPAND_LISTS;KEEP_SUCCESS_ARTIFACTS" "NAME;WORKING_DIRECTORY" "COMMAND;CONFIGURATIONS")
  if(NOT _test_NAME MATCHES "^qcs-tests-" OR NOT _test_COMMAND)
    message(FATAL_ERROR "Owned tests require a qcs-tests- name and an explicit command")
  endif()
  set(_extra)
  if(_test_COMMAND_EXPAND_LISTS)
    list(APPEND _extra COMMAND_EXPAND_LISTS)
  endif()
  if(_test_CONFIGURATIONS)
    list(APPEND _extra CONFIGURATIONS ${_test_CONFIGURATIONS})
  endif()
  set(_runner_options)
  if(_test_KEEP_SUCCESS_ARTIFACTS)
    list(APPEND _runner_options --keep-success)
  endif()
  add_test(NAME "${_test_NAME}" COMMAND "${Python3_EXECUTABLE}" -B
    "${PROJECT_SOURCE_DIR}/tools/qcs-test-runner.py"
    --label "${_test_NAME}" --root "${QCOPILOTS_SEARCH_TEST_ROOT}" ${_runner_options}
    -- ${_test_COMMAND} ${_extra})
  set_tests_properties("${_test_NAME}" PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}")
endfunction()

if(NOT TARGET qcs-test-support)
  add_subdirectory(support)
endif()

foreach(_qcopilots_test_module core runtime extraction storage service processes gui acceptance semantic)
  if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${_qcopilots_test_module}/CMakeLists.txt")
    add_subdirectory("${_qcopilots_test_module}")
  endif()
endforeach()

function(qcopilots_search_configure_test_directory directory)
  get_property(_tests DIRECTORY "${directory}" PROPERTY TESTS)
  foreach(_test IN LISTS _tests)
    if(NOT _test MATCHES "^qcs-tests-")
      message(FATAL_ERROR "Owned test must use qcs-tests- prefix: ${_test}")
    endif()
    if(NOT _test MATCHES "^qcs-tests-semantic")
      # Qt 6.12's plain Windows logger otherwise chooses OutputDebugStringA
      # even for a console executable whose stdout is captured by CTest.
      set_property(TEST "${_test}" DIRECTORY "${directory}" APPEND PROPERTY
        ENVIRONMENT_MODIFICATION "QT_FORCE_STDERR_LOGGING=set:1")
    endif()
    # Do not override inherited per-run environment. The developer runner gives
    # each invocation its own test root. Direct CTest runs use QTemporaryDir's
    # system root unless the caller explicitly provides an alternative.
  endforeach()
  get_property(_children DIRECTORY "${directory}" PROPERTY SUBDIRECTORIES)
  foreach(_child IN LISTS _children)
    qcopilots_search_configure_test_directory("${_child}")
  endforeach()
endfunction()
qcopilots_search_configure_test_directory("${CMAKE_CURRENT_SOURCE_DIR}")

qcopilots_search_add_test(NAME qcs-tests-acceptance-runners
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/acceptance/qcs-test-runners.py")
set_tests_properties(qcs-tests-acceptance-runners PROPERTIES LABELS "unit;tooling;acceptance" TIMEOUT 120)

qcopilots_search_add_test(NAME qcs-tests-runner
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-runner-tests.py")
qcopilots_search_add_test(NAME qcs-tests-path-budget
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-path-budget.py")
if(CMAKE_GENERATOR MATCHES "Ninja")
  set_property(TEST qcs-tests-path-budget APPEND PROPERTY ENVIRONMENT
    "QCOPILOTS_SEARCH_NINJA=${CMAKE_MAKE_PROGRAM}")
endif()
set_tests_properties(qcs-tests-runner qcs-tests-path-budget PROPERTIES LABELS "unit;tooling" TIMEOUT 120)
qcopilots_search_add_test(NAME qcs-tests-temp-paths
  COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/support/temp_paths_test.py")
set_tests_properties(qcs-tests-temp-paths PROPERTIES LABELS "unit;portability" TIMEOUT 60)
