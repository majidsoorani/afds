# AFDS Open Source + Commercialization Plan (Execution Guide)

This document is the practical next-step plan to make AFDS publicly visible while preserving a paid path for business use.

Important: this is product strategy guidance, not legal advice. Get a software licensing lawyer to review the final license text before publishing.

---

## 1) First Decision: Choose Your Model

You cannot be both "open source" (OSI definition) and "non-commercial use only".

Pick one model:

### Model A: Open Core + Dual License (recommended)
- Public core under AGPLv3.
- Commercial license for companies that do not want AGPL obligations.
- Revenue from hosted cloud, support, enterprise modules, and commercial license.

### Model B: Source-Available + Paid Production Use
- Use BUSL/FSL/PolyForm-style licensing.
- Free for dev/test/evaluation.
- Paid for production or real business use.
- Call it "source-available", not open source.

Recommended default for AFDS: Model A, unless strict production control is your highest priority.

---

## 2) Public Repo Readiness (Must Do Before Launch)

### Security and data hygiene
- Rotate all secrets that may have been used in this repo history.
- Keep `.env` ignored and ensure no real credentials appear in docs/examples.
- Remove or anonymize any production-derived records.
- Move sensitive datasets to a private diligence pack.

### Content and IP hygiene
- Remove customer-identifying references from examples and docs.
- Replace account IDs and names with synthetic fixtures.
- Keep third-party connectors, but do not bundle restricted data dumps.

### Legal hygiene
- Add a real `LICENSE` file.
- Add `COMMERCIAL_LICENSE.md` (for dual-license or source-available terms).
- Add `SECURITY.md`, `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md`.
- Add a `THIRD_PARTY_DATA_NOTICES.md` that lists each data/API dependency and usage constraints.

---

## 3) Product Packaging for Revenue

### Community Edition (free)
- Core scoring engine and rule engine.
- Basic dashboards and local deployment.
- Synthetic/demo datasets only.
- No SLA, no enterprise compliance package.

### Enterprise / Commercial (paid)
- Commercial license (if customer cannot accept AGPL).
- Hosted AFDS Cloud or managed private deployment.
- SLA and support.
- Compliance export packs and model governance workflows.
- Premium integrations and advanced rule packs.
- Audit readiness features and enterprise auth options.

---

## 4) Fundraising Narrative (What Investors Need)

Position AFDS as:

"Transparent, self-hostable fraud/AML infrastructure with real-time decisioning and AI-assisted investigations for regulated fintechs."

Show these proofs:
- 2-5 design partners in a clear ICP (fintech, EMI, PSP, digital bank).
- Paid pilot(s), not only demos.
- Measurable impact: lower false positives, faster investigations, fraud loss reduction.
- Legally clean data rights and deployment story.
- Repeatable go-to-market motion.

Without these proofs, fundraising is harder regardless of license quality.

---

## 5) 30-Day Execution Plan

### Week 1: Cleanup and legal base
- Choose Model A or B.
- Draft license terms and send to counsel.
- Remove production data artifacts from public scope.
- Add legal/compliance markdown files listed above.

### Week 2: Public package and demo
- Create a synthetic demo dataset and scripted demo flow.
- Publish a clean quickstart that runs end-to-end locally.
- Add architecture and boundary docs (community vs commercial).

### Week 3: Commercial offer
- Define 3 paid tiers (Startup, Growth, Enterprise).
- Write a 1-page commercial terms summary.
- Prepare customer-facing deployment options (SaaS / private VPC / on-prem).

### Week 4: Pipeline and validation
- Reach out to 30 target companies.
- Secure 5-8 discovery calls.
- Aim for 2 pilot conversations and 1 signed pilot/LOI.
- Track a single KPI set per pilot: precision, recall proxy, analyst time saved.

---

## 6) Concrete Next Deliverables for This Repo

1. Add `LICENSE` and `COMMERCIAL_LICENSE.md` based on chosen model.
2. Add `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`.
3. Add `THIRD_PARTY_DATA_NOTICES.md` (OpenSanctions, MaxMind, HIBP, third-party vendor, etc.).
4. Add `data-pipeline/scored-output/README.md` describing what must remain private vs public.
5. Replace production-derived examples with synthetic fixtures.

---

## 7) Decision Checklist (Fast)

Answer yes/no:
- Do you want OSI-compliant open source branding?
- Are you comfortable with AGPL obligations for users?
- Is "business must pay for production" a hard requirement?
- Do you have legal budget this month for license review?
- Can you run pilots within 30-45 days?

Interpretation:
- Mostly yes on open-source branding: use Model A (AGPL + commercial license).
- Mostly yes on strict paid production: use Model B (source-available).
