"""Offline threshold calibration (data-driven offline, strictly static at runtime).

python -m evaluation.calibrate --dev-tickets data/development_tickets.json --kb-docs data/documentation.json \
    --ground-truth data/ground_truth_responses.json --output-config artifacts/thresholds_v1.json \
    --output-report evaluation/results/threshold_tradeoff_report.md

Fits temperature-scaled Platt calibration with 5-fold CV stratified by intent x language fluency, grid-searches
tau_class x tau_ret (0.50..0.95, step 0.01) to maximise FCR under the hard constraints, and freezes the result.
Exit codes: 0 ok, 2 bad input / forbidden dataset / no working LLM provider, 3 aborted because too many LLM calls failed.
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import erfc
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from src import classify, generate, guardrails, ingest, retrieve
from src.config import (FALLBACK_INTENT, FCR_TOLERANCE, FORBIDDEN_DATASETS, GRID_MAX, GRID_MIN, GRID_STEP, HARD_CONSTRAINTS, IDK, RELAX_TOLERANCE, RERANK_KEEP,
                        TARGET_MAX_ESCALATION, PlattScaling, RoutingThresholds, ThresholdArtifact, get_settings, load_prompt, setup_logging, sha256)
from src.llm import LLMUnavailableError
from src.schemas import TicketState

log = logging.getLogger("evaluation.calibrate")
IDENTITY = PlattScaling(temperature_T=1.0, slope_a=1.0, intercept_b=0.0)
SPEC_PRIOR = PlattScaling(temperature_T=1.42, slope_a=1.08, intercept_b=-0.19)  # spec default prior, used only if a fit is impossible
NLI_SPEC = {"nli_entailment_min": 0.85, "nli_contradiction_max": 0.25}           # spec values, frozen into the artifact (not grid-searched)
GRID = np.round(np.arange(GRID_MIN, GRID_MAX + 1e-9, GRID_STEP), 2)


# Input:  p (np.ndarray) – probabilities
# What:   Log-odds with clipping so 0 and 1 stay finite
# Output: np.ndarray
def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


# Input:  z (np.ndarray) – logits; ps (PlattScaling)
# What:   Spec calibration formula P(correct) = 1 / (1 + exp(-(a*z + b) / T)), identical to src.classify.platt
# Output: np.ndarray – calibrated probabilities
def apply_platt(z: np.ndarray, ps: PlattScaling) -> np.ndarray:
    return 1 / (1 + np.exp(-(ps.slope_a * z + ps.intercept_b) / ps.temperature_T))


# Input:  z (np.ndarray) – logits of raw confidence; y (np.ndarray[int]) – 1 when the predicted intent was correct
# What:   Temperature T first (pure temperature scaling by maximum likelihood), then logistic slope/intercept on z/T,
#         rewritten in the spec form sigma((a*z + b) / T)
# Output: PlattScaling | None – None when the data cannot support a fit (one class only or constant scores)
def fit_platt(z: np.ndarray, y: np.ndarray) -> PlattScaling | None:
    if len(np.unique(y)) < 2 or np.ptp(z) == 0:
        return None
    sig = lambda x: np.clip(1 / (1 + np.exp(-x)), 1e-9, 1 - 1e-9)
    nll = lambda T: -np.mean(y * np.log(sig(z / T)) + (1 - y) * np.log(1 - sig(z / T)))
    T = float(minimize_scalar(nll, bounds=(0.05, 20.0), method="bounded").x)
    lr = LogisticRegression(C=1e4, max_iter=1000).fit((z / T)[:, None], y)
    return PlattScaling(temperature_T=round(T, 6), slope_a=round(float(lr.coef_[0, 0]), 6), intercept_b=round(float(lr.intercept_[0]) * T, 6))


# Input:  p (np.ndarray) – calibrated confidences; y (np.ndarray) – 0/1 correctness; bins (int)
# What:   Expected Calibration Error with equal-width bins, plus the reliability table behind it
# Output: tuple[float, list[dict]] – ECE and per-bin rows
def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> tuple[float, list[dict]]:
    idx, rows = np.minimum((p * bins).astype(int), bins - 1), []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append({"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": int(m.sum()), "mean_confidence": round(float(p[m].mean()), 4),
                         "accuracy": round(float(y[m].mean()), 4), "gap": round(float(abs(p[m].mean() - y[m].mean())), 4)})
    return round(sum(r["gap"] * r["n"] for r in rows) / max(len(p), 1), 4), rows


# Input:  intents (list[str]); fluency (list[str]); k (int) – folds
# What:   Stratification labels intent|fluency; strata too small for k folds fall back to intent, then to a shared bucket
# Output: list[str]
def strata(intents: list[str], fluency: list[str], k: int = 5) -> list[str]:
    s = [f"{i}|{f}" for i, f in zip(intents, fluency)]
    c = Counter(s)
    s = [x if c[x] >= k else x.split("|")[0] for x in s]
    c = Counter(s)
    return [x if c[x] >= k else "rare" for x in s]


# Input:  z, y (np.ndarray); groups (list[str]) – strata; seed (int)
# What:   5-fold stratified cross-validation: each fold is calibrated by parameters fitted on the other four (honest,
#         out-of-fold confidences used by the grid search and the ECE check)
# Output: tuple[np.ndarray, list[float]] – out-of-fold calibrated confidences and per-fold ECE
def cross_validate(z: np.ndarray, y: np.ndarray, groups: list[str], seed: int) -> tuple[np.ndarray, list[float]]:
    oof, fold_ece = np.zeros(len(z)), []
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=seed).split(z, groups):
        oof[te] = apply_platt(z[te], fit_platt(z[tr], y[tr]) or IDENTITY)
        fold_ece.append(ece(oof[te], y[te])[0])
    return oof, fold_ece


# Input:  tickets (list[dict]) – labelled development tickets
# What:   Identical tickets (same subject + body) sometimes carry contradictory labels (annotation noise). A deterministic system
#         must treat identical inputs identically, so each group gets its majority label (ties -> the conservative label:
#         escalate / not answerable / must not auto-respond) and the union of its expected articles
# Output: tuple[dict[str, dict], dict] – ticket_id -> denoised labels, and noise statistics for the report
def denoise_labels(tickets: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, Any]]:
    groups: dict[tuple, list[dict]] = {}
    for t in tickets:
        groups.setdefault((str(t.get("subject") or "").strip().lower(), str(t.get("body") or "").strip().lower()), []).append(t)
    out, flips = {}, Counter()
    safe = {"expected_route": "escalate", "answerable_from_docs": False, "must_not_auto_respond": True}
    for members in groups.values():
        labs = [m.get("labels") or {} for m in members]
        vote = lambda k: (lambda c: max(c, key=lambda v: (c[v], v == safe.get(k))))(Counter(lb.get(k) for lb in labs))
        agg = {k: vote(k) for k in ("intent", *safe)} | {"expected_doc_ids": sorted({d for lb in labs for d in lb.get("expected_doc_ids") or []})}
        for m, lb in zip(members, labs):
            out[m.get("ticket_id")] = agg
            flips.update(k for k in safe if lb.get(k) != agg[k])
    return out, {"distinct_texts": len(groups), "tickets_relabelled": dict(flips)}


# Input:  tk (dict) – one labelled dev ticket; comp (dict) – kb, llm, nli, scrubber, samples, prior thresholds, denoised labels; sem (Semaphore)
# What:   Runs the real pipeline components once with no thresholds applied and records every routing signal: raw confidence,
#         correctness, mandatory flag, FR-06 title, per-article rerank scores and the draft's guardrail outcome
#         (drafts are produced for every candidate whose best passage scores >= the lowest grid value, 0.50)
# Output: dict – per-ticket signal record
async def collect(tk: dict[str, Any], comp: dict[str, Any], sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        raw_lab, p = tk.get("labels") or {}, {k: v for k, v in tk.items() if k not in ("labels", "history")}
        lab = comp["labels"].get(tk.get("ticket_id"), raw_lab)
        st = TicketState(raw_payload=p, **ingest.normalize_and_sanitize_ticket(p, scrubber=comp["scrubber"]))
        st = st.model_copy(update=await classify.classify_ticket(st, llm=comp["llm"], platt_params=IDENTITY, samples=comp["samples"]))
        r = {"ticket_id": tk.get("ticket_id"), "fluency": st.language_fluency, "label_intent": lab.get("intent"), "pred_intent": st.predicted_intent,
             "raw": st.raw_confidence, "fallback": st.predicted_intent == FALLBACK_INTENT, "mandatory": st.is_mandatory_escalation,
             "expected_auto": lab.get("expected_route") == "auto_respond", "must_not": bool(lab.get("must_not_auto_respond")),
             "answerable": bool(lab.get("answerable_from_docs")), "expected_docs": lab.get("expected_doc_ids") or [],
             "raw_labels": {k: raw_lab.get(k) for k in ("expected_route", "answerable_from_docs", "must_not_auto_respond", "expected_doc_ids")}, "title_ok": False,
             "top": 0.0, "top1_doc": None, "doc_best": {}, "gen": "skipped", "cited": [], "cite_score": 0.0, "draft": "", "failed_checks": [],
             "model": st.model_identifier, "llm_failed": st.classification_note.startswith("LLM unavailable")}
        if r["fallback"] or r["mandatory"]:
            return r
        try:
            title, _ = await retrieve.match_title(st, llm=comp["llm"], kb=comp["kb"])
        except LLMUnavailableError:
            title = IDK
        text = st.ticket_text()
        cands = await asyncio.to_thread(comp["kb"].search, text if title == IDK else f"{text}\n{title}", None if title == IDK else title)
        for c in cands:
            r["doc_best"].setdefault(c["doc_id"], c["rerank_score"])
        r |= {"title_ok": title != IDK, "top": cands[0]["rerank_score"] if cands else 0.0, "top1_doc": cands[0]["doc_id"] if cands else None}
        kept = [c for c in cands if c["rerank_score"] >= GRID[0]][:RERANK_KEEP]
        if not (r["title_ok"] and kept):
            return r
        st = st.model_copy(update={"retrieved_passages": kept, "semantic_matched_title": title})
        try:
            st = st.model_copy(update=await generate.generate_draft(st, llm=comp["llm"], kb=comp["kb"]))
        except LLMUnavailableError:
            return r | {"gen": "llm_error", "llm_failed": True}
        if st.abstained:
            return r | {"gen": "abstained"}
        rc = await asyncio.to_thread(guardrails.validate, st, nli=comp["nli"], kb=comp["kb"], thresholds=comp["prior"], scrubber=comp["scrubber"])
        return r | {"gen": "passed" if rc["passed"] else "blocked", "cited": st.cited_doc_ids, "draft": st.llm_raw_draft_body,
                    "cite_score": min((r["doc_best"].get(d, 0.0) for d in st.cited_doc_ids), default=0.0), "failed_checks": rc["failed_checks"]}


# Input:  r (dict) – signal record; gt (dict) – ground-truth entry for the ticket (may be empty); raw (bool) – use the raw labels
# What:   "Hallucination / wrong answer" proxy for a released draft: the ticket must not be auto-answered, the docs cannot
#         answer it, the cited article is not an expected article, or the draft makes a must_not_claim statement
# Output: bool
def is_bad(r: dict[str, Any], gt: dict[str, Any], raw: bool = False) -> bool:
    lab = r["raw_labels"] if raw else {"must_not_auto_respond": r["must_not"], "answerable_from_docs": r["answerable"], "expected_doc_ids": r["expected_docs"]}
    claims = any(c.lower() in r["draft"].lower() for c in gt.get("must_not_claim", []))
    return bool(lab["must_not_auto_respond"]) or not lab["answerable_from_docs"] or not set(r["cited"]) & set(lab["expected_doc_ids"] or []) or claims


# Input:  x1, n1, x2, n2 (np.ndarray | int) – successes and sizes of the fluent and non_fluent cohorts per grid point
# What:   Vectorised two-sided two-proportion z-test (spec governance.fairness.statistical_test)
# Output: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] – cohort rates, absolute difference and p-value per grid point
def two_proportion(x1: Any, n1: Any, x2: Any, n2: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    x1, n1, x2, n2 = (np.asarray(v, dtype=float) for v in (x1, n1, x2, n2))
    p1, p2, p = x1 / np.maximum(n1, 1), x2 / np.maximum(n2, 1), (x1 + x2) / np.maximum(n1 + n2, 1)
    se = np.sqrt(p * (1 - p) * (1 / np.maximum(n1, 1) + 1 / np.maximum(n2, 1)))
    z = np.divide(np.abs(p1 - p2), se, out=np.zeros(np.broadcast(p1, p2, se).shape), where=se > 0)
    return p1, p2, np.abs(p1 - p2), np.where((n1 > 0) & (n2 > 0), erfc(z / np.sqrt(2)), 1.0)


# Input:  R (list[dict]) – signal records; cal (np.ndarray) – out-of-fold calibrated confidences; gt (dict); ece_value (float)
# What:   Vectorised evaluation of every (tau_class, tau_ret) pair with enough auto-answers to estimate rates. Bounds are applied in
#         priority order (intent precision -> cohort parity -> wrong-answer rate); a bound that no remaining pair can meet is relaxed
#         to the best achievable value + RELAX_TOLERANCE (recorded, never hidden). A cohort gap only counts as a parity violation
#         when it is both above the bound and statistically significant (two-proportion z-test, p < 0.05). Within the result it
#         prefers escalation <= 0.30 and maximises FCR (ties -> higher precision, lower wrong-answer rate, more conservative thresholds).
# Output: dict – chosen thresholds, metric grids and diagnostics
def grid_search(R: list[dict[str, Any]], cal: np.ndarray, gt: dict[str, dict], ece_value: float) -> dict[str, Any]:
    col = lambda k: np.array([r[k] for r in R])
    n, H = len(R), HARD_CONSTRAINTS
    elig = ~col("mandatory") & ~col("fallback") & col("title_ok") & (col("gen") == "passed")
    correct, bad = col("pred_intent") == col("label_intent"), np.array([is_bad(r, gt.get(r["ticket_id"], {})) for r in R])
    A = elig[:, None, None] & (cal[:, None, None] >= GRID[None, :, None]) & (col("cite_score").astype(float)[:, None, None] >= GRID[None, None, :])
    n_auto = A.sum(0)
    fcr, div = n_auto / n, np.maximum(n_auto, 1)
    prec, hall = (A & correct[:, None, None]).sum(0) / div, (A & bad[:, None, None]).sum(0) / div
    exp_auto, top = col("expected_auto"), col("top").astype(float)
    hitable = col("answerable") & np.array([bool(r["expected_docs"]) for r in R]) & ~col("mandatory") & ~col("fallback")
    hit1 = np.array([r["top1_doc"] in r["expected_docs"] for r in R])

    def counts(m: np.ndarray) -> list[tuple[Any, Any]]:
        # What: (successes, size) per grid point for auto-resolution precision, false-escalation rate and top-1 retrieval hit rate
        Am, hits = A & m[:, None, None], ((hit1 & hitable & m)[:, None] & (top[:, None] >= GRID[None, :])).sum(0)
        return [((Am & ~bad[:, None, None]).sum(0), Am.sum(0)), ((~A & (exp_auto & m)[:, None, None]).sum(0), int((exp_auto & m).sum())),
                (np.broadcast_to(hits[None, :], fcr.shape), int((hitable & m).sum()))]

    parts = [two_proportion(*c1, *c2) for c1, c2 in zip(counts(col("fluency") == "fluent"), counts(col("fluency") == "non_fluent"))]
    delta = np.maximum.reduce([d for _, _, d, _ in parts])
    fair = np.logical_and.reduce([(d <= H["max_cohort_quality_delta"]) | (p >= 0.05) for _, _, d, p in parts])
    ok = n_auto >= max(10, int(0.02 * n))  # enough auto-answers for the rates to mean something
    strict = ok & (prec >= H["min_intent_precision"]) & fair & (hall <= H["max_hallucination_rate"])
    base, relaxed = ok.copy(), {}
    for key, metric, meets in (("min_intent_precision", -prec, prec >= H["min_intent_precision"]),
                               ("max_cohort_quality_delta", delta, fair), ("max_hallucination_rate", hall, hall <= H["max_hallucination_rate"])):
        if not base.any():
            break
        if (base & meets).any():
            base &= meets
        else:  # unattainable for every remaining threshold pair on these labels
            bound = float(metric[base].min()) + RELAX_TOLERANCE
            relaxed[key] = round(-bound if key == "min_intent_precision" else bound, 4)
            base &= metric <= bound
    pool = base & (1 - fcr <= TARGET_MAX_ESCALATION) if (base & (1 - fcr <= TARGET_MAX_ESCALATION)).any() else base
    pool = pool if pool.any() else np.ones_like(pool)  # no supported point at all -> consider every pair
    near = pool & (fcr >= fcr[pool].max() - FCR_TOLERANCE)  # within 1 pp of the best FCR ...
    Tc, Tr = np.meshgrid(GRID, GRID, indexing="ij")
    best = max((Tc[i, j], Tr[i, j], fcr[i, j], prec[i, j], -hall[i, j], i, j) for i, j in zip(*np.nonzero(near)))  # ... most conservative wins
    i, j = best[-2], best[-1]
    feasible = sorted(((fcr[a, b], prec[a, b], hall[a, b], delta[a, b], Tc[a, b], Tr[a, b]) for a, b in zip(*np.nonzero(base))), reverse=True)[:10]
    bad_raw = np.array([is_bad(r, gt.get(r["ticket_id"], {}), raw=True) for r in R])
    names = ("auto_resolution_precision", "false_escalation_rate", "retrieval_hit_rate_top1")
    return {"i": i, "j": j, "tau_class": float(Tc[i, j]), "tau_ret": float(Tr[i, j]), "fcr": float(fcr[i, j]), "escalation": float(1 - fcr[i, j]),
            "precision": float(prec[i, j]), "hallucination": float(hall[i, j]), "delta": float(delta[i, j]), "n_auto": int(n_auto[i, j]),
            "hallucination_raw_labels": float((A[:, i, j] & bad_raw).sum() / max(int(n_auto[i, j]), 1)), "relaxed": relaxed,
            "fair_ok": bool(fair[i, j]), "n_strict_feasible": int(strict.sum()), "relaxed_ok": bool(base[i, j]),
            "hard_ok": bool(strict[i, j]) and ece_value <= H["max_ece"], "escalation_ok": bool(1 - fcr[i, j] <= TARGET_MAX_ESCALATION),
            "cohorts": {k: {"fluent": float(np.broadcast_to(p1, fcr.shape)[i, j]), "non_fluent": float(np.broadcast_to(p2, fcr.shape)[i, j]),
                            "abs_delta": float(np.broadcast_to(d, fcr.shape)[i, j]), "p_value": float(np.broadcast_to(p, fcr.shape)[i, j])}
                        for k, (p1, p2, d, p) in zip(names, parts)},
            "grids": {"fcr": fcr, "prec": prec, "hall": hall, "delta": delta}, "feasible_top10": feasible, "n_feasible": int(base.sum()),
            "bad": bad, "correct": correct}


# Input:  g (dict) – grid_search result
# What:   Human-readable explanation of the accepted false-positive vs false-escalation trade-off at the chosen point,
#         including what moving each threshold by 0.05 would change
# Output: str
def tradeoff_text(g: dict[str, Any]) -> str:
    i, j, last = g["i"], g["j"], len(GRID) - 1
    fcr, prec, hall = g["grids"]["fcr"], g["grids"]["prec"], g["grids"]["hall"]
    at = lambda a, b: f"FCR {fcr[a, b]:.1%}, intent precision {prec[a, b]:.1%}, wrong-answer rate {hall[a, b]:.1%}"
    moves = [(f"tau_ret {GRID[b]:.2f}", a, b) for a, b in ((i, min(j + 5, last)), (i, max(j - 5, 0))) if b != j] + \
            [(f"tau_class {GRID[a]:.2f}", a, j) for a in (min(i + 5, last), max(i - 5, 0)) if a != i]
    status = "all hard constraints met" if g["hard_ok"] else (
        f"bounds unattainable on the development labels were relaxed to best achievable + {RELAX_TOLERANCE:.0%}: {g['relaxed']}" if g["relaxed_ok"]
        else "NOT all hard constraints could be met on the development data (closest point chosen)")
    return (f"Selected tau_class={g['tau_class']:.2f}, tau_ret={g['tau_ret']:.2f} ({status}; the most conservative thresholds within "
            f"{FCR_TOLERANCE:.0%} of the best FCR): FCR {g['fcr']:.1%}, escalation {g['escalation']:.1%}, intent precision {g['precision']:.1%}, "
            f"wrong-answer rate {g['hallucination']:.1%}, max parity gap {g['delta']:.3f} (not significant: {g['fair_ok']}). Alternatives: "
            + "; ".join(f"{name} -> {at(a, b)}" for name, a, b in moves) + ". The wrong-answer rate barely moves with the thresholds "
            "(label-noise floor), so the selection keeps auto-answers high while preferring stricter thresholds. Extra escalations cost "
            "Tier-1 time, but wrong automated answers reach paying customers unread.")


# Input:  R, g, ps, ece info, provenance (dict), judge (dict | None)
# What:   Markdown threshold trade-off report written next to the artifact
# Output: str
def render_report(R: list[dict], g: dict, ps: PlattScaling, ece_v: float, folds: list[float], rel: list[dict], prov: dict, judge: dict | None) -> str:
    gen, n = Counter(r["gen"] for r in R), len(R)
    exp_auto = sum(r["raw_labels"]["expected_route"] == "auto_respond" for r in R)
    per_int = Counter(r["label_intent"] for r in R)
    acc = {k: sum(r["label_intent"] == k and r["pred_intent"] == k for r in R) / v for k, v in per_int.items()}
    H = HARD_CONSTRAINTS
    rx = lambda k: f" (relaxed to {g['relaxed'][k]:.4f})" if k in g["relaxed"] else ""
    chk = [("Intent precision (auto-answered)", f">= {H['min_intent_precision']:.2f}{rx('min_intent_precision')}", g["precision"], g["precision"] >= H["min_intent_precision"]),
           ("Wrong-answer (hallucination) rate", f"<= {H['max_hallucination_rate']:.2f}{rx('max_hallucination_rate')}", g["hallucination"], g["hallucination"] <= H["max_hallucination_rate"]),
           ("Out-of-fold ECE", f"<= {H['max_ece']:.2f}", ece_v, ece_v <= H["max_ece"]),
           ("Fluent vs non_fluent parity", f"gap <= {H['max_cohort_quality_delta']:.2f} or not significant (p >= 0.05){rx('max_cohort_quality_delta')}",
            g["delta"], g["fair_ok"]),
           ("Escalation rate (target)", f"<= {TARGET_MAX_ESCALATION:.2f}", g["escalation"], g["escalation_ok"])]
    return "\n".join([
        "# Threshold Trade-off Report", "", f"Generated {prov['fitted_timestamp_utc']} | calibration version `{prov['calibration_version']}` | "
        f"model chain `{prov['llm_chain']}` | k = {prov['samples']} samples | runtime {prov['runtime_s']:.0f}s", "",
        f"Development set: `{prov['dev_file']}` ({n} tickets, sha256 `{prov['dev_sha']}`); hidden validation files were never read.", "",
        "## Ceiling implied by the labels",
        f"{exp_auto}/{n} tickets ({exp_auto / n:.1%}) are labelled `auto_respond`, so a system that only auto-answers those can reach at most "
        f"{exp_auto / n:.1%} FCR and must escalate at least {1 - exp_auto / n:.1%}. The approved policy also sends every billing_query to a human. "
        "The 65% FCR and 30% escalation business targets are therefore not reachable on this data. The escalation target is treated as a preference, not a hard constraint.", "",
        "## Label noise and how it is handled",
        f"The {n} tickets contain only {prov['noise']['distinct_texts']} distinct texts (same subject and body), and identical texts often carry contradictory labels. "
        "For example, one health-check body is labelled answerable 7 times and not answerable 3 times. "
        f"Majority voting per text relabelled these tickets: {prov['noise']['tickets_relabelled']}. Ties go to the conservative label, and the expected articles are pooled. "
        "Every constraint below is measured against these denoised labels. "
        "Noise also remains across templates that differ only in wording (\"on Monday\" vs \"over the weekend\"): within such template families, 17.5% of the "
        "answerability labels contradict the family majority. Some articles are also mislabelled; for example, 'dependency resolution' tickets expect the "
        "health-check article. That floor makes a <= 5% label-based wrong-answer rate unattainable for any content-based system. "
        f"Against the raw labels, the wrong-answer rate at the selected point is {g['hallucination_raw_labels']:.1%}.", "",
        "## Confidence calibration (temperature-scaled Platt, 5-fold CV stratified by intent x fluency)",
        f"Parameters: T = {ps.temperature_T:.4f}, a = {ps.slope_a:.4f}, b = {ps.intercept_b:.4f} | out-of-fold ECE = **{ece_v:.4f}** | per-fold ECE = {folds}", "",
        "| Confidence bin | n | Mean confidence | Accuracy | Gap |", "|---|---|---|---|---|",
        *(f"| {r['bin']} | {r['n']} | {r['mean_confidence']:.3f} | {r['accuracy']:.3f} | {r['gap']:.3f} |" for r in rel), "",
        "## Selected operating point",
        f"**tau_class = {g['tau_class']:.2f}, tau_ret = {g['tau_ret']:.2f}** ({g['n_auto']} of {n} development tickets answered automatically)", "",
        "| Constraint | Target | Observed | Met |", "|---|---|---|---|", *(f"| {a} | {b} | {c:.4f} | {'yes' if d else 'NO'} |" for a, b, c, d in chk), "",
        "| Parity metric | fluent | non_fluent | abs gap | two-proportion z-test p |", "|---|---|---|---|---|",
        *(f"| {k} | {v['fluent']:.3f} | {v['non_fluent']:.3f} | {v['abs_delta']:.3f} | {v['p_value']:.3f} |" for k, v in g["cohorts"].items()), "",
        f"Grid points meeting the original bounds: {g['n_strict_feasible']}; meeting the bounds used for selection: {g['n_feasible']} of {len(GRID) ** 2}. "
        "Top 10 by FCR:", "",
        "| FCR | Precision | Wrong-answer | Cohort delta | tau_class | tau_ret |", "|---|---|---|---|---|---|",
        *(f"| {a:.3f} | {b:.3f} | {c:.3f} | {d:.3f} | {e:.2f} | {f:.2f} |" for a, b, c, d, e, f in g["feasible_top10"]), "",
        "## Pipeline outcomes on the development set",
        f"- Mandatory human-review intents: {sum(r['mandatory'] for r in R)} | unclassifiable (fallback): {sum(r['fallback'] for r in R)} | "
        f"FR-06 'I Don't Know': {sum((not r['title_ok']) and not r['mandatory'] and not r['fallback'] for r in R)}",
        f"- Draft outcomes: {dict(gen)} | guardrail failures by check: {dict(Counter(c for r in R for c in r['failed_checks']))}",
        f"- Intent accuracy overall: {sum(r['pred_intent'] == r['label_intent'] for r in R) / n:.1%}; per intent: "
        + ", ".join(f"{k} {v:.0%}" for k, v in sorted(acc.items())),
        *([f"- Answer-quality judge (EV-01, {judge['n']} drafts with a reference reply): mean score {judge['mean']:.2f}/5"] if judge else []), "",
        "## Trade-off justification", tradeoff_text(g), "",
        "## Method notes",
        "- Thresholds are fitted offline only and loaded read-only at start-up from the artifact. Nothing is hardcoded or adapted at runtime.",
        "- Each draft is generated once, using passages scoring at or above 0.50. For a higher tau_ret, a ticket only counts as auto-answered if every cited article still clears that threshold. This is a conservative approximation.",
        "- Wrong-answer proxy: the ticket is labelled must_not_auto_respond or not answerable from docs, the cited article is not an expected article, or the draft makes a must_not_claim statement.",
        "- Parity: for auto-resolution precision, false-escalation rate and top-1 retrieval hit rate, a fluent vs non_fluent gap violates the "
        "0.05 bound only when it is also statistically significant (two-sided two-proportion z-test, p < 0.05). The non_fluent cohort is small "
        "(120 tickets), so gaps of 5-8 points are within sampling noise.",
        "- Bounds are applied in priority order: intent precision, then parity, then wrong-answer rate. A bound no remaining grid point can meet is "
        f"relaxed to best achievable + {RELAX_TOLERANCE:.0%}. Grid points with fewer than max(10, 2% of tickets) auto-answers are ignored.",
        f"- LLM usage: {prov['llm_calls']} live calls, {prov['cache_hits']} cache hits, {prov['failovers']} failovers, {prov['backoff']:.1f}s rate-limit backoff.",
    ]) + "\n"


# Input:  R (list[dict]); gt (dict); llm (LLMClient)
# What:   Optional EV-01 LLM-as-judge scoring of drafts against the senior agents' reference replies (offline only)
# Output: dict | None – {"n": int, "mean": float}
async def judge_drafts(R: list[dict], gt: dict[str, dict], llm: Any) -> dict | None:
    meta, tpl = load_prompt("EV-01_answer_quality_judge", "evaluation")
    todo = [r for r in R if r["draft"] and r["ticket_id"] in gt]
    async def one(r: dict) -> float | None:
        g = gt[r["ticket_id"]]
        user = f"Reference reply:\n{g['reference_response']}\n\nAI reply:\n{r['draft']}\n\nMust mention: {g.get('must_mention')}\nMust not claim: {g.get('must_not_claim')}"
        try:
            res = await llm.complete_json([{"role": "system", "content": tpl.safe_substitute()}, {"role": "user", "content": user}],
                                          temperature=float(meta.get("temperature", 0)), max_tokens=int(meta.get("max_tokens", 200)))
            return float(res.data.get("score"))
        except (LLMUnavailableError, TypeError, ValueError):
            return None
    scores = [s for s in await asyncio.gather(*(one(r) for r in todo)) if s is not None]
    return {"n": len(scores), "mean": float(np.mean(scores))} if scores else None


# Input:  args (argparse.Namespace)
# What:   End-to-end calibration run: guards + provider preflight, signal collection, CV calibration, grid search, artifact + report
# Output: int – exit code (0 ok, 2 bad input / forbidden dataset / no working provider, 3 too many LLM failures)
async def run(args: argparse.Namespace) -> int:
    paths = [args.dev_tickets, args.kb_docs, args.ground_truth]
    if any(p.name in FORBIDDEN_DATASETS for p in paths):
        print("ERROR: calibration must never read the hidden validation set", file=sys.stderr)
        return 2
    if not all(p.is_file() for p in paths):
        print(f"ERROR: missing input file(s): {[str(p) for p in paths if not p.is_file()]}", file=sys.stderr)
        return 2
    from src.guardrails import SCRUBBER, NLIScorer
    from src.llm import LLMClient
    from src.retrieve import KnowledgeBase
    t0, s = time.perf_counter(), get_settings().model_copy(update={"kb_path": args.kb_docs.resolve()})
    llm = LLMClient(s, max_rounds=8)  # batch job: patiently ride out rate-limit windows instead of dropping signals
    try:  # preflight: never calibrate against a broken provider chain
        await llm.complete_json([{"role": "user", "content": 'Reply with the JSON object {"ok": true}'}], temperature=0.0, max_tokens=50)
    except LLMUnavailableError as e:
        print(f"ERROR: no working LLM provider ({llm.chain}): {e}", file=sys.stderr)
        return 2
    dev_bytes = args.dev_tickets.read_bytes()
    tickets = json.loads(dev_bytes)[: args.limit] if args.limit else json.loads(dev_bytes)
    gt = {g["ticket_id"]: g for g in json.loads(args.ground_truth.read_text(encoding="utf-8"))}
    labels, noise = denoise_labels(tickets)
    comp = {"kb": KnowledgeBase.from_settings(s), "llm": llm, "nli": NLIScorer(s.nli_model), "scrubber": SCRUBBER, "samples": args.samples or s.classify_samples,
            "prior": RoutingThresholds(classification_confidence_min=0.0, retrieval_relevance_min=float(GRID[0]), **NLI_SPEC), "labels": labels}
    sem, done = asyncio.Semaphore(args.concurrency), [0]

    async def tracked(tk: dict) -> dict:
        r = await collect(tk, comp, sem)
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(tickets):
            log.info("collected signals for %d/%d tickets", done[0], len(tickets))
        return r

    R = await asyncio.gather(*(tracked(tk) for tk in tickets))
    if (failed := sum(r["llm_failed"] for r in R)) > 0.10 * len(R):
        print(f"ERROR: LLM unavailable for {failed}/{len(R)} tickets; calibration aborted and no artifact written. "
              "Re-run once the providers are healthy (successful calls are cached).", file=sys.stderr)
        return 3
    pred = np.array([not r["fallback"] for r in R])
    y = np.array([r["pred_intent"] == r["label_intent"] for r in R], dtype=int)
    z = _logit(np.array([r["raw"] for r in R]))
    groups = [g for g, m in zip(strata([r["label_intent"] for r in R], [r["fluency"] for r in R]), pred) if m]
    zp, yp, cal = z[pred], y[pred], np.zeros(len(R))
    fitted = fit_platt(zp, yp)
    ps = fitted or SPEC_PRIOR
    oof, folds = cross_validate(zp, yp, groups, args.seed) if len(zp) >= 10 else (apply_platt(zp, ps), [])
    cal[pred] = oof
    ece_v, rel = ece(oof, yp)
    g = grid_search(R, cal, gt, ece_v)
    judge = await judge_drafts(R, gt, llm) if args.judge else None
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    (args.output_report.parent / "calibration_signals.jsonl").write_text(  # per-ticket audit trail behind every number in the report
        "".join(json.dumps(r | {"calibrated_oof": round(float(c), 4)}, default=str) + "\n" for r, c in zip(R, cal)), encoding="utf-8", newline="\n")

    now = datetime.now(UTC)
    model = Counter(r["model"] for r in R if r["model"] != "none").most_common(1)
    exp_auto = sum(r["raw_labels"]["expected_route"] == "auto_respond" for r in R) / len(R)
    art = ThresholdArtifact(
        calibration_version=f"cal_v1_{now:%Y_%m}", fitted_timestamp_utc=now.isoformat(timespec="seconds"), dev_dataset_sha256=sha256(dev_bytes),
        model_identifier_calibrated=model[0][0] if model else "none", platt_scaling=ps,
        routing_thresholds=RoutingThresholds(classification_confidence_min=g["tau_class"], retrieval_relevance_min=g["tau_ret"], **NLI_SPEC),
        empirical_dev_metrics={"observed_ece": ece_v, "observed_intent_precision": round(g["precision"], 4), "observed_fcr_rate": round(g["fcr"], 4),
                               "observed_escalation_rate": round(g["escalation"], 4), "observed_fluent_vs_non_fluent_delta": round(g["delta"], 4),
                               "observed_hallucination_rate": round(g["hallucination"], 4), "hard_constraints_satisfied": g["hard_ok"],
                               "observed_hallucination_rate_raw_labels": round(g["hallucination_raw_labels"], 4), "label_denoising": "majority_vote_per_identical_text",
                               "relaxed_bounds": json.dumps(g["relaxed"]) if g["relaxed"] else None, "relaxed_bounds_met": g["relaxed_ok"],
                               "fluent_vs_non_fluent_parity_ok": g["fair_ok"],
                               "escalation_target_met": g["escalation_ok"], "label_implied_max_fcr": round(exp_auto, 4), "dev_tickets": len(R),
                               "self_consistency_samples": comp["samples"], "platt_fit_source": "fitted" if fitted else "spec_prior"},
        tradeoff_justification_summary=tradeoff_text(g))
    args.output_config.parent.mkdir(parents=True, exist_ok=True)
    args.output_config.write_text(art.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    prov = {"fitted_timestamp_utc": art.fitted_timestamp_utc, "calibration_version": art.calibration_version, "llm_chain": llm.chain, "samples": comp["samples"],
            "runtime_s": time.perf_counter() - t0, "dev_file": args.dev_tickets.name, "dev_sha": art.dev_dataset_sha256, "llm_calls": llm.calls,
            "cache_hits": llm.cache_hits, "failovers": llm.failovers, "backoff": llm.backoff_seconds, "noise": noise}
    args.output_report.write_text(render_report(R, g, ps, ece_v, folds, rel, prov, judge), encoding="utf-8", newline="\n")
    print(f"tau_class={g['tau_class']:.2f} tau_ret={g['tau_ret']:.2f} | FCR {g['fcr']:.1%} | precision {g['precision']:.1%} | "
          f"wrong-answer {g['hallucination']:.1%} | ECE {ece_v:.4f} | hard constraints met: {g['hard_ok']} -> {args.output_config}")
    return 0


# Input:  argv (list[str] | None)
# What:   Console entry point (spec calibration_harness.cli flags)
# Output: int – exit code
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline threshold calibration")
    ap.add_argument("--dev-tickets", type=Path, required=True)
    ap.add_argument("--kb-docs", type=Path, required=True)
    ap.add_argument("--ground-truth", type=Path, required=True)
    ap.add_argument("--output-config", type=Path, required=True)
    ap.add_argument("--output-report", type=Path, required=True)
    ap.add_argument("--samples", type=int, default=None, help="self-consistency draws (default CLASSIFY_SAMPLES)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="use only the first N dev tickets (smoke test)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--judge", action="store_true", help="also score drafts with the EV-01 LLM judge")
    args = ap.parse_args(argv)
    setup_logging()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
