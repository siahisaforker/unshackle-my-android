#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class Error(Exception):
    pass


class Mode(StrEnum):
    SPEED = "speed"
    BALANCED = "speed-profile"
    VERIFY = "verify"


class Scope(StrEnum):
    ALL = "all"
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class Device:
    serial: str
    model: str
    android: str
    build: str
    battery: int | None
    thermal: int | None
    free_gib: float | None
    booted: bool


@dataclass(frozen=True)
class Status:
    primary: tuple[str, ...] = ()
    secondary: tuple[str, ...] = ()


def adb_cache():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "unshackle-my-android" / "platform-tools"


def download_adb():
    platform = {"win32": "windows", "darwin": "darwin", "linux": "linux"}.get(sys.platform)
    if not platform:
        raise Error("unsupported platform")

    folder = adb_cache()
    folder.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://dl.google.com/android/repository/platform-tools-latest-{platform}.zip"
    print("downloading platform-tools from google...")

    with tempfile.TemporaryDirectory(dir=folder.parent) as temporary:
        temp = Path(temporary)
        archive_path = temp / "platform-tools.zip"
        with urllib.request.urlopen(url, timeout=40) as response, archive_path.open("wb") as output:
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 150 * 1024 * 1024:
                    raise Error("download is too large")
                output.write(block)

        with zipfile.ZipFile(archive_path) as archive:
            files = archive.infolist()
            if not files or sum(f.file_size for f in files) > 300 * 1024 * 1024:
                raise Error("invalid platform-tools archive")
            for info in files:
                parts = Path(info.filename).parts
                if not parts or parts[0] != "platform-tools" or any(x in ("..", "") for x in parts):
                    raise Error("invalid platform-tools archive")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise Error("invalid platform-tools archive")
            archive.extractall(temp)

        executable = temp / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        if not executable.is_file():
            raise Error("adb missing from archive")
        if os.name != "nt":
            executable.chmod(executable.stat().st_mode | 0o111)
        subprocess.run([str(executable), "version"], capture_output=True, check=True, timeout=20)
        if folder.exists():
            shutil.rmtree(folder)
        (temp / "platform-tools").rename(folder)

    return str(folder / executable.name)


def system_adb():
    if sys.platform == "win32":
        command = ["winget", "install", "-e", "--id", "Google.PlatformTools"]
    elif sys.platform == "darwin":
        command = ["brew", "install", "--cask", "android-platform-tools"]
    elif sys.platform == "linux":
        managers = (
            ("apt-get", ["apt-get", "install", "adb"]),
            ("dnf", ["dnf", "install", "android-tools"]),
            ("pacman", ["pacman", "-S", "android-tools"]),
            ("zypper", ["zypper", "install", "android-tools"]),
            ("apk", ["apk", "add", "android-tools"]),
        )
        command = next((args for name, args in managers if shutil.which(name)), None)
        if command and os.geteuid() != 0:
            if not shutil.which("sudo"):
                raise Error("sudo not found")
            command = ["sudo", *command]
    else:
        command = None

    if not command or not shutil.which(command[0]):
        raise Error("no supported package manager found; use local installation")
    print("command:", " ".join(command))
    if input("run this command? [y/N] ").strip().lower() not in ("y", "yes"):
        raise Error("cancelled")
    if subprocess.run(command, check=False).returncode:
        raise Error("install failed")
    found = shutil.which("adb")
    if not found and os.name == "nt":
        links = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Microsoft/WinGet/Links/adb.exe"
        if links.is_file():
            found = str(links)
    if not found:
        raise Error("adb installed but not on PATH; restart your terminal")
    return found


