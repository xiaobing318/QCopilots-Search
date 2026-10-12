add_library(qcs-app-components STATIC
    app_backend.cpp app_backend.h
    main_window.cpp main_window.h
    model_dialog.cpp model_dialog.h
    platform_integration.cpp platform_integration.h
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
    find_library(QCOPILOTS_SEARCH_CARBON_FRAMEWORK Carbon REQUIRED)
    target_link_libraries(qcs-app-components PRIVATE "${QCOPILOTS_SEARCH_CARBON_FRAMEWORK}")
endif()

qt_add_executable(qcs WIN32 MACOSX_BUNDLE main.cpp)
target_compile_definitions(qcs PRIVATE QCOPILOTS_SEARCH_VERSION="${PROJECT_VERSION}")
target_link_libraries(qcs PRIVATE qcs-app-components)
set_target_properties(qcs PROPERTIES
    MACOSX_BUNDLE_GUI_IDENTIFIER "com.qcopilots.qcopilots-search"
    MACOSX_BUNDLE_BUNDLE_NAME "QCopilots Search"
    MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
    MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}"
)
