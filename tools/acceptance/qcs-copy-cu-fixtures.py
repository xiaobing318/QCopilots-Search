#!/usr/bin/env python3
"""Prepare original CU fixtures, snapshot them, then copy into a fresh CU run.

Copying is a dry-run unless --execute is supplied. This tool never starts an
application, modifies fixture sources or run.json, or declares CU acceptance.
The source root contains samples/ and optional negative-models/ directories.
Keep license notices under negative-models/fixture-licenses so search does not
index them. Provide a fresh canary in the source fixture set when testing edits.
Paths are canonicalized after link checks. The only linked ancestors allowed
are macOS's standard /var and /tmp aliases to /private/var and /private/tmp.
Fixture roots, fixture files, run roots and paths below them must not be links.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import uuid
import wave
import zipfile

MANIFEST = "negative-models/qcs-fixture-copy.json"
FAILURE = "negative-models/qcs-fixture-copy-failure.json"
SOURCE_MANIFEST = "negative-models/qcs-fixture-source.json"
PREPARING = ".qcs-fixture-preparing.json"
BROWSE_ROOT = "samples/分页 普通文件"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_EPOCH_MS = 1767225600000
ROOTS = {"samples", "negative-models"}
SHA256 = re.compile(r"[0-9a-f]{64}")
RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.IGNORECASE)


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _system_alias(path):
    """Recognize exact OS aliases, never arbitrary user-controlled links."""
    if sys.platform != "darwin":
        return False
    canonical = {"/var": "/private/var", "/tmp": "/private/tmp"}.get(path.as_posix())
    return canonical is not None and path.resolve().as_posix() == canonical


def unlinked(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        linked = part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
        # Python 3.10 lacks Path.is_junction. Windows exposes the reparse bit
        # through lstat, including directory junctions and other redirects.
        try:
            linked |= bool(getattr(part.lstat(), "st_file_attributes", 0)
                           & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except FileNotFoundError:
            pass
        require(not linked or (part != path and _system_alias(part)),
                "Linked path is not permitted: " + str(part))
    return path.resolve()


def external_output(path):
    require(Path(path).is_absolute(), "Output must be an explicit absolute path")
    path = unlinked(path)
    require(not path.is_relative_to(REPOSITORY_ROOT), "Output must be outside the source checkout")
    return path


def tree_files(root):
    """Walk explicitly so unreadable directories cannot silently lose inputs."""
    pending = [unlinked(root)]
    while pending:
        directory = unlinked(pending.pop())
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = unlinked(directory / entry.name)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            else:
                require(entry.is_file(follow_symlinks=False), "Fixture source is not a regular file")
                yield path


def relative_parts(relative):
    require(isinstance(relative, str) and ":" not in relative and "\\" not in relative,
            "Unsafe fixture path")
    parts = relative.split("/")
    require(not PurePosixPath(relative).is_absolute() and len(parts) >= 2
            and parts[0] in ROOTS and all(part not in {"", ".", ".."} for part in parts),
            "Fixture path outside permitted roots")
    require(all(not part.endswith((" ", ".")) and not RESERVED.fullmatch(part)
                and not any(ord(char) < 32 or char in '<>"|?*' for char in part)
                for part in parts), "Fixture path is not portable")
    require(relative.casefold() not in {MANIFEST.casefold(), FAILURE.casefold()},
            "Fixture path collides with the copy report")
    return parts


def target(root, relative):
    value = unlinked(root.joinpath(*relative_parts(relative)))
    require(value.is_relative_to(root) and value != root, "Fixture resolves outside root")
    return value


def report_target(root, relative):
    # Report names are reserved against input files, but have the same boundary.
    value = unlinked(root / relative)
    require(value.is_relative_to(root) and value != root, "Report resolves outside run")
    return value


def check_file(path, size, expected, mtime_ms=None):
    require(type(size) is int and size >= 0 and isinstance(expected, str)
            and SHA256.fullmatch(expected), "Invalid file size or SHA-256")
    path = unlinked(path)
    require(path.is_file() and path.stat().st_size == size, "File size differs: " + str(path))
    require(sha(path) == expected, "File SHA-256 differs: " + str(path))
    result = {"path": str(path), "bytes": size, "sha256": expected}
    if mtime_ms is not None:
        require(type(mtime_ms) is int and mtime_ms >= 0, "Invalid file mtimeMs")
        require(path.stat().st_mtime_ns // 1_000_000 == mtime_ms, "File mtimeMs differs: " + str(path))
        result["mtimeMs"] = mtime_ms
    return result


def empty(path, required=True):
    path = unlinked(path)
    require(path.is_dir() if required else not path.exists() or path.is_dir(),
            "Expected fixture directory: " + str(path))
    require(not path.exists() or not any(path.iterdir()),
            "Refusing an already nonempty directory: " + str(path))


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def _pdf_fixture():
    """Small real PDF with explicit physical pages and a non-first-page token."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 5 0 R 7 0 R] /Count 3 >>"]
    for page, token in enumerate(("qcscovermarker", "qcssecondmarker", "qcsthirdpagemarker"), 1):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 9 0 R >> >> /Contents {page * 2 + 2} 0 R >>".encode())
        lines = [f"QCopilots Search physical page {page}", f"Unique location token: {token}",
                 "This original fixture has three separate physical pages.",
                 "Select the requested page in the preview and external reader.",
                 "A successful application launch alone does not prove positioning."]
        stream = b"BT /F1 16 Tf 50 780 Td 26 TL " + b" ".join(
            (b"T* " if i else b"") + b"(" + line.encode() + b") Tj" for i, line in enumerate(lines)) + b" ET\n"
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(document)


