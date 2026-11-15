#!/usr/bin/env python3
"""Desktop and application-menu launcher for the Worker Control Panel.

    python3 tools/panel/desktop_launcher.py install [--desktop] [--no-menu]
    python3 tools/panel/desktop_launcher.py uninstall
    python3 tools/panel/desktop_launcher.py show            # the entry, without writing it

``install`` writes a freedesktop.org desktop entry for *this* installation of
the development system:

* the application menu: ``$XDG_DATA_HOME/applications/`` (default
  ``~/.local/share/applications``);
* with ``--desktop``, also a copy on the desktop (``xdg-user-dir DESKTOP``),
  marked executable and trusted so the desktop's icon view may launch it.

The entry runs ``tools/worker-panel`` of the directory this file belongs to.
A desktop entry must name absolute paths, so they are computed here, at
install time, from this file's own location; nothing machine-specific is kept
in the repository. Moving the installation means running ``install`` again
(the old entry is replaced: its file name is derived from the project name
and the installation's path). Several installations (projects) each get
their own entry.

The icon is the generic freedesktop icon ``utilities-system-monitor`` (no
asset is shipped). Only the standard library is used, so this works before
PySide6 is installed; ``tools/worker-panel`` reports a missing PySide6 when
the entry is launched.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM = HERE.parent.parent
sys.path.insert(0, str(SYSTEM / "scripts"))
sys.dont_write_bytecode = True

import devsys_config as cfgmod  # noqa: E402

APP_NAME = "Development System — Worker Control"
ICON = "utilities-system-monitor"
WM_CLASS = "devsys-worker-control"      # tools/panel/worker_panel.py APP_NAME
KEY = "X-Devsys-System"                 # which installation an entry launches


def _quote(arg: str) -> str:
    """An Exec argument, quoted as the Desktop Entry Specification requires."""
    if arg and not re.search(r'[\s"\'\\><~|&;$*?#()`]', arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def _value(text: str) -> str:
    """A string value: newlines, tabs and backslashes escaped."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def installation() -> dict:
    """What the entry is about, from this file's location and devsys.json."""
    project_root = cfgmod.project_root(SYSTEM)
    name = str(cfgmod.get("project.name", SYSTEM, "") or project_root.name)
    if name.startswith("<"):                           # the unconfigured template's placeholder
        name = project_root.name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    digest = hashlib.sha1(str(SYSTEM).encode()).hexdigest()[:8]
    return {"system": SYSTEM, "project_root": project_root, "project": name,
            "entry_id": f"devsys-worker-control-{slug}-{digest}", "launcher": SYSTEM / "tools" / "worker-panel"}


def entry_text(info: dict) -> str:
    where = info["project_root"] if info["project_root"] != info["system"] else info["system"]
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.5",
        f"Name={_value(APP_NAME)} ({_value(info['project'])})",
        "GenericName=Worker Control Panel",
        f"Comment={_value(f'Workers, queue and requests of the development system in {where}')}",
        f"Exec={_quote(str(info['launcher']))}",
        f"TryExec={_quote(str(info['launcher']))}",
        f"Path={_value(str(info['system']))}",
        f"Icon={ICON}",
        "Terminal=false",
        "Categories=Development;",
        "Keywords=workers;requests;queue;development;",
        "StartupNotify=true",
        f"StartupWMClass={WM_CLASS}",
        f"{KEY}={_value(str(info['system']))}",
        "",
    ])


def menu_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"


def desktop_dir() -> Path | None:
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    path = Path(out) if out else Path.home() / "Desktop"
    return path if path.is_dir() and path != Path.home() else None


def _validate(path: Path) -> str | None:
    tool = shutil.which("desktop-file-validate")
    if not tool:
        return None
    r = subprocess.run([tool, str(path)], capture_output=True, text=True)
    problems = [l for l in (r.stdout + r.stderr).splitlines() if "error:" in l]
    return "; ".join(problems) or None


def install(menu: bool = True, desktop: bool = False) -> list[str]:
    info = installation()
    if not os.access(info["launcher"], os.X_OK):
        raise SystemExit(f"{info['launcher']} is missing or not executable")
    text = entry_text(info)
    done = []
    targets = []
    if menu:
        targets.append(menu_dir() / f"{info['entry_id']}.desktop")
    if desktop:
        d = desktop_dir()
        if d is None:
            raise SystemExit("no desktop directory (xdg-user-dir DESKTOP)")
        targets.append(d / f"{info['entry_id']}.desktop")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".desktop.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o755)                      # a desktop icon view launches only executable entries
        problem = _validate(tmp)
        if problem:
            tmp.unlink()
            raise SystemExit(f"desktop-file-validate rejected the entry: {problem}")
        os.replace(tmp, target)
        done.append(f"wrote {target}")
        if target.parent != menu_dir() and shutil.which("gio"):
            # GNOME's desktop icons launch an entry only once it is trusted.
            r = subprocess.run(["gio", "set", str(target), "metadata::trusted", "true"], capture_output=True, text=True)
            done.append("marked it trusted" if r.returncode == 0 else f"could not mark it trusted: {r.stderr.strip()}")
    if menu and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(menu_dir())], capture_output=True)
    return done


def uninstall() -> list[str]:
    """Remove every entry that launches this installation."""
    done = []
    for d in (menu_dir(), desktop_dir()):
        if d is None or not d.is_dir():
            continue
        for f in sorted(d.glob("devsys-worker-control-*.desktop")):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            if f"\n{KEY}={_value(str(SYSTEM))}\n" in text:
                f.unlink()
                done.append(f"removed {f}")
    return done or ["no launcher of this installation found"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("install", help="write the application-menu entry (and optionally a desktop icon)")
    ins.add_argument("--desktop", action="store_true", help="also put the launcher on the desktop")
    ins.add_argument("--no-menu", action="store_true", help="no application-menu entry")
    sub.add_parser("uninstall", help="remove this installation's entries")
    sub.add_parser("show", help="print the entry for this installation")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        info = installation()
        print(f"# {info['entry_id']}.desktop")
        print(entry_text(info), end="")
        return 0
    lines = install(menu=not a.no_menu, desktop=a.desktop) if a.cmd == "install" else uninstall()
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
