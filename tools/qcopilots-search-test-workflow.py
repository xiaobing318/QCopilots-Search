#!/usr/bin/env python3
"""Safety and relocation contracts for the developer workflow."""
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
SCRIPT = Path(__file__).resolve().with_name("qcopilots-search-dev.py")
spec = importlib.util.spec_from_file_location("qcopilots_search_dev", SCRIPT)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
package_spec = importlib.util.spec_from_file_location("qcopilots_search_verify_package", SCRIPT.with_name("qcopilots-search-verify-package.py"))
package_verifier = importlib.util.module_from_spec(package_spec)
package_spec.loader.exec_module(package_verifier)
response_spec = importlib.util.spec_from_file_location(
    "qcopilots_search_msvc_response", SCRIPT.with_name("qcopilots-search-msvc-response.py"))
msvc_response = importlib.util.module_from_spec(response_spec)
response_spec.loader.exec_module(msvc_response)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="qcopilots-search-workflow-", dir=configured)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)], cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", timeout=30)

    def test_source_tree_and_descendant_rejected(self):
        for path in (workflow.SOURCE, workflow.SOURCE / "nested output"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                workflow.assert_external(path)

    def test_volume_root_rejected(self):
        with self.assertRaises(RuntimeError):
            workflow.assert_external(Path(self.root.anchor))

    def test_qt_deploy_rejects_zero_exit_error_diagnostics(self):
        helper = workflow.SOURCE / "cmake/QCopilotsSearchCheckedDeploy.cmake"
        deployment = self.root / "deploy script.cmake"
        wrapper = self.root / "checked deployment.cmake"
        prefix = self.root / "安装 空格"
        wrapper.write_text(
            f'include("{helper.as_posix()}")\n'
            f'qcopilots_search_checked_deploy("{deployment.as_posix()}")\n',
            encoding="utf-8")
        cases = [
            ('message(STATUS "Deployment complete")\n', True),
            ('execute_process(COMMAND "${CMAKE_COMMAND}" -E echo "ERROR: unresolved library")\n', False),
            ('message("ERROR: unresolved library")\n', False),
            ('message(FATAL_ERROR "Tool returned a failure")\n', False),
        ]
        for program, expected in cases:
            with self.subTest(program=program):
                deployment.write_text(
                    f'if(NOT CMAKE_INSTALL_PREFIX STREQUAL "{prefix.as_posix()}" OR '
                    'NOT CMAKE_INSTALL_CONFIG_NAME STREQUAL "Release")\n'
                    'message(FATAL_ERROR "Installation context was lost")\nendif()\n' + program,
                    encoding="utf-8")
                result = subprocess.run(
                    ["cmake", f"-DCMAKE_INSTALL_PREFIX={prefix.as_posix()}",
                     "-DCMAKE_INSTALL_CONFIG_NAME=Release", "-P", str(wrapper)],
                    cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode == 0, expected, result.stdout + result.stderr)
                if not expected:
                    self.assertIn("Qt deployment failed", result.stderr)

    def test_package_failure_reports_current_generator_diagnostics(self):
        import contextlib
        import io
        import time
        current = self.root / "packages/_CPack_Packages/win64/NSIS/NSISOutput.log"
        current.parent.mkdir(parents=True)
        current.write_text("LicenseData: unable to open 中文路径", encoding="utf-8")
        stale = current.parent.parent / "ZIP/CPackError.log"
        stale.parent.mkdir()
        stale.write_text("stale package failure", encoding="utf-8")
        os.utime(stale, (1, 1))
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            workflow.report_package_errors(self.root, time.time())
        self.assertIn("LicenseData: unable to open 中文路径", output.getvalue())
        self.assertNotIn("stale package failure", output.getvalue())
        self.assertTrue(current.is_file())

    def test_cpack_matches_full_install_before_final_bundle_hook(self):
        import zipfile
        project = self.root / "package source"
        project.mkdir()
        for name in ("application.txt", "runtime.txt", "internal-export.cmake"):
            (project / name).write_text(name, encoding="utf-8")
        packaging = workflow.SOURCE / "cmake/QCopilotsSearchCPackConfig.cmake"
        # DragNDrop installs components by default. Force that behavior for the
        # portable ZIP fixture so the real project policy must suppress it.
        (project / "CMakeLists.txt").write_text('''
cmake_minimum_required(VERSION 3.28)
project(qcopilots-search-package-layout VERSION 1.0 LANGUAGES NONE)
set(bundle "qcopilots-search.app/Contents")
install(FILES application.txt DESTINATION "${bundle}/MacOS")
install(FILES runtime.txt DESTINATION "${bundle}/Resources"
  COMPONENT qcopilots-search-runtime)
install(FILES internal-export.cmake DESTINATION "${bundle}/Frameworks/cmake"
  EXCLUDE_FROM_ALL)
install(CODE [[
  set(bundle "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/qcopilots-search.app/Contents")
  foreach(resource MacOS/application.txt Resources/runtime.txt)
    if(NOT EXISTS "${bundle}/${resource}")
      message(FATAL_ERROR "Final bundle hook ran before runtime installation: ${resource}")
    endif()
  endforeach()
  if(EXISTS "${bundle}/Frameworks/cmake/internal-export.cmake")
    message(FATAL_ERROR "Internal developer metadata entered the runtime bundle")
  endif()
  file(WRITE "${bundle}/Resources/final-hook.txt" "all runtime resources installed")
]])
set(CPACK_GENERATOR ZIP)
set(CPACK_PACKAGE_FILE_NAME qcopilots-search-layout)
set(CPACK_ARCHIVE_COMPONENT_INSTALL ON)
set(CPACK_COMPONENTS_GROUPING ALL_COMPONENTS_IN_ONE)
set(CPACK_PROJECT_CONFIG_FILE "''' + packaging.as_posix() + '''")
include(CPack)
''', encoding="utf-8")
        build, stage = self.root / "package build", self.root / "package stage"
        for command in (["cmake", "-S", str(project), "-B", str(build), "-G", "Ninja"],
                        ["cmake", "--install", str(build), "--prefix", str(stage)],
                        ["cpack", "--config", str(build / "CPackConfig.cmake"), "-G", "ZIP"]):
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((stage / "qcopilots-search.app/Contents/Resources/final-hook.txt").is_file())
        self.assertFalse(list(stage.rglob("*.cmake")))
        with zipfile.ZipFile(self.root / "qcopilots-search-layout.zip") as archive:
            names = archive.namelist()
            for suffix in ("MacOS/application.txt", "Resources/runtime.txt", "Resources/final-hook.txt"):
                self.assertEqual(len([name for name in names if name.endswith(suffix)]), 1)
            self.assertFalse(any(name.endswith(".cmake") for name in names))

    def test_distribution_archive_traversal_rejected_before_write(self):
        import zipfile
        package = self.root / "qcopilots-search-test.zip"
        for member in ("../escaped.txt", "C:/escaped.txt", "/escaped.txt", "nested/../../escaped.txt"):
            with self.subTest(member=member):
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr(member, "must not be written")
                destination = self.root / ("unpack-" + str(len(list(self.root.iterdir()))))
                with self.assertRaises(RuntimeError):
                    package_verifier.unpack(package, destination)
                self.assertFalse((self.root / "escaped.txt").exists())

    def test_release_package_requires_models_before_launching(self):
        import contextlib
        import io
        report = self.root / "missing-model-report"
        output = io.StringIO()
        with patch.object(package_verifier.subprocess, "Popen") as launch, \
                patch.object(package_verifier.shutil, "which", return_value=None), \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "requires --model-dir"):
                package_verifier.verify(None, self.root / "build", report, self.root / "installed")
        launch.assert_not_called()
        evidence = json.loads((report / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(output.getvalue()), evidence)
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["releaseComplete"])
        self.assertTrue(evidence["realModelRequired"])
        self.assertEqual(evidence["verificationScope"], "release")

    def test_jpeg_contract_uses_consumer_build_directory_and_header_order(self):
        project = self.root / "源码 source"
        vendor = self.root / "依赖 sources"
        build = self.root / "构建 output"
        project.mkdir()
        vendor.mkdir()
        for name in ("qcopilots-search-native-images.cmake", "qcopilots-search-jpeg-contract.c"):
            shutil.copyfile(workflow.SOURCE / "cmake" / name, project / name)
        (project / "cmake").mkdir()
        (project / "tools").mkdir()
        shutil.copyfile(workflow.SOURCE / "cmake/QCopilotsSearchMsvcResponse.cmake",
                        project / "cmake/QCopilotsSearchMsvcResponse.cmake")
        shutil.copyfile(SCRIPT.with_name("qcopilots-search-msvc-response.py"),
                        project / "tools/qcopilots-search-msvc-response.py")
        python = Path(sys.executable)
        if os.name == "nt":
            import venv
            interpreter_root = self.root / "Python 空格"
            venv.EnvBuilder(with_pip=False).create(interpreter_root)
            python = interpreter_root / "Scripts/python.exe"
        (vendor / "unit.cpp").write_text(
            'extern "C"\n#ifdef _WIN32\n__declspec(dllexport)\n#endif\n'
            "int fixture_value() { return 42; }\n", encoding="utf-8")
        (vendor / "export.c").write_text(
            "#ifdef _WIN32\n__declspec(dllexport)\n#endif\nint fixture_value(void) { return 42; }\n",
            encoding="utf-8")
        (vendor / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        # Enough objects exercise an actual MSVC link response file, without
        # rebuilding the full codecs. Every generated file stays in this run.
        for index in range(100):
            (vendor / f"unit{index:03}.c").write_text(
                f"int fixture_{index}(void) {{ return {index}; }}\n", encoding="utf-8")
        (vendor / "CMakeLists.txt").write_text('''
project(qcopilots-search-codec-fixture LANGUAGES C CXX)
file(GLOB units "${CMAKE_CURRENT_SOURCE_DIR}/unit*.c")
add_library(qcopilots-search-contract-objects OBJECT ${units})
set(objects "$<TARGET_OBJECTS:qcopilots-search-contract-objects>")
add_library(qcopilots-search-contract-consumer SHARED unit.cpp ${objects})
target_link_libraries(qcopilots-search-contract-consumer PRIVATE jpeg-headers)
qcopilots_search_add_jpeg_contract(qcopilots-search-contract-consumer)
add_library(qcopilots-search-contract-c SHARED export.c ${objects})
target_link_libraries(qcopilots-search-contract-c PRIVATE jpeg-headers)
qcopilots_search_add_jpeg_contract(qcopilots-search-contract-c)
add_executable(qcopilots-search-contract-exe main.c ${objects})
foreach(kind IN ITEMS c cpp)
  if(kind STREQUAL "c")
    set(source export.c)
  else()
    set(source unit.cpp)
  endif()
  add_library(qcopilots-search-archive-${kind} STATIC ${source} ${objects})
  if(MSVC)
    add_library(qcopilots-search-archive-${kind}-ipo STATIC ${source} ${objects})
    set_property(TARGET qcopilots-search-archive-${kind}-ipo PROPERTY INTERPROCEDURAL_OPTIMIZATION TRUE)
  endif()
endforeach()
''', encoding="utf-8")
        (project / "CMakeLists.txt").write_text('''
cmake_minimum_required(VERSION 3.28)
project(qcopilots-search-jpeg-contract-test LANGUAGES C CXX)
include("${CMAKE_CURRENT_SOURCE_DIR}/qcopilots-search-native-images.cmake")
set(CMAKE_C_LINKER_LAUNCHER "${CMAKE_COMMAND};-E;env")
set(CMAKE_CXX_LINKER_LAUNCHER "${CMAKE_COMMAND};-E;env")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/QCopilotsSearchMsvcResponse.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/QCopilotsSearchMsvcResponse.cmake")
add_library(jpeg-headers INTERFACE)
target_include_directories(jpeg-headers INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}")
add_subdirectory("${VENDOR_SOURCE}" "${QCOPILOTS_SEARCH_NATIVE_BINARY_ROOT}/consumer")
''', encoding="utf-8")
        for layout in ("default", "external"):
            with self.subTest(layout=layout):
                current_build = build / layout
                native = (current_build / "native-dependencies" if layout == "default"
                          else self.root / "独立 native output")
                (project / "jpeglib.h").write_text(
                    "#define JPEG_LIB_VERSION 62\n#define LIBJPEG_TURBO_VERSION_NUMBER 3001004\n",
                    encoding="utf-8")
                configure_command = ["cmake", "-S", str(project), "-B", str(current_build), "-G", "Ninja",
                     "-DCMAKE_BUILD_TYPE=Release", f"-DVENDOR_SOURCE={vendor.as_posix()}",
                     f"-DQCOPILOTS_SEARCH_NATIVE_BINARY_ROOT={native.as_posix()}",
                     f"-DPython3_EXECUTABLE={python}"]
                for _ in range(2):
                    configure = subprocess.run(configure_command, cwd=self.root, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=60)
                    self.assertEqual(configure.returncode, 0, configure.stdout + configure.stderr)
                copied = (native / "consumer/qcopilots-search-contracts"
                          / "qcopilots-search-contract-consumer/qcopilots-search-jpeg-contract.c")
                self.assertEqual(copied.read_bytes(), (project / "qcopilots-search-jpeg-contract.c").read_bytes())
                command = ["cmake", "--build", str(current_build), "--parallel", "2", "--", "-d", "keeprsp"]
                compiled = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", timeout=120)
                self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                application = native / "consumer" / ("qcopilots-search-contract-exe" + (".exe" if os.name == "nt" else ""))
                launched = subprocess.run([str(application)], cwd=self.root, capture_output=True, timeout=30)
                self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
                if os.name == "nt":
                    response = current_build / "CMakeFiles/qcopilots-search-contract-consumer.rsp"
                    self.assertTrue(response.is_file(), "The Windows regression must exercise the linker response file")
                    self.assertIn("qcopilots-search-jpeg-contract.c.obj", response.read_text(encoding="utf-8"))
                    self.assertNotIn("源码", response.read_text(encoding="utf-8"))
                    for kind in ("c", "cpp", "c-ipo", "cpp-ipo"):
                        self.assertTrue((native / "consumer" / f"qcopilots-search-archive-{kind}.lib").is_file())
                    rules = (current_build / "CMakeFiles/rules.ninja").read_text(encoding="utf-8")
                    launcher_lines = [line for line in rules.splitlines()
                                      if "command =" in line and "qcopilots-search-msvc-response.py" in line]
                    self.assertTrue(launcher_lines)
                    self.assertTrue(all(line.count("qcopilots-search-msvc-response.py") == 1 for line in launcher_lines))
                    self.assertEqual(list(current_build.rglob("qcopilots-search-msvc-*.rsp")), [])
                # The copied unit must still use the actual consumer headers.
                (project / "jpeglib.h").write_text(
                    "#define JPEG_LIB_VERSION 80\n#define LIBJPEG_TURBO_VERSION_NUMBER 3001004\n",
                    encoding="utf-8")
                rejected = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", timeout=120)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("requires the pinned libjpeg API 62", rejected.stdout + rejected.stderr)

    def test_msvc_response_encoding_exit_cleanup_and_parallel_calls(self):
        import codecs
        import concurrent.futures
        import threading
        directory = self.root / "响应 文件"
        directory.mkdir()
        content = '"路径 with space\\one.obj"\n"quoted\\two.obj"\r\n/LIBPATH:"a b"\n'
        responses = []
        for name, encoded in (
            ("utf8", content.encode("utf-8")),
            ("utf8-bom", codecs.BOM_UTF8 + content.encode("utf-8")),
            ("utf16-le", codecs.BOM_UTF16_LE + content.encode("utf-16-le")),
            ("utf16-be", codecs.BOM_UTF16_BE + content.encode("utf-16-be")),
        ):
            path = directory / (name + " 空格.rsp")
            path.write_bytes(encoded)
            responses.append(path)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in responses]
        arguments = ["tool", 'untouched "quotes"', *("@" + str(path) for path in responses)]
        with msvc_response.response_arguments(arguments) as converted:
            self.assertEqual(converted[:2], arguments[:2])
            replacement = Path(converted[2][1:])
            self.assertNotEqual(replacement, responses[0])
            self.assertEqual(replacement.read_bytes(), codecs.BOM_UTF16_LE + content.encode("utf-16-le"))
            self.assertEqual(converted[3:], arguments[3:])
        self.assertFalse(replacement.exists())
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in responses])
        with msvc_response.response_arguments(["tool", "@" + str(responses[0]), "@" + str(responses[0])]) as copied:
            self.assertNotEqual(copied[1], copied[2])
        for code in (0, 23):
            self.assertEqual(msvc_response.main(
                [sys.executable, "-c", f"import sys; sys.exit({code})", "@" + str(responses[0])]), code)
            self.assertEqual(list(directory.glob("qcopilots-search-msvc-*.rsp")), [])
        with self.assertRaises(OSError):
            msvc_response.main([str(directory / "missing-tool"), "@" + str(responses[0])])
        malformed = directory / "not-utf8.rsp"
        malformed.write_bytes(b"\xffinvalid")
        with self.assertRaises(UnicodeError), patch.object(msvc_response.subprocess, "call") as invoke:
            msvc_response.main(["tool", "@" + str(responses[0]), "@" + str(malformed)])
        invoke.assert_not_called()
        self.assertEqual(list(directory.glob("qcopilots-search-msvc-*.rsp")), [])
        barrier = threading.Barrier(4)
        def concurrent_copy(_):
            with msvc_response.response_arguments(["tool", "@" + str(responses[0])]) as command:
                temporary = Path(command[1][1:])
                barrier.wait(timeout=10)
                self.assertTrue(temporary.is_file())
                return temporary
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            copies = list(executor.map(concurrent_copy, range(4)))
        self.assertEqual(len(set(copies)), 4)
        self.assertTrue(all(not path.exists() for path in copies))
        self.assertEqual(list(directory.glob("qcopilots-search-msvc-*.rsp")), [])

    def test_license_inventory_cannot_be_omitted_or_satisfied_by_unrelated_files(self):
        binary = self.root / "app/bin"
        binary.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "complete runtime license manifest"):
            package_verifier.verify_license_materials(binary, self.root / "app", self.root / "reports")
        materials = binary.parent / "share/qcopilots-search/licenses/qcopilots-search-runtime"
        materials.mkdir(parents=True)
        (materials / "qcopilots-search-license-manifest.json").write_text(
            json.dumps({"schemaVersion": 1, "kind": "runtime-license-materials", "files": []}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version/modules"):
            package_verifier.verify_license_materials(binary, self.root / "app", self.root / "reports")
        self.assertFalse((self.root / "reports/runtime-license-materials").exists())

    def test_dev_forwards_explicit_models_or_partial_scope(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                models = self.root / "模型 cache"
                arguments = [str(SCRIPT), "verify-package", "--build-root", str(self.root / "output"),
                             "--model-dir", str(models), *(["--keyword-only"] if partial else [])]
                with patch.object(sys, "argv", arguments), patch.object(workflow, "run") as run:
                    workflow.main()
                command = list(map(str, run.call_args.args[0]))
                self.assertEqual("--keyword-only" in command, partial)
                self.assertEqual("--model-dir" in command, not partial)
                if not partial:
                    self.assertEqual(command[command.index("--model-dir") + 1], str(models.resolve()))

    def test_real_model_manifest_rejects_changed_missing_or_duplicate_artifacts(self):
        import copy
        result = {"valid": True, "directory": str(self.root), "artifacts": [
            {"name": name, "present": True, "bytes": size, "expectedBytes": size, "sha256": digest}
            for name, (size, digest) in package_verifier.MODEL_ARTIFACTS.items()]}
        self.assertEqual(package_verifier.model_verification(result, self.root), result)
        mutations = [lambda value: value.update(valid=False),
                     lambda value: value["artifacts"].pop(),
                     lambda value: value["artifacts"][0].update(sha256="0" * 64),
                     lambda value: value["artifacts"][0].update(bytes=1),
                     lambda value: value["artifacts"].__setitem__(1, value["artifacts"][0]),
                     lambda value: value.update(directory=str(self.root / "elsewhere"))]
        for mutate in mutations:
            changed = copy.deepcopy(result)
            mutate(changed)
            with self.assertRaises(RuntimeError):
                package_verifier.model_verification(changed, self.root)

    def test_packaged_vector_requires_dimensions_finite_l2_and_old_reference(self):
        reference = [1.0] + [0.0] * 767
        self.assertEqual(package_verifier.validate_vector(reference, reference)["cosine"], 1.0)
        for actual in (reference[:-1], [False] + reference[1:], [math.nan] + reference[1:],
                       [math.inf] + reference[1:], [0.0] * 768, [2.0] + reference[1:],
                       [0.0, 1.0] + [0.0] * 766):
            with self.subTest(first=actual[:2]), self.assertRaises(RuntimeError):
                package_verifier.validate_vector(actual, reference)

    def test_model_smoke_uses_unmodified_reference_inputs(self):
        destination = self.root / "参考 vectors"
        vectors = package_verifier.reference_inputs(destination)
        self.assertEqual(set(vectors), {"query", "query-bn", "query-ar", "document", "document-long",
                                       "screen-280", "photo-140", "frame-70", "speech"})
        self.assertEqual(package_verifier.file_digest(destination / "Screenshot golden.png"),
                         package_verifier.REFERENCE_HASHES["screen.png"])
        self.assertFalse((destination / "screen.png").exists())
        with patch.object(package_verifier, "REFERENCE_HASHES", {"screen.png": "0" * 64}), self.assertRaises(RuntimeError):
            package_verifier.reference_inputs(self.root / "tampered-input")

    def test_macos_library_inventory_requires_mapped_text_descriptor(self):
        output = "p100\0\nftxt\0n/app/Contents/Frameworks/libllama.dylib\0\nf7\0n/sdk/libmtmd.dylib\0\nftxt\0n/app/Contents/MacOS/qcopilots-search-worker\0"
        self.assertEqual(package_verifier.mapped_macos_paths(output), ["/app/Contents/Frameworks/libllama.dylib",
                          "/app/Contents/MacOS/qcopilots-search-worker"])
        with self.assertRaisesRegex(RuntimeError, "enumerate all mapped"):
            package_verifier.mapped_macos_paths(output + "\nferr\0nregion info error\0")

    def test_inference_library_roles_do_not_accept_similarly_named_files(self):
        expected = {"llama.dll": "llama", "libmtmd.0.dylib": "mtmd", "libggml.so.0": "ggml",
                    "ggml-base.dll": "ggml-base", "libggml-cpu-x64.so": "cpu-backend"}
        for name, role in expected.items():
            self.assertEqual(package_verifier.inference_library_role(name), role)
        for name in ("llama.dll.txt", "libggml-extra.so", "mtmd.json", "Qt6Core.dll"):
            self.assertIsNone(package_verifier.inference_library_role(name))

    def test_inference_origins_require_all_dynamic_roles_and_package_membership(self):
        application = self.root / "app"
        binary = application / "bin"
        binary.mkdir(parents=True)
        worker = binary / "qcopilots-search-worker"
        worker.write_bytes(b"owned worker fixture")
        files = []
        for name in ("libllama.so", "libmtmd.so", "libggml.so", "libggml-base.so", "libggml-cpu-x64.so"):
            path = binary / name
            path.write_bytes(name.encode("ascii"))
            files.append(str(path))
        with patch.object(package_verifier.platform, "machine", return_value="x86_64"), \
                patch.object(package_verifier.sys, "platform", "linux"), \
                patch.object(package_verifier, "loaded_libraries", return_value=files):
            inventory = package_verifier.inference_runtime_origins(None, worker, application)
            self.assertEqual(len(inventory["libraries"]), 5)
            self.assertEqual(inventory["linkage"], "shared-cpu-dispatch")
            for missing in files:
                with patch.object(package_verifier, "loaded_libraries", return_value=[path for path in files if path != missing]), self.assertRaises(RuntimeError):
                    package_verifier.inference_runtime_origins(None, worker, application)
            with self.assertRaises(RuntimeError):
                package_verifier.inference_runtime_origins(None, worker, application, {worker.resolve()})
            external = self.root / "SDK/libllama.so"
            external.parent.mkdir()
            external.write_bytes(b"external dependency")
            with patch.object(package_verifier, "loaded_libraries", return_value=[*files, str(external)]), self.assertRaises(RuntimeError):
                package_verifier.inference_runtime_origins(None, worker, application)

    def test_arm_static_runtime_still_requires_owned_worker_and_qt_origins(self):
        application = self.root / "app"
        binary = application / "Contents/MacOS"
        binary.mkdir(parents=True)
        worker = binary / "qcopilots-search-worker"
        worker.write_bytes(b"static inference worker fixture")
        qt = application / "Contents/Frameworks/QtNetwork.framework/Versions/A/QtNetwork"
        qt.parent.mkdir(parents=True)
        qt.write_bytes(b"Qt framework fixture")
        with patch.object(package_verifier.platform, "machine", return_value="arm64"), \
                patch.object(package_verifier.sys, "platform", "darwin"), \
                patch.object(package_verifier, "loaded_libraries", return_value=[str(qt)]):
            inventory = package_verifier.inference_runtime_origins(None, worker, application)
            self.assertEqual(inventory["linkage"], "static-cpu")
            self.assertEqual(inventory["libraries"], [])
            with self.assertRaises(RuntimeError):
                package_verifier.inference_runtime_origins(None, self.root / "external-worker", application)

    def test_worker_protocol_rejects_wrong_ids_eof_and_nonzero_shutdown(self):
        import io
        import queue
        from unittest.mock import Mock
        worker = Mock()
        worker.stdin = io.StringIO()
        replies = queue.Queue()
        replies.put(json.dumps({"id": "wrong", "ok": True, "result": {}}))
        with self.assertRaises(RuntimeError):
            package_verifier.worker_request(worker, replies, {"id": "expected", "operation": "query"}, self.root)
        replies.put(None)
        with self.assertRaisesRegex(RuntimeError, "without a response"):
            package_verifier.worker_request(worker, replies, {"id": "expected", "operation": "query"}, self.root)
        worker.poll.return_value = None
        worker.returncode = 3
        reader = Mock()
        reader.is_alive.return_value = False
        replies.put(json.dumps({"id": "worker-shutdown", "ok": True, "result": {"stopping": True}}))
        with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
            package_verifier.stop_worker(worker, replies, reader, self.root)

    @unittest.skipUnless(os.name == "nt", "NSIS is native to Windows")
    def test_nsis_compiles_recursive_unicode_payload_beyond_max_path(self):
        compiler = shutil.which("makensis")
        self.assertIsNotNone(compiler, "NSIS compiler missing: run bootstrap before Windows CTest")
        template = (workflow.SOURCE / "cmake/nsis/NSIS.template.in").read_text(encoding="utf-8")
        macro = template[template.index("!macro QCOPILOTS_SEARCH_COMPILER_PATH"):
                         template.index("!macroend") + len("!macroend")]
        guard_start = template.index("!macro QCOPILOTS_SEARCH_REQUIRE_WRITE_SUCCESS")
        guard = template[guard_start:template.index("!macroend", guard_start) + len("!macroend")]
        payload = self.root / "编译输入 空格"
        while len(str(payload)) < 290:
            payload /= "long-payload-segment"
        license = payload / "share/qcopilots-search/licenses/qt/third-party/许可声明.txt"
        license.parent.mkdir(parents=True)
        content = "Real recursive license payload 中文\n"
        license.write_text(content, encoding="utf-8")
        output = payload.parent / "installer-output.exe"
        cases = [
            ("C:/外置 build/payload", r"\\?\C:\外置 build\payload"),
            ("//server/share/许可 payload", r"\\?\UNC\server\share\许可 payload"),
            (r"\\?\C:\already long", r"\\?\C:\already long"),
            (r"\\?\UNC\server\share\already long", r"\\?\UNC\server\share\already long"),
        ]
        manifest_option = next(line for line in template.splitlines()
                               if line.startswith("ManifestLongPathAware "))
        program = ["Unicode true", manifest_option, macro, guard]
        for index, (value, expected) in enumerate(cases):
            program += [f'!insertmacro QCOPILOTS_SEARCH_COMPILER_PATH P{index} "{value}"',
                        f'!assert "${{P{index}}}" S== "{expected}" "Path conversion failed"']
        program += [f'!insertmacro QCOPILOTS_SEARCH_COMPILER_PATH PAYLOAD "{payload.as_posix()}"',
                    f'!insertmacro QCOPILOTS_SEARCH_COMPILER_PATH OUTPUT "{output.as_posix()}"',
                    f'!insertmacro QCOPILOTS_SEARCH_COMPILER_PATH LICENSE "{license.as_posix()}"',
                    'Name "QCopilots Search compiler path fixture"', 'OutFile "${OUTPUT}"',
                    'RequestExecutionLevel user', 'LicenseData "${LICENSE}"',
                    'Section', 'ClearErrors', 'SetOutPath "$INSTDIR"', 'File /r "${PAYLOAD}\\*.*"',
                    '!insertmacro QCOPILOTS_SEARCH_REQUIRE_WRITE_SUCCESS',
                    'FileOpen $0 "$INSTDIR\\completed.txt" w', 'FileWrite $0 "completed"',
                    'FileClose $0', 'SectionEnd']
        script = self.root / "compiler-path.nsi"
        script.write_text("\n".join(program) + "\n", encoding="utf-8")
        # Feed the script on stdin so this regression concerns compiler file
        # paths even when the parent test root itself exceeds MAX_PATH.
        result = subprocess.run([compiler, "/INPUTCHARSET", "UTF8", "/NOCD", "-"],
                                input=script.read_text(encoding="utf-8"),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(output.stat().st_size, 1000)
        self.assertEqual(license.read_text(encoding="utf-8"), content)
        destination = self.root / "安装目标 空格"
        while len(str(destination)) < 230:
            destination /= "long-target-segment"
        executable = str(output.resolve())
        if executable.startswith("\\\\?\\"):
            pass
        elif executable.startswith("\\\\"):
            executable = "\\\\?\\UNC\\" + executable[2:]
        else:
            executable = "\\\\?\\" + executable
        # An explicit executable allows CreateProcessW to launch the long
        # output filename. NSIS requires the final /D value to be unquoted.
        installed = subprocess.run('"qcopilots-search-path-fixture.exe" /S /D=' + str(destination),
                                   executable=executable, capture_output=True, timeout=30)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        received = destination / license.relative_to(payload)
        self.assertGreater(len(str(received)), 259)
        self.assertEqual(received.read_text(encoding="utf-8"), content)
        self.assertEqual((destination / "completed.txt").read_text(), "completed")
        failed_destination = destination.parent / (destination.name + "-write-failure")
        (failed_destination / license.relative_to(payload)).mkdir(parents=True)
        failed = subprocess.run('"qcopilots-search-path-fixture.exe" /S /D=' + str(failed_destination),
                                executable=executable, capture_output=True, timeout=30)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertFalse((failed_destination / "completed.txt").exists())

    @unittest.skipUnless(os.name == "nt", "NSIS is native to Windows")
    def test_installer_guard_rejects_existing_start_menu_before_execution(self):
        from unittest.mock import patch
        appdata = self.root / "test AppData"
        (appdata / "Microsoft/Windows/Start Menu/Programs/QCopilots Search").mkdir(parents=True)
        with patch.dict(os.environ, {"APPDATA": str(appdata)}), self.assertRaises(RuntimeError):
            package_verifier.NsisInstallation(self.root / "installer.exe", self.root / "install")
        self.assertFalse((self.root / "install").exists())

    def test_two_checkouts_cannot_share_build_root(self):
        build = self.root / "build"
        workflow.initialize_root(build)
        (build / workflow.MARKER).write_text(json.dumps({"source": str(self.root / "other checkout")}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            workflow.initialize_root(build)

    def test_help_from_arbitrary_working_directory(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--build-root", result.stdout)

    def test_clean_preserves_unowned_directory(self):
        build = self.root / "build 中文 空间"
        release = build / "release"
        release.mkdir(parents=True)
        sentinel = release / "user-data.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        result = self.invoke("clean", "--build-root", build)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_clean_removes_only_owned_configuration(self):
        build = self.root / "owned output"
        release = build / "release"
        release.mkdir(parents=True)
        (release / "CMakeCache.txt").write_text("CMAKE_HOME_DIRECTORY:INTERNAL=" + workflow.SOURCE.as_posix() + "\n", encoding="utf-8")
        preserved = build / "development-profile"
        preserved.mkdir()
        result = self.invoke("clean", "--build-root", build)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(release.exists())
        self.assertTrue(preserved.is_dir())

    def test_disk_guard_reports_failure(self):
        with self.assertRaises(RuntimeError):
            workflow.disk_guard(self.root, 1e12)

    def test_fail_fast_stops_ctest_without_hiding_failure(self):
        import shutil
        import xml.etree.ElementTree as ET
        checkout = self.root / "测试源码 source"
        tools = checkout / "tools"
        tools.mkdir(parents=True)
        shutil.copy2(SCRIPT, tools / SCRIPT.name)
        (checkout / "CMakeLists.txt").write_text('''
cmake_minimum_required(VERSION 3.28)
project(qcopilots-search-fail-fast-test LANGUAGES NONE)
enable_testing()
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/fail.cmake"
  "message(FATAL_ERROR \\\"intentional fixture failure\\\")\\n")
add_test(NAME qcopilots-search-tests-first-failure
  COMMAND "${CMAKE_COMMAND}" -P "${CMAKE_CURRENT_BINARY_DIR}/fail.cmake")
add_test(NAME qcopilots-search-tests-second-marker
  COMMAND "${CMAKE_COMMAND}" -E touch "${CMAKE_CURRENT_BINARY_DIR}/second-ran.txt")
''', encoding="utf-8")
        build = self.root / "test build"
        command = [sys.executable, str(tools / SCRIPT.name)]
        result = subprocess.run([*command, "configure", "--build-root", str(build)],
                                cwd=self.root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = build / "release/second-ran.txt"
        for fail_fast in (False, True):
            with self.subTest(fail_fast=fail_fast):
                marker.unlink(missing_ok=True)
                report = self.root / ("fail-fast" if fail_fast else "complete-tests")
                result = subprocess.run(
                    [*command, "test", "--build-root", str(build), "--jobs", "1",
                     "--report-dir", str(report), *(["--fail-fast"] if fail_fast else [])],
                    cwd=self.root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("intentional fixture failure", result.stdout + result.stderr)
                self.assertEqual(marker.exists(), not fail_fast)
                cases = ET.parse(report / "ctest.xml").findall("testcase")
                self.assertEqual(len(cases), 1 if fail_fast else 2)
                self.assertEqual(len([case for case in cases if case.find("failure") is not None]), 1)

    def test_qt_sdk_relocation_with_unicode(self):
        sdk = self.root / "SDK 中文 空格"
        kit = {"Windows": "msvc2022_64", "Darwin": "macos", "Linux": "gcc_64"}[workflow.platform.system()]
        prefix = sdk / "6.12.0" / kit
        cmake = prefix / "lib/cmake/Qt6"
        cmake.mkdir(parents=True)
        (cmake / "Qt6Config.cmake").write_text("# fixture", encoding="utf-8")
        self.assertEqual(workflow.qt_prefix(sdk), prefix.resolve())
        self.assertEqual(workflow.qt_prefix(prefix), prefix.resolve())

    def test_unicode_diagnostic_works_with_redirected_cp1252_environment(self):
        build = self.root / "构建 中文 空格"
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        result = subprocess.run([sys.executable, str(SCRIPT), "doctor", "--build-root", str(build),
                                 "--qt-root", str(self.root / "不存在的 SDK")], cwd=self.root,
                                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("构建 中文 空格", result.stdout)
        self.assertNotIn("UnicodeEncodeError", result.stderr)

    def test_relocated_checkout_configure_and_clean_with_unicode(self):
        # A language-free CMake fixture exercises the real wrappers and CMake
        # path handling without duplicating the application's native build.
        checkout = self.root / "源码 中文 空格"
        tools = checkout / "tools"
        tools.mkdir(parents=True)
        import shutil
        shutil.copy2(SCRIPT, tools / SCRIPT.name)
        (checkout / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.28)\nproject(qcopilots-search-relocation-test NONE)\n", encoding="utf-8")
        environment = os.environ.copy()
        environment["QCOPILOTS_SEARCH_PYTHON"] = sys.executable
        if os.name == "nt":
            wrapper = checkout / "qcopilots-search-dev.ps1"
            shutil.copy2(workflow.SOURCE / wrapper.name, wrapper)
            command = [shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
        else:
            wrapper = checkout / "qcopilots-search-dev.sh"
            shutil.copy2(workflow.SOURCE / wrapper.name, wrapper)
            command = ["sh", str(wrapper)]
        build = self.root / "构建 output"
        result = subprocess.run([*command, "configure", "--build-root", str(build), "--qt-root", str(self.root / "SDK 空格")],
                                cwd=self.root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(checkout), result.stdout)
        self.assertTrue((build / "release/CMakeCache.txt").is_file())
        result = subprocess.run([sys.executable, str(tools / SCRIPT.name), "clean", "--build-root", str(build)],
                                cwd=self.root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((build / "release").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