def _docx_fixture():
    content = io.BytesIO()
    entries = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>原创 Office 文档 qcswordbodymarker</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格中文 qcswordtablemarker</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>Use the real extracted text to verify preview and keyword search.</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
    }
    with zipfile.ZipFile(content, "w") as archive:
        for name, text in entries.items():
            archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), text.encode("utf-8"))
    return content.getvalue()


def _video_fixture():
    """Original 12-second AVI: red circle first, blue square from second six."""
    width, height, fps, frames = 160, 120, 2, 24
    size = width * height * 3
    def chunk(tag, data):
        return tag + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")
    def listing(tag, data):
        return chunk(b"LIST", tag + data)
    avih = struct.pack("<14I", 500000, size * fps, 0, 0x10, frames, 0, 1, size, width, height, 0, 0, 0, 0)
    strh = b"vidsDIB " + struct.pack("<10I4H", 0, 0, 0, 1, fps, 0, frames, size, 0xffffffff, 0, 0, 0, width, height)
    strf = struct.pack("<IIIHH6I", 40, width, height, 1, 24, 0, size, 0, 0, 0, 0)
    movi, index, offset = bytearray(), bytearray(), 4
    for frame in range(frames):
        blue = frame >= frames // 2
        pixels = bytearray()
        for y in reversed(range(height)):
            for x in range(width):
                # Also move the object. Production frame deduplication uses a
                # grayscale hash, so a color-only change at the same location
                # can correctly collapse into the first sample.
                inside = (100 <= x < 150 and 10 <= y < 60) if blue else (x - 80) ** 2 + (y - 60) ** 2 <= 30 ** 2
                pixels.extend((255, 0, 0) if inside and blue else (0, 0, 255) if inside else (255, 255, 255))
        movi.extend(chunk(b"00db", pixels))
        index.extend(b"00db" + struct.pack("<3I", 0x10, offset, size))
        offset += size + 8
    return chunk(b"RIFF", b"AVI " + listing(b"hdrl", chunk(b"avih", avih) + listing(b"strl", chunk(b"strh", strh) + chunk(b"strf", strf))) + listing(b"movi", movi) + chunk(b"idx1", index))


