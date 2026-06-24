"""
engine.py
=========
Core domain logic for the Company X liquidity-routing prototype.

This module is intentionally framework-agnostic (no Streamlit imports) so the
business logic can be unit-tested, reused by an API layer, or swapped behind a
real backend later. The Streamlit layer in `app.py` only orchestrates state and
renders these results.

Four logical engines live here:
    1. UnderwritingEngine  -> Triple-Match verification + risk pricing
    2. TreasuryRouter      -> Two-tier capital pool + JIT funding + non-utilization fee
    3. PortfolioAnalytics  -> DSO, blended yield, concentration metrics
    4. ConsoleLog          -> Structured event log feeding the "Live Usage Guide"

NOTE: All external data sources (customs networks, carrier APIs, tokenized
treasury vaults) are deterministically MOCKED. Search for `# MOCK` to find each
integration seam where a real adapter would be substituted.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Constants / configurable platform parameters
# ---------------------------------------------------------------------------
# These would normally live in a config service. Centralised here so the demo
# is easy to tune.

PREMIUM_RATE_PER_30D = 0.015          # 1.5% factoring fee per 30 days
ANNUALIZED_PREMIUM = 0.18             # 18% headline annualized SME yield
ADVANCE_RATE = 0.80                   # 80% advance, 20% protective cushion
CUSHION_RATE = 1.0 - ADVANCE_RATE     # 20% reserve held back
TREASURY_BASELINE_YIELD = 0.045       # 4.5% idle-capital sweep yield
POOL_TOTAL_DEFAULT = 2_000_000.0      # V.capitale committed capital
MONTHLY_VOLUME_TARGET = 573_000.0     # Floor billing threshold
NON_UTILIZATION_FEE_RATE = 0.005      # 0.5% charged on the volume shortfall

# Single-trade concentration policy (fractions of total committed capital).
SINGLE_BUYER_CAP = 0.25               # No single buyer > 25% of pool
GEOGRAPHIC_CAP = 0.40                 # No single country corridor > 40%
SECTOR_CAP = 0.35                     # No single sector > 35%


# ---------------------------------------------------------------------------
# Mock reference data
# ---------------------------------------------------------------------------
# A small catalogue of "blue-chip" buyers. In production this is replaced by a
# real credit-bureau / GLEIF lookup keyed on the buyer's legal entity ID.

# MOCK: blue-chip buyer credit registry
BLUE_CHIP_BUYERS = {
    "Walmart Inc.":          {"rating": "AA",  "pd_bps": 25,  "country": "USA",        "sector": "Retail"},
    "Tesco PLC":             {"rating": "A+",  "pd_bps": 40,  "country": "UK",         "sector": "Retail"},
    "Carrefour SA":          {"rating": "A",   "pd_bps": 55,  "country": "France",     "sector": "Retail"},
    "Toyota Motor Corp.":    {"rating": "AA-", "pd_bps": 30,  "country": "Japan",      "sector": "Automotive"},
    "Siemens AG":            {"rating": "A+",  "pd_bps": 38,  "country": "Germany",    "sector": "Industrial"},
    "Nestle SA":             {"rating": "AA",  "pd_bps": 22,  "country": "Switzerland","sector": "FMCG"},
    "Unilever PLC":          {"rating": "A+",  "pd_bps": 42,  "country": "UK",         "sector": "FMCG"},
    "Samsung Electronics":   {"rating": "AA-", "pd_bps": 33,  "country": "South Korea","sector": "Electronics"},
}

# MOCK: recognised ocean carriers for the logistics data hook
KNOWN_CARRIERS = ("MAERSK", "MSC", "CMA", "HAPAG", "COSCO", "ONE", "EVERGREEN")


# ---------------------------------------------------------------------------
# Enumerations & value objects
# ---------------------------------------------------------------------------

class MatchStatus(str, Enum):
    """Outcome of a single data-silo check inside the Triple-Match."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    PENDING = "PENDING"


class DealState(str, Enum):
    """Lifecycle state of a trade as it moves through the platform."""
    DRAFT = "DRAFT"
    UNDERWRITING = "UNDERWRITING"
    APPROVED = "TRIPLE-MATCHED & APPROVED"
    VETOED = "VETOED BY V.CAPITALE"
    FUNDED = "FUNDED (JIT ADVANCE RELEASED)"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"


@dataclass
class SiloCheck:
    """Result of verifying one of the three independent data silos."""
    name: str
    status: MatchStatus
    detail: str
    latency_ms: int


