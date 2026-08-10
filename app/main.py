"""EGLE Air Emissions Tracker - PTI 183-15.

Run locally:   uvicorn app.main:app --reload
On Azure App Service, authentication is handled by Easy Auth (Entra ID);
the signed-in user's name arrives in the X-MS-CLIENT-PRINCIPAL-NAME header
and is stamped onto every write for the audit trail.
"""
import io
from datetime import date, datetime
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload
import os

from .database import Base, engine, get_db
from . import models, seed, reports, calc

app = FastAPI(title="EGLE Air Emissions Tracker", version="1.0")


def _migrate():
    """Additive schema upgrades for databases created by earlier versions."""
    from sqlalchemy import inspect as sqla_inspect, text
    insp = sqla_inspect(engine)
    cols = [col["name"] for col in insp.get_columns("products")]
    if "mix" not in cols:
        coltype = "NVARCHAR(MAX)" if engine.dialect.name == "mssql" else "TEXT"
        kw = "" if engine.dialect.name == "mssql" else "COLUMN "
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE products ADD {kw}mix {coltype}"))


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    _migrate()
    from .database import SessionLocal
    db = SessionLocal()
    try:
        seed.run(db)
    finally:
        db.close()


def current_user(request: Request) -> str:
    return request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "local-user")


# ---------- Products ----------

class ChemicalIn(BaseModel):
    cas: str
    name: str
    wt_fraction: float = Field(ge=0, le=1, description="0.05 = 5% by weight")
    is_hap: bool = False
    is_solid: bool = False


class ProductIn(BaseModel):
    number: str
    name: str = ""
    supplier: str = ""
    coating_type: str = "Basecoat"
    density: float = 0.0
    specific_gravity: float = 0.0
    voc_content: float = 0.0
    voc_received: float = 0.0
    volatile_weight_pct: float = 0.0
    solids_volume_pct: float = 0.0
    eds_hap_content: float = 0.0
    as_applied_voc: Optional[float] = None
    as_applied_category: str = ""
    default_part_type: str = "Automotive"
    active: bool = True
    notes: str = ""
    content_override: Optional[dict] = None   # EDS-authoritative lbs/gal for tracked CAS
    mix: Optional[list] = None                # [{"label":"A","product_number":"..","ratio":8}]
    chemicals: list[ChemicalIn] = []


def product_out(p: models.Product, by_number=None):
    cc = p.cas_content()
    eff = models.as_applied_voc_effective(p, by_number) if by_number else p.as_applied_voc
    return {
        "id": p.id, "number": p.number, "name": p.name, "supplier": p.supplier,
        "coating_type": p.coating_type, "density": p.density,
        "specific_gravity": p.specific_gravity, "voc_content": p.voc_content,
        "voc_received": p.voc_received, "volatile_weight_pct": p.volatile_weight_pct,
        "solids_volume_pct": p.solids_volume_pct, "eds_hap_content": p.eds_hap_content,
        "as_applied_voc": p.as_applied_voc, "as_applied_category": p.as_applied_category,
        "default_part_type": p.default_part_type, "active": p.active,
        "historical_only": p.historical_only, "notes": p.notes,
        "content_override": p.content_override, "mix": p.mix,
        "as_applied_effective": eff,
        "chemicals": [{"cas": c.cas, "name": c.name, "wt_fraction": c.wt_fraction,
                       "is_hap": c.is_hap, "is_solid": c.is_solid} for c in p.chemicals],
        "derived": {
            "cumene": cc.get(models.CUMENE_CAS, 0.0),
            "dibasic_ester": sum(cc.get(c, 0.0) for c in models.DBE_CAS),
            "ethylbenzene": cc.get(models.EB_CAS, 0.0),
            "isobutyl_acetate": cc.get(models.IBA_CAS, 0.0),
        },
    }


@app.get("/api/products")
def list_products(include_historical: bool = False, db: Session = Depends(get_db)):
    q = db.query(models.Product).options(joinedload(models.Product.chemicals))
    if not include_historical:
        q = q.filter(models.Product.historical_only == False)  # noqa: E712
    by_number = {p.number: p for p in db.query(models.Product).all()}
    return [product_out(p, by_number) for p in q.order_by(models.Product.number)]


