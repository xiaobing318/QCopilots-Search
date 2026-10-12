#!/usr/bin/env python3
"""Exercise a real distribution archive after relocation without the developer SDK."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import wave
import zipfile

PRODUCT = "qcopilots-search"
WINDOWS_PRODUCT_KEY = r"Software\QCopilots\qcopilots-search"
WINDOWS_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\qcopilots-search"


class NsisInstallation:
    """Only exercise the real installer when it cannot replace a user install."""
    def __init__(self, package, destination):
        self.mutex = None
        if os.name != "nt":
            raise RuntimeError("NSIS installation can only be verified on Windows")
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = self.kernel.CreateMutexW(None, False, r"Local\qcopilots-search-package-installer-tests")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.kernel.WaitForSingleObject(handle, 0) not in (0, 0x80):
            self.kernel.CloseHandle(handle)
            raise RuntimeError("Another NSIS package test is active in this Windows session")
        self.mutex = handle
        import winreg
        self.registry = winreg
        self.destination = destination.resolve()
        self.started = False
        self.start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/QCopilots Search"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                for key in (WINDOWS_PRODUCT_KEY, WINDOWS_UNINSTALL_KEY):
                    try:
                        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view):
                            raise RuntimeError("An existing QCopilots Search installation was found. Run installer verification in a clean Windows account or CI runner.")
                    except FileNotFoundError:
                        pass
        if self.start_menu.exists() or self.destination.exists():
            raise RuntimeError("Installer verification refuses an existing install or Start Menu directory")
        self.package = package

    def close(self):
        if self.mutex:
            self.kernel.ReleaseMutex(self.mutex)
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None

    def __del__(self):
        self.close()

    def install(self):
        self.started = True
        # NSIS requires /D as the last argument, without quotes even with spaces.
        command = subprocess.list2cmdline([str(self.package), "/S"]) + " /D=" + str(self.destination)
        subprocess.run(command, check=True, timeout=180)
        with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, WINDOWS_UNINSTALL_KEY) as key:
            location = self.registry.QueryValueEx(key, "InstallLocation")[0]
        if Path(location).resolve() != self.destination:
            raise RuntimeError(f"Installer ignored the isolated destination: {location}")

    def uninstall(self):
        if not self.started:
            return
        uninstaller = self.destination / "Uninstall.exe"
        if not uninstaller.is_file():
            raise RuntimeError("Installer did not leave its owned uninstaller for cleanup")
        # _?= runs this exact uninstaller synchronously rather than spawning a
        # temporary copy. Its executable is the only file we remove afterward.
        command = subprocess.list2cmdline([str(uninstaller), "/S"]) + " _?=" + str(self.destination)
        subprocess.run(command, check=True, timeout=120)
        for key_name in (WINDOWS_PRODUCT_KEY, WINDOWS_UNINSTALL_KEY):
            try:
                with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, key_name):
                    raise RuntimeError(f"Uninstaller retained its registration: {key_name}")
            except FileNotFoundError:
                pass
        if self.start_menu.exists():
            raise RuntimeError("Uninstaller retained its Start Menu directory")
        remaining = [path for path in self.destination.rglob("*") if path.is_file() and path != uninstaller]
        if remaining:
            raise RuntimeError(f"Uninstaller retained installed payload: {remaining[:5]}")
        if uninstaller.exists():
            uninstaller.unlink()



def loaded_libraries(process):
    """Inspect our child process, so SDK fallback paths cannot hide missing files."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        psapi.EnumProcessModules.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        psapi.GetModuleFileNameExW.argtypes = (wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, process.pid)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        try:
            modules = (wintypes.HMODULE * 2048)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcessModules(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            paths = []
            for index in range(min(len(modules), needed.value // ctypes.sizeof(wintypes.HMODULE))):
                buffer = ctypes.create_unicode_buffer(32768)
                if psapi.GetModuleFileNameExW(handle, modules[index], buffer, len(buffer)):
                    paths.append(buffer.value)
            return paths
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        return sorted({line.split(maxsplit=5)[-1].strip() for line in Path(f"/proc/{process.pid}/maps").read_text().splitlines() if "/" in line})
    result = subprocess.run(["/usr/sbin/lsof", "-p", str(process.pid), "-Fn"], capture_output=True, text=True, check=True)
    return [line[1:] for line in result.stdout.splitlines() if line.startswith("n/")]


def verify_library_origins(process, application, installed_files=None):
    names = ("qt6", "qtcore", "qtgui", "qtwidgets", "qtmultimedia", "qtpdf", "avcodec", "avformat", "avutil", "swscale", "swresample", "tesseract", "leptonica", "heif", "libraw", "de265", "ggml", "llama", "mtmd", "git2")
    checked = []
    for library in loaded_libraries(process):
        path = Path(library)
        if any(name in path.name.lower() for name in names):
            if application.resolve() not in path.resolve().parents:
                raise RuntimeError(f"Packaged process loaded a development/system application dependency: {library}")
            if installed_files is not None and path.resolve() not in installed_files:
                raise RuntimeError(f"Loaded application dependency is absent from package manifest: {library}")
            checked.append(library)
    if not checked:
        raise RuntimeError("Cannot verify loaded package library origins")
    return checked


def checked_relative(name):
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute() or ".." in value.parts or any(":" in part for part in value.parts):
        raise RuntimeError(f"Archive path escapes extraction root: {name}")
    return Path(*value.parts)


def unpack(package, destination):
    destination.mkdir(parents=True)
    if package.suffix == ".zip":
        with zipfile.ZipFile(package) as archive:
            for entry in archive.infolist():
                checked_relative(entry.filename)
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("ZIP packages must not contain symbolic links")
            archive.extractall(destination)
    elif package.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(package) as archive:
            for entry in archive.getmembers():
                relative = checked_relative(entry.name)
                if entry.isdev() or entry.isfifo():
                    raise RuntimeError("Device nodes and FIFOs are not distribution files")
                if entry.issym() or entry.islnk():
                    if Path(entry.linkname).is_absolute():
                        raise RuntimeError("Absolute archive link rejected")
                    resolved = (destination / relative.parent / entry.linkname).resolve()
                    if destination.resolve() not in resolved.parents:
                        raise RuntimeError("Archive link escapes extraction root")
            archive.extractall(destination, **({"filter": "data"} if sys.version_info >= (3, 12) else {}))
    elif package.suffix == ".dmg" and sys.platform == "darwin":
        with tempfile.TemporaryDirectory(prefix=PRODUCT + "-mount-", dir=destination.parent) as mount:
            subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, str(package)], check=True)
            try:
                for item in Path(mount).iterdir():
                    if item.is_dir() and not item.is_symlink():
                        shutil.copytree(item, destination / item.name, symlinks=True)
            finally:
                subprocess.run(["hdiutil", "detach", mount], check=True)
    else:
        raise RuntimeError("Verification expects a ZIP, TGZ, native macOS DMG or Windows NSIS distribution")


def create_fixtures(directory):
    directory.mkdir()
    (directory / "中文 package notes.txt").write_text("中文验收 qcopilots_package_4317 portable document search.\n" * 30, encoding="utf-8")
    # Minimal standards-conforming PDF with real text and a correct xref table.
    content = b"BT /F1 12 Tf 50 740 Td " + b"(Package PDF 4317 portable searchable document.) Tj 0 -20 Td " * 10 + b"ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    (directory / "package.pdf").write_bytes(pdf)
    # Fixed bitmap glyphs avoid platform font dependencies. OCR is checked below.
    font = {"P": [30,17,17,30,16,16,16], "A": [14,17,17,31,17,17,17], "C": [14,17,16,16,16,17,14],
            "K": [17,18,20,24,20,18,17], "G": [14,17,16,23,17,17,15], "E": [31,16,16,30,16,16,31],
            "4": [2,6,10,18,31,2,2], "3": [30,1,1,14,1,1,30], "1": [4,12,4,4,4,4,14], "7": [31,1,2,4,8,8,8], " ": [0]*7}
    width, height, scale = 640, 160, 7
    pixels = bytearray([255]) * (width * height * 3)
    for index, character in enumerate("PACKAGE 4317"):
        for row, bits in enumerate(font[character]):
            for column in range(5):
                if bits & (1 << (4-column)):
                    for dy in range(scale):
                        for dx in range(scale):
                            position = ((height - 1 - (45 + row*scale + dy))*width + 40 + index*6*scale + column*scale + dx)*3
                            pixels[position:position+3] = b"\0\0\0"
    bmp = b"BM" + struct.pack("<IHHI", 54+len(pixels),0,0,54) + struct.pack("<IiiHHIIiiII",40,width,height,1,24,0,len(pixels),2835,2835,0,0) + pixels
    (directory / "Screenshot package.bmp").write_bytes(bmp)
    with wave.open(str(directory / "package.wav"), "wb") as audio:
        audio.setparams((1,2,16000,0,"NONE","not compressed"))
        audio.writeframes(b"".join(struct.pack("<h",int(12000*math.sin(2*math.pi*440*i/16000))) for i in range(48000)))
    def chunk(kind, body):
        return kind + struct.pack("<I",len(body)) + body + (b"\0" if len(body)%2 else b"")
    size, count = 16*16*3, 60
    avih = struct.pack("<14I",100000,size*10,0,0,count,0,1,size,16,16,0,0,0,0)
    strh = b"vidsDIB " + struct.pack("<IHH8I4h",0,0,0,0,1,10,0,count,size,0xffffffff,0,0,0,16,16)
    strf = struct.pack("<IiiHHIIiiII",40,16,16,1,24,0,size,0,0,0,0)
    headers = chunk(b"LIST", b"hdrl"+chunk(b"avih",avih)+chunk(b"LIST",b"strl"+chunk(b"strh",strh)+chunk(b"strf",strf)))
    frames = b"".join(chunk(b"00db",bytes([255 if (x//3%16 < 8 if i<30 else x//48<8) else 0 for x in range(size)])) for i in range(count))
    (directory / "package.avi").write_bytes(chunk(b"RIFF",b"AVI "+headers+chunk(b"LIST",b"movi"+frames)))


def verify(package, build_root, explicit_report=None, installed_root=None, explicit_profile=None, installed_manifest=None):
    run_id = str(uuid.uuid4())
    report_dir = explicit_report or build_root / "reports" / ("package-"+run_id)
    report_dir.mkdir(parents=True)
    work = build_root / "test-runs" / ("package 中文 空格-"+run_id)
    work.mkdir(parents=True)
    report = {"package":str(package) if package else None, "installedRoot":str(installed_root) if installed_root else None,
              "runId":run_id, "workingDirectory":str(work), "checks":[], "passed":False}
    report["commit"] = os.environ.get("QCOPILOTS_SEARCH_SOURCE_COMMIT") or os.environ.get("GITHUB_SHA")
    report["workingTreeModified"] = None
    if shutil.which("git"):
        checkout_status = subprocess.run(["git", "status", "--porcelain"], cwd=Path(__file__).resolve().parent.parent,
                                         capture_output=True, text=True, encoding="utf-8", timeout=30)
        if checkout_status.returncode == 0:
            report["workingTreeModified"] = bool(checkout_status.stdout.strip())
    if package:
        package_hash = hashlib.sha256()
        with package.open("rb") as package_stream:
            for block in iter(lambda: package_stream.read(1024 * 1024), b""):
                package_hash.update(block)
        report["packageSha256"] = package_hash.hexdigest()
    gui = worker = service = None
    installer = None
    service_started = False
    cli = None
    logs = []
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith(("QT_", "QML_", "QML2_", "QCOPILOTS_SEARCH_")) or key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
            environment.pop(key, None)
    environment["PATH"] = os.pathsep.join((str(Path(os.environ["SystemRoot"])/"System32"),os.environ["SystemRoot"])) if os.name == "nt" else "/usr/bin:/bin"
    profile = explicit_profile or work / "独立配置"
    report["profileDirectory"] = str(profile)
    if explicit_profile and profile.exists() and (not profile.is_dir() or any(profile.iterdir())):
        raise RuntimeError("Explicit package-test profile must be a new or empty dedicated directory")
    application = installed_root or work/"应用"
    installed_files = {Path(line).resolve() for line in installed_manifest.read_text(encoding="utf-8").splitlines()
                       if line.startswith("/")} if installed_manifest else None
    def invoke(*arguments):
        completed = subprocess.run([str(cli),"--profile-dir",str(profile),"--no-start",*map(str,arguments)],cwd=work,env=environment,capture_output=True,text=True,encoding="utf-8",timeout=120)
        (report_dir / (str(len(report["checks"]))+"-cli.log")).write_text(completed.stdout+completed.stderr,encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"CLI failed: {arguments}: {completed.stderr} {completed.stdout}")
        result=json.loads(completed.stdout)
        if not result.get("ok"):
            raise RuntimeError(f"CLI error: {result}")
        return result["result"]
    try:
        if installed_root:
            report["checks"].append("validating the actual installed package tree")
        elif package.suffix.lower() == ".exe":
            installer = NsisInstallation(package, work/"应用")
            installer.install()
            report["checks"].append("NSIS installed for this user into an isolated Unicode directory")
        else:
            unpack(package,work/"应用")
        suffix=".exe" if os.name=="nt" else ""
        candidates = [candidate for candidate in (
            application/"bin"/(PRODUCT+"-cli"+suffix),
            application/"qcopilots-search.app/Contents/MacOS"/(PRODUCT+"-cli"+suffix),
            application/"Contents/MacOS"/(PRODUCT+"-cli"+suffix)) if candidate.is_file()]
        if not candidates and not installed_root:
            candidates=list(application.rglob(PRODUCT+"-cli"+suffix))
        if len(candidates)!=1:
            raise RuntimeError(f"Expected exactly one installed CLI, found {len(candidates)}")
        cli=candidates[0]
        binary=cli.parent
        if os.name == "nt":
            for runtime in ("msvcp140.dll", "vcruntime140.dll", "vcomp140.dll"):
                if not (binary/runtime).is_file():
                    raise RuntimeError(f"Portable Windows distribution lacks app-local compiler runtime: {runtime}")
        data_root = application/"share/qcopilots-search" if installed_root else application
        for language in ("eng", "chi_sim", "chi_tra"):
            if not list(data_root.rglob(language+".traineddata")):
                raise RuntimeError(f"OCR language is missing from distribution: {language}")
        if not list(data_root.rglob("jieba.dict.utf8")):
            raise RuntimeError("Chinese tokenizer dictionary is missing from distribution")
        for name in (PRODUCT, PRODUCT+"-service", PRODUCT+"-worker", PRODUCT+"-cli"):
            completed=subprocess.run([str(binary/(name+suffix)),"--version"],cwd=work,env=environment,capture_output=True,text=True,timeout=30)
            if completed.returncode:
                raise RuntimeError(f"Packaged program failed to load: {name}: {completed.stderr}")
        report["checks"].append("all four relocated executables load without SDK environment")
        service_log = (report_dir/"service.log").open("w",encoding="utf-8"); logs.append(service_log)
        service = subprocess.Popen([str(binary/(PRODUCT+"-service"+suffix)), "--profile-dir", str(profile)],
                                   cwd=work, env=environment, stdout=service_log, stderr=service_log)
        readiness_deadline = time.monotonic() + 30
        while True:
            if service.poll() is not None:
                raise RuntimeError(f"Packaged service exited before IPC readiness: {service.returncode}")
            try:
                invoke("status", "--timeout", "1000")
                service_started = True
                break
            except RuntimeError:
                if time.monotonic() >= readiness_deadline:
                    raise
                time.sleep(0.1)
        report["checks"].append("isolated service IPC status")
        fixtures=work/"样本"
        create_fixtures(fixtures)
        error_log=(report_dir/"worker.log").open("w",encoding="utf-8"); logs.append(error_log)
        worker_environment=environment.copy(); worker_environment["QT_QPA_PLATFORM"]="offscreen"
        worker=subprocess.Popen([str(binary/(PRODUCT+"-worker"+suffix)),"--mode","extract","--profile-dir",str(work/"worker-profile")],cwd=work,env=worker_environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=error_log,text=True,encoding="utf-8")
        replies=queue.Queue()
        threading.Thread(target=lambda:[replies.put(line) for line in worker.stdout],daemon=True).start()
        for fixture in sorted(fixtures.iterdir()):
            request={"id":fixture.name,"operation":"extract","path":str(fixture),"embed":False,"enableOcr":True}
            worker.stdin.write(json.dumps(request,ensure_ascii=False)+"\n"); worker.stdin.flush()
            try:
                reply=json.loads(replies.get(timeout=90))
            except queue.Empty as error:
                raise RuntimeError(f"Packaged worker response timed out for {fixture.name}") from error
            result=reply.get("result",{})
            if not reply.get("ok") or not result.get("complete") or not result.get("segments"):
                raise RuntimeError(f"Packaged extractor failed for {fixture.name}: {reply}")
            if fixture.suffix in (".bmp", ".pdf") and "4317" not in " ".join(segment.get("text","") for segment in result["segments"]):
                raise RuntimeError(f"Packaged text/OCR extraction lost expected content: {fixture.name}: {reply}")
            report["checks"].append("packaged extraction "+fixture.name)
        report["workerLibraries"]=verify_library_origins(worker,application,installed_files)
        invoke("index",fixtures,"--wait","--timeout","120000")
        hits=invoke("search","qcopilots_package_4317","--keyword-only").get("hits",[])
        if not any(hit.get("path","").endswith("中文 package notes.txt") for hit in hits):
            raise RuntimeError("Packaged index/search did not retrieve its text fixture")
        report["checks"].append("packaged service index and exact keyword retrieval")
        if service.poll() is not None:
            raise RuntimeError("The explicitly started package-test service exited unexpectedly")
        report["serviceLibraries"]=verify_library_origins(service,application,installed_files)
        gui_log=(report_dir/"gui.log").open("w",encoding="utf-8"); logs.append(gui_log)
        gui=subprocess.Popen([str(binary/(PRODUCT+suffix)),"--profile-dir",str(profile),"--no-onboarding","--no-tray"],cwd=work,env=environment,stdout=gui_log,stderr=gui_log)
        time.sleep(3)
        if gui.poll() is not None:
            raise RuntimeError(f"Packaged GUI exited during startup: {gui.returncode}")
        report["guiLibraries"]=verify_library_origins(gui,application,installed_files)
        report["checks"].append("native GUI startup with relocated profile (interactive flows covered separately)")
        report["passed"]=True
    except Exception as error:
        report["error"]=str(error)
    finally:
        for process in (gui,worker):
            if process is not None and process.poll() is None:
                if process is worker:
                    try:
                        process.stdin.write('{"id":"shutdown","operation":"shutdown"}\n'); process.stdin.flush()
                        process.wait(timeout=10)
                    except (OSError,subprocess.TimeoutExpired):
                        process.terminate()
                else:
                    process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
        if cli is not None and service_started:
            try: invoke("shutdown","--timeout","10000")
            except Exception as error:
                report["cleanupError"]=str(error)
                report["passed"]=False
        if service is not None:
            try:
                service.wait(timeout=20)
                if service.returncode != 0:
                    raise RuntimeError(f"Packaged service shutdown failed: {service.returncode}")
            except Exception as error:
                report["cleanupError"]=str(error)
                report["passed"]=False
                if service.poll() is None:
                    service.terminate()
                    try: service.wait(timeout=10)
                    except subprocess.TimeoutExpired: service.kill(); service.wait(timeout=10)
        for log in logs: log.close()
        if installer is not None:
            try:
                # User data lives outside the install tree and must survive
                # removal of the actual package payload and registrations.
                sentinel = profile / "qcopilots-search-uninstall-preservation.txt"
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text(run_id, encoding="utf-8")
                installer.uninstall()
                if sentinel.read_text(encoding="utf-8") != run_id:
                    raise RuntimeError("NSIS uninstall modified the isolated user profile")
                report["checks"].append("NSIS uninstall removed payload, registry and shortcuts while retaining user data")
            except Exception as error:
                report["cleanupError"]=str(error)
                report["passed"]=False
            finally:
                installer.close()
        (report_dir/"report.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
        print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
        if report["passed"]:
            shutil.rmtree(work)
    if not report["passed"]:
        raise RuntimeError(report.get("error", report.get("cleanupError", "Package verification failed")))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root",required=True,type=Path)
    parser.add_argument("--package-file",type=Path)
    parser.add_argument("--report-dir",type=Path)
    parser.add_argument("--installed-root",type=Path,help="Verify an already installed package in place, without deleting it")
    parser.add_argument("--installed-manifest",type=Path,help="Absolute file list from the OS package manager, such as dpkg -L")
    parser.add_argument("--profile-dir",type=Path,help="Fresh dedicated test profile retained after verification")
    args=parser.parse_args()
    root=args.build_root.resolve()
    source=Path(__file__).resolve().parent.parent
    if root==source or source in root.parents or root==Path(root.anchor):
        raise RuntimeError("Package verification requires a dedicated external build root")
    package=args.package_file
    if package is not None and args.installed_root:
        raise RuntimeError("Choose either --package-file or --installed-root")
    if args.installed_root and not args.installed_manifest:
        raise RuntimeError("Installed-tree verification requires --installed-manifest from the package manager")
    if package is None and not args.installed_root:
        suffix=".zip" if os.name=="nt" else ".dmg" if sys.platform=="darwin" else ".tar.gz"
        packages=[path for path in (root/"packages").glob(PRODUCT+"-*"+suffix)
                  if "-source-materials-" not in path.name and "-symbols-" not in path.name]
        if not packages: raise RuntimeError("No distribution archive exists. Run package first.")
        package=max(packages,key=lambda value:value.stat().st_mtime)
    report=args.report_dir.expanduser().resolve() if args.report_dir else None
    if report is not None and (report==source or source in report.parents or report==Path(report.anchor)):
        raise RuntimeError("Package report directory must be outside the checkout")
    installed = args.installed_root.expanduser().resolve() if args.installed_root else None
    profile = args.profile_dir.expanduser().resolve() if args.profile_dir else None
    if profile and (profile == source or source in profile.parents or profile == Path(profile.anchor)):
        raise RuntimeError("Package-test profile must be an isolated directory outside the checkout")
    manifest = args.installed_manifest.expanduser().resolve() if args.installed_manifest else None
    verify(package.resolve() if package else None,root,report,installed,profile,manifest)


if __name__=="__main__":
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="backslashreplace")
    try: main()
    except Exception as error:
        print(f"{PRODUCT} package verification failed: {error}",file=sys.stderr)
        sys.exit(1)
