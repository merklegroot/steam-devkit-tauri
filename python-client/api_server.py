#!/usr/bin/env python3
"""Headless HTTP API for the SteamOS Devkit client (Tauri frontend)."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import logging
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Mock GUI-only dependencies before importing gui2.
for _mod in ("sdl2", "imgui", "OpenGL", "OpenGL.GL"):
    sys.modules[_mod] = MagicMock()
sys.modules["imgui.integrations"] = MagicMock()
sys.modules["imgui.integrations.sdl2"] = MagicMock()

CLIENT_ROOT = Path(__file__).resolve().parent / "client"
sys.path.insert(0, str(CLIENT_ROOT))

import devkit_client
import devkit_client.proxy
import signalslot
import zeroconf

from devkit_client.gui2.gui2 import (
    Devkit,
    DevkitCommands,
    DevkitNoConnectivity,
    DevkitNotRegistered,
    DevkitReleased,
    DevkitState,
    Settings,
)

logger = logging.getLogger(__name__)

API_HOST = "127.0.0.1"
API_PORT = 32100


class SimpleConf:
    """Minimal stand-in for gui2 argparse namespace."""

    def __init__(self, check_port_timeout: int = 4):
        self.verbose = "INFO"
        self.logfile = None
        self.valve = False
        self.check_port_timeout = check_port_timeout
        self.disable_popen_capture = False


def devkit_to_dict(devkit: Devkit) -> dict[str, Any]:
    machine = devkit.machine
    return {
        "name": devkit.name,
        "full_name": devkit.full_name,
        "state": devkit.state.name,
        "address": devkit.address or (machine.address if machine else None),
        "http_port": devkit.http_port,
        "added_by_ip": devkit.added_by_ip,
        "has_mdns_service": devkit.has_mdns_service(),
        "ssh_connectivity": devkit.ssh_connectivity,
        "http_connectivity": devkit.http_connectivity,
        "limited_connectivity": devkit.limited_connectivity,
        "guest_lan": devkit.guest_lan,
        "is_steamos": devkit.is_steamOS if devkit.state == DevkitState.devkit_online else False,
        "steamos_status": devkit.steamos_status,
        "steam_client_status": devkit.steam_client_status
        if devkit.state == DevkitState.devkit_online
        else None,
        "steam_configuration": devkit.steam_configuration
        if devkit.state == DevkitState.devkit_online
        else None,
        "os_name": devkit.os_name,
        "os_version": devkit.os_version,
        "user_password_is_set": devkit.user_password_is_set
        if devkit.state == DevkitState.devkit_online
        else None,
        "cef_debugging_enabled": devkit.cef_debugging_enabled
        if devkit.state == DevkitState.devkit_online
        else None,
        "machine_login": machine.login if machine else None,
    }


class DevkitManager:
    """Headless devkit discovery and lifecycle (DevkitsWindow without ImGui)."""

    def __init__(self, conf: SimpleConf, devkit_commands: DevkitCommands, settings: Settings):
        self.conf = conf
        self.devkit_commands = devkit_commands
        self.settings = settings
        self.zc: zeroconf.Zeroconf | None = None
        self.zc_listener: devkit_client.ServiceListener | None = None
        self.zc_browser: zeroconf.ServiceBrowser | None = None
        self.devkits: collections.OrderedDict[str, Devkit] = collections.OrderedDict()
        self._selected_devkit_name: str | None = None
        self.preferred_devkit_name = settings.get("DevkitsWindow.preferred_devkit_name", None)
        self._lock = threading.RLock()
        self.pending_tasks: dict[str, concurrent.futures.Future] = {}

    def setup(self) -> None:
        devkit_client.proxy.disable_proxy()
        self.zc = zeroconf.Zeroconf()
        self.zc_listener = devkit_client.ServiceListener(self.zc)
        self.zc_browser = zeroconf.ServiceBrowser(
            self.zc,
            devkit_client.STEAM_DEVKIT_TYPE,
            self.zc_listener,
        )
        devkits_by_ip = self.settings.get(Devkit.ADDED_BY_IP_KEY, set())
        for address in devkits_by_ip:
            addr = address
            port = devkit_client.DEFAULT_DEVKIT_SERVICE_HTTP
            if isinstance(address, tuple):
                addr, port = address
            devkit = Devkit(
                self.devkit_commands,
                self.settings,
                address=addr,
                port=port,
            )
            devkit.setup()
            self.devkits[devkit.name] = devkit
        self.settings.save_settings()

    def shutdown(self) -> None:
        if self.zc is not None:
            self.zc.close()
            self.zc = None

    def tick(self) -> None:
        with self._lock:
            if self.zc_listener is None:
                return
            while not self.zc_listener.devkit_events.empty():
                op, service_name = self.zc_listener.devkit_events.get()
                if op == "add":
                    devkit = Devkit(
                        self.devkit_commands,
                        self.settings,
                        zc_listener=self.zc_listener,
                        service_name=service_name,
                    )
                    devkit.setup()
                    self.devkits[devkit.name] = devkit
                elif op == "update":
                    pass
                else:
                    assert op == "del"
                    self.devkits.pop(service_name, None)

            online_kits = [
                k for k in self.devkits.values() if k.state == DevkitState.devkit_online
            ]
            self.selected_devkit  # validate selection
            for kit in online_kits:
                if self._selected_devkit_name is None:
                    self._selected_devkit_name = kit.name
                if (
                    self._selected_devkit_name != kit.name
                    and kit.name == self.preferred_devkit_name
                ):
                    self._selected_devkit_name = kit.name

    @property
    def selected_devkit(self) -> Devkit | None:
        if self._selected_devkit_name is None:
            return None
        if self._selected_devkit_name not in self.devkits:
            self._selected_devkit_name = None
            return None
        devkit = self.devkits[self._selected_devkit_name]
        if devkit.state != DevkitState.devkit_online:
            self._selected_devkit_name = None
            return None
        return devkit

    def select_devkit(self, name: str | None) -> Devkit | None:
        with self._lock:
            if name is None:
                self._selected_devkit_name = None
                return None
            if name not in self.devkits:
                raise KeyError(f"Unknown devkit: {name}")
            kit = self.devkits[name]
            if kit.state != DevkitState.devkit_online:
                raise ValueError(f"Devkit {name!r} is not online")
            self._selected_devkit_name = name
            self.preferred_devkit_name = name
            self.settings["DevkitsWindow.preferred_devkit_name"] = name
            self.settings.save_settings()
            return kit

    def get_devkit(self, name: str) -> Devkit:
        if name not in self.devkits:
            raise KeyError(f"Unknown devkit: {name}")
        return self.devkits[name]

    def connect_by_ip(self, address: str, port: int | None = None) -> Devkit:
        port = port or devkit_client.DEFAULT_DEVKIT_SERVICE_HTTP
        with self._lock:
            devkit = Devkit(
                self.devkit_commands,
                self.settings,
                address=address,
                port=port,
            )
            devkit.setup()
            self.devkits[devkit.name] = devkit
            return devkit

    def forget_ip_devkit(self, name: str) -> None:
        with self._lock:
            kit = self.get_devkit(name)
            if not kit.added_by_ip:
                raise ValueError("Only IP-added devkits can be forgotten")
            if kit.init_future is not None:
                kit.init_future.cancel()
            kit.forget_added_by_ip()
            del self.devkits[name]
            if self._selected_devkit_name == name:
                self._selected_devkit_name = None

    def retry_devkit(self, name: str) -> None:
        kit = self.get_devkit(name)
        kit.state = DevkitState.devkit_init
        kit.setup()

    def register_devkit(self, name: str) -> concurrent.futures.Future:
        kit = self.get_devkit(name)
        if kit.state != DevkitState.devkit_not_registered:
            raise ValueError("Devkit is not awaiting registration")
        return kit.register()

    def list_devkits(self) -> list[dict[str, Any]]:
        self.tick()
        with self._lock:
            return [devkit_to_dict(k) for k in self.devkits.values()]

    def run_task(self, key: str, fn, *args, **kwargs) -> str:
        future = self.devkit_commands.executor.submit(fn, *args, **kwargs)
        self.pending_tasks[key] = future
        return key

    def task_status(self, key: str) -> dict[str, Any]:
        future = self.pending_tasks.get(key)
        if future is None:
            raise KeyError(f"Unknown task: {key}")
        if not future.done():
            return {"key": key, "done": False}
        exc = future.exception()
        if exc is not None:
            return {"key": key, "done": True, "ok": False, "error": str(exc)}
        try:
            result = future.result()
            if result is None:
                payload = True
            elif isinstance(result, (dict, list, str, int, float, bool)):
                payload = result
            else:
                payload = str(result)
            return {"key": key, "done": True, "ok": True, "result": payload}
        except Exception as e:
            return {"key": key, "done": True, "ok": False, "error": str(e)}


class AppState:
    def __init__(self):
        self.shutdown_signal = signalslot.Signal()
        self.conf = SimpleConf()
        self.settings = Settings()
        self.devkit_commands = DevkitCommands(self.conf, self.shutdown_signal)
        self.manager = DevkitManager(self.conf, self.devkit_commands, self.settings)

    def setup(self) -> None:
        self.devkit_commands.setup()
        self.manager.setup()

    def shutdown(self) -> None:
        self.shutdown_signal.emit()
        self.manager.shutdown()
        self.settings.shutdown()


g_app: AppState | None = None


def get_app() -> AppState:
    if g_app is None:
        raise RuntimeError("API server not initialized")
    return g_app


class APIHandler(BaseHTTPRequestHandler):
    server_version = "SteamDevkitTauri/1.0"

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8"))

    def _respond(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._respond({"error": message}, status=status)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        app = get_app()
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/health":
                self._respond({"ok": True, "version": devkit_client.__version__})
                return
            if path == "/api/devkits":
                self._respond({"devkits": app.manager.list_devkits()})
                return
            if path == "/api/selected":
                kit = app.manager.selected_devkit
                self._respond(
                    {"devkit": devkit_to_dict(kit) if kit else None}
                )
                return
            if path.startswith("/api/devkits/") and path.endswith("/games"):
                name = path.split("/")[3]
                kit = app.manager.get_devkit(name)
                if kit.state != DevkitState.devkit_online:
                    self._error(HTTPStatus.BAD_REQUEST, "Devkit not online")
                    return

                class ListGamesArgs:
                    def __init__(self, devkit):
                        self.machine, self.machine_name_type = (
                            devkit.machine_command_args
                        )
                        self.http_port = devkit.http_port
                        self.login = None

                games = devkit_client.list_games(ListGamesArgs(kit))
                self._respond({"games": games})
                return
            if path.startswith("/api/tasks/"):
                key = path.split("/")[-1]
                self._respond(app.manager.task_status(key))
                return
            self._error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as e:
            logger.exception("GET %s failed", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def do_POST(self) -> None:
        app = get_app()
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        try:
            if path == "/api/devkits/connect":
                address = body.get("address", "").strip()
                if not address:
                    self._error(HTTPStatus.BAD_REQUEST, "address required")
                    return
                port = body.get("port")
                port = int(port) if port is not None else None
                devkit = app.manager.connect_by_ip(address, port)
                self._respond({"devkit": devkit_to_dict(devkit)})
                return

            if path == "/api/selected":
                name = body.get("name")
                kit = app.manager.select_devkit(name)
                self._respond(
                    {"devkit": devkit_to_dict(kit) if kit else None}
                )
                return

            if path.startswith("/api/devkits/"):
                parts = path.strip("/").split("/")
                # api, devkits, {name}, {action}
                if len(parts) < 4:
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                name = parts[2]
                action = parts[3]
                kit = app.manager.get_devkit(name)

                if action == "register":
                    future = app.manager.register_devkit(name)
                    key = f"register:{name}:{time.time_ns()}"
                    app.manager.pending_tasks[key] = future
                    self._respond({"task": key})
                    return

                if action == "forget":
                    app.manager.forget_ip_devkit(name)
                    self._respond({"ok": True})
                    return

                if action == "retry":
                    app.manager.retry_devkit(name)
                    self._respond({"devkit": devkit_to_dict(kit)})
                    return

                if action == "refresh-status":
                    key = app.manager.run_task(
                        f"status:{name}",
                        app.devkit_commands._steamos_get_status,
                        kit,
                    )
                    self._respond({"task": key})
                    return

                if kit.state != DevkitState.devkit_online:
                    self._error(HTTPStatus.BAD_REQUEST, "Devkit not online")
                    return

                if action == "remote-shell":
                    key = app.manager.run_task(
                        f"shell:{name}",
                        app.devkit_commands._open_remote_shell,
                        kit,
                    )
                    self._respond({"task": key})
                    return

                if action == "restart-session":
                    key = app.manager.run_task(
                        f"restart:{name}",
                        app.devkit_commands._restart_session,
                        kit,
                    )
                    self._respond({"task": key})
                    return

                if action == "cef-console":
                    key = app.manager.run_task(
                        f"cef:{name}",
                        app.devkit_commands._open_cef_console,
                        kit,
                    )
                    self._respond({"task": key})
                    return

                if action == "screenshot":
                    folder = body.get("folder") or str(Path.home() / "Pictures")
                    filename = body.get("filename") or "screenshot.png"
                    key = app.manager.run_task(
                        f"screenshot:{name}",
                        app.devkit_commands._screenshot,
                        kit,
                        folder,
                        filename,
                        body.get("do_timestamp", True),
                        body.get("xprop", False),
                    )
                    self._respond({"task": key})
                    return

                if action == "sync-logs":
                    folder = body.get("folder") or str(
                        Path.home() / "devkit-logs"
                    )
                    key = app.manager.run_task(
                        f"logs:{name}",
                        app.devkit_commands._sync_logs,
                        kit,
                        folder,
                    )
                    self._respond({"task": key})
                    return

            self._error(HTTPStatus.NOT_FOUND, "Not found")
        except KeyError as e:
            self._error(HTTPStatus.NOT_FOUND, str(e))
        except ValueError as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:
            logger.exception("POST %s failed", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))


def tick_loop(app: AppState, stop: threading.Event) -> None:
    while not stop.is_set():
        app.manager.tick()
        stop.wait(0.5)


def main() -> None:
    global g_app
    parser = argparse.ArgumentParser(description="Steam Devkit Tauri API")
    parser.add_argument("--host", default=API_HOST)
    parser.add_argument("--port", type=int, default=API_PORT)
    parser.add_argument("--verbose", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.verbose.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    g_app = AppState()
    g_app.setup()

    stop = threading.Event()
    ticker = threading.Thread(target=tick_loop, args=(g_app, stop), daemon=True)
    ticker.start()

    server = ThreadingHTTPServer((args.host, args.port), APIHandler)
    logger.info("Steam Devkit API listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        g_app.shutdown()


if __name__ == "__main__":
    main()
