"""Emissions calculation engine.

Methodology verified line-by-line against the January 2024 Expanded Usage
Report and Aggregate Emissions Report v10:

  gallons            = pounds logged / product density (lbs/gal)
  VOC tons (per EU)  = sum(gallons x VOC lbs/gal) / 2000        [matched to 7 dp]
  CAS lbs (facility) = sum(gallons x density x weight fraction)
  Aggregate HAP tons = sum(gallons x EDS HAP lbs/gal) / 2000    [matched 0.7224 vs 0.7221]
  Dibasic ester      = CAS 627-93-0 + 1119-40-0 + 106-65-0, monthly in lbs
  Ethylbenzene       = CAS 100-41-4, monthly in lbs
  Cumene             = CAS 98-82-8, monthly in tons
  IBA lbs/8-hr shift = gallons x IBA lbs/gal x (8 / shift hours) [matched exactly]
  12-month rolling   = trailing 12-month sum (true window)
"""
from collections import defaultdict
from datetime import date
from sqlalchemy.orm import Session, joinedload
from . import models
from .models import DBE_CAS, EB_CAS, CUMENE_CAS, IBA_CAS


def month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def compute_month(db: Session, year: int, month: int, hap_columns, mu_hap_columns):
    """Aggregate one month of usage logs into report metrics."""
    start, end = month_bounds(year, month)
    logs = (db.query(models.UsageLog)
              .options(joinedload(models.UsageLog.product)
                       .joinedload(models.Product.chemicals))
              .filter(models.UsageLog.use_date >= start,
                      models.UsageLog.use_date < end,
                      models.UsageLog.voided == False)  # noqa: E712
              .all())

    eu_gal = defaultdict(float)
    eu_voc_lbs = defaultdict(float)
    cas_lbs = defaultdict(float)          # facility-wide lbs by CAS
    agghap_lbs = 0.0
    group_gals = defaultdict(float)       # gallons of material containing X
    cas_gals = defaultdict(float)
    daily = defaultdict(list)             # eu -> daily-use rows
    iba_rows = []

    for lg in logs:
        p = lg.product
        g = lg.gallons
        eu_gal[lg.emission_unit] += g
        eu_voc_lbs[lg.emission_unit] += g * (p.voc_content or 0.0)
        agghap_lbs += g * (p.eds_hap_content or 0.0)
        content = p.cas_content()
        for cas, lbs_per_gal in content.items():
            if lbs_per_gal:
                cas_lbs[cas] += g * lbs_per_gal
                cas_gals[cas] += g
        if any(content.get(c, 0.0) for c in DBE_CAS):
            group_gals["dbe"] += g
        if content.get(EB_CAS, 0.0):
            group_gals["eb"] += g
        if content.get(CUMENE_CAS, 0.0):
            group_gals["cumene"] += g
        if (p.eds_hap_content or 0.0) > 0:
            group_gals["agghap"] += g

        daily[lg.emission_unit].append({
            "date": lg.use_date.strftime("%Y%m%d"),
            "coating_type": p.coating_type,
            "product": p.number,
            "part_type": lg.part_type,
            "gallons": g,
        })
        iba = content.get(IBA_CAS, 0.0)
        if iba:
            hours = lg.shift_hours or 8.0
            iba_rows.append({
                "date": lg.use_date.strftime("%Y%m%d"),
                "product": p.number,
                "gallons": g,
                "content": iba,
                "lbs_per_8hr": g * iba * (8.0 / hours),
            })

    dbe_lbs = sum(cas_lbs.get(c, 0.0) for c in DBE_CAS)
    hap_tons = {}
    for idx, col in enumerate(hap_columns):
        v = cas_lbs.get(col["cas"], 0.0) / 2000.0
        if v:
            hap_tons[str(idx)] = v
    hap_gals = {}
    for idx, col in enumerate(mu_hap_columns):
        v = cas_gals.get(col["cas"], 0.0)
        if v:
            hap_gals[str(idx)] = v

    emissions = {
        "voc_eu1": eu_voc_lbs.get("EU-CoatingLine-01", 0.0) / 2000.0,
        "voc_eu2": eu_voc_lbs.get("EU-CoatingLine-02", 0.0) / 2000.0,
        "voc_eu3": eu_voc_lbs.get("EU-CoatingLine-03", 0.0) / 2000.0,
        "dbe_lbs": dbe_lbs,
        "eb_lbs": cas_lbs.get(EB_CAS, 0.0),
        "cumene_tons": cas_lbs.get(CUMENE_CAS, 0.0) / 2000.0,
        "agghap_tons": agghap_lbs / 2000.0,
        "hap_tons": hap_tons,
    }
    material_use = {
        "gals_eu1": eu_gal.get("EU-CoatingLine-01", 0.0),
        "gals_eu2": eu_gal.get("EU-CoatingLine-02", 0.0),
        "gals_eu3": eu_gal.get("EU-CoatingLine-03", 0.0),
        "gals_dbe": group_gals.get("dbe", 0.0),
        "gals_eb": group_gals.get("eb", 0.0),
        "gals_cumene": group_gals.get("cumene", 0.0),
        "gals_agghap": group_gals.get("agghap", 0.0),
        "hap_gals": hap_gals,
    }
    for eu in daily:
        daily[eu].sort(key=lambda r: (r["date"], -r["gallons"]))
    iba_rows.sort(key=lambda r: r["date"])
    return emissions, material_use, dict(daily), iba_rows


def monthly_series(db: Session, through_year: int, through_month: int,
                   hap_columns, mu_hap_columns):
    """Merged series Jan 2019 -> report month. Frozen months come from the
    imported v10 history; months after the frozen horizon are computed from
    usage logs. Returns ordered list of dicts with emissions + material use."""
    frozen = {(f.year, f.month): f for f in db.query(models.FrozenMonthly).all()}
    horizon = db.get(models.Setting, "frozen_through")
    hy, hm = (horizon.value if horizon else [2026, 6])

    series = []
    y, m = 2019, 1
    while (y, m) <= (through_year, through_month):
        if (y, m) <= (hy, hm):
            f = frozen.get((y, m))
            em = dict(f.emissions) if f else {}
            mu = dict(f.material_use) if f else {}
            daily, iba = None, None    # frozen daily rows stored separately
        else:
            em, mu, daily, iba = compute_month(db, y, m, hap_columns, mu_hap_columns)
        series.append({"year": y, "month": m, "emissions": em,
                       "material_use": mu, "daily": daily, "iba": iba,
                       "frozen": (y, m) <= (hy, hm)})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    # trailing 12-month rolling sums
    def val(rec, key):
        return rec["emissions"].get(key, 0.0) or 0.0

    for i, rec in enumerate(series):
        window = series[max(0, i - 11): i + 1]
        rec["rolling"] = {
            "voc_eu1": sum(val(r, "voc_eu1") for r in window),
            "voc_eu2": sum(val(r, "voc_eu2") for r in window),
            "voc_eu3": sum(val(r, "voc_eu3") for r in window),
            "dbe_tons": sum(val(r, "dbe_lbs") for r in window) / 2000.0,
            "eb_tons": sum(val(r, "eb_lbs") for r in window) / 2000.0,
            "cumene_tons": sum(val(r, "cumene_tons") for r in window),
            "agghap_tons": sum(val(r, "agghap_tons") for r in window),
        }
        hap_roll = defaultdict(float)
        for r in window:
            for k, v in (r["emissions"].get("hap_tons") or {}).items():
                hap_roll[k] += v
        rec["rolling"]["hap_tons"] = dict(hap_roll)
    return series