def prepare_source(source_root):
    """Generate one explicit, fresh source set. Never creates a CU pass record."""
    root = external_output(source_root)
    require(not root.exists(), "Fixture source output already exists")
    # Only these named, repository-owned inputs are read. In particular, no
    # previous profile, semantic run or holdout result is discovered implicitly.
    input_names = ("tests/semantic/assets.json", "tests/semantic/assets/NOTICE.md",
                   "tests/semantic/assets/photo-002.jpg", "tests/semantic/assets/speech-001.wav",
                   "tests/semantic/assets/speech-002.wav")
    inputs, source_bytes = [], {}
    for name in input_names:
        path = unlinked(REPOSITORY_ROOT / name)
        data = path.read_bytes()
        source_bytes[name] = data
        inputs.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    assets = json.loads(source_bytes[input_names[0]])
    locked = {item["path"]: item for item in assets["assets"]} if "assets" in assets else {}
    for name in input_names[2:]:
        key = name.removeprefix("tests/semantic/")
        row = locked.get(key)
        require(row is not None and row["bytes"] == len(source_bytes[name])
                and row["sha256"] == hashlib.sha256(source_bytes[name]).hexdigest(), "Fixed CU input does not match assets.json: " + name)
    pcm = bytearray(16000 * 2 * 75)
    for name, start_seconds in ((input_names[3], 0), (input_names[4], 55)):
        with wave.open(io.BytesIO(source_bytes[name]), "rb") as incoming:
            require((incoming.getnchannels(), incoming.getsampwidth(), incoming.getframerate(), incoming.getcomptype()) == (1, 2, 16000, "NONE"), "Unexpected speech fixture format")
            data = incoming.readframes(incoming.getnframes())
        start = start_seconds * 16000 * 2
        require(start + len(data) <= len(pcm), "Speech exceeds the fixed CU audio timeline")
        pcm[start:start + len(data)] = data
    audio = io.BytesIO()
    with wave.open(audio, "wb") as outgoing:
        outgoing.setnchannels(1)
        outgoing.setsampwidth(2)
        outgoing.setframerate(16000)
        outgoing.writeframes(pcm)
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / PREPARING, {"kind": "qcopilots-search-cu-fixture-preparing",
                                 "sourceRoot": str(root), "generatorSha256": sha(__file__)})
    files = []
    def emit(relative, content, role, *, mtime_ms=FIXTURE_EPOCH_MS, **expected):
        path = target(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        target(root, relative)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with path.open("xb") as output:
            output.write(data)
        os.utime(path, ns=(mtime_ms * 1_000_000, mtime_ms * 1_000_000))
        checked = check_file(path, len(data), hashlib.sha256(data).hexdigest(), mtime_ms)
        files.append({"relativePath": relative, "bytes": checked["bytes"], "sha256": checked["sha256"], "mtimeMs": mtime_ms, "role": role, **expected})
    for i in range(205):
        group = 98 if 98 <= i <= 101 else 198 if 198 <= i <= 201 else i
        name = f"文档 {i + 1:03d} 中文.txt"
        if i == 204:
            name = "长路径 中文目录/第二层 有空格目录/第三层用于验证列表省略与完整路径提示/" + name
        emit(BROWSE_ROOT + "/" + name, f"独立分页样本 {i + 1:03d}。\nOriginal browse fixture qcsbrowse{i + 1:03d}marker.\n正文只能在右侧预览。\n", "browse", mtime_ms=FIXTURE_EPOCH_MS - group * 1000, query=f"qcsbrowse{i + 1:03d}marker")
    emit("samples/变更与续读/修改 canary.txt", "Before edit qcseditbeforemarker.\n", "edit-canary")
    emit("samples/变更与续读/删除 canary.txt", "Delete only this run copy qcsdeletecanarymarker.\n", "delete-canary")
    emit("negative-models/actions/修改后内容.txt", "After edit qcseditaftermarker.\n", "edit-template")
    emit("negative-models/actions/待新增 short.txt", "New short file qcsarrivingshortmarker.\n", "new-template")
    paragraphs = []
    for i in range(240):
        paragraphs.append(f"Paragraph {i + 1:03d} qcslongpart{i + 1:03d}marker.\n" + (f"Original continuation paragraph {i + 1:03d} explains its numbered observations. " * 25) + "\n\n")
    emit("samples/变更与续读/长文档 中文.txt", "".join(paragraphs), "continuation", queries=["qcslongpart001marker", "qcslongpart120marker", "qcslongpart240marker"])
    emit("samples/内容定位/三页 literal {page}.pdf", _pdf_fixture(), "pdf", query="qcsthirdpagemarker", physicalPage=3)
    emit("samples/内容定位/Office 中文.docx", _docx_fixture(), "office", queries=["qcswordbodymarker", "qcswordtablemarker"])
    emit("samples/内容定位/照片 中文.jpg", source_bytes[input_names[2]], "image", sourceInput=input_names[2], query="A domestic cat walking through white snow outdoors")
    emit("samples/内容定位/延迟语音 中文.wav", audio.getvalue(), "audio", query="A spoken reminder to buy milk, eggs and bread", targetRegionMs=[55000, 75000], expectedWindowStartMs=50000)
    emit("samples/内容定位/两段图形 中文.avi", _video_fixture(), "video", query="A blue square on a white background", targetRegionMs=[6000, 12000], expectedFrameStartMs=6500)
    notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"language_info": {"name": "python"}}, "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# 原创 Notebook\n"]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["def first_cell():\n", "    return 1\n"]},
        {"cell_type": "markdown", "metadata": {}, "source": ["目标位于第四个单元格。\n"]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["def qcsnotebooklocationmarker():\n", "    return 42\n"]}]}
    emit("samples/代码 单元格/Notebook 中文.ipynb", json.dumps(notebook, ensure_ascii=False, indent=2) + "\n", "notebook", query="qcsnotebooklocationmarker", cell=4)
    code = "".join(f"# Original numbered line {i}\n" for i in range(1, 61)) + "def qcscodelocationmarker():\n    return 42\n"
    emit("samples/代码 单元格/代码 literal {line}.py", code, "code", query="qcscodelocationmarker", targetLine=61)
    emit("samples/共享 文本/双空间共享.txt", "Independent ordinary/code visibility qcssharedspacemarker.\n", "shared")
    emit("samples/普通 排除/排除样本.txt", "qcsordinaryexcludedmarker\n", "ordinary-exclusion")
    emit("samples/代码 排除/排除样本.py", "qcs_code_excluded_marker = 1\n", "code-exclusion")
    emit("samples/故障输入/损坏文档.docx", b"deliberately-not-a-zip", "invalid-document")
    emit("samples/故障输入/损坏音频.wav", b"deliberately-not-a-wave", "invalid-audio")
    emit("negative-models/embeddinggemma-2-Q8_0.gguf", b"deliberately-not-a-gguf\n", "invalid-model")
    for name in input_names[:2]:
        emit("negative-models/fixture-licenses/" + Path(name).name, source_bytes[name], "license", sourceInput=name)
    manifest = {"schemaVersion": 1, "kind": "qcopilots-search-cu-fixture-source", "fixtureVersion": "cu-v1",
                "generator": {"path": "tools/acceptance/qcs-copy-cu-fixtures.py", "sha256": sha(__file__)},
                "sourceInputs": inputs, "sourceRoot": str(root), "browseRoot": BROWSE_ROOT, "browsingFiles": 205,
                "browseOrder": {"primary": "mtimeMs DESC", "tie": "actual indexed file_id DESC", "tieGroupsZeroBased": [[98, 99, 100, 101], [198, 199, 200, 201]], "pageSizes": [100, 100, 5]},
                "files": files, "acceptancePassed": False,
                "license": "Original generated text, code, PDF, DOCX and AVI use the repository MIT license. Photo and source audio retain their recorded asset licenses.",
                "limits": ["Media target regions are authored expectations. Actual search hits and external positions must be observed.", "Generated UI fixtures are not semantic-quality or capacity acceptance evidence.", "Index only browseRoot first. Add the other roots for later scenarios."]}
    path = target(root, SOURCE_MANIFEST)
    write_json(path, manifest)
    os.utime(path, ns=(FIXTURE_EPOCH_MS * 1_000_000, FIXTURE_EPOCH_MS * 1_000_000))
    unlinked(root / PREPARING).unlink()
    return {"sourceRoot": str(root), "manifest": str(path), "manifestSha256": sha(path), "browsingFiles": 205, "files": len(files), "acceptancePassed": False}


