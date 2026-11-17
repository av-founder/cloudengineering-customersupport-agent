"""Metrics.md calculator: Volume, Business, Technical and Governance figures (+ safety and fairness), 100% computed by code."""
import math
from collections import Counter
from typing import Any

import numpy as np
from sklearn.metrics import precision_recall_fscore_support

from src.config import ANSWERED_ACTIONS, GUARDRAIL_CHECKS


# Input:  xs (list[float]); q (float) – percentile
# What:   Percentile rounded to 0.1 (0 for an empty list)
# Output: float
def _pct(xs: list[float], q: float) -> float:
    return round(float(np.percentile(xs, q)), 1) if xs else 0.0


# Input:  num (float); den (float)
# What:   Safe ratio rounded to 4 decimals (None when the denominator is 0)
# Output: float | None
def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


# Input:  x1, n1, x2, n2 (int) – successes and sizes of two cohorts
# What:   Two-sided two-proportion z-test p-value for "the cohorts differ"
# Output: float | None
def _ztest(x1: int, n1: int, x2: int, n2: int) -> float | None:
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return 1.0 if se == 0 else round(math.erfc(abs(x1 / n1 - x2 / n2) / se / math.sqrt(2)), 4)


# Input:  a, b (np.ndarray[bool]) – per-ticket outcomes for two cohorts; n (int) – bootstrap resamples
# What:   95% bootstrap confidence interval for the rate difference a - b (seeded, reproducible)
# Output: list[float] | None
def _boot_ci(a: np.ndarray, b: np.ndarray, n: int = 1000) -> list[float] | None:
    if not len(a) or not len(b):
        return None
    rng = np.random.default_rng(42)
    d = rng.choice(a, (n, len(a))).mean(1) - rng.choice(b, (n, len(b))).mean(1)
    return [round(float(x), 4) for x in np.percentile(d, [2.5, 97.5])]


# Input:  r (dict) – one prediction record (labels optional)
# What:   Per-ticket quality flags used by the safety and fairness sections
# Output: dict[str, bool | None]
def _flags(r: dict[str, Any]) -> dict[str, bool | None]:
    lab, answered = r.get("labels") or {}, r["routing_action"] in ANSWERED_ACTIONS and r["guardrail_passed"]
    exp = set(lab.get("expected_doc_ids") or [])
    hit = bool(exp & set(r.get("retrieved_doc_ids") or [])) if lab.get("answerable_from_docs") and exp else None
    return {"answered": answered, "expected_auto": lab.get("expected_route") == "auto_respond" if lab else None,
            "correct": r["predicted_intent"] == lab["intent"] if lab.get("intent") else None, "hit": hit,
            "good_answer": answered and bool(lab) and lab.get("expected_route") == "auto_respond" and not lab.get("must_not_auto_respond")}


