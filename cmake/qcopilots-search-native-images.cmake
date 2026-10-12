include_guard(GLOBAL)

function(qcopilots_search_add_jpeg_contract target)
    # A source outside a dependency's source/build trees becomes an absolute
    # object name. MSVC's link response file can misread its Unicode prefix.
    # Keep this translation unit in each consumer's own binary directory while
    # retaining that consumer's actual JPEG include search order.
    get_target_property(consumer_binary "${target}" BINARY_DIR)
    set(contract "${consumer_binary}/qcopilots-search-contracts/${target}/qcopilots-search-jpeg-contract.c")
    configure_file("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcopilots-search-jpeg-contract.c"
                   "${contract}" COPYONLY)
    target_sources("${target}" PRIVATE "${contract}")
endfunction()

function(qcopilots_search_add_image_codec_dependencies)
    if(TARGET qcopilots-search::zlib AND TARGET qcopilots-search::jpeg)
        return()
    endif()
    if(TARGET ZLIB::ZLIB OR TARGET JPEG::JPEG)
        message(FATAL_ERROR "Image codecs require the pinned QCopilots Search dependency targets")
    endif()
    qcopilots_search_fetch(qcopilots_search_zlib
        "https://zlib.net/fossils/zlib-1.3.2.tar.gz"
        "bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16")
    # libgit2 has an internal OBJECT library called zlib. Use an owned target
    # for the official, small source manifest to avoid colliding with it.
    set(zlib_sources adler32.c compress.c crc32.c deflate.c gzclose.c gzlib.c
        gzread.c gzwrite.c inflate.c infback.c inftrees.c inffast.c trees.c
        uncompr.c zutil.c)
    list(TRANSFORM zlib_sources PREPEND "${qcopilots_search_zlib_SOURCE_DIR}/")
    add_library(qcopilots-search-native-zlib SHARED ${zlib_sources})
    target_include_directories(qcopilots-search-native-zlib SYSTEM PUBLIC
        "$<BUILD_INTERFACE:${qcopilots_search_zlib_SOURCE_DIR}>")
    target_compile_definitions(qcopilots-search-native-zlib PRIVATE ZLIB_BUILD)
    if(WIN32)
        target_compile_definitions(qcopilots-search-native-zlib PUBLIC ZLIB_DLL)
        target_compile_definitions(qcopilots-search-native-zlib PRIVATE
            _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_DEPRECATE)
    else()
        target_compile_definitions(qcopilots-search-native-zlib PRIVATE HAVE_UNISTD_H)
    endif()
    if(WIN32)
        set(zlib_output_name zlib1)
    else()
        set(zlib_output_name z)
    endif()
    set_target_properties(qcopilots-search-native-zlib PROPERTIES
        OUTPUT_NAME "${zlib_output_name}" VERSION 1.3.2 SOVERSION 1)
    if(UNIX AND NOT APPLE AND NOT CMAKE_SYSTEM_NAME STREQUAL "AIX"
            AND NOT CMAKE_SYSTEM_NAME STREQUAL "SunOS")
        # Preserve the upstream ELF symbol versions. Qt's binary SDK and system
        # libraries bind to names such as compressBound@ZLIB_1.2.0.
        target_link_options(qcopilots-search-native-zlib PRIVATE
            "LINKER:--version-script,${qcopilots_search_zlib_SOURCE_DIR}/zlib.map")
        set_property(TARGET qcopilots-search-native-zlib APPEND PROPERTY LINK_DEPENDS
            "${qcopilots_search_zlib_SOURCE_DIR}/zlib.map")
    endif()
    add_library(qcopilots-search::zlib ALIAS qcopilots-search-native-zlib)
    add_library(ZLIB::ZLIB ALIAS qcopilots-search-native-zlib)

    qcopilots_search_fetch(qcopilots_search_jpeg
        "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/3.1.4.1/libjpeg-turbo-3.1.4.1.tar.gz"
        "ecae8008e2cc9ade2f2c1bb9d5e6d4fb73e7c433866a056bd82980741571a022")
    # libjpeg-turbo explicitly supports ExternalProject, not add_subdirectory.
    # Build its public libjpeg API with portable C, without NASM, CLI, tests,
    # Java or the separate TurboJPEG API library.
    include(ExternalProject)
    set(jpeg_stage "${qcopilots_search_jpeg_BINARY_DIR}/stage")
    if(CMAKE_CONFIGURATION_TYPES OR CMAKE_BUILD_TYPE)
        set(jpeg_config "$<CONFIG>")
    else()
        set(jpeg_config Release)
    endif()
    # Install all public headers together. In particular, jpeglib.h includes
    # "jconfig.h", which must be the configuration of this exact shared library.
    # Separate source/build include paths can pick up an unrelated host header.
    set(jpeg_include "${jpeg_stage}/${jpeg_config}/include")
    foreach(config IN ITEMS Debug Release RelWithDebInfo MinSizeRel ${CMAKE_CONFIGURATION_TYPES} ${CMAKE_BUILD_TYPE})
        file(MAKE_DIRECTORY "${jpeg_stage}/${config}/include")
    endforeach()
    file(MAKE_DIRECTORY "${qcopilots_search_jpeg_BINARY_DIR}")
    set(jpeg_cmake_args
        "-DCMAKE_C_COMPILER:FILEPATH=${CMAKE_C_COMPILER}"
        "-DCMAKE_BUILD_TYPE:STRING=${jpeg_config}"
        "-DCMAKE_INSTALL_PREFIX:PATH=${jpeg_stage}/${jpeg_config}"
        "-DCMAKE_INSTALL_LIBDIR:STRING=lib"
        "-DCMAKE_INSTALL_BINDIR:STRING=bin"
        "-DCMAKE_INSTALL_INCLUDEDIR:STRING=include"
        "-DENABLE_SHARED:BOOL=ON" "-DENABLE_STATIC:BOOL=OFF"
        "-DWITH_SIMD:BOOL=OFF" "-DREQUIRE_SIMD:BOOL=OFF"
        "-DWITH_TURBOJPEG:BOOL=OFF" "-DWITH_JAVA:BOOL=OFF"
        "-DWITH_TOOLS:BOOL=OFF" "-DWITH_TESTS:BOOL=OFF"
        "-DWITH_FUZZ:BOOL=OFF" "-DWITH_CRT_DLL:BOOL=ON"
        "-DWITH_JPEG7:BOOL=OFF" "-DWITH_JPEG8:BOOL=OFF"
        "-DBUILD:STRING=3.1.4.1")
    if(CMAKE_TOOLCHAIN_FILE)
        list(APPEND jpeg_cmake_args "-DCMAKE_TOOLCHAIN_FILE:FILEPATH=${CMAKE_TOOLCHAIN_FILE}")
    endif()
    if(APPLE)
        string(REPLACE ";" "|" jpeg_architectures "${CMAKE_OSX_ARCHITECTURES}")
        list(APPEND jpeg_cmake_args
            "-DCMAKE_OSX_ARCHITECTURES:STRING=${jpeg_architectures}"
            "-DCMAKE_OSX_DEPLOYMENT_TARGET:STRING=${CMAKE_OSX_DEPLOYMENT_TARGET}"
            "-DCMAKE_INSTALL_NAME_DIR:STRING=@rpath")
    endif()
    if(WIN32)
        set(jpeg_runtime "bin/jpeg62.dll")
        set(jpeg_import "lib/jpeg.lib")
    elseif(APPLE)
        set(jpeg_runtime "lib/libjpeg.62.dylib")
    else()
        set(jpeg_runtime "lib/libjpeg.so.62")
    endif()
    set(jpeg_byproducts "${jpeg_stage}/${jpeg_config}/${jpeg_runtime}")
    if(WIN32)
        list(APPEND jpeg_byproducts "${jpeg_stage}/${jpeg_config}/${jpeg_import}")
    endif()
    foreach(header IN ITEMS jconfig.h jerror.h jmorecfg.h jpeglib.h)
        list(APPEND jpeg_byproducts "${jpeg_include}/${header}")
    endforeach()
    ExternalProject_Add(qcopilots-search-build-jpeg
        SOURCE_DIR "${qcopilots_search_jpeg_SOURCE_DIR}"
        BINARY_DIR "${qcopilots_search_jpeg_BINARY_DIR}"
        PREFIX "${qcopilots_search_jpeg_BINARY_DIR}/external-project"
        DOWNLOAD_COMMAND "" UPDATE_COMMAND "" PATCH_COMMAND ""
        LIST_SEPARATOR "|"
        CMAKE_ARGS ${jpeg_cmake_args}
        BUILD_COMMAND "${CMAKE_COMMAND}" --build <BINARY_DIR> --config "${jpeg_config}" --target jpeg
        INSTALL_COMMAND "${CMAKE_COMMAND}" --install <BINARY_DIR> --config "${jpeg_config}"
            --prefix "${jpeg_stage}/${jpeg_config}"
        INSTALL_BYPRODUCTS ${jpeg_byproducts}
        EXCLUDE_FROM_ALL TRUE)
    add_library(qcopilots-search-native-jpeg SHARED IMPORTED GLOBAL)
    set_target_properties(qcopilots-search-native-jpeg PROPERTIES
        SYSTEM FALSE
        INTERFACE_INCLUDE_DIRECTORIES "${jpeg_include}"
        IMPORTED_LOCATION "${jpeg_stage}/Release/${jpeg_runtime}")
    if(WIN32)
        set_target_properties(qcopilots-search-native-jpeg PROPERTIES
            IMPORTED_IMPLIB "${jpeg_stage}/Release/${jpeg_import}")
    elseif(APPLE)
        # The ExternalProject library does not exist at initial configure time.
        # Declare its actual install name so CMake can generate consumer build
        # rpaths without trying to inspect that not-yet-built Mach-O file.
        set_target_properties(qcopilots-search-native-jpeg PROPERTIES
            IMPORTED_SONAME "@rpath/libjpeg.62.dylib")
    endif()
    foreach(config IN ITEMS Debug Release RelWithDebInfo MinSizeRel)
        string(TOUPPER "${config}" upper_config)
        set_property(TARGET qcopilots-search-native-jpeg APPEND PROPERTY IMPORTED_CONFIGURATIONS "${config}")
        set_target_properties(qcopilots-search-native-jpeg PROPERTIES
            IMPORTED_LOCATION_${upper_config} "${jpeg_stage}/${config}/${jpeg_runtime}")
        if(WIN32)
            set_target_properties(qcopilots-search-native-jpeg PROPERTIES
                IMPORTED_IMPLIB_${upper_config} "${jpeg_stage}/${config}/${jpeg_import}")
        elseif(APPLE)
            set_target_properties(qcopilots-search-native-jpeg PROPERTIES
                IMPORTED_SONAME_${upper_config} "@rpath/libjpeg.62.dylib")
        endif()
    endforeach()
    add_dependencies(qcopilots-search-native-jpeg qcopilots-search-build-jpeg)
    add_library(qcopilots-search::jpeg ALIAS qcopilots-search-native-jpeg)
    add_library(JPEG::JPEG ALIAS qcopilots-search-native-jpeg)
    include(GNUInstallDirs)
    if(WIN32)
        install(FILES "$<TARGET_FILE:qcopilots-search-native-jpeg>"
            DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT qcopilots-search-runtime)
    elseif(APPLE)
        install(DIRECTORY "${jpeg_stage}/${jpeg_config}/lib/"
            DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT qcopilots-search-runtime
            FILES_MATCHING PATTERN "libjpeg*.dylib")
    else()
        install(DIRECTORY "${jpeg_stage}/${jpeg_config}/lib/"
            DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT qcopilots-search-runtime
            FILES_MATCHING PATTERN "libjpeg.so*")
    endif()
    install(FILES "${qcopilots_search_jpeg_SOURCE_DIR}/LICENSE.md"
        "${qcopilots_search_jpeg_SOURCE_DIR}/README.ijg"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/libjpeg-turbo"
        COMPONENT qcopilots-search-runtime)
    install(FILES "${qcopilots_search_zlib_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/zlib"
        COMPONENT qcopilots-search-runtime)
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_IMAGE_IMPORTED_RUNTIME_TARGETS
        qcopilots-search-native-jpeg)
