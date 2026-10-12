if(CPACK_GENERATOR MATCHES "^NSIS")
  # CMake 3.28's built-in template unconditionally asks for administrator
  # access. Our small template installs only for the current Windows user.
  list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_LIST_DIR}/nsis")
  set(CPACK_MONOLITHIC_INSTALL ON)
  set(CPACK_NSIS_INSTALL_ROOT "$LOCALAPPDATA/Programs")
  set(CPACK_NSIS_EXECUTION_LEVEL user)
elseif(CPACK_GENERATOR STREQUAL "DEB")
  set(CPACK_PACKAGING_INSTALL_PREFIX "/usr")
endif()
