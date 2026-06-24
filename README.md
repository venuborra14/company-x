OWG — Automated Trade-Finance Liquidity Router (Prototype)
PROTOTYPE — NOT FOR PRODUCTION. All customs, carrier, credit-bureau and tokenized-treasury integrations are mocked/simulated. The system, its risk logic, and every figure shown are subject to further technical, regulatory, and legal modification. Nothing here is financial, legal, or investment advice.

An interactive Streamlit prototype that routes high-yield SME export invoices to a private credit pool managed by V.capitale.
Quick start
pip install -r requirements.txt

streamlit run app.py

Then open the URL Streamlit prints (default http://localhost:8501).
Files
File
Purpose
engine.py
Framework-agnostic core logic (no Streamlit imports): underwriting, treasury, analytics, console log. Unit-testable and reusable behind a real API.
app.py
Streamlit presentation/orchestration layer. Holds session state and renders the four tabs + sidebar.
requirements.txt
Pinned dependencies.

The four engines
Underwriting Engine — Triple-Match. A deal is APPROVED only when all three independent silos pass: digital invoice, origin customs clearance, and carrier logistics (container LADEN on a known vessel). Pricing is charged to the SME exporter at a premium (1.5%/30d, 18% annualized) while default underwriting is anchored on the blue-chip buyer's credit profile.

Treasury Router — JIT funding. A $2,000,000 V.capitale pool split into Tier 1 (idle cash swept into a 4.5% tokenized T-bill vault) and Tier 2 (live advances). On approval, the 80% advance is redeemed from Tier 1 and wired to the exporter atomically; 20% is retained as a protective cushion. A non-utilization fee is charged when monthly volume falls below $573,000.

Capital Portal Dashboard. Portfolio value, blended live yield (18% factoring blended with 4.5% sweep), capital velocity (DSO), and concentration exposures (buyer / geographic / sector) overlaid against configurable caps.

Live Usage Guide. Three stakeholder walkthroughs (Tech/Developer, V.capitale, SME Exporter) that update interactively as you run a mock trade.
Try it
Happy path: invoice uploaded + buyer Walmart Inc. + carrier MAERSK + container MAEU1234567 → all three silos pass → fund the JIT advance.
Failure (logistics): Carrier UNKNOWN → logistics silo fails.
Failure (customs): A container ID whose SHA-256 ends in 7 → simulated customs hold.
Veto: Arm the veto in the sidebar → approved deals are blocked from funding.
⏩ Use Advance 1 day in the sidebar to accrue treasury sweep yield.
Where the real integrations would plug in
Search engine.py for # MOCK to find each integration seam:

blue-chip buyer credit registry → GLEIF / credit bureau
customs gateway → sovereign customs network (e.g. ICEGATE)
carrier telemetry → carrier BL APIs + AIS vessel tracking (Maersk/MSC)
tokenized T-bill vault redemption → institutional money-market / RWA rails

