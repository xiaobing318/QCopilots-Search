#!/usr/bin/env python3
"""Verify shipped icon images and optionally the icons embedded in built products."""
from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import plistlib
import struct
import sys
import zlib


SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256, 512)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def png_pixels(data: bytes, size: int, label: str) -> bytes:
    """Check PNG integrity and decode 8-bit RGBA pixels, including PNG row filters."""
    require(data.startswith(PNG_SIGNATURE), f"{label}: not a PNG image")
    offset, compressed, header, ended = 8, bytearray(), None, False
    while offset < len(data):
        require(offset + 12 <= len(data), f"{label}: truncated PNG chunk")
        length, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 12 + length
        require(end <= len(data), f"{label}: truncated {kind!r} chunk")
        payload = data[offset + 8:end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        require(zlib.crc32(kind + payload) == crc, f"{label}: incorrect {kind!r} CRC")
        if header is None:
            require(kind == b"IHDR" and length == 13, f"{label}: missing PNG header")
        if kind == b"IHDR":
            require(header is None, f"{label}: duplicate PNG header")
            header = struct.unpack(">IIBBBBB", payload)
            require(header == (size, size, 8, 6, 0, 0, 0),
                    f"{label}: expected {size} x {size} non-interlaced 8-bit RGBA, got {header}")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            require(length == 0 and end == len(data), f"{label}: invalid PNG end")
            ended = True
        offset = end
    require(ended and compressed, f"{label}: missing image data or PNG end")
    raw = zlib.decompress(compressed)
    stride = size * 4
    require(len(raw) == size * (stride + 1), f"{label}: incorrect decompressed size")
    pixels, previous = bytearray(), bytearray(stride)
    for y in range(size):
        start = y * (stride + 1)
        method, row = raw[start], bytearray(raw[start + 1:start + stride + 1])
        require(method <= 4, f"{label}: unknown PNG filter {method}")
        for x in range(stride):
            left = row[x - 4] if x >= 4 else 0
            above = previous[x]
            upper_left = previous[x - 4] if x >= 4 else 0
            if method == 1:
                value = left
            elif method == 2:
                value = above
            elif method == 3:
                value = (left + above) // 2
            elif method == 4:
                estimate = left + above - upper_left
                distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
                value = (left, above, upper_left)[distances.index(min(distances))]
            else:
                value = 0
            row[x] = (row[x] + value) & 255
        pixels.extend(row)
        previous = row
    alpha = pixels[3::4]
    require(0 in alpha and max(alpha) > 0, f"{label}: expected visible artwork and transparent background")
    return bytes(pixels)


def ico_images(data: bytes, pixels: dict[int, bytes]) -> dict[int, bytes]:
    require(len(data) >= 6, "ICO: missing header")
    reserved, kind, count = struct.unpack_from("<HHH", data)
    require(reserved == 0 and kind == 1 and count > 0, "ICO: invalid header")
    table_end = 6 + count * 16
    require(table_end <= len(data), "ICO: truncated directory")
    images = {}
    for index in range(count):
        width, height, colors, reserved, planes, depth, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + index * 16)
        width, height = width or 256, height or 256
        require(width == height and width in pixels and width not in images,
                f"ICO: unexpected or repeated image size {width} x {height}")
        require(colors == reserved == 0 and planes in (0, 1) and depth == 32,
                f"ICO: invalid {width}-pixel directory entry")
        require(offset >= table_end and offset + length <= len(data), "ICO: invalid image offset")
        image = data[offset:offset + length]
        require(png_pixels(image, width, f"ICO/{width}") == pixels[width],
                f"ICO: {width}-pixel image differs from its source PNG")
        images[width] = image
    require({16, 32, 48, 256}.issubset(images), "ICO: missing small, medium or large Windows icons")
    return images


def verify_icns(data: bytes, pixels: dict[int, bytes]) -> None:
    require(len(data) >= 8 and data[:4] == b"icns", "ICNS: missing header")
    require(struct.unpack_from(">I", data, 4)[0] == len(data), "ICNS: incorrect container size")
    dimensions = {b"icp4": 16, b"icp5": 32, b"icp6": 64, b"ic07": 128,
                  b"ic08": 256, b"ic09": 512, b"ic10": 1024, b"ic11": 32,
                  b"ic12": 64, b"ic13": 256, b"ic14": 512}
    offset, seen, sizes = 8, set(), set()
    while offset < len(data):
        require(offset + 8 <= len(data), "ICNS: truncated chunk header")
        kind, length = struct.unpack_from(">4sI", data, offset)
        require(length >= 8 and offset + length <= len(data), "ICNS: invalid chunk size")
        require(kind not in seen, f"ICNS: repeated chunk {kind!r}")
        seen.add(kind)
        if kind in dimensions:
            size = dimensions[kind]
            require(png_pixels(data[offset + 8:offset + length], size, f"ICNS/{kind!r}") == pixels[size],
                    f"ICNS: {kind!r} image differs from its source PNG")
            sizes.add(size)
        else:
            require(kind == b"TOC ", f"ICNS: unsupported image encoding {kind!r}")
        offset += length
    require({32, 128, 256, 512, 1024}.issubset(sizes), "ICNS: missing required desktop/Retina sizes")