def validate_generated_source(root):
    require(not unlinked(root / PREPARING).exists(),
            "Incomplete fixture preparation. Preserve the partial directory and prepare a new output root.")
    path = target(root, SOURCE_MANIFEST)
    if not path.exists():
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    require(manifest.get("schemaVersion") == 1
            and manifest.get("kind") == "qcopilots-search-cu-fixture-source", "Invalid prepared fixture source manifest")
    require(manifest.get("generator") == {"path": "tools/acceptance/qcs-copy-cu-fixtures.py", "sha256": sha(__file__)},
            "Prepared fixture generator differs from the current candidate source")
    for item in manifest["sourceInputs"]:
        name = PurePosixPath(item["path"])
        require(not name.is_absolute() and ".." not in name.parts and "\\" not in item["path"] and ":" not in item["path"], "Unsafe prepared source input")
        check_file(REPOSITORY_ROOT.joinpath(*name.parts), item["bytes"], item["sha256"])
    expected = {SOURCE_MANIFEST}
    for item in manifest["files"]:
        relative = item["relativePath"]
        require(relative not in expected and type(item.get("mtimeMs")) is int, "Invalid prepared file entry")
        expected.add(relative)
        check_file(target(root, relative), item["bytes"], item["sha256"], item["mtimeMs"])
    actual = set()
    for file in tree_files(root):
        actual.add(file.relative_to(root).as_posix())
    require(actual == expected, "Prepared fixture source contains unrecorded or missing files")


