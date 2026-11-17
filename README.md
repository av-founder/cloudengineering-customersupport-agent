# CloudServe Automated Triage & Support System

This system triages CloudServe support tickets and drafts grounded replies, implementing `docs/architecture.md` (spec v3.3).
It classifies each ticket, retrieves evidence from the 29 official knowledge-base articles only, routes it with thresholds frozen by offline calibration, and drafts a cited answer.
Before release, four guardrails check the draft for private data, unsupported claims, citation integrity, and the disclaimer and tone.
Every decision is logged in an append-only, SHA-256 hash-chained audit log.

```
INGEST -> CLASSIFY -> ROUTE stage 1 -+-> mandatory intent (security, compliance, billing, feature request, unclear) -> ESCALATE
                                     +-> RETRIEVE (FR-06 title bridge + dense/BM25 RRF + cross-encoder) -> ROUTE stage 2
                                            +-> low confidence / no grounded doc -> ESCALATE (FR-07 brief + attempted docs)
                                            +-> GENERATE (temp 0, sandboxed) -> VALIDATE (4 blocking checks) -> AUTO_REPLY | BLOCK
```

## Quick start (Windows / PowerShell, Python 3.14)

```powershell
py -3.14 -m venv .venv; .\.venv\Scripts\Activate.ps1   # Python 3.14 specifically: every pin was installed and tested on 3.14
python -m pip install -r requirements.txt
Copy-Item .env.example .env        # then add GROQ_API_KEY and/or OPENROUTER_API_KEY and a CUSTOMER_ID_SALT
```

The first run downloads three local models from Hugging Face (about 700 MB): MiniLM embeddings, an MS-MARCO cross-encoder and a DeBERTa NLI model.

To run the evaluation set unattended on a fresh machine (Windows or Linux), follow [`instructions.md`](instructions.md) step by step.

## Single commands

| Purpose | Command |
|---|---|
| Calibrate thresholds (offline, dev data only) | `python -m evaluation.calibrate --dev-tickets data/development_tickets.json --kb-docs data/documentation.json --ground-truth data/ground_truth_responses.json --output-config artifacts/thresholds_v1.json --output-report evaluation/results/threshold_tradeoff_report.md` |
| Start the API | `python -m src.api` (OpenAPI docs at `http://127.0.0.1:8000/docs`, Prometheus metrics at `/metrics`) |
| Unattended evaluation | `python -m evaluation.harness --input <tickets.json> --output <results_dir>` (alias: `python scripts/run_final_evaluation.py ...`) |
| Tests (offline, about 40 s) | `python -m pytest tests/ -v` (add `RUN_INTEGRATION=1` to also run the real local models) |
| FR-09 knowledge-base lock (CI gate) | `python -m scripts.verify_kb_manifest` |
| LLM provider health check (keys, credits, model) | `python -m scripts.check_providers` |
| Live API smoke test (with the API running) | `python -m scripts.smoke_api` |

`artifacts/thresholds_v1.json` is the **only** source of runtime thresholds (tau_class, tau_ret, NLI) and Platt parameters.
The API and the harness refuse to start without it (exit code 2).
The harness writes `predictions.jsonl`, `decision_audit_log.jsonl`, `metrics_report.json` and `metrics_report.md`, and exits with 0 (ok), 1 (an invariant was broken) or 2 (bad input or configuration).
By default it processes one ticket at a time, so the latency figures are true per-ticket pipeline latency. `--concurrency N` is faster, but latency then includes queueing behind the shared local models.

## API (`/api/v1`)

| Endpoint | Purpose |
|---|---|
| `POST /tickets/ingest` | Full triage: priority and queue position, routing decision with a manager-readable reason, draft and doc link (when answered), FR-07 escalation brief (when escalated), governance metadata |
| `POST /tickets/{ticket_id}/feedback` | Agent or technical-writer correction, linked to a specific decision |
| `GET /governance/audit/{ticket_id}` | Latest decision with its sanitised input, plus hash-chain verification |
| `GET /health` | Calibration version, routing mode and LLM provider chain |

## Project layout and mapping to `docs/architecture.md`

