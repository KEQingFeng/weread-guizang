#!/usr/bin/env python3
"""平台差异模块的自测：本机跑不到 Windows，就把 Windows 分支「假装」跑一遍。

重点不是覆盖率，是防止这几处又退化成「静默什么都不做」——
所以每条断言都盯着「到底调用了什么」，而不是「有没有抛异常」。

跑法：.venv/bin/python tools/test_platform_compat.py      （macOS / Linux）
     .venv\\Scripts\\python.exe tools\\test_platform_compat.py   （Windows）
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import platform_compat as pc  # noqa: E402


def _fake_binary(root, *parts):
    p = Path(root, *parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return str(p)


class VenvPython(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GUIZANG_PYTHON", None)

    def test_prefers_windows_layout(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_binary(d, ".venv", "Scripts", "python.exe")
            self.assertEqual(pc.venv_python(d), exe)

    def test_prefers_posix_layout(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_binary(d, ".venv", "bin", "python")
            self.assertEqual(pc.venv_python(d), exe)

    def test_falls_back_to_current_interpreter(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pc.venv_python(d), sys.executable)

    def test_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_binary(d, ".venv", "bin", "python")
            other = _fake_binary(d, "elsewhere", "python")
            with mock.patch.dict(os.environ, {"GUIZANG_PYTHON": other}):
                self.assertEqual(pc.venv_python(d), other)

    def test_override_ignored_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_binary(d, ".venv", "bin", "python")
            with mock.patch.dict(os.environ, {"GUIZANG_PYTHON": os.path.join(d, "nope")}):
                self.assertEqual(pc.venv_python(d), exe)


class PlaywrightDir(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    def test_windows_uses_localappdata(self):
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join("C:", "Users", "x", "AppData", "Local")}):
            self.assertEqual(pc.ms_playwright_dir(),
                             os.path.join("C:", "Users", "x", "AppData", "Local", "ms-playwright"))

    def test_env_var_wins(self):
        target = os.path.join(os.sep, "custom", "pw")
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": target}):
            self.assertEqual(pc.ms_playwright_dir(), target)

    def test_env_var_disabled_by_zero(self):
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0",
                                             "LOCALAPPDATA": os.path.join("C:", "L")}):
            self.assertEqual(pc.ms_playwright_dir(), os.path.join("C:", "L", "ms-playwright"))

    def test_posix_defaults(self):
        with mock.patch.object(pc, "IS_WIN", False), mock.patch.object(pc, "IS_MAC", True):
            self.assertTrue(pc.ms_playwright_dir().endswith(os.path.join("Library", "Caches", "ms-playwright")))
        with mock.patch.object(pc, "IS_WIN", False), mock.patch.object(pc, "IS_MAC", False):
            self.assertEqual(pc.ms_playwright_dir(), os.path.expanduser("~/.cache/ms-playwright"))


class SpawnKwargs(unittest.TestCase):
    def test_posix_new_session(self):
        with mock.patch.object(pc, "IS_WIN", False):
            self.assertEqual(pc.spawn_kwargs(), {"start_new_session": True})

    def test_windows_new_process_group(self):
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUIZANG_HIDE_WINDOW", None)
            self.assertEqual(pc.spawn_kwargs(), {"creationflags": 0x200})

    def test_windows_hide_window_opt_in(self):
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True), \
                mock.patch.dict(os.environ, {"GUIZANG_HIDE_WINDOW": "1"}):
            self.assertEqual(pc.spawn_kwargs(), {"creationflags": 0x200 | 0x8000000})

    def test_hide_window_not_set_keeps_console(self):
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True):
            os.environ.pop("GUIZANG_HIDE_WINDOW", None)
            self.assertEqual(pc.spawn_kwargs()["creationflags"], 0x200)


class FakeProc:
    def __init__(self, pid=4242, fail=()):
        self.pid = pid
        self.fail = set(fail)
        self.signals = []
        self.killed = False

    def send_signal(self, sig):
        if "signal" in self.fail:
            raise OSError("no")
        self.signals.append(sig)

    def terminate(self):
        if "terminate" in self.fail:
            raise OSError("no")

    def kill(self):
        self.killed = True


class SendStop(unittest.TestCase):
    def test_windows_prefers_ctrl_break(self):
        proc = FakeProc()
        # macOS 的 signal 模块没有 CTRL_BREAK_EVENT，假装置上（真机是 1）
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.object(pc.signal, "CTRL_BREAK_EVENT", 1, create=True):
            self.assertTrue(pc.send_stop(proc))
        self.assertEqual(proc.signals, [1])

    def test_windows_without_ctrl_break_falls_back(self):
        proc = FakeProc()
        with mock.patch.object(pc, "IS_WIN", True), \
                mock.patch.object(pc.signal, "CTRL_BREAK_EVENT", None, create=True):
            self.assertTrue(pc.send_stop(proc))
        self.assertEqual(proc.signals, [])

    def test_posix_prefers_sigterm(self):
        proc = FakeProc()
        with mock.patch.object(pc, "IS_WIN", False):
            self.assertTrue(pc.send_stop(proc))
        self.assertEqual(proc.signals, [pc.signal.SIGTERM])

    def test_falls_back_to_terminate(self):
        proc = FakeProc(fail={"signal"})
        with mock.patch.object(pc, "IS_WIN", False):
            self.assertTrue(pc.send_stop(proc))
        self.assertEqual(proc.signals, [])

    def test_reports_failure_when_nothing_works(self):
        proc = FakeProc(fail={"signal", "terminate"})
        with mock.patch.object(pc, "IS_WIN", False):
            self.assertFalse(pc.send_stop(proc))


class HardKill(unittest.TestCase):
    def test_windows_uses_taskkill_tree(self):
        proc = FakeProc(pid=99)
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return mock.Mock()

        with mock.patch.object(pc, "IS_WIN", True), mock.patch.object(subprocess, "run", fake_run):
            pc.hard_kill(proc)
        self.assertEqual(seen["argv"][:3], ["taskkill", "/PID", "99"])
        self.assertIn("/T", seen["argv"])
        self.assertIn("/F", seen["argv"])

    def test_posix_kills_process_group(self):
        proc = FakeProc()
        with mock.patch.object(pc, "IS_WIN", False), \
                mock.patch.object(pc.os, "getpgid", return_value=7, create=True), \
                mock.patch.object(pc.os, "killpg") as killpg:
            pc.hard_kill(proc)
        killpg.assert_called_once_with(7, pc.signal.SIGKILL)


class KillStrayBrowsers(unittest.TestCase):
    def test_posix_matches_absolute_profile_path(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return mock.Mock()

        with mock.patch.object(pc, "IS_WIN", False), mock.patch.object(subprocess, "run", fake_run):
            pc.kill_stray_browsers("cache/browser_profile", wait=0)
        self.assertEqual(seen["argv"][0], "pkill")
        self.assertEqual(seen["argv"][1], "-f")
        self.assertTrue(os.path.isabs(seen["argv"][2]))

    def test_windows_uses_powershell_and_literal_match(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return mock.Mock()

        with mock.patch.object(pc, "IS_WIN", True), mock.patch.object(subprocess, "run", fake_run):
            pc.kill_stray_browsers("cache/browser_profile", wait=0)
        self.assertEqual(seen["argv"][0], "powershell")
        script = seen["argv"][-1]
        self.assertIn(".Contains(", script)
        self.assertNotIn("-like", script)
        self.assertTrue(os.path.isabs(seen["argv"][-1].split("'")[1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
