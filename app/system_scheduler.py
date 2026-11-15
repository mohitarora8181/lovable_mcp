"""Opt-in per-user operating-system startup and minute scheduler integration."""
from __future__ import annotations

import os
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys

from .config import get_settings

ROOT = Path(__file__).resolve().parent.parent
MARKER = ROOT / "data" / ".desktop_mode"
TASK_NAME = "LovableWorkflowRunner"
TRAY_TASK_NAME = "LovableWorkflowTray"


def is_installed() -> bool:
    return MARKER.exists()


def _windows_task(name: str, args: list[str], schedule: str) -> None:
    executable = sys.executable
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        executable = str(candidate)
    inner = subprocess.list2cmdline([executable, *args])
    command = subprocess.list2cmdline(["cmd.exe", "/c", f'cd /d "{ROOT}" && {inner}'])
    subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", name, "/SC", schedule, "/TR", command],
        check=True, capture_output=True, text=True,
    )


def _mac_agent(label: str, args: list[str], interval: int | None) -> Path:
    import plistlib
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / f"{label}.plist"
    value = {"Label": label, "ProgramArguments": [sys.executable, *args], "WorkingDirectory": str(ROOT), "RunAtLoad": True, "StandardOutPath": str(ROOT / "data" / f"{label}.log"), "StandardErrorPath": str(ROOT / "data" / f"{label}.log")}
    if interval:
        value["StartInterval"] = interval
    path.write_bytes(plistlib.dumps(value))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False, capture_output=True)
    return path


def install() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    if sys.platform == "win32":
        _windows_task(TASK_NAME, ["-m", "app.system_runner", "tick"], "MINUTE")
        _windows_task(TRAY_TASK_NAME, ["-m", "app.tray"], "ONLOGON")
    elif sys.platform == "darwin":
        _mac_agent("dev.lovable.workflow.runner", ["-m", "app.system_runner", "tick"], 60)
        _mac_agent("dev.lovable.workflow.tray", ["-m", "app.tray"], None)
    else:
        _install_linux()
    state_path = get_settings().system_schedule_state_file
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_checked": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    MARKER.write_text("enabled\n", encoding="utf-8")


def _install_linux() -> None:
    cron_line = f"* * * * * cd {shlex.quote(str(ROOT))} && {shlex.quote(sys.executable)} -m app.system_runner tick >/dev/null 2>&1"
    begin, end = "# BEGIN LOVABLE WORKFLOW RUNNER", "# END LOVABLE WORKFLOW RUNNER"
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = existing.splitlines()
    if begin in lines:
        lines = lines[:lines.index(begin)] + (lines[lines.index(end) + 1:] if end in lines else [])
    lines += [begin, cron_line, end]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", check=True, text=True)
    autostart = Path.home() / ".config" / "autostart"
    autostart.mkdir(parents=True, exist_ok=True)
    desktop = autostart / "lovable-workflow-tray.desktop"
    executable = str(Path(sys.executable).resolve()).replace("\\", "\\\\").replace('"', '\\"')
    desktop.write_text(f'[Desktop Entry]\nType=Application\nName=Lovable Workflow Tray\nExec="{executable}" -m app.tray\nX-GNOME-Autostart-enabled=true\n', encoding="utf-8")


def uninstall() -> None:
    MARKER.unlink(missing_ok=True)
    if sys.platform == "win32":
        for name in (TASK_NAME, TRAY_TASK_NAME):
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], check=False, capture_output=True)
    elif sys.platform == "darwin":
        for label in ("dev.lovable.workflow.runner", "dev.lovable.workflow.tray"):
            path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True)
            path.unlink(missing_ok=True)
    else:
        begin, end = "# BEGIN LOVABLE WORKFLOW RUNNER", "# END LOVABLE WORKFLOW RUNNER"
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        lines = existing.splitlines()
        if begin in lines:
            start = lines.index(begin)
            finish = lines.index(end) if end in lines else start
            lines = lines[:start] + lines[finish + 1:]
            subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", check=True, text=True)
        (Path.home() / ".config" / "autostart" / "lovable-workflow-tray.desktop").unlink(missing_ok=True)

