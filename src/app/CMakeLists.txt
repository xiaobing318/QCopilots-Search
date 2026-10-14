add_library(qcs-app-components STATIC
    app_backend.cpp app_backend.h
    gui_instance.cpp gui_instance.h
    main_window.cpp main_window.h
    model_dialog.cpp model_dialog.h
    platform_integration.cpp platform_integration.h
    login_startup.cpp login_startup.h
    platform_external_open.cpp platform_external_open.h
    preview_panel.cpp preview_panel.h
    settings_dialog.cpp settings_dialog.h
)
target_link_libraries(qcs-app-components PUBLIC
    qcs-client qcs-extraction Qt6::Widgets Qt6::Network Qt6::Concurrent
    Qt6::Pdf Qt6::PdfWidgets Qt6::Multimedia Qt6::MultimediaWidgets
)
target_compile_features(qcs-app-components PUBLIC cxx_std_20)
target_include_directories(qcs-app-components PUBLIC "${PROJECT_SOURCE_DIR}/src")
if(WIN32)
    target_link_libraries(qcs-app-components PRIVATE user32)
elseif(APPLE)
    target_sources(qcs-app-components PRIVATE platform_login_mac.mm)
    set_target_properties(qcs-app-components PROPERTIES OBJCXX_STANDARD 20 OBJCXX_STANDARD_REQUIRED ON)
    find_library(QCOPILOTS_SEARCH_CARBON_FRAMEWORK Carbon REQUIRED)
    find_library(QCOPILOTS_SEARCH_APPKIT_FRAMEWORK AppKit REQUIRED)
    find_library(QCOPILOTS_SEARCH_CORESERVICES_FRAMEWORK CoreServices REQUIRED)
    find_library(QCOPILOTS_SEARCH_SERVICEMANAGEMENT_FRAMEWORK ServiceManagement REQUIRED)
    target_link_libraries(qcs-app-components PRIVATE "${QCOPILOTS_SEARCH_CARBON_FRAMEWORK}"
        "${QCOPILOTS_SEARCH_APPKIT_FRAMEWORK}" "${QCOPILOTS_SEARCH_CORESERVICES_FRAMEWORK}"
        "${QCOPILOTS_SEARCH_SERVICEMANAGEMENT_FRAMEWORK}")
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    find_package(Qt6 6.12 REQUIRED COMPONENTS DBus)
    target_sources(qcs-app-components PRIVATE linux_global_shortcut.cpp linux_global_shortcut.h)
    target_link_libraries(qcs-app-components PRIVATE Qt6::DBus)
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(XCB QUIET IMPORTED_TARGET xcb)
        if(TARGET PkgConfig::XCB)
            target_link_libraries(qcs-app-components PRIVATE PkgConfig::XCB)
            target_compile_definitions(qcs-app-components PRIVATE QCOPILOTS_SEARCH_HAS_XCB=1)
        endif()
    endif()
endif()

qt_add_executable(qcs-gui WIN32 MACOSX_BUNDLE main.cpp)
target_compile_definitions(qcs-gui PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
target_link_libraries(qcs-gui PRIVATE qcs-app-components)
set_target_properties(qcs-gui PROPERTIES
    MACOSX_BUNDLE_GUI_IDENTIFIER "com.qcopilots.qcopilots-search"
    MACOSX_BUNDLE_BUNDLE_NAME "QCopilots Search"
    MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
    MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}"
)