| This repo (`Project Structure.md`) | Spec component |
|---|---|
| `src/ingest.py` | C1 INGEST (`src/services/ingest.py`) |
| `src/classify.py` | C2 CLASSIFY (`src/chains/classify.py`) |
| `src/retrieve.py` | C3 RETRIEVE (`src/services/retrieve.py`) + FR-09 manifest lock |
| `src/route.py` | C4 ROUTE (`src/orchestrator/router.py`) + FR-07 / FR-08 |
| `src/generate.py` | C5 GENERATE (`src/chains/generate.py`) + ResponseFormatter (`src/services/formatter.py`) |
| `src/guardrails.py` | C6 VALIDATE (`src/services/validate.py`) + shared PII/secret scrubber |
| `src/graph.py` | LangGraph two-stage state machine (DEP-01) + kill-switch modes |
| `src/logging_store.py` | 4-table schema, append-only triggers, hash chain (NFR-05) |
| `src/llm.py` | Groq -> OpenRouter failover, Tenacity retries, circuit breaker, disk cache |
| `src/config.py`, `src/schemas.py`, `src/api.py` | Settings and policy constants, typed contracts, FastAPI service |
| `prompts/` | Versioned prompt register (`prompts/README.md`) |
| `evaluation/` | `calibrate.py`, `harness.py`, `metrics_calculator.py`, `results/` |
| `tests/features/*.feature` | The spec's 27 Gherkin scenarios, adapted to the real data, run by pytest-bdd |

## Decisions where the data or reality differ from the spec (approved: real data wins)

| Topic | Spec | Implemented |
|---|---|---|
| Intent taxonomy | 15 example intents | The 22 intents in `development_tickets.json` |
| Always-human intents | security, compliance, billing | security, compliance, **billing, feature_request, unclear_request** |
| Tiers / channels | free/standard/pro/enterprise; `web_form` | standard/business/enterprise; `docs_comment` (aliases accepted) |
| Doc links | `doc_link` field | Not in the data, so the link is built as `https://docs.cloudserve.io/kb/<doc_id>` |
| Keyword trapdoor | includes "data residency" | Removed: `data_residency` is an answerable intent in the data |
| Runtime stack | Python 3.11.9, 2024-era pins | Python 3.14.10 with the pinned versions in `requirements.txt` |
| LLMs | Groq `llama-3.3-70b-versatile`, OpenRouter `claude-3.5-haiku` | Both models are retired, so `openai/gpt-oss-120b` runs on **both** providers (same model, so failover does not invalidate the calibration) |
| Database | PostgreSQL 16 | SQLite by default, and the only backend tested. PostgreSQL 16 works through `DATABASE_URL` (asyncpg is pinned) but has not been run against a real server |
| Business targets | FCR >= 65%, escalation <= 30% | Not reachable on this data: the labels allow at most 62.2% FCR, and billing escalation lowers that further. The calibrator enforces the hard constraints and reports the gap |

### Other changes from the spec, each with a reason
- **FR-07 escalation brief**: built from a deterministic Markdown template, not an LLM. It is fast and cannot invent anything.
- **FR-02 / FR-03 / FR-05 / FR-08 / FR-09**: deterministic Python, following DEP-03.
- **Generation context**: the top passages are expanded to their full parent article (at most 2 articles; each is under 1.4K characters), so resolution steps are never cut in half.
- **NLI evidence**: each draft sentence is checked against the 4 most similar *sentences* of the cited article, not whole sections. The DeBERTa NLI model gives faithful paraphrases near-zero entailment when the evidence is a long multi-step section (measured: 0.000 versus 0.92–0.99 at sentence level).
- **Plain typography in drafts**: dashes, quotes and spaces are converted to plain ASCII. The model emits non-breaking hyphens, and these alone dropped the NLI entailment of an identical sentence from 0.98 to 0.07.
- **Performance**:
  - NLI scores each claim's 2 closest evidence sentences first, and the next 2 only for claims not yet supported. This gave the same decisions on all 283 dev drafts with 44% less compute.
  - The cross-encoder scores the top 12 fused candidates plus the matched article's sections (same top-3 hit rate as scoring all of them).
  - All local models are warmed up at start-up.
