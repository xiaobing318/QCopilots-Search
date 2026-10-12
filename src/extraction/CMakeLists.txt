add_library(qcs-extraction STATIC
    extractor.cpp extractor.h
    document_extractor.cpp document_extractor.h
    legacy_doc_extractor.cpp legacy_doc_extractor.h
    image_extractor.cpp image_extractor.h
    media_extractor.cpp media_extractor.h)
target_compile_features(qcs-extraction PUBLIC cxx_std_20)
target_include_directories(qcs-extraction PUBLIC "${PROJECT_SOURCE_DIR}/src")
target_link_libraries(qcs-extraction PUBLIC qcs-core Qt6::Core
    PRIVATE Qt6::Gui Qt6::Pdf Qt6::Core5Compat qcs::tesseract qcs::heif
    qcs::raw qcs::miniz qcs::ffmpeg-headers qcs::wv2)
if(MSVC)
    target_compile_options(qcs-extraction PRIVATE /utf-8 /W4)
else()
    target_compile_options(qcs-extraction PRIVATE -Wall -Wextra -Wpedantic)
endif()