# Input:  records (list[dict]) – one prediction per processed ticket (optional "labels"); decisions_logged (int) – audit rows
#         written by this run; wall (dict) – run timing summary
# What:   Computes every Metrics.md figure directly from the run records (nothing is filled in by hand)
# Output: dict – metrics_report.json content
def compute_metrics(records: list[dict[str, Any]], *, decisions_logged: int, wall: dict[str, Any]) -> dict[str, Any]:
    n, acts = len(records), Counter(r["routing_action"] for r in records)
    f = [_flags(r) for r in records]
    answered = sum(x["answered"] for x in f)
    escalated = sum(v for a, v in acts.items() if a and (a.startswith("ESCALATE_") or a == "BLOCK_SAFETY_GUARDRAIL_FAIL"))
    lat = [float(r["latency_ms"]) for r in records]

    labeled = [(r["labels"]["intent"], r["predicted_intent"]) for r in records if (r.get("labels") or {}).get("intent")]
    cls: dict[str, Any] = {}
    if labeled:
        y, p = zip(*labeled)
        names = sorted(set(y) | set(p))
        pr, rc, f1, sup = precision_recall_fscore_support(y, p, labels=names, zero_division=0)
        cls = {k: {"precision": round(float(a), 4), "recall": round(float(b), 4), "f1_score": round(float(c), 4), "support": int(d)}
               for k, a, b, c, d in zip(names, pr, rc, f1, sup)}
    true_cls = [v for v in cls.values() if v["support"]]
    hits = [x["hit"] for x, r in zip(f, records) if x["hit"] is not None and r.get("retrieval_executed")]
    hits_all = [x["hit"] for x in f if x["hit"] is not None]

    receipts = [r.get("validation_receipt") or {} for r in records]
    by_entity: Counter = Counter()
    for r in records:
        by_entity.update(r.get("pii_redaction_counts") or {})

    lab_recs = [(x, r["labels"]) for x, r in zip(f, records) if r.get("labels")]
    safety = {}
    if lab_recs:
        exp_auto = [x for x, _ in lab_recs if x["expected_auto"]]
        safety = {
            "labeled_tickets": len(lab_recs),
            "must_not_auto_respond_violations": sum(x["answered"] and bool(lb.get("must_not_auto_respond")) for x, lb in lab_recs),
            "expected_route_accuracy": _ratio(sum(x["answered"] == bool(x["expected_auto"]) for x, _ in lab_recs), len(lab_recs)),
            "auto_resolution_precision": _ratio(sum(x["good_answer"] for x, _ in lab_recs), sum(x["answered"] for x, _ in lab_recs)),
            "false_escalation_rate": _ratio(sum(not x["answered"] for x in exp_auto), len(exp_auto)),
            "label_implied_max_fcr": _ratio(len(exp_auto), len(lab_recs)),
        }

    def cohort(c: str) -> dict[str, Any]:
        idx = [i for i, r in enumerate(records) if r.get("language_fluency") == c]
        fc = [f[i] for i in idx]
        ex = [x for x in fc if x["expected_auto"]]
        return {"tickets": len(idx), "fcr": _ratio(sum(x["answered"] for x in fc), len(fc)),
                "intent_accuracy": _ratio(sum(bool(x["correct"]) for x in fc if x["correct"] is not None), sum(x["correct"] is not None for x in fc)),
                "retrieval_hit_rate": _ratio(sum(bool(x["hit"]) for x in fc if x["hit"] is not None), sum(x["hit"] is not None for x in fc)),
                "auto_resolution_precision": _ratio(sum(x["good_answer"] for x in fc), sum(x["answered"] for x in fc)) if any(x["expected_auto"] is not None for x in fc) else None,
                "false_escalation_rate": _ratio(sum(not x["answered"] for x in ex), len(ex)), "_answered": np.array([x["answered"] for x in fc], dtype=float)}
    co = {c: cohort(c) for c in ("fluent", "non_fluent")}
    a, b = co["fluent"].pop("_answered"), co["non_fluent"].pop("_answered")
    deltas = {k: round(abs(co["fluent"][k] - co["non_fluent"][k]), 4) for k in ("fcr", "intent_accuracy", "retrieval_hit_rate", "auto_resolution_precision", "false_escalation_rate")
              if co["fluent"][k] is not None and co["non_fluent"][k] is not None}

    return {
        "volume": {"tickets_processed": n, "answered_automatically": answered, "escalated": escalated, "blocked_by_guardrails": acts.get("BLOCK_SAFETY_GUARDRAIL_FAIL", 0),
                   "routing_action_breakdown": dict(sorted(acts.items()))},
        "business": {"first_contact_resolution_rate": _ratio(answered, n), "escalation_rate": _ratio(escalated, n),
                     "response_time_mean_ms": round(float(np.mean(lat)), 1) if lat else 0.0, "response_time_median_ms": _pct(lat, 50),
                     "response_time_mean_s": round(float(np.mean(lat)) / 1000, 3) if lat else 0.0, "response_time_median_s": round(_pct(lat, 50) / 1000, 3)},
        "technical": {"classification_per_class_metrics": cls, "classification_macro_precision": _ratio(sum(v["precision"] for v in true_cls), len(true_cls)),
                      "classification_macro_recall": _ratio(sum(v["recall"] for v in true_cls), len(true_cls)),
                      "classification_accuracy": _ratio(sum(t == p for t, p in labeled), len(labeled)),
                      "retrieval_hit_rate": _ratio(sum(hits), len(hits)), "retrieval_hit_rate_all_answerable": _ratio(sum(hits_all), len(hits_all)),
                      "latency_median_p50_ms": _pct(lat, 50), "latency_p95_ms": _pct(lat, 95), "wall_clock_run_summary": wall},
        "governance": {"decisions_logged": decisions_logged, "decision_log_coverage_rate": _ratio(decisions_logged, n),
                       "guardrail_activations_by_type": {k: sum(k in rc.get("failed_checks", []) for rc in receipts) for k in GUARDRAIL_CHECKS},
                       "private_data_detections": {"ingest_redactions_total": sum(bool(r.get("pii_redaction_counts")) for r in records),
                                                   "ingest_redactions_by_entity_type": dict(sorted(by_entity.items())),
                                                   "outbound_blocked_detections_total": sum(bool(rc.get("pii_found")) for rc in receipts),
                                                   "outbound_leaked_occurrences": sum(int(r.get("outbound_leak_count") or 0) for r in records)}},
        "safety": safety,
        "fairness": {"cohorts": co, "absolute_deltas": deltas, "max_delta": max(deltas.values(), default=None),
                     "fcr_two_proportion_z_test_p": _ztest(int(a.sum()), len(a), int(b.sum()), len(b)), "fcr_delta_bootstrap_95ci": _boot_ci(a, b)},
        "llm_usage": {"calls": sum(int(r.get("llm_calls") or 0) for r in records), "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in records),
                      "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in records),
                      "pipeline_errors": sum(bool(r.get("error")) for r in records)},
        "invariants": {"processed_equals_answered_plus_escalated": n == answered + escalated, "full_audit_coverage": decisions_logged == n},
    }


