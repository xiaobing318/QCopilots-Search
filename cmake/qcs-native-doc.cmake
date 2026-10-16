include_guard(GLOBAL)
include("${CMAKE_CURRENT_LIST_DIR}/qcs-write-if-different.cmake")

# Calligra's maintained wv2 parser can run independently of its KDE application.
# Only POLE is needed from libmso. Building all of libmso would introduce the ODF
# renderer and GUI framework, neither of which belongs in a text worker.
function(qcopilots_search_add_doc_dependency)
    if(TARGET qcs::wv2)
        return()
    endif()
    find_package(Qt6 6.12 REQUIRED COMPONENTS Core)
    qcopilots_search_fetch(qcs_calligra
        "https://codeload.github.com/KDE/calligra/tar.gz/72123cc643f6625351139e7df294544255a1056f"
        "7d2d1c7596f226d4aca6cba64bf2a9f4d16b5aca59245ed2b8dba10900856f38")
    set(wv2_root "${qcs_calligra_SOURCE_DIR}/filters/words/msword-odf/wv2")
    set(wv2_source "${wv2_root}/src")
    set(pole_source "${qcs_calligra_SOURCE_DIR}/filters/libmso")
    set(wv2_binary "${qcs_calligra_BINARY_DIR}/wv2")
    file(MAKE_DIRECTORY "${wv2_binary}/include" "${wv2_binary}/private")

    include("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcs-native-iconv.cmake")
    qcopilots_search_add_iconv_dependency()
    include(TestBigEndian)
    test_big_endian(WORDS_BIGENDIAN)
    set(HAVE_ICONV_H 1)
    set(ICONV_REQUIRES_CONST 0)
    set(HAVE_STRING_H 1)
    set(HAVE_MATH_H 1)
    set(HAVE_FLOAT_H 1)
    if(WIN32)
        set(HAVE_FUNC__FINITE 1)
        set(SIZEOF_LONG 4)
    else()
        set(HAVE_STRINGS_H 1)
        set(HAVE_FUNC_ISNAN 1)
        set(HAVE_FUNC_ISINF 1)
        set(SIZEOF_LONG 8)
    endif()
    set(SIZEOF_CHAR 1)
    set(SIZEOF_SHORT 2)
    set(SIZEOF_INT 4)
    set(SIZEOF_VOID_P "${CMAKE_SIZEOF_VOID_P}")
    configure_file("${wv2_root}/config.h.cmake" "${wv2_binary}/private/config.h")

    set(wv2_sources
        annotations.cpp olestorage.cpp olestream.cpp word97_helper.cpp
        word95_helper.cpp word_helper.cpp global.cpp parser.cpp parser9x.cpp
        parser95.cpp parser97.cpp ustring.cpp textconverter.cpp styles.cpp
        handlers.cpp properties97.cpp fonts.cpp lists.cpp paragraphproperties.cpp
        headers.cpp headers95.cpp headers97.cpp footnotes97.cpp bookmark.cpp
        functor.cpp functordata.cpp associatedstrings.cpp fields.cpp utilities.cpp
        crc32.c graphics.cpp wvlog.cpp word95_generated.cpp word97_generated.cpp
        convert.cpp)
    list(TRANSFORM wv2_sources PREPEND "${wv2_source}/")
    add_library(qcs-native-wv2 SHARED ${wv2_sources}
        "${pole_source}/pole.cpp" "${wv2_source}/parserfactory.cpp")
    include(GenerateExportHeader)
    generate_export_header(qcs-native-wv2 BASE_NAME wv2
        EXPORT_FILE_NAME "${wv2_binary}/include/wv2_export.h")
    target_include_directories(qcs-native-wv2 SYSTEM PUBLIC
        "$<BUILD_INTERFACE:${wv2_source}>" "$<BUILD_INTERFACE:${wv2_binary}/include>")
    target_include_directories(qcs-native-wv2 PRIVATE "${pole_source}" "${wv2_binary}/private")
    target_link_libraries(qcs-native-wv2 PUBLIC Qt6::Core PRIVATE qcs::iconv)
    target_compile_features(qcs-native-wv2 PRIVATE cxx_std_20)
    target_compile_definitions(qcs-native-wv2 PRIVATE
        HAVE_CONFIG_H MAKE_WV2_LIB UNICODE _UNICODE NOMINMAX
        "$<$<CXX_COMPILER_ID:MSVC>:_CRT_SECURE_NO_WARNINGS>")
    if(MSVC)
        target_compile_options(qcs-native-wv2 PRIVATE /utf-8 /bigobj)
    endif()
    set_target_properties(qcs-native-wv2 PROPERTIES
        AUTOMOC OFF AUTOUIC OFF AUTORCC OFF
        VERSION 0.9.0 SOVERSION 0
        POSITION_INDEPENDENT_CODE ON)
    add_library(qcs::wv2 ALIAS qcs-native-wv2)
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_DOC_RUNTIME_TARGETS "qcs-native-wv2;qcs-native-iconv")
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_DOC_SOURCE_DIR "${qcs_calligra_SOURCE_DIR}")
    install(FILES "${wv2_root}/COPYING.LIB"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/wv2" COMPONENT qcs-runtime)
    # POLE's full BSD notice is in its public header, not a separate license.
    install(FILES "${pole_source}/pole.h"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/pole" COMPONENT qcs-runtime)
endfunction()
