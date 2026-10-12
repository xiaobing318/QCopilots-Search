add_library(qcopilots-search-extraction STATIC
    extractor.cpp extractor.h
    document_extractor.cpp document_extractor.h
    legacy_doc_extractor.cpp legacy_doc_extractor.h
    image_extractor.cpp image_extractor.h
    media_extractor.cpp media_extractor.h)
target_compile_features(qcopilots-search-extraction PUBLIC cxx_std_20)
target_include_directories(qcopilots-search-extraction PUBLIC "${PROJECT_SOURCE_DIR}/src")
target_link_libraries(qcopilots-search-extraction PUBLIC qcopilots-search-core Qt6::Core
    PRIVATE Qt6::Gui Qt6::Pdf Qt6::Core5Compat qcopilots-search::tesseract qcopilots-search::heif
    qcopilots-search::raw qcopilots-search::miniz qcopilots-search::ffmpeg-headers qcopilots-search::wv2)
if(MSVC)
    target_compile_options(qcopilots-search-extraction PRIVATE /utf-8 /W4)
else()
    target_compile_options(qcopilots-search-extraction PRIVATE -Wall -Wextra -Wpedantic)
endif()
