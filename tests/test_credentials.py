import json
from unittest.mock import Mock

import pytest

from spotify2ytmusic import backend, ytmusic_credentials


def test_browser_headers_use_untracked_local_filename(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "raw_headers.local.txt").write_text("test headers", encoding="utf-8")
    setup = Mock(return_value="configuration")
    monkeypatch.setattr(ytmusic_credentials.ytmusicapi, "setup", setup)
    assert ytmusic_credentials.setup_ytmusic_with_raw_headers() == "configuration"
    setup.assert_called_once_with(filepath="oauth.json", headers_raw="test headers")


def test_header_auth_loads_without_custom_oauth_client(monkeypatch, tmp_path):
    auth = tmp_path / "headers.json"
    auth.write_text(json.dumps({"cookie": "test-value"}), encoding="utf-8")
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", str(auth))
    client = Mock()
    monkeypatch.setattr(backend, "YTMusic", client)
    backend.get_ytmusic()
    client.assert_called_once_with(str(auth))


def test_oauth_tokens_require_custom_client_configuration(monkeypatch, tmp_path):
    auth = tmp_path / "oauth.json"
    auth.write_text(json.dumps({"access_token": "test-token"}), encoding="utf-8")
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", str(auth))
    monkeypatch.delenv("YTMUSIC_CLIENT_ID", raising=False)
    monkeypatch.delenv("YTMUSIC_CLIENT_SECRET", raising=False)
    with pytest.raises(ValueError, match="YTMUSIC_CLIENT_ID"):
        backend.get_ytmusic()
