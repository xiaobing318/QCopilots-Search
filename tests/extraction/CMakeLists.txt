if(WIN32)
    set(_qcopilots_extraction_platform windows)
elseif(APPLE)
    set(_qcopilots_extraction_platform cocoa)
else()
    set(_qcopilots_extraction_platform offscreen)
endif()
foreach(suite IN ITEMS document image media legacy_doc)
    add_executable(qcopilots-search-tests-extraction-${suite} "${suite}_tests.cpp")
    target_link_libraries(qcopilots-search-tests-extraction-${suite}
        PRIVATE qcopilots-search-extraction Qt6::Test Qt6::Gui Qt6::Pdf qcopilots-search::miniz)
    if(MSVC)
        target_compile_options(qcopilots-search-tests-extraction-${suite} PRIVATE /utf-8)
    endif()
    set(_workdir "${CMAKE_CURRENT_BINARY_DIR}/${suite}-work")
    file(MAKE_DIRECTORY "${_workdir}")
    add_test(NAME qcopilots-search-tests-extraction-${suite}
        COMMAND $<TARGET_FILE:qcopilots-search-tests-extraction-${suite}> -o "-,txt")
    set_tests_properties(qcopilots-search-tests-extraction-${suite} PROPERTIES
        WORKING_DIRECTORY "${_workdir}" TIMEOUT 180 LABELS "unit;extraction;portable"
        ENVIRONMENT "QT_QPA_PLATFORM=${_qcopilots_extraction_platform};QCOPILOTS_SEARCH_TESSDATA_DIR=${QCOPILOTS_SEARCH_TESSDATA_DIR}")
endforeach()
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_libraries(qcopilots-search-tests-extraction-media PRIVATE ${CMAKE_DL_LIBS})
endif()
target_sources(qcopilots-search-tests-extraction-legacy_doc PRIVATE legacy_doc_fixtures.qrc)
set_target_properties(qcopilots-search-tests-extraction-legacy_doc PROPERTIES AUTORCC ON)
target_link_libraries(qcopilots-search-tests-extraction-legacy_doc PRIVATE Qt6::Core5Compat)
if(APPLE)
    # This test exercises the private inline adapter as well as the wv2 DLL.
    target_link_libraries(qcopilots-search-tests-extraction-legacy_doc PRIVATE "${QCOPILOTS_SEARCH_COREFOUNDATION_LIBRARY}")
endif()
target_sources(qcopilots-search-tests-extraction-image PRIVATE image_fixtures.qrc)
set_target_properties(qcopilots-search-tests-extraction-image PROPERTIES AUTORCC ON)
add_executable(qcopilots-search-tests-jpeg-probe jpeg_probe.cpp)
target_link_libraries(qcopilots-search-tests-jpeg-probe PRIVATE
    Qt6::Core qcopilots-search::raw JPEG::JPEG ${CMAKE_DL_LIBS})
target_compile_features(qcopilots-search-tests-jpeg-probe PRIVATE cxx_std_20)
target_compile_definitions(qcopilots-search-tests-extraction-image PRIVATE
    QCOPILOTS_SEARCH_TEST_JPEG_PROBE_PATH="$<TARGET_FILE:qcopilots-search-tests-jpeg-probe>"
    QCOPILOTS_SEARCH_TEST_IMAGE_WORKER_PATH="$<TARGET_FILE:qcopilots-search-worker>")
add_dependencies(qcopilots-search-tests-extraction-image
    qcopilots-search-tests-jpeg-probe qcopilots-search-worker)
