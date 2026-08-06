"""Shared test guards.

The one rule enforced here: a test may not talk to the network.

This is not hypothetical tidiness. `test_lmstudio_test_separates_chat_models_
from_embedding_models` monkeypatched the health probe but not the classify
round trip, so it quietly opened a socket to 192.168.145.36 — a real LM Studio
server on the author's LAN. It passed at that desk and nowhere else, and on CI
it spent seventy-five seconds waiting for a connection that was never coming
before failing on an assertion that had nothing to do with the cause.

An outbound connection now fails loudly, immediately, and names the address, so
the next one is a five-second diagnosis instead of an afternoon.
"""

from __future__ import annotations

import socket

import pytest

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex

# Loopback stays open: it is not the network, and anyio's own plumbing uses it
# on platforms without a socketpair.
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}  # noqa: S104


class NetworkUseInTest(AssertionError):
    """A test tried to reach the network. Monkeypatch the client instead."""


def _is_local(address: object) -> bool:
    # AF_UNIX addresses are plain strings; nothing to police there.
    if not isinstance(address, tuple) or not address:
        return True
    return str(address[0]) in _LOCAL_HOSTS


def _blocked(address: object) -> NetworkUseInTest:
    return NetworkUseInTest(
        f"test opened a connection to {address!r}. Tests must not touch the "
        "network — patch the client function instead, or mark the test with "
        "@pytest.mark.allow_network if it genuinely has to."
    )


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("allow_network"):
        return

    def guard(self, address):  # noqa: ANN001, ANN202
        if _is_local(address):
            return _real_connect(self, address)
        raise _blocked(address)

    def guard_ex(self, address):  # noqa: ANN001, ANN202
        if _is_local(address):
            return _real_connect_ex(self, address)
        raise _blocked(address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_ex)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "allow_network: this test is allowed to make real connections"
    )
