from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    # owner | agronomist | accountant | viewer
    role: Mapped[str] = mapped_column(String(40), default="owner")
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CropYear(Base):
    __tablename__ = "crop_years"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    fields: Mapped[list["Field"]] = relationship(back_populates="crop_year")
    trials: Mapped[list["CropTrial"]] = relationship(back_populates="crop_year")


class Party(Base):
    __tablename__ = "parties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    party_type: Mapped[str] = mapped_column(String(40), default="other")
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    fields: Mapped[list["Field"]] = relationship(back_populates="party")


class Field(Base):
    __tablename__ = "fields"
    __table_args__ = (UniqueConstraint("crop_year_id", "name", name="uq_field_year_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    acres_total: Mapped[float] = mapped_column(Float, default=0.0)
    acres_mine: Mapped[float] = mapped_column(Float, default=0.0)
    crop: Mapped[str] = mapped_column(String(40), default="None")
    ownership_mode: Mapped[str] = mapped_column(String(40), default="operated_by_me")
    party_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parties.id"), nullable=True)
    my_share_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lease_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    rent_per_acre: Mapped[float] = mapped_column(Float, default=0.0)
    expected_yield: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    crop_year: Mapped["CropYear"] = relationship(back_populates="fields")
    party: Mapped[Optional["Party"]] = relationship(back_populates="fields")
    trials: Mapped[list["CropTrial"]] = relationship(back_populates="field")
    shares: Mapped[list["FieldShare"]] = relationship(
        back_populates="field",
        cascade="all, delete-orphan",
        order_by="FieldShare.sort_order",
    )

    @property
    def rent_my_share(self) -> float:
        return round((self.acres_mine or 0) * (self.rent_per_acre or 0), 2)

    @property
    def expected_bushels(self) -> Optional[float]:
        if self.expected_yield is None:
            return None
        return round((self.acres_mine or 0) * self.expected_yield, 1)

    @property
    def share_total_pct(self) -> float:
        return round(sum((s.share_pct or 0) for s in (self.shares or [])), 2)


class FieldShare(Base):
    """Ownership / crop-share partners on a field (supports 2+ partners)."""

    __tablename__ = "field_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    party_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parties.id"), nullable=True)
    partner_name: Mapped[str] = mapped_column(String(160), default="")
    share_pct: Mapped[float] = mapped_column(Float, default=0.0)
    is_me: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    field: Mapped["Field"] = relationship(back_populates="shares")
    party: Mapped[Optional["Party"]] = relationship()

    @property
    def display_name(self) -> str:
        if self.is_me:
            return self.partner_name.strip() or "Me"
        if self.party is not None and self.party.name:
            return self.party.name
        return self.partner_name.strip() or "Partner"


class CropTrial(Base):
    __tablename__ = "crop_trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    crop: Mapped[str] = mapped_column(String(40), default="Corn")
    question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    factor_tested: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    design_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="planned")
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    grain_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    conclusion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    field: Mapped["Field"] = relationship(back_populates="trials")
    crop_year: Mapped["CropYear"] = relationship(back_populates="trials")
    treatments: Mapped[list["TrialTreatment"]] = relationship(
        back_populates="trial", cascade="all, delete-orphan"
    )
    notes: Mapped[list["TrialNote"]] = relationship(
        back_populates="trial", cascade="all, delete-orphan"
    )


class TrialTreatment(Base):
    __tablename__ = "trial_treatments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey("crop_trials.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    is_control: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    trial: Mapped["CropTrial"] = relationship(back_populates="treatments")
    results: Mapped[list["TrialResult"]] = relationship(
        back_populates="treatment", cascade="all, delete-orphan"
    )


class TrialNote(Base):
    __tablename__ = "trial_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey("crop_trials.id"), index=True)
    treatment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("trial_treatments.id"), nullable=True
    )
    note_date: Mapped[date] = mapped_column(Date)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    trial: Mapped["CropTrial"] = relationship(back_populates="notes")


class TrialResult(Base):
    __tablename__ = "trial_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    treatment_id: Mapped[int] = mapped_column(ForeignKey("trial_treatments.id"), index=True)
    rep_label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    harvest_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    yield_bu_ac: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    moisture: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    test_weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    treatment: Mapped["TrialTreatment"] = relationship(back_populates="results")


