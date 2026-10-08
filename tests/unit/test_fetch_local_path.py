"""fetch-file / fetch-path name the local copy after the device path, which is device data and may be anything a
POSIX file name allows. It must stay a plain file under <retrieved>/fetched/ on Windows: no device names (com1.log
would open COM1), no "..", "\\" or "//" escaping the session, no characters Windows rejects. The remote path is also
put into the device shell command, which must keep its quoting and must not log the device's shell out.
"""

import base64
import ctypes
import io
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from autodbg.cli import main as cli_main
from autodbg.cli.transport import (
    _FETCH_COMMAND_MAX_BYTES,
    _build_fetch_file_command,
    _build_fetch_path_command,
    _fetch_artifact_relative_path,
    _fetch_remote_file_artifact,
    _fetch_remote_path_artifact,
    _parse_fetch_output_lines,
)
from autodbg.control.controller import CommandResult, DeviceController
from autodbg.evidence.collector import EvidenceCollector
from autodbg.models.profile import DeviceProfile, ModelProfile, RunProfiles, SerialSettings, TaskProfile, TransportProfile
from autodbg.models.session import SessionContext, SessionPaths
from autodbg.session.manager import SessionManager
from autodbg.state.machine import StateSnapshot


if sys.platform == "win32":
    _is_dos_device_name = ctypes.WinDLL("ntdll").RtlIsDosDeviceName_U
    _is_dos_device_name.argtypes = [ctypes.c_wchar_p]
    _is_dos_device_name.restype = ctypes.c_ulong
else:
    _is_dos_device_name = None


def _find_sh() -> str | None:
    found = shutil.which("sh")
    if found or sys.platform != "win32":
        return found
    # Not on PATH (e.g. PowerShell): Git for Windows' bin\sh.exe sets up its own PATH (base64, tar); usr\bin\sh.exe
    # does not.
    git = shutil.which("git")
    candidate = Path(git).resolve().parents[1] / "bin" / "sh.exe" if git else None
    return str(candidate) if candidate is not None and candidate.is_file() else None


_SH = _find_sh()

_HOSTILE_REMOTE_PATHS = [
    "/mnt/sdcard/com1.log",
    "/tmp/COM9",
    "/tmp/con",
    "/tmp/NUL.txt",
    "/tmp/aux.tar.gz",
    "/tmp/lpt1",
    "/tmp/con .log",
    "/tmp/con ",
    "/tmp/con.",
    "/tmp/conin$",
    "/tmp/COM¹.log",
    "/tmp/x/../../../../outside.txt",
    "../../escape.bin",
    "..",
    "/tmp/a\\..\\..\\..\\evil.txt",
    "//server/share/file",
    "/tmp/what?.log",
    "/tmp/a|b<c>d\"e*f",
    "/tmp/tab\there",
    "/tmp/trailing.",
    "/tmp/space ",
    "/tmp/...",
    "/tmp/c:stream",
    "C:/Windows/win.ini",
]


def _path_problems(relative_path: str) -> list[str]:
    """Why relative_path (under some retrieved dir) would not be a plain file inside fetched/ on Windows.

    Pure string checks: GetFullPathNameW (via ntpath.abspath) and RtlIsDosDeviceName_U never open anything, so a
    regression cannot make this test open a real COM port.
    """
    base = "C:\\session\\retrieved"
    problems = []
    full = os.path.normcase(os.path.abspath(os.path.join(base, relative_path))) if sys.platform == "win32" else None
    if full is not None and not full.startswith(os.path.normcase(base + "\\fetched\\")):
        problems.append(f"resolves outside fetched/: {full}")
    parts = PureWindowsPath(relative_path).parts
    if parts[:1] != ("fetched",) or any(part in {".", ".."} for part in parts):
        problems.append(f"unexpected components {parts}")
    for part in parts:
        if _is_dos_device_name is not None and _is_dos_device_name(part):
            problems.append(f"{part!r} is a device name")
        if any(ch in part for ch in '<>:"|?*\\/') or any(ord(ch) < 32 for ch in part):
            problems.append(f"{part!r} has a character Windows rejects")
        if part.endswith((".", " ")):
            problems.append(f"{part!r} ends with a dot or space Windows would strip")
    return problems


