"""
Canadian Fire Weather Index (FWI) System — Hourly Resolution.

Implements the full FWI chain: FFMC → DMC → DC → ISI → BUI → FWI.

For daily calculations, delegates to xclim (pip-installable, maintained)
which implements the standard Van Wagner & Pickett 1985 equations.

For hourly calculations, implements the hourly FFMC from cffdrs-ng
(Van Wagner 1977), with DMC and DC updated during daylight hours only
(matching cffdrs-ng NG_FWI.py approach).

The FWI power calculation bug is fixed: log is only computed when b > 1.

References:
    - Van Wagner & Pickett 1985: Canadian FWI System (daily)
    - Van Wagner 1977: Hourly FFMC method
    - cffdrs-ng NG_FWI.py: Hourly FWI implementation (NRCan)
    - Argonne (Yu/Feng): Daily CFWI code (validation reference)
    - xclim: pip-installable FWI implementation
"""

import numpy as np
from math import exp, log, sqrt

from aorc_tools.config import FWI_DEFAULTS, FWI_DAY_LENGTH_DMC, FWI_DAY_LENGTH_DC


# =============================================================================
# Moisture Content ↔ Code Conversions (from cffdrs-ng NG_FWI.py)
# =============================================================================

def ffmc_to_mc(ffmc):
    """Convert FFMC code to moisture content (%)."""
    C_FFMC = 14875.0 / 101.0
    return C_FFMC * (101.0 - ffmc) / (59.5 + ffmc)


def mc_to_ffmc(mc):
    """Convert moisture content (%) to FFMC code."""
    C_FFMC = 14875.0 / 101.0
    return 59.5 * (250.0 - mc) / (C_FFMC + mc)


def dmc_to_mc(dmc):
    """Convert DMC code to moisture content (%)."""
    return (280.0 / np.exp(dmc / 43.43)) + 20.0


def mc_to_dmc(mc):
    """Convert moisture content (%) to DMC code."""
    return 43.43 * np.log(280.0 / (mc - 20.0))


def dc_to_mc(dc):
    """Convert DC code to moisture content (%)."""
    return 400.0 * np.exp(-dc / 400.0)


def mc_to_dc(mc):
    """Convert moisture content (%) to DC code."""
    return 400.0 * np.log(400.0 / mc)


# =============================================================================
# Hourly FFMC (from cffdrs-ng hourly_fine_fuel_moisture)
# =============================================================================

def hourly_ffmc_vectorized(lastmc, temp, rh, ws, rain, time_increment=1.0):
    """Vectorized hourly Fine Fuel Moisture Code calculation.

    Fully vectorized NumPy implementation of cffdrs-ng hourly_fine_fuel_moisture().
    Processes all points simultaneously instead of one-by-one Python loop.

    Args:
        lastmc: Previous hour's moisture content (%) — array of n_points.
        temp: Temperature (°C) — array.
        rh: Relative humidity (%) — array.
        ws: Wind speed (km/h) — array.
        rain: Precipitation (mm) — array.
        time_increment: Time step in hours (default 1.0).

    Returns:
        Updated moisture content (%) — array of n_points.
    """
    lastmc = np.asarray(lastmc, dtype=np.float64)
    temp = np.asarray(temp, dtype=np.float64)
    rh = np.asarray(rh, dtype=np.float64)
    ws = np.asarray(ws, dtype=np.float64)
    rain = np.asarray(rain, dtype=np.float64)

    rf = 42.5
    drf = 0.0579
    mo = lastmc.copy()

    # Rain effect (vectorized)
    has_rain = rain != 0.0
    if np.any(has_rain):
        rain_effect = rf * rain * np.exp(-100.0 / (251.0 - lastmc)) * (1.0 - np.exp(-6.93 / np.maximum(rain, 1e-10)))
        mo = np.where(has_rain, mo + rain_effect, mo)
        # Extra wetting for very wet fuels
        high_mc = has_rain & (lastmc > 150.0)
        if np.any(high_mc):
            extra = 0.0015 * (lastmc - 150.0) ** 2 * np.sqrt(np.maximum(rain, 0.0))
            mo = np.where(high_mc, mo + extra, mo)
        mo = np.minimum(mo, 250.0)

    # Equilibrium moisture contents
    e1 = 0.18 * (21.1 - temp) * (1.0 - np.exp(-0.115 * rh))
    ed = 0.942 * np.power(rh, 0.679) + 11.0 * np.exp((rh - 100.0) / 10.0) + e1
    ew = 0.618 * np.power(rh, 0.753) + 10.0 * np.exp((rh - 100.0) / 10.0) + e1

    # Drying regime (mo > ed)
    a1_dry = rh / 100.0
    k0_dry = 0.424 * (1.0 - np.power(a1_dry, 1.7)) + 0.0694 * np.sqrt(ws) * (1.0 - np.power(a1_dry, 8))
    kd = 2.0 * drf * k0_dry * np.exp(0.0365 * temp)
    m_dry = ed + (mo - ed) * np.power(10.0, -kd * time_increment)

    # Wetting regime (mo < ew)
    a1_wet = (100.0 - rh) / 100.0
    k0_wet = 0.424 * (1.0 - np.power(a1_wet, 1.7)) + 0.0694 * np.sqrt(ws) * (1.0 - np.power(a1_wet, 8))
    kw = 2.0 * drf * k0_wet * np.exp(0.0365 * temp)
    m_wet = ew - (ew - mo) * np.power(10.0, -kw * time_increment)

    # Select regime per point
    m = np.where(mo > ed, m_dry, np.where(mo < ew, m_wet, mo))

    return m


