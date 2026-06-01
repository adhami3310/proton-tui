from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs

import pytest

from pvpn_tui.config import QBittorrentConfig
from pvpn_tui.qbittorrent import QBittorrentError, push_listen_port


class _FakeQbtHandler(BaseHTTPRequestHandler):
    server: _FakeQbtServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence the per-request stderr line.
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        params = {k: v[0] for k, v in parse_qs(body).items()}

        if self.path == "/api/v2/auth/login":
            self.server.login_calls.append(params)
            ok = (
                params.get("username") == self.server.expected_user
                and params.get("password") == self.server.expected_pass
            )
            if self.server.api_version >= 5:
                # qBittorrent >= 5: 204 (empty) on success, 401 on failure.
                if ok:
                    self.send_response(204)
                    self.send_header("Set-Cookie", "SID=fake; path=/; HttpOnly")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    response = b"Unauthorized"
                    self.send_response(401)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                return
            # qBittorrent < 5: 200 with "Ok." / "Fails." in the body.
            response = b"Ok." if ok else b"Fails."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            if ok:
                self.send_header("Set-Cookie", "SID=fake; path=/; HttpOnly")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if self.path == "/api/v2/app/setPreferences":
            self.server.set_pref_calls.append(params)
            cookie = self.headers.get("Cookie", "")
            if "SID=fake" not in cookie:
                self.send_response(403)
                self.end_headers()
                return
            payload = json.loads(params.get("json", "{}"))
            self.server.last_listen_port = payload.get("listen_port")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()


class _FakeQbtServer(HTTPServer):
    def __init__(
        self, expected_user: str, expected_pass: str, api_version: int
    ) -> None:
        super().__init__(("127.0.0.1", 0), _FakeQbtHandler)
        self.expected_user = expected_user
        self.expected_pass = expected_pass
        self.api_version = api_version
        self.login_calls: list[dict[str, str]] = []
        self.set_pref_calls: list[dict[str, str]] = []
        self.last_listen_port: int | None = None


@pytest.fixture(params=[4, 5], ids=["qbt4", "qbt5"])
def fake_qbt(request: Any) -> Any:
    server = _FakeQbtServer(
        expected_user="admin", expected_pass="hunter2", api_version=request.param
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _cfg(
    server: _FakeQbtServer, user: str = "admin", pw: str = "hunter2"
) -> QBittorrentConfig:
    addr = server.server_address
    return QBittorrentConfig(
        url=f"http://{addr[0]}:{addr[1]}",
        username=user,
        password=pw,
    )


async def test_push_listen_port_succeeds(fake_qbt: _FakeQbtServer) -> None:
    await push_listen_port(_cfg(fake_qbt), 51820)
    assert fake_qbt.last_listen_port == 51820
    assert len(fake_qbt.login_calls) == 1
    assert len(fake_qbt.set_pref_calls) == 1


async def test_push_listen_port_bad_credentials(fake_qbt: _FakeQbtServer) -> None:
    with pytest.raises(QBittorrentError, match="login refused"):
        await push_listen_port(_cfg(fake_qbt, pw="wrong"), 51820)
    assert fake_qbt.last_listen_port is None
    assert fake_qbt.set_pref_calls == []


async def test_push_listen_port_unreachable() -> None:
    cfg = QBittorrentConfig(
        url="http://127.0.0.1:1",  # nothing listens here
        username="x",
        password="y",
    )
    with pytest.raises(QBittorrentError, match="login failed"):
        await push_listen_port(cfg, 51820)
