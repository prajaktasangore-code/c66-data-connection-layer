"""Redaction and telemetry hooks."""

from __future__ import annotations

import pytest

from enterprise_connectors import SecretStr
from enterprise_connectors.utils.logging import (
    ConnectionEvent,
    EventEmitter,
    redact,
    redact_mapping,
)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("host=db user=bob password=hunter2 dbname=x", "hunter2"),
        ("postgresql://bob:hunter2@db:5432/x", "hunter2"),
        ('{"client_secret": "hunter2"}', "hunter2"),
        ("{'password': 'hunter 2'}", "hunter 2"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("https://api/x?api_key=hunter2&page=1", "hunter2"),
        ("PG_PASSWORD=hunter2", "hunter2"),
        ("sslpassword=hunter2", "hunter2"),
    ],
)
def test_redact_masks_known_patterns(text: str, secret: str) -> None:
    assert secret not in redact(text)


def test_redact_keeps_non_secret_text() -> None:
    assert redact("host=db port=5432 user=bob") == "host=db port=5432 user=bob"


def test_redact_mapping() -> None:
    data = {"user": "bob", "password": "x", "nested": {"api_key": "y"}, "s": SecretStr("z"), "n": 1}
    assert redact_mapping(data) == {"user": "bob", "password": "***", "nested": {"api_key": "***"}, "s": "***", "n": 1}


def test_event_attributes_are_redacted() -> None:
    event = ConnectionEvent("x", "pg", attributes={"token": "abc", "tenant_id": "t"})
    assert event.attributes == {"token": "***", "tenant_id": "t"}


def test_emitter_add_remove_and_isolation() -> None:
    seen: list[str] = []
    emitter = EventEmitter()

    def hook(event: ConnectionEvent) -> None:
        seen.append(event.name)

    emitter.add_hook(lambda e: (_ for _ in ()).throw(RuntimeError("bad hook")))
    emitter.add_hook(hook)
    emitter.emit(ConnectionEvent("a", "pg"))
    emitter.remove_hook(hook)
    emitter.emit(ConnectionEvent("b", "pg"))
    assert seen == ["a"]
