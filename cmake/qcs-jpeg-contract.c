/* Compiled in every pinned JPEG consumer with that target's header search path. */
#include <stddef.h>
#include <stdio.h>
#include <jpeglib.h>

#if JPEG_LIB_VERSION != 62
#error "QCopilots Search requires the pinned libjpeg API 62 headers"
#endif

#if !defined(LIBJPEG_TURBO_VERSION_NUMBER) || LIBJPEG_TURBO_VERSION_NUMBER != 3001004
#error "QCopilots Search requires the pinned libjpeg-turbo 3.1.4.1 headers"
#endif
