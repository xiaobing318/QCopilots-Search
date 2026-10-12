include_guard(GLOBAL)

# Every downloaded source and generated file belongs to the external build tree.
# Third-party releases keep their own names. Application-facing aliases have one
# consistent prefix and never silently resolve to an unpinned system package.
include(FetchContent)
include(GNUInstallDirs)

function(qcopilots_search_fetch name url sha256)
    if(NOT sha256 MATCHES "^[0-9a-fA-F]+$")
        message(FATAL_ERROR "Missing SHA-256 for dependency ${name}")
    endif()
    string(LENGTH "${sha256}" hash_length)
    if(NOT hash_length EQUAL 64)
        message(FATAL_ERROR "Invalid SHA-256 for dependency ${name}")
    endif()
    if(NOT FETCHCONTENT_BASE_DIR)
        set(FETCHCONTENT_BASE_DIR "${CMAKE_BINARY_DIR}/_deps" CACHE PATH "External dependency directory")
    endif()
    set(QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT "${CMAKE_BINARY_DIR}/native-dependencies"
        CACHE PATH "Per-configuration native dependency build directory")
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
    # Source archives can be shared by Release, Debug and parallel checkouts.
    # Their object files and generated headers must never share a build tree.
    # Serialize first population, then reuse a completed source tree without
    # re-extracting it while another configuration may be compiling its files.
    file(LOCK "${FETCHCONTENT_BASE_DIR}/${name}.lock" GUARD FUNCTION TIMEOUT 300)
    set(cached_source "${FETCHCONTENT_BASE_DIR}/${name}-src")
    set(source_marker "${cached_source}/.qcopilots-search-source-sha256")
    string(TOUPPER "${name}" upper_name)
    set(explicit_source_override "${FETCHCONTENT_SOURCE_DIR_${upper_name}}")
    if(NOT explicit_source_override AND EXISTS "${source_marker}")
        file(READ "${source_marker}" cached_hash)
        string(STRIP "${cached_hash}" cached_hash)
        if(cached_hash STREQUAL sha256)
            set(FETCHCONTENT_SOURCE_DIR_${upper_name} "${cached_source}")
        endif()
    endif()
    FetchContent_Declare(${name}
        URL "${url}"
        URL_HASH "SHA256=${sha256}"
        TLS_VERIFY TRUE
        TIMEOUT 300
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
        BINARY_DIR "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}/${name}-build"
        SUBBUILD_DIR "${CMAKE_BINARY_DIR}/native-dependencies/${name}-subbuild"
        DOWNLOAD_DIR "${FETCHCONTENT_BASE_DIR}/downloads/${name}")
    FetchContent_GetProperties(${name})
    if(NOT ${name}_POPULATED)
        # Populate only: upstream projects with examples, downloads or native CPU
        # flags are added explicitly below with controlled options.
        FetchContent_Populate(${name})
    endif()
    file(REAL_PATH "${${name}_SOURCE_DIR}" populated_source)
    cmake_path(IS_PREFIX source_root "${populated_source}" NORMALIZE populated_inside_source)
    if(populated_inside_source)
        message(FATAL_ERROR "Dependency ${name} source must be outside the application source tree")
    endif()
    if(NOT explicit_source_override AND EXISTS "${${name}_SOURCE_DIR}" AND NOT cached_hash STREQUAL sha256)
        file(WRITE "${source_marker}" "${sha256}\n")
    endif()
    set(${name}_SOURCE_DIR "${${name}_SOURCE_DIR}" PARENT_SCOPE)
    set(${name}_BINARY_DIR "${${name}_BINARY_DIR}" PARENT_SCOPE)
endfunction()

