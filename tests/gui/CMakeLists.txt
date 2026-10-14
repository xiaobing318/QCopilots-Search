add_executable(qcs-tests-gui gui_tests.cpp)
target_link_libraries(qcs-tests-gui PRIVATE qcs-app-components qcs-test-support Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-gui COMMAND $<TARGET_FILE:qcs-tests-gui> -o "-,txt")
set_tests_properties(qcs-tests-gui PROPERTIES TIMEOUT 120 LABELS "gui;unit;portable")

add_executable(qcs-tests-gui-service service_gui_tests.cpp)
target_link_libraries(qcs-tests-gui-service PRIVATE qcs-app-components qcs-test-support Qt6::Test)
target_compile_definitions(qcs-tests-gui-service PRIVATE QCOPILOTS_SEARCH_SERVICE_EXECUTABLE="$<TARGET_FILE:qcs-service>")
add_dependencies(qcs-tests-gui-service qcs-service qcs-worker)
qcopilots_search_add_test(NAME qcs-tests-gui-service COMMAND $<TARGET_FILE:qcs-tests-gui-service> -o "-,txt")
set_tests_properties(qcs-tests-gui-service PROPERTIES TIMEOUT 300 LABELS "gui;integration;portable")

# Both suites show native windows. Keep their shared display exclusive so a
# second test process cannot steal focus during keyboard/selection assertions.
set_tests_properties(qcs-tests-gui qcs-tests-gui-service PROPERTIES
    RESOURCE_LOCK qcs-native-display)

add_executable(qcs-tests-login-startup login_startup_tests.cpp)
target_link_libraries(qcs-tests-login-startup PRIVATE qcs-app-components Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-login-startup COMMAND $<TARGET_FILE:qcs-tests-login-startup> -o "-,txt")
set_tests_properties(qcs-tests-login-startup PROPERTIES TIMEOUT 120 LABELS "gui;unit;portable")

add_executable(qcs-tests-gui-instance gui_instance_tests.cpp)
target_link_libraries(qcs-tests-gui-instance PRIVATE qcs-app-components qcs-test-support Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-gui-instance COMMAND $<TARGET_FILE:qcs-tests-gui-instance> -o "-,txt")
set_tests_properties(qcs-tests-gui-instance PROPERTIES TIMEOUT 120 LABELS "gui;integration;portable")

add_executable(qcs-tests-platform platform_integration_tests.cpp)
target_link_libraries(qcs-tests-platform PRIVATE qcs-app-components qcs-test-support Qt6::Test)
qcopilots_search_add_test(NAME qcs-tests-platform COMMAND $<TARGET_FILE:qcs-tests-platform> -o "-,txt")
set_tests_properties(qcs-tests-platform PROPERTIES TIMEOUT 120 LABELS "gui;unit;portable")

if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    find_program(QCOPILOTS_SEARCH_DBUS_RUN_SESSION dbus-run-session REQUIRED)
    add_executable(qcs-tests-linux-shortcut "${PROJECT_SOURCE_DIR}/tests/test_linux_global_shortcut.cpp")
    target_link_libraries(qcs-tests-linux-shortcut PRIVATE qcs-app-components Qt6::Gui Qt6::DBus Qt6::Test)
    qcopilots_search_add_test(NAME qcs-tests-linux-shortcut EVIDENCE_KIND qttest
        COMMAND "${QCOPILOTS_SEARCH_DBUS_RUN_SESSION}" -- "${CMAKE_COMMAND}" -E env
            QCOPILOTS_SEARCH_ISOLATED_DBUS=1 QT_QPA_PLATFORM=offscreen
            $<TARGET_FILE:qcs-tests-linux-shortcut> -o "-,txt")
    set_tests_properties(qcs-tests-linux-shortcut PROPERTIES TIMEOUT 120 LABELS "gui;unit;linux;portal")
endif()