class Hybrid(Base):
    __tablename__ = "hybrids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40), default="Corn")
    brand: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    maturity: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    traits: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Pricing
    unit_label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # bag | unit | 80k | lb
    cost_per_unit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_per_acre: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class FieldHybrid(Base):
    __tablename__ = "field_hybrids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    hybrid_id: Mapped[int] = mapped_column(ForeignKey("hybrids.id"), index=True)
    rate: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    applied_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


class SprayMix(Base):
    __tablename__ = "spray_mixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    timing: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    crop: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    products_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Rollup of component $/ac (cached when lines saved)
    cost_per_acre: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class SprayMixLine(Base):
    """One product inside a spray / tank mix, with rate and cost."""

    __tablename__ = "spray_mix_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    spray_mix_id: Mapped[int] = mapped_column(ForeignKey("spray_mixes.id"), index=True)
    product_name: Mapped[str] = mapped_column(String(160))
    rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rate_unit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # oz/ac | pt/ac | qt/ac | lb/ac
    unit_label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # gal | qt | lb | oz
    cost_per_unit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_per_acre: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class FieldSprayMix(Base):
    __tablename__ = "field_spray_mixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    spray_mix_id: Mapped[int] = mapped_column(ForeignKey("spray_mixes.id"), index=True)
    timing_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    applied_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


class FieldPlan(Base):
    __tablename__ = "field_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    plan_type: Mapped[str] = mapped_column(String(40))  # planting | fertilizer | spray
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40), default="planned")
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    completed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    estimated_cost_per_acre: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class InputProduct(Base):
    __tablename__ = "input_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    category: Mapped[str] = mapped_column(String(60), default="other")
    unit: Mapped[str] = mapped_column(String(40), default="gal")
    avg_unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    on_hand: Mapped[float] = mapped_column(Float, default=0.0)


class InputPurchase(Base):
    __tablename__ = "input_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("input_products.id"), index=True)
    crop_year_id: Mapped[Optional[int]] = mapped_column(ForeignKey("crop_years.id"), nullable=True, index=True)
    purchase_date: Mapped[date] = mapped_column(Date)
    vendor: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class FieldAssignment(Base):
    __tablename__ = "field_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("input_products.id"), index=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    assign_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class FieldOperation(Base):
    __tablename__ = "field_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    op_date: Mapped[date] = mapped_column(Date)
    op_type: Mapped[str] = mapped_column(String(80))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    billable: Mapped[int] = mapped_column(Integer, default=0)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    party_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parties.id"), nullable=True)
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="unpaid")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total: Mapped[float] = mapped_column(Float, default=0.0)


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    description: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    rate: Mapped[float] = mapped_column(Float, default=0.0)
    field_id: Mapped[Optional[int]] = mapped_column(ForeignKey("fields.id"), nullable=True)


class GrainBin(Base):
    __tablename__ = "grain_bins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    crop: Mapped[str] = mapped_column(String(40), default="Corn")
    capacity_bu: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BinShare(Base):
    __tablename__ = "bin_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bin_id: Mapped[int] = mapped_column(ForeignKey("grain_bins.id"), index=True)
    owner_name: Mapped[str] = mapped_column(String(160))
    bushels: Mapped[float] = mapped_column(Float, default=0.0)


