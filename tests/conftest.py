"""Shared pytest setup.

Tests run on invented data through the fakes in tests/fakes.py. The guard below
turns "no network in tests" from a convention into a failure: any attempt to open
a socket connection raises.
"""

import socket

import pytest


def _refuse_connection(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("tests must not use the network; use the fakes in tests/fakes.py")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _refuse_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse_connection)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="rewrite tests/golden/ from the demo generator and derive, then check it",
    )