@dataclass
class UnderwritingResult:
    """Aggregate output of the Triple-Match underwriting engine."""
    deal_id: str
    invoice_amount: float
    buyer_name: str
    container_id: str
    tenor_days: int
    checks: list[SiloCheck]
    triple_matched: bool
    # Risk + pricing
    buyer_rating: str
    buyer_pd_bps: int
    buyer_country: str
    buyer_sector: str
    premium_fee: float          # absolute fee charged to exporter
    effective_rate: float       # fee / invoice for this tenor
    annualized_rate: float
    advance_amount: float       # 80% of invoice
    cushion_amount: float       # 20% retained
    risk_note: str

    @property
    def state(self) -> DealState:
        return DealState.APPROVED if self.triple_matched else DealState.REJECTED


# ---------------------------------------------------------------------------
# 4. Structured console log (feeds the Tech/Developer usage panel)
# ---------------------------------------------------------------------------

class ConsoleLog:
    """
    Append-only structured event log. The developer-facing tab renders these
    entries verbatim so a viewer can watch the data payloads and verification
    triggers fire behind the UI.
    """

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def emit(self, level: str, source: str, message: str, payload: Optional[dict] = None) -> None:
        self.entries.append({
            "ts": datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
            "level": level.upper(),
            "source": source,
            "message": message,
            "payload": payload or {},
        })

    def tail(self, n: int = 40) -> list[dict]:
        return self.entries[-n:]

    def clear(self) -> None:
        self.entries.clear()


# ---------------------------------------------------------------------------
# 1. Underwriting engine (Triple-Match)
# ---------------------------------------------------------------------------

