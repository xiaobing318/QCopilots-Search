include_guard(GLOBAL)
include("${CMAKE_CURRENT_LIST_DIR}/qcs-write-if-different.cmake")

# The embedded Leptonica bitmap fonts are CCITT G4 TIFF images. Keep this
# decoder independent of all optional TIFF external codecs and system packages.
function(qcopilots_search_add_ocr_tiff_dependency)
    set(CMAKE_POLICY_DEFAULT_CMP0077 NEW)
    set(CMAKE_AUTOMOC OFF)
    set(CMAKE_AUTORCC OFF)
    set(CMAKE_AUTOUIC OFF)
    set(BUILD_SHARED_LIBS ON)
    foreach(tiff_option IN ITEMS tiff-static tiff-tools tiff-tests tiff-contrib
            tiff-docs tiff-install tiff-cxx tiff-opengl tiff-framework
            cxx-compat-mode extra-warnings broken-warnings fatal-warnings
            cxx-compat-warnings packbits lzw thunder next logluv mdi zlib
            libdeflate pixarlog jpeg old-jpeg jpeg12 jbig lerc lzma zstd webp)
        set(${tiff_option} OFF)
    endforeach()
    set(ccitt ON)
    set(jpeg-prefer-standard ON)
    foreach(package IN ITEMS ZLIB Deflate libjpeg-turbo JPEG JBIG LERC
                             liblzma ZSTD WebP OpenGL GLUT)
        set(CMAKE_DISABLE_FIND_PACKAGE_${package} TRUE)
        set(${package}_FOUND FALSE)
    endforeach()
    foreach(found IN ITEMS JPEG12_FOUND LIBLZMA_FOUND OPENGL_FOUND OPENGL_GLU_FOUND)
        set(${found} FALSE)
    endforeach()
    add_subdirectory("${qcs_tiff_SOURCE_DIR}"
                     "${qcs_tiff_BINARY_DIR}" EXCLUDE_FROM_ALL)
endfunction()