@app.post("/api/products")
def create_product(body: ProductIn, db: Session = Depends(get_db)):
    if db.query(models.Product).filter_by(number=body.number).first():
        raise HTTPException(409, f"Product {body.number} already exists")
    p = models.Product(**body.dict(exclude={"chemicals"}))
    for c in body.chemicals:
        p.chemicals.append(models.ProductChemical(
            cas=c.cas.replace("-", ""), name=c.name, wt_fraction=c.wt_fraction,
            is_hap=c.is_hap, is_solid=c.is_solid))
    db.add(p); db.commit()
    return product_out(p, {x.number: x for x in db.query(models.Product).all()})


@app.put("/api/products/{pid}")
def update_product(pid: int, body: ProductIn, db: Session = Depends(get_db)):
    p = db.get(models.Product, pid)
    if not p:
        raise HTTPException(404, "Not found")
    for k, v in body.dict(exclude={"chemicals"}).items():
        setattr(p, k, v)
    p.chemicals.clear()
    for c in body.chemicals:
        p.chemicals.append(models.ProductChemical(
            cas=c.cas.replace("-", ""), name=c.name, wt_fraction=c.wt_fraction,
            is_hap=c.is_hap, is_solid=c.is_solid))
    db.commit()
    return product_out(p, {x.number: x for x in db.query(models.Product).all()})


# ---------- Usage logs ----------

class UsageIn(BaseModel):
    use_date: date
    emission_unit: str
    product_number: str
    quantity: float = Field(gt=0)
    unit: str = "lbs"                  # 'lbs' or 'gal'
    part_type: Optional[str] = None    # defaults from product
    shift: str = "Shift 1"
    shift_hours: float = 8.5
    employee: str = ""
    notes: str = ""


def usage_out(u: models.UsageLog):
    return {"id": u.id, "use_date": u.use_date.isoformat(),
            "emission_unit": u.emission_unit, "product_number": u.product.number,
            "product_name": u.product.name, "coating_type": u.product.coating_type,
            "quantity": u.quantity, "unit": u.unit, "gallons": u.gallons,
            "part_type": u.part_type, "shift": u.shift, "shift_hours": u.shift_hours,
            "employee": u.employee, "notes": u.notes, "voided": u.voided,
            "void_reason": u.void_reason,
            "created_by": u.created_by, "updated_by": u.updated_by}


def _to_gallons(quantity: float, unit: str, product: models.Product) -> float:
    if unit == "gal":
        return quantity
    if unit == "lbs":
        if not product.density:
            raise HTTPException(400, f"Product {product.number} has no density; enter gallons or fix the product record")
        return quantity / product.density
    raise HTTPException(400, "unit must be 'lbs' or 'gal'")


@app.post("/api/usage")
def log_usage(body: UsageIn, request: Request, db: Session = Depends(get_db)):
    if body.emission_unit not in models.EMISSION_UNITS:
        raise HTTPException(400, f"emission_unit must be one of {models.EMISSION_UNITS}")
    p = db.query(models.Product).filter_by(number=body.product_number).first()
    if not p:
        raise HTTPException(404, f"Unknown product {body.product_number}")
    u = models.UsageLog(
        use_date=body.use_date, emission_unit=body.emission_unit, product_id=p.id,
        quantity=body.quantity, unit=body.unit,
        gallons=_to_gallons(body.quantity, body.unit, p),
        part_type=body.part_type or p.default_part_type,
        shift=body.shift, shift_hours=body.shift_hours,
        employee=body.employee, notes=body.notes,
        created_by=current_user(request), updated_by=current_user(request))
    db.add(u); db.commit()
    return usage_out(u)


@app.get("/api/usage")
def list_usage(year: int, month: int, include_voided: bool = False,
               db: Session = Depends(get_db)):
    start, end = calc.month_bounds(year, month)
    q = (db.query(models.UsageLog).options(joinedload(models.UsageLog.product))
         .filter(models.UsageLog.use_date >= start, models.UsageLog.use_date < end))
    if not include_voided:
        q = q.filter(models.UsageLog.voided == False)  # noqa: E712
    return [usage_out(u) for u in q.order_by(models.UsageLog.use_date,
                                             models.UsageLog.id)]


