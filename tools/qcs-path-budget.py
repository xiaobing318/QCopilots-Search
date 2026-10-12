#!/usr/bin/env python3
"""Inspect actual generated paths without changing a build or its source tree."""
from __future__ import annotations

import argparse
from collections import Counter, namedtuple
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


WINDOWS_OBJECT_BUDGET = 200
GENERATED_BUDGET = 240
NinjaTarget = namedtuple("NinjaTarget", ("name", "path"))
SKIP_DIRECTORIES = frozenset({
    ".git", "models", "deps", "dependencies", "source-cache", "sources",
    "runs", "test-runs", "capacity-runs", "profiles", "reports", "Testing",
})


def path_length(value: str | Path, platform: str) -> int:
    """Windows APIs count UTF-16 code units, including surrogate pairs."""
    value = str(value)
    if platform == "windows":
        return len(value.encode("utf-16-le", errors="surrogatepass")) // 2
    return len(os.fsencode(value))


def classify(path: Path, context: str = "build") -> str:
    name = path.name.lower()
    parts = [part.lower() for part in path.parts]
    if path.suffix.lower() in {".obj", ".o"}:
        return "object"
    if name.endswith(".rsp"):
        return "response"
    if name.startswith("qrc_") or "rcc" in parts:
        return "rcc"
    if name.startswith("moc_") or name == "mocs_compilation.cpp" or any(
        part.endswith("_autogen") for part in parts
    ):
        return "moc"
    if any(part.endswith("-stamp") or part.endswith("-subbuild") for part in parts) or "sb" in parts:
        return "fetchcontent"
    return context


def parse_ninja_targets(output: str, build: Path) -> list[NinjaTarget]:
    """Ninja prints decoded paths followed by ': rule', including drive colons."""
    result = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if ": " not in line:
            raise RuntimeError(f"Unexpected Ninja target line: {line}")
        name, rule = line.rsplit(": ", 1)
        if not name or not rule:
            raise RuntimeError(f"Incomplete Ninja target line: {line}")
        # Phony target names and input CMakeLists.txt entries are not generated files.
        if rule == "phony":
            continue
        path = Path(name.replace("\\", "/"))
        # Ninja graph keys retain their own spelling. An absolute CMake dyndep
        # output and a relative output can resolve to the same filesystem path,
        # but are distinct command-query keys. Canonicalize only for reporting.
        result.append(NinjaTarget(name, (path if path.is_absolute() else build / path).resolve()))
    return result


def find_ninja(build: Path, explicit: str | None = None) -> str:
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        raise RuntimeError(f"Requested Ninja executable is unavailable: {explicit}")
    cache = build / "CMakeCache.txt"
    if cache.is_file():
        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CMAKE_MAKE_PROGRAM:FILEPATH="):
                candidate = line.split("=", 1)[1]
                if Path(candidate).is_file():
                    return candidate
    found = shutil.which("ninja")
    if not found:
        raise RuntimeError("Ninja is unavailable. Set --ninja or configure CMAKE_MAKE_PROGRAM.")
    return found


def ninja_outputs(build: Path, executable: str | None = None) -> tuple[list[NinjaTarget], str]:
    if not (build / "build.ninja").is_file():
        raise RuntimeError(f"Configured Ninja build is required: {build / 'build.ninja'}")
    ninja = find_ninja(build, executable)
    process = subprocess.run(
        [ninja, "-C", str(build), "-t", "targets", "all"],
        cwd=build, capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=120,
    )
    if process.returncode:
        raise RuntimeError(f"Ninja target inspection failed ({process.returncode}): {process.stderr.strip()}")
    paths = parse_ninja_targets(process.stdout, build)
    if not paths:
        raise RuntimeError("Ninja returned no generated file targets. Build inspection is incomplete.")
    return paths, ninja


def response_arguments(command: str, platform: str) -> list[str]:
    if platform != "windows":
        return [token[1:] for token in shlex.split(command) if token.startswith("@") and len(token) > 1]
    # cmd.exe commands can contain an outer /C quote and inner executable quotes.
    # Match only response arguments, without attempting to execute or rewrite that shell syntax.
    pattern = r'''(?:^|\s)(?:@"([^"]+)"|"@([^"]+)"|@'([^']+)'|'@([^']+)'|@([^\s"'<>&|]+))'''
    return [next(value for value in match.groups() if value is not None)
            for match in re.finditer(pattern, command)]