# =============================================================================
# Rain interception, cffdrs-ng style (NG_FWI.py prec_effective /
# rain_since_intercept_reset, nrcan-cfs-fire/cffdrs-ng main ed143d7)
# =============================================================================

FFMC_INTERCEPT = 0.5                     # cffdrs-ng PREC_MIN_FFMC (threshold and subtraction)
CANOPY_RESET_DRY_HOURS = 5               # dry hours before the canopy rain total resets


def prec_effective(prec, prec_sum, threshold, subtract, rate):
    """Effective rain for one moisture code (vectorised NG_FWI.prec_effective).

    prec      rain this hour (mm);  prec_sum  canopy rain total BEFORE this hour.
    Nothing gets through until the running total passes ``threshold``; the hour
    it does, ``(prec_sum + prec)*rate - subtract``; afterwards ``prec*rate``.
    Rounding to 0.1 mm mirrors the reference implementation.
    """
    prec = np.asarray(prec, dtype=np.float64)
    prec_sum = np.asarray(prec_sum, dtype=np.float64)
    return np.where(np.round(prec + prec_sum, 1) <= threshold, 0.0,
                    np.where(np.round(prec_sum, 1) > threshold, prec * rate,
                             (prec_sum + prec) * rate - subtract))


# =============================================================================
# Hourly DMC (from cffdrs-ng duff_moisture_code)
# =============================================================================

# DMC constants.
# LEGACY (what this port has always used, and what the batch record was built
# with): the DAILY-system drying coefficients from Van Wagner & Pickett 1985.
DMC_INTERCEPT = 1.5
DMC_REGRESSION = 1.894e-6
DMC_OFFSET_TEMP = 1.1
# cffdrs-ng (NRCan, FWI/Python/NG_FWI.py on main, checked 2026-10-20) uses
# hourly-calibrated coefficients instead:
#     DMC_REGRESSION = 2.22e-4, DMC_OFFSET_TEMP = 0.0
# Selectable through HourlyFWI(variant="cffdrs-ng"); default stays legacy so
# existing outputs are reproducible. See CLAUDE.md "FWI hourly constants".
DMC_REGRESSION_NG = 2.22e-4
DMC_OFFSET_TEMP_NG = 0.0