class GrainMovement(Base):
    __tablename__ = "grain_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bin_id: Mapped[Optional[int]] = mapped_column(ForeignKey("grain_bins.id"), nullable=True)
    to_bin_id: Mapped[Optional[int]] = mapped_column(ForeignKey("grain_bins.id"), nullable=True)
    field_id: Mapped[Optional[int]] = mapped_column(ForeignKey("fields.id"), nullable=True)
    contract_id: Mapped[Optional[int]] = mapped_column(ForeignKey("grain_contracts.id"), nullable=True)
    move_date: Mapped[date] = mapped_column(Date)
    move_type: Mapped[str] = mapped_column(String(40))
    # fill | delivery | transfer | adjust
    crop: Mapped[str] = mapped_column(String(40), default="Corn")
    owner_name: Mapped[str] = mapped_column(String(160), default="Me")
    wet_bu: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    moisture: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_bu: Mapped[float] = mapped_column(Float, default=0.0)
    ticket_number: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    destination: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class GrainContract(Base):
    __tablename__ = "grain_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40), default="Corn")
    contract_type: Mapped[str] = mapped_column(String(60), default="cash")
    buyer: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    bushels: Mapped[float] = mapped_column(Float, default=0.0)
    delivered_bu: Mapped[float] = mapped_column(Float, default=0.0)
    futures_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    basis: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cash_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    futures_month: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    delivery_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    delivery_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="open")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ProductionEstimate(Base):
    __tablename__ = "production_estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40))
    expected_bu: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PanoramaConnection(Base):
    __tablename__ = "panorama_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_code: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    leaf_user_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="not_configured")
    # not_configured | pending_auth | active | error | file_only
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sign_in_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PanoramaSyncLog(Base):
    __tablename__ = "panorama_sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ok: Mapped[int] = mapped_column(Integer, default=1)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    filename: Mapped[str] = mapped_column(String(255))
    import_type: Mapped[str] = mapped_column(String(80), default="generic")
    status: Mapped[str] = mapped_column(String(40), default="uploaded")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Guided Inputs wizard state (JSON): type, column map, row proposals, etc.
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    category: Mapped[str] = mapped_column(String(60), default="other")
    # tractor | planter | sprayer | combine | tillage | truck | trailer | other
    make: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="active")
    # active | sold | traded | retired
    finance_status: Mapped[str] = mapped_column(String(40), default="owned")
    # owned | loan | leased
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    purchase_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    salvage_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    useful_life_years: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payment_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payment_frequency: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # monthly | quarterly | annual
    loan_balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lender_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lease_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    annual_insurance: Mapped[float] = mapped_column(Float, default=0.0)
    annual_taxes: Mapped[float] = mapped_column(Float, default=0.0)
    annual_housing: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    events: Mapped[list["EquipmentEvent"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )
    maintenance: Mapped[list["EquipmentMaintenance"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )


class EquipmentEvent(Base):
    """Buy, sell, trade, and other acquisition/disposition events."""

    __tablename__ = "equipment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    # buy | sell | trade_in | trade_out | other
    event_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    counterparty: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    equipment: Mapped["Equipment"] = relationship(back_populates="events")


class EquipmentMaintenance(Base):
    __tablename__ = "equipment_maintenance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    service_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(255))
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    hours_at_service: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    equipment: Mapped["Equipment"] = relationship(back_populates="maintenance")


class BalanceSheetItem(Base):
    """Manual balance-sheet lines (cash, land, loans, etc.). Equipment/grain can auto-feed views."""

    __tablename__ = "balance_sheet_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    side: Mapped[str] = mapped_column(String(20))  # asset | liability
    category: Mapped[str] = mapped_column(String(80))
    # cash | grain | receivables | prepaid | equipment | land | other_asset
    # operating_note | equipment_loan | land_loan | payables | other_liability
    label: Mapped[str] = mapped_column(String(160))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    is_current: Mapped[int] = mapped_column(Integer, default=1)  # current vs long-term
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    # manual | equipment | grain


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm_name: Mapped[str] = mapped_column(String(160), default="Beam Farm")
    active_crop_year_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("crop_years.id"), nullable=True
    )
    # Breakeven / COP defaults ($/ac) for risk screen
    corn_cost_per_ac: Mapped[float] = mapped_column(Float, default=0.0)
    soy_cost_per_ac: Mapped[float] = mapped_column(Float, default=0.0)
    corn_price_assumption: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    soy_price_assumption: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sprayer_tank_gal: Mapped[float] = mapped_column(Float, default=1000.0)
    sprayer_gpa: Mapped[float] = mapped_column(Float, default=15.0)
    # CME / board (stored $/bu; delayed or manual)
    corn_futures: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    soy_futures: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    corn_futures_change: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    soy_futures_change: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quote_as_of: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    quote_source: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    futures_strip_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Nearby board detail: open / change / 52w range (JSON by crop)
    quote_board_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    corn_local_basis: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    soy_local_basis: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Risk stress shocks ($/bu drop)
    corn_stress_shock: Mapped[float] = mapped_column(Float, default=0.50)
    soy_stress_shock: Mapped[float] = mapped_column(Float, default=1.00)
    # Cost of carry
    carry_interest_apr: Mapped[float] = mapped_column(Float, default=7.0)
    corn_storage_per_bu_mo: Mapped[float] = mapped_column(Float, default=0.03)
    soy_storage_per_bu_mo: Mapped[float] = mapped_column(Float, default=0.04)
    corn_shrink_per_bu_mo: Mapped[float] = mapped_column(Float, default=0.003)
    soy_shrink_per_bu_mo: Mapped[float] = mapped_column(Float, default=0.004)
    # futures | cash (futures + local basis)
    carry_mark_mode: Mapped[str] = mapped_column(String(20), default="cash")
    # Show monthly carry $ on grain bins overview
    bins_show_carry: Mapped[int] = mapped_column(Integer, default=0)


class ProductReturn(Base):
    """Return unused product from field / shop back into the on-hand pool."""

    __tablename__ = "product_returns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("input_products.id"), index=True)
    field_id: Mapped[Optional[int]] = mapped_column(ForeignKey("fields.id"), nullable=True)
    return_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # ticket | contract | trial | equipment | receipt | statement | scan | other
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    caption: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class DocumentScan(Base):
    """Phone/receipt capture with OCR draft and guided filing."""

    __tablename__ = "document_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachments.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")  # pending | filed | discarded
    doc_kind: Mapped[str] = mapped_column(String(40), default="receipt")  # receipt | statement | ticket | contract | other
    ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    destination: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    linked_entity_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    linked_entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    party_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parties.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    settlement_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), default="draft")  # draft | final
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_due: Mapped[float] = mapped_column(Float, default=0.0)