def ninja_responses(build: Path, ninja: str, platform: str, targets: list[NinjaTarget]) -> list[Path]:
    # Ninja expands variables and subninja scopes for us. RSP files are not targets
    # and normally disappear after compilation, so a filesystem scan is insufficient.
    # `compdb` omits edges with no inputs, and plain `commands` follows only the
    # default targets. Inspect every actual graph output, without its dependency chain.
    batches = []
    batch = []
    size = 0
    for argument in dict.fromkeys(target.name for target in targets):
        length = path_length(argument, "windows") + 3
        if batch and size + length > 12000:
            batches.append(batch)
            batch = []
            size = 0
        batch.append(argument)
        size += length
    if batch:
        batches.append(batch)
    result = []
    for batch in batches:
        process = subprocess.run([ninja, "-C", str(build), "-t", "commands", "-s", *batch], cwd=build,
                                 capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=120)
        if process.returncode:
            raise RuntimeError(f"Ninja command inspection failed ({process.returncode}): {process.stderr.strip()}")
        for command in process.stdout.splitlines():
            for value in response_arguments(command, platform):
                path = Path(value.replace("\\", "/"))
                result.append((path if path.is_absolute() else build / path).resolve())
    return result


def autogen_outputs(metadata: Path) -> list[Path]:
    # CMake configures this manifest before any individual moc file exists.
    info = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(info, dict) or not isinstance(info.get("BUILD_DIR"), str):
        raise RuntimeError(f"Invalid CMake AUTOMOC metadata: {metadata}")
    build = Path(info["BUILD_DIR"]).resolve()
    result = []
    include_dirs = [Path(value).resolve() for key, value in info.items()
                    if (key == "INCLUDE_DIR" or key.startswith("INCLUDE_DIR_")) and isinstance(value, str)]
    for key, value in info.items():
        if (key.startswith("MOC_COMPILATION_FILE") or key.startswith("MOC_PREDEFS_FILE")) and value:
            result.append(Path(value).resolve())
    for header in info.get("HEADERS", []):
        if not isinstance(header, list) or len(header) < 3:
            raise RuntimeError(f"Unsupported CMake AUTOMOC header entry: {metadata}")
        if isinstance(header[2], str) and header[2]:
            bases = include_dirs if info.get("MULTI_CONFIG") else [build]
            if not bases:
                raise RuntimeError(f"Missing CMake multi-config AUTOMOC output directory: {metadata}")
            for base in bases:
                output = (base / header[2]).resolve()
                result.extend([output, Path(str(output) + ".d")])
    for source in info.get("SOURCES", []):
        if not isinstance(source, list) or len(source) < 2:
            raise RuntimeError(f"Unsupported CMake AUTOMOC source entry: {metadata}")
        if "M" not in source[1]:
            continue
        content = Path(source[0]).read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r'^\s*#\s*include\s*["<]([^">]+)[">]', content, flags=re.MULTILINE):
            if value.endswith(".moc") or (Path(value).name.startswith("moc_") and value.endswith(".cpp")):
                for base in include_dirs:
                    output = (base / value).resolve()
                    result.extend([output, Path(str(output) + ".d")])
    return result


def scan_generated(root: Path, *, installed: bool = False) -> list[Path]:
    if not root.is_dir():
        raise RuntimeError(f"Generated directory does not exist: {root}")
    result = []

    def failed(error):
        raise RuntimeError(f"Cannot inspect generated paths: {error}") from error

    for current, directories, files in os.walk(root, followlinks=False, onerror=failed):
        current_path = Path(current)
        for directory in list(directories):
            path = current_path / directory
            is_junction = getattr(path, "is_junction", lambda: False)()
            if path.is_symlink() or is_junction:
                # Do not follow links into dependency sources or runtime user data.
                directories.remove(directory)
                result.append(path.absolute())
            elif not installed and (
                directory in SKIP_DIRECTORIES or directory.endswith("-src")
            ):
                directories.remove(directory)
        result.extend((current_path / name).absolute() for name in files)
    return result