def hourly_dmc_vectorized(last_mcdmc, hr, temp, rh, precip, sunrise, sunset,
                          precip_cumulative_prev, time_increment=1.0,
                          regression=DMC_REGRESSION, offset_temp=DMC_OFFSET_TEMP,
                          daylight=None, rw=None, exact_ng=False):
    """Vectorized hourly Duff Moisture Code calculation.

    Fully vectorized NumPy implementation of cffdrs-ng duff_moisture_code().
    Drying only occurs during daylight hours.

    Args:
        last_mcdmc: Previous moisture content for DMC (%) — array.
        hr: Current hour (0-23) — scalar.
        temp: Temperature (°C) — array.
        rh: Relative humidity (%) — array.
        precip: Precipitation this hour (mm) — array.
        sunrise: Sunrise hour (decimal) — scalar.
        sunset: Sunset hour (decimal) — scalar.
        precip_cumulative_prev: Cumulative precip (mm) — array.
        time_increment: Time step in hours (default 1.0).
        regression, offset_temp: drying coefficients. Defaults are the legacy
            daily-system values; pass DMC_REGRESSION_NG / DMC_OFFSET_TEMP_NG
            for the cffdrs-ng hourly calibration.
        daylight: optional boolean array (one per point). When given it
            replaces the scalar ``sunrise <= hr <= sunset`` test, so each point
            dries according to its own sun position (see
            aorc_tools.explorer.timebase.daylight_solar). ``hr``, ``sunrise``
            and ``sunset`` are then ignored.
        rw: optional effective rain (mm) already computed with
            ``prec_effective(prec, canopy_total, 1.5, 1.27, 0.92)`` (cffdrs-ng
            mode). When None the legacy running-total rule is used.
        exact_ng: mirror cffdrs-ng's 0.1 degC rounding of the freezing test.

    Returns:
        Updated DMC moisture content (%) — array.
    """
    last_mcdmc = np.asarray(last_mcdmc, dtype=np.float64)
    temp = np.asarray(temp, dtype=np.float64)
    rh = np.asarray(rh, dtype=np.float64)
    precip = np.asarray(precip, dtype=np.float64)
    precip_cumulative_prev = np.asarray(precip_cumulative_prev, dtype=np.float64)

    if rw is None:
        total_precip = precip_cumulative_prev + precip
        exceeds = total_precip > DMC_INTERCEPT
        just_exceeded = exceeds & (precip_cumulative_prev <= DMC_INTERCEPT)
        rw = np.where(just_exceeded,
                      total_precip * 0.92 - 1.27,
                      precip * 0.92)
    else:
        rw = np.asarray(rw, dtype=np.float64)
        exceeds = rw > 0

    last_dmc_code = mc_to_dmc(np.clip(last_mcdmc, 20.01, 300.0))
    b = np.where(last_dmc_code <= 33,
                 100.0 / (0.5 + 0.3 * last_dmc_code),
                 np.where(last_dmc_code <= 65,
                          14.0 - 1.3 * np.log(np.maximum(last_dmc_code, 1e-10)),
                          6.2 * np.log(np.maximum(last_dmc_code, 1e-10)) - 17.2))

    mr = np.where(exceeds,
                  last_mcdmc + (1000.0 * rw) / (48.77 + b * rw),
                  last_mcdmc)
    mr = np.minimum(mr, 300.0)

    # Drying (daylight hours only)
    if daylight is not None:
        t = np.where(np.round(temp, 1) < 0, 0.0, temp) if exact_ng else np.maximum(temp, 0.0)
        rk = regression * (t + offset_temp) * (100.0 - rh)
        invtau = rk / 43.43
        mcdmc = np.where(np.asarray(daylight, dtype=bool),
                         (mr - 20.0) * np.exp(-time_increment * invtau) + 20.0, mr)
        return np.minimum(mcdmc, 300.0)
    is_daylight = (sunrise <= hr <= sunset) or (hr < 6 and sunrise <= hr + 24 <= sunset)
    if is_daylight:
        t = np.maximum(temp, 0.0)
        rk = regression * (t + offset_temp) * (100.0 - rh)
        invtau = rk / 43.43
        mcdmc = (mr - 20.0) * np.exp(-time_increment * invtau) + 20.0
    else:
        mcdmc = mr

    return np.minimum(mcdmc, 300.0)


# =============================================================================
# Hourly DC (from cffdrs-ng drought_code)
# =============================================================================

DC_INTERCEPT = 2.8
DC_REGRESSION = 0.36        # legacy: daily-system coefficient
DC_OFFSET_TEMP = 2.8        # legacy
DC_REGRESSION_NG = 1.5e-2   # cffdrs-ng hourly (NG_FWI.py main, 2026-10-20)
DC_OFFSET_TEMP_NG = 0.0