class _CannedController:
    """Plays back what the device would print for a fetch; never touches a device."""

    def __init__(self, output_lines: list[str], exit_code: int = 0) -> None:
        self.output_lines = output_lines
        self.exit_code = exit_code
        self.commands: list[str] = []

    def execute(self, command: str, *, timeout: float, serial_port=None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command=command, exit_code=self.exit_code, output_lines=list(self.output_lines), transcript=[])


class _HostShellController:
    """Runs the fetch command in a host POSIX shell inside device_root, standing in for the device's login shell.

    The command is wrapped by DeviceController._wrap_command (begin marker; command; end marker with $?), so a command
    that ends the shell loses its end marker here (exit_code None) just as it would on the device.
    """

    _MARKER = "__AUTODBG_0000TEST__"

    def __init__(self, device_root: Path) -> None:
        self.device_root = device_root
        self.stderr = ""
        self.shell_survived = False

    def execute(self, command: str, *, timeout: float, serial_port=None) -> CommandResult:
        wrapped = DeviceController._wrap_command(command, self._MARKER) + "; echo __SHELL_ALIVE__"
        completed = subprocess.run([_SH, "-c", wrapped], cwd=self.device_root, capture_output=True, timeout=timeout)
        self.stderr = completed.stderr.decode("utf-8", "replace")
        lines = completed.stdout.decode("utf-8", "replace").splitlines()
        self.shell_survived = "__SHELL_ALIVE__" in lines
        output_lines: list[str] = []
        exit_code = None
        capture = False
        for line in lines:
            if line == f"{self._MARKER}_BEGIN":
                capture = True
            elif line.startswith(f"{self._MARKER}_END:"):
                exit_code = int(line.removeprefix(f"{self._MARKER}_END:"))
                break
            elif capture:
                output_lines.append(line)
        return CommandResult(command=command, exit_code=exit_code, output_lines=output_lines, transcript=[])


def _canned_file_output(remote_path: str, payload: bytes, mode: str = "file") -> list[str]:
    return [
        f"__AUTODBG_META__MODE={mode}",
        f"__AUTODBG_META__SOURCE={remote_path}",
        "__AUTODBG_B64__" + base64.b64encode(payload).decode("ascii"),
    ]


def _make_collector(temp_dir: Path) -> EvidenceCollector:
    root = temp_dir / "session"
    session = SessionContext.create(
        session_id="fetch-session",
        device_id="lab-device",
        task_type="fetch_file",
        session_paths=SessionPaths(
            root=root,
            core_dir=root / "core",
            deploy_dir=root / "deploy",
            logs_dir=root / "logs",
            retrieved_dir=temp_dir / "retrieved" / "fetch-session",
        ),
    )
    for path in (root, root / "core", root / "deploy", root / "logs", session.session_paths.retrieved_dir):
        path.mkdir(parents=True, exist_ok=True)
    collector = EvidenceCollector(session)
    collector.bootstrap(
        profiles=RunProfiles(
            device=DeviceProfile(device_id="lab-device", model_id="lab-model", serial=SerialSettings(port="COM19")),
            model=ModelProfile(model_id="lab-model", platform="LAB", app_name="LeCam"),
            task=TaskProfile(
                task_type="fetch_file",
                description="fetch",
                deploy_strategy="manual",
                success_template="default",
                evidence_template="default",
                manual_check_items=[],
            ),
            transport=TransportProfile(transport_id="serial", control_channels=["serial"]),
        ),
        state_snapshot=StateSnapshot(),
        workflow_name="fetch_file",
        workflow_steps=[],
        plan_details={"control": {"mode": "fetch_file"}},
    )
    return collector


