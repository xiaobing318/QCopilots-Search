# CPack runs this after generating the DEB. A policy failure makes CPack fail,
# so qcs-dev never promotes this invocation's private output to verified packages.
if(NOT CPACK_PACKAGE_FILES)
  message(FATAL_ERROR "CUDA DEB verification did not receive a generated package")
endif()
foreach(_qcs_package IN LISTS CPACK_PACKAGE_FILES)
  if(NOT _qcs_package MATCHES "\\.deb$" OR NOT EXISTS "${_qcs_package}")
    message(FATAL_ERROR "CUDA DEB verification received an unexpected package: ${_qcs_package}")
  endif()
  execute_process(COMMAND "${CPACK_QCS_PYTHON_EXECUTABLE}" -B "${CPACK_QCS_SOURCE_DIR}/tools/qcs-gpu-runtime.py"
    --variant "${CPACK_QCS_VARIANT}" --verify-deb "${_qcs_package}"
    RESULT_VARIABLE _qcs_verify_status OUTPUT_VARIABLE _qcs_verify_output ERROR_VARIABLE _qcs_verify_error)
  if(NOT _qcs_verify_status EQUAL 0)
    message(FATAL_ERROR "CUDA DEB driver policy failed:\n${_qcs_verify_output}\n${_qcs_verify_error}")
  endif()
  message(STATUS "CUDA DEB driver policy: ${_qcs_verify_output}")
endforeach()