def hourly_dc_vectorized(last_mcdc, hr, temp, precip, sunrise, sunset,
                         precip_cumulative_prev, time_increment=1.0,
                         regression=DC_REGRESSION, offset_temp=DC_OFFSET_TEMP,
                         daylight=None, rw=None, exact_ng=False):
    """Vectorized hourly Drought Code calculation.

    Fully vectorized NumPy implementation of cffdrs-ng drought_code().
    Drying only during daylight hours.

    Args:
        last_mcdc: Previous DC moisture content (%) — array.
        hr: Current hour (0-23) — scalar.
        temp: Temperature (°C) — array.
        precip: Precipitation this hour (mm) — array.
        sunrise: Sunrise hour (decimal) — scalar.
        sunset: Sunset hour (decimal) — scalar.
        precip_cumulative_prev: Cumulative precip — array.
        time_increment: Time step in hours (default 1.0).
        regression, offset_temp: drying coefficients (legacy daily-system
            defaults; DC_REGRESSION_NG / DC_OFFSET_TEMP_NG for cffdrs-ng).
        daylight: optional per-point boolean mask replacing the scalar test
            (see hourly_dmc_vectorized).
        rw: optional effective rain, ``prec_effective(prec, total, 2.8, 1.27, 0.83)``.
        exact_ng: mirror cffdrs-ng's 0.1 degC rounding of the freezing test.

    Returns:
        Updated DC moisture content (%) — array.
    """
    last_mcdc = np.asarray(last_mcdc, dtype=np.float64)
    temp = np.asarray(temp, dtype=np.float64)
    precip = np.asarray(precip, dtype=np.float64)
    precip_cumulative_prev = np.asarray(precip_cumulative_prev, dtype=np.float64)

    if rw is None:
        total_precip = precip_cumulative_prev + precip
        exceeds = total_precip > DC_INTERCEPT
        just_exceeded = exceeds & (precip_cumulative_prev <= DC_INTERCEPT)
        rw = np.where(just_exceeded,
                      total_precip * 0.83 - 1.27,
                      precip * 0.83)
    else:
        rw = np.asarray(rw, dtype=np.float64)
        exceeds = rw > 0

    mr = np.where(exceeds,
                  last_mcdc + 3.937 * rw / 2.0,
                  last_mcdc)
    mr = np.minimum(mr, 400.0)

    # Drying (daylight hours only)
    if daylight is not None:
        warm = (np.round(temp, 1) > 0) if exact_ng else (temp > 0)
        pe = np.where(warm, regression * (temp + offset_temp) + 3.0 / 16.0, 0.0)
        invtau = pe / 400.0
        mcdc = np.where(np.asarray(daylight, dtype=bool), mr * np.exp(-time_increment * invtau), mr)
        return np.minimum(mcdc, 400.0)
    is_daylight = (sunrise <= hr <= sunset) or (hr < 6 and sunrise <= hr + 24 <= sunset)
    if is_daylight:
        pe = np.where(temp > 0,
                      regression * (temp + offset_temp) + 3.0 / 16.0,
                      0.0)
        invtau = pe / 400.0
        mcdc = mr * np.exp(-time_increment * invtau)
    else:
        mcdc = mr

    return np.minimum(mcdc, 400.0)


# =============================================================================
# ISI, BUI, FWI (standard equations, matching Argonne and cffdrs-ng)
# =============================================================================

def initial_spread_index(ws_kph, ffmc_code, exact_ng=False):
    """Initial Spread Index from wind speed and FFMC.

    Args:
        ws_kph: Wind speed in km/h.
        ffmc_code: Fine Fuel Moisture Code (0-101).

    Returns:
        ISI value.
    """
    fm = ffmc_to_mc(ffmc_code)
    ws_kph = np.asarray(ws_kph, dtype=np.float64)
    fw = np.where(
        (np.round(ws_kph) >= 40) if exact_ng else (ws_kph >= 40),
        12.0 * (1.0 - np.exp(-0.0818 * (ws_kph - 28.0))),
        np.exp(0.05039 * ws_kph),
    )
    ff = 91.9 * np.exp(-0.1386 * fm) * (1.0 + fm ** 5.31 / 4.93e7)
    return 0.208 * fw * ff


