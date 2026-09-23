from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceRef(Model):
    file: str
    sheet: str
    cell: str


class Source(Model):
    file: str
    supplier: str
    role: str
    status: Literal["ok", "invalid_data", "source_unavailable"] = "ok"
    rows: int = 0
    message: str = ""


class Month(Model):
    month: date
    quantity: float | None
    source: SourceRef
    opening_stock: float | None = None
    stock_source: SourceRef | None = None


class Sale(Model):
    date: date
    quantity: float
    document: str
    warehouse: str
    source: SourceRef
    customer_id: str | None = None


class Shipment(Model):
    quantity: float = Field(ge=0)
    arrival: date | None
    source: SourceRef


class Item(Model):
    supplier: str
    sku: str
    article: str = ""
    name: str = ""
    unit: str | None = None
    category: str | None = None
    months: list[Month] = Field(default_factory=list)
    sales: list[Sale] = Field(default_factory=list)
    stock: float | None = Field(default=None, ge=0)
    stock_date: date | None = None
    shipments: list[Shipment] = Field(default_factory=list)
    transit_known: bool = False
    seasonality: dict[int, float] = Field(default_factory=dict)
    growth_rate: float | None = Field(default=None, ge=-1)
    minimum: float | None = Field(default=None, gt=0)
    multiple: float | None = Field(default=None, gt=0)
    sources: dict[str, SourceRef] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    invalid: bool = False
    unit_conversion_required: bool = False

    @property
    def key(self) -> str:
        return self.supplier + ":" + self.sku


class Dataset(Model):
    items: list[Item]
    sources: list[Source] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    synthetic: bool = False


class StockInput(Model):
    quantity: float = Field(ge=0)
    as_of: date


class Policy(Model):
    as_of: date
    horizon_days: int = Field(ge=1, le=366)
    lead_days: int = Field(ge=0, le=366)
    suppliers: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    lookback_months: int = Field(default=12, ge=3, le=36)
    outlier_multiplier: float = Field(default=4, ge=2, le=20)
    mad_multiplier: float = Field(default=6, ge=2, le=20)
    # Explicit user inputs. Never derived from an opening-stock snapshot.
    stocks: dict[str, StockInput] = Field(default_factory=dict)
    stockout_days: dict[str, dict[date, int]] = Field(default_factory=dict)
    growth_overrides: dict[str, float] = Field(default_factory=dict)
    category_horizons: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self):
        import calendar
        for months in self.stockout_days.values():
            for month, days in months.items():
                if month.day != 1 or not 0 <= days <= calendar.monthrange(month.year, month.month)[1]:
                    raise ValueError("Stockout days must fit a calendar month")
        if any(not -1 <= x <= 10 for x in self.growth_overrides.values()):
            raise ValueError("Growth override must be between -1 and 10")
        if any(not 1 <= x <= 366 for x in self.category_horizons.values()):
            raise ValueError("Category horizon must be between 1 and 366 days")
        return self


class Recommendation(Model):
    key: str
    supplier: str
    sku: str
    article: str
    name: str
    unit: str | None
    category: str | None
    status: Literal["ok", "needs_data", "invalid_data"]
    quantity: float | None = None
    raw_quantity: float | None = None
    minimum: float | None = None
    multiple: float | None = None
    urgency: str = "не определена"
    explanation: str = ""
    components: dict[str, float | int | str | None] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[SourceRef] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)


class Calculation(Model):
    rows: list[Recommendation]
    issues: list[str]
    synthetic: bool
    policy: Policy
