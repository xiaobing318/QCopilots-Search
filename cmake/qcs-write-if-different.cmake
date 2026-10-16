include_guard(GLOBAL)

# Keep configure-time generated text stable so an unchanged configuration does
# not rebuild native libraries. Indexed arguments preserve semicolons and do not
# expand template-like text embedded in an upstream source file.
function(qcopilots_search_write_if_different path)
  set(_content "")
  if(ARGC GREATER 1)
    math(EXPR _last "${ARGC} - 1")
    foreach(_index RANGE 1 ${_last})
      string(APPEND _content "${ARGV${_index}}")
    endforeach()
  endif()
  if(EXISTS "${path}")
    file(READ "${path}" _existing)
    if(_existing STREQUAL _content)
      return()
    endif()
  endif()
  file(WRITE "${path}" "${_content}")
endfunction()
