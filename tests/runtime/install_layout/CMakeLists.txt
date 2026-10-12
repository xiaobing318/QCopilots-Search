cmake_minimum_required(VERSION 3.28)
project(qcopilots-search-install-layout-fixture LANGUAGES C)

# Exercise the real install rules with tiny host-native libraries. On Windows a
# MODULE models the LIBRARY artifact category used by macOS shared libraries.
if(QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT AND WIN32)
  add_library(qcopilots-search-base-fixture MODULE fixture.c)
else()
  add_library(qcopilots-search-base-fixture SHARED fixture.c)
endif()
add_library(qcopilots-search-cpu-fixture MODULE fixture.c)
if(QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT)
  set(CMAKE_INSTALL_BINDIR "qcopilots-search.app/Contents/MacOS")
  set(CMAKE_INSTALL_LIBDIR "qcopilots-search.app/Contents/Frameworks")
  set_target_properties(qcopilots-search-base-fixture PROPERTIES PREFIX "lib" SUFFIX ".dylib")
  set_target_properties(qcopilots-search-cpu-fixture PROPERTIES PREFIX "lib" SUFFIX ".so")
else()
  set(CMAKE_INSTALL_BINDIR "bin")
  set(CMAKE_INSTALL_LIBDIR "lib")
endif()
set(APPLE "${QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT}")
include("${QCOPILOTS_SEARCH_RUNTIME_CMAKE}")
qcopilots_search_install_inference_runtime(
  LIBRARIES qcopilots-search-base-fixture CPU_BACKENDS qcopilots-search-cpu-fixture)
if(QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT)
  cmake_path(GET QCOPILOTS_SEARCH_RUNTIME_CMAKE PARENT_PATH runtime_cmake_directory)
  include("${runtime_cmake_directory}/../../cmake/QCopilotsSearchMacOSDeploy.cmake")
  # Exercise final target selection and CPU placement on every host. Actual
  # Mach-O relocation and macdeployqt are covered by the native macOS fixture.
  qcopilots_search_install_macos_runtime_after_bundle(
    NATIVE_TARGETS qcopilots-search-base-fixture
    INFERENCE_TARGETS qcopilots-search-base-fixture qcopilots-search-cpu-fixture
    CPU_TARGETS qcopilots-search-cpu-fixture)
endif()
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/files.txt" CONTENT
  "$<TARGET_FILE_NAME:qcopilots-search-base-fixture>\n$<TARGET_FILE_NAME:qcopilots-search-cpu-fixture>\n")
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/cpu-rpath.txt" CONTENT
  "$<TARGET_PROPERTY:qcopilots-search-cpu-fixture,INSTALL_RPATH>")
