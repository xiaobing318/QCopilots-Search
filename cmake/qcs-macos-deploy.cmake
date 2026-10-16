include_guard(GLOBAL)

# A development .app contains copies of the original build libraries. Installing
# that whole bundle can overwrite the libraries CMake already installed with
# their install RPATH. Finish target installation after the bundle copy and
# before macdeployqt sees it, including the CPU modules loaded with dlopen.
function(qcopilots_search_install_macos_runtime_after_bundle)
  cmake_parse_arguments(PARSE_ARGV 0 _runtime "" "" "NATIVE_TARGETS;INFERENCE_TARGETS;CPU_TARGETS;GPU_TARGETS")
  if(NOT APPLE)
    message(FATAL_ERROR "The macOS runtime finalization requires an Apple target")
  endif()
  set(libraries ${_runtime_NATIVE_TARGETS} ${_runtime_INFERENCE_TARGETS})
  list(REMOVE_DUPLICATES libraries)
  if(_runtime_CPU_TARGETS)
    list(REMOVE_ITEM libraries ${_runtime_CPU_TARGETS})
  endif()
  if(_runtime_GPU_TARGETS)
    list(REMOVE_ITEM libraries ${_runtime_GPU_TARGETS})
  endif()
  set(shared_libraries)
  foreach(library IN LISTS libraries)
    get_target_property(kind "${library}" TYPE)
    get_target_property(imported "${library}" IMPORTED)
    if(NOT imported AND (kind STREQUAL "SHARED_LIBRARY" OR kind STREQUAL "MODULE_LIBRARY"))
      list(APPEND shared_libraries "${library}")
    endif()
  endforeach()
  include("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../src/runtime/install_inference_runtime.cmake")
  qcopilots_search_install_inference_runtime(
    LIBRARIES ${shared_libraries} CPU_BACKENDS ${_runtime_CPU_TARGETS} GPU_BACKENDS ${_runtime_GPU_TARGETS})
endfunction()
