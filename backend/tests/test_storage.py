import os
from collections import namedtuple

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from app.api import routes


DiskUsage = namedtuple("DiskUsage", "total used free")


def test_storage_dedupes_same_device(monkeypatch, tmp_path):
    first = tmp_path / "storage1"
    second = tmp_path / "storage2"
    first.mkdir()
    second.mkdir()

    monkeypatch.setattr(routes, "DEFAULT_STORAGE_MOUNT_PATHS", (str(first), str(second)))
    monkeypatch.setattr(routes.shutil, "disk_usage", lambda path: DiskUsage(1000, 400, 600))

    result = routes.nas_storage_from_configs(None)

    assert result["nas_storage_pool_count"] == 1
    assert result["nas_storage_folder_count"] == 2
    assert result["nas_total_space"] == 1000
    assert result["nas_used_space"] == 400


def test_storage_reports_primary_path_capacity_when_devices_differ(monkeypatch, tmp_path):
    first = tmp_path / "storage1"
    second = tmp_path / "storage2"
    first.mkdir()
    second.mkdir()

    monkeypatch.setattr(routes, "DEFAULT_STORAGE_MOUNT_PATHS", (str(first), str(second)))
    monkeypatch.setattr(routes, "_storage_disk_key", lambda path, raw_path: raw_path)
    monkeypatch.setattr(routes.shutil, "disk_usage", lambda path: DiskUsage(1000, 250, 750))

    result = routes.nas_storage_from_configs(None)

    assert result["nas_storage_pool_count"] == 2
    assert result["nas_storage_folder_count"] == 2
    assert result["nas_total_space"] == 1000
    assert result["nas_used_space"] == 250


def test_storage_requires_primary_path_for_capacity(monkeypatch, tmp_path):
    mounted = tmp_path / "storage2"
    missing_primary = tmp_path / "storage1"
    mounted.mkdir()

    monkeypatch.setattr(routes, "DEFAULT_STORAGE_MOUNT_PATHS", (str(missing_primary), str(mounted)))
    monkeypatch.setattr(routes.shutil, "disk_usage", lambda path: DiskUsage(1000, 100, 900))

    result = routes.nas_storage_from_configs(None)

    assert result["nas_storage_folder_count"] == 1
    assert result["nas_storage_readable"] is False
    assert result["nas_total_space"] == 0


def test_storage_skips_missing_paths(monkeypatch, tmp_path):
    mounted = tmp_path / "storage1"
    missing = tmp_path / "missing"
    mounted.mkdir()

    monkeypatch.setattr(routes, "DEFAULT_STORAGE_MOUNT_PATHS", (str(mounted), str(missing)))
    monkeypatch.setattr(routes.shutil, "disk_usage", lambda path: DiskUsage(1000, 100, 900))

    result = routes.nas_storage_from_configs(None)

    assert result["nas_storage_folder_count"] == 1
    assert result["nas_storage_detected_paths"] == [str(mounted)]
    assert result["nas_storage_errors"][0]["path"] == str(missing)


def test_storage_ignores_legacy_qb_mount_paths(monkeypatch, tmp_path):
    mounted = tmp_path / "storage1"
    mounted.mkdir()

    monkeypatch.setattr(routes, "DEFAULT_STORAGE_MOUNT_PATHS", (str(mounted),))
    monkeypatch.setattr(routes.shutil, "disk_usage", lambda path: DiskUsage(1000, 200, 800))

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("legacy qB storage config should not be read")

    monkeypatch.setattr(routes, "get_decrypted_config", fail_if_called)

    result = routes.nas_storage_from_configs(None)

    assert result["nas_storage_readable"] is True
    assert result["nas_storage_detected_paths"] == [str(mounted)]
