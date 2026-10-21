"""
Classic DAILY Canadian Fire Weather Index (Van Wagner 1987; Van Wagner & Pickett
1985), vectorised over cells.

This is the form most existing products report -- CWFIS, the Copernicus/GEFF
ERA5 fire-danger reanalysis, xclim ``cffwis_indices`` and the Argonne CONUS
study -- so the explorer computes it next to the hourly cffdrs-ng chain:

    one update per cell per day at 12:00 local STANDARD time,
    noon temperature / RH / 10 m wind, rain = 24 h total ending at noon LST.

Equations follow the R ``cffdrs`` package (``fwi()``, Wang et al. 2017) and
xclim's ``_fire_weather_calc`` without overwintering or fire-season masking:
the chain runs continuously from FFMC 85 / DMC 6 / DC 15, like the hourly
chain. Day-length factors are the standard tables, which apply as published
for the WUP's 46-48 N (Le: lat > 33 N; Lf: lat > 20 N).
scripts/compare_daily_fwi_xclim.py checks this module against xclim.
"""

from __future__ import annotations

import numpy as np

# Effective day length for DMC (Le) and day-length factor for DC (Lf), Jan..Dec
DMC_DAY_LENGTH = np.array([6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0])
DC_DAY_FACTOR = np.array([-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6])
FFMC0, DMC0, DC0 = 85.0, 6.0, 15.0


