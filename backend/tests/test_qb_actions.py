import pytest

from app.adapters.qbittorrent.client import QbittorrentApiError, QbittorrentWebAdapter


def qb_adapter() -> QbittorrentWebAdapter:
    return QbittorrentWebAdapter({"base_url": "http://qb.local:8080", "username": "user", "password": "password"})


def test_pause_falls_back_to_qb5_stop_and_verifies_state(monkeypatch):
    adapter = qb_adapter()
    commands: list[str] = []

    def text_request(_method, path, **_kwargs):
        commands.append(path)
        if path.endswith("/pause"):
            raise QbittorrentApiError("not found", 404)
        return ""

    monkeypatch.setattr(adapter, "_text_request", text_request)
    monkeypatch.setattr(adapter, "_json_request", lambda *_args, **_kwargs: [{"state": "stoppedDL"}])

    result = adapter.mutate_torrent("qb1", "abc", "pause", {})

    assert commands == ["/api/v2/torrents/pause", "/api/v2/torrents/stop"]
    assert result["verified"] is True
    assert result["state"] == "stoppedDL"


def test_resume_falls_back_to_qb5_start_and_verifies_seeding(monkeypatch):
    adapter = qb_adapter()
    commands: list[str] = []

    def text_request(_method, path, **_kwargs):
        commands.append(path)
        if path.endswith("/resume"):
            raise QbittorrentApiError("not found", 404)
        return ""

    monkeypatch.setattr(adapter, "_text_request", text_request)
    monkeypatch.setattr(adapter, "_json_request", lambda *_args, **_kwargs: [{"state": "stalledUP"}])

    result = adapter.mutate_torrent("qb1", "abc", "resume", {})

    assert commands == ["/api/v2/torrents/resume", "/api/v2/torrents/start"]
    assert result["verified"] is True
    assert result["state"] == "stalledUP"


def test_pause_reports_when_qb_accepts_request_without_changing_state(monkeypatch):
    adapter = qb_adapter()
    monkeypatch.setattr(adapter, "_text_request", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(adapter, "_json_request", lambda *_args, **_kwargs: [{"state": "downloading"}])
    monkeypatch.setattr("app.adapters.qbittorrent.client.time.sleep", lambda _seconds: None)

    with pytest.raises(QbittorrentApiError, match="操作未确认生效"):
        adapter.mutate_torrent("qb1", "abc", "pause", {})


def test_delete_files_sends_destructive_qb_flag(monkeypatch):
    adapter = qb_adapter()
    requests: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        adapter,
        "_text_request",
        lambda _method, path, form=None, **_kwargs: requests.append((path, form or {})) or "",
    )

    adapter.mutate_torrent("qb1", "abc", "delete_files", {"delete_files": True})

    assert requests == [("/api/v2/torrents/delete", {"hashes": "abc", "deleteFiles": "true"})]
