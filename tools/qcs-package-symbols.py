#!/usr/bin/env python3
"""Build a separate, verified debug-symbol archive from CMake's target manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def command(*arguments):
    completed = subprocess.run(list(map(str, arguments)), check=True, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=300)
    return completed.stdout


def build_symbols(manifest_path, output):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {"qcs-gui", "qcs-service", "qcs-worker", "qcs-cli"}
    if {entry["name"] for entry in manifest["programs"]} != expected or len(manifest["programs"]) != 4:
        raise RuntimeError("Debug symbols require all four application executables")
    for field in ("version", "platform", "architecture"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", manifest[field]):
            raise RuntimeError(f"Invalid CMake symbols manifest field: {field}")
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ("qcs-symbols-" + "-".join(manifest[field] for field in ("version", "platform", "architecture")) + ".zip")
    report = {"schemaVersion": 1, "version": manifest["version"], "platform": manifest["platform"],
              "architecture": manifest["architecture"], "scope": "application code, without third-party library debug information", "programs": []}
    with tempfile.TemporaryDirectory(prefix="qcs-symbols-", dir=output) as temporary:
        root = Path(temporary)
        for entry in sorted(manifest["programs"], key=lambda item: item["name"]):
            binary = Path(entry["binary"])
            if not binary.is_file():
                raise RuntimeError(f"Application executable is missing: {binary}")
            record = {"name": entry["name"], "unstrippedBinarySha256": digest(binary)}
            if manifest["platform"] == "Windows":
                pdb = Path(entry["pdb"])
                if not pdb.is_file():
                    raise RuntimeError(f"Application PDB is missing. Rebuild with debug information: {pdb}")
                with pdb.open("rb") as stream:
                    if not stream.read(32).startswith(b"Microsoft C/C++ MSF 7.00"):
                        raise RuntimeError(f"Invalid PDB header: {pdb}")
                shutil.copy2(pdb, root / (entry["name"] + ".pdb"))
            elif manifest["platform"] == "Linux":
                readelf = shutil.which("readelf")
                objcopy = shutil.which("objcopy")
                if not readelf or not objcopy:
                    raise RuntimeError("binutils readelf and objcopy are required to package Linux symbols")
                if ".debug_info" not in command(readelf, "--sections", "--wide", binary):
                    raise RuntimeError(f"Executable has no DWARF debug information: {binary}")
                symbols = root / (entry["name"] + ".debug")
                command(objcopy, "--only-keep-debug", binary, symbols)
                if ".debug_info" not in command(readelf, "--sections", "--wide", symbols):
                    raise RuntimeError(f"Debug information was not retained: {symbols}")
                record["elfNotes"] = command(readelf, "--notes", binary)
            elif manifest["platform"] == "Darwin":
                symbols = root / (entry["name"] + ".dSYM")
                command("xcrun", "dsymutil", binary, "-o", symbols)
                details = command("xcrun", "dwarfdump", "--debug-info", symbols)
                if "DW_TAG_compile_unit" not in details:
                    raise RuntimeError(f"Generated dSYM has no source debug information: {symbols}")
                record["machOUuids"] = command("xcrun", "dwarfdump", "--uuid", binary)
                record["symbolsUuids"] = command("xcrun", "dwarfdump", "--uuid", symbols)
                if set(re.findall(r"UUID: ([A-Fa-f0-9-]+)", record["machOUuids"])) != set(re.findall(r"UUID: ([A-Fa-f0-9-]+)", record["symbolsUuids"])):
                    raise RuntimeError(f"dSYM UUID does not match the executable: {binary}")
            else:
                raise RuntimeError(f"Unsupported symbol format: {manifest['platform']}")
            report["programs"].append(record)
        report["files"] = [{"path": path.relative_to(root).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
                           for path in sorted(root.rglob("*")) if path.is_file()]
        (root / "qcs-symbols-manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary_zip = root / "archive.zip"
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file() and path != temporary_zip:
                    item = zipfile.ZipInfo(path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0))
                    item.compress_type = zipfile.ZIP_DEFLATED
                    item.external_attr = 0o100644 << 16
                    with path.open("rb") as source, archive.open(item, "w", force_zip64=True) as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
        os.replace(temporary_zip, archive_path)
    archive_hash = digest(archive_path)
    archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(f"{archive_hash}  {archive_path.name}\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive_path), "sha256": archive_hash, "programs": len(report["programs"])}))
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    output = args.output_dir.expanduser().resolve()
    if output == source or source in output.parents or output == Path(output.anchor):
        raise RuntimeError("Symbol archive output must be outside the checkout")
    build_symbols(args.manifest.expanduser().resolve(), output)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        main()
    except Exception as error:
        print(f"qcs symbols: {error}", file=sys.stderr)
        sys.exit(1)
