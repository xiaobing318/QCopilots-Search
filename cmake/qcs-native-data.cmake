include_guard(GLOBAL)

# The inference runtime consumes these small, redistributable LSTM files.
# Fetching individual pinned files avoids downloading the full language corpus.
function(qcopilots_search_add_ocr_data)
    set(data_root "${FETCHCONTENT_BASE_DIR}/qcs-tessdata")
    file(MAKE_DIRECTORY "${data_root}")
    file(LOCK "${data_root}/.qcs-data.lock" GUARD FUNCTION TIMEOUT 300)
    set(revision "87416418657359cb625c412a48b6e1d6d41c29bd")
    set(data_files eng.traineddata chi_sim.traineddata chi_tra.traineddata LICENSE)
    set(data_hashes
        "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
        "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730"
        "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b"
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")
    foreach(name hash IN ZIP_LISTS data_files data_hashes)
        set(destination "${data_root}/${name}")
        set(actual_hash "")
        if(EXISTS "${destination}")
            file(SHA256 "${destination}" actual_hash)
        endif()
        if(NOT actual_hash STREQUAL hash)
            if(FETCHCONTENT_FULLY_DISCONNECTED)
                message(FATAL_ERROR "Missing or invalid offline OCR data: ${destination}")
            endif()
            message(STATUS "Fetching pinned OCR data ${name}")
            file(DOWNLOAD
                "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/${revision}/${name}"
                "${destination}.part" EXPECTED_HASH "SHA256=${hash}"
                TLS_VERIFY TRUE TIMEOUT 300 STATUS download_status)
            list(GET download_status 0 download_code)
            if(NOT download_code EQUAL 0)
                message(FATAL_ERROR "Cannot fetch OCR data ${name}: ${download_status}")
            endif()
            file(RENAME "${destination}.part" "${destination}")
        endif()
        if(NOT name STREQUAL "LICENSE")
            install(FILES "${destination}"
                DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/tessdata"
                COMPONENT qcs-runtime)
        endif()
    endforeach()
    install(FILES "${data_root}/LICENSE"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/qcs/licenses/tessdata-fast"
        COMPONENT qcs-runtime)
    set(QCOPILOTS_SEARCH_TESSDATA_DIR "${data_root}"
        CACHE INTERNAL "Pinned OCR language data directory" FORCE)
    set_property(GLOBAL PROPERTY QCOPILOTS_SEARCH_TESSDATA_REVISION "${revision}")
endfunction()
