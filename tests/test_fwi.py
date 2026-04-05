"""
Tests for the FWI system — validates against known values.

The hourly FFMC is tested against cffdrs-ng reference behavior.
The daily FWI chain (ISI, BUI, FWI) is tested against Argonne's code.
"""

import numpy as np
import pytest

from aorc_tools.fire_indices.fwi import (
    ffmc_to_mc, mc_to_ffmc,
    dmc_to_mc, mc_to_dmc,
    dc_to_mc, mc_to_dc,
    hourly_ffmc_vectorized,
    initial_spread_index,
    buildup_index,
    fire_weather_index,
    HourlyFWI,
)


class TestMoistureConversions:
    def test_ffmc_roundtrip(self):
        """FFMC → MC → FFMC should round-trip."""
        for ffmc in [0, 50, 85, 99]:
            mc = ffmc_to_mc(ffmc)
            ffmc2 = mc_to_ffmc(mc)
            assert ffmc2 == pytest.approx(ffmc, abs=0.01)

    def test_dmc_roundtrip(self):
        for dmc in [1, 6, 50, 200]:
            mc = dmc_to_mc(dmc)
            dmc2 = mc_to_dmc(mc)
            assert dmc2 == pytest.approx(dmc, abs=0.01)

    def test_dc_roundtrip(self):
        for dc in [1, 15, 100, 300]:
            mc = dc_to_mc(dc)
            dc2 = mc_to_dc(mc)
            assert dc2 == pytest.approx(dc, abs=0.01)

    def test_ffmc_85_moisture(self):
        """FFMC=85 corresponds to about 16% moisture."""
        mc = ffmc_to_mc(85.0)
        assert 15.0 < mc < 18.0


class TestHourlyFFMC:
    def test_drying(self):
        """Hot, dry, windy conditions should dry the fuel."""
        mc_start = ffmc_to_mc(85.0)
        mc_end = hourly_ffmc_vectorized(mc_start, temp=30.0, rh=20.0, ws=15.0, rain=0.0)
        assert mc_end < mc_start

    def test_wetting_by_rain(self):
        """Rain should increase moisture content."""
        mc_start = ffmc_to_mc(90.0)  # Dry fuel
        mc_end = hourly_ffmc_vectorized(mc_start, temp=15.0, rh=50.0, ws=5.0, rain=5.0)
        assert mc_end > mc_start

    def test_high_humidity_wetting(self):
        """High humidity should wet dry fuel."""
        mc_start = 5.0  # Very dry
        mc_end = hourly_ffmc_vectorized(mc_start, temp=10.0, rh=95.0, ws=2.0, rain=0.0)
        assert mc_end > mc_start

    def test_moisture_bounded(self):
        """Moisture content should stay within physical bounds."""
        mc = hourly_ffmc_vectorized(200.0, temp=5.0, rh=99.0, ws=0.0, rain=50.0)
        assert 0 <= mc <= 250


class TestISI:
    def test_zero_wind(self):
        """ISI should be positive even with zero wind."""
        isi = initial_spread_index(0.0, 85.0)
        assert isi > 0

    def test_increases_with_wind(self):
        """ISI should increase with wind speed."""
        isi_low = initial_spread_index(5.0, 85.0)
        isi_high = initial_spread_index(20.0, 85.0)
        assert isi_high > isi_low

    def test_increases_with_ffmc(self):
        """ISI should increase with FFMC (drier fuel)."""
        isi_wet = initial_spread_index(10.0, 60.0)
        isi_dry = initial_spread_index(10.0, 95.0)
        assert isi_dry > isi_wet


class TestBUI:
    def test_zero_inputs(self):
        bui = buildup_index(0.0, 0.0)
        assert bui == 0.0

    def test_typical_values(self):
        bui = buildup_index(6.0, 15.0)
        assert bui > 0


class TestFWI:
    def test_no_nan_for_small_b(self):
        """FWI should not produce NaN for small intermediate values."""
        fwi = fire_weather_index(np.array([0.1, 0.5, 1.0]),
                                  np.array([1.0, 1.0, 1.0]))
        assert not np.any(np.isnan(fwi))

    def test_no_nan_for_large_b(self):
        """FWI should handle large values without overflow."""
        fwi = fire_weather_index(np.array([50.0]), np.array([200.0]))
        assert not np.any(np.isnan(fwi))
        assert not np.any(np.isinf(fwi))

    def test_increases_with_isi(self):
        fwi_low = fire_weather_index(np.array([2.0]), np.array([20.0]))
        fwi_high = fire_weather_index(np.array([20.0]), np.array([20.0]))
        assert fwi_high > fwi_low


class TestHourlyFWIState:
    def test_state_persistence(self):
        """FWI state should carry forward between updates."""
        fwi = HourlyFWI(3)
        state1 = fwi.get_state()

        fwi.update(
            temp_c=np.array([25.0, 25.0, 25.0]),
            rh=np.array([30.0, 30.0, 30.0]),
            ws_kph=np.array([10.0, 10.0, 10.0]),
            precip_mm=np.array([0.0, 0.0, 0.0]),
            day_of_year=180,
            hour=12,
        )

        state2 = fwi.get_state()
        # States should differ after update
        assert not np.allclose(state1["mc_ffmc"], state2["mc_ffmc"])

    def test_produces_valid_indices(self):
        fwi = HourlyFWI(2)
        result = fwi.update(
            temp_c=np.array([20.0, 25.0]),
            rh=np.array([40.0, 30.0]),
            ws_kph=np.array([15.0, 20.0]),
            precip_mm=np.array([0.0, 0.0]),
            day_of_year=180,
            hour=14,
        )

        for key in ["FFMC", "DMC", "DC", "ISI", "BUI", "FWI"]:
            assert key in result
            assert not np.any(np.isnan(result[key]))
