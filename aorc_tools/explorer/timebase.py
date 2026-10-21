"""
Time basis for the hourly fire-danger chain.

Three clocks matter for an AORC packet (one cell, one hour):

    UTC              AORC's own time stamps. Temperature/humidity are instantaneous at
                     the top of the hour; precipitation is the accumulation over the hour
                     ENDING at the stamp (AORC v1.1 methods, OWP).
    local standard   UTC-6 (CST) for Gogebic and Iron counties, UTC-5 (EST) for Baraga,
    time (LST)       Houghton, Keweenaw (incl. Isle Royale) and Ontonagon. cffdrs-ng and
                     the daily FWI/NFDRS conventions are defined in LST (no DST).
    local clock      LST plus daylight saving time (CDT/EDT in summer) -- what people read
                     on a watch; only used for display.

and one physical quantity -- where the sun is:

    solar position   computed per cell (its own latitude and longitude) at the packet's
                     UTC instant with the NOAA solar-position equations. A packet is
                     "daylight" when the solar zenith is < 90.833 deg (refraction + disk),
                     the same threshold cffdrs-ng uses for sunrise/sunset.

The DMC and DC moisture codes only dry in daylight. The legacy port compared the UTC
hour against sunrise/sunset expressed in local solar hours at one fixed latitude, which
shifts the drying window by about six hours in the WUP. ``daylight_solar`` replaces that
test; ``daylight_legacy`` reproduces it exactly so the difference can be shown.

Evaluating the sun position at the UTC instant is equivalent to cffdrs-ng's
``sunrise <= hr <= sunset`` test done in LST, without needing a time zone at all.
Time zones therefore only change *daily* aggregations (which hours make up "the day")
and what is printed on the screen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np
import pandas as pd

ZENITH_HORIZON = 90.833  # degrees: geometric horizon + refraction + solar semi-diameter

# County -> (IANA zone, standard UTC offset in hours, short label)
COUNTY_TZ = {
    "Gogebic": ("America/Menominee", -6, "CT"),
    "Iron": ("America/Menominee", -6, "CT"),
    "Baraga": ("America/Detroit", -5, "ET"),
    "Houghton": ("America/Detroit", -5, "ET"),
    "Keweenaw": ("America/Detroit", -5, "ET"),
    "Ontonagon": ("America/Detroit", -5, "ET"),
}


# ---------------------------------------------------------------------------
# NOAA solar position
# ---------------------------------------------------------------------------

def _fractional_year(doy, hour, days_in_year):
    return 2.0 * np.pi / days_in_year * (doy - 1 + (hour - 12.0) / 24.0)


def eqtime_declination(gamma):
    """Equation of time (minutes) and solar declination (radians) -- NOAA series."""
    g = gamma
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                       - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    return eqtime, decl


def _days_in_year(year):
    return 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365


def solar_zenith(lat, lon, t_utc) -> np.ndarray:
    """Solar zenith angle (degrees) for each (lat, lon) at UTC instant ``t_utc``.

    lat/lon in degrees (lon east-positive, so the WUP is negative)."""
    t = pd.Timestamp(t_utc)
    hour = t.hour + t.minute / 60.0 + t.second / 3600.0
    gamma = _fractional_year(t.dayofyear, hour, _days_in_year(t.year))
    eqtime, decl = eqtime_declination(gamma)
    lat_r = np.radians(np.asarray(lat, dtype=float))
    tst = hour * 60.0 + eqtime + 4.0 * np.asarray(lon, dtype=float)   # true solar time, minutes
    ha = np.radians(tst / 4.0 - 180.0)
    cosz = np.sin(lat_r) * np.sin(decl) + np.cos(lat_r) * np.cos(decl) * np.cos(ha)
    return np.degrees(np.arccos(np.clip(cosz, -1.0, 1.0)))


def solar_elevation(lat, lon, t_utc) -> np.ndarray:
    return 90.0 - solar_zenith(lat, lon, t_utc)


def daylight_solar(lat, lon, t_utc) -> np.ndarray:
    """FIXED test: True where the sun is above the horizon at the packet's instant."""
    return solar_zenith(lat, lon, t_utc) < ZENITH_HORIZON


def sun_times_utc(lat, lon, date) -> dict:
    """Sunrise, solar noon and sunset in hours after 00:00 UTC of ``date``
    (values can be < 0 or > 24: WUP summer sunsets fall after 00Z the next day).

    Uses the fractional year at local noon, as cffdrs-ng's get_sunlight does."""
    d = pd.Timestamp(date).normalize()
    gamma = _fractional_year(d.dayofyear, 12.0, _days_in_year(d.year))
    eqtime, decl = eqtime_declination(gamma)
    lat_r = np.radians(np.asarray(lat, dtype=float))
    lon = np.asarray(lon, dtype=float)
    cos_ha = (np.cos(np.radians(ZENITH_HORIZON)) / (np.cos(lat_r) * np.cos(decl))) - np.tan(lat_r) * np.tan(decl)
    ha = np.degrees(np.arccos(np.clip(cos_ha, -1.0, 1.0)))
    sunrise = (720.0 - 4.0 * (lon + ha) - eqtime) / 60.0
    sunset = (720.0 - 4.0 * (lon - ha) - eqtime) / 60.0
    noon = (720.0 - 4.0 * lon - eqtime) / 60.0
    return {"sunrise": sunrise, "sunset": sunset, "noon": noon, "day_length": sunset - sunrise}


