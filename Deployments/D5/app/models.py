"""Data model.

Products hold the Environmental Data Sheet (EDS) values. Chemical rows hold the
CAS-level breakdown; per-CAS content in lbs/gal is density x weight fraction,
which is exactly how the Material Content Report is derived.

Usage is logged per event. Quantity may be entered in pounds (as the scales
report) or gallons; gallons are always derived/stored so reports never repeat
the manual lbs-to-gal conversion. Nothing is ever hard-deleted: voiding a log
keeps the row with an audit trail, replacing the old monthly Excel cleanup.
"""
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Float, Boolean, Date, DateTime,
                        ForeignKey, Text, JSON)
from sqlalchemy.orm import relationship
from .database import Base

COATING_TYPES = {"1": "Basecoat", "2": "Hardener", "3": "Solvent", "4": "Primer", "5": "Clearcoat"}
PART_TYPES = ["Automotive", "Non-Automotive Specialty"]
EMISSION_UNITS = ["EU-CoatingLine-01", "EU-CoatingLine-02", "EU-CoatingLine-03"]
DBE_CAS = {"627930", "1119400", "119400", "106650"}   # dimethyl adipate / glutarate / succinate
EB_CAS = "100414"
CUMENE_CAS = "98828"
IBA_CAS = "110190"


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    number = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(255), default="")
    supplier = Column(String(255), default="")
    coating_type = Column(String(32), default="Basecoat")       # Basecoat/Hardener/Solvent/Primer/Clearcoat
    density = Column(Float, default=0.0)                        # lbs/gal (ProdWperV)
    specific_gravity = Column(Float, default=0.0)
    voc_content = Column(Float, default=0.0)                    # regulatory VOC lbs/gal (less exempts)
    voc_received = Column(Float, default=0.0)                   # VOC as received lbs/gal
    volatile_weight_pct = Column(Float, default=0.0)
    solids_volume_pct = Column(Float, default=0.0)
    eds_hap_content = Column(Float, default=0.0)                # aggregate HAP lbs/gal from EDS
    as_applied_voc = Column(Float, nullable=True)               # lbs/gal, as-applied mix
    as_applied_category = Column(String(64), default="")
    default_part_type = Column(String(64), default="Automotive")
    active = Column(Boolean, default=True)
    historical_only = Column(Boolean, default=False)            # kept for Material Content Report continuity
    content_override = Column(JSON, nullable=True)              # legacy per-CAS lbs/gal for historical-only products
    notes = Column(Text, default="")
    mix = Column(JSON, nullable=True)   # as-applied mix: [{"label":"A","product_number":"..","ratio":8.0},..]
    chemicals = relationship("ProductChemical", back_populates="product",
                             cascade="all, delete-orphan")

    def cas_content(self):
        """Per-CAS content lbs/gal.

        Default derivation is density x weight fraction from the chemical rows
        (deduplicated - the PDS export sometimes repeats a chemical row).
        For the permit-tracked CAS numbers (cumene, the three dibasic esters,
        ethylbenzene) the hand-entered EDS values from the Material Content
        Report are AUTHORITATIVE when present, matching how the filed reports
        were produced: supplier EDS revisions land there first."""
        out = {}
        seen = set()
        for c in self.chemicals:
            key = (c.cas, c.name, c.wt_fraction)
            if key in seen:
                continue
            seen.add(key)
            out[c.cas] = out.get(c.cas, 0.0) + self.density * c.wt_fraction
        if self.content_override:
            ov = self.content_override
            for cas, k in ((CUMENE_CAS, "cumene"), ("627930", "dma"),
                           ("1119400", "dmg"), ("106650", "dms"), (EB_CAS, "eb")):
                if k in ov:
                    out[cas] = ov[k]
        return out


def as_applied_voc_effective(product, by_number):
    """Volume-weighted as-applied VOC from the mix ratio, e.g. A:B:C = 8:2:1
    -> (8*VOCa + 2*VOCb + 1*VOCc) / 11. Falls back to the manually entered
    as_applied_voc when no mix is defined. Always computed from the components'
    CURRENT VOC values, so supplier revisions flow through automatically."""
    if product.mix:
        total = weighted = 0.0
        for comp in product.mix:
            p = by_number.get(str(comp.get("product_number", "")).strip())
            try:
                r = float(comp.get("ratio") or 0)
            except (TypeError, ValueError):
                r = 0.0
            if p is None or r <= 0:
                continue
            total += r
            weighted += r * (p.voc_content or 0.0)
        if total > 0:
            return weighted / total
    return product.as_applied_voc


class ProductChemical(Base):
    __tablename__ = "product_chemicals"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    cas = Column(String(32), index=True)          # stored without dashes
    name = Column(String(255))
    wt_fraction = Column(Float, default=0.0)      # 0.05 = 5% by weight
    is_hap = Column(Boolean, default=False)
    is_solid = Column(Boolean, default=False)
    product = relationship("Product", back_populates="chemicals")


class UsageLog(Base):
    __tablename__ = "usage_logs"
    id = Column(Integer, primary_key=True)
    use_date = Column(Date, index=True, nullable=False)
    emission_unit = Column(String(32), index=True, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Float, nullable=False)          # as entered
    unit = Column(String(8), default="lbs")           # 'lbs' or 'gal'
    gallons = Column(Float, nullable=False)           # derived, authoritative
    part_type = Column(String(64), default="Automotive")
    shift = Column(String(32), default="Shift 1")
    shift_hours = Column(Float, default=8.5)
    employee = Column(String(128), default="")
    notes = Column(Text, default="")
    voided = Column(Boolean, default=False, index=True)
    void_reason = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(128), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(128), default="")
    product = relationship("Product")


class FrozenMonthly(Base):
    """Historical monthly values imported verbatim from Aggregate Emissions
    Report v10 (Jan 2019 - Jun 2026). Months at or before the frozen horizon
    always report these values; later months are computed from usage logs."""
    __tablename__ = "frozen_monthly"
    id = Column(Integer, primary_key=True)
    year = Column(Integer, index=True)
    month = Column(Integer, index=True)
    emissions = Column(JSON)       # voc_eu1.., dbe_lbs, eb_lbs, cumene_tons, agghap_tons, hap_tons{idx: v}
    material_use = Column(JSON)    # gals_eu1.., gals_dbe.., hap_gals{idx: v}


class FrozenDailyRow(Base):
    """Historical daily-detail rows (Daily Use tabs and Isobutyl Acetate tab)."""
    __tablename__ = "frozen_daily_rows"
    id = Column(Integer, primary_key=True)
    sheet = Column(String(64), index=True)   # 'IBA' or an emission unit name
    row = Column(JSON)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(JSON)