class UsageEdit(BaseModel):
    use_date: Optional[date] = None
    emission_unit: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    part_type: Optional[str] = None
    shift: Optional[str] = None
    shift_hours: Optional[float] = None
    notes: Optional[str] = None


@app.put("/api/usage/{uid}")
def edit_usage(uid: int, body: UsageEdit, request: Request, db: Session = Depends(get_db)):
    u = db.get(models.UsageLog, uid)
    if not u:
        raise HTTPException(404, "Not found")
    data = body.dict(exclude_none=True)
    for k, v in data.items():
        setattr(u, k, v)
    if "quantity" in data or "unit" in data:
        u.gallons = _to_gallons(u.quantity, u.unit, u.product)
    u.updated_by = current_user(request)
    u.updated_at = datetime.utcnow()
    db.commit()
    return usage_out(u)


class VoidIn(BaseModel):
    reason: str


@app.post("/api/usage/{uid}/void")
def void_usage(uid: int, body: VoidIn, request: Request, db: Session = Depends(get_db)):
    u = db.get(models.UsageLog, uid)
    if not u:
        raise HTTPException(404, "Not found")
    u.voided = True
    u.void_reason = body.reason
    u.updated_by = current_user(request)
    db.commit()
    return usage_out(u)


# ---------- Dashboard / reports ----------

@app.get("/api/summary")
def summary(year: int, month: int, db: Session = Depends(get_db)):
    """Month-to-date + rolling status vs permit limits."""
    hap_cols = db.get(models.Setting, "hap_columns").value
    mu_cols = db.get(models.Setting, "mu_hap_columns").value
    series = calc.monthly_series(db, year, month, hap_cols, mu_cols)
    rec = series[-1]
    em, rl, mu = rec["emissions"], rec["rolling"], rec["material_use"]
    fg = (em.get("voc_eu1", 0) + em.get("voc_eu2", 0) + em.get("voc_eu3", 0))
    return {
        "year": year, "month": month, "frozen": rec["frozen"],
        "monthly": {"voc_eu1": em.get("voc_eu1", 0), "voc_eu2": em.get("voc_eu2", 0),
                    "voc_eu3": em.get("voc_eu3", 0), "voc_fg": fg,
                    "dbe_lbs": em.get("dbe_lbs", 0), "eb_lbs": em.get("eb_lbs", 0),
                    "cumene_tons": em.get("cumene_tons", 0),
                    "agghap_tons": em.get("agghap_tons", 0),
                    "gals": mu.get("gals_eu1", 0) + mu.get("gals_eu2", 0) + mu.get("gals_eu3", 0)},
        "rolling": {"voc_eu1": rl["voc_eu1"], "voc_eu2": rl["voc_eu2"],
                    "voc_eu3": rl["voc_eu3"],
                    "voc_fg": rl["voc_eu1"] + rl["voc_eu2"] + rl["voc_eu3"],
                    "dbe_tons": rl["dbe_tons"], "eb_tons": rl["eb_tons"],
                    "cumene_tons": rl["cumene_tons"], "agghap_tons": rl["agghap_tons"]},
        "limits": {"voc_eu1": 40, "voc_eu2": 40, "voc_eu3": 10, "voc_fg": 89.9,
                   "dbe_tons": 2.9, "eb_tons": 2.9, "cumene_tons": 1.4,
                   "agghap_tons": 8.9},
    }


@app.get("/api/report/aggregate")
def aggregate_report(year: int, month: int, db: Session = Depends(get_db)):
    data = reports.generate_workbook(db, year, month)
    fname = f"Aggregate_Emissions_Report_{year}-{month:02d}.xlsx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/meta")
def meta(db: Session = Depends(get_db)):
    return {"emission_units": models.EMISSION_UNITS,
            "coating_types": list(models.COATING_TYPES.values()),
            "part_types": models.PART_TYPES,
            "frozen_through": db.get(models.Setting, "frozen_through").value}


static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