function(qcopilots_search_add_ocr_codec_dependencies)
    if(NOT TARGET qcs::zlib OR NOT TARGET qcs::jpeg)
        message(FATAL_ERROR "Create the pinned image codec dependencies before OCR")
    endif()
    if(TARGET png_shared OR TARGET tiff)
        message(FATAL_ERROR "OCR codecs require the pinned QCopilots Search targets")
    endif()
    qcopilots_search_fetch(qcs_png
        "https://github.com/pnggroup/libpng/archive/refs/tags/v1.6.59.tar.gz"
        "2540302a1844ad2b2b501977abecfa850f265f97b78f065a712ab4074a89f5b5")
    qcopilots_search_fetch(qcs_tiff
        "https://download.osgeo.org/libtiff/tiff-4.7.2.tar.gz"
        "672bd7d10aee4606171afb864f3570b83340f6a33e2c186dc0512f7145ffdf6a")
    FetchContent_GetProperties(qcs_zlib)
    FetchContent_GetProperties(qcs_jpeg)

    # These private find modules never search the host. They bridge upstream
    # variable-based discovery to the exact targets already built in this tree.
    set(find_modules "${qcs_png_BINARY_DIR}/find")
    file(MAKE_DIRECTORY "${find_modules}")
    qcopilots_search_write_if_different("${find_modules}/FindZLIB.cmake"
        "set(ZLIB_FOUND TRUE)\nset(ZLIB_VERSION 1.3.2)\n"
        "set(ZLIB_LIBRARY ZLIB::ZLIB)\nset(ZLIB_LIBRARIES ZLIB::ZLIB)\n"
        "set(ZLIB_INCLUDE_DIR [==[${qcs_zlib_SOURCE_DIR}]==])\n"
        "set(ZLIB_INCLUDE_DIRS [==[${qcs_zlib_SOURCE_DIR}]==])\n")
    qcopilots_search_write_if_different("${find_modules}/FindJPEG.cmake"
        "set(JPEG_FOUND TRUE)\nset(JPEG_VERSION 3.1.4.1)\n"
        "set(JPEG_LIBRARY JPEG::JPEG)\nset(JPEG_LIBRARIES JPEG::JPEG)\n"
        "get_target_property(JPEG_INCLUDE_DIR JPEG::JPEG INTERFACE_INCLUDE_DIRECTORIES)\n"
        "set(JPEG_INCLUDE_DIRS \"\${JPEG_INCLUDE_DIR}\")\n")
    qcopilots_search_write_if_different("${find_modules}/FindPNG.cmake"
        "set(PNG_FOUND TRUE)\nset(PNG_VERSION 1.6.59)\n"
        "set(PNG_LIBRARY PNG::PNG)\nset(PNG_LIBRARIES PNG::PNG)\n"
        "set(PNG_INCLUDE_DIR [==[${qcs_png_SOURCE_DIR};${qcs_png_BINARY_DIR}]==])\n"
        "set(PNG_INCLUDE_DIRS [==[${qcs_png_SOURCE_DIR};${qcs_png_BINARY_DIR}]==])\n")
    qcopilots_search_write_if_different("${find_modules}/FindTIFF.cmake"
        "set(TIFF_FOUND TRUE)\nset(TIFF_VERSION 4.7.2)\n"
        "set(TIFF_LIBRARY TIFF::tiff)\nset(TIFF_LIBRARIES TIFF::tiff)\n"
        "set(TIFF_INCLUDE_DIR [==[${qcs_tiff_SOURCE_DIR}/libtiff;${qcs_tiff_BINARY_DIR}/libtiff]==])\n"
        "set(TIFF_INCLUDE_DIRS [==[${qcs_tiff_SOURCE_DIR}/libtiff;${qcs_tiff_BINARY_DIR}/libtiff]==])\n")
    list(PREPEND CMAKE_MODULE_PATH "${find_modules}")
    set(CMAKE_POLICY_DEFAULT_CMP0077 NEW)
    set(CMAKE_POLICY_DEFAULT_CMP0126 NEW)
    set(CMAKE_AUTOMOC OFF)
    set(CMAKE_AUTORCC OFF)
    set(CMAKE_AUTOUIC OFF)
    set(PNG_SHARED ON)
    set(PNG_STATIC OFF)
    set(PNG_FRAMEWORK OFF)
    set(PNG_TESTS OFF)
    set(PNG_TOOLS OFF)
    set(PNG_HARDWARE_OPTIMIZATIONS OFF)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
        # A libpng16.so.16 loaded beside system FreeType must export PNG16_0.
        # Upstream only generates libpng.vers in the AWK configuration branch.
        # The prebuilt header skips that branch and produces an incompatible
        # unversioned-symbol library despite retaining the same ELF soname.
        set(PNG_LIBCONF_HEADER "")
        set(ld-version-script ON)
        find_program(AWK NAMES gawk nawk awk REQUIRED)
    else()
        set(PNG_LIBCONF_HEADER "${qcs_png_SOURCE_DIR}/scripts/pnglibconf.h.prebuilt")
    endif()
    set(SKIP_INSTALL_ALL ON)
    add_subdirectory("${qcs_png_SOURCE_DIR}"
                     "${qcs_png_BINARY_DIR}" EXCLUDE_FROM_ALL)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
        get_target_property(png_link_flags png_shared LINK_FLAGS)
        if(NOT png_link_flags MATCHES "--version-script=")
            message(FATAL_ERROR "The Linux libpng build must export the PNG16_0 symbol version")
        endif()
    endif()
    add_library(PNG::PNG ALIAS png_shared)
    add_library(qcs::png ALIAS png_shared)
    qcopilots_search_add_ocr_tiff_dependency()
    add_library(qcs::tiff ALIAS tiff)

    # Leptonica exports its transitive shared-library dependencies. Register an
    # internal export set to satisfy that contract without installing developer
    # metadata or duplicating the application's explicit runtime install rules.
    export(TARGETS png_shared tiff qcs-native-zlib
        FILE "${qcs_png_BINARY_DIR}/qcs-codecs.cmake")
    install(TARGETS png_shared tiff qcs-native-zlib
        EXPORT qcs-ocr-codecs
        RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" EXCLUDE_FROM_ALL
        LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}" EXCLUDE_FROM_ALL
        ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}" EXCLUDE_FROM_ALL)
    install(EXPORT qcs-ocr-codecs
        DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/qcs" EXCLUDE_FROM_ALL)
    install(FILES "${qcs_png_SOURCE_DIR}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/libpng"
        COMPONENT qcs-runtime)
    install(DIRECTORY "${qcs_png_SOURCE_DIR}/LICENSES/"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/libpng"
        COMPONENT qcs-runtime)
    install(FILES "${qcs_tiff_SOURCE_DIR}/LICENSE.md"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/libtiff"
        COMPONENT qcs-runtime)
    set(QCOPILOTS_SEARCH_OCR_FIND_MODULE_DIR "${find_modules}" PARENT_SCOPE)
endfunction()

# Qt decodes user images before passing PIX pixels to Tesseract. Leptonica also
# needs TIFF/PNG internally for its font data and in-memory image serialization.
function(qcopilots_search_add_ocr_dependencies)
    if(TARGET qcs::tesseract)
        return()
    endif()
    if(NOT COMMAND qcopilots_search_fetch)
        message(FATAL_ERROR "Load qcs native dependency helpers before OCR")
    endif()
    if(TARGET leptonica OR TARGET libtesseract)
        message(FATAL_ERROR "OCR requires the pinned QCopilots Search dependency targets")
    endif()

    qcopilots_search_fetch(qcs_leptonica
        "https://github.com/DanBloomberg/leptonica/releases/download/1.87.0/leptonica-1.87.0.tar.gz"
        "c73363397f96eb1295602bf44d708a994ad42046c791bf03ea0505d829bdb6a7")
    qcopilots_search_fetch(qcs_tesseract
        "https://github.com/tesseract-ocr/tesseract/archive/refs/tags/5.5.3.tar.gz"
        "9218e62793116d42a9f6d14cd9348518b27f382096eea3d0f2d1a24616bb5884")
    qcopilots_search_add_ocr_codec_dependencies()
    list(PREPEND CMAKE_MODULE_PATH "${QCOPILOTS_SEARCH_OCR_FIND_MODULE_DIR}")

    # Normal variables and CMP0077 isolate upstream switches from the project's
    # own options, while overriding any unrelated options in the parent cache.
    set(CMAKE_POLICY_DEFAULT_CMP0077 NEW)
    set(BUILD_SHARED_LIBS ON)
    set(BUILD_PROG OFF)
    set(SW_BUILD OFF)
    set(SYM_LINK OFF)
    set(STRICT_CONF ON)
    foreach(codec IN ITEMS ZLIB PNG GIF JPEG TIFF WEBP OPENJPEG)
        set(ENABLE_${codec} OFF)
    endforeach()
    # Leptonica gates tiffio.c on BOTH HAVE_LIBTIFF and HAVE_LIBJPEG, even
    # when the TIFF decoder itself needs only its built-in CCITT G4 codec.
    foreach(codec IN ITEMS ZLIB PNG JPEG TIFF)
        set(ENABLE_${codec} ON)
    endforeach()
    # Upstream checks these again when linking, even with the codec disabled.
    # Do not inherit libraries discovered by unrelated parent subdirectories.
    foreach(package IN ITEMS ZLIB PNG GIF JPEG TIFF WebP OPENJPEG)
        set(${package}_FOUND FALSE)
        set(${package}_LIBRARIES "")
    endforeach()
    foreach(feature IN ITEMS HAVE_LIBGIF HAVE_LIBJP2K HAVE_LIBJPEG HAVE_LIBPNG
                             HAVE_LIBTIFF HAVE_LIBWEBP HAVE_LIBWEBP_ANIM HAVE_LIBZ
                             OPENJPEG_SUPPORT LIBWEBP_SUPPORT)
        set(${feature} FALSE)
    endforeach()
    set(CMAKE_DISABLE_FIND_PACKAGE_PkgConfig TRUE)
    add_subdirectory("${qcs_leptonica_SOURCE_DIR}"
                     "${qcs_leptonica_BINARY_DIR}" EXCLUDE_FROM_ALL)
    qcopilots_search_add_jpeg_contract(leptonica)
    # Expose only the generated public header. The private config_auto.h has
    # the same basename as Tesseract's and must not shadow its configuration.
    set(leptonica_public_config_dir "${qcs_leptonica_BINARY_DIR}/include")
    file(MAKE_DIRECTORY "${leptonica_public_config_dir}")
    configure_file("${qcs_leptonica_BINARY_DIR}/src/endianness.h"
                   "${leptonica_public_config_dir}/endianness.h" COPYONLY)
    target_include_directories(leptonica PUBLIC
        "$<BUILD_INTERFACE:${qcs_leptonica_SOURCE_DIR}/src>"
        "$<BUILD_INTERFACE:${leptonica_public_config_dir}>")
    target_compile_definitions(leptonica INTERFACE "$<$<PLATFORM_ID:Windows>:LIBLEPT_IMPORTS>")
    add_library(qcs::leptonica ALIAS leptonica)

    # Tesseract expects an installed Leptonica Config package. Supply a private
    # build-tree package referencing the already-created pinned target instead.
    set(Leptonica_DIR "${qcs_tesseract_BINARY_DIR}/lept")
    file(MAKE_DIRECTORY "${Leptonica_DIR}")
    qcopilots_search_write_if_different("${Leptonica_DIR}/LeptonicaConfig.cmake"
        "set(Leptonica_FOUND TRUE)\n"
        "set(Leptonica_VERSION 1.87.0)\n"
        "set(Leptonica_LIBRARIES leptonica)\n"
        "set(Leptonica_INCLUDE_DIRS [==[${qcs_leptonica_SOURCE_DIR}/src;${leptonica_public_config_dir}]==])\n")
    include(CMakePackageConfigHelpers)
    write_basic_package_version_file("${Leptonica_DIR}/LeptonicaConfigVersion.cmake"
        VERSION 1.87.0 COMPATIBILITY AnyNewerVersion)

    set(OPENMP_BUILD OFF)
    set(GRAPHICS_DISABLED ON)
    set(DISABLED_LEGACY_ENGINE ON)
    set(ENABLE_LTO OFF)
    set(FAST_FLOAT ON)
    set(ENABLE_NATIVE OFF)
    set(BUILD_TRAINING_TOOLS OFF)
    set(BUILD_TESTS OFF)
    set(USE_SYSTEM_ICU OFF)
    set(DISABLE_TIFF ON)
    set(DISABLE_ARCHIVE ON)
    set(DISABLE_CURL ON)
    set(INSTALL_CONFIGS OFF)
    set(ENABLE_UNITY_BUILD OFF)
    set(ENABLE_PRECOMPILED_HEADERS ON)
    set(ENABLE_CCACHE OFF)
    set(ENABLE_NINJA_POOL OFF)
    set(OpenMP_CXX_FOUND FALSE)
    set(LibArchive_FOUND FALSE)
    set(CURL_FOUND FALSE)
    set(PACKAGE_VERSION "5.5.3")

    # Upstream creates a CLI unconditionally. EXCLUDE_FROM_ALL keeps it and its
    # install rules out of the application. The caller installs only the two
    # shared-library runtime artifacts alongside the application worker.
    add_subdirectory("${qcs_tesseract_SOURCE_DIR}"
                     "${qcs_tesseract_BINARY_DIR}" EXCLUDE_FROM_ALL)
    set_target_properties(tesseract PROPERTIES EXCLUDE_FROM_ALL TRUE)
    target_include_directories(libtesseract BEFORE PRIVATE
        "${qcs_tesseract_BINARY_DIR}")
    target_include_directories(libtesseract PUBLIC
        "$<BUILD_INTERFACE:${qcs_tesseract_BINARY_DIR}/include>")
    add_library(qcs::tesseract ALIAS libtesseract)

    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_OCR_RUNTIME_TARGETS "leptonica;libtesseract;png_shared;tiff")
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_LEPTONICA_VERSION "1.87.0")
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_TESSERACT_VERSION "5.5.3")
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_LEPTONICA_SOURCE_DIR "${qcs_leptonica_SOURCE_DIR}")
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_TESSERACT_SOURCE_DIR "${qcs_tesseract_SOURCE_DIR}")
endfunction()
