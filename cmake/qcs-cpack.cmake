# Every distribution contains the whole application. Component installation
# would include EXCLUDE_FROM_ALL developer exports and could sign the macOS
# bundle before a later component writes runtime resources into it.
set(CPACK_MONOLITHIC_INSTALL ON)
if(DEFINED CPACK_QCS_SOURCE_DIR)
  set(Python3_EXECUTABLE "${CPACK_QCS_PYTHON_EXECUTABLE}")
  include("${CMAKE_CURRENT_LIST_DIR}/qcs-output-paths.cmake")
  foreach(_root CPACK_PACKAGE_DIRECTORY CPACK_TOPLEVEL_DIRECTORY)
    if(DEFINED ${_root} AND NOT "${${_root}}" STREQUAL "")
      qcopilots_search_require_external_path("${_root}" "${${_root}}" "${CPACK_QCS_SOURCE_DIR}")
    endif()
  endforeach()
endif()

if(CPACK_GENERATOR MATCHES "^NSIS")
  # CMake 3.28's built-in template unconditionally asks for administrator
  # access. Our small template installs only for the current Windows user.
  list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_LIST_DIR}/nsis")
  set(CPACK_NSIS_INSTALL_ROOT "$LOCALAPPDATA/Programs")
  set(CPACK_NSIS_EXECUTION_LEVEL user)
  # CPack writes UTF-8 scripts without a BOM. NSIS otherwise interprets paths
  # using the host ANSI code page, breaking a checkout containing Chinese text.
  set(CPACK_NSIS_EXECUTABLE_PRE_ARGUMENTS "INPUTCHARSET UTF8")
elseif(CPACK_GENERATOR STREQUAL "DEB")
  set(CPACK_PACKAGING_INSTALL_PREFIX "/usr")
endif()