def find_adb(executable):
    found = shutil.which(executable)
    if found:
        return found
    if executable != "adb":
        raise Error(f"{executable} not found")

    cached = adb_cache() / ("adb.exe" if os.name == "nt" else "adb")
    if cached.is_file() and (os.name == "nt" or os.access(cached, os.X_OK)):
        return str(cached)
    if not sys.stdin.isatty():
        raise Error("adb not found; run this in a terminal to install it")

    print("adb is not present. do you want to install it?")
    if input("[y/N] ").strip().lower() not in ("y", "yes"):
        raise Error("adb is required")
    print("1. local (google platform-tools)")
    print("2. system-wide (package manager)")
    option = input("[1/2] ").strip()
    if option not in ("1", "2"):
        raise Error("cancelled")
    try:
        return download_adb() if option == "1" else system_adb()
    except (OSError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        raise Error(f"adb install failed: {exc}") from exc


class ADB:
    def __init__(self, serial=None, executable="adb"):
        self.executable = find_adb(executable)
        self.serial = serial or self.find_device()

    def call(self, *args, timeout=30, allow_error=False):
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, text=True,
                errors="replace", timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error(str(exc)) from exc
        if result.returncode and not allow_error:
            raise Error(result.stderr.strip() or result.stdout.strip() or "adb failed")
        return result.stdout.strip() or (result.stderr.strip() if allow_error else "")

    def find_device(self):
        devices = []
        for line in self.call("devices", "-l").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1] == "device":
                devices.append(parts[0])
        if len(devices) != 1:
            raise Error("no device connected" if not devices else "select a device with --serial")
        return devices[0]

    def shell(self, *args, timeout=30, allow_error=False):
        return self.call("-s", self.serial, "shell", *args, timeout=timeout, allow_error=allow_error)

    def device(self):
        battery = self.shell("dumpsys", "battery")
        thermal = self.shell("dumpsys", "thermalservice")
        storage = self.shell("df", "-k", "/data")
        return Device(
            self.serial,
            self.shell("getprop", "ro.product.model") or "unknown",
            self.shell("getprop", "ro.build.version.release") or "unknown",
            self.shell("getprop", "ro.build.fingerprint") or "unknown",
            number(battery, r"^\s*level:\s*(\d+)"),
            number(thermal, r"Thermal Status:\s*(\d+)"),
            parse_storage(storage),
            self.shell("getprop", "sys.boot_completed") == "1",
        )

    def packages(self, scope):
        all_apps = set(parse_packages(self.shell("pm", "list", "packages")))
        if scope == Scope.ALL:
            return sorted(all_apps)
        user = set(parse_packages(self.shell("pm", "list", "packages", "-3")))
        return sorted(user if scope == Scope.USER else all_apps - user)

    def paths(self):
        return parse_paths(self.shell("pm", "list", "packages", "-f"))

    def compile_options(self):
        help_text = self.shell("cmd", "package", "help", allow_error=True)
        lines = help_text.splitlines()
        start = next((i for i, line in enumerate(lines) if re.match(r"^  compile(?:\s|\[)", line)), None)
        if start is None:
            return False, False
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  [a-z][\w-]*(?:\s|$)", lines[i])), len(lines))
        section = "\n".join(lines[start:end])
        return "--full" in section, "PRIORITY_BACKGROUND" in section

    def compile(self, package, mode, full, background, force, timeout):
        args = ["cmd", "package", "compile", "-m", mode.value]
        if background:
            args += ["-p", "PRIORITY_BACKGROUND"]
        if full:
            args += ["--full"]
        if force:
            args += ["-f"]
        return self.shell(*args, package, timeout=timeout)

    def status(self):
        return parse_status(self.shell("dumpsys", "package", timeout=90))


def number(text, pattern):
    match = re.search(pattern, text, re.M)
    return int(match.group(1)) if match else None


def parse_storage(text):
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    try:
        return int(lines[-1].split()[3]) / 1024 ** 2
    except (IndexError, ValueError):
        return None


def parse_packages(text):
    return sorted({line[8:] for line in text.splitlines() if line.startswith("package:")})


def parse_paths(text):
    paths = {}
    for line in text.splitlines():
        if line.startswith("package:") and "=" in line:
            path, package = line[8:].rsplit("=", 1)
            paths[package] = path
    return paths


def parse_status(text):
    _, marker, text = text.partition("Dexopt state:")
    if not marker:
        return {}
    data = {}
    name = None
    secondary = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            break
        if match := re.fullmatch(r"  \[([^]]+)\]", line):
            name = match.group(1)
            data[name] = {"primary": [], "secondary": []}
            secondary = False
        elif name:
            if line.strip() == "known secondary dex files:":
                secondary = True
            elif match := re.search(r"\[status=([^]]+)\]", line):
                data[name]["secondary" if secondary else "primary"].append(match.group(1))
    return {name: Status(tuple(item["primary"]), tuple(item["secondary"])) for name, item in data.items()}


def check(device, minimum_battery, minimum_free):
    if not device.booted:
        raise Error("phone hasn't finished booting")
    if device.battery is None or device.battery < minimum_battery:
        raise Error(f"battery is too low ({device.battery}%)")
    if device.free_gib is None or device.free_gib < minimum_free:
        raise Error(f"not enough free space ({device.free_gib} GiB)")
    if device.thermal is not None and device.thermal >= 3:
        raise Error(f"phone is too hot (thermal status {device.thermal})")


def default_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UnshackleMyAndroid"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "unshackle-my-android"


def report_file(directory, serial, mode, scope):
    serial = re.sub(r"[^a-zA-Z0-9._-]", "_", serial)
    return directory / f"{serial}-{mode.value}-{scope.value}.json"


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def load_state(path, device, mode, scope):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Error(f"can't read {path}: {exc}") from exc
    if (data.get("build"), data.get("mode"), data.get("scope")) != (device.build, mode.value, scope.value):
        raise Error("saved run doesn't match this build or mode")
    if not isinstance(data.get("packages"), dict):
        raise Error("invalid save file")
    return data


def print_device(device):
    print(f"{device.model} ({device.serial}), Android {device.android}")
    print(f"Battery: {device.battery}% | Thermal: {device.thermal} | Free: {device.free_gib} GiB")


