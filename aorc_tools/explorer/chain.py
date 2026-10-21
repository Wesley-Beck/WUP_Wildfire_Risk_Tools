"""
The hourly fire-danger chain, run packet by packet with carry-forward state.

One engine serves two uses:

* ``mode="store"`` -- background jobs: keeps only the four indices and the
  resolution tiers, plus the state at the start and end of the window so any
  stored chunk can later be re-run exactly for a full trace.
* ``mode="trace"`` -- interactive: keeps every intermediate, and runs
  comparator chains (the other daylight basis, the other DMC/DC coefficients,
  the other dead-fuel-moisture model, the other FPI moisture basis) from the
  same starting state so differences can be shown side by side.

The science is called, not re-implemented: aorc_tools.climate_convert,
aorc_tools.fire_indices.{fwi,fuel_moisture,nfdrs,fpi}. Daylight comes from
explorer.timebase (per-cell sun position).

"As temporally and spatially specific as possible" in practice:
  FWI      hourly: hourly FFMC; DMC/DC dry only in daylight hours, per cell
           (cffdrs-ng mode reproduces NRCan's NG_FWI.py reference exactly)
  FWI_daily classic daily FWI at 12:00 LST per cell (Van Wagner 1987; == xclim),
           the form existing products report -- kept alongside for comparison
  ERC, BI  hourly from timelag dead-fuel moisture (1/10/100/1000 h, stateful)
  FPI      hourly weather term (the same 10-h timelag moisture) x daily NDVI term
  inputs   per cell-hour, with the ladder in explorer.ladder / explorer.ndvi when missing
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace

import numpy as np
import pandas as pd

from aorc_tools.climate_convert import (
    celsius_to_fahrenheit, equilibrium_moisture_content, kelvin_to_celsius, ms_to_kph, ms_to_mph,
    specific_to_relative_humidity, wind_components_to_speed,
)
from aorc_tools.config import FWI_DEFAULTS, NFDRS_FUEL_MODELS
from aorc_tools.explorer import timebase
from aorc_tools.explorer.ndvi import fuel_loadings
from aorc_tools.fire_indices.fpi import compute_fpi
from aorc_tools.fire_indices.fuel_moisture import NelsonFuelMoisture, emc_fuel_moisture
from aorc_tools.fire_indices.fwi import HourlyFWI, dc_to_mc, dmc_to_mc, ffmc_to_mc
from aorc_tools.fire_indices.fwi_daily import DC0, DMC0, FFMC0, DailyFWI
from aorc_tools.fire_indices.nfdrs import compute_erc_bi

FDIS = ["FWI", "FPI", "ERC", "BI"]
MC_HERB_CONST = 120.0
MC_WOOD_CONST = 100.0
STATE_KEYS = ("mc_ffmc", "mc_dmc", "mc_dc", "precip_cumulative", "canopy_drying", "fm1", "fm10", "fm100", "fm1000",
              "d_ffmc", "d_dmc", "d_dc", "d_prec", "d_fwi")
FWI_STATE = ("mc_ffmc", "mc_dmc", "mc_dc", "precip_cumulative", "canopy_drying")
DAILY_STATE = ("d_ffmc", "d_dmc", "d_dc", "d_prec", "d_fwi")
DAILY_OBS_LST = 12          # classic FWI observation hour (local standard time)
WX_VARS = ["TMP_2maboveground", "SPFH_2maboveground", "PRES_surface", "UGRD_10maboveground",
           "VGRD_10maboveground", "APCP_surface"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainConfig:
    fuel_model: str = "G"
    fuel_moisture: str = "timelag"        # timelag (hourly, stateful) | emc (stateless multipliers, legacy)
    daylight: str = "solar"               # solar (per-cell sun position, fixed) | legacy-utc
    fwi_coefficients: str = "cffdrs-ng"   # cffdrs-ng (official NG_FWI.py, verified exact) | legacy (old port)
    fpi_moisture: str = "hourly"          # hourly (10-h timelag FM) | daily-burgan (Tmax/RHmin, 1.28*EMC)
    gap_fill: bool = True
    allow_ndvi_climatology: bool = False  # outside the locked FPI decision when True
    legacy_latitude: float = 46.5         # only used by the legacy daylight test

    CHOICES = {
        "fuel_moisture": ("timelag", "emc"),
        "daylight": ("solar", "legacy-utc"),
        "fwi_coefficients": ("cffdrs-ng", "legacy"),
        "fpi_moisture": ("hourly", "daily-burgan"),
    }

    def validate(self):
        if self.fuel_model not in NFDRS_FUEL_MODELS:
            raise ValueError(f"unknown fuel model {self.fuel_model}")
        for k, allowed in self.CHOICES.items():
            if getattr(self, k) not in allowed:
                raise ValueError(f"{k} must be one of {allowed}")
        return self

    def as_dict(self):
        d = asdict(self)
        return {k: d[k] for k in sorted(d)}

    @property
    def id(self) -> str:
        return hashlib.sha1(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()[:10]

    @classmethod
    def from_dict(cls, d: dict | None):
        d = d or {}
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        if "fuel_model" in known:
            known["fuel_model"] = str(known["fuel_model"]).upper()
        if known.get("fuel_moisture") == "nelson":
            known["fuel_moisture"] = "timelag"
        return cls(**known).validate()

    def label(self) -> str:
        return (f"FM {self.fuel_model} · {self.fuel_moisture} · daylight {self.daylight} · "
                f"FWI {self.fwi_coefficients} · FPI {self.fpi_moisture}" + (" · gap-fill" if self.gap_fill else ""))


PRESETS = {
    "hourly-default": ChainConfig(),
    "legacy-batch": ChainConfig(fuel_moisture="emc", daylight="legacy-utc", fwi_coefficients="legacy",
                                fpi_moisture="daily-burgan", gap_fill=False),
}


def other(cfg: ChainConfig, key: str) -> ChainConfig:
    a, b = ChainConfig.CHOICES[key]
    return replace(cfg, **{key: b if getattr(cfg, key) == a else a})


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def cold_state(n: int) -> dict:
    nel = NelsonFuelMoisture(n)
    return {
        "mc_ffmc": np.full(n, ffmc_to_mc(FWI_DEFAULTS["FFMC"])),
        "mc_dmc": np.full(n, dmc_to_mc(FWI_DEFAULTS["DMC"])),
        "mc_dc": np.full(n, dc_to_mc(FWI_DEFAULTS["DC"])),
        "precip_cumulative": np.zeros(n),
        "canopy_drying": np.zeros(n),
        **{k: nel.state[k].copy() for k in ("fm1", "fm10", "fm100", "fm1000")},
        "d_ffmc": np.full(n, FFMC0), "d_dmc": np.full(n, DMC0), "d_dc": np.full(n, DC0),
        "d_prec": np.zeros(n), "d_fwi": np.full(n, np.nan),
    }


def _snapshot(fwi: HourlyFWI, nel: NelsonFuelMoisture | None, n: int, daily: "DailyState | None" = None) -> dict:
    s = _fwi_arrays(fwi)
    if daily is not None:
        s.update(daily.snapshot())
    if nel is not None:
        s.update({k: nel.state[k].copy() for k in ("fm1", "fm10", "fm100", "fm1000")})
    else:
        s.update({k: np.full(n, np.nan) for k in ("fm1", "fm10", "fm100", "fm1000")})
    return s


def _fwi_arrays(f: HourlyFWI) -> dict:
    return {"mc_ffmc": f.mc_ffmc.copy(), "mc_dmc": f.mc_dmc.copy(), "mc_dc": f.mc_dc.copy(),
            "precip_cumulative": f.precip_cumulative.copy(), "canopy_drying": f.canopy_drying.copy()}


def _restore_fwi(f: HourlyFWI, saved: dict, bad):
    f.mc_ffmc[bad] = saved["mc_ffmc"][bad]
    f.mc_dmc[bad] = saved["mc_dmc"][bad]
    f.mc_dc[bad] = saved["mc_dc"][bad]
    f.precip_cumulative[bad] = saved["precip_cumulative"][bad]
    f.canopy_drying[bad] = saved["canopy_drying"][bad]


class DailyState:
    """Classic daily FWI carried alongside the hourly chain: one update per cell at 12:00 LST,
    rain = total of the 24 hour-ending packets up to and including that noon packet."""

    def __init__(self, n, state=None):
        st = state or {}
        ok = all(k in st and np.isfinite(np.asarray(st[k], dtype=float)).all() for k in ("d_ffmc", "d_dmc", "d_dc"))
        if ok:
            self.f = DailyFWI(n, st["d_ffmc"], st["d_dmc"], st["d_dc"])
            self.prec = np.nan_to_num(np.array(st.get("d_prec", np.zeros(n)), dtype=np.float64))
            self.fwi = np.array(st.get("d_fwi", np.full(n, np.nan)), dtype=np.float64)
        else:
            self.f, self.prec, self.fwi = DailyFWI(n), np.zeros(n), np.full(n, np.nan)
        self.last = {k: np.full(n, np.nan) for k in ("FFMC", "DMC", "DC", "ISI", "BUI")}
        self.last.update({"FFMC": self.f.ffmc.copy(), "DMC": self.f.dmc.copy(), "DC": self.f.dc.copy()})

    def snapshot(self):
        return {"d_ffmc": self.f.ffmc.copy(), "d_dmc": self.f.dmc.copy(), "d_dc": self.f.dc.copy(),
                "d_prec": self.prec.copy(), "d_fwi": self.fwi.copy()}

    def step(self, ok, lst_hour, month_lst, t, rh, ws, p):
        self.prec = self.prec + np.where(ok, p, 0.0)
        noon = lst_hour == DAILY_OBS_LST
        upd = noon & ok
        if upd.any():
            r = self.f.update(t, rh, ws, self.prec, month_lst, mask=upd)
            self.fwi[upd] = r["FWI"]
            for k in self.last:
                self.last[k][upd] = r[k]
        self.prec = np.where(upd, 0.0, self.prec)        # an invalid noon carries its rain to the next valid one
        return noon


def subset_state(state: dict, idx) -> dict:
    return {k: np.asarray(v)[idx] for k, v in state.items()}


def _fwi_from_state(n, state, cfg, coefficients=None):
    init = None
    if state is not None:
        init = {k: np.asarray(state[k], dtype=np.float64) for k in FWI_STATE if k in state}
        init["canopy_drying"] = np.nan_to_num(init.get("canopy_drying", np.zeros(n)))
        init["precip_cumulative"] = np.nan_to_num(init["precip_cumulative"])
    return HourlyFWI(n, latitude=cfg.legacy_latitude, initial_state=init,
                     coefficients=coefficients or cfg.fwi_coefficients)


def _nel_from_state(n, state):
    if state is None or not np.isfinite(np.asarray(state.get("fm1", [np.nan]))).all():
        return NelsonFuelMoisture(n)
    return NelsonFuelMoisture(n, initial_fm={k: state[k] for k in ("fm1", "fm10", "fm100", "fm1000")})


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ChainResult:
    times: pd.DatetimeIndex
    point_ids: np.ndarray
    steps: dict
    start_state: dict
    end_state: dict
    spinup_hours: int
    cfg: ChainConfig
    notes: list = field(default_factory=list)
    alts: dict = field(default_factory=dict)       # comparator step id -> label
    meta: dict = field(default_factory=dict)

    @property
    def analysis_mask(self):
        m = np.ones(len(self.times), dtype=bool)
        m[: self.spinup_hours] = False
        return m


def _trailing(arr, window, fn):
    out = np.empty_like(arr)
    for i in range(arr.shape[0]):
        with np.errstate(all="ignore"):
            out[i] = fn(arr[max(0, i - window + 1): i + 1], axis=0)
    return out


def _nfdrs(models, fm, ws_mph):
    """compute_erc_bi per fuel-model group (per-cell fuel models)."""
    n = len(ws_mph)
    out = {k: np.full(n, np.nan) for k in ("SC", "ERC", "BI")}
    for m in np.unique(models):
        idx = np.where(models == m)[0]
        r = compute_erc_bi(fm["fm1"][idx], fm["fm10"][idx], fm["fm100"][idx], fm["fm1000"][idx],
                           np.full(len(idx), MC_HERB_CONST), np.full(len(idx), MC_WOOD_CONST), ws_mph[idx], fuel_model=m)
        for k in out:
            out[k][idx] = r[k]
    return out


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

def run_chain(raw, *, grid, cfg: ChainConfig = ChainConfig(), state: dict | None = None, spinup_hours: int = 0,
              mode: str = "trace", ndvi=None, fuel_map=None, climatology=None) -> ChainResult:
    cfg.validate()
    trace = mode == "trace"
    n_t, n_c = raw.n_hours, raw.n_cells
    rows = grid.rows_for(raw.point_ids)
    lat = grid.points.latitude.values[rows].astype(float)
    lon = grid.points.longitude.values[rows].astype(float)
    std_off = grid.points.std_offset_h.values[rows].astype(int)
    tiers = getattr(raw, "tiers", None)
    if tiers is None:
        tiers = np.zeros((n_t, n_c), dtype=np.uint8)
    notes = []

    # ---- conversions (identical functions to extract.py)
    temp_k = raw.data["TMP_2maboveground"].astype(np.float64)
    temp_c = kelvin_to_celsius(temp_k)
    temp_f = celsius_to_fahrenheit(temp_c)
    rh = specific_to_relative_humidity(raw.data["SPFH_2maboveground"].astype(np.float64), temp_k,
                                       raw.data["PRES_surface"].astype(np.float64))
    ws_ms = wind_components_to_speed(raw.data["UGRD_10maboveground"].astype(np.float64),
                                     raw.data["VGRD_10maboveground"].astype(np.float64))
    ws_kph, ws_mph = ms_to_kph(ws_ms), ms_to_mph(ws_ms)
    precip = raw.data["APCP_surface"].astype(np.float64)
    valid = np.isfinite(temp_c) & np.isfinite(rh) & np.isfinite(ws_kph) & np.isfinite(precip)

    S: dict[str, np.ndarray] = {}
    if trace:
        for v in raw.data:
            S[v] = raw.data[v].astype(np.float32)
        for k, a in [("temperature_c", temp_c), ("temperature_f", temp_f), ("relative_humidity", rh),
                     ("wind_speed_ms", ws_ms), ("wind_speed_kph", ws_kph), ("wind_speed_mph", ws_mph),
                     ("precipitation_mm", precip)]:
            S[k] = a.astype(np.float32)

    # ---- fuel models per cell (ladder: per-cell map -> default)
    if fuel_map is not None:
        models, fm_tier = fuel_map.models(raw.point_ids, cfg.fuel_model)
    else:
        models, fm_tier = np.full(n_c, cfg.fuel_model, dtype=object), np.ones(n_c, dtype=np.uint8)

    # ---- chains
    fwi = _fwi_from_state(n_c, state, cfg)
    daily = DailyState(n_c, state)
    nel = _nel_from_state(n_c, state) if cfg.fuel_moisture == "timelag" else None
    alt_dl = alt_cf = alt_fm = None
    alts = {}
    if trace:
        alt_dl = _fwi_from_state(n_c, state, cfg)                                   # other daylight basis
        alt_cf = _fwi_from_state(n_c, state, cfg, coefficients=other(cfg, "fwi_coefficients").fwi_coefficients)
        alt_fm = _nel_from_state(n_c, state) if cfg.fuel_moisture == "emc" else None  # other moisture model
        o = other(cfg, "daylight").daylight
        alts.update({f"{k}@daylight": f"{k} with {o} daylight" for k in ("DMC", "DC", "BUI", "FWI")})
        o = other(cfg, "fwi_coefficients").fwi_coefficients
        alts.update({f"{k}@coef": f"{k} with the {o} FWI port" for k in ("FFMC", "DMC", "DC", "BUI", "FWI")})
        o = other(cfg, "fuel_moisture").fuel_moisture
        alts.update({f"{k}@fm": f"{k} with {o} dead-fuel moisture" for k in ("FM1000", "ERC", "BI")})

    keep_trace = ["mc_ffmc", "FFMC", "precip_cumulative", "mc_dmc", "DMC", "mc_dc", "DC", "ISI", "BUI",
                  "EMC", "FM1", "FM10", "FM100", "FM1000", "SC", "solar_elevation", "daylight_fixed",
                  "daylight_legacy", "daylight_used", "lst_hour", "sunrise_lst", "sunset_lst",
                  "MC_HERB", "MC_WOOD", "precip_24h_noon", "FFMC_daily", "DMC_daily", "DC_daily", "ISI_daily",
                  "BUI_daily", "canopy_drying"] + list(alts)
    for k in ["FWI", "FWI_daily", "ERC", "BI"] + (keep_trace if trace else []):
        S[k] = np.full((n_t, n_c), np.nan, dtype=np.float32)
    fm10_hourly = np.full((n_t, n_c), np.nan, dtype=np.float64)
    if trace:
        S["MC_HERB"][:] = MC_HERB_CONST
        S["MC_WOOD"][:] = MC_WOOD_CONST
        S["fuel_model_tier"] = np.broadcast_to(fm_tier, (n_t, n_c)).astype(np.float32).copy()
        # sunrise/sunset of each packet's local-standard day
        day_lst = ((raw.times.values.astype("datetime64[h]").astype(np.int64)[:, None] + std_off[None]) // 24)
        for d in np.unique(day_lst):
            date = pd.Timestamp(int(d) * 24 * 3600, unit="s")
            st = timebase.sun_times_utc(lat, lon, date)
            m = day_lst == d
            sr = np.broadcast_to(st["sunrise"] + std_off, (n_t, n_c))
            ss = np.broadcast_to(st["sunset"] + std_off, (n_t, n_c))
            S["sunrise_lst"][m] = sr[m]
            S["sunset_lst"][m] = ss[m]
        S["lst_hour"][:] = (raw.times.hour.values[:, None] + std_off[None]) % 24

    lst_hours = (raw.times.hour.values[:, None] + std_off[None]) % 24
    lst_month = (raw.times.values.astype("datetime64[h]")[:, None] + std_off[None].astype("timedelta64[h]")
                 ).astype("datetime64[M]").astype(int) % 12 + 1
    start_state = None
    for i, ts in enumerate(raw.times):
        if i == spinup_hours:
            start_state = _snapshot(fwi, nel, n_c, daily)
        doy, hr = ts.dayofyear, ts.hour
        ok = valid[i]
        zen = timebase.solar_zenith(lat, lon, ts)
        dl_fixed = zen < timebase.ZENITH_HORIZON
        dl_leg = timebase.daylight_legacy(ts, cfg.legacy_latitude)
        dl_used = dl_fixed if cfg.daylight == "solar" else None
        t_i, rh_i, w_i, p_i = (np.where(ok, a[i], 0.0) for a in (temp_c, rh, ws_kph, precip))
        tf_i, wm_i = np.where(ok, temp_f[i], 32.0), np.where(ok, ws_mph[i], 0.0)

        prev = _snapshot(fwi, nel, n_c) if not ok.all() else None
        p24 = daily.prec + np.where(ok, p_i, 0.0)
        daily.step(ok, lst_hours[i], lst_month[i], t_i, rh_i, w_i, p_i)
        prev_alt = ([_fwi_arrays(c) for c in (alt_dl, alt_cf)], (None if alt_fm is None else {k: alt_fm.state[k].copy() for k in alt_fm.state})) \
            if (trace and prev is not None) else None
        res = fwi.update(t_i, rh_i, w_i, p_i, doy, hr, daylight=dl_used)
        if nel is not None:
            fm = nel.update(tf_i, rh_i, p_i)
        else:
            fm = emc_fuel_moisture(tf_i, rh_i)
        if prev is not None:                         # tier-9 packets: hold state, output NaN
            bad = ~ok
            for k, arr in (("mc_ffmc", fwi.mc_ffmc), ("mc_dmc", fwi.mc_dmc), ("mc_dc", fwi.mc_dc),
                           ("precip_cumulative", fwi.precip_cumulative), ("canopy_drying", fwi.canopy_drying)):
                arr[bad] = prev[k][bad]
            if nel is not None:
                for k in ("fm1", "fm10", "fm100", "fm1000"):
                    nel.state[k][bad] = prev[k][bad]
        nf = _nfdrs(models, fm, wm_i)
        nan_bad = lambda a: np.where(ok, a, np.nan)  # noqa: E731
        S["FWI"][i] = nan_bad(res["FWI"])
        S["FWI_daily"][i] = daily.fwi
        S["ERC"][i] = nan_bad(nf["ERC"])
        S["BI"][i] = nan_bad(nf["BI"])
        fm10_hourly[i] = nan_bad(fm["fm10"])

        if trace:
            S["mc_ffmc"][i], S["mc_dmc"][i], S["mc_dc"][i] = fwi.mc_ffmc, fwi.mc_dmc, fwi.mc_dc
            S["precip_cumulative"][i] = fwi.precip_cumulative
            S["canopy_drying"][i] = fwi.canopy_drying
            S["precip_24h_noon"][i] = np.where(lst_hours[i] == DAILY_OBS_LST, p24, np.nan)
            for k in ("FFMC", "DMC", "DC", "ISI", "BUI"):
                S[f"{k}_daily"][i] = daily.last[k]
            for k in ("FFMC", "DMC", "DC", "ISI", "BUI"):
                S[k][i] = nan_bad(res[k])
            emc = equilibrium_moisture_content(tf_i, rh_i)
            S["EMC"][i] = nan_bad(emc)
            for k in ("fm1", "fm10", "fm100", "fm1000"):
                S[k.upper()][i] = nan_bad(fm[k])
            S["SC"][i] = nan_bad(nf["SC"])
            S["solar_elevation"][i] = 90.0 - zen
            S["daylight_fixed"][i] = dl_fixed
            S["daylight_legacy"][i] = dl_leg
            S["daylight_used"][i] = fwi.last_daylight
            # comparators
            r_dl = alt_dl.update(t_i, rh_i, w_i, p_i, doy, hr, daylight=(None if cfg.daylight == "solar" else dl_fixed))
            r_cf = alt_cf.update(t_i, rh_i, w_i, p_i, doy, hr, daylight=dl_used)
            for k in ("DMC", "DC", "BUI", "FWI"):
                S[f"{k}@daylight"][i] = nan_bad(r_dl[k])
            for k in ("FFMC", "DMC", "DC", "BUI", "FWI"):
                S[f"{k}@coef"][i] = nan_bad(r_cf[k])
            fm_o = emc_fuel_moisture(tf_i, rh_i) if cfg.fuel_moisture == "timelag" else alt_fm.update(tf_i, rh_i, p_i)
            nf_o = _nfdrs(models, fm_o, wm_i)
            if prev_alt is not None:
                bad = ~ok
                for c, saved in zip((alt_dl, alt_cf), prev_alt[0]):
                    _restore_fwi(c, saved, bad)
                if alt_fm is not None:
                    for k in alt_fm.state:
                        alt_fm.state[k][bad] = prev_alt[1][k][bad]
            S["FM1000@fm"][i] = nan_bad(fm_o["fm1000"])
            S["ERC@fm"][i] = nan_bad(nf_o["ERC"])
            S["BI@fm"][i] = nan_bad(nf_o["BI"])
    if start_state is None:
        start_state = _snapshot(fwi, nel, n_c, daily)
    end_state = _snapshot(fwi, nel, n_c, daily)

    # ---- FPI (Burgan via the Argonne port), NDVI ladder
    S["wx_tier"] = tiers.astype(np.uint8)
    nd = ndvi.get(grid, raw.point_ids, raw.times) if ndvi is not None else None
    tmax24 = _trailing(temp_f, 24, np.nanmax)
    rhmin24 = _trailing(rh, 24, np.nanmin)
    S["FPI"] = np.full((n_t, n_c), np.nan, dtype=np.float32)
    S["ndvi_tier"] = np.full((n_t, n_c), 9, dtype=np.uint8)
    if nd is None:
        notes.append("FPI not computed: no NDVI inputs configured (data/static, see USAGE.md).")
    else:
        load = fuel_loadings(models)
        b = lambda a: np.broadcast_to(np.asarray(a, dtype=float), (n_t, n_c))  # noqa: E731
        llfm, dlfm, mxd = (b(nd[k]) if k in nd else b(load[k]) for k in ("llfm", "dlfm", "mxd"))
        nd0 = b(nd["nd0"])

        def fpi_for(basis):
            if basis == "hourly":
                r = compute_fpi(nd0, b(nd["nd_min"]), b(nd["nd_max"]), llfm, dlfm, mxd, tmax24, rhmin24, fm10=fm10_hourly)
            else:
                r = compute_fpi(nd0, b(nd["nd_min"]), b(nd["nd_max"]), llfm, dlfm, mxd, tmax24, rhmin24)
            ok_f = np.isfinite(nd0) & valid
            return {k: np.where(ok_f, v, np.nan) for k, v in r.items()}

        main = fpi_for(cfg.fpi_moisture)
        S["FPI"] = main["FPI"].astype(np.float32)
        S["ndvi_tier"] = np.asarray(nd["tier"], dtype=np.uint8)
        if trace:
            S["NDVI"] = nd0.astype(np.float32)
            S["ND_MIN"], S["ND_MAX"] = b(nd["nd_min"]).astype(np.float32), b(nd["nd_max"]).astype(np.float32)
            S["RG"] = main["RG"].astype(np.float32)
            S["FM10_FPI"] = main["FM10"].astype(np.float32)
            S["TMAX24_F"], S["RHMIN24"] = tmax24.astype(np.float32), rhmin24.astype(np.float32)
            S["EMC_FPI"] = fpi_for("daily-burgan")["EMC"].astype(np.float32)
            o = other(cfg, "fpi_moisture").fpi_moisture
            S["FPI@fpi"] = fpi_for(o)["FPI"].astype(np.float32)
            alts["FPI@fpi"] = f"FPI with {o} moisture basis"
        if getattr(ndvi, "name", "") == "synthetic":
            notes.append("NDVI is SYNTHETIC (demo).")

    # ---- percentiles (post-pass)
    if climatology is not None and getattr(climatology, "available", False):
        for k in FDIS:
            if climatology.has(k):
                S[f"{k}_pct"] = climatology.percentile(k, S[k]).astype(np.float32)
        notes.append(f"Percentiles: {climatology.describe()}")

    if spinup_hours > 0 and state is None:
        notes.append(f"Cold start (FFMC {FWI_DEFAULTS['FFMC']}, DMC {FWI_DEFAULTS['DMC']}, DC {FWI_DEFAULTS['DC']}) "
                     f"{spinup_hours} h before the window; spin-up excluded from the analysis window.")
    elif state is not None:
        notes.append("Started from stored state (continuous with the results store).")
    else:
        notes.append("Cold start at the first hour (no spin-up).")
    n_bad = int((~valid[spinup_hours:]).sum())
    if n_bad:
        notes.append(f"{n_bad} packet(s) had no usable weather even after the ladder (tier 9): indices NaN, state held.")

    return ChainResult(times=raw.times, point_ids=np.asarray(raw.point_ids), steps=S, start_state=start_state,
                       end_state=end_state, spinup_hours=spinup_hours, cfg=cfg, notes=notes, alts=alts,
                       meta={"fuel_models": {str(k): int(v) for k, v in zip(*np.unique(models, return_counts=True))},
                             "fuel_model_tier": {int(k): int(v) for k, v in zip(*np.unique(fm_tier, return_counts=True))}})
