include_guard(GLOBAL)

# Compile GNU's release sources unchanged. Its configure-generated public
# headers and the platform feature configuration belong only to the build tree.
function(qcopilots_search_add_iconv_dependency)
    if(TARGET qcs::iconv)
        return()
    endif()
    qcopilots_search_fetch(qcs_libiconv
        "https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.19.tar.gz"
        "88dd96a8c0464eca144fc791ae60cd31cd8ee78321e67397e25fc095c4a19aa6")
    include(CheckSymbolExists)
    include(CheckCSourceCompiles)
    include(TestBigEndian)
    check_symbol_exists(EILSEQ "errno.h" HAVE_EILSEQ)
    if(NOT HAVE_EILSEQ)
        message(FATAL_ERROR "GNU libiconv requires EILSEQ on this platform")
    endif()
    check_symbol_exists(mbrtowc "wchar.h" HAVE_MBRTOWC)
    check_symbol_exists(wcrtomb "wchar.h" HAVE_WCRTOMB)
    check_symbol_exists(setlocale "locale.h" HAVE_SETLOCALE)
    check_symbol_exists(CODESET "langinfo.h" HAVE_LANGINFO_CODESET)
    check_c_source_compiles("#include <wchar.h>\nint main(void) { mbstate_t s = {0}; return sizeof(s) == 0; }" USE_MBSTATE_T)
    test_big_endian(WORDS_BIGENDIAN)
    if(NOT WORDS_BIGENDIAN)
        set(WORDS_LITTLEENDIAN 1)
    endif()
    set(HAVE_VISIBILITY 0)
    set(BROKEN_WCHAR_H 0)
    set(ICONV_CONST "")
    set(EILSEQ 0)
    if(USE_MBSTATE_T)
        set(USE_MBSTATE_T 1)
    else()
        set(USE_MBSTATE_T 0)
    endif()
    set(iconv_generated "${qcs_libiconv_BINARY_DIR}/generated")
    file(MAKE_DIRECTORY "${iconv_generated}/build" "${iconv_generated}/public")
    set(DLL_VARIABLE "")
    configure_file("${qcs_libiconv_SOURCE_DIR}/include/iconv.h.build.in"
        "${iconv_generated}/build/iconv.h" @ONLY)
    configure_file("${qcs_libiconv_SOURCE_DIR}/libcharset/include/localcharset.h.build.in"
        "${iconv_generated}/build/localcharset.h" @ONLY)
    if(WIN32)
        set(DLL_VARIABLE "__declspec(dllimport)")
    endif()
    configure_file("${qcs_libiconv_SOURCE_DIR}/include/iconv.h.in"
        "${iconv_generated}/public/iconv.h" @ONLY)
    configure_file("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/qcs-native-iconv-config.h.in"
        "${iconv_generated}/build/config.h")
    add_library(qcs-native-iconv SHARED
        "${qcs_libiconv_SOURCE_DIR}/lib/iconv.c"
        "${qcs_libiconv_SOURCE_DIR}/libcharset/lib/localcharset.c"
        "${qcs_libiconv_SOURCE_DIR}/lib/compat.c")
    target_compile_features(qcs-native-iconv PRIVATE c_std_11)
    target_include_directories(qcs-native-iconv PRIVATE
        "${iconv_generated}/build" "${qcs_libiconv_SOURCE_DIR}/lib")
    target_include_directories(qcs-native-iconv SYSTEM INTERFACE "${iconv_generated}/public")
    target_compile_definitions(qcs-native-iconv PRIVATE BUILDING_LIBICONV=1 BUILDING_LIBCHARSET=1)
    if(WIN32)
        target_compile_definitions(qcs-native-iconv PRIVATE DLL_EXPORT=1 _CRT_SECURE_NO_WARNINGS)
    endif()
    set_target_properties(qcs-native-iconv PROPERTIES
        AUTOMOC OFF AUTOUIC OFF AUTORCC OFF
        POSITION_INDEPENDENT_CODE ON VERSION 2.7.1 SOVERSION 2)
    add_library(qcs::iconv ALIAS qcs-native-iconv)
    install(FILES "${qcs_libiconv_SOURCE_DIR}/COPYING.LIB"
        "${qcs_libiconv_SOURCE_DIR}/COPYING"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/libiconv" COMPONENT qcs-runtime)
    install(FILES "${qcs_libiconv_SOURCE_DIR}/libcharset/COPYING.LIB"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/libcharset" COMPONENT qcs-runtime)
endfunction()