def buildup_index(dmc_code, dc_code):
    """Buildup Index from DMC and DC.

    Args:
        dmc_code: Duff Moisture Code.
        dc_code: Drought Code.

    Returns:
        BUI value.
    """
    dmc_code = np.asarray(dmc_code, dtype=np.float64)
    dc_code = np.asarray(dc_code, dtype=np.float64)

    denom = dmc_code + 0.4 * dc_code
    denom_safe = np.where(denom == 0, 1.0, denom)
    bui = np.where(
        (dmc_code == 0) & (dc_code == 0),
        0.0,
        0.8 * dc_code * dmc_code / denom_safe,
    )

    # Correction when BUI < DMC
    mask = bui < dmc_code
    if np.any(mask):
        p = np.where(mask, (dmc_code - bui) / np.maximum(dmc_code, 1e-10), 0.0)
        cc = 0.92 + (0.0114 * dmc_code) ** 1.7
        bui = np.where(mask, dmc_code - cc * p, bui)
        bui = np.maximum(bui, 0.0)

    return bui


def fire_weather_index(isi, bui):
    """Fire Weather Index from ISI and BUI.

    FWI power calculation handles the b ≤ 1 edge case safely
    (matching Argonne's branch-zeroing approach).

    Args:
        isi: Initial Spread Index.
        bui: Buildup Index.

    Returns:
        FWI value.
    """
    isi = np.asarray(isi, dtype=np.float64)
    bui = np.asarray(bui, dtype=np.float64)

    fd = np.where(
        bui > 80,
        1000.0 / (25.0 + 108.64 / np.exp(0.023 * bui)),
        0.626 * bui ** 0.809 + 2.0,
    )

    bb = 0.1 * isi * fd

    # Safe power calculation: only compute log when bb > 1
    # (Argonne approach: compute both branches, zero out the invalid one)
    safe_log = np.where(bb > 1, np.log(bb), 0.0)
    safe_term = np.where(bb > 1, 0.434 * safe_log, 0.0)
    safe_power = np.where(bb > 1, safe_term ** 0.647, 0.0)
    fwi_high = np.where(bb > 1, np.exp(2.72 * safe_power), 0.0)

    fwi = np.where(bb > 1, fwi_high, bb)
    return fwi


# =============================================================================
# Solar Geometry (sunrise/sunset for hourly drying)
# =============================================================================

def solar_declination(day_of_year):
    """Solar declination angle in radians."""
    return 23.45 * np.sin(np.radians(360 / 365 * (day_of_year - 81))) * np.pi / 180


def sunrise_sunset(latitude, day_of_year):
    """Compute sunrise and sunset hours (local solar time).

    Args:
        latitude: Latitude in degrees.
        day_of_year: Day of year (1-366).

    Returns:
        (sunrise_hour, sunset_hour) as decimal hours.
    """
    lat_rad = np.radians(latitude)
    decl = solar_declination(day_of_year)

    # Hour angle at sunrise/sunset
    cos_ha = -np.tan(lat_rad) * np.tan(decl)
    cos_ha = np.clip(cos_ha, -1.0, 1.0)

    ha = np.degrees(np.arccos(cos_ha))
    sunrise = 12.0 - ha / 15.0
    sunset = 12.0 + ha / 15.0

    return sunrise, sunset


# =============================================================================
# FWI State Manager (hourly calculation with carry-forward)
# =============================================================================