# ---------------------------------------------------------------------------
# Legacy test (reproduced exactly so it can be compared)
# ---------------------------------------------------------------------------

def legacy_sunrise_sunset(latitude, doy):
    """fwi.sunrise_sunset as used by the batch extractor: solar hours from a simple
    declination formula at ONE latitude, no longitude, no equation of time."""
    from aorc_tools.fire_indices.fwi import sunrise_sunset
    return sunrise_sunset(latitude, doy)


def daylight_legacy(t_utc, latitude=46.5) -> bool:
    """LEGACY test: the UTC hour compared with solar-time sunrise/sunset (fwi.py)."""
    t = pd.Timestamp(t_utc)
    sr, ss = legacy_sunrise_sunset(latitude, t.dayofyear)
    hr = t.hour
    return bool((sr <= hr <= ss) or (hr < 6 and sr <= hr + 24 <= ss))


# ---------------------------------------------------------------------------
# Time zones
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _zone(name):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def local_clock(t_utc, tz_name):
    """(local datetime, abbreviation, utc offset hours, is_dst) for display."""
    t = pd.Timestamp(t_utc)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    loc = t.tz_convert(_zone(tz_name))
    off = loc.utcoffset().total_seconds() / 3600.0
    dst = bool(loc.dst() and loc.dst().total_seconds() != 0)
    return loc.tz_localize(None).to_pydatetime(), loc.tzname(), off, dst


def lst_hour(t_utc, std_offset_h):
    t = pd.Timestamp(t_utc)
    return (t.hour + std_offset_h) % 24


def zone_table() -> list[dict]:
    seen = {}
    for county, (tz, off, lab) in COUNTY_TZ.items():
        seen.setdefault(tz, {"tz": tz, "std_offset_h": off, "label": lab, "counties": []})["counties"].append(county)
    return list(seen.values())


def _fmt_h(h):
    """Decimal hours (may be <0 or >=24) -> 'HH:MM' with a day marker."""
    if h is None or not np.isfinite(h):
        return None
    day = int(np.floor(h / 24.0))
    m = int(round((h - 24 * day) * 60))
    if m == 1440:
        m, day = 0, day + 1
    s = f"{m // 60:02d}:{m % 60:02d}"
    return s + ("(+1d)" if day == 1 else "(-1d)" if day == -1 else "")


def day_report(lat, lon, county, date, legacy_latitude=46.5) -> dict:
    """Everything the 'Time & daylight' view prints for one cell and one UTC date."""
    tz, off, lab = COUNTY_TZ.get(county, ("America/Detroit", -5, "ET"))
    d = pd.Timestamp(date).normalize()
    st = sun_times_utc(lat, lon, d)
    sr, ss, noon = float(st["sunrise"]), float(st["sunset"]), float(st["noon"])
    _, abbr_noon, clock_off, is_dst = local_clock(d + pd.Timedelta(hours=18), tz)
    leg_sr, leg_ss = legacy_sunrise_sunset(legacy_latitude, d.dayofyear)
    hours = []
    for h in range(24):
        t = d + pd.Timedelta(hours=h)
        loc, abbr, coff, _ = local_clock(t, tz)
        hours.append({
            "utc": t.strftime("%Y-%m-%dT%H:00Z"),
            "utc_hour": h,
            "lst_hour": (h + off) % 24,
            "clock": loc.strftime("%H:%M"), "clock_abbr": abbr,
            "solar_time_h": round(((h * 60 + float(eqtime_declination(_fractional_year(d.dayofyear, h, _days_in_year(d.year)))[0]) + 4 * lon) / 60.0) % 24, 3),
            "elevation_deg": round(float(solar_elevation(lat, lon, t)), 2),
            "daylight_fixed": bool(daylight_solar(lat, lon, t)),
            "daylight_legacy": daylight_legacy(t, legacy_latitude),
        })
    mis = sum(h["daylight_fixed"] != h["daylight_legacy"] for h in hours)
    return {
        "date": d.strftime("%Y-%m-%d"), "lat": lat, "lon": lon, "county": county,
        "tz": tz, "zone": lab, "std_offset_h": off, "clock_offset_h": clock_off, "dst": is_dst, "clock_abbr": abbr_noon,
        "fixed": {"sunrise_utc": _fmt_h(sr), "sunset_utc": _fmt_h(ss), "solar_noon_utc": _fmt_h(noon),
                  "sunrise_lst": _fmt_h(sr + off), "sunset_lst": _fmt_h(ss + off),
                  "sunrise_clock": _fmt_h(sr + clock_off), "sunset_clock": _fmt_h(ss + clock_off),
                  "sunrise_utc_h": sr, "sunset_utc_h": ss, "day_length_h": round(ss - sr, 3)},
        "legacy": {"latitude": legacy_latitude, "sunrise_solar_h": round(float(leg_sr), 3), "sunset_solar_h": round(float(leg_ss), 3),
                   "note": "legacy compares these SOLAR hours with the UTC hour of the packet",
                   "daylight_utc_hours": [h["utc_hour"] for h in hours if h["daylight_legacy"]]},
        "hours": hours,
        "misclassified_hours": mis,
    }
