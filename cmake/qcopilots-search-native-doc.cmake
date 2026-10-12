include_guard(GLOBAL)

# Calligra's maintained wv2 parser can run independently of its KDE application.
# Only POLE is needed from libmso. Building all of libmso would introduce the ODF
# renderer and GUI framework, neither of which belongs in a text worker.
function(qcopilots_search_add_doc_dependency)
    if(TARGET qcopilots-search::wv2)
        return()
    endif()
    find_package(Qt6 6.12 REQUIRED COMPONENTS Core Core5Compat)
    qcopilots_search_fetch(qcopilots_search_calligra
        "https://codeload.github.com/KDE/calligra/tar.gz/88bde0ce71a5c12fb50786017b2e2e07c5ce13e4"
        "52611a41dc5d3d741f13adfaa5e8c1621a37f2a6fb19e74c038eed64d8e39a19")
    set(wv2_root "${qcopilots_search_calligra_SOURCE_DIR}/filters/words/msword-odf/wv2")
    set(wv2_source "${wv2_root}/src")
    set(pole_source "${qcopilots_search_calligra_SOURCE_DIR}/filters/libmso")
    set(wv2_binary "${qcopilots_search_calligra_BINARY_DIR}/qcopilots-search-wv2")
    file(MAKE_DIRECTORY "${wv2_binary}/include" "${wv2_binary}/private")

    # Generate only external build-tree copies. Patch anchors are checked so an
    # upstream update cannot silently discard the Unicode-path adaptations.
    file(READ "${pole_source}/pole.cpp" pole_content)
    string(FIND "${pole_content}" "file.open(filename.c_str()," pole_anchor)
    if(pole_anchor EQUAL -1)
        message(FATAL_ERROR "Calligra POLE UTF-8 path patch anchor changed")
    endif()
    string(REPLACE "#include <fstream>" "#include <fstream>\n#include <filesystem>" pole_content "${pole_content}")
    string(REPLACE "file.open(filename.c_str()," "file.open(std::filesystem::u8path(filename)," pole_content "${pole_content}")
    file(WRITE "${wv2_binary}/pole.cpp" "${pole_content}")

    file(READ "${wv2_source}/parserfactory.cpp" parser_content)
    string(FIND "${parser_content}" "FILE *file = fopen(fileName.c_str(), \"r\");" parser_anchor)
    if(parser_anchor EQUAL -1)
        message(FATAL_ERROR "Calligra ParserFactory UTF-8 path patch anchor changed")
    endif()
    string(REPLACE "#include <stdio.h>" "#include <stdio.h>\n#include <QFile>" parser_content "${parser_content}")
    string(REPLACE "FILE *file = fopen(fileName.c_str(), \"r\");\n        if (!file) {"
        "QFile file(QString::fromUtf8(fileName.data(), static_cast<qsizetype>(fileName.size())));\n        if (!file.open(QIODevice::ReadOnly)) {"
        parser_content "${parser_content}")
    string(REPLACE "unsigned char buffer[4];\n        fread(buffer, 1, 4, file);\n        diagnose(buffer);\n        fclose(file);"
        "unsigned char buffer[4] = {};\n        if (file.read(reinterpret_cast<char *>(buffer), 4) == 4)\n            diagnose(buffer);"
        parser_content "${parser_content}")
    file(WRITE "${wv2_binary}/parserfactory.cpp" "${parser_content}")

    # wv2's sole iconv consumer converts legacy code pages to UCS-2. A private
    # adapter uses the exact Qt SDK's Core5Compat tables across all platforms.
    configure_file("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcopilots-search-native-doc-iconv.h"
        "${wv2_binary}/private/iconv.h" COPYONLY)
    include(TestBigEndian)
    test_big_endian(WORDS_BIGENDIAN)
    set(HAVE_ICONV_H 1)
    set(ICONV_REQUIRES_CONST 1)
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
    add_library(qcopilots-search-native-wv2 SHARED ${wv2_sources}
        "${wv2_binary}/pole.cpp" "${wv2_binary}/parserfactory.cpp")
    include(GenerateExportHeader)
    generate_export_header(qcopilots-search-native-wv2 BASE_NAME wv2
        EXPORT_FILE_NAME "${wv2_binary}/include/wv2_export.h")
    target_include_directories(qcopilots-search-native-wv2 SYSTEM PUBLIC
        "$<BUILD_INTERFACE:${wv2_source}>" "$<BUILD_INTERFACE:${wv2_binary}/include>")
    target_include_directories(qcopilots-search-native-wv2 PRIVATE "${pole_source}" "${wv2_binary}/private")
    target_link_libraries(qcopilots-search-native-wv2 PUBLIC Qt6::Core PRIVATE Qt6::Core5Compat)
    target_compile_features(qcopilots-search-native-wv2 PRIVATE cxx_std_20)
    target_compile_definitions(qcopilots-search-native-wv2 PRIVATE
        HAVE_CONFIG_H MAKE_WV2_LIB UNICODE _UNICODE NOMINMAX
        "$<$<CXX_COMPILER_ID:MSVC>:_CRT_SECURE_NO_WARNINGS>")
    if(MSVC)
        target_compile_options(qcopilots-search-native-wv2 PRIVATE /utf-8 /bigobj)
    endif()
    set_target_properties(qcopilots-search-native-wv2 PROPERTIES
        AUTOMOC OFF AUTOUIC OFF AUTORCC OFF
        VERSION 0.9.0 SOVERSION 0
        POSITION_INDEPENDENT_CODE ON)
    add_library(qcopilots-search::wv2 ALIAS qcopilots-search-native-wv2)
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_DOC_RUNTIME_TARGETS qcopilots-search-native-wv2)
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_DOC_SOURCE_DIR "${qcopilots_search_calligra_SOURCE_DIR}")
    install(FILES "${wv2_root}/COPYING.LIB"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/wv2" COMPONENT qcopilots-search-runtime)
    # POLE's full BSD notice is in its public header, not a separate license.
    install(FILES "${pole_source}/pole.h"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/pole" COMPONENT qcopilots-search-runtime)
endfunction()
