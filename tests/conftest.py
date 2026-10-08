"""Keep ordinary tests isolated from external services; loopback is allowed."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def offline_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("eval") is not None:
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def check_address(sock: socket.socket, address: Any) -> None:
        if sock.family == socket.AF_UNIX:
            return
        host = address[0]
        if host == "localhost":
            return
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise AssertionError("External network access is forbidden in offline tests")

    def local_connect(sock: socket.socket, address: Any) -> None:
        check_address(sock, address)
        connect(sock, address)

    def local_connect_ex(sock: socket.socket, address: Any) -> int:
        check_address(sock, address)
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
