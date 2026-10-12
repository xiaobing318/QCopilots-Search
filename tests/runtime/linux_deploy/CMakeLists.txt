cmake_minimum_required(VERSION 3.28)
project(qcopilots-search-linux-deploy-fixture LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
include(GNUInstallDirs)
find_package(Qt6 6.12 REQUIRED COMPONENTS Core)
set(CMAKE_BUILD_TYPE Release)
set(CMAKE_INSTALL_RPATH "$ORIGIN")
set(CMAKE_BUILD_RPATH_USE_ORIGIN OFF)

# The indirect dependency intentionally has no install rule. Qt must discover
# and deploy it through the explicitly scanned owned library.
add_library(qcopilots-search-transitive-fixture SHARED transitive.c)
set_target_properties(qcopilots-search-transitive-fixture PROPERTIES
  VERSION 1.2 SOVERSION 1)
add_library(qcopilots-search-owned-fixture SHARED owned.c)
set_target_properties(qcopilots-search-owned-fixture PROPERTIES VERSION 2.3 SOVERSION 2)
target_link_libraries(qcopilots-search-owned-fixture PRIVATE qcopilots-search-transitive-fixture)
add_library(qcopilots-search-module-fixture MODULE module.c)
target_link_libraries(qcopilots-search-module-fixture PRIVATE qcopilots-search-owned-fixture)
add_executable(qcopilots-search-deploy-fixture main.cpp)
set_target_properties(qcopilots-search-deploy-fixture PROPERTIES INSTALL_RPATH "$ORIGIN/../lib")
target_link_libraries(qcopilots-search-deploy-fixture PRIVATE
  Qt6::Core qcopilots-search-owned-fixture ${CMAKE_DL_LIBS})
install(TARGETS qcopilots-search-owned-fixture qcopilots-search-module-fixture
  LIBRARY DESTINATION lib)
install(TARGETS qcopilots-search-deploy-fixture RUNTIME DESTINATION bin)

include("${QCOPILOTS_SEARCH_LINUX_DEPLOY_CMAKE}")
qcopilots_search_linux_deploy_arguments(_arguments
  qcopilots-search-owned-fixture qcopilots-search-module-fixture)
qt_generate_deploy_script(TARGET qcopilots-search-deploy-fixture OUTPUT_SCRIPT _deploy
  CONTENT "qt_deploy_runtime_dependencies(\n EXECUTABLE \"$<TARGET_FILE:qcopilots-search-deploy-fixture>\"\n${_arguments} NO_PLUGINS\n NO_TRANSLATIONS\n GENERATE_QT_CONF\n)")
install(SCRIPT "${_deploy}")
file(GENERATE OUTPUT "files-$<CONFIG>.txt" CONTENT
  "$<TARGET_FILE:qcopilots-search-owned-fixture>\n$<TARGET_FILE:qcopilots-search-module-fixture>\n$<TARGET_FILE:qcopilots-search-transitive-fixture>\n")
set(CPACK_PACKAGE_NAME qcopilots-search-linux-deploy-fixture)
set(CPACK_PACKAGE_VERSION 1.0.0)
set(CPACK_PACKAGE_FILE_NAME qcopilots-search-linux-deploy-fixture)
set(CPACK_STRIP_FILES TRUE)
set(CPACK_GENERATOR TGZ)
include(CPack)
