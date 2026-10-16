cmake_minimum_required(VERSION 3.28)
project(qcs-macos-deploy-fixture LANGUAGES C CXX)
if(NOT APPLE)
  message(FATAL_ERROR "The macOS deployment fixture requires native Mach-O tools")
endif()
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Qt6 6.12 REQUIRED COMPONENTS Core)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/libraries")
set(CMAKE_INSTALL_NAME_DIR "@rpath")
set(CMAKE_BUILD_RPATH "${CMAKE_LIBRARY_OUTPUT_DIRECTORY}")
set(CMAKE_INSTALL_RPATH "@loader_path")
set(CMAKE_INSTALL_BINDIR "qcs-deploy-fixture.app/Contents/MacOS")
set(CMAKE_INSTALL_LIBDIR "qcs-deploy-fixture.app/Contents/Frameworks")

add_library(qcs-transitive-fixture SHARED transitive.c)
set_target_properties(qcs-transitive-fixture PROPERTIES VERSION 1.2 SOVERSION 1)
add_library(qcs-owned-fixture SHARED owned.c)
set_target_properties(qcs-owned-fixture PROPERTIES VERSION 2.3 SOVERSION 2)
target_link_libraries(qcs-owned-fixture PRIVATE qcs-transitive-fixture)
add_executable(qcs-deploy-fixture MACOSX_BUNDLE main.cpp)
set_target_properties(qcs-deploy-fixture PROPERTIES
  MACOSX_BUNDLE_GUI_IDENTIFIER "org.qcopilots-search.deploy-fixture"
  INSTALL_RPATH "@executable_path/../Frameworks")
target_link_libraries(qcs-deploy-fixture PRIVATE
  Qt6::Core qcs-owned-fixture ${CMAKE_DL_LIBS})

# Match production ordering: target installation first, followed by the whole
# development bundle containing copies of the original build libraries.
install(TARGETS qcs-owned-fixture qcs-transitive-fixture
  LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}")
add_custom_target(qcs-deploy-development-fixture ALL
  COMMAND "${CMAKE_COMMAND}" -E make_directory
    "$<TARGET_BUNDLE_DIR:qcs-deploy-fixture>/Contents/Frameworks"
  COMMAND "${CMAKE_COMMAND}" -E copy_if_different
    "$<TARGET_FILE:qcs-owned-fixture>"
    "$<TARGET_BUNDLE_DIR:qcs-deploy-fixture>/Contents/Frameworks/$<TARGET_FILE_NAME:qcs-owned-fixture>"
  COMMAND "${CMAKE_COMMAND}" -E copy_if_different
    "$<TARGET_FILE:qcs-transitive-fixture>"
    "$<TARGET_BUNDLE_DIR:qcs-deploy-fixture>/Contents/Frameworks/$<TARGET_FILE_NAME:qcs-transitive-fixture>"
  DEPENDS qcs-deploy-fixture qcs-owned-fixture
    qcs-transitive-fixture VERBATIM)
install(TARGETS qcs-deploy-fixture BUNDLE DESTINATION .)

# Prove that the development copy really recreated the regression before the
# repair. Without this observation, a fixture that never overwrites an installed
# library could pass while exercising the wrong ordering.
set(_observe [=[
set(_bundle "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/qcs-deploy-fixture.app")
execute_process(COMMAND /usr/bin/otool -l
  "${_bundle}/Contents/Frameworks/$<TARGET_FILE_NAME:qcs-owned-fixture>"
  RESULT_VARIABLE _result OUTPUT_VARIABLE _commands ERROR_VARIABLE _errors)
string(FIND "${_commands}" "@CMAKE_LIBRARY_OUTPUT_DIRECTORY@" _build_rpath)
if(NOT _result EQUAL 0 OR _build_rpath EQUAL -1)
  message(FATAL_ERROR "Fixture did not recreate the overwritten build RPATH: ${_errors}${_commands}")
endif()
file(TIMESTAMP "$<TARGET_FILE:qcs-owned-fixture>" _source_time "%s" UTC)
file(TIMESTAMP "${_bundle}/Contents/Frameworks/$<TARGET_FILE_NAME:qcs-owned-fixture>"
  _installed_time "%s" UTC)
if(NOT _source_time STREQUAL _installed_time)
  message(FATAL_ERROR "Fixture must exercise final target installation with matching source and destination timestamps")
endif()
file(MAKE_DIRECTORY "${_bundle}/Contents/Resources")
file(WRITE "${_bundle}/Contents/Resources/qcs-before-runtime-finalization.txt"
  "sourceEpoch=${_source_time}\ninstalledEpoch=${_installed_time}\n${_commands}")
]=])
string(CONFIGURE "${_observe}" _observe @ONLY)
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/observe-$<CONFIG>.cmake" CONTENT "${_observe}")
install(SCRIPT "${CMAKE_CURRENT_BINARY_DIR}/observe-$<CONFIG>.cmake")

include("${QCOPILOTS_SEARCH_MACOS_DEPLOY_CMAKE}")
qcopilots_search_install_macos_runtime_after_bundle(
  NATIVE_TARGETS qcs-owned-fixture qcs-transitive-fixture)
qt_generate_deploy_script(TARGET qcs-deploy-fixture OUTPUT_SCRIPT _deploy
  CONTENT "qt_deploy_runtime_dependencies(\n EXECUTABLE \"qcs-deploy-fixture.app\"\n NO_PLUGINS\n NO_TRANSLATIONS\n DEPLOY_TOOL_OPTIONS -always-overwrite -no-codesign\n GENERATE_QT_CONF\n)")
get_filename_component(_helper_directory "${QCOPILOTS_SEARCH_MACOS_DEPLOY_CMAKE}" DIRECTORY)
install(CODE "include(\"${_helper_directory}/qcs-checked-deploy.cmake\")\nqcopilots_search_checked_deploy(\"${_deploy}\")")
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/files-$<CONFIG>.txt" CONTENT
  "$<TARGET_FILE:qcs-owned-fixture>\n$<TARGET_FILE:qcs-transitive-fixture>\n$<TARGET_SONAME_FILE_NAME:qcs-owned-fixture>\n$<TARGET_SONAME_FILE_NAME:qcs-transitive-fixture>\n$<TARGET_BUNDLE_DIR:qcs-deploy-fixture>\n")
set(CPACK_PACKAGE_NAME qcs-macos-deploy-fixture)
set(CPACK_PACKAGE_VERSION 1.0.0)
set(CPACK_PACKAGE_FILE_NAME qcs-macos-deploy-fixture)
set(CPACK_STRIP_FILES TRUE)
set(CPACK_GENERATOR TGZ)
include(CPack)
