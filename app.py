"""
app.py
======
OWG -- Automated Liquidity Router (interactive prototype).

Run with:
    streamlit run app.py

This file is the presentation/orchestration layer only. All business logic
lives in `engine.py`. The app maintains one PlatformState per browser session
in st.session_state and renders four areas:

    Tab 1  Underwriting Engine   (SME exporter submits a trade -> Triple-Match)
    Tab 2  Treasury Router       (V.capitale two-tier pool + JIT funding)
    Tab 3  Capital Portal        (V.capitale analytics dashboard)
    Tab 4  Live Usage Guide      (Tech / V.capitale / SME walkthroughs)

The sidebar carries V.capitale's risk controls and the manual veto gate.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from engine import (
    PlatformState,
    DealState,
    MatchStatus,
    BLUE_CHIP_BUYERS,
    KNOWN_CARRIERS,
    ADVANCE_RATE,
    CUSHION_RATE,
    ANNUALIZED_PREMIUM,
    TREASURY_BASELINE_YIELD,
    MONTHLY_VOLUME_TARGET,
    SINGLE_BUYER_CAP,
    GEOGRAPHIC_CAP,
    SECTOR_CAP,
)

# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="OWG · Liquidity Router",
    page_icon=":material/account_balance:",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Session state bootstrap
# ---------------------------------------------------------------------------
def get_state() -> PlatformState:
    """Return the per-session PlatformState, creating it on first run."""
    if "platform" not in st.session_state:
        st.session_state.platform = PlatformState()
    if "veto_armed" not in st.session_state:
        st.session_state.veto_armed = False
    if "day_counter" not in st.session_state:
        st.session_state.day_counter = 0
    if "treasury_interest" not in st.session_state:
        st.session_state.treasury_interest = 0.0
    if "guide_stage" not in st.session_state:
        # Tracks how far the SME walkthrough has progressed for the usage guide.
        st.session_state.guide_stage = "registered"
    return st.session_state.platform


state = get_state()


# ---------------------------------------------------------------------------
# Disclaimer banner
# ---------------------------------------------------------------------------
st.warning(
    "**PROTOTYPE — NOT FOR PRODUCTION USE.** This is an initial technical "
    "outline. All customs, carrier, credit-bureau and tokenized-treasury "
    "integrations are **mocked/simulated**. The system, its risk logic, and "
    "all figures shown are subject to further technical, regulatory, and legal "
    "modification. Nothing here constitutes financial, legal, or investment advice."
)

st.title("OWG — Automated Trade-Finance Liquidity Router")
st.caption(
    "Bridging the SME trade-finance gap: high-yield export invoices routed to a "
    "private credit pool managed by **V.capitale**."
)


# ---------------------------------------------------------------------------
# Sidebar — V.capitale controls (risk caps + veto gate + treasury sweep)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("V.capitale Control Panel")

    st.subheader("Risk Thresholds")
    st.caption("Concentration caps as a % of the committed pool.")
    buyer_cap = st.slider("Single-buyer cap", 0.05, 0.50, SINGLE_BUYER_CAP, 0.05,
                          format="%.0f%%", help="Max exposure to any one buyer.")
    geo_cap = st.slider("Geographic cap", 0.10, 0.80, GEOGRAPHIC_CAP, 0.05,
                        format="%.0f%%", help="Max exposure to any one country corridor.")
    sector_cap = st.slider("Sector cap", 0.10, 0.70, SECTOR_CAP, 0.05,
                           format="%.0f%%", help="Max exposure to any one sector.")
    # Multiply by 100 only for display; slider already returns the fraction.
    st.session_state.cap_buyer = buyer_cap
    st.session_state.cap_geo = geo_cap
    st.session_state.cap_sector = sector_cap

    st.divider()

    st.subheader("Manual Veto Gate")
    st.caption("V.capitale holds absolute Go/No-Go power on liquidity release.")
    st.session_state.veto_armed = st.toggle(
        "Arm veto (block next funding)",
        value=st.session_state.veto_armed,
        help="When armed, the next approved deal will NOT be auto-funded even "
             "if it passes Triple-Match.",
    )
    if st.session_state.veto_armed:
        st.error("VETO ARMED — JIT release is paused.")
    else:
        st.success("Veto disarmed — JIT release is live.")

    st.divider()

    st.subheader("Treasury Sweep")
    t = state.treasury
    st.caption("Idle Tier-1 capital earns the tokenized T-bill baseline.")
    st.metric("Idle (Tier 1)", f"${t.tier1_idle:,.0f}")
    st.metric("Deployed (Tier 2)", f"${t.tier2_deployed:,.0f}")
    if st.button("Advance 1 day (accrue sweep yield)", use_container_width=True):
        interest = t.accrued_treasury_interest(days=1.0)
        t.tier1_idle += interest
        st.session_state.treasury_interest += interest
        st.session_state.day_counter += 1
        t.log.emit("info", "TREASURY", "Daily treasury sweep accrued",
                   {"interest": interest, "day": st.session_state.day_counter})
        st.rerun()
    st.metric("Accrued sweep interest",
              f"${st.session_state.treasury_interest:,.2f}",
              help=f"Cumulative over {st.session_state.day_counter} simulated day(s).")

    st.divider()
    if st.button("Reset entire simulation", use_container_width=True):
        for k in ("platform", "veto_armed", "day_counter",
                  "treasury_interest", "guide_stage"):
            st.session_state.pop(k, None)
        st.rerun()


# ---------------------------------------------------------------------------
# Main tabs
# ---------------------------------------------------------------------------
tab_uw, tab_treasury, tab_portal, tab_guide = st.tabs([
    "1 · Underwriting Engine",
    "2 · Treasury Router",
    "3 · Capital Portal",
    "4 · Live Usage Guide",
])


# ===========================================================================
# TAB 1 — UNDERWRITING ENGINE (Triple-Match)
# ===========================================================================
with tab_uw:
    st.subheader("Triple-Match Verification")
    st.write(
        "An SME exporter submits trade details. Capital releases **only** when "
        "three independent data silos all return `PASSED`."
    )

    col_form, col_hint = st.columns([2, 1])

    with col_form:
        invoice_amount = st.number_input(
            "Invoice amount (USD)", min_value=1_000.0, max_value=2_000_000.0,
            value=250_000.0, step=10_000.0,
        )
        buyer_name = st.selectbox(
            "Blue-chip buyer (credit anchor)",
            options=list(BLUE_CHIP_BUYERS.keys()) + ["Unknown Buyer Ltd."],
        )
        carrier = st.selectbox("Ocean carrier", options=list(KNOWN_CARRIERS) + ["UNKNOWN"])
        container_id = st.text_input(
            "Shipping container ID (BIC code)", value="MAEU1234567",
            help="Format: 4 letters + 7 digits, e.g. MAEU1234567. "
                 "Container IDs whose hash ends in '7' simulate a customs hold.",
        )
        tenor_days = st.select_slider("Tenor (days)", options=[30, 45, 60, 90], value=30)
        invoice_uploaded = st.checkbox("Digital invoice uploaded", value=True)

        run = st.button("Run Triple-Match Underwriting", type="primary",
                        use_container_width=True)

    with col_hint:
        st.info(
            "**Try these:**\n\n"
            "PASS: `MAEU1234567` + MAERSK → all pass\n\n"
            "FAIL: Carrier `UNKNOWN` → logistics fails\n\n"
            "FAIL: A container ID hashing to '7' → customs hold\n\n"
            "FAIL: Uncheck invoice → invoice silo fails"
        )

    if run:
        uw = state.underwriter.underwrite(
            invoice_amount=invoice_amount,
            buyer_name=buyer_name,
            container_id=container_id,
            carrier=carrier,
            invoice_uploaded=invoice_uploaded,
            tenor_days=tenor_days,
        )
        state.last_underwriting = uw
        st.session_state.guide_stage = "underwritten"

    uw = state.last_underwriting
    if uw is not None:
        st.divider()
        st.markdown(f"#### Result · Deal `{uw.deal_id}`")

        # Three silo checks shown as columns.
        c1, c2, c3 = st.columns(3)
        for col, chk in zip((c1, c2, c3), uw.checks):
            with col:
                if chk.status == MatchStatus.PASSED:
                    st.success(f"**{chk.name}** · PASSED")
                else:
                    st.error(f"**{chk.name}** · FAILED")
                st.caption(chk.detail)
                st.caption(f"latency: {chk.latency_ms} ms")

        st.divider()

        if uw.triple_matched:
            st.success(f"**{DealState.APPROVED.value}** — eligible for JIT funding.")
        else:
            st.error(f"**{DealState.REJECTED.value}** — one or more silos failed. No capital will release.")

        # Pricing + risk summary.
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Premium fee (exporter)", f"${uw.premium_fee:,.2f}",
                  help=f"{uw.effective_rate:.2%} for a {uw.tenor_days}-day tenor "
                       f"(1.5%/30d · {ANNUALIZED_PREMIUM:.0%} annualized).")
        m2.metric("80% advance", f"${uw.advance_amount:,.2f}")
        m3.metric("20% cushion (retained)", f"${uw.cushion_amount:,.2f}")
        m4.metric("Buyer credit", f"{uw.buyer_rating}",
                  help=f"PD {uw.buyer_pd_bps} bps · {uw.buyer_country} · {uw.buyer_sector}")

        st.info(f"**Risk anchoring:** {uw.risk_note}")

        # Funding action — gated by triple-match AND V.capitale veto.
        if uw.triple_matched:
            already_funded = any(d.deal_id == uw.deal_id for d in state.treasury.funded_deals)
            if already_funded:
                st.success("This deal has already been funded (see Treasury Router / Capital Portal).")
            elif st.session_state.veto_armed:
                st.warning("V.capitale veto is ARMED — funding blocked. Disarm in the sidebar to release.")
            elif st.button("Trigger JIT Advance (Tier 1 \u2192 Exporter)", type="primary"):
                deal = state.treasury.fund_deal(uw)
                if deal is None:
                    st.error("Funding failed — insufficient idle liquidity. See console log.")
                else:
                    st.session_state.guide_stage = "funded"
                    st.success(
                        f"JIT advance of **${deal.advance_amount:,.2f}** wired to exporter. "
                        f"Expected settlement: {deal.expected_settlement():%Y-%m-%d}."
                    )


# ===========================================================================
# TAB 2 — TREASURY ROUTER
# ===========================================================================
with tab_treasury:
    t = state.treasury
    st.subheader("Two-Tier Treasury & Just-in-Time Funding")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total committed pool", f"${t.total_pool:,.0f}")
    c2.metric("Tier 1 · Idle (T-bill vault)", f"${t.tier1_idle:,.0f}",
              help=f"Earning the {TREASURY_BASELINE_YIELD:.1%} tokenized-treasury baseline.")
    c3.metric("Tier 2 · Deployed advances", f"${t.tier2_deployed:,.0f}")
    c4.metric("Cushion reserved (20%)", f"${t.cushion_reserved:,.0f}")

    st.write("**Capital utilization**")
    st.progress(min(t.utilization, 1.0),
                text=f"{t.utilization:.1%} of pool deployed as live advances")

    # Visual split of the pool.
    split_df = pd.DataFrame({
        "Tier": ["Tier 1 · Idle (4.5% sweep)", "Tier 2 · Deployed (18% factoring)"],
        "Amount": [t.tier1_idle, t.tier2_deployed],
    })
    fig = px.pie(split_df, names="Tier", values="Amount", hole=0.55,
                 color_discrete_sequence=["#4C9AFF", "#36B37E"])
    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300,
                      legend=dict(orientation="h", y=-0.1))
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # Floor billing / non-utilization fee.
    st.subheader("Floor Billing — Non-Utilization Fee")
    nuf = t.non_utilization_fee()
    fc1, fc2, fc3 = st.columns(3)
    fc1.metric("Monthly volume target", f"${nuf['target']:,.0f}")
    fc2.metric("Actual volume booked", f"${nuf['actual']:,.0f}")
    fc3.metric("Shortfall", f"${nuf['shortfall']:,.0f}")
    if nuf["triggered"]:
        st.warning(
            f"Volume below target — non-utilization fee of **${nuf['fee']:,.2f}** "
            f"charged to the client to preserve V.capitale's minimum yield floor."
        )
    else:
        st.success("Volume target met — no non-utilization fee due this period.")

    st.divider()

    # Live funded deals + settlement controls.
    st.subheader("Live Funded Deals")
    if not t.funded_deals:
        st.info("No deals funded yet. Approve a trade in the Underwriting Engine, then trigger the JIT advance.")
    else:
        rows = []
        for d in t.funded_deals:
            rows.append({
                "Deal": d.deal_id,
                "Buyer": d.buyer_name,
                "Country": d.buyer_country,
                "Sector": d.buyer_sector,
                "Invoice": d.invoice_amount,
                "Advance (80%)": d.advance_amount,
                "Fee": d.premium_fee,
                "Tenor (d)": d.tenor_days,
                "Status": "SETTLED" if d.settled else "LIVE",
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Invoice": st.column_config.NumberColumn(format="$%.0f"),
                "Advance (80%)": st.column_config.NumberColumn(format="$%.0f"),
                "Fee": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        live_ids = [d.deal_id for d in t.funded_deals if not d.settled]
        if live_ids:
            settle_id = st.selectbox("Settle a deal (buyer remits on maturity)", live_ids)
            if st.button("Mark settled (principal returns to Tier 1)"):
                if t.settle_deal(settle_id):
                    st.success(f"Deal {settle_id} settled. Principal swept back into the idle vault.")
                    st.rerun()


# ===========================================================================
# TAB 3 — CAPITAL PORTAL DASHBOARD (V.capitale view)
# ===========================================================================
with tab_portal:
    a = state.analytics
    t = state.treasury
    st.subheader("V.capitale — Capital Portal Dashboard")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Portfolio value", f"${a.portfolio_value():,.0f}",
              help="Committed pool + accrued premium fees.")
    k2.metric("Blended live yield", f"{a.blended_yield():.2%}",
              help=f"Weighted blend of {ANNUALIZED_PREMIUM:.0%} factoring tranches "
                   f"and {TREASURY_BASELINE_YIELD:.1%} idle treasury sweep.")
    dso = a.days_sales_outstanding()
    k3.metric("Capital velocity (DSO)", f"{dso:.1f} days",
              help="Advance-weighted average tenor across live deals. Lower = faster recycling.")
    k4.metric("Fees accrued", f"${t.fees_accrued:,.2f}")

    st.divider()

    # Concentration metrics across three dimensions, with cap overlays.
    st.subheader("Risk Concentration vs. Caps")
    cap_buyer = st.session_state.get("cap_buyer", SINGLE_BUYER_CAP)
    cap_geo = st.session_state.get("cap_geo", GEOGRAPHIC_CAP)
    cap_sector = st.session_state.get("cap_sector", SECTOR_CAP)

    dims = [
        ("Single-Buyer", "buyer_name", cap_buyer),
        ("Geographic", "buyer_country", cap_geo),
        ("Sector", "buyer_sector", cap_sector),
    ]

    cols = st.columns(3)
    any_data = False
    for col, (label, dim, cap) in zip(cols, dims):
        with col:
            st.markdown(f"**{label} exposure** · cap {cap:.0%}")
            conc = a.concentration(dim)
            if not conc:
                st.caption("No live exposure yet.")
                continue
            any_data = True
            cdf = pd.DataFrame(
                {"Category": list(conc.keys()),
                 "Exposure": [v for v in conc.values()]}
            ).sort_values("Exposure", ascending=True)
            bar = px.bar(cdf, x="Exposure", y="Category", orientation="h",
                         color_discrete_sequence=["#6554C0"])
            bar.add_vline(x=cap, line_dash="dash", line_color="#FF5630")
            bar.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=240,
                              xaxis_tickformat=".0%", xaxis_title=None, yaxis_title=None)
            st.plotly_chart(bar, use_container_width=True)

    # Override caps in the analytics module for breach detection display.
    breaches = []
    for label, dim, cap in dims:
        for k, frac in a.concentration(dim).items():
            if frac > cap:
                breaches.append(f"{label} cap breached: {k} at {frac:.0%} (limit {cap:.0%})")

    if breaches:
        for b in breaches:
            st.error(f"{b}")
    elif any_data:
        st.success("All concentration exposures within configured caps.")

    st.divider()

    # Yield composition mini-table.
    st.subheader("Yield Composition")
    yc = pd.DataFrame({
        "Tranche": ["Factoring (Tier 2)", "Treasury sweep (Tier 1)"],
        "Capital": [t.tier2_deployed, t.tier1_idle],
        "Rate": [ANNUALIZED_PREMIUM, TREASURY_BASELINE_YIELD],
    })
    yc["Annual yield $"] = yc["Capital"] * yc["Rate"]
    st.dataframe(
        yc, use_container_width=True, hide_index=True,
        column_config={
            "Capital": st.column_config.NumberColumn(format="$%.0f"),
            "Rate": st.column_config.NumberColumn(format="%.2f%%"),
            "Annual yield $": st.column_config.NumberColumn(format="$%.0f"),
        },
    )


# ===========================================================================
# TAB 4 — LIVE USAGE GUIDE (interactive, updates as you run a trade)
# ===========================================================================
with tab_guide:
    st.subheader("Live Usage Guide")
    st.caption("These panels update as you run a mock trade through the platform.")

    stage = st.session_state.guide_stage
    stage_rank = {"registered": 0, "underwritten": 1, "funded": 2}.get(stage, 0)

    persona = st.radio(
        "Choose a stakeholder view",
        ["Tech / Developer", "V.capitale (Fund Manager)", "SME Exporter"],
        horizontal=True,
    )

    # ---- Tech / Developer path ------------------------------------------
    if persona.startswith("Tech"):
        st.markdown("##### Developer Path — live console & payloads")
        st.write(
            "Below is the structured event log emitted by the engines as they "
            "run. Each entry is the kind of payload a real adapter would send to "
            "the customs gateway, carrier telemetry stream, or treasury vault."
        )
        log_entries = state.log.tail(60)
        if not log_entries:
            st.info("No events yet. Run a Triple-Match in Tab 1 to generate console activity.")
        else:
            # Render as a console-style block.
            lines = []
            for e in log_entries:
                lines.append(f"[{e['ts']}] {e['level']:<5} {e['source']:<18} {e['message']}")
                if e["payload"]:
                    lines.append(f"            ↳ {e['payload']}")
            st.code("\n".join(lines), language="bash")

            st.markdown("**Verification triggers (event → effect):**")
            st.markdown(
                "- `INVOICE_SILO` parse → must reconcile against PO before proceeding\n"
                "- `CUSTOMS_GATEWAY GET /clearance` → export hold blocks the match\n"
                "- `CARRIER_TELEMETRY subscribe vessel.position` → container must be LADEN\n"
                "- `UNDERWRITER Triple-Match resolved` → sets deal state APPROVED/REJECTED\n"
                "- `TREASURY redeem` → Tier-1 → Tier-2 atomic JIT release"
            )

    # ---- V.capitale path -------------------------------------------------
    elif persona.startswith("V.capitale"):
        st.markdown("##### V.capitale Path — risk control & JIT oversight")
        steps = [
            ("Set risk thresholds",
             "Use the sidebar sliders to set single-buyer, geographic, and sector "
             "caps. The Capital Portal flags any live exposure that breaches them."),
            ("Monitor the treasury sweep",
             "Idle Tier-1 capital sits in the tokenized T-bill vault at "
             f"{TREASURY_BASELINE_YIELD:.1%}. Use 'Advance 1 day' to accrue sweep "
             "yield and watch idle cash grow."),
            ("Exercise the veto gate",
             "Arm the veto toggle in the sidebar to block the next JIT release even "
             "if a deal passes Triple-Match — V.capitale's absolute Go/No-Go power."),
            ("Watch JIT release",
             "On approval + disarmed veto, the 80% advance is redeemed from Tier 1 "
             "and wired to the exporter in one transaction; 20% cushion is retained."),
        ]
        for i, (title, body) in enumerate(steps):
            complete = (i == 0 or stage_rank >= 1)
            if i == 3:
                complete = stage_rank >= 2
            marker = "**[Done]**" if complete else "[Pending]"
            st.markdown(f"{marker} **{i+1}. {title}** — {body}")

        st.divider()
        st.markdown("**Current control state:**")
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Veto", "ARMED" if st.session_state.veto_armed else "disarmed")
        cc2.metric("Buyer cap", f"{st.session_state.get('cap_buyer', SINGLE_BUYER_CAP):.0%}")
        cc3.metric("Idle vault", f"${state.treasury.tier1_idle:,.0f}")

    # ---- SME Exporter path ----------------------------------------------
    else:
        st.markdown("##### SME Exporter Path — deal lifecycle")
        lifecycle = [
            ("Register & connect ERP",
             "Onboard the exporting entity and link the local accounting/ERP "
             "system so invoices and POs stream in automatically.", 0),
            ("Upload invoice + trade details",
             "Submit invoice amount, buyer, carrier, and container ID in the "
             "Underwriting Engine tab.", 1),
            ("Pass the Triple-Match data lock",
             "Invoice + customs clearance + carrier telemetry must all return "
             "PASSED. This is the fraud / double-invoicing lock.", 1),
            ("Receive the JIT advance",
             "On approval (and no veto), 80% of the invoice is wired instantly; "
             "20% is held as a protective cushion.", 2),
            ("Settlement",
             "When the blue-chip buyer pays at maturity, the cushion (minus the "
             "premium fee) is released and the principal recycles to Tier 1.", 2),
        ]
        for i, (title, body, need) in enumerate(lifecycle):
            marker = "**[Done]**" if stage_rank >= need else "[Pending]"
            st.markdown(f"{marker} **{i+1}. {title}** — {body}")

        st.divider()
        if state.last_underwriting and state.last_underwriting.triple_matched:
            uw = state.last_underwriting
            st.success(
                f"Your latest deal `{uw.deal_id}` passed Triple-Match. "
                f"Advance available: **${uw.advance_amount:,.2f}** "
                f"(fee ${uw.premium_fee:,.2f})."
            )
        else:
            st.info("Run a trade in the Underwriting Engine to advance this walkthrough.")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.divider()
st.caption(
    "OWG prototype · Triple-Match underwriting · Two-tier JIT treasury · "
    "Capital portal analytics. All integrations mocked. Figures illustrative only."
)