class SettlementLine(Base):
    __tablename__ = "settlement_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), index=True)
    field_id: Mapped[Optional[int]] = mapped_column(ForeignKey("fields.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    # positive = owed to party; negative = owed by party / credit


class CropInsurance(Base):
    __tablename__ = "crop_insurance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40))
    policy_type: Mapped[str] = mapped_column(String(80), default="RP")  # RP | YP | other
    coverage_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # e.g. 0.85
    acres: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    premium: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    guarantee_bu: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ContractEvent(Base):
    """Partial pricing, HTA rolls, basis sets, notes on a contract."""

    __tablename__ = "contract_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("grain_contracts.id"), index=True)
    event_date: Mapped[date] = mapped_column(Date)
    event_type: Mapped[str] = mapped_column(String(40))
    # partial_price | hta_roll | basis_set | note | cancel
    bushels: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    futures_month: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MarketingTarget(Base):
    __tablename__ = "marketing_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop_year_id: Mapped[int] = mapped_column(ForeignKey("crop_years.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40))
    target_pct: Mapped[float] = mapped_column(Float, default=0.0)
    by_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    price_floor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TruckingRate(Base):
    __tablename__ = "trucking_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination: Mapped[str] = mapped_column(String(160))
    hauler: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    rate_per_bu: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rate_per_load: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    miles: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TruckLoad(Base):
    __tablename__ = "truck_loads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    load_date: Mapped[date] = mapped_column(Date)
    crop: Mapped[str] = mapped_column(String(40), default="Corn")
    destination: Mapped[str] = mapped_column(String(160))
    hauler: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    bushels: Mapped[float] = mapped_column(Float, default=0.0)
    rate_paid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ticket_number: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SoilTest(Base):
    __tablename__ = "soil_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"), index=True)
    test_date: Mapped[date] = mapped_column(Date)
    lab: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    ph: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    k: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    om: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recommendations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BinConditionNote(Base):
    __tablename__ = "bin_condition_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bin_id: Mapped[int] = mapped_column(ForeignKey("grain_bins.id"), index=True)
    note_date: Mapped[date] = mapped_column(Date)
    moisture: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    as_of_date: Mapped[date] = mapped_column(Date)
    label: Mapped[str] = mapped_column(String(160), default="")
    assets_total: Mapped[float] = mapped_column(Float, default=0.0)
    liabilities_total: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float, default=0.0)
    working_capital: Mapped[float] = mapped_column(Float, default=0.0)
    detail_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EquipmentFuel(Base):
    __tablename__ = "equipment_fuel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    fill_date: Mapped[date] = mapped_column(Date)
    gallons: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    hours_at_fill: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ImportMappingTemplate(Base):
    __tablename__ = "import_mapping_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    import_type: Mapped[str] = mapped_column(String(80), default="generic")
    mapping_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
