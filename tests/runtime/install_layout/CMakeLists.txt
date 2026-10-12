cmake_minimum_required(VERSION 3.28)
project(qcs-install-layout-fixture LANGUAGES C)

# Exercise the real install rules with tiny host-native libraries. On Windows a
# MODULE models the LIBRARY artifact category used by macOS shared libraries.
if(QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT AND WIN32)
  add_library(qcs-base-fixture MODULE fixture.c)
else()
  add_library(qcs-base-fixture SHARED fixture.c)
endif()
add_library(qcs-cpu-fixture MODULE fixture.c)
if(QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT)
  set(CMAKE_INSTALL_BINDIR "qcs.app/Contents/MacOS")
  set(CMAKE_INSTALL_LIBDIR "qcs.app/Contents/Frameworks")
  set_target_properties(qcs-base-fixture PROPERTIES PREFIX "lib" SUFFIX ".dylib")
  set_target_properties(qcs-cpu-fixture PROPERTIES PREFIX "lib" SUFFIX ".so")
else()
  set(CMAKE_INSTALL_BINDIR "bin")
  set(CMAKE_INSTALL_LIBDIR "lib")
endif()
set(APPLE "${QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT}")
include("${QCOPILOTS_SEARCH_RUNTIME_CMAKE}")
qcopilots_search_install_inference_runtime(
  LIBRARIES qcs-base-fixture CPU_BACKENDS qcs-cpu-fixture)
if(QCOPILOTS_SEARCH_TEST_APPLE_LAYOUT)
  cmake_path(GET QCOPILOTS_SEARCH_RUNTIME_CMAKE PARENT_PATH runtime_cmake_directory)
  include("${runtime_cmake_directory}/../../cmake/qcs-macos-deploy.cmake")
  # Exercise final target selection and CPU placement on every host. Actual
  # Mach-O relocation and macdeployqt are covered by the native macOS fixture.
  qcopilots_search_install_macos_runtime_after_bundle(
    NATIVE_TARGETS qcs-base-fixture
    INFERENCE_TARGETS qcs-base-fixture qcs-cpu-fixture
    CPU_TARGETS qcs-cpu-fixture)
endif()
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/files.txt" CONTENT
  "$<TARGET_FILE_NAME:qcs-base-fixture>\n$<TARGET_FILE_NAME:qcs-cpu-fixture>\n")
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/cpu-rpath.txt" CONTENT
  "$<TARGET_PROPERTY:qcs-cpu-fixture,INSTALL_RPATH>")