class FetchArtifactRelativePathTest(unittest.TestCase):
    def test_ordinary_paths_keep_their_names(self) -> None:
        self.assertEqual(
            PureWindowsPath(_fetch_artifact_relative_path("/var/log/messages")).parts,
            ("fetched", "var", "log", "messages"),
        )
        self.assertEqual(
            PureWindowsPath(_fetch_artifact_relative_path("/mnt/sdcard/COM10.log")).parts,
            ("fetched", "mnt", "sdcard", "COM10.log"),
        )
        self.assertEqual(
            PureWindowsPath(_fetch_artifact_relative_path("/opt/lecam/.config")).parts,
            ("fetched", "opt", "lecam", ".config"),
        )
        # ":" was already replaced before this fix; keep the same name.
        self.assertEqual(PureWindowsPath(_fetch_artifact_relative_path("/tmp/c:stream")).name, "c_stream")
        self.assertEqual(PureWindowsPath(_fetch_artifact_relative_path("/")).parts, ("fetched", "fetched-device-file.bin"))

    def test_device_names_get_an_underscore_after_the_stem(self) -> None:
        cases = {
            "/mnt/sdcard/com1.log": "com1_.log",
            "/tmp/COM9": "COM9_",
            "/tmp/con": "con_",
            "/tmp/NUL.txt": "NUL_.txt",
            "/tmp/aux.tar.gz": "aux_.tar.gz",
            "/tmp/lpt1": "lpt1_",
            "/tmp/con .log": "con_.log",
            "/tmp/conin$": "conin$_",
        }
        for remote_path, expected in cases.items():
            with self.subTest(remote_path=remote_path):
                self.assertEqual(PureWindowsPath(_fetch_artifact_relative_path(remote_path)).name, expected)

    def test_hostile_paths_stay_plain_files_inside_fetched(self) -> None:
        for remote_path in _HOSTILE_REMOTE_PATHS:
            for suffix in ("", ".tar"):  # fetch-path appends .tar for directories
                with self.subTest(remote_path=remote_path, suffix=suffix):
                    relative_path = _fetch_artifact_relative_path(remote_path) + suffix
                    self.assertEqual(_path_problems(relative_path), [])

    def test_dot_dot_and_backslash_are_kept_as_names(self) -> None:
        self.assertEqual(
            PureWindowsPath(_fetch_artifact_relative_path("../../escape.bin")).parts,
            ("fetched", ".._", ".._", "escape.bin"),
        )
        self.assertEqual(
            PureWindowsPath(_fetch_artifact_relative_path("/tmp/a\\..\\evil.txt")).parts,
            ("fetched", "tmp", "a_.._evil.txt"),
        )
        self.assertEqual(
            PureWindowsPath(_fetch_artifact_relative_path("//server/share/file")).parts,
            ("fetched", "server", "share", "file"),
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows path rules")
    def test_problem_checker_flags_the_old_names(self) -> None:
        # Guard against a checker that passes everything.
        self.assertTrue(_path_problems("fetched\\mnt\\com1.log"))
        self.assertTrue(_path_problems("fetched\\..\\..\\escape.bin"))
        self.assertTrue(_path_problems("\\\\server\\share\\file"))
        self.assertTrue(_path_problems("fetched\\tmp\\what?.log"))
        self.assertTrue(_path_problems("fetched\\tmp\\trailing."))


class FetchWritesInsideRetrievedDirTest(unittest.TestCase):
    def test_fetch_file_writes_hostile_names_inside_fetched(self) -> None:
        for index, remote_path in enumerate(_HOSTILE_REMOTE_PATHS):
            with self.subTest(remote_path=remote_path), tempfile.TemporaryDirectory() as temp_dir:
                # Check the name before anything is written, so a regression fails here instead of opening COM1.
                self.assertEqual(_path_problems(_fetch_artifact_relative_path(remote_path)), [])
                collector = _make_collector(Path(temp_dir))
                session_root = collector.session.session_paths.root.resolve()
                fetched_root = (collector.session.session_paths.retrieved_dir / "fetched").resolve()
                payload = f"payload {index}\n".encode()
                summary = _fetch_remote_file_artifact(
                    controller=_CannedController(_canned_file_output(remote_path, payload)),
                    collector=collector,
                    remote_path=remote_path,
                    timeout=5,
                    prefix="fetch-file",
                )
                if any(ord(ch) < 32 for ch in remote_path):  # refused before sending, see FetchRefusedBeforeSendingTest
                    self.assertIn("control characters", summary["error"])
                    continue
                self.assertEqual(summary["status"], "ok", summary.get("error"))
                local_path = Path(summary["local_path"]).resolve()
                self.assertTrue(local_path.is_relative_to(fetched_root), local_path)
                self.assertEqual(local_path.read_bytes(), payload)
                # The fetched file is the only thing written outside the session's own logs/manifest.
                others = [
                    path for path in Path(temp_dir).resolve().rglob("*")
                    if path.is_file() and path != local_path and not path.is_relative_to(session_root)
                ]
                self.assertEqual(others, [])

    def test_fetch_path_tar_of_a_device_named_directory(self) -> None:
        self.assertEqual(_path_problems(_fetch_artifact_relative_path("/tmp/con") + ".tar"), [])
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = _make_collector(Path(temp_dir))
            summary = _fetch_remote_path_artifact(
                controller=_CannedController(_canned_file_output("/tmp/con", b"tar-bytes", mode="tar")),
                collector=collector,
                remote_path="/tmp/con",
                timeout=5,
                prefix="fetch-path",
            )
            self.assertEqual(summary["status"], "ok", summary.get("error"))
            self.assertEqual(summary["mode"], "tar")
            self.assertEqual(Path(summary["local_path"]).name, "con_.tar")
            self.assertEqual(Path(summary["local_path"]).read_bytes(), b"tar-bytes")
            manifest = json.loads(collector.manifest_path.read_text(encoding="utf-8"))
            self.assertIn({"type": "retrieved_tar", "path": summary["local_path"]}, manifest["artifacts"])

    def test_a_name_too_long_to_save_is_reported_not_raised(self) -> None:
        # 255 bytes is a legal device name; the "_" that keeps its trailing dot makes it 256, too long for NTFS.
        remote_path = "/tmp/" + "a" * 254 + "."
        self.assertEqual(_path_problems(_fetch_artifact_relative_path(remote_path)), [])
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = _fetch_remote_file_artifact(
                controller=_CannedController(_canned_file_output(remote_path, b"data")),
                collector=_make_collector(Path(temp_dir)),
                remote_path=remote_path,
                timeout=5,
                prefix="fetch-file",
            )
        self.assertEqual(summary["status"], "error")
        self.assertIn("could not save them locally", summary["error"])


class FetchRefusedBeforeSendingTest(unittest.TestCase):
    """Paths the device shell's line editor would mangle are refused without sending anything."""

    def _fetch_all(self, remote_path: str, exit_code: int = 2) -> list[tuple[dict, _CannedController]]:
        results = []
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = _make_collector(Path(temp_dir))
            for fetch in (_fetch_remote_file_artifact, _fetch_remote_path_artifact):
                controller = _CannedController([], exit_code=exit_code)
                summary = fetch(controller=controller, collector=collector, remote_path=remote_path, timeout=5, prefix="f")
                results.append((summary, controller))
        return results

    def test_control_characters_are_refused(self) -> None:
        for remote_path in ["/tmp/a\tb", "/tmp/a\x7fb", "/tmp/a\nb", "/tmp/a\x01b", "/tmp/a\rb", "/tmp/\x1b[A"]:
            for summary, controller in self._fetch_all(remote_path):
                with self.subTest(remote_path=remote_path):
                    self.assertEqual(summary["status"], "error")
                    self.assertIs(summary["sent"], False)
                    self.assertIn("control characters", summary["error"])
                    self.assertEqual(controller.commands, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows device names")
    def test_output_naming_a_device_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = _make_collector(Path(temp_dir))
            outputs = [Path(temp_dir) / name for name in ("com1.log", "CON", "nul.txt", "lpt3.bin", "con .log")]
            outputs += [Path(temp_dir) / "COM5" / "messages.log", Path("captures/nul/messages.log")]
            outputs += [Path("\\\\.\\COM1"), Path("//./COM1"), Path("\\\\?\\C:\\x\\com2")]
            for output_path in outputs:
                for fetch in (_fetch_remote_file_artifact, _fetch_remote_path_artifact):
                    with self.subTest(output=str(output_path), fetch=fetch.__name__):
                        # exit code 2: even if the check regressed, nothing would be written (no device opened).
                        controller = _CannedController([], exit_code=2)
                        summary = fetch(
                            controller=controller,
                            collector=collector,
                            remote_path="/var/log/messages",
                            timeout=5,
                            prefix="f",
                            output_path=output_path,
                        )
                        self.assertIn("Windows device", summary["error"])
                        self.assertIs(summary["retryable"], False)
                        self.assertEqual(controller.commands, [])

    def test_ordinary_output_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = _CannedController(_canned_file_output("/var/log/messages", b"log\n"))
            output_path = Path(temp_dir) / "out" / "COM10.messages.log"
            summary = _fetch_remote_file_artifact(
                controller=controller,
                collector=_make_collector(Path(temp_dir)),
                remote_path="/var/log/messages",
                timeout=5,
                prefix="f",
                output_path=output_path,
            )
            self.assertEqual(summary["status"], "ok", summary.get("error"))
            self.assertEqual(output_path.read_bytes(), b"log\n")

    def test_missing_path_is_reported_and_not_retryable(self) -> None:
        output = ["AUTODBG_FETCH_MISSING /tmp/nope.log"]
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = _fetch_remote_file_artifact(
                controller=_CannedController(output, exit_code=2),
                collector=_make_collector(Path(temp_dir)),
                remote_path="/tmp/nope.log",
                timeout=5,
                prefix="f",
            )
        self.assertEqual(summary["status"], "error")
        self.assertIn("missing on the device", summary["error"])
        self.assertIs(summary["retryable"], False)

    def test_commands_too_long_for_the_line_editor_are_refused(self) -> None:
        for remote_path in ["/mnt/sdcard/" + "x" * 700, "/tmp/" + "'" * 200, "/tmp/" + "日" * 300]:
            for summary, controller in self._fetch_all(remote_path):
                with self.subTest(remote_path=remote_path[:20]):
                    self.assertEqual(summary["status"], "error")
                    self.assertIn("too long", summary["error"])
                    self.assertEqual(controller.commands, [])

    def test_limit_boundary_and_device_line_budget(self) -> None:
        for builder in (_build_fetch_file_command, _build_fetch_path_command):
            with self.subTest(builder=builder.__name__):
                at_limit = "/" + "d" * (_FETCH_COMMAND_MAX_BYTES - len(builder("").encode("utf-8")) - 1)
                command = builder(at_limit)
                self.assertEqual(len(command.encode("utf-8")), _FETCH_COMMAND_MAX_BYTES)
                # As DeviceController wraps it; the device's busybox line editor was measured to keep 1020 bytes.
                wrapped = DeviceController._wrap_command(command, "__AUTODBG_0123ABCD__")
                self.assertLessEqual(len(wrapped.encode("utf-8")), 1000)
                fetch = _fetch_remote_file_artifact if builder is _build_fetch_file_command else _fetch_remote_path_artifact
                with tempfile.TemporaryDirectory() as temp_dir:
                    collector = _make_collector(Path(temp_dir))
                    sent = _CannedController([], exit_code=2)
                    fetch(controller=sent, collector=collector, remote_path=at_limit, timeout=5, prefix="f")
                    refused = _CannedController([], exit_code=2)
                    summary = fetch(controller=refused, collector=collector, remote_path=at_limit + "d", timeout=5, prefix="f")
                self.assertEqual(sent.commands, [command])
                self.assertEqual(refused.commands, [])
                self.assertIn("too long", summary["error"])


class FetchCommandQuotingTest(unittest.TestCase):
    _AWKWARD = "it's 100%d %s \\c.log"

    def test_commands_hold_the_path_once_on_one_line_in_a_subshell(self) -> None:
        quoted = "'/tmp/it'\"'\"'s 100%d %s \\c.log'"
        for builder in (_build_fetch_file_command, _build_fetch_path_command):
            command = builder("/tmp/" + self._AWKWARD)
            with self.subTest(builder=builder.__name__):
                # A subshell, so "exit 2" ends the fetch rather than the device's login shell.
                self.assertTrue(command.startswith(f"(p={quoted}; "), command)
                self.assertTrue(command.endswith(")"), command)
                self.assertEqual(command.count("100%d %s"), 1)
                self.assertNotIn("\n", command)
                # The path is only ever a printf argument, never pasted into a literal or a format.
                self.assertIn("printf 'AUTODBG_FETCH_MISSING %s\\n' \"$p\" >&2; exit 2", command)
                self.assertIn("__AUTODBG_META__MODE=file\\n__AUTODBG_META__SOURCE=%s\\n' \"$p\"", command)

    def test_fetch_path_command_stays_short(self) -> None:
        # The device shell's line editor holds about 1 KB; the path used to appear seven times.
        self.assertLess(len(_build_fetch_path_command("/mnt/sdcard/" + "x" * 400)), 900)

    @unittest.skipUnless(_SH, "needs a POSIX sh")
    def test_commands_parse_in_sh(self) -> None:
        for remote_path in ["/tmp/" + self._AWKWARD, "/tmp/'", "/tmp/''", "/tmp/a\nb", "/tmp/$(id)`id`", "-x", ""]:
            for builder in (_build_fetch_file_command, _build_fetch_path_command):
                with self.subTest(remote_path=remote_path, builder=builder.__name__):
                    checked = subprocess.run([_SH, "-n", "-c", builder(remote_path)], capture_output=True)
                    self.assertEqual(checked.returncode, 0, checked.stderr)

    @unittest.skipUnless(_SH, "needs a POSIX sh")
    def test_missing_path_reports_the_exact_path_and_keeps_the_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = _HostShellController(Path(temp_dir))
            for remote_path in ["missing/it's.log", "missing/100%d.log", "missing/$(echo pwned)", "missing/a\\cb"]:
                for builder in (_build_fetch_file_command, _build_fetch_path_command):
                    with self.subTest(remote_path=remote_path, builder=builder.__name__):
                        result = controller.execute(builder(remote_path), timeout=20)
                        # Without the subshell "exit 2" ended the login shell: no end marker, exit code None.
                        self.assertEqual(result.exit_code, 2)
                        self.assertTrue(controller.shell_survived)
                        self.assertEqual(result.output_lines, [])
                        self.assertEqual(controller.stderr.strip(), f"AUTODBG_FETCH_MISSING {remote_path}")

    @unittest.skipUnless(_SH, "needs a POSIX sh")
    def test_fetch_file_with_awkward_name_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            device_root = Path(temp_dir) / "device"
            (device_root / "logs").mkdir(parents=True)
            name = "it's 100%d %s.log"  # no "\": the host here may be Windows
            payload = b"line one\nline two\x00\xff\n"
            (device_root / "logs" / name).write_bytes(payload)
            remote_path = f"logs/{name}"
            controller = _HostShellController(device_root)

            output = controller.execute(_build_fetch_file_command(remote_path), timeout=20)
            metadata, _ = _parse_fetch_output_lines(output.output_lines)
            self.assertEqual(metadata.get("source"), remote_path)  # "%d" / "%s" used to be eaten by printf

            collector = _make_collector(Path(temp_dir) / "host")
            summary = _fetch_remote_file_artifact(
                controller=controller,
                collector=collector,
                remote_path=remote_path,
                timeout=20,
                prefix="fetch-file",
            )
            self.assertEqual(summary["status"], "ok", summary.get("error"))
            self.assertEqual(Path(summary["local_path"]).read_bytes(), payload)
            self.assertEqual(Path(summary["local_path"]).name, name)

    @unittest.skipUnless(_SH, "needs a POSIX sh")
    def test_fetch_path_directory_with_awkward_name_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            device_root = Path(temp_dir) / "device"
            directory = device_root / "it's 100%s dir"
            directory.mkdir(parents=True)
            (directory / "a.txt").write_bytes(b"alpha\n")
            remote_path = directory.name
            collector = _make_collector(Path(temp_dir) / "host")
            summary = _fetch_remote_path_artifact(
                controller=_HostShellController(device_root),
                collector=collector,
                remote_path=remote_path,
                timeout=20,
                prefix="fetch-path",
            )
            self.assertEqual(summary["status"], "ok", summary.get("error"))
            self.assertEqual(summary["mode"], "tar")
            local_path = Path(summary["local_path"])
            self.assertEqual(local_path.name, f"{remote_path}.tar")
            with tarfile.open(fileobj=io.BytesIO(local_path.read_bytes())) as archive:
                member = archive.extractfile(f"{remote_path}/a.txt")
                self.assertEqual(member.read(), b"alpha\n")

    @unittest.skipUnless(_SH, "needs a POSIX sh")
    def test_fetch_path_directory_starting_with_a_dash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            device_root = Path(temp_dir) / "device"
            (device_root / "-n").mkdir(parents=True)
            (device_root / "-n" / "a.txt").write_bytes(b"dash\n")
            summary = _fetch_remote_path_artifact(
                controller=_HostShellController(device_root),
                collector=_make_collector(Path(temp_dir) / "host"),
                remote_path="-n",
                timeout=20,
                prefix="fetch-path",
            )
            # Without "--" tar took "-n" for an option and sent no payload.
            self.assertEqual(summary["status"], "ok", summary.get("error"))
            with tarfile.open(fileobj=io.BytesIO(Path(summary["local_path"]).read_bytes())) as archive:
                self.assertEqual(archive.extractfile("-n/a.txt").read(), b"dash\n")


class FetchFileCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from autodbg.profiles.loader import load_run_profiles

        root = Path(__file__).resolve().parents[2]
        cls.profiles = load_run_profiles(
            root / "profiles" / "devices" / "av130n-lab.toml",
            root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            root / "profiles" / "tasks" / "startup-check.toml",
            root / "profiles" / "transports" / "network-serial-fallback.toml",
        )

    def test_fetch_file_transfers_once_and_saves_under_fetched(self) -> None:
        remote_path = "/mnt/sdcard/com1.log"
        self.assertEqual(_path_problems(_fetch_artifact_relative_path(remote_path)), [])
        payload = b"serial log\n" * 100
        controller = _CannedController(_canned_file_output(remote_path, payload))
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            retrieved_root = Path(temp_dir) / "retrieved"
            args = cli_main._build_parser().parse_args(
                ["fetch-file", "--remote-path", remote_path, "--artifacts-root", str(artifacts_root)]
            )
            output = io.StringIO()
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=self.profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, retrieved_root)),
                patch.object(cli_main, "DeviceController", return_value=controller),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main._command_fetch_file(args)

            self.assertEqual(exit_code, 0, output.getvalue())
            # fetch-file used to run the transfer, drop the result and run it again.
            self.assertEqual(len(controller.commands), 1)
            local_lines = [line for line in output.getvalue().splitlines() if line.startswith("[DONE] Local file: ")]
            self.assertEqual(len(local_lines), 1, output.getvalue())
            local_path = Path(local_lines[0].removeprefix("[DONE] Local file: "))
            self.assertEqual(local_path.name, "com1_.log")
            self.assertTrue(local_path.resolve().is_relative_to(retrieved_root.resolve()), local_path)
            self.assertEqual(local_path.read_bytes(), payload)

    def test_refused_fetch_says_nothing_was_sent(self) -> None:
        for command_name, remote_path in [("fetch-file", "/tmp/a\tb"), ("fetch-path", "/mnt/" + "d" * 900)]:
            controller = _CannedController([])
            with self.subTest(command=command_name), tempfile.TemporaryDirectory() as temp_dir:
                artifacts_root = Path(temp_dir) / "artifacts"
                args = cli_main._build_parser().parse_args(
                    [command_name, f"--remote-path={remote_path}", "--artifacts-root", str(artifacts_root)]
                )
                output = io.StringIO()
                with (
                    patch.object(cli_main, "_load_profiles_from_args", return_value=self.profiles),
                    patch.object(
                        cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "retrieved")
                    ),
                    patch.object(cli_main, "DeviceController", return_value=controller),
                    patch("sys.stdout", output),
                ):
                    handler = cli_main._command_fetch_file if command_name == "fetch-file" else cli_main._command_fetch_path
                    exit_code = handler(args)
                self.assertEqual(exit_code, 1)
                self.assertEqual(controller.commands, [])
                # Agents report the last printed line as the error.
                self.assertTrue(output.getvalue().splitlines()[-1].startswith("[TODO] Nothing was sent to the device: "))
                # ...and must not be told to retry the identical request.
                session_dir = SessionManager(artifacts_root).latest_session_dir()
                result = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))["result"]
                self.assertIs(result["retryable"], False)
                self.assertIn("change --remote-path or --output", result["next_actions"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