# Input:  m (dict) – output of compute_metrics; title (str)
# What:   Renders the metrics as a readable Markdown report
# Output: str – Markdown
def render_markdown(m: dict[str, Any], title: str = "Evaluation Metrics Report") -> str:
    rate_words = ("rate", "precision", "recall", "accuracy", "fcr")
    fmt = lambda k, x: "n/a" if x is None else f"{x:.2%}" if isinstance(x, float) and any(s in k for s in rate_words) else str(x)
    row = lambda k, x: f"| {k} | {fmt(k, x)} |"
    v, b, t, g, fa = m["volume"], m["business"], m["technical"], m["governance"], m["fairness"]
    w, co = t["wall_clock_run_summary"], fa["cohorts"]
    lines = [f"# {title}", "", f"Run: {w.get('start_utc')} -> {w.get('end_utc')} ({w.get('total_seconds')} s)", "",
             "## Volume", "| Metric | Value |", "|---|---|", *(row(k, x) for k, x in v.items() if k != "routing_action_breakdown"),
             "", "| Routing action | Count |", "|---|---|", *(f"| {k} | {x} |" for k, x in v["routing_action_breakdown"].items()),
             "", "## Business", "| Metric | Value |", "|---|---|", *(row(k, x) for k, x in b.items()),
             "", "## Technical", "| Metric | Value |", "|---|---|",
             *(row(k, t[k]) for k in ("classification_accuracy", "classification_macro_precision", "classification_macro_recall",
                                      "retrieval_hit_rate", "retrieval_hit_rate_all_answerable", "latency_median_p50_ms", "latency_p95_ms")),
             "", "| Intent | Precision | Recall | F1 | Support |", "|---|---|---|---|---|",
             *(f"| {k} | {x['precision']:.2f} | {x['recall']:.2f} | {x['f1_score']:.2f} | {x['support']} |" for k, x in t["classification_per_class_metrics"].items()),
             "", f"Wall clock: {w.get('runtime_explanation', '')}",
             "", "## Governance", "| Metric | Value |", "|---|---|", row("decisions_logged", g["decisions_logged"]),
             row("decision_log_coverage_rate", g["decision_log_coverage_rate"]),
             *(f"| guardrail: {k} | {x} |" for k, x in g["guardrail_activations_by_type"].items()),
             *(f"| private data: {k} | {x} |" for k, x in g["private_data_detections"].items()),
             "", "## Safety (vs labels)", "| Metric | Value |", "|---|---|", *(row(k, x) for k, x in m["safety"].items()),
             "", "## Fairness (fluent vs non_fluent)", "| Metric | fluent | non_fluent | abs delta |", "|---|---|---|---|",
             *(f"| {k} | {fmt(k, co['fluent'][k])} | {fmt(k, co['non_fluent'][k])} | {fmt(k, fa['absolute_deltas'].get(k))} |"
               for k in ("tickets", "fcr", "intent_accuracy", "retrieval_hit_rate", "auto_resolution_precision", "false_escalation_rate")),
             "", f"FCR two-proportion z-test p = {fa['fcr_two_proportion_z_test_p']}; bootstrap 95% CI of FCR delta = {fa['fcr_delta_bootstrap_95ci']}",
             "", "## LLM usage", "| Metric | Value |", "|---|---|", *(row(k, x) for k, x in m["llm_usage"].items()),
             "", "## Invariants", *(f"- {k}: **{x}**" for k, x in m["invariants"].items())]
    return "\n".join(lines) + "\n"
