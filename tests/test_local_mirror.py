"""
Tests for the local AORC mirror system.

Tests the download, load, manifest tracking, and sync functionality
without requiring S3 access (uses mocked data for unit tests).
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from aorc_tools.local_mirror import LocalAORCMirror


@pytest.fixture
def points_df():
    """Small test points DataFrame."""
    return pd.DataFrame({
        "point_id": [1, 2, 3],
        "latitude": [46.5, 46.6, 46.7],
        "longitude": [-88.5, -88.4, -88.3],
        "lat_idx": [100, 101, 102],
        "lon_idx": [200, 201, 202],
    })


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def mock_loader():
    """Mock AORCDataLoader that returns synthetic data."""
    loader = MagicMock()
    loader.VARIABLES = [
        "TMP_2maboveground", "SPFH_2maboveground", "PRES_surface",
        "UGRD_10maboveground", "VGRD_10maboveground", "APCP_surface",
        "DSWRF_surface", "DLWRF_surface",
    ]

    def make_multiday_data(year, month, start_day, end_day, *args, **kwargs):
        result = {}
        for day in range(start_day, end_day + 1):
            timestamps = pd.date_range(
                f"{year}-{month:02d}-{day:02d} 00:00",
                f"{year}-{month:02d}-{day:02d} 23:00",
                freq="h",
            )
            daily = {
                "hours": list(range(24)),
                "timestamps": timestamps,
            }
            for var in loader.VARIABLES:
                daily[var] = np.random.rand(24, 3).astype(np.float32)
            result[day] = daily
        return result

    loader.get_multiday_point_values.side_effect = make_multiday_data
    return loader


class TestMirrorInit:
    def test_creates_directory(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        assert mirror_dir.exists()

    def test_empty_manifest(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        assert mirror._manifest["downloaded"] == {}

    def test_status_empty(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        status = mirror.status()
        assert status["months_downloaded"] == 0
        assert status["total_size_gb"] == 0
        assert status["n_points"] == 3


class TestDownloadMonth:
    def test_download_creates_npz(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        npz_path = mirror.download_month(2020, 7)

        assert npz_path.exists()
        assert npz_path.suffix == ".npz"

    def test_download_updates_manifest(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)

        assert mirror.is_downloaded(2020, 7)
        assert not mirror.is_downloaded(2020, 8)

    def test_download_skips_existing(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        call_count = mock_loader.get_multiday_point_values.call_count

        # Second download should skip
        mirror.download_month(2020, 7)
        assert mock_loader.get_multiday_point_values.call_count == call_count

    def test_download_force_redownloads(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        call_count = mock_loader.get_multiday_point_values.call_count

        mirror.download_month(2020, 7, force=True)
        assert mock_loader.get_multiday_point_values.call_count > call_count

    def test_npz_contains_expected_keys(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        npz_path = mirror.download_month(2020, 7)

        data = np.load(npz_path, allow_pickle=True)
        assert "timestamps" in data.files
        assert "TMP_2maboveground" in data.files
        assert "APCP_surface" in data.files

    def test_npz_shapes(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        npz_path = mirror.download_month(2020, 7)

        data = np.load(npz_path, allow_pickle=True)
        # July has 31 days × 24 hours = 744 hours
        assert len(data["timestamps"]) == 744
        assert data["TMP_2maboveground"].shape == (744, 3)


class TestLoadMonth:
    def test_load_returns_day_dict(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        result = mirror.load_month(2020, 7)

        # Should have entries for days 1-31
        assert len(result) == 31
        assert 1 in result
        assert 31 in result

    def test_load_day_structure(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        result = mirror.load_month(2020, 7)

        day1 = result[1]
        assert "hours" in day1
        assert "timestamps" in day1
        assert "TMP_2maboveground" in day1
        assert len(day1["hours"]) == 24

    def test_load_not_downloaded_raises(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        with pytest.raises(FileNotFoundError):
            mirror.load_month(2020, 7)

    def test_load_day(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        day_data = mirror.load_day(2020, 7, 15)

        assert "hours" in day_data
        assert len(day_data["hours"]) == 24


class TestDownloadRange:
    def test_download_multiple_months(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        count = mirror.download_range(2020, 2020, start_month=7, end_month=9)

        assert count == 3
        assert mirror.is_downloaded(2020, 7)
        assert mirror.is_downloaded(2020, 8)
        assert mirror.is_downloaded(2020, 9)

    def test_download_range_skips_existing(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        count = mirror.download_range(2020, 2020, start_month=7, end_month=9)

        # Only 2 new months (Aug, Sep)
        assert count == 2

    def test_callback_called(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        calls = []
        mirror.download_range(2020, 2020, start_month=7, end_month=7,
                              callback=lambda y, m, msg: calls.append((y, m, msg)))
        assert len(calls) == 1
        assert calls[0][0] == 2020
        assert calls[0][1] == 7


class TestManifestPersistence:
    def test_manifest_survives_reload(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)

        # Create new mirror instance (simulates restart)
        mirror2 = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        assert mirror2.is_downloaded(2020, 7)

    def test_manifest_json_valid(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)

        manifest_path = mirror_dir / "manifest.json"
        assert manifest_path.exists()
        with open(manifest_path) as f:
            data = json.load(f)
        assert "2020-07" in data["downloaded"]
        assert data["n_points"] == 3


class TestStatus:
    def test_status_after_downloads(self, mirror_dir, points_df, mock_loader):
        mirror = LocalAORCMirror(mirror_dir, points_df, loader=mock_loader)
        mirror.download_month(2020, 7)
        mirror.download_month(2020, 8)

        status = mirror.status()
        assert status["months_downloaded"] == 2
        assert status["years_covered"] == [2020]
        assert status["total_size_gb"] >= 0
        assert status["n_points"] == 3
        assert status["last_sync"] is not None