class UnderwritingEngine:
    """
    Programmatic Triple-Match underwriting.

    A deal is APPROVED only when all three independent silos return PASSED:
        (a) Digital invoice present and parsed
        (b) Origin customs export clearance confirmed
        (c) Carrier logistics confirm container loaded on a known vessel

    Pricing is charged to the capital-starved SME exporter at a premium, while
    the *default* underwriting is anchored on the blue-chip buyer's credit.
    """

    def __init__(self, log: ConsoleLog) -> None:
        self.log = log

    # -- individual silo checks -------------------------------------------

    def _check_invoice(self, invoice_uploaded: bool, invoice_amount: float) -> SiloCheck:
        # MOCK: a real adapter parses the PDF/UBL invoice and validates totals.
        ok = invoice_uploaded and invoice_amount > 0
        self.log.emit(
            "debug", "INVOICE_SILO",
            "Parsing digital invoice payload",
            {"uploaded": invoice_uploaded, "amount": invoice_amount},
        )
        return SiloCheck(
            name="Digital Invoice",
            status=MatchStatus.PASSED if ok else MatchStatus.FAILED,
            detail="Invoice parsed; line items + totals reconciled against PO."
                   if ok else "No invoice document detected in the deal package.",
            latency_ms=42,
        )

    def _check_customs(self, container_id: str, buyer_country: str) -> SiloCheck:
        """
        Simulate an origin-customs export-clearance lookup (e.g. ICEGATE).
        We deterministically derive a clearance flag from the container ID hash
        so the demo is reproducible: container IDs whose hash is even == cleared.
        """
        # MOCK: real implementation calls the sovereign customs gateway.
        cid = (container_id or "").strip().upper()
        digest = int(hashlib.sha256(cid.encode()).hexdigest(), 16) if cid else 1
        cleared = bool(cid) and (digest % 10 != 7)   # ~90% clear, deterministic
        self.log.emit(
            "info", "CUSTOMS_GATEWAY",
            "GET /shipping-bill/clearance",
            {"container_id": cid, "destination": buyer_country, "cleared": cleared},
        )
        return SiloCheck(
            name="Origin Customs Clearance",
            status=MatchStatus.PASSED if cleared else MatchStatus.FAILED,
            detail=f"Shipping bill cleared for export to {buyer_country}."
                   if cleared else "Export control hold: no valid shipping bill on file.",
            latency_ms=310,
        )

    def _check_logistics(self, container_id: str, carrier: str) -> SiloCheck:
        """
        Simulate a live carrier/satellite hook confirming the container is
        physically loaded on a tracked vessel.
        """
        # MOCK: real implementation streams carrier BL + AIS vessel telemetry.
        cid = (container_id or "").strip().upper()
        carrier_norm = (carrier or "").strip().upper()
        carrier_ok = any(carrier_norm.startswith(c) for c in KNOWN_CARRIERS)
        # Container must look like a real BIC code: 4 letters + 7 digits (loose check).
        shape_ok = len(cid) >= 10 and cid[:4].isalpha() and cid[4:].isdigit()
        loaded = carrier_ok and shape_ok
        vessel = f"{carrier_norm or 'UNKNOWN'} EXPRESS" if loaded else "—"
        self.log.emit(
            "info", "CARRIER_TELEMETRY",
            "WS subscribe vessel.position",
            {"container_id": cid, "carrier": carrier_norm,
             "vessel": vessel, "status": "LADEN" if loaded else "NOT_FOUND"},
        )
        return SiloCheck(
            name="Carrier Logistics",
            status=MatchStatus.PASSED if loaded else MatchStatus.FAILED,
            detail=f"Container LADEN on {vessel}; AIS ping confirmed at sea."
                   if loaded else "Container not located on any tracked vessel manifest.",
            latency_ms=520,
        )

    # -- pricing + risk ---------------------------------------------------

    @staticmethod
    def _resolve_buyer(buyer_name: str) -> dict:
        # MOCK: blue-chip lookup. Unknown buyers get a conservative profile.
        return BLUE_CHIP_BUYERS.get(
            buyer_name,
            {"rating": "BBB", "pd_bps": 180, "country": "Unknown", "sector": "General"},
        )

    def underwrite(
        self,
        invoice_amount: float,
        buyer_name: str,
        container_id: str,
        carrier: str,
        invoice_uploaded: bool,
        tenor_days: int = 30,
    ) -> UnderwritingResult:
        """Run the full Triple-Match and produce a priced underwriting result."""
        deal_id = f"CX-{uuid.uuid4().hex[:8].upper()}"
        self.log.emit("info", "UNDERWRITER", "Opening underwriting session", {"deal_id": deal_id})

        buyer = self._resolve_buyer(buyer_name)

        # Run the three independent silo checks.
        checks = [
            self._check_invoice(invoice_uploaded, invoice_amount),
            self._check_customs(container_id, buyer["country"]),
            self._check_logistics(container_id, carrier),
        ]
        triple_matched = all(c.status == MatchStatus.PASSED for c in checks)

        # Pricing charged to the exporter (premium), scaled by tenor.
        periods = max(tenor_days / 30.0, 0.0)
        effective_rate = PREMIUM_RATE_PER_30D * periods
        premium_fee = round(invoice_amount * effective_rate, 2)
        advance_amount = round(invoice_amount * ADVANCE_RATE, 2)
        cushion_amount = round(invoice_amount * CUSHION_RATE, 2)

        # Default underwriting is anchored on the BUYER, not the SME.
        risk_note = (
            f"Default risk underwritten on buyer '{buyer_name}' "
            f"(rating {buyer['rating']}, PD {buyer['pd_bps']}bps). "
            f"SME exporter charged premium spread but is not the credit anchor."
        )

        self.log.emit(
            "info", "UNDERWRITER",
            "Triple-Match resolved",
            {"deal_id": deal_id, "triple_matched": triple_matched,
             "checks": {c.name: c.status.value for c in checks}},
        )

        return UnderwritingResult(
            deal_id=deal_id,
            invoice_amount=round(invoice_amount, 2),
            buyer_name=buyer_name,
            container_id=container_id,
            tenor_days=tenor_days,
            checks=checks,
            triple_matched=triple_matched,
            buyer_rating=buyer["rating"],
            buyer_pd_bps=buyer["pd_bps"],
            buyer_country=buyer["country"],
            buyer_sector=buyer["sector"],
            premium_fee=premium_fee,
            effective_rate=effective_rate,
            annualized_rate=ANNUALIZED_PREMIUM,
            advance_amount=advance_amount,
            cushion_amount=cushion_amount,
            risk_note=risk_note,
        )


# ---------------------------------------------------------------------------
# 2. Treasury router (Just-in-Time funding)
# ---------------------------------------------------------------------------

@dataclass
class FundedDeal:
    """A deal that has received a JIT advance; the unit of the live portfolio."""
    deal_id: str
    buyer_name: str
    buyer_country: str
    buyer_sector: str
    invoice_amount: float
    advance_amount: float
    cushion_amount: float
    premium_fee: float
    tenor_days: int
    funded_at: datetime
    settled: bool = False

    def expected_settlement(self) -> datetime:
        return self.funded_at + timedelta(days=self.tenor_days)


