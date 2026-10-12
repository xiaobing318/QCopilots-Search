include_guard(GLOBAL)
include(FetchContent)
find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)

set(QCOPILOTS_SEARCH_LICENSE_SOURCE_CACHE "${QCOPILOTS_SEARCH_DEPENDENCY_ROOT}/../source-cache"
  CACHE PATH "External cache for pinned Qt license source archives")
option(QCOPILOTS_SEARCH_DOWNLOAD_LICENSE_SOURCES "Download missing hash-locked Qt license source archives" ON)
get_filename_component(_qcopilots_license_qt_prefix "${Qt6Core_DIR}/../../.." ABSOLUTE)
FetchContent_GetProperties(qcopilots_search_llama SOURCE_DIR _qcopilots_license_llama_source)
if(NOT _qcopilots_license_llama_source)
  message(FATAL_ERROR "The verified llama.cpp source is required for its runtime license materials")
endif()

set(_qcopilots_license_tool "${PROJECT_SOURCE_DIR}/tools/qcopilots-search-license-materials.py")
set(_qcopilots_license_lock "${PROJECT_SOURCE_DIR}/tools/qcopilots-search-source-packages.lock.json")
set(_qcopilots_license_output "${CMAKE_BINARY_DIR}/generated/qcopilots-search-runtime-licenses")
set(_qcopilots_license_download)
if(QCOPILOTS_SEARCH_DOWNLOAD_LICENSE_SOURCES)
  set(_qcopilots_license_download --download)
endif()
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${_qcopilots_license_tool}" "${_qcopilots_license_lock}" "${_qcopilots_license_llama_source}/LICENSE")
execute_process(COMMAND "${Python3_EXECUTABLE}" -X utf8 -B "${_qcopilots_license_tool}"
  --qt-prefix "${_qcopilots_license_qt_prefix}" --llama-source "${_qcopilots_license_llama_source}"
  --cache-dir "${QCOPILOTS_SEARCH_LICENSE_SOURCE_CACHE}" --output-dir "${_qcopilots_license_output}"
  --source-lock "${_qcopilots_license_lock}" ${_qcopilots_license_download}
  RESULT_VARIABLE _qcopilots_license_result OUTPUT_VARIABLE _qcopilots_license_stdout
  ERROR_VARIABLE _qcopilots_license_stderr)
if(NOT _qcopilots_license_result EQUAL 0)
  message(FATAL_ERROR "Cannot prepare complete Qt/llama/ggml license materials:\n${_qcopilots_license_stdout}${_qcopilots_license_stderr}")
endif()
message(STATUS "Runtime license materials: ${_qcopilots_license_stdout}")

# Check the generated inputs again at install time. Both cmake --install and
# CPack stop before deploying incomplete or modified license material.
install(CODE "execute_process(COMMAND \"${Python3_EXECUTABLE}\" -X utf8 -B \"${_qcopilots_license_tool}\" --verify \"${_qcopilots_license_output}\" RESULT_VARIABLE _license_result OUTPUT_VARIABLE _license_output ERROR_VARIABLE _license_error)\nif(NOT _license_result EQUAL 0)\n  message(FATAL_ERROR \"Runtime license validation failed: \${_license_output}\${_license_error}\")\nendif()")
install(DIRECTORY "${_qcopilots_license_output}/"
  DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/qcopilots-search-runtime")

if(QCOPILOTS_SEARCH_BUILD_TESTS)
  add_test(NAME qcopilots-search-tests-license-materials COMMAND "${Python3_EXECUTABLE}" -X utf8 -B
    "${PROJECT_SOURCE_DIR}/tools/qcopilots-search-test-license-materials.py")
  set_tests_properties(qcopilots-search-tests-license-materials PROPERTIES
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}" LABELS "unit;portability;package"
    ENVIRONMENT "PYTHONDONTWRITEBYTECODE=1")
endif()