def create_plan(source_root, plan_path, excluded_relative=None):
    """Hash current fixtures, without embedding any candidate or historical run."""
    source_root, plan_path = unlinked(source_root), external_output(plan_path)
    require(source_root.is_dir(), "Fixture source root does not exist")
    require(not plan_path.is_relative_to(source_root), "Write the plan outside the read-only fixture root")
    validate_generated_source(source_root)
    files = []
    for name in sorted(ROOTS):
        subtree = source_root / name
        if not subtree.exists():
            continue
        require(unlinked(subtree).is_dir(), "Fixture subtree is not a directory")
        for path in sorted(tree_files(subtree)):
            relative = path.relative_to(source_root).as_posix()
            relative_parts(relative)
            size = path.stat().st_size
            # Negative model fixtures may be deliberately invalid tiny GGUFs.
            # Real model weights belong in the recorder's model workflow.
            require(path.suffix.lower() != ".gguf" or size <= 1024 * 1024,
                    "Real model files must not be copied as CU fixtures")
            files.append({"sourceRelativePath": relative, "targetRelativePath": relative,
                          "bytes": size, "sha256": sha(path), "mtimeMs": path.stat().st_mtime_ns // 1_000_000,
                          "role": "sample" if name == "samples" else "negative-or-license"})
    require(any(row["targetRelativePath"].startswith("samples/") for row in files),
            "At least one sample fixture is required")
    require(len({row["targetRelativePath"].casefold() for row in files}) == len(files),
            "Case-insensitive fixture destination collision")
    if excluded_relative is not None:
        excluded = target(source_root, excluded_relative)
        require(excluded_relative.startswith("samples/") and excluded.is_dir(),
                "Excluded fixture directory must exist inside samples")
    plan = {"schemaVersion": 2, "kind": "qcopilots-search-cu-fixture-copy-plan",
            "sourceRoot": str(source_root), "files": files,
            "excludedRelativePath": excluded_relative}
    write_json(plan_path, plan)
    return {"plan": str(plan_path), "planSha256": sha(plan_path), "files": len(files)}


