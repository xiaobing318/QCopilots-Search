include_guard(GLOBAL)

function(qcopilots_search_install_linux_desktop)
  install(FILES "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../resources/com.qcopilots.qcopilots-search.desktop"
    DESTINATION "${CMAKE_INSTALL_DATAROOTDIR}/applications")
endfunction()

function(qcopilots_search_linux_deploy_arguments output)
  # CMake installs, rewrites the RPATH and optionally strips these targets
  # before Qt deploys dependencies. A second raw copy from the build tree
  # would undo that work, including when `cmake --install --strip` is used.
  set(_libraries)
  set(_modules)
  set(_excluded)
  set(_targets ${ARGN})
  # Dynamically loaded GPU modules have their own verified dependency closure.
  # Qt must not traverse their SDK paths and flatten a second copy of ROCm's
  # component directories into lib, where relative BLAS resources would break.
  get_property(_gpu_targets GLOBAL PROPERTY QCOPILOTS_SEARCH_INFERENCE_GPU_RUNTIME_TARGETS)
  if(_gpu_targets)
    list(REMOVE_ITEM _targets ${_gpu_targets})
  endif()
  list(REMOVE_DUPLICATES _targets)
  foreach(_target IN LISTS _targets)
    if(NOT TARGET "${_target}")
      message(FATAL_ERROR "Unknown installed Linux runtime target: ${_target}")
    endif()
    get_target_property(_type "${_target}" TYPE)
    if(_type STREQUAL "SHARED_LIBRARY")
      string(APPEND _libraries " \"$<TARGET_FILE:${_target}>\"")
    elseif(_type STREQUAL "MODULE_LIBRARY")
      string(APPEND _modules " \"$<TARGET_FILE:${_target}>\"")
    else()
      continue()
    endif()
    string(APPEND _excluded " \"$<TARGET_FILE:${_target}>\"")
  endforeach()

  # POST_EXCLUDE_FILES also stops recursive traversal. Register every owned
  # library/module as an explicit scan root so its other dependencies still
  # reach the package. CMake resolves symlinks when matching excluded files.
  set(_arguments "")
  if(_libraries)
    string(APPEND _arguments " ADDITIONAL_LIBRARIES${_libraries}\n")
  endif()
  if(_modules)
    string(APPEND _arguments " ADDITIONAL_MODULES${_modules}\n")
  endif()
  if(_excluded)
    string(APPEND _arguments " POST_EXCLUDE_FILES${_excluded}\n")
  endif()
  set("${output}" "${_arguments}" PARENT_SCOPE)
endfunction()
