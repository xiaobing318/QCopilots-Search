include_guard(GLOBAL)
# Windows does not have an ELF-style build RPATH. Copy precisely the native
# runtime targets into the application's build directory, without searching PATH.
if(WIN32)
  if(TARGET qcs-gui)
    set(_qcopilots_binary_directory "$<TARGET_FILE_DIR:qcs-gui>")
  else()
    set(_qcopilots_binary_directory "${CMAKE_RUNTIME_OUTPUT_DIRECTORY}")
  endif()
  get_property(_qcopilots_native GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_RUNTIME_TARGETS)
  get_property(_qcopilots_imported GLOBAL PROPERTY QCOPILOTS_SEARCH_IMAGE_IMPORTED_RUNTIME_TARGETS)
  set(_qcopilots_runtime_targets ${_qcopilots_native} ${_qcopilots_imported})
  set(_qcopilots_commands)
  set(_qcopilots_dependencies)
  foreach(_runtime IN LISTS _qcopilots_runtime_targets)
    if(TARGET ${_runtime})
      get_target_property(_kind ${_runtime} TYPE)
      if(_kind STREQUAL "SHARED_LIBRARY" OR _kind STREQUAL "MODULE_LIBRARY")
        list(APPEND _qcopilots_commands COMMAND "${CMAKE_COMMAND}" -E copy_if_different
          "$<TARGET_FILE:${_runtime}>" "${_qcopilots_binary_directory}/$<TARGET_FILE_NAME:${_runtime}>")
        list(APPEND _qcopilots_dependencies ${_runtime})
      endif()
    endif()
  endforeach()
  if(_qcopilots_commands)
    add_custom_target(qcs-deploy-development ALL
      COMMAND "${CMAKE_COMMAND}" -E make_directory "${_qcopilots_binary_directory}"
      ${_qcopilots_commands} DEPENDS ${_qcopilots_dependencies} VERBATIM)
  endif()
endif()
if(APPLE AND TARGET qcs-gui)
  get_property(_qcopilots_native GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_RUNTIME_TARGETS)
  get_property(_qcopilots_imported GLOBAL PROPERTY QCOPILOTS_SEARCH_IMAGE_IMPORTED_RUNTIME_TARGETS)
  get_property(_qcopilots_inference GLOBAL PROPERTY QCOPILOTS_SEARCH_INFERENCE_RUNTIME_TARGETS)
  get_property(_qcopilots_inference_cpu GLOBAL PROPERTY QCOPILOTS_SEARCH_INFERENCE_CPU_RUNTIME_TARGETS)
  get_property(_qcopilots_inference_gpu GLOBAL PROPERTY QCOPILOTS_SEARCH_INFERENCE_GPU_RUNTIME_TARGETS)
  set(_qcopilots_bundle_commands COMMAND "${CMAKE_COMMAND}" -E make_directory
    "$<TARGET_FILE_DIR:qcs-gui>/../Frameworks"
    "$<TARGET_FILE_DIR:qcs-gui>/../PlugIns/ggml")
  set(_qcopilots_bundle_dependencies qcs-gui)
  foreach(_helper qcs-service qcs-worker qcs-cli)
    if(TARGET ${_helper})
      list(APPEND _qcopilots_bundle_dependencies ${_helper})
      list(APPEND _qcopilots_bundle_commands COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "$<TARGET_FILE:${_helper}>" "$<TARGET_FILE_DIR:qcs-gui>/$<TARGET_FILE_NAME:${_helper}>")
    endif()
  endforeach()
  foreach(_library IN LISTS _qcopilots_native _qcopilots_imported _qcopilots_inference)
    if(TARGET ${_library})
      get_target_property(_kind ${_library} TYPE)
      if(_kind STREQUAL "SHARED_LIBRARY" OR _kind STREQUAL "MODULE_LIBRARY")
        list(APPEND _qcopilots_bundle_dependencies ${_library})
        if(_library IN_LIST _qcopilots_inference_cpu OR _library IN_LIST _qcopilots_inference_gpu)
          set(_qcopilots_library_directory "PlugIns/ggml")
          # Remove only this generated target's obsolete development copy.
          # Leaving it in Frameworks would also copy an unsigned duplicate
          # into subsequent installs of a reused development bundle.
          list(APPEND _qcopilots_bundle_commands COMMAND "${CMAKE_COMMAND}" -E rm -f
            "$<TARGET_FILE_DIR:qcs-gui>/../Frameworks/$<TARGET_FILE_NAME:${_library}>")
        else()
          set(_qcopilots_library_directory "Frameworks")
        endif()
        list(APPEND _qcopilots_bundle_commands COMMAND "${CMAKE_COMMAND}" -E copy_if_different
          "$<TARGET_FILE:${_library}>" "$<TARGET_FILE_DIR:qcs-gui>/../${_qcopilots_library_directory}/$<TARGET_FILE_NAME:${_library}>")
      endif()
    endif()
  endforeach()
  add_custom_target(qcs-deploy-development ALL
    ${_qcopilots_bundle_commands} DEPENDS ${_qcopilots_bundle_dependencies} VERBATIM)
endif()
