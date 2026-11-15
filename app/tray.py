"""Desktop tray companion for locally scheduled Lovable workflows."""
import socket
import threading
import webbrowser
import os
from pathlib import Path

import uvicorn

from .system_scheduler import install, uninstall


def _server_is_up() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", 8000)) == 0


def _serve() -> None:
    os.chdir(Path(__file__).resolve().parent.parent)
    if not _server_is_up():
        uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="warning")


def main() -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise SystemExit("Install desktop dependencies with: pip install -r requirements.txt") from exc

    threading.Thread(target=_serve, daemon=True, name="lovable-api").start()
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill="#635bff")
    draw.text((17, 17), "L", fill="white")
    def open_dashboard(_icon, _item):
        webbrowser.open("http://127.0.0.1:8000/")
    def enable(_icon, _item):
        install()
        from .main import get_workflows
        service = get_workflows()
        for job in service.scheduler.get_jobs():
            service.scheduler.remove_job(job.id)
    def disable(_icon, _item):
        uninstall()
    menu = pystray.Menu(
        pystray.MenuItem("Open workflow dashboard", open_dashboard, default=True),
        pystray.MenuItem("Enable startup and scheduled runs", enable),
        pystray.MenuItem("Disable startup and scheduled runs (restart app)", disable),
        pystray.MenuItem("Quit tray", lambda icon, _item: icon.stop()),
    )
    icon = pystray.Icon("lovable-workflows", image, "Lovable Workflows", menu)
    print(
        "Lovable Workflow Tray is running. The process stays open to keep the tray icon active; "
        "use the tray menu's Quit option to stop it. The icon may be under the system tray overflow arrow.",
        flush=True,
    )
    icon.run()


if __name__ == "__main__":
    main()