class TreasuryRouter:
    """
    Two-tier capital management for V.capitale's committed pool.

        Tier 1 (Idle):   un-deployed cash swept into a tokenized US Treasury
                         vault earning a 4.5% baseline.
        Tier 2 (JIT):    on APPROVED, redeem from Tier 1 and wire the 80%
                         advance to the exporter in the same transaction.

    Also computes the monthly non-utilization (floor billing) fee.
    """

    def __init__(self, log: ConsoleLog, total_pool: float = POOL_TOTAL_DEFAULT) -> None:
        self.log = log
        self.total_pool = total_pool
        # Everything starts idle and swept into the treasury vault.
        self.tier1_idle = total_pool          # cash in tokenized T-bill vault
        self.tier2_deployed = 0.0             # cash out as live advances
        self.cushion_reserved = 0.0           # 20% cushions retained per deal
        self.fees_accrued = 0.0               # premium fees booked to the fund
        self.funded_deals: list[FundedDeal] = []
        self.month_volume = 0.0               # rolling invoice volume this period

    # -- properties -------------------------------------------------------

    @property
    def available_liquidity(self) -> float:
        """Cash that can still be deployed (idle tier minus reserved cushion)."""
        return max(self.tier1_idle, 0.0)

    @property
    def utilization(self) -> float:
        """Fraction of the pool currently deployed as live advances."""
        return self.tier2_deployed / self.total_pool if self.total_pool else 0.0

    # -- treasury operations ----------------------------------------------

    def accrued_treasury_interest(self, days: float = 1.0) -> float:
        """
        Simulated daily interest on the idle Tier-1 balance.
        Returned, not auto-applied, so the UI controls when to 'tick' time.
        """
        daily = self.tier1_idle * (TREASURY_BASELINE_YIELD / 365.0) * days
        return round(daily, 2)

    def can_fund(self, advance_amount: float) -> bool:
        return advance_amount <= self.available_liquidity

    def fund_deal(self, uw: UnderwritingResult) -> Optional[FundedDeal]:
        """
        Execute the JIT release: redeem from Tier 1, deploy as a Tier 2 advance.
        Returns the FundedDeal on success, or None if blocked.
        """
        if not uw.triple_matched:
            self.log.emit("warn", "TREASURY", "Funding refused: deal not triple-matched",
                          {"deal_id": uw.deal_id})
            return None

        if not self.can_fund(uw.advance_amount):
            self.log.emit("error", "TREASURY", "Insufficient idle liquidity for JIT release",
                          {"deal_id": uw.deal_id, "need": uw.advance_amount,
                           "available": self.available_liquidity})
            return None

        # MOCK: redeem tokenized T-bills, then auto-wire advance to exporter.
        self.log.emit("info", "TREASURY", "Redeeming tokenized T-bills (Tier 1 -> Tier 2)",
                      {"deal_id": uw.deal_id, "redeem": uw.advance_amount})
        self.tier1_idle -= uw.advance_amount
        self.tier2_deployed += uw.advance_amount
        self.cushion_reserved += uw.cushion_amount
        self.fees_accrued += uw.premium_fee
        self.month_volume += uw.invoice_amount

        deal = FundedDeal(
            deal_id=uw.deal_id,
            buyer_name=uw.buyer_name,
            buyer_country=uw.buyer_country,
            buyer_sector=uw.buyer_sector,
            invoice_amount=uw.invoice_amount,
            advance_amount=uw.advance_amount,
            cushion_amount=uw.cushion_amount,
            premium_fee=uw.premium_fee,
            tenor_days=uw.tenor_days,
            funded_at=datetime.utcnow(),
        )
        self.funded_deals.append(deal)
        self.log.emit("info", "TREASURY", "JIT advance wired to exporter",
                      {"deal_id": uw.deal_id, "advance": uw.advance_amount,
                       "utilization": round(self.utilization, 4)})
        return deal

    def settle_deal(self, deal_id: str) -> bool:
        """
        Buyer pays on maturity: return the advance + cushion to Tier 1, keep fee.
        """
        for d in self.funded_deals:
            if d.deal_id == deal_id and not d.settled:
                # MOCK: buyer remittance received into escrow, swept back to vault.
                self.tier2_deployed -= d.advance_amount
                self.cushion_reserved -= d.cushion_amount
                self.tier1_idle += d.advance_amount  # principal returns to idle
                d.settled = True
                self.log.emit("info", "TREASURY", "Deal settled; principal swept back to Tier 1",
                              {"deal_id": deal_id})
                return True
        return False

    # -- floor billing ----------------------------------------------------

    def non_utilization_fee(self) -> dict:
        """
        Charge a non-utilization fee on the shortfall if monthly invoice volume
        falls below the contractual target, preserving V.capitale's yield floor.
        """
        shortfall = max(MONTHLY_VOLUME_TARGET - self.month_volume, 0.0)
        fee = round(shortfall * NON_UTILIZATION_FEE_RATE, 2)
        self.log.emit("debug", "FLOOR_BILLING", "Evaluating non-utilization fee",
                      {"target": MONTHLY_VOLUME_TARGET, "actual": self.month_volume,
                       "shortfall": shortfall, "fee": fee})
        return {
            "target": MONTHLY_VOLUME_TARGET,
            "actual": self.month_volume,
            "shortfall": shortfall,
            "fee": fee,
            "triggered": shortfall > 0,
        }


