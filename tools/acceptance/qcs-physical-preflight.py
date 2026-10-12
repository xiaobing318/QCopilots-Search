"""Read-only adapter to the repository's existing evidence validators."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True
os.environ["GIT_OPTIONAL_LOCKS"] = "0"


def is_redirect(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def run_preflight(args):
    result = {"passed": False, "readOnly": True}
    try:
        source = args.source.resolve()
        # Reject redirection before resolving, then exclude both nested layouts.
        raw_build = args.build_root.absolute()
        for item in (raw_build, *raw_build.parents):
            if is_redirect(item):
                raise ValueError("BuildRoot ancestors must not redirect writes")
        build_root = raw_build.resolve()
        if build_root.is_relative_to(source) or source.is_relative_to(build_root):
            raise ValueError("BuildRoot must be separate from the source tree")
        if not build_root.is_dir():
            raise ValueError("BuildRoot must already exist")
        for name in ("reports", "release-evidence", "runs"):
            output = build_root / name
            if is_redirect(output):
                raise ValueError(f"Output directory redirects writes: {output}")
        qt_prefix = args.qt_prefix.resolve()
        build_path = args.build_record.resolve()
        expected = args.expected_commit.lower()
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError("ExpectedCommit must be a full immutable Git SHA")
        tool = source / "tools/qcs-release-evidence.py"
        spec = importlib.util.spec_from_file_location("qcopilots_search_physical_evidence_api", tool)
        evidence = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = evidence
        spec.loader.exec_module(evidence)
        build = evidence.read_json(build_path)
        errors = evidence.verified_build(build_path.parent, build)
        identity = evidence.source_identity(source)
        errors.extend(evidence.valid_identity(identity))
        if Path(build.get("sourceDirectory", "")).resolve() != source:
            errors.append("Build sourceDirectory does not match the explicit Source")
        if identity != build.get("identity"):
            errors.append("Current source tree, locks or working-tree state differ from the build receipt")
        if identity.get("checkoutCommit") != expected or identity.get("candidateHead") != expected:
            errors.append("Current clean source identity does not match ExpectedCommit")
        required = {"qcs", "qcs-service", "qcs-worker",
                    "qcs-cli", "qcs-benchmark", "qcs-tests-file-capacity"}
        programs = build.get("programs", {})
        if set(programs) != required:
            errors.append("Build receipt must bind exactly the six approved producer programs")
        observed = {}
        for name in sorted(required & set(programs)):
            program = programs[name]
            original = Path(program["originalPath"]).resolve()
            if not original.is_relative_to(build_root / "release"):
                errors.append(f"Producer is outside the supplied BuildRoot/release: {name}")
            actual = evidence.digest(original)
            observed[name] = {"path": str(original), "expectedSha256": program["sha256"], "actualSha256": actual}
            if actual != program["sha256"]:
                errors.append(f"Current producer differs from its recorded build: {name}")
        runtime_libraries = {}
        if not (qt_prefix / "bin/Qt6Core.dll").is_file():
            errors.append("QtPrefix does not contain the Windows Qt6Core runtime")
        for directory in (build_root / "release/bin", build_root / "release/lib", qt_prefix / "bin"):
            for library in sorted(directory.glob("*.dll")):
                runtime_libraries[str(library.resolve())] = evidence.digest(library)
        copy_budget = 0
        if source.is_relative_to(build_path.parent) or build_root.is_relative_to(build_path.parent):
            errors.append("BuildRecord must live in its own evidence directory, not a source/build ancestor")
        else:
            for item in build_path.parent.rglob("*"):
                if is_redirect(item):
                    errors.append(f"Build evidence contains a redirected path: {item}")
                elif item.is_file():
                    copy_budget += ((item.stat().st_size + 4095) // 4096) * 4096 + 4096
                elif item.is_dir():
                    copy_budget += 4096
        result.update(identity=identity, source=str(source), buildRoot=str(build_root), qtPrefix=str(qt_prefix),
                      buildRecord=str(build_path), buildRecordSha256=evidence.digest(build_path),
                      producers=observed, runtimeLibraries=runtime_libraries,
                      buildEvidenceCopyBudgetBytes=copy_budget)
        if args.run_record:
            record_path = args.run_record.resolve()
            record = evidence.read_json(record_path)
            errors.extend(evidence.inspect_local_run(record_path.parent, record, "physical", identity))
            if record.get("buildRecordSha256") != evidence.digest(build_path):
                errors.append("Physical execution references a different supplied build receipt")
            if record.get("programName") != "qcs-tests-file-capacity":
                errors.append("Physical execution did not select the approved capacity producer")
            raw_path = evidence.safe_file(record_path.parent, record["report"])
            raw = evidence.read_json(raw_path)
            errors.extend(evidence.validate_physical(raw, minimum_files=100000))
            if raw.get("createdPhysicalFiles") != 100000:
                errors.append("This entry point requires exactly 100000 physical files")
            actual_root = Path(raw["actualRoot"])
            expected_parent = (record_path.parent / "temporary").resolve()
            if not actual_root.is_absolute() or not actual_root.resolve().is_relative_to(expected_parent):
                errors.append("Reported fixture root is outside this execution's owned temporary root")
            if actual_root.exists():
                errors.append("The supposedly cleaned physical fixture still exists")
            result.update(record=str(record_path), report=str(raw_path), physical=raw,
                          actualFixtureRemoved=not actual_root.exists())
        result["errors"] = errors
        result["passed"] = not errors
    except Exception as error:
        result["errors"] = [f"{type(error).__name__}: {error}"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--qt-prefix", type=Path, required=True)
    parser.add_argument("--build-record", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--run-record", type=Path)
    args = parser.parse_args(argv)
    result = run_preflight(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
