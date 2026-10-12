include_guard(GLOBAL)

function(qcopilots_search_install_inference_runtime)
  cmake_parse_arguments(PARSE_ARGV 0 _runtime "" "" "LIBRARIES;CPU_BACKENDS")
  if(_runtime_LIBRARIES)
    install(TARGETS ${_runtime_LIBRARIES}
      RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}"
      LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}"
      ARCHIVE EXCLUDE_FROM_ALL)
  endif()
  if(_runtime_CPU_BACKENDS)
    set(_backend_directory "${CMAKE_INSTALL_LIBDIR}")
    if(APPLE)
      cmake_path(GET CMAKE_INSTALL_BINDIR PARENT_PATH _bundle_contents)
      set(_backend_directory "${_bundle_contents}/PlugIns/ggml")
      # CPU modules are loaded with dlopen and must be inside macdeployqt's
      # signed PlugIns tree. Their linked base libraries stay in Frameworks.
      set_target_properties(${_runtime_CPU_BACKENDS} PROPERTIES
        INSTALL_RPATH "@loader_path/../../Frameworks")
    endif()
    install(TARGETS ${_runtime_CPU_BACKENDS}
      RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}"
      LIBRARY DESTINATION "${_backend_directory}"
      ARCHIVE EXCLUDE_FROM_ALL)
  endif()
endfunction()