function(qcopilots_search_add_storage_dependencies)
    if(TARGET qcopilots-search::sqlite3)
        return()
    endif()
    qcopilots_search_fetch(qcopilots_search_sqlite
        "https://sqlite.org/2026/sqlite-amalgamation-3530400.zip"
        "1e71ddf93849c6a6ecf58b827c0692073d2dd7ee40196158068f7b29f422e87d")
    add_library(qcopilots-search-native-sqlite STATIC "${qcopilots_search_sqlite_SOURCE_DIR}/sqlite3.c")
    target_include_directories(qcopilots-search-native-sqlite SYSTEM PUBLIC "${qcopilots_search_sqlite_SOURCE_DIR}")
    target_compile_definitions(qcopilots-search-native-sqlite PRIVATE
        SQLITE_ENABLE_FTS5 SQLITE_THREADSAFE=1 SQLITE_DQS=0 SQLITE_DEFAULT_FOREIGN_KEYS=1
        SQLITE_OMIT_LOAD_EXTENSION SQLITE_USE_URI=1)
    set_target_properties(qcopilots-search-native-sqlite PROPERTIES POSITION_INDEPENDENT_CODE ON)
    if(UNIX)
        find_package(Threads REQUIRED)
        target_link_libraries(qcopilots-search-native-sqlite PUBLIC Threads::Threads ${CMAKE_DL_LIBS})
        if(NOT APPLE)
            target_link_libraries(qcopilots-search-native-sqlite PUBLIC m)
        endif()
    endif()
    add_library(qcopilots-search::sqlite3 ALIAS qcopilots-search-native-sqlite)
    add_library(SQLite::SQLite3 ALIAS qcopilots-search-native-sqlite)

    qcopilots_search_fetch(qcopilots_search_hnswlib
        "https://codeload.github.com/nmslib/hnswlib/tar.gz/refs/tags/v0.8.0"
        "cf61d7dd8dc9bfba7f4abe0ed26698e90ac9f49a0badb2e1b0f3ba89b72cf3bb")
    add_library(qcopilots-search-native-hnswlib INTERFACE)
    target_include_directories(qcopilots-search-native-hnswlib SYSTEM INTERFACE "${qcopilots_search_hnswlib_SOURCE_DIR}")
    # Deliberately avoid upstream's -march=native setting on distribution builds.
    add_library(qcopilots-search::hnswlib ALIAS qcopilots-search-native-hnswlib)
    add_library(hnswlib::hnswlib ALIAS qcopilots-search-native-hnswlib)

    qcopilots_search_fetch(qcopilots_search_cppjieba
        "https://codeload.github.com/yanyiwu/cppjieba/tar.gz/refs/tags/v5.6.7"
        "08322f7c2a06a88e52eb0fdbc286ec86a85abae5b2a33b1e1430dba50b71960e")
    # GitHub source archives omit submodules. Use the exact gitlink from v5.6.7.
    qcopilots_search_fetch(qcopilots_search_limonp
        "https://codeload.github.com/yanyiwu/limonp/tar.gz/9d74077dfcdf8073536c97a00bb79d7a3c3fdaba"
        "2fff67ade507900ab7acb3c630b7b91232ff882f1b6240990875056befaa731b")
    add_library(qcopilots-search-native-cppjieba INTERFACE)
    target_include_directories(qcopilots-search-native-cppjieba SYSTEM INTERFACE
        "${qcopilots_search_cppjieba_SOURCE_DIR}/include" "${qcopilots_search_limonp_SOURCE_DIR}/include")
    add_library(qcopilots-search::cppjieba ALIAS qcopilots-search-native-cppjieba)
    add_library(cppjieba::cppjieba ALIAS qcopilots-search-native-cppjieba)
    set(QCOPILOTS_SEARCH_CPPJIEBA_DICT_DIR "${qcopilots_search_cppjieba_SOURCE_DIR}/dict"
        CACHE INTERNAL "Pinned cppjieba dictionary directory" FORCE)
    install(DIRECTORY "${qcopilots_search_cppjieba_SOURCE_DIR}/dict/"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/dict" COMPONENT qcopilots-search-runtime)
    install(FILES "${qcopilots_search_hnswlib_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/hnswlib" COMPONENT qcopilots-search-runtime)
    install(FILES "${qcopilots_search_cppjieba_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/cppjieba" COMPONENT qcopilots-search-runtime)
    install(FILES "${qcopilots_search_limonp_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/limonp" COMPONENT qcopilots-search-runtime)
endfunction()

function(qcopilots_search_add_archive_dependency)
    if(TARGET qcopilots-search::miniz)
        return()
    endif()
    qcopilots_search_fetch(qcopilots_search_miniz
        "https://codeload.github.com/richgel999/miniz/tar.gz/refs/tags/3.1.0"
        "09569fc19d060ac9f5999ba9356728c2494ebe6a24ac0eb0a6b6ae3d396cfea6")
    # Upstream CMake changes runtime outputs to its source tree. Compile the four
    # documented sources directly to keep downloaded sources strictly read-only.
    add_library(qcopilots-search-native-miniz STATIC
        "${qcopilots_search_miniz_SOURCE_DIR}/miniz.c"
        "${qcopilots_search_miniz_SOURCE_DIR}/miniz_zip.c"
        "${qcopilots_search_miniz_SOURCE_DIR}/miniz_tinfl.c"
        "${qcopilots_search_miniz_SOURCE_DIR}/miniz_tdef.c")
    file(MAKE_DIRECTORY "${qcopilots_search_miniz_BINARY_DIR}")
    file(WRITE "${qcopilots_search_miniz_BINARY_DIR}/miniz_export.h"
        "#pragma once\n#define MINIZ_EXPORT\n#define MINIZ_NO_EXPORT\n")
    target_include_directories(qcopilots-search-native-miniz SYSTEM PUBLIC
        "${qcopilots_search_miniz_SOURCE_DIR}" "${qcopilots_search_miniz_BINARY_DIR}")
    target_compile_definitions(qcopilots-search-native-miniz PUBLIC MINIZ_NO_ZLIB_COMPATIBLE_NAMES)
    set_target_properties(qcopilots-search-native-miniz PROPERTIES POSITION_INDEPENDENT_CODE ON)
    add_library(qcopilots-search::miniz ALIAS qcopilots-search-native-miniz)
    add_library(miniz::miniz ALIAS qcopilots-search-native-miniz)
    install(FILES "${qcopilots_search_miniz_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/miniz" COMPONENT qcopilots-search-runtime)
endfunction()

function(qcopilots_search_add_git_dependency)
    if(TARGET qcopilots-search::git2)
        return()
    endif()
    qcopilots_search_fetch(qcopilots_search_libgit2
        "https://codeload.github.com/libgit2/libgit2/tar.gz/refs/tags/v1.9.1"
        "14cab3014b2b7ad75970ff4548e83615f74d719afe00aa479b4a889c1e13fc00")
    include("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcopilots-search-native-git-patch.cmake")
    qcopilots_search_prepare_git_source("${qcopilots_search_libgit2_SOURCE_DIR}"
        "${qcopilots_search_libgit2_BINARY_DIR}" patched_git_source)
    set(qcopilots_search_libgit2_SOURCE_DIR "${patched_git_source}")
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
        # libgit2 1.9.1 probes struct stat before DefaultCFlags adds _GNU_SOURCE
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
    add_subdirectory("${qcopilots_search_libgit2_SOURCE_DIR}" "${qcopilots_search_libgit2_BINARY_DIR}" EXCLUDE_FROM_ALL)
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
        "$<BUILD_INTERFACE:${qcopilots_search_libgit2_SOURCE_DIR}/include>"
        "$<BUILD_INTERFACE:${qcopilots_search_libgit2_BINARY_DIR}/include>")
    add_library(qcopilots-search::git2 ALIAS libgit2package)
    add_library(libgit2::libgit2 ALIAS libgit2package)
    install(TARGETS libgit2package
        RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT qcopilots-search-runtime
        LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT qcopilots-search-runtime)
    install(FILES "${qcopilots_search_libgit2_SOURCE_DIR}/COPYING"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/libgit2" COMPONENT qcopilots-search-runtime)
endfunction()

include("${CMAKE_CURRENT_LIST_DIR}/qcopilots-search-native-ocr.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcopilots-search-native-images.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcopilots-search-native-data.cmake")
include("${CMAKE_CURRENT_LIST_DIR}/qcopilots-search-native-doc.cmake")

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
        RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT qcopilots-search-runtime
        LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT qcopilots-search-runtime)
    foreach(dependency IN ITEMS leptonica tesseract libde265 libheif libraw)
        FetchContent_GetProperties(qcopilots_search_${dependency} SOURCE_DIR dependency_source)
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
            DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/${dependency}" COMPONENT qcopilots-search-runtime)
    endforeach()
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_RUNTIME_TARGETS "libgit2package;${runtime_targets}")
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_NATIVE_DEPENDENCIES_ADDED TRUE)
endfunction()