def daily_ffmc(ffmc_yda, temp, rh, ws, prec):
    ffmc_yda, temp, rh, ws, prec = (np.asarray(a, dtype=np.float64) for a in (ffmc_yda, temp, rh, ws, prec))
    rh = np.clip(rh, 0.0, 100.0)
    wmo = 147.2 * (101.0 - ffmc_yda) / (59.5 + ffmc_yda)
    rf = np.where(prec > 0.5, prec - 0.5, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        wet = 42.5 * rf * np.exp(-100.0 / (251.0 - wmo)) * (1.0 - np.exp(-6.93 / np.where(rf > 0, rf, 1.0)))
        extra = np.where(wmo > 150.0, 0.0015 * (wmo - 150.0) ** 2 * np.sqrt(rf), 0.0)
    wmo = np.where(prec > 0.5, np.minimum(wmo + wet + extra, 250.0), wmo)
    e1 = 0.18 * (21.1 - temp) * (1.0 - 1.0 / np.exp(0.115 * rh))
    ed = 0.942 * rh ** 0.679 + 11.0 * np.exp((rh - 100.0) / 10.0) + e1
    ew = 0.618 * rh ** 0.753 + 10.0 * np.exp((rh - 100.0) / 10.0) + e1
    kl_d = 0.424 * (1.0 - (rh / 100.0) ** 1.7) + 0.0694 * np.sqrt(ws) * (1.0 - (rh / 100.0) ** 8)
    kl_w = 0.424 * (1.0 - ((100.0 - rh) / 100.0) ** 1.7) + 0.0694 * np.sqrt(ws) * (1.0 - ((100.0 - rh) / 100.0) ** 8)
    kd = kl_d * 0.581 * np.exp(0.0365 * temp)
    kw = kl_w * 0.581 * np.exp(0.0365 * temp)
    wm = np.where(wmo > ed, ed + (wmo - ed) / 10.0 ** kd,
                  np.where(wmo < ew, ew - (ew - wmo) / 10.0 ** kw, wmo))
    return np.clip(59.5 * (250.0 - wm) / (147.2 + wm), 0.0, 101.0)


def daily_dmc(dmc_yda, temp, rh, prec, month):
    dmc_yda, temp, rh, prec = (np.asarray(a, dtype=np.float64) for a in (dmc_yda, temp, rh, prec))
    rh = np.clip(rh, 0.0, 100.0)
    t = np.maximum(temp, -1.1)
    rk = 1.894 * (t + 1.1) * (100.0 - rh) * DMC_DAY_LENGTH[np.asarray(month) - 1] * 1e-4
    rw = 0.92 * prec - 1.27
    wmi = 20.0 + 280.0 / np.exp(0.023 * dmc_yda)
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.where(dmc_yda <= 33.0, 100.0 / (0.5 + 0.3 * dmc_yda),
                     np.where(dmc_yda <= 65.0, 14.0 - 1.3 * np.log(dmc_yda), 6.2 * np.log(dmc_yda) - 17.2))
        wmr = wmi + 1000.0 * rw / (48.77 + b * rw)
        pr = np.maximum(43.43 * (5.6348 - np.log(wmr - 20.0)), 0.0)
    pr = np.where(prec > 1.5, pr, dmc_yda)
    return np.maximum(pr + rk, 0.0)


def daily_dc(dc_yda, temp, prec, month):
    dc_yda, temp, prec = (np.asarray(a, dtype=np.float64) for a in (dc_yda, temp, prec))
    t = np.maximum(temp, -2.8)
    pe = np.maximum((0.36 * (t + 2.8) + DC_DAY_FACTOR[np.asarray(month) - 1]) / 2.0, 0.0)
    rw = 0.83 * prec - 1.27
    smi = 800.0 * np.exp(-dc_yda / 400.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        dr = np.maximum(dc_yda - 400.0 * np.log(1.0 + 3.937 * rw / smi), 0.0)
    dr = np.where(prec > 2.8, dr, dc_yda)
    return np.maximum(dr + pe, 0.0)


def daily_isi(ffmc, ws):
    fm = 147.2 * (101.0 - ffmc) / (59.5 + ffmc)
    ff = 91.9 * np.exp(-0.1386 * fm) * (1.0 + fm ** 5.31 / 4.93e7)
    return 0.208 * np.exp(0.05039 * np.asarray(ws, dtype=np.float64)) * ff


def daily_bui(dmc, dc):
    dmc, dc = np.asarray(dmc, dtype=np.float64), np.asarray(dc, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        bui = np.where((dmc == 0) & (dc == 0), 0.0, 0.8 * dc * dmc / (dmc + 0.4 * dc))
        p = np.where(dmc > 0, (dmc - bui) / dmc, 0.0)
        cc = 0.92 + (0.0114 * dmc) ** 1.7
    bui = np.where(bui < dmc, dmc - cc * p, bui)
    return np.maximum(np.nan_to_num(bui), 0.0)


def daily_fwi(isi, bui):
    isi, bui = np.asarray(isi, dtype=np.float64), np.asarray(bui, dtype=np.float64)
    bb = np.where(bui > 80.0, 0.1 * isi * (1000.0 / (25.0 + 108.64 / np.exp(0.023 * bui))),
                  0.1 * isi * (0.626 * bui ** 0.809 + 2.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        hi = np.exp(2.72 * (0.434 * np.log(np.where(bb > 1.0, bb, np.e))) ** 0.647)
    return np.where(bb > 1.0, hi, bb)


class DailyFWI:
    """Daily CFFWIS state for n cells; update(...) once per day per cell."""

    def __init__(self, n, ffmc=None, dmc=None, dc=None):
        self.ffmc = np.full(n, FFMC0) if ffmc is None else np.array(ffmc, dtype=np.float64)
        self.dmc = np.full(n, DMC0) if dmc is None else np.array(dmc, dtype=np.float64)
        self.dc = np.full(n, DC0) if dc is None else np.array(dc, dtype=np.float64)

    def update(self, temp, rh, ws, prec24, month, mask=None):
        """Advance cells in ``mask`` (default all) by one day. Returns a dict of full-length arrays."""
        idx = slice(None) if mask is None else np.asarray(mask, dtype=bool)
        f = daily_ffmc(self.ffmc[idx], temp[idx], rh[idx], ws[idx], prec24[idx])
        d = daily_dmc(self.dmc[idx], temp[idx], rh[idx], prec24[idx], np.asarray(month)[idx] if np.ndim(month) else month)
        c = daily_dc(self.dc[idx], temp[idx], prec24[idx], np.asarray(month)[idx] if np.ndim(month) else month)
        self.ffmc[idx], self.dmc[idx], self.dc[idx] = f, d, c
        isi = daily_isi(f, ws[idx])
        bui = daily_bui(d, c)
        out = {"FFMC": f, "DMC": d, "DC": c, "ISI": isi, "BUI": bui, "FWI": daily_fwi(isi, bui)}
        return out
