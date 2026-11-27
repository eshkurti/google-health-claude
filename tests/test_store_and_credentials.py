import json

import pytest

from google_health_claude.api.credentials import CredentialsError, load_credentials
from google_health_claude.store import merge_items, write_if_changed


def test_write_if_changed(tmp_path):
    path = tmp_path / "a" / "b.json"
    assert write_if_changed(path, {"b": 1, "a": 2}) is True
    assert write_if_changed(path, {"a": 2, "b": 1}) is False  # same content, different key order
    assert write_if_changed(path, {"a": 3}) is True
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "refresh")
    creds = load_credentials()
    assert (creds.client_id, creds.client_secret, creds.refresh_token) == (
        "id",
        "secret",
        "refresh",
    )


def test_credentials_from_files(tmp_path, monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}))
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "r"}))
    creds = load_credentials(secret, token)
    assert (creds.client_id, creds.client_secret, creds.refresh_token) == ("id", "s", "r")


def test_missing_credentials_are_reported(tmp_path, monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CredentialsError):
        load_credentials(None, tmp_path / "nope.json")


@pytest.mark.parametrize(
    ("secret_body", "token_body"),
    [
        ({"installed": {"client_id": "id"}}, {"refresh_token": "r"}),  # no client_secret
        ({"installed": ["id", "s"]}, {"refresh_token": "r"}),  # section is not an object
        ({"installed": {"client_id": 1, "client_secret": "s"}}, {"refresh_token": "r"}),
        ([], {"refresh_token": "r"}),  # not a JSON object at all
        ({"installed": {"client_id": "id", "client_secret": "s"}}, ["r"]),
        ({"installed": {"client_id": "id", "client_secret": "s"}}, {"refresh_token": 5}),
    ],
    ids=["no-secret", "section-list", "id-number", "secret-list", "token-list", "token-number"],
)
def test_malformed_credential_files_are_credentials_errors(
    tmp_path, monkeypatch, secret_body, token_body
):
    """The CLI turns CredentialsError into one line; anything else would be a traceback."""
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps(secret_body))
    token = tmp_path / "token.json"
    token.write_text(json.dumps(token_body))
    with pytest.raises(CredentialsError):
        load_credentials(secret, token)


def test_write_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "d" / "x.json"
    write_if_changed(path, {"a": 1})
    write_if_changed(path, {"a": 2})
    assert sorted(p.name for p in path.parent.iterdir()) == ["x.json"]


def test_merge_items_keeps_stored_items_and_replaces_by_name():
    old = [{"name": "s/1", "v": 1}, {"name": "s/2", "v": 2}]
    new = [{"name": "s/2", "v": 20}, {"name": "s/3", "v": 3}]
    assert merge_items(old, new) == [
        {"name": "s/1", "v": 1},
        {"name": "s/2", "v": 20},
        {"name": "s/3", "v": 3},
    ]
    assert merge_items(old, old) == old  # stable: re-merging changes nothing


def test_merge_items_without_names_uses_content_identity():
    assert merge_items([{"v": 1}], [{"v": 1}, {"v": 2}]) == [{"v": 1}, {"v": 2}]
