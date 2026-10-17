# Medical Record Settlement Bill Validation System

## ① Product Overview

This system serves the medical insurance settlement clerk at a hospital. The clerk uploads a discharge summary or front sheet of a medical record; a 5-stage chained NLP pipeline (paragraph segmentation → field extraction → cross-field validation → rule review → LLM semantic check) automatically produces 193 field-level compliance marks with original-text span citations. The clerk clicks Confirm / Reject per row, then one-click exports a 7-sheet settlement bill Excel and writes back the field notes to the case record system via HTTP POST. Each rule hit, LLM call and writeback is logged as JSON Lines with a tamper-evident hash chain, with dead-letter fallback on writeback failure.

Built-in: 193 field specs across 4 sections (31 basic info + 6 outpatient chronic/special disease + 58 inpatient treatment + 98 medical charges), 24 violation rules (regulation articles 38/40 + local inventory + AUDIT smart review), 6-state field and 5-state document state machines, ICD-10/ICD-9-CM-3 code range validation, and an OpenAI-compatible model gateway with structured JSON output. When the model is unreachable, the pipeline falls back to rule-check only — never silently writes data.

Target roles: medical insurance office settlement clerks, medical record room coders, insurance office directors, and IT department integration engineers. Domain: hospital settlement, medical insurance review, EMR front-sheet validation.

## ② Core Capabilities

- **Upload → 193 field marks in ≤ 2 minutes**: 5-stage pipeline with SSE progress; auto-degrade on stage failure.
- **193 fields + 24 violations fully offline**: SQLite + in-memory rule set, loaded at startup.
- **Click "View Evidence" → jump to original text span; 3-second auto-fade highlight**.
- **Confirm / Reject take effect instantly; illegal transitions rejected with 409**.
- **One-click 7-sheet Excel export** (4 sections + coverage stats + source citations + audit ops) with formula columns.
- **OpenAI-compatible model gateway**: 401/timeout/JSON-parse failures → `rule_checked` + `semantic_skipped` + `logs/audit/llm_calls.jsonl` audit; never silently overwrites state.
- **Tamper-evident hash chain** on `logs/audit/<case_id>.jsonl`; break detection via `verify_chain`.
- **Dead-letter directory** `logs/case_system_dead/` for writeback failures after 3× exponential backoff (1s/2s/4s).

## ③ Quick Start

```bash
cd case-bill-validation
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # set MODEL_GATEWAY_API_KEY, CASE_SYSTEM_WRITE_TOKEN, HIS_DISCHARGE_TOKEN

pytest -q
python -m case_bill_validation    # FastAPI on 127.0.0.1:8000
# open http://127.0.0.1:8000/ in browser → drag a discharge file → "Start Validation"
```

Key endpoints: `POST /api/cases` (upload), `GET /api/cases/{case_id}/stream` (SSE), `POST /api/cases/{case_id}/fields/{field}/confirm|reject`, `POST /api/cases/{case_id}/export` (Excel + writeback). See Chinese README "Commands / API / Configuration" for the full table including errors & degradation and existing-system integration.

## ④ Repository Layout

```
case-bill-validation/
├── README.md                   # Chinese product manual (canonical)
├── pyproject.toml              # Python ≥ 3.11 metadata + deps + entry
├── case_bill_validation/       # Main package
│   ├── server.py               # FastAPI app + routes
│   ├── document.py             # BillingCaseDocument / FieldMark / SpanRef
│   ├── pipeline/               # 5-stage pipeline (segmenter, extractor, cross_validator, rule_checker, semantic_checker)
│   ├── rules/                  # 193-field spec + violation list (SQLite + in-memory)
│   ├── llm/gateway.py          # OpenAI-compatible ModelGateway
│   ├── ingest/parser.py        # PDF/Word/text parsing
│   ├── export/                 # Excel exporter + case-record writeback
│   ├── persistence/            # JSON Lines audit + hash chain
│   └── static/                 # Browser panel (HTML/JS/CSS, no framework)
├── tests/                      # pytest + ≥10 desensitized discharge fixtures
├── docs/concepts/              # Structured visual target card
├── docs/screenshots/           # Real runtime screenshots (1440×900 / 1440×1069 PNG)
├── exports/                    # Settlement bill Excel output
└── logs/                       # Audit logs + dead-letter directory
```

---

Full documentation: see [README.md](README.md) (Chinese).

---

## Follow Us

Scan the QR code to follow our official account for updates and community access:

![Follow Us](qrcode.jpg)