def print_status(artifacts, selected, mode):
    primary = Counter(value for name in selected for value in artifacts.get(name, Status()).primary)
    secondary = Counter(value for name in selected for value in artifacts.get(name, Status()).secondary)
    missing = sum(not artifacts.get(name, Status()).primary for name in selected)
    different = [name for name in selected if any(item != mode.value for item in artifacts.get(name, Status()).primary)]
    print(f"Primary: {dict(primary)}")
    print(f"Secondary: {dict(secondary)}")
    print(f"No DEX: {missing}")
    if different:
        print(f"Other filters: {', '.join(different[:8])}" + (f" (+{len(different) - 8})" if len(different) > 8 else ""))
    return {
        "primary": dict(primary), "secondary": dict(secondary),
        "no_primary_artifacts": missing, "different_primary_filters": different,
    }


def run(adb, options):
    device = adb.device()
    apps = adb.packages(options.scope)
    paths = adb.paths()
    full, background = adb.compile_options()
    print_device(device)
    print(f"{len(apps)} packages | {options.mode.value}")
    if not full:
        print("--full isn't supported on this build")
    if options.dry_run:
        print("dry run")
        for app in apps:
            print(app)
        return 0

    check(device, options.minimum_battery, options.minimum_free)
    path = report_file(options.state_dir, device.serial, options.mode, options.scope)
    data = load_state(path, device, options.mode, options.scope) if options.resume else {
        "build": device.build, "device": asdict(device), "mode": options.mode.value,
        "scope": options.scope.value, "started": datetime.now(timezone.utc).isoformat(), "packages": {},
    }
    saved = data["packages"]
    passed = failed = skipped = 0
    started = time.monotonic()
    print(f"report: {path}")

    for i, app in enumerate(apps, 1):
        old = saved.get(app, {})
        if options.resume and old.get("result") == "success" and old.get("apk_path") == paths.get(app):
            skipped += 1
            continue
        if i == 1 or i % options.check_every == 0:
            check(adb.device(), options.minimum_battery, options.minimum_free)

        t0 = time.monotonic()
        try:
            output = adb.compile(app, options.mode, full, background, options.force, options.timeout)
            ok = output.strip() == "Success"
            message = output.strip() or "no result"
        except Error as exc:
            ok = False
            message = str(exc)

        saved[app] = {
            "apk_path": paths.get(app), "duration_seconds": round(time.monotonic() - t0, 2),
            "result": "success" if ok else "failed", "detail": message,
        }
        save_json(path, data)
        passed += ok
        failed += not ok
        bar = "#" * round(i * 20 / len(apps))
        print(f"[{bar:<20}] {i}/{len(apps)} {'ok' if ok else 'fail'} {app}", flush=True)
        if not ok:
            print(message, file=sys.stderr)

    data["finished"] = datetime.now(timezone.utc).isoformat()
    data["elapsed_seconds"] = round(time.monotonic() - started, 2)
    data["summary"] = {"success": passed, "failed": failed, "resumed": skipped, "total": len(apps)}
    try:
        data["verification"] = print_status(adb.status(), apps, options.mode)
    except Error as exc:
        data["verification_error"] = str(exc)
        print(f"can't check status: {exc}", file=sys.stderr)
    save_json(path, data)
    print(f"done: {passed} ok, {skipped} skipped, {failed} failed")
    return int(failed > 0)


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    index = 0
    while index < len(args):
        if args[index] in ("--serial", "--adb") and index + 1 < len(args):
            index += 2
        elif args[index].startswith(("--serial=", "--adb=")):
            index += 1
        else:
            break
    if index == len(args) or args[index] not in ("run", "scan", "status", "-h", "--help"):
        args.insert(index, "run")

    parser = argparse.ArgumentParser(description="compile android apps with ART")
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    parser.add_argument("--adb", default="adb")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--scope", type=Scope, choices=list(Scope), default=Scope.ALL)
    status = commands.add_parser("status")
    status.add_argument("--scope", type=Scope, choices=list(Scope), default=Scope.ALL)
    status.add_argument("--mode", type=Mode, choices=list(Mode), default=Mode.SPEED)
    cmd = commands.add_parser("run")
    cmd.add_argument("--scope", type=Scope, choices=list(Scope), default=Scope.ALL)
    cmd.add_argument("--mode", type=Mode, choices=list(Mode), default=Mode.SPEED)
    cmd.add_argument("--dry-run", action="store_true")
    cmd.add_argument("--resume", action="store_true")
    cmd.add_argument("--force", action="store_true")
    cmd.add_argument("--timeout", type=int, default=300)
    cmd.add_argument("--minimum-battery", type=int, default=30)
    cmd.add_argument("--minimum-free", type=float, default=4)
    cmd.add_argument("--check-every", type=int, default=8)
    cmd.add_argument("--state-dir", type=Path, default=default_dir())
    options = parser.parse_args(args)
    if options.command == "run" and (options.timeout < 1 or options.check_every < 1 or options.minimum_battery < 0 or options.minimum_free < 0):
        parser.error("invalid limits")

    try:
        adb = ADB(options.serial, options.adb)
        if options.command == "scan":
            device = adb.device()
            print_device(device)
            print(f"{len(adb.packages(options.scope))} packages")
            return 0
        if options.command == "status":
            print_device(adb.device())
            print_status(adb.status(), adb.packages(options.scope), options.mode)
            return 0
        return run(adb, options)
    except KeyboardInterrupt:
        print("stopped. use --resume to continue", file=sys.stderr)
        return 130
    except Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
