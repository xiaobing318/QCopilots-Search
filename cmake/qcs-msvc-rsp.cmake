include_guard(GLOBAL)

if(NOT MSVC OR NOT CMAKE_GENERATOR MATCHES "^Ninja")
    return()
endif()

find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
set(_qcopilots_response_script "${CMAKE_CURRENT_LIST_DIR}/../tools/qcs-msvc-response.py")
set(_qcopilots_response_launcher "${Python3_EXECUTABLE}" -B "${_qcopilots_response_script}")
set(_qcopilots_response_prefix "\"${Python3_EXECUTABLE}\" -B \"${_qcopilots_response_script}\"")
foreach(_qcopilots_language IN ITEMS C CXX)
    # CMake inserts this immediately before link.exe, inside vs_link_exe/dll.
    # Retain a caller's launcher and its arguments after our encoding adapter.
    set(CMAKE_${_qcopilots_language}_LINKER_LAUNCHER
        ${_qcopilots_response_launcher} ${CMAKE_${_qcopilots_language}_LINKER_LAUNCHER})
    # MSVC uses CREATE_STATIC_LIBRARY, not the ARCHIVE_CREATE fallback. IPO
    # has its own initialized template and must retain its /LTCG arguments.
    foreach(_qcopilots_rule IN ITEMS CREATE_STATIC_LIBRARY CREATE_STATIC_LIBRARY_IPO)
        if(DEFINED CMAKE_${_qcopilots_language}_${_qcopilots_rule})
            set(CMAKE_${_qcopilots_language}_${_qcopilots_rule}
                "${_qcopilots_response_prefix} ${CMAKE_${_qcopilots_language}_${_qcopilots_rule}}")
        endif()
    endforeach()
endforeach()
