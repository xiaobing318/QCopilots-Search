# Each actual CTest invocation gets an atomically created working directory.
# Configuration-time directories named only after a test are shared by two
# concurrent invocations and must never hold writable test state.
find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
function(qcopilots_search_add_test)
  cmake_parse_arguments(PARSE_ARGV 0 _test "COMMAND_EXPAND_LISTS;KEEP_SUCCESS_ARTIFACTS" "NAME;WORKING_DIRECTORY;EVIDENCE_KIND" "COMMAND;CONFIGURATIONS")
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
  if(NOT _test_EVIDENCE_KIND)
    list(GET _test_COMMAND 0 _program)
    if(_program STREQUAL "${Python3_EXECUTABLE}")
      set(_test_EVIDENCE_KIND python-unittest)
    elseif(_program MATCHES "^\\$<TARGET_FILE:")
      set(_test_EVIDENCE_KIND qttest)
    else()
      message(FATAL_ERROR "Test ${_test_NAME} requires an explicit EVIDENCE_KIND")
    endif()
  endif()
  if(NOT _test_EVIDENCE_KIND MATCHES "^(qttest|python-unittest|program)$")
    message(FATAL_ERROR "Invalid test evidence kind: ${_test_EVIDENCE_KIND}")
  endif()
  # Keep metadata separate until all callers have assigned their LABELS.
  set_property(TEST "${_test_NAME}" PROPERTY _QCS_EVIDENCE_KIND "${_test_EVIDENCE_KIND}")
endfunction()

if(NOT TARGET qcs-test-support)
  add_subdirectory(support)
endif()

foreach(_qcopilots_test_module core runtime extraction storage service processes gui semantic)
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
    get_property(_evidence_kind TEST "${_test}" DIRECTORY "${directory}" PROPERTY _QCS_EVIDENCE_KIND)
    if(NOT _evidence_kind)
      message(FATAL_ERROR "Test has no evidence policy: ${_test}")
    endif()
    set_property(TEST "${_test}" DIRECTORY "${directory}" APPEND PROPERTY LABELS "${_evidence_kind}")
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
# Some top-level tooling tests are declared after add_subdirectory(tests).
# Finalize all labels only after every registration has completed.
cmake_language(DEFER DIRECTORY "${PROJECT_SOURCE_DIR}" CALL
  qcopilots_search_configure_test_directory "${PROJECT_SOURCE_DIR}")

qcopilots_search_add_test(NAME qcs-tests-acceptance-runners
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/acceptance/qcs-test-runners.py" --suite pure)
set_tests_properties(qcs-tests-acceptance-runners PROPERTIES LABELS "unit;tooling;acceptance" TIMEOUT 240)
if(WIN32)
  qcopilots_search_add_test(NAME qcs-tests-acceptance-native
    COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/acceptance/qcs-test-runners.py" --suite native)
  # Real ACL restoration, process token recovery and owned timeout cleanup.
  set_tests_properties(qcs-tests-acceptance-native PROPERTIES LABELS "tooling;acceptance;native;slow" TIMEOUT 240)
endif()

qcopilots_search_add_test(NAME qcs-tests-ci-evidence
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/.github/scripts/qcs-tests-ci-evidence.py" -v)
qcopilots_search_add_test(NAME qcs-tests-deb-ci
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/.github/scripts/qcs-tests-deb-ci.py" -v)
qcopilots_search_add_test(NAME qcs-tests-release-evidence
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/.github/scripts/qcs-tests-release-evidence.py" -v)
qcopilots_search_add_test(NAME qcs-tests-test-policy
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-policy-tests.py" -v)
qcopilots_search_add_test(NAME qcs-tests-check
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-check.py" -v)
qcopilots_search_add_test(NAME qcs-tests-artifact-selection
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/.github/scripts/qcs-tests-artifact-selection.py" -v)
set_tests_properties(qcs-tests-ci-evidence qcs-tests-deb-ci qcs-tests-release-evidence qcs-tests-test-policy qcs-tests-check
  qcs-tests-artifact-selection
  PROPERTIES LABELS "unit;tooling;evidence" TIMEOUT 240)

qcopilots_search_add_test(NAME qcs-tests-runner
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-runner-tests.py")
qcopilots_search_add_test(NAME qcs-tests-path-budget
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-path-budget.py")
qcopilots_search_add_test(NAME qcs-tests-windows-package
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-package-windows-tests.py" -v)
qcopilots_search_add_test(NAME qcs-tests-package-variants
  COMMAND "${Python3_EXECUTABLE}" -B "${PROJECT_SOURCE_DIR}/tools/qcs-test-package-variants.py")
set_tests_properties(qcs-tests-windows-package qcs-tests-package-variants PROPERTIES
  LABELS "unit;tooling;package" TIMEOUT 240)
if(CMAKE_GENERATOR MATCHES "Ninja")
  set_property(TEST qcs-tests-path-budget APPEND PROPERTY ENVIRONMENT
    "QCOPILOTS_SEARCH_NINJA=${CMAKE_MAKE_PROGRAM}")
endif()
set_tests_properties(qcs-tests-runner qcs-tests-path-budget PROPERTIES LABELS "unit;tooling" TIMEOUT 120)
qcopilots_search_add_test(NAME qcs-tests-temp-paths
  COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/support/temp_paths_test.py")
set_tests_properties(qcs-tests-temp-paths PROPERTIES LABELS "unit;portability" TIMEOUT 60)
