if(WIN32)
    set(_qcopilots_extraction_platform windows)
elseif(APPLE)
    set(_qcopilots_extraction_platform cocoa)
else()
    set(_qcopilots_extraction_platform offscreen)
endif()
foreach(suite IN ITEMS document image media legacy_doc)
    add_executable(qcs-tests-extraction-${suite} "${suite}_tests.cpp")
    target_link_libraries(qcs-tests-extraction-${suite}
        PRIVATE qcs-extraction qcs-test-support Qt6::Test Qt6::Gui Qt6::Pdf qcs::miniz)
    if(MSVC)
        target_compile_options(qcs-tests-extraction-${suite} PRIVATE /utf-8)
    endif()
    qcopilots_search_add_test(NAME qcs-tests-extraction-${suite}
        COMMAND $<TARGET_FILE:qcs-tests-extraction-${suite}> -o "-,txt")
    set_tests_properties(qcs-tests-extraction-${suite} PROPERTIES
        TIMEOUT 180 LABELS "unit;extraction;portable"
        ENVIRONMENT "QT_QPA_PLATFORM=${_qcopilots_extraction_platform};QCOPILOTS_SEARCH_TESSDATA_DIR=${QCOPILOTS_SEARCH_TESSDATA_DIR}")
endforeach()
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_libraries(qcs-tests-extraction-media PRIVATE ${CMAKE_DL_LIBS})
endif()
target_sources(qcs-tests-extraction-legacy_doc PRIVATE qcs-doc.qrc)
set_target_properties(qcs-tests-extraction-legacy_doc PROPERTIES AUTORCC ON)
target_link_libraries(qcs-tests-extraction-legacy_doc PRIVATE qcs::iconv)

target_sources(qcs-tests-extraction-image PRIVATE image_fixtures.qrc)
set_target_properties(qcs-tests-extraction-image PROPERTIES AUTORCC ON)
add_executable(qcs-tests-jpeg-probe jpeg_probe.cpp)
target_link_libraries(qcs-tests-jpeg-probe PRIVATE
    Qt6::Core qcs::raw JPEG::JPEG ${CMAKE_DL_LIBS})
qcopilots_search_add_jpeg_contract(qcs-tests-jpeg-probe)
target_compile_features(qcs-tests-jpeg-probe PRIVATE cxx_std_20)
target_compile_definitions(qcs-tests-extraction-image PRIVATE
    QCOPILOTS_SEARCH_TEST_JPEG_PROBE_PATH="$<TARGET_FILE:qcs-tests-jpeg-probe>"
    QCOPILOTS_SEARCH_TEST_IMAGE_WORKER_PATH="$<TARGET_FILE:qcs-worker>")
add_dependencies(qcs-tests-extraction-image
    qcs-tests-jpeg-probe qcs-worker)
