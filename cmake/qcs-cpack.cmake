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
  if(CPACK_QCS_SYSTEM_NAME STREQUAL "Linux" AND CPACK_QCS_GPU_BACKEND STREQUAL "cuda")
    # CUDA's optional backend references the display driver's SONAME. Analyze
    # it using an SDK stub copy outside every payload, never a runtime path.
    set(_qcs_analysis_config "${CPACK_BUILD_CONFIG}")
    if(_qcs_analysis_config STREQUAL "")
      set(_qcs_analysis_config "${CPACK_QCS_BUILD_TYPE}")
    endif()
    if(NOT _qcs_analysis_config MATCHES "^[A-Za-z0-9_.+-]+$")
      message(FATAL_ERROR "CUDA DEB analysis requires an explicit build configuration")
    endif()
    if(NOT CPACK_QCS_VARIANT MATCHES "^linux-x64-cuda-[0-9]+\\.[0-9]+$")
      message(FATAL_ERROR "CUDA DEB analysis requires a Linux x64 CUDA variant")
    endif()
    if(NOT IS_ABSOLUTE "${CPACK_QCS_BUILD_DIR}" OR NOT EXISTS "${CPACK_QCS_GPU_SDK_MANIFEST}")
      message(FATAL_ERROR "CUDA DEB analysis requires the external build directory and verified SDK manifest")
    endif()
    set(_qcs_analysis "${CPACK_QCS_BUILD_DIR}/qcs-deb-analysis/${CPACK_QCS_VARIANT}/${_qcs_analysis_config}")
    qcopilots_search_require_external_path(CPACK_QCS_DEB_ANALYSIS_DIRECTORY "${_qcs_analysis}" "${CPACK_QCS_SOURCE_DIR}")
    set(_qcs_analysis_paths "${_qcs_analysis}")
    set(_qcs_analysis_boundaries)
    foreach(_qcs_boundary CPACK_QCS_INSTALL_PREFIX CPACK_PACKAGE_DIRECTORY CPACK_TOPLEVEL_DIRECTORY CPACK_TEMPORARY_DIRECTORY)
      if(DEFINED ${_qcs_boundary} AND NOT "${${_qcs_boundary}}" STREQUAL "")
        list(APPEND _qcs_analysis_paths "${${_qcs_boundary}}")
        list(APPEND _qcs_analysis_boundaries "${_qcs_boundary}")
      endif()
    endforeach()
    # REAL_PATH can preserve the lexical input when its final component does
    # not exist. pathlib also resolves existing linked ancestors of new paths.
    execute_process(COMMAND "${CPACK_QCS_PYTHON_EXECUTABLE}" -B -c
      "import json,os,pathlib,sys; p=[pathlib.Path(s).resolve().as_posix() for s in sys.argv[1:]]; print(json.dumps([s.lower() if os.name == 'nt' else s for s in p]))"
      ${_qcs_analysis_paths}
      RESULT_VARIABLE _qcs_paths_status OUTPUT_VARIABLE _qcs_resolved_paths ERROR_VARIABLE _qcs_paths_error)
    if(NOT _qcs_paths_status EQUAL 0)
      message(FATAL_ERROR "Cannot resolve CUDA DEB analysis boundaries: ${_qcs_paths_error}")
    endif()
    string(JSON _qcs_analysis GET "${_qcs_resolved_paths}" 0)
    set(_qcs_boundary_index 0)
    foreach(_qcs_boundary IN LISTS _qcs_analysis_boundaries)
      math(EXPR _qcs_boundary_index "${_qcs_boundary_index} + 1")
      string(JSON _qcs_payload_root GET "${_qcs_resolved_paths}" ${_qcs_boundary_index})
      cmake_path(IS_PREFIX _qcs_payload_root "${_qcs_analysis}" NORMALIZE _qcs_inside_payload)
      cmake_path(IS_PREFIX _qcs_analysis "${_qcs_payload_root}" NORMALIZE _qcs_contains_payload)
      if(_qcs_inside_payload OR _qcs_contains_payload)
        message(FATAL_ERROR "CUDA driver analysis directory overlaps ${_qcs_boundary}: ${_qcs_analysis}")
      endif()
    endforeach()
    execute_process(COMMAND "${CPACK_QCS_PYTHON_EXECUTABLE}" -B "${CPACK_QCS_SOURCE_DIR}/tools/qcs-gpu-runtime.py"
      --variant "${CPACK_QCS_VARIANT}" --sdk-manifest "${CPACK_QCS_GPU_SDK_MANIFEST}"
      --deb-analysis-directory "${_qcs_analysis}"
      RESULT_VARIABLE _qcs_analysis_status OUTPUT_VARIABLE _qcs_analysis_output ERROR_VARIABLE _qcs_analysis_error)
    if(NOT _qcs_analysis_status EQUAL 0)
      message(FATAL_ERROR "CUDA DEB driver analysis preparation failed:\n${_qcs_analysis_output}\n${_qcs_analysis_error}")
    endif()
    message(STATUS "CUDA DEB driver analysis: ${_qcs_analysis_output}")
    list(APPEND CPACK_DEBIAN_PACKAGE_SHLIBDEPS_PRIVATE_DIRS "${_qcs_analysis}")
    list(APPEND CPACK_POST_BUILD_SCRIPTS "${CMAKE_CURRENT_LIST_DIR}/qcs-deb-verify.cmake")
  endif()
endif()
