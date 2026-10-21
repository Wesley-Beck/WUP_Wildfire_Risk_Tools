"""
Fire Potential Index (FPI) — Burgan et al. 1998.

Ported directly from Argonne's FPI code (Yu/Feng).

FPI integrates satellite-derived vegetation condition (NDVI) with
weather-derived fuel moisture to estimate fire potential. This is
NOT the Fosberg FFWI — it's a completely different index requiring
NDVI data.

Required inputs:
- NDVI (ND0): Current NDVI values per pixel (from MODIS/VIIRS)
- NDVI min/max: Historical min/max per pixel (static dataset)
- LLfm: Live fuel moisture loading map (static, from LANDFIRE)
- DLfm: Dead fuel moisture loading map (static, from LANDFIRE)
- MXd: Moisture of extinction map (static, from fuel model)
- Tmax: Daily maximum temperature (°F)
- RH_min: Daily minimum relative humidity (%)

References:
    - Burgan et al. 1998: FPI methodology
    - Argonne (Yu/Feng): FPI calculation code (primary reference)
    - USGS WFPI: Wildland Fire Potential Index operational product
"""

import logging

import numpy as np

from aorc_tools.climate_convert import equilibrium_moisture_content

logger = logging.getLogger(__name__)


def compute_fpi(nd0, nd_min, nd_max, llfm, dlfm, mxd, tmax_f, rh_min, fm10=None):
    """Compute Fire Potential Index (Burgan 1998, Argonne implementation).

    Ported directly from Argonne's FPI code. All array inputs must have
    compatible shapes.

    Args:
        nd0: Current NDVI values (array). Scale: -1 to 1 (or raw MODIS scale).
        nd_min: Historical minimum NDVI per pixel (array, same shape as nd0).
        nd_max: Historical maximum NDVI per pixel (array).
        llfm: Live fuel moisture loading (array, from LANDFIRE/NFDRS).
        dlfm: Dead fuel moisture loading (array).
        mxd: Dead fuel moisture of extinction (array, %).
        tmax_f: Daily maximum temperature in °F (array).
        rh_min: Daily minimum relative humidity in % (array).
        fm10: Optional 10-h dead fuel moisture (%) supplied directly, e.g. an
            hourly timelag value. When given, steps 3-4 (EMC from Tmax/RHmin
            and FM10 = 1.28*EMC) are skipped, EMC is returned as NaN, and
            ``tmax_f``/``rh_min`` are ignored. This is the explorer's hourly
            adaptation; the Burgan/Argonne daily form is the default.

    Returns:
        dict with keys:
            RG: Relative Greenness [0, 1]
            EMC: Equilibrium Moisture Content
            FM10: 10-hr fuel moisture
            FPI: Fire Potential Index [0, 100]
    """
    nd0 = np.asarray(nd0, dtype=np.float64)
    nd_min = np.asarray(nd_min, dtype=np.float64)
    nd_max = np.asarray(nd_max, dtype=np.float64)
    llfm = np.asarray(llfm, dtype=np.float64)
    dlfm = np.asarray(dlfm, dtype=np.float64)
    mxd = np.asarray(mxd, dtype=np.float64)
    tmax_f = np.asarray(tmax_f, dtype=np.float64)
    rh_min = np.asarray(rh_min, dtype=np.float64)

    # =========================================================================
    # Step 1: Relative Greenness (Argonne-exact)
    # =========================================================================
    denom = nd_max - nd_min
    denom = np.where(denom == 0, np.nan, denom)  # Avoid division by zero
    rg = (nd0 - nd_min) / denom
    rg = np.where(np.isinf(rg), np.nan, rg)

    # =========================================================================
    # Step 2: Live/dead fuel partitioning (Argonne-exact)
    # =========================================================================
    total_fm = llfm + dlfm
    total_fm_safe = np.maximum(total_fm, 1e-10)

    llp = llfm * rg                     # Live fuel portion
    dlp = (1.0 - rg) * llfm + dlfm     # Dead fuel portion

    lf = llp / total_fm_safe            # Live fraction
    df = dlp / total_fm_safe            # Dead fraction

    # =========================================================================
    # Step 3: EMC from Simard 1968 (same as existing code, Argonne-exact)
    # =========================================================================
    if fm10 is None:
        emc = equilibrium_moisture_content(tmax_f, rh_min)
        emc = np.maximum(emc, 0.0)

        # =====================================================================
        # Step 4: 10-hr fuel moisture (Argonne-exact)
        # =====================================================================
        fm10 = emc * 1.28
    else:
        fm10 = np.asarray(fm10, dtype=np.float64)
        emc = np.full(np.shape(fm10), np.nan)

    # =========================================================================
    # Step 5: Dead fuel tension (ratio to moisture of extinction)
    # =========================================================================
    tnf = fm10 / np.maximum(mxd, 1e-10)
    tnf = np.minimum(tnf, 1.0)

    # =========================================================================
    # Step 6: FPI calculation (Argonne-exact)
    # =========================================================================
    fpiu = 100.0 - (rg * lf + tnf * df) * 100.0
    fpi_max = 100.0 - (2.0 / np.maximum(mxd, 1e-10) * 100.0)
    fpi_max_safe = np.where(fpi_max == 0, np.nan, fpi_max)

    fpi = fpiu + (2.0 / np.maximum(mxd, 1e-10)) * (fpiu / fpi_max_safe) * 100.0

    # Clamp to valid range
    fpi = np.clip(fpi, 0.0, 100.0)

    return {
        "RG": rg,
        "EMC": emc,
        "FM10": fm10,
        "FPI": fpi,
    }


def compute_fpi_from_hourly(nd0, nd_min, nd_max, llfm, dlfm, mxd,
                             hourly_temp_f, hourly_rh):
    """Compute FPI using hourly data by extracting daily max temp / min RH.

    For hourly FPI: uses rolling 24-hour max temperature and min RH,
    matching how Argonne derived daily Tmax and RH_min from hourly data.

    Args:
        nd0, nd_min, nd_max, llfm, dlfm, mxd: Same as compute_fpi.
        hourly_temp_f: Array of hourly temperatures in °F (time × points).
        hourly_rh: Array of hourly RH in % (time × points).

    Returns:
        Same as compute_fpi, computed from daily max/min of hourly data.
    """
    tmax_f = np.max(hourly_temp_f, axis=0)
    rh_min = np.min(hourly_rh, axis=0)

    return compute_fpi(nd0, nd_min, nd_max, llfm, dlfm, mxd, tmax_f, rh_min)


class NDVIDownloader:
    """Download NDVI data from NASA MODIS/VIIRS for FPI calculation.

    Handles fetching 16-day composite NDVI products and deriving the
    static NDVI min/max climatology needed for Relative Greenness.

    This is a placeholder that will be expanded when NASA Earthdata
    access is configured.
    """

    def __init__(self, earthdata_token=None):
        self.token = earthdata_token

    def get_ndvi(self, date, lat_bounds, lon_bounds):
        """Fetch NDVI for a given date and region.

        Returns None if data is unavailable (FPI will output NaN).
        """
        logger.warning(
            "NDVI download not yet configured. FPI will output NaN. "
            "Set up NASA Earthdata access and provide NDVI data in data/static/."
        )
        return None

    def get_ndvi_climatology(self, lat_bounds, lon_bounds):
        """Fetch NDVI min/max climatology for a region.

        Returns None if data is unavailable.
        """
        logger.warning("NDVI climatology not yet configured.")
        return None