class HourlyFWI:
    """Manages hourly FWI calculation state for a set of points.

    Tracks FFMC, DMC, DC moisture contents and produces hourly
    FFMC, DMC, DC codes plus ISI, BUI, FWI.

    State carries forward between hours and can be serialized/deserialized
    for continuity between extraction runs.
    """

    COEFFICIENTS = ("legacy", "cffdrs-ng")
    RAIN = ("legacy", "cffdrs-ng")

    def __init__(self, n_points, latitude=46.5, initial_state=None,
                 coefficients="legacy", rain=None):
        """Initialize FWI state.

        Args:
            n_points: Number of spatial points.
            latitude: Latitude for the LEGACY daylight test only (used when
                ``update`` is called without a ``daylight`` mask).
            initial_state: Optional dict to restore saved state.
            coefficients: DMC/DC drying coefficients.
                "legacy"    -- daily-system values (DMC 1.894e-6 with +1.1 C,
                               DC 0.36 with +2.8 C); reproduces old outputs.
                "cffdrs-ng" -- NRCan NG_FWI.py hourly calibration (DMC 2.22e-4,
                               DC 1.5e-2, no temperature offsets).
                The choice is an open project decision (CLAUDE.md).
            rain: rain handling. Defaults to follow ``coefficients``:
                "legacy"    -- running total resets every dry hour; FFMC wets on
                               any rain (what the batch record used).
                "cffdrs-ng" -- NG_FWI.py: canopy total that resets only after
                               5 dry hours, FFMC 0.5 mm interception, DMC/DC
                               effective-rain via prec_effective(), 0.1-unit
                               rounding as in the reference. With
                               coefficients="cffdrs-ng" this reproduces the
                               official PRF2007 reference output
                               (scripts/compare_with_cffdrs_ng.py).

        Daylight: pass ``daylight=`` (one bool per point) to ``update``. Build it
        with ``aorc_tools.explorer.timebase.daylight_solar(lat, lon, t_utc)``.
        Without it the legacy test runs (UTC hour vs solar sunrise/sunset at
        ``latitude``), which shifts drying by ~6 h in the WUP.
        """
        if coefficients not in self.COEFFICIENTS:
            raise ValueError(f"coefficients must be one of {self.COEFFICIENTS}")
        rain = rain or coefficients
        if rain not in self.RAIN:
            raise ValueError(f"rain must be one of {self.RAIN}")
        self.rain = rain
        self.n_points = n_points
        self.latitude = latitude
        self.coefficients = coefficients
        if coefficients == "cffdrs-ng":
            self._dmc_coef = (DMC_REGRESSION_NG, DMC_OFFSET_TEMP_NG)
            self._dc_coef = (DC_REGRESSION_NG, DC_OFFSET_TEMP_NG)
        else:
            self._dmc_coef = (DMC_REGRESSION, DMC_OFFSET_TEMP)
            self._dc_coef = (DC_REGRESSION, DC_OFFSET_TEMP)
        self.last_daylight = None

        if initial_state is not None:
            self.mc_ffmc = np.array(initial_state["mc_ffmc"], dtype=np.float64)
            self.mc_dmc = np.array(initial_state["mc_dmc"], dtype=np.float64)
            self.mc_dc = np.array(initial_state["mc_dc"], dtype=np.float64)
            self.precip_cumulative = np.array(
                initial_state.get("precip_cumulative", np.zeros(n_points)),
                dtype=np.float64,
            )
            self.canopy_drying = np.array(
                initial_state.get("canopy_drying", np.zeros(n_points)), dtype=np.float64)
        else:
            self.mc_ffmc = np.full(n_points, ffmc_to_mc(FWI_DEFAULTS["FFMC"]))
            self.mc_dmc = np.full(n_points, dmc_to_mc(FWI_DEFAULTS["DMC"]))
            self.mc_dc = np.full(n_points, dc_to_mc(FWI_DEFAULTS["DC"]))
            self.precip_cumulative = np.zeros(n_points)
            self.canopy_drying = np.zeros(n_points)

    def update(self, temp_c, rh, ws_kph, precip_mm, day_of_year, hour, daylight=None):
        """Advance FWI state by one hour and return all index values.

        Args:
            temp_c: Temperature in °C (array of n_points).
            rh: Relative humidity in % (array).
            ws_kph: Wind speed in km/h (array).
            precip_mm: Precipitation in mm (array).
            day_of_year: Day of year (int, 1-366).
            hour: Hour of day (int, 0-23) -- the UTC hour of the AORC packet.
            daylight: Optional bool array (n_points): is the sun up at this
                packet, per point? (timebase.daylight_solar). If omitted the
                legacy scalar test is used.

        Returns:
            dict with keys: FFMC, DMC, DC, ISI, BUI, FWI (all arrays).
        """
        sunrise, sunset = sunrise_sunset(self.latitude, day_of_year)
        hour_used = hour
        if daylight is None:
            legacy = (sunrise <= hour <= sunset) or (hour < 6 and sunrise <= hour + 24 <= sunset)
            self.last_daylight = np.full(self.n_points, bool(legacy))
        else:
            daylight = np.asarray(daylight, dtype=bool)
            self.last_daylight = daylight

        ng = self.rain == "cffdrs-ng"
        if ng:
            if daylight is None:      # the rounding-exact functions take a per-point mask
                daylight = self.last_daylight
            p = np.asarray(precip_mm, dtype=np.float64)
            # canopy total: reset only after CANOPY_RESET_DRY_HOURS dry hours (NG_FWI order)
            wet = np.round(p, 1) > 0
            restart = wet | (self.precip_cumulative == 0)
            dry = np.where(restart, 0.0, self.canopy_drying + 1.0)
            reset = dry >= CANOPY_RESET_DRY_HOURS
            self.precip_cumulative = np.where(reset, 0.0, self.precip_cumulative)
            self.canopy_drying = np.where(reset, 0.0, dry)
            total = self.precip_cumulative
            r_ffmc = prec_effective(p, total, FFMC_INTERCEPT, FFMC_INTERCEPT, 1.0)
            r_dmc = prec_effective(p, total, DMC_INTERCEPT, 1.27, 0.92)
            r_dc = prec_effective(p, total, DC_INTERCEPT, 1.27, 0.83)
        else:
            r_ffmc, r_dmc, r_dc = precip_mm, None, None

        # Vectorized update — all 28K+ points computed simultaneously via NumPy
        self.mc_ffmc = hourly_ffmc_vectorized(
            self.mc_ffmc, temp_c, rh, ws_kph, r_ffmc,
        )
        self.mc_dmc = hourly_dmc_vectorized(
            self.mc_dmc, hour_used, temp_c, rh, precip_mm,
            sunrise, sunset, self.precip_cumulative,
            regression=self._dmc_coef[0], offset_temp=self._dmc_coef[1],
            daylight=daylight, rw=r_dmc, exact_ng=ng,
        )
        self.mc_dc = hourly_dc_vectorized(
            self.mc_dc, hour_used, temp_c, precip_mm,
            sunrise, sunset, self.precip_cumulative,
            regression=self._dc_coef[0], offset_temp=self._dc_coef[1],
            daylight=daylight, rw=r_dc, exact_ng=ng,
        )

        # Update precipitation accumulator
        if ng:
            self.precip_cumulative = self.precip_cumulative + np.asarray(precip_mm, dtype=np.float64)
        else:      # legacy: reset on every dry hour
            self.precip_cumulative = np.where(
                precip_mm > 0,
                self.precip_cumulative + precip_mm,
                0.0,
            )

        # Convert moisture contents to codes
        ffmc_codes = mc_to_ffmc(self.mc_ffmc)
        if ng:   # reference converts without clipping (mc stays inside (20, 300] / (0, 400])
            dmc_codes = mc_to_dmc(np.clip(self.mc_dmc, 20.0 + 1e-9, 300.0))
            dc_codes = mc_to_dc(np.clip(self.mc_dc, 1e-9, 400.0))
        else:
            dmc_codes = mc_to_dmc(np.clip(self.mc_dmc, 20.01, 300.0))
            dc_codes = mc_to_dc(np.clip(self.mc_dc, 0.01, 400.0))

        # Compute downstream indices
        isi = initial_spread_index(ws_kph, ffmc_codes, exact_ng=ng)
        bui = buildup_index(dmc_codes, dc_codes)
        fwi = fire_weather_index(isi, bui)

        return {
            "FFMC": ffmc_codes,
            "DMC": dmc_codes,
            "DC": dc_codes,
            "ISI": isi,
            "BUI": bui,
            "FWI": fwi,
        }

    def get_state(self):
        """Return serializable state dict."""
        return {
            "mc_ffmc": self.mc_ffmc.tolist(),
            "mc_dmc": self.mc_dmc.tolist(),
            "mc_dc": self.mc_dc.tolist(),
            "precip_cumulative": self.precip_cumulative.tolist(),
            "canopy_drying": self.canopy_drying.tolist(),
        }

    def set_state(self, state_dict):
        """Restore state from a saved dict."""
        self.mc_ffmc = np.array(state_dict["mc_ffmc"], dtype=np.float64)
        self.mc_dmc = np.array(state_dict["mc_dmc"], dtype=np.float64)
        self.mc_dc = np.array(state_dict["mc_dc"], dtype=np.float64)
        self.canopy_drying = np.array(
            state_dict.get("canopy_drying", np.zeros(self.n_points)), dtype=np.float64)
        self.precip_cumulative = np.array(
            state_dict.get("precip_cumulative", np.zeros(self.n_points)),
            dtype=np.float64,
        )
