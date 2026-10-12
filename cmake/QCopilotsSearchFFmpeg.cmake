include_guard(GLOBAL)
include(FetchContent)
# Qt 6.12.0 deploys FFmpeg 9.0.1. Only the official public headers are used
# here, no FFmpeg source is compiled and the runtime is loaded via QLibrary.
qcopilots_search_fetch(qcopilots_search_ffmpeg
  "https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz"
  "cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635")
if(NOT CMAKE_SYSTEM_PROCESSOR MATCHES "^(AMD64|amd64|x86_64|aarch64|arm64|ARM64)$")
  message(FATAL_ERROR "FFmpeg header configuration requires a validated x64 or arm64 target")
endif()
include(TestBigEndian)
test_big_endian(_qcopilots_big_endian)
if(_qcopilots_big_endian)
  message(FATAL_ERROR "This FFmpeg SDK configuration supports little-endian targets only")
endif()
set(_qcopilots_ffmpeg_generated "${CMAKE_BINARY_DIR}/generated/ffmpeg")
set(_qcopilots_ffmpeg_headers "${CMAKE_BINARY_DIR}/generated/ffmpeg-headers")
# The upstream repository root contains a plain file named "version". Adding
# it to the C++ include path shadows the C++20 <version> header on MSVC. Stage
# only header files below their original libav*/libsw* namespaces.
foreach(_module libavcodec libavformat libavutil libswresample libswscale)
  file(GLOB_RECURSE _headers CONFIGURE_DEPENDS "${qcopilots_search_ffmpeg_SOURCE_DIR}/${_module}/*.h")
  foreach(_header IN LISTS _headers)
    file(RELATIVE_PATH _relative "${qcopilots_search_ffmpeg_SOURCE_DIR}" "${_header}")
    configure_file("${_header}" "${_qcopilots_ffmpeg_headers}/${_relative}" COPYONLY)
  endforeach()
endforeach()
file(MAKE_DIRECTORY "${_qcopilots_ffmpeg_generated}/libavutil")
file(WRITE "${_qcopilots_ffmpeg_generated}/libavutil/avconfig.h"
  "#ifndef AVUTIL_AVCONFIG_H\n#define AVUTIL_AVCONFIG_H\n#define AV_HAVE_BIGENDIAN 0\n#define AV_HAVE_FAST_UNALIGNED 1\n#endif\n")
add_library(qcopilots-search-ffmpeg-headers INTERFACE)
add_library(qcopilots-search::ffmpeg-headers ALIAS qcopilots-search-ffmpeg-headers)
target_include_directories(qcopilots-search-ffmpeg-headers SYSTEM INTERFACE
  "${_qcopilots_ffmpeg_generated}" "${_qcopilots_ffmpeg_headers}")
get_target_property(_qcopilots_qmake Qt6::qmake IMPORTED_LOCATION)
execute_process(COMMAND "${_qcopilots_qmake}" -query QT_INSTALL_PREFIX
  OUTPUT_VARIABLE _qcopilots_qt_prefix OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
if(WIN32)
  set(_qcopilots_ffmpeg_runtime "${_qcopilots_qt_prefix}/bin")
else()
  set(_qcopilots_ffmpeg_runtime "${_qcopilots_qt_prefix}/lib")
endif()
target_compile_definitions(qcopilots-search-ffmpeg-headers INTERFACE
  QCOPILOTS_SEARCH_FFMPEG_RUNTIME_DIR="${_qcopilots_ffmpeg_runtime}")
set(QCOPILOTS_SEARCH_FFMPEG_INCLUDE_DIR "${_qcopilots_ffmpeg_headers}" CACHE INTERNAL "FFmpeg official headers")
set(QCOPILOTS_SEARCH_FFMPEG_GENERATED_INCLUDE_DIR "${_qcopilots_ffmpeg_generated}" CACHE INTERNAL "FFmpeg configuration headers")
set(QCOPILOTS_SEARCH_FFMPEG_RUNTIME_DIR "${_qcopilots_ffmpeg_runtime}" CACHE INTERNAL "Matching Qt FFmpeg runtime")
if(UNIX AND NOT APPLE)
  file(GLOB _qcopilots_ffmpeg_libraries "${_qcopilots_ffmpeg_runtime}/libav*.so*" "${_qcopilots_ffmpeg_runtime}/libsw*.so*")
  install(FILES ${_qcopilots_ffmpeg_libraries} DESTINATION ${CMAKE_INSTALL_LIBDIR})
endif()
install(FILES "${qcopilots_search_ffmpeg_SOURCE_DIR}/COPYING.LGPLv2.1"
  "${qcopilots_search_ffmpeg_SOURCE_DIR}/COPYING.LGPLv3"
  DESTINATION "${CMAKE_INSTALL_DATADIR}/qcopilots-search/licenses/ffmpeg")
