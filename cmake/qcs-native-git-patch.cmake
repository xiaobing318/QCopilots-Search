include_guard(GLOBAL)
include("${CMAKE_CURRENT_LIST_DIR}/qcs-write-if-different.cmake")

# Keep the shared, archive-verified cache immutable. Each build configuration
# receives a content-addressed source copy with only these pinned adaptations.
function(qcopilots_search_prepare_git_source source binary output)
    file(SHA256 "${CMAKE_CURRENT_FUNCTION_LIST_FILE}" patch_hash)
    string(SUBSTRING "${patch_hash}" 0 16 patch_id)
    set(patched "${binary}/src-${patch_id}")
    file(MAKE_DIRECTORY "${binary}")
    file(LOCK "${binary}/qcs-patch.lock" GUARD FUNCTION TIMEOUT 300)
    set(paths src/util/fs_path.h src/util/fs_path.c
        src/util/win32/posix_w32.c include/git2/sys/config.h src/libgit2/CMakeLists.txt)
    set(hashes
        0a2fb2eff8adacc9985b1474592dfa50410b045d4ae81815ff43086a82f7cc35
        f00858c426c4d680a6868af30386a1df99c27d45db5ddb975c1f11e79d50a547
        4cb0cf6500dfb19ed11feb47e013e2ae06b0a04464a8e8ff27e3282e5a7b3fbd
        cc8010d313368f7a5c599b6dea8ab90c8a35e54fa19d8e19d208e8e509922199
        e29c23d10ffb21a723f0e7493675f14c7e04a6e27cb452a32c811f51427a6556)
    foreach(index RANGE 0 4)
        list(GET paths ${index} path)
        list(GET hashes ${index} expected)
        file(SHA256 "${source}/${path}" actual)
        if(NOT actual STREQUAL expected)
            message(FATAL_ERROR "Pinned libgit2 1.9.1 source changed: ${path}")
        endif()
    endforeach()
    if(NOT EXISTS "${patched}/.qcs-patch-complete")
        file(COPY "${source}/" DESTINATION "${patched}"
            FILE_PERMISSIONS OWNER_READ OWNER_WRITE GROUP_READ WORLD_READ
            DIRECTORY_PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE)
    endif()

    foreach(path IN LISTS paths)
        file(READ "${source}/${path}" content)
        if(path STREQUAL "src/util/fs_path.h")
            # p_realpath callers supply GIT_PATH_MAX bytes, including NUL.
            # UTF-8 bytes also bound the number of UTF-16 code units, so this
            # remains within the native wide conversion buffers for Unicode.
            string(REPLACE "path_chars = git_utf8_char_length(path, path_len);"
                "path_chars = path_len;" content "${content}")
            string(REPLACE "total_chars > MAX_PATH" "total_chars >= GIT_PATH_MAX" content "${content}")
        elseif(path STREQUAL "src/util/fs_path.c")
            string(REPLACE "size_t utf8_len = git_utf8_char_length(path->ptr, path->size);"
                "size_t utf8_len = path->size;" content "${content}")
            string(REPLACE "total_len > MAX_PATH" "total_len >= GIT_PATH_MAX" content "${content}")
        elseif(path STREQUAL "include/git2/sys/config.h")
            # This documented factory is missing its export annotation upstream.
            string(REPLACE "extern int git_config_backend_from_string("
                "GIT_EXTERN(int) git_config_backend_from_string(" content "${content}")
        elseif(path STREQUAL "src/libgit2/CMakeLists.txt")
            # Upstream rewrites this public umbrella header on every configure,
            # which needlessly rebuilds all translation units that include it.
            set(old [=[FILE(WRITE "${PROJECT_BINARY_DIR}/include/${LIBGIT2_FILENAME}.h" ${LIBGIT2_INCLUDE})]=])
            set(new [=[qcopilots_search_write_if_different("${PROJECT_BINARY_DIR}/include/${LIBGIT2_FILENAME}.h" "${LIBGIT2_INCLUDE}")]=])
            string(FIND "${content}" "${old}" anchor)
            if(anchor EQUAL -1)
                message(FATAL_ERROR "Pinned libgit2 umbrella header patch anchor changed")
            endif()
            string(REPLACE "${old}" "${new}" content "${content}")
        else()
            string(REPLACE "git_win32_path orig_path_w, buffer_w;\n\tDWORD long_len;"
                "git_win32_path orig_path_w, buffer_w;\n\tgit_win32_utf8_path canonical;\n\tDWORD long_len, full_len;\n\tsize_t utf8_len;" content "${content}")
            string(REPLACE "if (!GetFullPathNameW(orig_path_w, GIT_WIN_PATH_UTF16, buffer_w, NULL)) {\n\t\tif (GetLastError() == ERROR_INSUFFICIENT_BUFFER)"
                "if ((full_len = GetFullPathNameW(orig_path_w, GIT_WIN_PATH_UTF16, buffer_w, NULL)) == 0 ||\n\t    full_len >= GIT_WIN_PATH_UTF16) {\n\t\tif (full_len >= GIT_WIN_PATH_UTF16 || GetLastError() == ERROR_INSUFFICIENT_BUFFER)" content "${content}")
            string(REPLACE "if (long_len > GIT_WIN_PATH_UTF16)" "if (long_len >= GIT_WIN_PATH_UTF16)" content "${content}")
            set(old [=[	if (!buffer && !(buffer = git__malloc(GIT_WIN_PATH_UTF8))) {
		errno = ENOMEM;
		return NULL;
	}

	/* Convert the path to UTF-8. If the caller provided a buffer, then it
	 * is assumed to be GIT_WIN_PATH_UTF8 characters in size. If it isn't,
	 * then we may overflow. */
	if (git_win32_path_to_utf8(buffer, buffer_w) < 0)
		return NULL;

	git_fs_path_mkposix(buffer);]=])
            set(new [=[	/* The actual callers provide GIT_PATH_MAX bytes. Convert separately
	 * before checking that complete UTF-8 bytes, including UNC, fit. */
	if (git_win32_path_to_utf8(canonical, buffer_w) < 0)
		return NULL;
	utf8_len = strlen(canonical);
	if (buffer && utf8_len >= GIT_PATH_MAX) {
		errno = ENAMETOOLONG;
		return NULL;
	}
	if (!buffer && !(buffer = git__malloc(utf8_len + 1))) {
		errno = ENOMEM;
		return NULL;
	}
	memcpy(buffer, canonical, utf8_len + 1);
	git_fs_path_mkposix(buffer);]=])
            string(REPLACE "${old}" "${new}" content "${content}")
        endif()
        # Keep identical prepared files and their timestamps unchanged. Verify
        # cached adapted files rather than silently accepting mutations.
        if(EXISTS "${patched}/.qcs-patch-complete")
            file(READ "${patched}/${path}" existing)
            if(NOT existing STREQUAL content)
                message(FATAL_ERROR "Prepared libgit2 source changed: ${path}")
            endif()
        else()
            file(WRITE "${patched}/${path}" "${content}")
        endif()
    endforeach()
    qcopilots_search_write_if_different("${patched}/.qcs-patch-complete" "${patch_hash}\n")
    set(${output} "${patched}" PARENT_SCOPE)
endfunction()