- **Calibration on noisy labels**: identical dev tickets often carry contradictory labels. Labels are therefore majority-voted per identical text, with ties going to the conservative label.
  - Bounds are applied in order: intent precision, then fluent/non-fluent parity, then the wrong-answer rate. Any bound that no remaining threshold pair can meet is relaxed to "best achievable + 2 percentage points".
  - A parity gap counts as a violation only if it is above 0.05 **and** statistically significant (two-proportion z-test, the test named in the spec's fairness audit). The non-fluent cohort has only 120 tickets.
  - Among the points within 1 percentage point of the best FCR, the most conservative thresholds win, so a few lucky tickets cannot move the thresholds.
  - Grid points with fewer than 10 auto-answers are ignored.
  - Every relaxation is written to the artifact and to `evaluation/results/threshold_tradeoff_report.md`. Per-ticket signals are saved in `calibration_signals.jsonl`.
- **Confidence score**: k = 3 self-consistency draws, each weighted by the model's stated probability. This gives a continuous score for temperature-scaled Platt calibration.
- **`ASSIST_AND_ESCALATE_ONLY` mode**: drafts are still produced but labelled `TIER1_DRAFT_ASSIST`. The spec's "tau_class = 1.01" would otherwise stop all drafts.
- **Extra blocked commitments**: the tone check also blocks the ground truth's three "must not claim" commitments (refund issued, fixed on our side, fix dates).
- **Schema and quarantine**: the database schema is created at start-up with idempotent trigger DDL instead of Alembic. The `is_official_approved = TRUE` CHECK constraint is dropped so the runbook's quarantine SQL can work; quarantined articles are excluded from retrieval.
- **Cross-encoders**: run through sentence-transformers (PyTorch, CPU). Prometheus `/metrics` is provided; Grafana dashboards are out of scope.

## Results on the development set (2026-11-16)

Full reports: `evaluation/results/threshold_tradeoff_report.md`, `evaluation/results/dev_run_2026-11-16/`, and `evaluation/results/latency_sample_2026-11-16/`.
The thresholds were fitted on this same set, so these figures are optimistic; the validation results below are the held-out test.

| Metric | Value |
|---|---|
| Intent accuracy / macro precision / macro recall | 99.2% / 99.4% / 99.3% |
| Confidence calibration error (out-of-fold ECE) | 0.0091 (target <= 0.05) |
| Frozen thresholds | tau_class 0.95, tau_ret 0.52, NLI entailment >= 0.85, contradiction <= 0.25 |
| Answered automatically (FCR) / escalated | 49.4% / 50.6% (the labels cap FCR at 62.2%) |
| Must-not-auto-respond violations / outbound PII leaks | 0 / 0 |
| Decision-log coverage / hash chain | 100% / verified |
| Fluent vs non-fluent FCR | 50.3% vs 46.7% (two-proportion z-test p = 0.49, not significant) |
| Live latency (40 tickets, no cache, one at a time, CPU laptop) | median 2.76 s, p95 5.64 s (the p95 target of 3 s needs GPU/ONNX inference for the local models) |

## Results on the validation set (2026-11-16, single run)

`data/validation_tickets.json` (80 labelled tickets) was run once with the frozen thresholds and no LLM cache. Nothing was tuned on it afterwards.
All 334 LLM calls went to Groq with no failovers; the OpenRouter fallback was configured, but its account had no credits during these runs.
Full report: `evaluation/results/validation_run_2026-11-16/`.

| Metric | Target | Development (500) | Validation (80) |
|---|---|---|---|
| Intent accuracy (macro precision) | precision >= 85% | 99.2% (99.4%) | 100% (100%) |
| Confidence calibration error (ECE) | <= 0.05 | 0.0091 | 0.0032 |
| Answered automatically (FCR) | 65% (baseline 42%) | 49.4% | 40.0% |
| Escalated | 30% (baseline 58%) | 50.6% | 60.0% |
| Share of always-human intents in the set | n/a | 22% | 30% |
| Must-not-auto-respond violations / outbound PII leaks | 0 / 0 | 0 / 0 | 0 / 0 |
| Citation accuracy (answers whose labels name an article) | >= 95% | 93.9% | 95.7% (22/23) |
| Answers on tickets the labels call answerable (proxy for the 5% hallucination limit) | >= 95% | 76.5% | 71.9% (23/32) |
| Decision-log coverage | 100% | 100% | 100% |
| Latency median / p95 (no cache) | p95 <= 3 s | 2.76 s / 5.64 s (40-ticket sample) | 2.60 s / 6.26 s |
| Fluent vs non-fluent FCR | gap <= 0.05 | 50.3% vs 46.7% (p = 0.49) | 36.1% vs 52.6% (p = 0.20) |

How to read these numbers:
- **Not fully independent of dev**: 45 of the 80 tickets repeat a dev ticket word for word, and 62 share a dev ticket's body. All 45 exact repeats got the same decision as on dev. Most of the 18 tickets with new wording are non-fluent rewrites.
- **Lower FCR**: 30% of these tickets are always-human intents, so escalation cannot fall below 30% on this set even if every other ticket were answered. The labels call 25 of the escalated tickets answerable:
  - 9 are billing questions, escalated by policy.
  - 11 had no article above the retrieval threshold. These are vaguely worded, e.g. "where do I start" or "get back to the previous version".
  - 5 were blocked by the fact-check.
- **The 9 answers on tickets labelled "escalate"**:
  - 7 have close dev twins (at least 60% word overlap). In all 7, the dev labels say "answerable" by majority and list the article that was cited.
  - The other 2 are non-fluent rewrites whose closest fluent dev tickets are labelled answerable.
  - On manual reading, 8 of the 9 are correct and on point; one (SSO failing for a single user group) is only partly relevant.
- **Fluent vs non-fluent**: non-fluent customers were answered more often, not less. Most of the gap comes from the ticket mix: 33% of fluent tickets are always-human intents, against 21% of non-fluent ones. With only 19 non-fluent tickets the test can neither confirm nor rule out a 0.05 gap; the 95% interval of the FCR difference (fluent minus non-fluent) runs from -42 to +9 points.
- **Fact-check blocks**: 6 drafts were blocked. On reading:
  - 4 contained a detail the article doesn't state, e.g. "create a new key with full access", so these blocks were correct.
  - 2 were blocked wrongly. The model had merged two adjacent article sentences into one, and checking against one article sentence at a time cannot confirm a merged sentence.

Known limitations, left unfixed so this stays a clean held-out result (each would need re-calibration on dev before use):
- Let the fact-check also compare each sentence with its two best-matching article sentences combined. In every wrong block seen so far (2 in validation, 2 in the live test), the model had merged two adjacent sentences of the same resolution step into one.
- Give the matched article title more weight when retrieving for vaguely worded tickets.

## Live API test (2026-11-16)

`python -m src.api` was started with the real local models, Groq and no LLM cache, and `python -m scripts.smoke_api` passed all 15 checks:
- Health reports calibration `cal_v1_2026_11` and the Groq -> OpenRouter chain.
- An answerable ticket gets an auto-reply with the disclaimer, citation and doc link, and a priority score of at least 85 for an enterprise customer.
- A security ticket goes straight to Tier-2 with no draft.
- A secret API key and an email address are redacted at ingest and never echoed back.
- A prompt injection produces no promise and a safe escalation, and an empty ticket escalates safely.
- Agent feedback is recorded, the audit record is reconstructable with its hash chain verified, and Prometheus metrics are exposed.

An answered ticket took about 3.0 s end to end over HTTP.

## Governance at a glance
- Customer text is scrubbed **before** it is persisted or sent to any model: Presidio plus regexes for API keys, AWS keys, bearer tokens, PEM keys, emails, IPs, cards and phone numbers.
- Customer text is always wrapped in `<untrusted_customer_ticket>`. The generator has no tools and runs at temperature 0.
- VALIDATE is compiled in as the only path to release, and no flag can bypass it. Blocked drafts are stored only in redacted form.
- `decision_audit_log` cannot be modified: database triggers block UPDATE and DELETE (and TRUNCATE on PostgreSQL), and every row is linked to the previous one by a SHA-256 hash.
- Kill switch (`ROUTING_MODE`): `FULL_AUTONOMOUS`, `ASSIST_AND_ESCALATE_ONLY`, or `PASSTHROUGH_QUEUE` (no model is called; FR-08 priority only).
- Quarantine a bad article: `UPDATE kb_articles_registry SET is_official_approved = FALSE WHERE doc_id = :faulting_doc_id;`, then restart.

## Troubleshooting
- `Calibration artifact not found`: run the calibrate command above (it needs a working LLM provider).
- `LLM provider ... disabled for this run (HTTP 401/402/403/404)`: the API key is invalid, its account has no credits (402), access is denied, or the model has been retired. Fix `.env`, then run `python -m scripts.check_providers`. The other provider keeps working, and if neither works, tickets escalate safely.
- OpenRouter `402 ... never purchased credits` after a top-up: the key belongs to a different account or organisation than the one you funded. Create a new key while signed in to the funded account (switch to the organisation first if you use one).
- Groq free tier (8K tokens per minute, 1,000 requests per day) is too small for the 500-ticket calibration. Use a funded OpenRouter key as the fallback, or a paid Groq tier. Successful LLM calls are cached in `storage/llm_cache.sqlite`, so re-runs are free.