def prepare(run_path, plan_path, source_root=None):
    run_path, plan_path = unlinked(run_path), unlinked(plan_path)
    require(run_path.name == "run.json", "Supply the recorder-created run.json")
    root = external_output(run_path.parent)
    state = json.loads(run_path.read_text(encoding="utf-8-sig"))
    require(state.get("schemaVersion") == 1 and state.get("kind") == "qcopilots-search-cu-run",
            "Not a recorder CU run")
    uuid.UUID(state["runId"])
    require({path.name for path in root.iterdir()} <=
            {"run.json", "inputs", "evidence", "profile", "models", "samples", "exports", "negative-models"},
            "Run already contains post-initialization artifacts")
    identity = state["identity"]
    require(re.fullmatch("[0-9a-f]{40}", identity["candidateHead"])
            and identity["candidateHead"] == identity["checkoutCommit"]
            and identity["workingTreeModified"] is False, "CU run candidate identity is not clean")
    for key, relative in (("samplesDirectory", "samples"), ("profileRoot", "profile"), ("modelDirectory", "models")):
        require(unlinked(state[key]) == unlinked(root / relative), "Recorder runtime path differs: " + key)
    for relative in ("samples", "profile", "models", "evidence", "exports"):
        empty(root / relative)
    empty(root / "negative-models", False)
    candidate_path = unlinked(root / "inputs/package-candidate.json")
    candidates = [item for item in state["inputs"] if item["path"] == "inputs/package-candidate.json"]
    require(len(candidates) == 1, "Recorder does not bind its package candidate input")
    check_file(candidate_path, candidates[0]["bytes"], candidates[0]["sha256"])
    candidate = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
    require(candidate.get("kind") == "package-candidate" and candidate["identity"] == identity
            and candidate["packageSha256"] == state["packageSha256"], "Recorder candidate provenance differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    require(plan.get("schemaVersion") == 2 and plan.get("kind") == "qcopilots-search-cu-fixture-copy-plan"
            and isinstance(plan.get("files"), list) and plan["files"], "Invalid fixture copy plan")
    source_root = unlinked(source_root if source_root is not None else plan["sourceRoot"])
    require(source_root.is_dir() and not root.is_relative_to(source_root) and not source_root.is_relative_to(root),
            "Fixture source and new run must be separate directories")
    validate_generated_source(source_root)
    excluded_relative = plan.get("excludedRelativePath")
    if excluded_relative is not None:
        require(excluded_relative.startswith("samples/") and target(source_root, excluded_relative).is_dir(),
                "Excluded fixture directory must exist inside samples")
    rows = []
    for item in plan["files"]:
        relative = item["targetRelativePath"]
        destination = target(root, relative)
        require(not destination.exists(), "Fixture destination already exists: " + str(destination))
        source = target(source_root, item["sourceRelativePath"])
        require(type(item.get("mtimeMs")) is int and item["mtimeMs"] >= 0, "Invalid file mtimeMs")
        source_record = check_file(source, item["bytes"], item["sha256"], item["mtimeMs"])
        require(source.suffix.lower() != ".gguf" or item["bytes"] <= 1024 * 1024,
                "Real model files must not be copied as CU fixtures")
        rows.append({"source": source_record, "targetPath": str(destination), "targetRelativePath": relative,
                     "expectedBytes": item["bytes"], "expectedSha256": item["sha256"],
                     "expectedMtimeMs": item["mtimeMs"], "role": item["role"]})
    require(len({row["targetRelativePath"].casefold() for row in rows}) == len(rows), "Duplicate destination names")
    require(any(row["targetRelativePath"].startswith("samples/") for row in rows), "At least one sample is required")
    return root, state, rows, excluded_relative


