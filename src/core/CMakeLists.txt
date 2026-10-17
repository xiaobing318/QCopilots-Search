add_library(qcs-core STATIC
    app_settings.cpp app_settings.h
    general_file_rules.cpp general_file_rules.h
    file_catalog.cpp file_catalog.h
    file_formats.h
    model_files.cpp model_files.h
    model_catalog.cpp model_catalog.h
    runtime_paths.cpp runtime_paths.h
    pipeline_manifest.h
    utf8_locale.h
    types.cpp types.h
)

qt_add_resources(qcs-core qcs_models
    PREFIX "/qcs"
    BASE "${PROJECT_SOURCE_DIR}/config"
    FILES "${PROJECT_SOURCE_DIR}/config/qcs-models.lock.json")
target_compile_features(qcs-core PUBLIC cxx_std_20)
target_include_directories(qcs-core PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/..")
target_link_libraries(qcs-core PUBLIC Qt6::Core Qt6::Network
    PRIVATE qcs::git2)
if(WIN32)
    target_link_libraries(qcs-core PRIVATE advapi32)
endif()
