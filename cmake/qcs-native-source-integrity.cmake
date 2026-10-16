include_guard(GLOBAL)

# The manifest is kept beside the cache, never inside an upstream source tree.
# Include filenames as well as bytes so additions and deletions are detected.
function(qcopilots_search_source_manifest root output)
    file(GLOB_RECURSE entries LIST_DIRECTORIES FALSE RELATIVE "${root}" "${root}/*")
    list(SORT entries)
    set(manifest "")
    foreach(entry IN LISTS entries)
        file(SHA256 "${root}/${entry}" digest)
        string(APPEND manifest "${digest}  ${entry}\n")
    endforeach()
    set(${output} "${manifest}" PARENT_SCOPE)
endfunction()

function(qcopilots_search_verify_source root manifest)
    if(NOT EXISTS "${manifest}")
        message(FATAL_ERROR "Missing upstream source integrity manifest: ${manifest}")
    endif()
    file(READ "${manifest}" expected)
    qcopilots_search_source_manifest("${root}" actual)
    if(NOT actual STREQUAL expected)
        message(FATAL_ERROR "Upstream source differs from its pinned archive: ${root}. Use a fresh dependency cache or restore the original source bytes.")
    endif()
endfunction()

if(DEFINED QCOPILOTS_SEARCH_SOURCE_RECORDS)
    include("${QCOPILOTS_SEARCH_SOURCE_RECORDS}")
endif()
