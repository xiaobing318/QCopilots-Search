include_guard(GLOBAL)

# Every downloaded source and generated file belongs to the external build tree.
# Third-party releases keep their own names. Application-facing aliases have one
# consistent prefix and never silently resolve to an unpinned system package.
include(FetchContent)
include(GNUInstallDirs)
include(qcs-output-paths)
include("${CMAKE_CURRENT_LIST_DIR}/qcs-write-if-different.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcs-native-source-integrity.cmake")

function(qcopilots_search_fetch name url sha256)
    # Short stable keys keep source caches readable and build trees bounded.
    if(NOT name MATCHES "^qcs_([a-z0-9_]+)$")
        message(FATAL_ERROR "Owned dependency names must use qcs_<dependency>")
    endif()
    set(dependency_key "${CMAKE_MATCH_1}")
    if(NOT sha256 MATCHES "^[0-9a-fA-F]+$")
        message(FATAL_ERROR "Missing SHA-256 for dependency ${name}")
    endif()
    string(LENGTH "${sha256}" hash_length)
    if(NOT hash_length EQUAL 64)
        message(FATAL_ERROR "Invalid SHA-256 for dependency ${name}")
    endif()
    if(NOT FETCHCONTENT_BASE_DIR)
        set(FETCHCONTENT_BASE_DIR "${CMAKE_BINARY_DIR}/../deps" CACHE PATH "External dependency directory")
    endif()
    set(QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT "${CMAKE_BINARY_DIR}/nd"
        CACHE PATH "Per-configuration native dependency build directory")
    qcopilots_search_require_external_path(FETCHCONTENT_BASE_DIR "${FETCHCONTENT_BASE_DIR}")
    qcopilots_search_require_external_path(QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}")
    file(REAL_PATH "${CMAKE_SOURCE_DIR}" source_root)
    file(REAL_PATH "${FETCHCONTENT_BASE_DIR}" dependency_root)
    cmake_path(IS_PREFIX source_root "${dependency_root}" NORMALIZE inside_source)
    if(inside_source)
        message(FATAL_ERROR "FETCHCONTENT_BASE_DIR must be outside the source tree")
    endif()
    file(REAL_PATH "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}" native_binary_root)
    cmake_path(IS_PREFIX source_root "${native_binary_root}" NORMALIZE binary_inside_source)
    if(binary_inside_source)
        message(FATAL_ERROR "QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT must be outside the source tree")
    endif()
    file(MAKE_DIRECTORY "${FETCHCONTENT_BASE_DIR}")
    # A digest-specific source path cannot reuse a previously patched cache.
    string(SUBSTRING "${sha256}" 0 12 source_key)
    set(metadata_root "${FETCHCONTENT_BASE_DIR}/.qcs-metadata")
    file(MAKE_DIRECTORY "${metadata_root}")
    file(LOCK "${metadata_root}/${name}-${source_key}.lock" GUARD FUNCTION TIMEOUT 300)
    set(cached_source "${FETCHCONTENT_BASE_DIR}/${name}-${source_key}-src")
    set(source_manifest "${metadata_root}/${name}-${sha256}.manifest")
    string(TOUPPER "${name}" upper_name)
    set(explicit_source_override "${FETCHCONTENT_SOURCE_DIR_${upper_name}}")
    if(explicit_source_override)
        qcopilots_search_require_external_path("FETCHCONTENT_SOURCE_DIR_${upper_name}" "${explicit_source_override}")
    endif()
    # Always obtain a verified archive baseline, even when a caller supplies an
    # external source override. The override must contain exactly those bytes.
    set(FETCHCONTENT_SOURCE_DIR_${upper_name} "")
    if(EXISTS "${source_manifest}")
        qcopilots_search_verify_source("${cached_source}" "${source_manifest}")
        set(FETCHCONTENT_SOURCE_DIR_${upper_name} "${cached_source}")
    elseif(EXISTS "${cached_source}")
        file(GLOB cache_entries "${cached_source}/*")
        if(cache_entries)
            message(FATAL_ERROR "Unverified dependency cache: ${cached_source}. Select a fresh FETCHCONTENT_BASE_DIR.")
        endif()
    endif()
    FetchContent_Declare(${name}
        URL "${url}"
        URL_HASH "SHA256=${sha256}"
        TLS_VERIFY TRUE
        TIMEOUT 300
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
        SOURCE_DIR "${cached_source}"
        BINARY_DIR "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}/${dependency_key}"
        SUBBUILD_DIR "${CMAKE_BINARY_DIR}/sb/${dependency_key}"
        DOWNLOAD_DIR "${FETCHCONTENT_BASE_DIR}/dl/${dependency_key}/${source_key}")
    FetchContent_GetProperties(${name})
    if(NOT ${name}_POPULATED)
        FetchContent_Populate(${name})
    endif()
    if(NOT EXISTS "${source_manifest}")
        qcopilots_search_source_manifest("${${name}_SOURCE_DIR}" manifest_content)
        if(manifest_content STREQUAL "")
            message(FATAL_ERROR "Pinned source archive was not populated for ${name}. Provide the verified dependency cache before using offline configuration.")
        endif()
        qcopilots_search_write_if_different("${source_manifest}" "${manifest_content}")
    endif()
    if(explicit_source_override)
        qcopilots_search_verify_source("${explicit_source_override}" "${source_manifest}")
        set(${name}_SOURCE_DIR "${explicit_source_override}")
        FetchContent_SetPopulated(${name} SOURCE_DIR "${explicit_source_override}"
            BINARY_DIR "${${name}_BINARY_DIR}")
    endif()
    set_property(GLOBAL APPEND PROPERTY QCOPILOTS_SEARCH_UPSTREAM_SOURCE_RECORDS
        "qcopilots_search_verify_source([==[${${name}_SOURCE_DIR}]==] [==[${source_manifest}]==])\n")
    set(${name}_SOURCE_DIR "${${name}_SOURCE_DIR}" PARENT_SCOPE)
    set(${name}_BINARY_DIR "${${name}_BINARY_DIR}" PARENT_SCOPE)
