# Every module owns its test sources. This entry point supplies an explicit
# working directory and an isolated writable root regardless of the caller's cwd.
if(NOT TARGET qcopilots-search-test-support)
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
    if(NOT _test MATCHES "^qcopilots-search-tests-")
      message(FATAL_ERROR "Owned test must use qcopilots-search-tests- prefix: ${_test}")
    endif()
    set(_working "${QCOPILOTS_SEARCH_TEST_ROOT}/working/${_test}")
    file(MAKE_DIRECTORY "${_working}")
    set_property(TEST "${_test}" DIRECTORY "${directory}" PROPERTY WORKING_DIRECTORY "${_working}")
    if(NOT _test MATCHES "^qcopilots-search-tests-semantic")
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
