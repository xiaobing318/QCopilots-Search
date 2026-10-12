add_library(qcs-core STATIC
    app_settings.cpp app_settings.h
    file_catalog.cpp file_catalog.h
    model_files.cpp model_files.h
    runtime_paths.cpp runtime_paths.h
    pipeline_manifest.h
    types.cpp types.h
)
target_compile_features(qcs-core PUBLIC cxx_std_20)
target_include_directories(qcs-core PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/..")
target_link_libraries(qcs-core PUBLIC Qt6::Core Qt6::Network
    PRIVATE qcs::git2)
if(WIN32)
    target_link_libraries(qcs-core PRIVATE advapi32)
endif()