def verify_executable(path: Path, expected: dict[int, bytes]) -> None:
    require(sys.platform == "win32", "--executable requires native Windows resource APIs")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = (wintypes.HMODULE,)
    kernel.FreeLibrary.restype = wintypes.BOOL
    kernel.FindResourceW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p)
    kernel.FindResourceW.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = (wintypes.HMODULE, ctypes.c_void_p)
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.LoadResource.argtypes = (wintypes.HMODULE, ctypes.c_void_p)
    kernel.LoadResource.restype = ctypes.c_void_p
    kernel.LockResource.argtypes = (ctypes.c_void_p,)
    kernel.LockResource.restype = ctypes.c_void_p
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_ssize_t)
    kernel.EnumResourceNamesW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, callback_type,
                                         ctypes.c_ssize_t)
    kernel.EnumResourceNamesW.restype = wintypes.BOOL
    # Map only resource data. Do not load DLL dependencies or execute initialization code.
    module = kernel.LoadLibraryExW(str(path.resolve(strict=True)), None, 0x02 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        names = []

        @callback_type
        def collect(_module, _kind, name, _parameter):
            names.append(name if name <= 0xFFFF else ctypes.wstring_at(name))
            return True

        if not kernel.EnumResourceNamesW(module, 14, collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        require(bool(names), f"{path}: no group icon resources")

        def resource(name: int | str, kind: int) -> bytes:
            identifier = ctypes.c_void_p(name) if isinstance(name, int) else ctypes.cast(
                ctypes.c_wchar_p(name), ctypes.c_void_p)
            found = kernel.FindResourceW(module, identifier, kind)
            if not found:
                raise ctypes.WinError(ctypes.get_last_error())
            size = kernel.SizeofResource(module, found)
            loaded = kernel.LoadResource(module, found)
            pointer = kernel.LockResource(loaded) if loaded else None
            require(bool(pointer) and size > 0, f"{path}: could not read resource {name}")
            return ctypes.string_at(pointer, size)

        for name in names:
            group = resource(name, 14)
            require(len(group) >= 6, f"{path}: truncated group icon")
            reserved, kind, count = struct.unpack_from("<HHH", group)
            require(reserved == 0 and kind == 1 and len(group) == 6 + count * 14,
                    f"{path}: invalid group icon directory")
            actual = {}
            for index in range(count):
                width, height, _colors, _reserved, _planes, _depth, length, icon_id = struct.unpack_from(
                    "<BBBBHHIH", group, 6 + index * 14)
                width, height = width or 256, height or 256
                require(width == height and width not in actual, f"{path}: invalid embedded icon size")
                actual[width] = resource(icon_id, 3)
                require(len(actual[width]) == length, f"{path}: embedded icon size mismatch")
            require(actual == expected, f"{path}: embedded icon group {name} differs from qcs.ico")
    finally:
        if not kernel.FreeLibrary(module):
            raise ctypes.WinError(ctypes.get_last_error())
    print(f"PASS {path.name}: actual PE icon resources match qcs.ico")


def verify_bundle(bundle: Path, expected: bytes) -> None:
    contents = bundle.resolve(strict=True) / "Contents"
    with (contents / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    name = info.get("CFBundleIconFile")
    require(isinstance(name, str) and name and Path(name).name == name,
            f"{bundle}: missing or invalid CFBundleIconFile")
    name = name if name.endswith(".icns") else name + ".icns"
    require((contents / "Resources" / name).read_bytes() == expected,
            f"{bundle}: bundle icon differs from qcs.icns")
    print(f"PASS {bundle.name}: Info.plist resolves to the shipped ICNS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True, help="Directory containing qcs.png and platform icons")
    parser.add_argument("--executable", type=Path, action="append", default=[], help="Built Windows EXE, repeat for each product")
    parser.add_argument("--bundle", type=Path, help="Built or installed macOS .app bundle")
    options = parser.parse_args()
    root = options.resources.resolve(strict=True)
    pixels = {1024: png_pixels((root / "qcs.png").read_bytes(), 1024, "qcs.png")}
    for size in SIZES:
        name = f"qcs-{size}.png"
        pixels[size] = png_pixels((root / name).read_bytes(), size, name)
    icons = ico_images((root / "qcs.ico").read_bytes(), pixels)
    icns = (root / "qcs.icns").read_bytes()
    verify_icns(icns, pixels)
    print("PASS PNG/ICO/ICNS: valid transparent images, required sizes and identical platform artwork")
    for executable in options.executable:
        verify_executable(executable, icons)
    if options.bundle:
        verify_bundle(options.bundle, icns)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        main()
    except (OSError, RuntimeError, ValueError, zlib.error, struct.error) as error:
        print(f"FAIL {error}", file=sys.stderr)
        sys.exit(1)