endfunction()

function(qcopilots_search_add_storage_dependencies)
    if(TARGET qcs::sqlite3)
        return()
    endif()
    qcopilots_search_fetch(qcs_sqlite
        "https://sqlite.org/2026/sqlite-amalgamation-3530400.zip"
        "1e71ddf93849c6a6ecf58b827c0692073d2dd7ee40196158068f7b29f422e87d")
    add_library(qcs-native-sqlite STATIC "${qcs_sqlite_SOURCE_DIR}/sqlite3.c")
    target_include_directories(qcs-native-sqlite SYSTEM PUBLIC "${qcs_sqlite_SOURCE_DIR}")
    target_compile_definitions(qcs-native-sqlite PRIVATE
        SQLITE_ENABLE_FTS5 SQLITE_THREADSAFE=1 SQLITE_DQS=0 SQLITE_DEFAULT_FOREIGN_KEYS=1
        SQLITE_OMIT_LOAD_EXTENSION SQLITE_USE_URI=1)
    set_target_properties(qcs-native-sqlite PROPERTIES POSITION_INDEPENDENT_CODE ON)
    if(UNIX)
        find_package(Threads REQUIRED)
        target_link_libraries(qcs-native-sqlite PUBLIC Threads::Threads ${CMAKE_DL_LIBS})
        if(NOT APPLE)
            target_link_libraries(qcs-native-sqlite PUBLIC m)
        endif()
    endif()
    add_library(qcs::sqlite3 ALIAS qcs-native-sqlite)
    add_library(SQLite::SQLite3 ALIAS qcs-native-sqlite)

    qcopilots_search_fetch(qcs_hnswlib
        "https://codeload.github.com/nmslib/hnswlib/tar.gz/refs/tags/v0.8.0"
        "cf61d7dd8dc9bfba7f4abe0ed26698e90ac9f49a0badb2e1b0f3ba89b72cf3bb")
    add_library(qcs-native-hnswlib INTERFACE)
    target_include_directories(qcs-native-hnswlib SYSTEM INTERFACE "${qcs_hnswlib_SOURCE_DIR}")
    # Deliberately avoid upstream's -march=native setting on distribution builds.
    add_library(qcs::hnswlib ALIAS qcs-native-hnswlib)
    add_library(hnswlib::hnswlib ALIAS qcs-native-hnswlib)

    qcopilots_search_fetch(qcs_cppjieba
        "https://codeload.github.com/yanyiwu/cppjieba/tar.gz/refs/tags/v5.6.7"
        "08322f7c2a06a88e52eb0fdbc286ec86a85abae5b2a33b1e1430dba50b71960e")
    # GitHub source archives omit submodules. Use the exact gitlink from v5.6.7.
    qcopilots_search_fetch(qcs_limonp
        "https://codeload.github.com/yanyiwu/limonp/tar.gz/9d74077dfcdf8073536c97a00bb79d7a3c3fdaba"
        "2fff67ade507900ab7acb3c630b7b91232ff882f1b6240990875056befaa731b")
    add_library(qcs-native-cppjieba INTERFACE)
    target_include_directories(qcs-native-cppjieba SYSTEM INTERFACE
        "${qcs_cppjieba_SOURCE_DIR}/include" "${qcs_limonp_SOURCE_DIR}/include")
    add_library(qcs::cppjieba ALIAS qcs-native-cppjieba)
    add_library(cppjieba::cppjieba ALIAS qcs-native-cppjieba)
    set(QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR "${qcs_cppjieba_SOURCE_DIR}/dict"
        CACHE INTERNAL "Pinned cppjieba dictionary directory" FORCE)
    install(DIRECTORY "${qcs_cppjieba_SOURCE_DIR}/dict/"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/dict" COMPONENT qcs-runtime)
    install(FILES "${qcs_hnswlib_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/hnswlib" COMPONENT qcs-runtime)
    install(FILES "${qcs_cppjieba_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/cppjieba" COMPONENT qcs-runtime)
    install(FILES "${qcs_limonp_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/limonp" COMPONENT qcs-runtime)
endfunction()

function(qcopilots_search_add_archive_dependency)
    if(TARGET qcs::miniz)
        return()
    endif()
    qcopilots_search_fetch(qcs_miniz
        "https://codeload.github.com/richgel999/miniz/tar.gz/refs/tags/3.1.0"
        "09569fc19d060ac9f5999ba9356728c2494ebe6a24ac0eb0a6b6ae3d396cfea6")
    # Upstream CMake changes runtime outputs to its source tree. Compile the four
    # documented sources directly to keep downloaded sources strictly read-only.
    add_library(qcs-native-miniz STATIC
        "${qcs_miniz_SOURCE_DIR}/miniz.c"
        "${qcs_miniz_SOURCE_DIR}/miniz_zip.c"
        "${qcs_miniz_SOURCE_DIR}/miniz_tinfl.c"
        "${qcs_miniz_SOURCE_DIR}/miniz_tdef.c")
    file(MAKE_DIRECTORY "${qcs_miniz_BINARY_DIR}")
    qcopilots_search_write_if_different("${qcs_miniz_BINARY_DIR}/miniz_export.h"
        "#pragma once\n#define MINIZ_EXPORT\n#define MINIZ_NO_EXPORT\n")
    target_include_directories(qcs-native-miniz SYSTEM PUBLIC
        "${qcs_miniz_SOURCE_DIR}" "${qcs_miniz_BINARY_DIR}")
    target_compile_definitions(qcs-native-miniz PUBLIC MINIZ_NO_ZLIB_COMPATIBLE_NAMES)
    set_target_properties(qcs-native-miniz PROPERTIES POSITION_INDEPENDENT_CODE ON)
    add_library(qcs::miniz ALIAS qcs-native-miniz)
    add_library(miniz::miniz ALIAS qcs-native-miniz)
    install(FILES "${qcs_miniz_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/miniz" COMPONENT qcs-runtime)
endfunction()

function(qcopilots_search_add_git_dependency)
    if(TARGET qcs::git2)
        return()
    endif()
    qcopilots_search_fetch(qcs_libgit2
        "https://codeload.github.com/libgit2/libgit2/tar.gz/refs/tags/v1.9.7"
        "1a4fbe7589e814777ae76b64734ad80f4ecad22cd33a22682a2aaea4ae5375e7")
    set(CMAKE_POLICY_DEFAULT_CMP0077 NEW)
    set(CMAKE_POLICY_DEFAULT_CMP0126 NEW)
    set(BUILD_SHARED_LIBS ON)
    set(BUILD_TESTS OFF)
    set(BUILD_CLI OFF)
    set(BUILD_EXAMPLES OFF)
    set(BUILD_FUZZERS OFF)
    set(USE_HTTPS OFF CACHE STRING "No network transport needed for ignore rules" FORCE)
    set(USE_SSH OFF CACHE STRING "No SSH transport needed" FORCE)
    set(USE_GSSAPI OFF)
    set(USE_NTLMCLIENT OFF)
    set(USE_SHA256 Builtin CACHE STRING "Pinned libgit2 builtin hash" FORCE)
    set(REGEX_BACKEND builtin CACHE STRING "Pinned libgit2 builtin regex" FORCE)
    set(USE_BUNDLED_ZLIB ON)
    set(STATIC_CRT OFF)
    set(USE_NSEC ON)
    # Upstream writes these generic settings to the global CMake cache. A
    # function alone does not isolate CACHE variables from later dependencies.
    foreach(language_setting IN ITEMS CMAKE_C_STANDARD CMAKE_C_EXTENSIONS)
        get_property(saved_${language_setting}_exists CACHE ${language_setting} PROPERTY TYPE SET)
        if(saved_${language_setting}_exists)
            foreach(cache_property IN ITEMS VALUE TYPE HELPSTRING)
                get_property(saved_${language_setting}_${cache_property}
                    CACHE ${language_setting} PROPERTY ${cache_property})
            endforeach()
        endif()
    endforeach()
    set(CMAKE_C_STANDARD 90)
    set(CMAKE_C_EXTENSIONS OFF)
    if(UNIX)
        # libgit2 probes struct stat before DefaultCFlags adds _GNU_SOURCE
        # to its real compilation. Match that feature-test environment, so
        # strict C90 does not hide glibc's nanosecond fields from the probes.
        list(APPEND CMAKE_REQUIRED_DEFINITIONS -D_GNU_SOURCE)
        # Recheck older build trees that cached results without the macro.
        foreach(stat_probe IN ITEMS HAVE_STRUCT_STAT_ST_MTIM
                HAVE_STRUCT_STAT_ST_MTIMESPEC HAVE_STRUCT_STAT_MTIME_NSEC
                HAVE_STRUCT_STAT_NSEC)
            unset(${stat_probe})
            unset(${stat_probe} CACHE)
        endforeach()
    endif()
    add_subdirectory("${qcs_libgit2_SOURCE_DIR}" "${qcs_libgit2_BINARY_DIR}" EXCLUDE_FROM_ALL)
    foreach(language_setting IN ITEMS CMAKE_C_STANDARD CMAKE_C_EXTENSIONS)
        if(saved_${language_setting}_exists)
            set(${language_setting} "${saved_${language_setting}_VALUE}"
                CACHE "${saved_${language_setting}_TYPE}"
                "${saved_${language_setting}_HELPSTRING}" FORCE)
        else()
            unset(${language_setting} CACHE)
        endif()
    endforeach()
    if(UNIX AND NOT (HAVE_STRUCT_STAT_MTIME_NSEC OR
            ((HAVE_STRUCT_STAT_ST_MTIM OR HAVE_STRUCT_STAT_ST_MTIMESPEC)
            AND HAVE_STRUCT_STAT_NSEC)))
        message(FATAL_ERROR "libgit2 requires a detected struct stat nanosecond field on this platform")
    endif()
    target_include_directories(libgit2package SYSTEM PUBLIC
        "$<BUILD_INTERFACE:${qcs_libgit2_SOURCE_DIR}/include>"
        "$<BUILD_INTERFACE:${qcs_libgit2_BINARY_DIR}/include>")
    add_library(qcs::git2 ALIAS libgit2package)
    add_library(libgit2::libgit2 ALIAS libgit2package)
    install(TARGETS libgit2package
        RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT qcs-runtime
        LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT qcs-runtime)
    install(FILES "${qcs_libgit2_SOURCE_DIR}/COPYING"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/libgit2" COMPONENT qcs-runtime)
endfunction()

include("${CMAKE_CURRENT_LIST_DIR}/qcs-native-ocr.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcs-native-images.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcs-native-data.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcs-native-doc.cmake")

function(qcopilots_search_finalize_native_source_records)
    get_property(source_records GLOBAL PROPERTY QCOPILOTS_SEARCH_UPSTREAM_SOURCE_RECORDS)
    set(source_records_file "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}/qcs-source-records.cmake")
    qcopilots_search_write_if_different("${source_records_file}" ${source_records})
    include("${source_records_file}")
endfunction()

function(qcopilots_search_add_native_dependencies)
    get_property(already_added GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_DEPENDENCIES_ADDED)
    if(already_added)
        return()
    endif()
    qcopilots_search_add_storage_dependencies()
    qcopilots_search_add_archive_dependency()
    qcopilots_search_add_git_dependency()
    # Leptonica's internal bitmap fonts need TIFF and its TIFF adapter also
    # requires JPEG support. Reuse exactly the image pipeline's pinned codecs.
    qcopilots_search_add_image_codec_dependencies()
    qcopilots_search_add_ocr_dependencies()
    qcopilots_search_add_image_dependencies()
    qcopilots_search_add_ocr_data()
    qcopilots_search_add_doc_dependency()
    get_property(ocr_runtime_targets GLOBAL PROPERTY QCOPILOTS_SEARCH_OCR_RUNTIME_TARGETS)
    get_property(doc_runtime_targets GLOBAL PROPERTY QCOPILOTS_SEARCH_DOC_RUNTIME_TARGETS)
    set(runtime_targets ${ocr_runtime_targets} ${QCOPILOTS_SEARCH_IMAGE_RUNTIME_TARGETS} ${doc_runtime_targets})
    install(TARGETS ${runtime_targets}
        RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT qcs-runtime
        LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT qcs-runtime)
    foreach(dependency IN ITEMS leptonica tesseract libde265 libheif libraw)
        FetchContent_GetProperties(qcs_${dependency} SOURCE_DIR dependency_source)
        if(dependency STREQUAL "libraw")
            set(license_files "${dependency_source}/COPYRIGHT" "${dependency_source}/LICENSE.LGPL" "${dependency_source}/LICENSE.CDDL")
        elseif(dependency STREQUAL "leptonica")
            set(license_files "${dependency_source}/leptonica-license.txt")
        elseif(dependency STREQUAL "tesseract")
            set(license_files "${dependency_source}/LICENSE")
        else()
            set(license_files "${dependency_source}/COPYING")
        endif()
        install(FILES ${license_files}
            DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/${dependency}" COMPONENT qcs-runtime)
    endforeach()
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_RUNTIME_TARGETS "libgit2package;${runtime_targets}")
    set(source_records_file "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}/qcs-source-records.cmake")
    # Later modules also fetch pinned sources, including FFmpeg's public headers.
    # Collect and check the complete set after the top-level configure has ended.
    cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}"
        CALL qcopilots_search_finalize_native_source_records)
    add_custom_target(qcs-check-native-sources
        COMMAND "${CMAKE_COMMAND}" "-DQCOPILOTS_SEARCH_SOURCE_RECORDS=${source_records_file}"
            -P "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcs-native-source-integrity.cmake"
        COMMENT "Checking pinned upstream native sources" VERBATIM)
    foreach(native_target IN ITEMS libgit2package qcs-native-sqlite qcs-native-miniz
            qcs-native-zlib qcs-build-jpeg ${runtime_targets})
        add_dependencies(${native_target} qcs-check-native-sources)
    endforeach()
    add_custom_target(qcs-verify-native-sources ALL
        COMMAND "${CMAKE_COMMAND}" "-DQCOPILOTS_SEARCH_SOURCE_RECORDS=${source_records_file}"
            -P "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcs-native-source-integrity.cmake"
        DEPENDS libgit2package qcs-native-sqlite qcs-native-miniz ${runtime_targets}
        COMMENT "Verifying unmodified upstream native sources" VERBATIM)
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_DEPENDENCIES_ADDED TRUE)
endfunction()