def inspect_paths(args: argparse.Namespace) -> dict:
    platform = args.platform
    object_budget = WINDOWS_OBJECT_BUDGET if platform == "windows" else GENERATED_BUDGET
    roots = {
        key: Path(value).expanduser().resolve()
        for key in ("source", "root", "build", "stage", "package_root")
        if (value := getattr(args, key, None))
    }
    report = {
        "schemaVersion": 1, "kind": "qcs-path-budget", "platform": platform,
        "lengthUnit": "utf16-code-units" if platform == "windows" else "filesystem-bytes",
        "budgets": {"object": object_budget, "generated": GENERATED_BUDGET},
        "configured": False, "status": "not-configured", "passed": None,
        "roots": {}, "paths": [], "offenders": [], "warnings": [], "errors": [],
        "excludedDirectoryNames": sorted(SKIP_DIRECTORIES), "ninja": None, "outputs": {},
    }
    for key, root in roots.items():
        length = path_length(root, platform)
        report["roots"][key] = {"path": str(root), "length": length,
                                  "generatedHeadroom": GENERATED_BUDGET - length}
        if key != "source" and length > GENERATED_BUDGET:
            report["offenders"].append({"path": str(root), "category": "root", "root": key,
                                        "length": length, "budget": GENERATED_BUDGET})
        if key == "build" and length + 40 > object_budget:
            report["warnings"].append(
                "Build root leaves fewer than 40 characters for object paths. "
                "Choose a shorter external build root if configured targets exceed the budget."
            )

    paths: dict[str, tuple[Path, str, set[str]]] = {}

    def add(path: Path, context: str, origin: str):
        key = os.path.normcase(str(path))
        if key in paths:
            paths[key][2].add(origin)
        else:
            paths[key] = (path, context, {origin})

    try:
        if "build" in roots:
            generated, ninja = ninja_outputs(roots["build"], getattr(args, "ninja", None))
            report["ninja"] = ninja
            report["configured"] = True
            for target in generated:
                add(target.path, "build", "ninja")
            for path in ninja_responses(roots["build"], ninja, platform, generated):
                add(path, "build", "ninja-command")
            for path in scan_generated(roots["build"]):
                add(path, "build", "filesystem")
                if path.name == "AutogenInfo.json":
                    for output in autogen_outputs(path):
                        add(output, "build", "cmake-autogen")
        for key, category in (("stage", "stage"), ("package_root", "package")):
            if key in roots:
                if not roots[key].exists():
                    report["outputs"][key] = {"status": "not-materialized", "count": 0}
                    report["warnings"].append(f"Output root has not been materialized: {roots[key]}")
                    continue
                generated = scan_generated(roots[key], installed=True)
                report["outputs"][key] = {"status": "inspected", "count": len(generated)}
                for path in generated:
                    add(path, category, "filesystem")
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError, ValueError, KeyError, TypeError) as error:
        report["errors"].append(str(error))

    for path, context, origins in paths.values():
        category = classify(path, context)
        budget = object_budget if category == "object" else GENERATED_BUDGET
        length = path_length(path, platform)
        containing = [(key, root) for key, root in roots.items() if path == root or root in path.parents]
        key, root = max(containing, key=lambda item: len(str(item[1])), default=(None, None))
        row = {"path": str(path), "category": category, "length": length,
               "budget": budget, "root": key,
               "rootLength": path_length(root, platform) if root else None,
               "origins": sorted(origins)}
        report["paths"].append(row)
        if length > budget:
            report["offenders"].append(row)
    report["paths"].sort(key=lambda row: (-row["length"], row["path"]))
    report["offenders"].sort(key=lambda row: (-row["length"], row["path"]))
    lengths = [row["length"] for row in report["paths"]]
    report["summary"] = {
        "count": len(lengths), "maxLength": max(lengths, default=0),
        "byCategory": dict(sorted(Counter(row["category"] for row in report["paths"]).items())),
        "distribution": {
            "atMost200": sum(length <= 200 for length in lengths),
            "201to240": sum(200 < length <= 240 for length in lengths),
            "over240": sum(length > 240 for length in lengths),
        },
    }
    if report["errors"]:
        report.update(status="unavailable", passed=False)
    elif getattr(args, "allow_stress_root", False):
        report.update(status="stress-observation", passed=None)
    elif report["offenders"]:
        report.update(status="failed", passed=False)
    elif report["configured"]:
        report.update(status="passed", passed=True)
    elif any(value["status"] == "inspected" for value in report["outputs"].values()):
        # Installed files were inspected, but this cannot stand in for a build graph check.
        report.update(status="files-inspected", passed=None)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "build", "root", "stage", "package-root"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--platform", choices=("windows", "posix"),
                        default="windows" if os.name == "nt" else "posix")
    parser.add_argument("--ninja", help="Ninja executable, otherwise resolved from CMakeCache or PATH")
    parser.add_argument("--allow-stress-root", action="store_true",
                        help="Record an explicit stress observation without claiming a normal gate pass")
    args = parser.parse_args(argv)
    if not any((args.source, args.build, args.root, args.stage, args.package_root)):
        parser.error("At least one source, build or output root must be supplied")
    destination = args.report.expanduser().resolve()
    sources = {Path(__file__).resolve().parent.parent}
    if args.source:
        sources.add(args.source.expanduser().resolve())
    if any(destination == source or source in destination.parents for source in sources):
        parser.error("Path reports must be outside the source tree")
    report = inspect_paths(args)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"Cannot write path report: {error}", file=sys.stderr)
        return 2
    print(f"Path inspection: {report['status']}, {report['summary']['count']} paths, "
          f"maximum {report['summary']['maxLength']}, {len(report['offenders'])} offenders. "
          f"Report: {destination}")
    for error in report["errors"]:
        print(error, file=sys.stderr)
    for offender in report["offenders"][:10]:
        print(f"{offender['category']}: {offender['length']} > {offender['budget']}: "
              f"{offender['path']}", file=sys.stderr)
    if report["errors"]:
        return 2
    if report["offenders"] and not args.allow_stress_root:
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
