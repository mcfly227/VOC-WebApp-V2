"""First-run seeding.

Loads seed_data/seed.json (extracted from Aggregate Emissions Report v10 and
the Jan-2024 Expanded Usage Report):
  - product library with chemical breakdowns and EDS content values
  - frozen monthly history Jan 2019 - Jun 2026
  - frozen daily-detail rows for the Daily Use and Isobutyl Acetate tabs
  - report column structure (full federal HAP list)
"""
import json
import os
from sqlalchemy.orm import Session
from . import models
from .models import COATING_TYPES

SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "seed_data", "seed.json")


def run(db: Session):
    if db.query(models.Setting).get("seeded"):
        return
    with open(SEED_PATH) as f:
        seed = json.load(f)

    db.add(models.Setting(key="hap_columns", value=seed["hap_columns"]))
    db.add(models.Setting(key="mu_hap_columns", value=seed["mu_hap_columns"]))
    db.add(models.Setting(key="frozen_through", value=[2026, 6]))

    mu_by_key = {(m["year"], m["month"]): m for m in seed["history_material_use"]}
    for em in seed["history_emissions"]:
        mu = mu_by_key.get((em["year"], em["month"]), {})
        db.add(models.FrozenMonthly(
            year=em["year"], month=em["month"],
            emissions={k: v for k, v in em.items() if k not in ("year", "month")},
            material_use={k: v for k, v in mu.items() if k not in ("year", "month")}))

    for r in seed["frozen_iba"]:
        db.add(models.FrozenDailyRow(sheet="IBA", row=r))
    for eu, rows in seed["frozen_daily"].items():
        for r in rows:
            db.add(models.FrozenDailyRow(sheet=eu, row=r))

    content = seed.get("material_content", {})
    for p in seed["products"]:
        if p["number"] in content and not p.get("content_override"):
            p["content_override"] = content[p["number"]]
        prod = models.Product(
            number=p["number"], name=p.get("name", ""), supplier=p.get("supplier", ""),
            coating_type=COATING_TYPES.get(p.get("prod_type", ""), "Basecoat"),
            density=p.get("density", 0.0), specific_gravity=p.get("sg", 0.0),
            voc_content=p.get("voc", 0.0), voc_received=p.get("voc_recvd", 0.0),
            volatile_weight_pct=p.get("volatile_w", 0.0),
            solids_volume_pct=p.get("solids_v", 0.0),
            eds_hap_content=p.get("eds_hap_content", 0.0),
            as_applied_voc=p.get("as_applied_voc"),
            as_applied_category=p.get("as_applied_category", ""),
            default_part_type=p.get("default_part_type", "Automotive"),
            historical_only=p.get("historical_only", False),
            content_override=p.get("content_override"))
        for c in p.get("chemicals", []):
            prod.chemicals.append(models.ProductChemical(
                cas=c["cas"].replace("-", ""), name=c["name"],
                wt_fraction=c["wt_fraction"], is_hap=c["is_hap"],
                is_solid=c["is_solid"]))
        db.add(prod)

    db.add(models.Setting(key="seeded", value=True))
    db.commit()
