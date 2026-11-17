# Prompt Register

Every prompt that runs inside the system lives in `prompts/build/` and is versioned by its front matter.
Every logged decision stores a `prompt_bundle_version` (a SHA-256 fingerprint of all build prompts), so any decision can be traced to the exact prompt text that produced it.

| ID | File | Version | Role | Model call | Temperature | Used by |
|---|---|---|---|---|---|---|
| FR-04 (+FR-08 urgency) | `build/FR-04_triage_classifier.md` + `build/FR-04_fewshot.json` | 1.0.0 | Triage Assistant | LLM, k=3 self-consistency | 0.15 | `src/classify.py` |
| FR-06 | `build/FR-06_semantic_title_matcher.md` | 1.0.0 | Semantic Search Assistant | LLM | 0.0 | `src/retrieve.py` |
| FR-01 (+FR-02, FR-09) | `build/FR-01_grounded_generator.md` | 1.0.0 | Support Assistant | LLM | 0.0 | `src/generate.py` |
| FR-02 / FR-03 / FR-05 | `src/generate.py::format_response` | 1.0.0 | Drafting envelope, citation, disclaimer | Deterministic Python (DEP-03) | n/a | `src/generate.py` |
| FR-07 | `src/route.py::escalation_summary` | 1.0.0 | Escalation Assistant | Deterministic Python template | n/a | `src/route.py` |
| FR-08 | `src/route.py::priority` | 1.0.0 | Routing Assistant | Deterministic Python rules | n/a | `src/route.py` |
| FR-09 | `kb_manifest.lock` + `src/retrieve.py::load_official_docs` | 1.0.0 | Knowledge Filter | Deterministic SHA-256 manifest lock | n/a | `src/retrieve.py`, CI |
| EV-01 | `evaluation/EV-01_answer_quality_judge.md` | 1.0.0 | Answer Quality Judge | LLM (offline only, `--judge`) | 0.0 | `evaluation/calibrate.py` |

## Rules
- Customer text is always wrapped in `<untrusted_customer_ticket>` and knowledge in `<official_kb_context>`.
- The FR-04 few-shot examples are paraphrased by hand rather than copied from `development_tickets.json`, so threshold calibration on the development set does not leak.
- Changing any build prompt changes `prompt_bundle_version`. Re-run `python -m evaluation.calibrate`, because the confidence calibration is specific to the model and prompt.

## Change log
| Date | Prompt | Version | Change |
|---|---|---|---|
| 2026-11-16 | FR-01, FR-04, FR-06, EV-01 | 1.0.0 | Initial release aligned with spec v3.3 and the real data taxonomy (22 intents). |