# ---------------------------------------------------------------------------
# 3. Portfolio analytics
# ---------------------------------------------------------------------------

class PortfolioAnalytics:
    """
    Derives the metrics shown on the V.capitale capital-portal dashboard:
    blended yield, DSO/capital velocity, and concentration exposures.
    """

    def __init__(self, treasury: TreasuryRouter) -> None:
        self.t = treasury

    def portfolio_value(self) -> float:
        """Total committed pool plus fees earned (simple book value)."""
        return self.t.total_pool + self.t.fees_accrued

    def blended_yield(self) -> float:
        """
        Weighted blend of the 18% factoring tranche (deployed capital) and the
        4.5% treasury sweep (idle capital).
        """
        deployed = self.t.tier2_deployed
        idle = self.t.tier1_idle
        base = deployed + idle
        if base <= 0:
            return TREASURY_BASELINE_YIELD
        return (deployed * ANNUALIZED_PREMIUM + idle * TREASURY_BASELINE_YIELD) / base

    def days_sales_outstanding(self) -> float:
        """
        Capital velocity proxy: weighted-average tenor across live (unsettled)
        deals. Lower DSO == faster capital recycling.
        """
        live = [d for d in self.t.funded_deals if not d.settled]
        if not live:
            return 0.0
        weighted = sum(d.advance_amount * d.tenor_days for d in live)
        total = sum(d.advance_amount for d in live)
        return round(weighted / total, 1) if total else 0.0

    def concentration(self, dimension: str) -> dict:
        """
        Exposure (as fraction of total pool) per category along a dimension:
        'buyer_name', 'buyer_country', or 'buyer_sector'.
        """
        live = [d for d in self.t.funded_deals if not d.settled]
        buckets: dict[str, float] = {}
        for d in live:
            key = getattr(d, dimension)
            buckets[key] = buckets.get(key, 0.0) + d.advance_amount
        return {k: v / self.t.total_pool for k, v in buckets.items()}

    def breached_caps(self) -> list[str]:
        """Return human-readable cap-breach warnings, if any."""
        warnings: list[str] = []
        for buyer, frac in self.concentration("buyer_name").items():
            if frac > SINGLE_BUYER_CAP:
                warnings.append(f"Single-buyer cap breached: {buyer} at {frac:.0%} (limit {SINGLE_BUYER_CAP:.0%})")
        for country, frac in self.concentration("buyer_country").items():
            if frac > GEOGRAPHIC_CAP:
                warnings.append(f"Geographic cap breached: {country} at {frac:.0%} (limit {GEOGRAPHIC_CAP:.0%})")
        for sector, frac in self.concentration("buyer_sector").items():
            if frac > SECTOR_CAP:
                warnings.append(f"Sector cap breached: {sector} at {frac:.0%} (limit {SECTOR_CAP:.0%})")
        return warnings


# ---------------------------------------------------------------------------
# Convenience: a single container the Streamlit layer keeps in session_state
# ---------------------------------------------------------------------------

@dataclass
class PlatformState:
    """Holds the live, mutable platform objects for one user session."""
    log: ConsoleLog = field(default_factory=ConsoleLog)
    underwriter: UnderwritingEngine = field(init=False)
    treasury: TreasuryRouter = field(init=False)
    analytics: PortfolioAnalytics = field(init=False)
    last_underwriting: Optional[UnderwritingResult] = None

    def __post_init__(self) -> None:
        self.underwriter = UnderwritingEngine(self.log)
        self.treasury = TreasuryRouter(self.log)
        self.analytics = PortfolioAnalytics(self.treasury)