endfunction()

# Decode-only image libraries. Sources are pinned to official release archives
# and populated outside the checkout by qcopilots_search_fetch().
function(qcopilots_search_add_image_dependencies)
    if(TARGET qcopilots-search::heif AND TARGET qcopilots-search::raw)
        return()
    endif()
    qcopilots_search_add_image_codec_dependencies()

    # Normal variables are intentionally function-scoped. Do not change the
    # application's BUILD_TESTING or BUILD_SHARED_LIBS cache options.
    set(CMAKE_POLICY_DEFAULT_CMP0077 NEW)
    set(BUILD_SHARED_LIBS ON)
    set(BUILD_TESTING OFF)
    set(BUILD_FRAMEWORK OFF)
    set(BUILD_DOCUMENTATION OFF)
    set(BUILD_DEVELOPMENT_TOOLS OFF)
    set(WITH_FUZZERS OFF)
    set(ENABLE_COVERAGE OFF)

    qcopilots_search_fetch(qcopilots_search_libde265
        "https://github.com/strukturag/libde265/releases/download/v1.0.17/libde265-1.0.17.tar.gz"
        "e919bbe34370fbcfa36c48ecc6efd5c861f7df43b9a58210e68350d43bab71a5")
    set(ENABLE_SDL OFF)
    # ENABLE_DECODER controls the CLI, not decoding in the library.
    set(ENABLE_DECODER OFF)
    set(ENABLE_ENCODER OFF)
    set(ENABLE_SHERLOCK265 OFF)
    set(ENABLE_TOOLS OFF)
    set(USE_IWYU OFF)
    add_subdirectory("${qcopilots_search_libde265_SOURCE_DIR}"
        "${qcopilots_search_libde265_BINARY_DIR}" EXCLUDE_FROM_ALL)
    find_package(Threads REQUIRED)
    target_link_libraries(de265 PRIVATE Threads::Threads)
    target_include_directories(de265 PUBLIC
        "$<BUILD_INTERFACE:${qcopilots_search_libde265_SOURCE_DIR}>"
        "$<BUILD_INTERFACE:${qcopilots_search_libde265_BINARY_DIR}>")

    qcopilots_search_fetch(qcopilots_search_libheif
        "https://github.com/strukturag/libheif/releases/download/v1.23.6/libheif-1.23.6.tar.gz"
        "4484346dc5995319dbc11e3a1c35d0a2ec46511ce370900869337fd2c7033125")
    set(ENABLE_PLUGIN_LOADING OFF)
    set(WITH_EXAMPLES OFF)
    set(WITH_EXAMPLE_HEIF_THUMB OFF)
    set(WITH_EXAMPLE_HEIF_VIEW OFF)
    set(WITH_GDK_PIXBUF OFF)
    set(ENABLE_EXPERIMENTAL_FEATURES OFF)
    set(WITH_LIBSHARPYUV OFF)
    set(WITH_LIBSHARPYUV_INTERNAL OFF)
    set(WITH_HEADER_COMPRESSION OFF)
    set(WITH_UNCOMPRESSED_CODEC OFF)
    set(WITH_WEBCODECS OFF)
    # Some upstream code branches use *_FOUND independently of WITH_*.
    # Mask values inherited from OCR/media dependencies in the parent scope.
    foreach(dependency IN ITEMS X265 KVAZAAR AOM_DECODER AOM_ENCODER AOM DAV1D
            SvtEnc RAV1E JPEG OpenJPEG OPENJPEG OPENJPH UVG266 vvenc vvdec
            X264 OpenH264 OpenH264_DECODER FFMPEG FFMPEG_avcodec WEBCODECS
            LIBSHARPYUV ZLIB Brotli)
        set(${dependency}_FOUND FALSE)
    endforeach()
    foreach(codec IN ITEMS X265 KVAZAAR UVG266 VVDEC VVENC X264
            OpenH264_DECODER OpenH264_ENCODER DAV1D AOM_DECODER AOM_ENCODER
            SvtEnc RAV1E JPEG_DECODER JPEG_ENCODER OpenJPEG_DECODER
            OpenJPEG_ENCODER FFMPEG_DECODER OPENJPH_DECODER OPENJPH_ENCODER)
        set(WITH_${codec} OFF)
        set(WITH_${codec}_PLUGIN OFF)
    endforeach()
    set(WITH_LIBDE265 ON)
    set(WITH_LIBDE265_PLUGIN OFF)
    # Use the target built above, never an unrelated system installation.
    # Upstream's legacy FindLIBDE265 module accepts the singular variables and
    # skips its package processing when the plural contract is already set.
    set(LIBDE265_FOUND TRUE)
    set(LIBDE265_VERSION "1.0.17")
    set(LIBDE265_INCLUDE_DIR "${qcopilots_search_libde265_SOURCE_DIR}")
    set(LIBDE265_INCLUDE_DIRS
        "${qcopilots_search_libde265_SOURCE_DIR};${qcopilots_search_libde265_BINARY_DIR}")
    set(LIBDE265_LIBRARY de265)
    set(LIBDE265_LIBRARIES de265)
    add_subdirectory("${qcopilots_search_libheif_SOURCE_DIR}"
        "${qcopilots_search_libheif_BINARY_DIR}" EXCLUDE_FROM_ALL)
    # 1.23.6 exports a non-existing source/include directory and marks its
    # consumers dllexport. Correct only the build interface, leaving the pinned
    # third-party archive untouched and consumers using dllimport on Windows.
    set_property(TARGET heif PROPERTY INTERFACE_COMPILE_DEFINITIONS "")
    set_property(TARGET heif PROPERTY INTERFACE_INCLUDE_DIRECTORIES
        "$<BUILD_INTERFACE:${qcopilots_search_libheif_SOURCE_DIR}/libheif/api>"
        "$<BUILD_INTERFACE:${qcopilots_search_libheif_BINARY_DIR}>"
        "$<INSTALL_INTERFACE:include>")
    add_library(qcopilots-search::heif ALIAS heif)

    qcopilots_search_fetch(qcopilots_search_libraw
        "https://www.libraw.org/data/LibRaw-0.22.2.tar.gz"
        "de86b035655accff8d4010f1a221fdf50d353cb7b1422ba26f14a0db92612cfa")
    # The official release has no CMake project. This is the same source set as
    # lib_libraw_a_SOURCES in its Makefile.am. The *_ph files are alternative
    # placeholder implementations and must not be linked into the full library.
    file(GLOB_RECURSE raw_sources CONFIGURE_DEPENDS
        "${qcopilots_search_libraw_SOURCE_DIR}/src/*.cpp")
    list(FILTER raw_sources EXCLUDE REGEX "/[^/]*_ph\\.cpp$")
    list(LENGTH raw_sources raw_source_count)
    if(NOT raw_source_count EQUAL 79)
        message(FATAL_ERROR "The pinned LibRaw source manifest changed: expected 79 compilation units")
    endif()
    add_library(qcopilots-search-libraw SHARED ${raw_sources})
    target_compile_features(qcopilots-search-libraw PRIVATE cxx_std_20)
    target_compile_definitions(qcopilots-search-libraw PRIVATE
        LIBRAW_BUILDLIB LIBRAW_CALLOC_RAWSTORE _FILE_OFFSET_BITS=64
        USE_JPEG USE_JPEG8 USE_ZLIB)
    target_link_libraries(qcopilots-search-libraw PRIVATE
        Threads::Threads JPEG::JPEG ZLIB::ZLIB)
    qcopilots_search_add_jpeg_contract(qcopilots-search-libraw)
    target_include_directories(qcopilots-search-libraw PUBLIC
        "$<BUILD_INTERFACE:${qcopilots_search_libraw_SOURCE_DIR}>"
        "$<INSTALL_INTERFACE:include>"
        PRIVATE "${qcopilots_search_libraw_SOURCE_DIR}/internal")
    if(WIN32)
        target_link_libraries(qcopilots-search-libraw PRIVATE ws2_32)
        add_custom_command(TARGET qcopilots-search-libraw POST_BUILD
            COMMAND "${CMAKE_COMMAND}" -E copy_if_different
                "$<TARGET_FILE:qcopilots-search-native-jpeg>"
                "$<TARGET_FILE_DIR:qcopilots-search-libraw>" VERBATIM)
    endif()
    if(MSVC)
        target_compile_definitions(qcopilots-search-libraw PRIVATE
            _CRT_SECURE_NO_WARNINGS NOMINMAX)
        target_compile_options(qcopilots-search-libraw PRIVATE /utf-8)
    elseif(UNIX)
        target_link_libraries(qcopilots-search-libraw PRIVATE m)
    endif()
    # No optional RawSpeed, DNG SDK, GPL demosaic pack, OpenMP or LCMS
    # dependency is implicitly discovered. Unsupported RAW subformats are
    # reported by LibRaw rather than changing with the developer's machine.
    set_target_properties(qcopilots-search-libraw PROPERTIES
        OUTPUT_NAME raw VERSION 25.0.0 SOVERSION 25
        CXX_VISIBILITY_PRESET default VISIBILITY_INLINES_HIDDEN OFF)
    add_library(qcopilots-search::raw ALIAS qcopilots-search-libraw)

    # The caller owns install destinations and runtime bundling.
    set(QCOPILOTS_SEARCH_IMAGE_RUNTIME_TARGETS
        de265 heif qcopilots-search-libraw qcopilots-search-native-zlib PARENT_SCOPE)
    set(QCOPILOTS_SEARCH_LIBDE265_SOURCE_DIR
        "${qcopilots_search_libde265_SOURCE_DIR}" PARENT_SCOPE)
    set(QCOPILOTS_SEARCH_LIBHEIF_SOURCE_DIR
        "${qcopilots_search_libheif_SOURCE_DIR}" PARENT_SCOPE)
    set(QCOPILOTS_SEARCH_LIBRAW_SOURCE_DIR
        "${qcopilots_search_libraw_SOURCE_DIR}" PARENT_SCOPE)
endfunction()
