include_guard(GLOBAL)

function(qcopilots_search_sign_macos_bundle bundle python helper report identity entitlements_map)
    set(arguments "${python}" -B "${helper}" --bundle "${bundle}"
        "--identity=${identity}" --report "${report}")
    if(NOT entitlements_map STREQUAL "")
        list(APPEND arguments --entitlements-map "${entitlements_map}")
    endif()
    execute_process(COMMAND ${arguments} RESULT_VARIABLE signing_result
        OUTPUT_VARIABLE signing_output ERROR_VARIABLE signing_errors)
    message("${signing_output}")
    if(NOT signing_result STREQUAL "0")
        message(FATAL_ERROR "Final bundle signing failed: ${signing_errors}")
    endif()
endfunction()

function(qcopilots_search_verify_deploy_rpath binary library_directory)
    find_program(QCOPILOTS_SEARCH_OTOOL otool REQUIRED)
    execute_process(COMMAND "${QCOPILOTS_SEARCH_OTOOL}" -l "${binary}"
        RESULT_VARIABLE inspect_result OUTPUT_VARIABLE load_commands ERROR_VARIABLE inspect_errors)
    if(NOT inspect_result STREQUAL "0")
        message(FATAL_ERROR "Cannot inspect deployment rpaths: ${inspect_errors}")
    endif()
    string(FIND "${load_commands}" "path ${library_directory} (offset" rpath_position)
    string(FIND "${load_commands}" "path @executable_path/../Frameworks (offset" bundle_rpath_position)
    string(FIND "${load_commands}" "path @loader_path/../Frameworks (offset" loader_rpath_position)
    if(NOT rpath_position EQUAL -1 OR
            (bundle_rpath_position EQUAL -1 AND loader_rpath_position EQUAL -1))
        message(FATAL_ERROR "Deployed executable retains a build rpath or lacks its bundle-relative Frameworks rpath")
    endif()
endfunction()

function(qcopilots_search_verify_bundle_signature bundle)
    find_program(QCOPILOTS_SEARCH_CODESIGN codesign REQUIRED)
    execute_process(COMMAND "${QCOPILOTS_SEARCH_CODESIGN}" --verify --deep --strict "${bundle}"
        RESULT_VARIABLE signature_result OUTPUT_VARIABLE signature_output ERROR_VARIABLE signature_errors)
    if(NOT signature_result STREQUAL "0")
        message(FATAL_ERROR "Deployed bundle signature is invalid: ${signature_output}${signature_errors}")
    endif()
endfunction()

function(qcopilots_search_checked_deploy deploy_script)
    execute_process(
        COMMAND "${CMAKE_COMMAND}"
            "-DCMAKE_INSTALL_PREFIX=${CMAKE_INSTALL_PREFIX}"
            "-DCMAKE_INSTALL_CONFIG_NAME=${CMAKE_INSTALL_CONFIG_NAME}"
            -P "${deploy_script}"
        RESULT_VARIABLE deploy_result
        OUTPUT_VARIABLE deploy_output
        ERROR_VARIABLE deploy_errors)
    if(NOT deploy_output STREQUAL "")
        message("${deploy_output}")
    endif()
    if(NOT deploy_errors STREQUAL "")
        message("${deploy_errors}")
    endif()
    if(NOT deploy_result STREQUAL "0" OR
            "${deploy_output}\n${deploy_errors}" MATCHES "(^|[\r\n])[ \t]*ERROR:")
        message(FATAL_ERROR "Qt deployment failed or reported an incomplete bundle")
    endif()
endfunction()
