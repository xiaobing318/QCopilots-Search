include_guard(GLOBAL)
# Keep source-level information for our own code in optimized releases. The
# redistributable package is stripped separately by CPack, symbols stay external.
foreach(_target qcopilots-search-core qcopilots-search-runtime
    qcopilots-search-extraction qcopilots-search-storage qcopilots-search-client qcopilots-search-service-core
    qcopilots-search-app-components qcopilots-search qcopilots-search-service
    qcopilots-search-worker qcopilots-search-cli)
  if(TARGET ${_target})
    if(MSVC)
      target_compile_options(${_target} PRIVATE /Zi /Zo)
      get_target_property(_kind ${_target} TYPE)
      if(_kind STREQUAL "EXECUTABLE")
        target_link_options(${_target} PRIVATE /DEBUG:FULL /INCREMENTAL:NO)
      endif()
    else()
      target_compile_options(${_target} PRIVATE -g)
    endif()
  endif()
endforeach()

set(_symbol_programs)
foreach(_target qcopilots-search qcopilots-search-service qcopilots-search-worker qcopilots-search-cli)
  if(TARGET ${_target})
    if(_symbol_programs)
      string(APPEND _symbol_programs ",\n")
    endif()
    set(_pdb "")
    if(MSVC)
      set(_pdb "$<TARGET_PDB_FILE:${_target}>")
    endif()
    string(APPEND _symbol_programs "    {\"name\":\"${_target}\",\"binary\":\"$<TARGET_FILE:${_target}>\",\"pdb\":\"${_pdb}\"}")
  endif()
endforeach()
file(GENERATE OUTPUT "${CMAKE_BINARY_DIR}/qcopilots-search-symbol-inputs-$<CONFIG>.json"
  CONTENT "{\n  \"version\":\"${PROJECT_VERSION}\",\n  \"platform\":\"${CMAKE_SYSTEM_NAME}\",\n  \"architecture\":\"${CMAKE_SYSTEM_PROCESSOR}\",\n  \"programs\":[\n${_symbol_programs}\n  ]\n}\n")
