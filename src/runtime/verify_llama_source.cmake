# The manifest is generated from the unmodified b11461 codeload archive:
# https://codeload.github.com/ggml-org/llama.cpp/tar.gz/4d756bc72bf00a4aacf410ae15a2d315f3db400d
# Archive SHA256: 778ee590ec7447075e08f2daf6301311ae7f8d9bf810a0b849f26af3f6d10867
# It lists every regular file. The archive contains no symbolic or hard links.
include_guard(GLOBAL)

function(qcopilots_search_verify_llama_archive source_dir)
  if(IS_SYMLINK "${source_dir}" OR NOT IS_DIRECTORY "${source_dir}")
    message(FATAL_ERROR "The llama.cpp archive source must be an existing directory, not a symlink: ${source_dir}")
  endif()
  set(_manifest "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/llama-source-manifest.sha256")
  file(SHA256 "${_manifest}" _manifest_hash)
  if(NOT _manifest_hash STREQUAL "816fbcfa3ab769842c05d82913bd8d81c9391e6cac34520fd18505ac8a14638e")
    message(FATAL_ERROR "The llama.cpp source manifest differs from its reviewed hash")
  endif()
  file(STRINGS "${_manifest}" _entries ENCODING UTF-8)
  set(_expected_files)
  foreach(_entry IN LISTS _entries)
    if(NOT _entry MATCHES "^([0-9a-f]+)  (.+)$")
      message(FATAL_ERROR "Invalid llama.cpp source manifest entry")
    endif()
    list(APPEND _expected_files "${CMAKE_MATCH_2}")
  endforeach()

  # Include directories in this walk so directory symlinks cannot hide source
  # outside the verified tree. Empty directories have no effect on a build.
  file(GLOB_RECURSE _entries_in_tree LIST_DIRECTORIES TRUE RELATIVE "${source_dir}" "${source_dir}/*")
  set(_actual_files)
  foreach(_relative IN LISTS _entries_in_tree)
    if(IS_SYMLINK "${source_dir}/${_relative}")
      message(FATAL_ERROR "The llama.cpp archive source contains a symlink: ${_relative}")
    endif()
    if(NOT IS_DIRECTORY "${source_dir}/${_relative}")
      list(APPEND _actual_files "${_relative}")
    endif()
  endforeach()
  list(SORT _actual_files)
  list(SORT _expected_files)
  if(NOT "${_actual_files}" STREQUAL "${_expected_files}")
    message(FATAL_ERROR "The llama.cpp archive source file set differs from the locked b11461 archive")
  endif()
  foreach(_entry IN LISTS _entries)
    string(REGEX MATCH "^([0-9a-f]+)  (.+)$" _unused "${_entry}")
    set(_expected_hash "${CMAKE_MATCH_1}")
    set(_relative "${CMAKE_MATCH_2}")
    file(SHA256 "${source_dir}/${_relative}" _actual_hash)
    if(_actual_hash STREQUAL _expected_hash)
      continue()
    endif()
    # Permit precisely the reviewed post-patch bytes on subsequent configure.
    # Patches themselves are independently SHA256 checked before git apply.
    if(_relative STREQUAL "src/llama-context.cpp" AND
       _actual_hash STREQUAL "513f5cc789180f4f094217c7397a53aa6cbda7c0a7c2aa239fe1cd0b19a2c025")
      continue()
    endif()
    if(_relative STREQUAL "ggml/src/ggml-backend-reg.cpp" AND
       _actual_hash STREQUAL "bfb21d94932be91e243e2746f0d19e8822a6ad91502c67b408e0d1d4d29828c9")
      continue()
    endif()
    message(FATAL_ERROR "The llama.cpp archive source file differs from its locked hash: ${_relative}")
  endforeach()
endfunction()

function(qcopilots_search_apply_llama_patch source_dir patch_dir name hash)
  set(_patch "${patch_dir}/${name}")
  file(SHA256 "${_patch}" _actual_hash)
  if(NOT _actual_hash STREQUAL hash)
    message(FATAL_ERROR "The llama.cpp patch ${name} differs from its reviewed hash")
  endif()
  # An archive may be unpacked inside another Git checkout. Without a ceiling,
  # git apply can silently skip paths relative to that enclosing repository.
  file(REAL_PATH "${source_dir}/.." _source_parent)
  set(_git_command "${CMAKE_COMMAND}" -E env "GIT_CEILING_DIRECTORIES=${_source_parent}"
    "${GIT_EXECUTABLE}")
  if(NOT EXISTS "${source_dir}/.git")
    # Preserve archive bytes independently of the developer's Git defaults.
    # The approved post-patch manifest entries use canonical LF on all hosts.
    list(APPEND _git_command -c core.autocrlf=false -c core.eol=lf)
  endif()
  list(APPEND _git_command -C "${source_dir}")
  execute_process(COMMAND ${_git_command} apply --reverse --check "${_patch}"
    RESULT_VARIABLE _applied OUTPUT_QUIET ERROR_QUIET)
  if(NOT _applied EQUAL 0)
    execute_process(COMMAND ${_git_command} apply --check "${_patch}"
      COMMAND_ERROR_IS_FATAL ANY)
    execute_process(COMMAND ${_git_command} apply "${_patch}"
      COMMAND_ERROR_IS_FATAL ANY)
  endif()
endfunction()

function(qcopilots_search_prepare_llama_source source_dir patch_dir)
  qcopilots_search_verify_llama_source("${source_dir}")
  qcopilots_search_apply_llama_patch("${source_dir}" "${patch_dir}"
    llama.cpp-no-logits.patch 33c8c4812a4307aef3f398e5ba622edcc54ef421054edd98ca0f71906b9a2d7e)
  qcopilots_search_apply_llama_patch("${source_dir}" "${patch_dir}"
    llama.cpp-utf8-backend-path.patch 28f3600f4dcb9a474987d3e016918f1170ac4a3bd3a1b16c194edb30957642a1)
endfunction()

function(qcopilots_search_verify_llama_source source_dir)
  if(EXISTS "${source_dir}/.git")
    # A worktree may use a .git file. Require the exact root so an enclosing
    # application's Git repository never authenticates an unpacked archive.
    execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${source_dir}" rev-parse --show-toplevel
      OUTPUT_VARIABLE _git_root OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
    file(REAL_PATH "${source_dir}" _source_root)
    file(REAL_PATH "${_git_root}" _git_root)
    if(WIN32)
      string(TOLOWER "${_source_root}" _source_root)
      string(TOLOWER "${_git_root}" _git_root)
    endif()
    if(NOT _source_root STREQUAL _git_root)
      message(FATAL_ERROR "The llama.cpp Git checkout root differs from the requested source directory")
    endif()
    execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${source_dir}" rev-parse HEAD
      OUTPUT_VARIABLE _commit OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
    if(NOT _commit STREQUAL "4d756bc72bf00a4aacf410ae15a2d315f3db400d")
      message(FATAL_ERROR "llama.cpp b11461 does not match the locked commit")
    endif()
  else()
    qcopilots_search_verify_llama_archive("${source_dir}")
  endif()
endfunction()