def materialize(run_path, plan_path, execute=False, source_root=None):
    run_sha, plan_sha = sha(unlinked(run_path)), sha(unlinked(plan_path))
    root, state, rows, excluded_relative = prepare(run_path, plan_path, source_root)
    require(sha(run_path) == run_sha and sha(plan_path) == plan_sha, "Run or source plan changed during validation")
    report = {"schemaVersion": 2, "kind": "qcopilots-search-cu-fixture-copy",
              "executed": execute, "complete": False, "runId": state["runId"], "identity": state["identity"],
              "packageSha256": state["packageSha256"], "modelFingerprint": state["modelFingerprint"],
              "extractionFingerprint": state["extractionFingerprint"], "runJsonSha256": run_sha,
              "planPath": str(Path(plan_path).resolve()), "planSha256": plan_sha, "filesToCopy": len(rows),
              "sampleRoot": str(root / "samples"),
              "excludedRoot": str(root / excluded_relative) if excluded_relative else None,
              "negativeModelDirectory": str(root / "negative-models"), "manifestRelativePath": MANIFEST,
              "files": rows, "acceptancePassed": False}
    if not execute:
        return report
    written = []
    try:
        for row in rows:
            destination = target(root, row["targetRelativePath"])
            source = Path(row["source"]["path"])
            check_file(source, row["source"]["bytes"], row["source"]["sha256"], row["source"]["mtimeMs"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            target(root, row["targetRelativePath"])
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            target(root, row["targetRelativePath"])
            os.utime(destination, ns=(destination.stat().st_atime_ns, row["expectedMtimeMs"] * 1_000_000))
            row["verifiedTarget"] = check_file(destination, row["expectedBytes"], row["expectedSha256"], row["expectedMtimeMs"])
            written.append(row["targetRelativePath"])
        require(sha(run_path) == run_sha and sha(plan_path) == plan_sha, "Run or source plan changed during copying")
        for row in rows:
            check_file(row["source"]["path"], row["source"]["bytes"], row["source"]["sha256"], row["source"]["mtimeMs"])
            check_file(row["targetPath"], row["expectedBytes"], row["expectedSha256"], row["expectedMtimeMs"])
        report.update({"complete": True, "finishedAt": datetime.now(timezone.utc).isoformat(),
                       "sourceFilesUnchanged": True, "verifiedFileCount": len(rows)})
        manifest = report_target(root, MANIFEST)
        manifest.parent.mkdir(exist_ok=True)
        report_target(root, MANIFEST)
        write_json(manifest, report)
        return {"executed": True, "complete": True, "acceptancePassed": False, "files": len(rows),
                "manifest": str(manifest), "manifestSha256": sha(manifest), "runRoot": str(root)}
    except BaseException as error:
        report.update({"complete": False, "error": type(error).__name__ + ": " + str(error), "completedFiles": written})
        try:
            failure = report_target(root, FAILURE)
            failure.parent.mkdir(exist_ok=True)
            report_target(root, FAILURE)
            write_json(failure, report)
        except (OSError, ValueError) as record_error:
            # BaseException.add_note was introduced in Python 3.11. Reporting
            # trouble must never replace the original failure on Python 3.10.
            add_note = getattr(error, "add_note", None)
            if callable(add_note):
                add_note("Could not preserve failure report: " + str(record_error))
        raise


def main():
    # Redirected Windows Python streams may otherwise use a legacy code page.
    # Every prepared set deliberately contains Chinese paths.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--source-root", type=Path, help="Fixture root, or override for a relocated plan")
    parser.add_argument("--excluded-relative", help="Optional source-relative samples directory to exclude")
    parser.add_argument("--create-plan", action="store_true")
    parser.add_argument("--prepare-source", action="store_true", help="Create a fresh external original CU source set, without models or applications")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.prepare_source:
        if args.source_root is None or args.plan is not None or args.run_json is not None or args.execute or args.create_plan or args.excluded_relative is not None:
            parser.error("--prepare-source requires only --source-root")
        result = prepare_source(args.source_root)
    elif args.create_plan:
        if args.plan is None or args.source_root is None or args.run_json is not None or args.execute:
            parser.error("--create-plan requires --source-root and forbids --run-json/--execute")
        result = create_plan(args.source_root, args.plan, args.excluded_relative)
    else:
        if args.plan is None or args.run_json is None or args.excluded_relative is not None:
            parser.error("Copying requires --run-json and forbids --excluded-relative")
        result = materialize(args.run_json, args.plan, args.execute, args.source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Fixture copy failed: " + str(error), file=sys.stderr)
        raise SystemExit(2) from error
