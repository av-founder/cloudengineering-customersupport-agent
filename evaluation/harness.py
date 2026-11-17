"""Unattended evaluation harness: python -m evaluation.harness --input <tickets.json> --output <dir> [--thresholds-config PATH]

Processes every supplied ticket with zero human intervention, logs every decision, and writes predictions.jsonl,
decision_audit_log.jsonl, metrics_report.json and metrics_report.md. Exit codes: 0 ok, 1 invariant broken, 2 bad input/config.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.metrics_calculator import compute_metrics, render_markdown
from src.config import ANSWERED_ACTIONS, ROOT, ThresholdConfigError, get_settings, load_thresholds, setup_logging
from src.schemas import TicketState

log = logging.getLogger("evaluation.harness")
_TIER_RANK = {"enterprise": 2, "business": 1}


# Input:  argv (list[str] | None)
# What:   Command-line contract (flags first, then EVAL_INPUT_PATH / EVAL_OUTPUT_DIR from the environment or .env; no hardcoded paths)
# Output: argparse.Namespace
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap, s = argparse.ArgumentParser(description=__doc__.splitlines()[0]), get_settings()
    ap.add_argument("--input", "--dataset", dest="input", default=os.getenv("EVAL_INPUT_PATH") or s.eval_input_path or None,
                    help="JSON array of tickets (any size)")
    ap.add_argument("--output", default=os.getenv("EVAL_OUTPUT_DIR") or s.eval_output_dir or None,
                    help="output directory (default: evaluation/results/<UTC timestamp>)")
    ap.add_argument("--thresholds-config", default=None, help="calibration artifact (default: THRESHOLD_CONFIG_PATH or artifacts/thresholds_v1.json)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="tickets processed in parallel (default 1 = honest per-ticket latency; higher is faster but latency then includes queueing)")
    ap.add_argument("--limit", type=int, default=None, help="process only the first N tickets (smoke tests)")
    return ap.parse_args(argv)


# Input:  i (int) – position in the input file; raw (Any) – one input record
# What:   Strips ground-truth fields (labels, history) so the pipeline can never see them, and gives id-less records an id
# Output: dict – payload for the pipeline
def _payload(i: int, raw: Any) -> dict[str, Any]:
    p = {k: v for k, v in raw.items() if k not in ("labels", "history")} if isinstance(raw, dict) else {}
    p.setdefault("ticket_id", f"INPUT-{i + 1:05d}")
    return p


# Input:  st (TicketState) – final state; raw (Any) – input record; pos (int) – batch queue position; leaks (int) – PII found
#         when re-scanning the released reply
# What:   One prediction line (ground-truth labels are kept separately and never written to predictions.jsonl)
# Output: dict
def _record(st: TicketState, raw: Any, pos: int, leaks: int) -> dict[str, Any]:
    answered = st.routing_action in ANSWERED_ACTIONS
    return {
        "ticket_id": st.external_ticket_ref, "internal_ticket_id": st.ticket_id, "trace_id": st.trace_id, "decision_id": st.decision_id,
        "channel": st.source_channel, "customer_tier": st.customer_tier, "language_fluency": st.language_fluency,
        "priority_level": st.priority_level, "priority_score": st.priority_score, "queue_position": pos,
        "predicted_intent": st.predicted_intent, "predicted_urgency": st.predicted_urgency, "raw_confidence": st.raw_confidence,
        "calibrated_confidence": st.calibrated_confidence, "considered_alternatives": st.considered_alternatives,
        "is_mandatory_escalation": st.is_mandatory_escalation, "semantic_matched_title": st.semantic_matched_title,
        "retrieval_executed": st.retrieval_executed, "retrieved_doc_ids": list(dict.fromkeys(p["doc_id"] for p in st.retrieved_passages)),
        "top_retrieval_score": st.top_retrieval_score, "routing_action": st.routing_action, "routing_reason": st.routing_reason_manager_text,
        "target_queue": st.target_queue, "guardrail_passed": st.guardrail_passed,
        "final_response": st.final_formatted_response if answered else None, "cited_doc_ids": st.cited_doc_ids, "doc_link": st.attached_doc_link,
        "escalation_summary_markdown": None if answered else st.escalation_summary_markdown, "validation_receipt": st.validation_receipt,
        "ingest_anomaly_flags": st.ingest_anomaly_flags, "pii_redaction_counts": st.pii_redaction_counts, "latency_ms": st.end_to_end_latency_ms,
        "llm_calls": st.llm_calls, "prompt_tokens": st.prompt_tokens, "completion_tokens": st.completion_tokens,
        "model_provider": st.model_provider, "error": st.error, "outbound_leak_count": leaks,
        "labels": raw.get("labels") if isinstance(raw, dict) else None,
    }


# Input:  path (Path); rows (list[dict])
# What:   Writes one JSON object per line (UTF-8)
# Output: None
def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8", newline="\n")


# Input:  args (argparse.Namespace); components (dict | None) – injected pipeline parts for tests (kb, llm, nli, scrubber, store)
# What:   Loads the tickets (UTF-8, with or without a byte-order mark) and frozen thresholds, processes every ticket (highest
#         tier first, in parallel), exports the audit rows, computes all metrics in code and writes the four output files
# Output: int – exit code
async def run(args: argparse.Namespace, components: dict[str, Any] | None = None) -> int:
    src = Path(args.input) if args.input else None
    if not src or not src.is_file():
        print(f"ERROR: input file not found: {args.input!r} (use --input or EVAL_INPUT_PATH)", file=sys.stderr)
        return 2
    try:
        tickets = json.loads(src.read_text(encoding="utf-8-sig"))
        tickets = tickets.get("tickets", [tickets]) if isinstance(tickets, dict) else tickets
        assert isinstance(tickets, list)
        thresholds = load_thresholds(args.thresholds_config)
    except (ValueError, AssertionError) as e:
        print(f"ERROR: unreadable input or calibration artifact: {e}", file=sys.stderr)
        return 2
    except ThresholdConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    tickets = tickets[: args.limit] if args.limit else tickets
    out = Path(args.output) if args.output else ROOT / "evaluation" / "results" / datetime.now(UTC).strftime("run_%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)

    from src.graph import TriagePipeline
    from src.guardrails import SCRUBBER
    start, t0, s, c = datetime.now(UTC), time.perf_counter(), get_settings(), dict(components or {})
    if "kb" not in c:
        from src.guardrails import NLIScorer
        from src.llm import LLMClient
        from src.logging_store import AuditStore
        from src.retrieve import KnowledgeBase
        c |= {"kb": KnowledgeBase.from_settings(s), "llm": LLMClient(s, max_rounds=8), "nli": NLIScorer(s.nli_model), "store": AuditStore(s.async_db_url)}
    c.setdefault("scrubber", SCRUBBER)
    await c["store"].init(c["kb"].registry)
    c["kb"].excluded = await c["store"].quarantined_doc_ids()
    pipe = TriagePipeline(settings=s, thresholds=thresholds, **c)
    await pipe.warm_up()
    setup = time.perf_counter() - t0

    payloads = [_payload(i, raw) for i, raw in enumerate(tickets)]
    order = sorted(range(len(payloads)), key=lambda i: (-_TIER_RANK.get(str(payloads[i].get("customer_tier")).lower(), 0), str(payloads[i].get("received_at") or ""), i))
    results: list[TicketState | None] = [None] * len(payloads)
    sem, done = asyncio.Semaphore(max(1, args.concurrency)), [0]

    async def one(i: int) -> None:
        async with sem:
            results[i] = await pipe.process(payloads[i])
        done[0] += 1
        if done[0] % 10 == 0 or done[0] == len(payloads):
            log.info("processed %d/%d tickets", done[0], len(payloads))

    await asyncio.gather(*(one(i) for i in order))
    from src.route import queue_order
    pos = queue_order([(st.priority_score, st.received_at) for st in results])
    records = [_record(st, raw, p, len(c["scrubber"].scrub(st.final_formatted_response)[1]) if st.routing_action in ANSWERED_ACTIONS else 0)
               for st, raw, p in zip(results, tickets, pos)]
    audit = await c["store"].export([st.trace_id for st in results])
    await c["store"].close()

    end, total, llm = datetime.now(UTC), time.perf_counter() - t0, c["llm"]
    wall = {"start_utc": start.isoformat(), "end_utc": end.isoformat(), "total_seconds": round(total, 2), "setup_seconds": round(setup, 2),
            "rate_limit_backoff_seconds": round(getattr(llm, "backoff_seconds", 0.0), 2),
            "runtime_explanation": f"{len(records)} tickets in {total:.1f}s ({(total - setup) / max(len(records), 1):.2f}s per ticket after {setup:.1f}s of "
                                   f"model loading/indexing) at concurrency {args.concurrency}; {getattr(llm, 'calls', 0)} live LLM calls, "
                                   f"{getattr(llm, 'cache_hits', 0)} cache hits, {getattr(llm, 'failovers', 0)} provider failovers, "
                                   f"{getattr(llm, 'backoff_seconds', 0.0):.1f}s rate-limit backoff."}
    metrics = compute_metrics(records, decisions_logged=len(audit), wall=wall)
    metrics["run"] = {"input_file": src.name, "total_input_records": len(tickets), "calibration_version": thresholds.calibration_version,
                      "routing_mode": pipe.mode, "llm_chain": getattr(llm, "chain", "n/a")}

    _jsonl(out / "predictions.jsonl", [{k: v for k, v in r.items() if k != "labels"} for r in records])
    _jsonl(out / "decision_audit_log.jsonl", audit)
    (out / "metrics_report.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8", newline="\n")
    (out / "metrics_report.md").write_text(render_markdown(metrics, f"Evaluation Metrics Report - {src.name}"), encoding="utf-8", newline="\n")
    v = metrics["volume"]
    print(f"Processed {v['tickets_processed']} | answered {v['answered_automatically']} | escalated {v['escalated']} | "
          f"blocked {v['blocked_by_guardrails']} | decisions logged {len(audit)} -> {out}")
    return 0 if all(metrics["invariants"].values()) and len(records) == len(tickets) else 1


# Input:  argv (list[str] | None)
# What:   Console entry point
# Output: int – exit code
def main(argv: list[str] | None = None) -> int:
    setup_logging()
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
