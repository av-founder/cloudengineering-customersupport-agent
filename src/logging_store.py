"""Decision log (NFR-05): 4-table schema, append-only audit triggers (SQLite + PostgreSQL 16) and SHA-256 hash chaining."""
import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from src.config import ANSWERED_ACTIONS, CHANNELS, FLUENCY, PRIORITY_LEVELS, ROUTING_ACTIONS, TIERS, sha256

GENESIS = "0" * 64
IMMUTABLE_MSG = "SECURITY VIOLATION: decision_audit_log is strictly append-only (NFR-05)."
MD = sa.MetaData()
JSONX = sa.JSON().with_variant(JSONB(), "postgresql")
NUM = sa.Numeric(5, 4, asdecimal=False)
TS = sa.DateTime(timezone=True)


# Input:  name (str); values (tuple[str, ...])
# What:   Portable enum: native type on PostgreSQL, CHECK constraint on SQLite
# Output: sa.Enum
def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, create_constraint=True, validate_strings=True)


# Input:  none
# What:   Current UTC timestamp
# Output: datetime
def _now() -> datetime:
    return datetime.now(UTC)


# Table 1 – the official article registry (single source of truth for citations; quarantine flag for the runbook)
kb_articles = sa.Table(
    "kb_articles_registry", MD,
    sa.Column("doc_id", sa.String(64), primary_key=True),
    sa.Column("article_title", sa.Text, nullable=False, unique=True),
    sa.Column("doc_link", sa.Text, nullable=False),
    sa.Column("content_sha256", sa.CHAR(64), nullable=False),
    sa.Column("category", sa.String(128), nullable=False),
    sa.Column("author", sa.String(128), default="Ines Varga"),
    sa.Column("is_official_approved", sa.Boolean, nullable=False, default=True),
    sa.Column("indexed_at", TS, nullable=False, default=_now),
)

# Table 2 – canonical sanitised tickets (raw text is never stored)
tickets = sa.Table(
    "tickets", MD,
    sa.Column("ticket_id", sa.Uuid, primary_key=True, default=uuid.uuid4),
    sa.Column("external_ticket_ref", sa.String(128), nullable=False, unique=True),
    sa.Column("channel", _enum("channel_enum", CHANNELS), nullable=False),
    sa.Column("customer_id_hash", sa.CHAR(64), nullable=False),
    sa.Column("customer_tier", _enum("customer_tier_enum", TIERS), nullable=False, default="standard"),
    sa.Column("language_fluency", _enum("fluency_cohort_enum", FLUENCY), nullable=False, default="unknown"),
    sa.Column("sanitized_subject", sa.Text, nullable=False),
    sa.Column("sanitized_body", sa.Text, nullable=False),
    sa.Column("ingest_anomaly_flags", JSONX, nullable=False, default=list),
    sa.Column("pii_redacted_at_ingest", sa.Boolean, nullable=False, default=False),
    sa.Column("pii_redaction_types", JSONX, nullable=False, default=list),
    sa.Column("priority_level", _enum("urgency_level_enum", PRIORITY_LEVELS), nullable=False, default="medium"),
    sa.Column("priority_score", sa.Integer, sa.CheckConstraint("priority_score BETWEEN 1 AND 100", name="ck_tickets_priority_score"), nullable=False, default=50),
    sa.Column("queue_status", sa.String(32), nullable=False, default="QUEUED"),
    sa.Column("created_at", TS, nullable=False, default=_now),
    sa.Column("processed_at", TS),
    sa.Index("idx_tickets_priority_queue", "queue_status", sa.desc("priority_score"), "created_at"),
    sa.Index("idx_tickets_fluency_cohort", "language_fluency", "created_at"),
    sa.Index("idx_tickets_repeat_contact", "customer_id_hash", "created_at"),
)

# Table 3 – immutable, hash-chained decision audit log
decisions = sa.Table(
    "decision_audit_log", MD,
    sa.Column("decision_id", sa.Uuid, primary_key=True, default=uuid.uuid4),
    sa.Column("chain_seq", sa.BigInteger, nullable=False, unique=True),
    sa.Column("ticket_id", sa.Uuid, sa.ForeignKey("tickets.ticket_id", ondelete="RESTRICT"), nullable=False),
    sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
    sa.Column("predicted_intent", sa.String(128), nullable=False),
    sa.Column("predicted_urgency", _enum("urgency_level_enum", PRIORITY_LEVELS), nullable=False),
    sa.Column("raw_classifier_confidence", NUM, sa.CheckConstraint("raw_classifier_confidence BETWEEN 0 AND 1", name="ck_dal_raw_conf"), nullable=False),
    sa.Column("calibrated_confidence", NUM, sa.CheckConstraint("calibrated_confidence BETWEEN 0 AND 1", name="ck_dal_cal_conf"), nullable=False),
    sa.Column("considered_alternatives", JSONX, nullable=False),
    sa.Column("calibration_version", sa.String(32), nullable=False),
    sa.Column("is_mandatory_escalation", sa.Boolean, nullable=False),
    sa.Column("matched_semantic_title", sa.Text),
    sa.Column("retrieved_sources", JSONX, nullable=False),
    sa.Column("top_retrieval_score", NUM, nullable=False),
    sa.Column("highest_rejected_score", NUM),
    sa.Column("routing_action", _enum("routing_action_enum", ROUTING_ACTIONS), nullable=False),
    sa.Column("routing_reason", sa.Text, nullable=False),
    sa.Column("drafted_response", sa.Text),
    sa.Column("cited_doc_id", sa.String(64), sa.ForeignKey("kb_articles_registry.doc_id")),
    sa.Column("attached_doc_link", sa.Text),
    sa.Column("escalation_summary_md", sa.Text),
    sa.Column("guardrail_passed", sa.Boolean, nullable=False),
    sa.Column("validation_receipt_json", JSONX, nullable=False),
    sa.Column("pii_outbound_detected", sa.Boolean, nullable=False),
    sa.Column("nli_groundedness_score", NUM),
    sa.Column("disclaimer_verified", sa.Boolean, nullable=False),
    sa.Column("citation_verified", sa.Boolean, nullable=False),
    sa.Column("model_provider", sa.String(64), nullable=False),
    sa.Column("model_identifier", sa.String(128), nullable=False),
    sa.Column("prompt_bundle_version", sa.String(64), nullable=False),
    sa.Column("end_to_end_latency_ms", sa.Integer, nullable=False),
    sa.Column("prompt_tokens", sa.Integer, nullable=False),
    sa.Column("completion_tokens", sa.Integer, nullable=False),
    sa.Column("prev_record_hash", sa.CHAR(64), nullable=False),
    sa.Column("record_hash", sa.CHAR(64), nullable=False),
    sa.Column("created_at", TS, nullable=False),
)

# Table 4 – human corrections and technical-writer feedback loop
feedback = sa.Table(
    "agent_feedback_loop", MD,
    sa.Column("feedback_id", sa.Uuid, primary_key=True, default=uuid.uuid4),
    sa.Column("decision_id", sa.Uuid, sa.ForeignKey("decision_audit_log.decision_id"), nullable=False),
    sa.Column("ticket_id", sa.Uuid, sa.ForeignKey("tickets.ticket_id"), nullable=False),
    sa.Column("agent_id", sa.String(64), nullable=False),
    sa.Column("agent_tier", sa.String(16), sa.CheckConstraint("agent_tier IN ('TIER_1', 'TIER_2', 'TECH_WRITER')", name="ck_fb_tier"), nullable=False),
    sa.Column("agent_action", sa.String(32), sa.CheckConstraint(
        "agent_action IN ('ACCEPTED_AS_IS', 'EDITED_AND_SENT', 'REJECTED_ESCALATED', 'RESOLVED_BY_TIER2')", name="ck_fb_action"), nullable=False),
    sa.Column("actual_true_intent", sa.String(128)),
    sa.Column("flag_for_tech_writer", sa.Boolean, nullable=False, default=False),
    sa.Column("failure_attribution", sa.String(64), sa.CheckConstraint(
        "failure_attribution IN ('DOC_MISSING', 'DOC_OUTDATED', 'AI_MISINTERPRETED', 'NONE')", name="ck_fb_attr")),
    sa.Column("agent_notes", sa.Text),
    sa.Column("customer_csat_score", sa.SmallInteger, sa.CheckConstraint("customer_csat_score BETWEEN 1 AND 5", name="ck_fb_csat")),
    sa.Column("created_at", TS, nullable=False, default=_now),
)

# Input:  none
# What:   Database-level immutability: any UPDATE / DELETE (and TRUNCATE on PostgreSQL) of the audit log aborts the transaction
# Output: list[str] – DDL statements per dialect
SQLITE_DDL = [f"CREATE TRIGGER IF NOT EXISTS trg_immutable_decision_audit_log_{op.lower()} BEFORE {op} ON decision_audit_log "
              f"BEGIN SELECT RAISE(ABORT, '{IMMUTABLE_MSG}'); END" for op in ("UPDATE", "DELETE")]
PG_DDL = [
    f"CREATE OR REPLACE FUNCTION fn_block_audit_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION '{IMMUTABLE_MSG}'; END; $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_immutable_decision_audit_log ON decision_audit_log",
    "CREATE TRIGGER trg_immutable_decision_audit_log BEFORE UPDATE OR DELETE ON decision_audit_log FOR EACH ROW EXECUTE FUNCTION fn_block_audit_mutation()",
    "DROP TRIGGER IF EXISTS trg_immutable_decision_audit_log_truncate ON decision_audit_log",
    "CREATE TRIGGER trg_immutable_decision_audit_log_truncate BEFORE TRUNCATE ON decision_audit_log FOR EACH STATEMENT EXECUTE FUNCTION fn_block_audit_mutation()",
]


# Input:  v (Any) – a column value as written or as read back
# What:   Normalises values so a hash computed at insert time equals the hash recomputed from the database later
#         (SQLite hands NUMERIC 0.0 back as int 0 and drops time zones, so every number becomes a 4-dp float, times UTC ISO)
# Output: Any – JSON-stable value
def _canon(v: Any) -> Any:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return round(float(v), 4)
    return v


# Input:  row (dict) – one decision row including prev_record_hash
# What:   SHA-256 over the previous hash plus the canonical JSON of the row, chaining every record to its predecessor
# Output: str – 64-char record hash
def record_hash(row: dict[str, Any]) -> str:
    body = json.dumps({k: _canon(v) for k, v in row.items() if k != "record_hash"}, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(row["prev_record_hash"] + body)


# Input:  row (Mapping) – database row
# What:   Converts UUIDs / datetimes into JSON-friendly strings for API responses and JSONL exports
# Output: dict
def jsonable(row: Any) -> dict[str, Any]:
    return {k: str(v) if isinstance(v, uuid.UUID) else _canon(v) if isinstance(v, datetime) else v for k, v in dict(row).items()}


class AuditStore:
    # Input:  url (str) – SQLAlchemy async URL (sqlite+aiosqlite or postgresql+asyncpg); engine_kw – extra engine options
    # What:   Owns the connection pool and every read/write of the four tables
    # Output: AuditStore
    def __init__(self, url: str, **engine_kw: Any):
        if (m := re.match(r"^sqlite\+aiosqlite:///(.+)$", url)) and m[1] != ":memory:":
            Path(m[1]).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, **engine_kw)
        self.is_pg, self._lock = self.engine.dialect.name == "postgresql", asyncio.Lock()
        if not self.is_pg:
            sa.event.listen(self.engine.sync_engine, "connect", _sqlite_fk_on)

    async def init(self, registry: dict[str, dict[str, Any]]) -> None:
        # Input:  registry (dict) – official articles from the KnowledgeBase
        # What:   Creates tables + immutability triggers (idempotent) and upserts the article registry (quarantine flags are kept)
        # Output: None
        async with self.engine.begin() as c:
            await c.run_sync(MD.create_all)
            for ddl in PG_DDL if self.is_pg else SQLITE_DDL:
                await c.exec_driver_sql(ddl)
            have = {r.doc_id: r for r in await c.execute(sa.select(kb_articles))}
            for d, r in registry.items():
                vals = {"article_title": r["title"], "doc_link": r["doc_link"], "content_sha256": r["content_sha256"], "category": r["category"]}
                if d not in have:
                    await c.execute(kb_articles.insert().values(doc_id=d, **vals))
                elif any(getattr(have[d], k) != v for k, v in vals.items()):
                    await c.execute(kb_articles.update().where(kb_articles.c.doc_id == d).values(**vals, indexed_at=_now()))

    async def quarantined_doc_ids(self) -> set[str]:
        # Input:  none
        # What:   Articles switched off by the incident runbook (is_official_approved = FALSE) – excluded from retrieval
        # Output: set[str]
        async with self.engine.connect() as c:
            return set((await c.execute(sa.select(kb_articles.c.doc_id).where(kb_articles.c.is_official_approved.is_(False)))).scalars())

    async def _upsert_ticket(self, c: AsyncConnection, st: Any, now: datetime) -> uuid.UUID:
        # Input:  c (AsyncConnection); st (TicketState); now (datetime)
        # What:   Inserts the sanitised ticket, or refreshes it when the same external reference is triaged again
        # Output: uuid.UUID – ticket_id
        vals = {k: getattr(st, k) for k in ("customer_id_hash", "customer_tier", "language_fluency", "sanitized_subject", "sanitized_body",
                                            "ingest_anomaly_flags", "pii_redacted_at_ingest", "pii_redaction_types", "priority_level", "priority_score")}
        vals |= {"channel": st.source_channel, "processed_at": now, "queue_status": "AUTO_RESOLVED" if st.routing_action in ANSWERED_ACTIONS else "QUEUED"}
        tid = (await c.execute(sa.select(tickets.c.ticket_id).where(tickets.c.external_ticket_ref == st.external_ticket_ref))).scalar()
        if tid:
            await c.execute(tickets.update().where(tickets.c.ticket_id == tid).values(**vals))
        else:
            tid = uuid.UUID(st.ticket_id) if st.ticket_id else uuid.uuid4()
            await c.execute(tickets.insert().values(ticket_id=tid, external_ticket_ref=st.external_ticket_ref, created_at=now, **vals))
        return tid

    async def record(self, st: Any) -> str:
        # Input:  st (TicketState) – final pipeline state
        # What:   Upserts the sanitised ticket, then appends exactly one immutable decision row whose hash links to the previous row
        #         (serialised in-process with a lock and across processes with a PostgreSQL advisory lock)
        # Output: str – decision_id (st.ticket_id is updated to the stored ticket id)
        r, now = st.validation_receipt or {}, _now()
        async with self._lock, self.engine.begin() as c:
            if self.is_pg:
                await c.exec_driver_sql("SELECT pg_advisory_xact_lock(424242)")
            tid = await self._upsert_ticket(c, st, now)
            registry = set((await c.execute(sa.select(kb_articles.c.doc_id))).scalars())
            prev = (await c.execute(sa.select(decisions.c.chain_seq, decisions.c.record_hash).order_by(decisions.c.chain_seq.desc()).limit(1))).first()
            row = {
                "decision_id": uuid.uuid4(), "chain_seq": prev[0] + 1 if prev else 1, "ticket_id": tid, "trace_id": st.trace_id,
                "predicted_intent": st.predicted_intent[:128], "predicted_urgency": st.predicted_urgency,
                "raw_classifier_confidence": round(st.raw_confidence, 4), "calibrated_confidence": round(st.calibrated_confidence, 4),
                "considered_alternatives": st.considered_alternatives, "calibration_version": st.calibration_version[:32],
                "is_mandatory_escalation": st.is_mandatory_escalation, "matched_semantic_title": st.semantic_matched_title,
                "retrieved_sources": [{k: p[k] for k in ("doc_id", "chunk_id", "section_heading", "rerank_score")} for p in st.retrieved_passages],
                "top_retrieval_score": round(st.top_retrieval_score, 4), "highest_rejected_score": round(st.highest_rejected_retrieval_score, 4),
                "routing_action": st.routing_action, "routing_reason": st.routing_reason_manager_text,
                "drafted_response": st.final_formatted_response or None,
                "cited_doc_id": next((d for d in st.cited_doc_ids if d in registry), None), "attached_doc_link": st.attached_doc_link,
                "escalation_summary_md": st.escalation_summary_markdown or None, "guardrail_passed": st.guardrail_passed,
                "validation_receipt_json": r, "pii_outbound_detected": bool(r.get("pii_found")),
                "nli_groundedness_score": r.get("nli_min_score"), "disclaimer_verified": bool(r.get("disclaimer_valid")),
                "citation_verified": bool(r.get("citation_valid")), "model_provider": st.model_provider[:64], "model_identifier": st.model_identifier[:128],
                "prompt_bundle_version": st.prompt_bundle_version, "end_to_end_latency_ms": st.end_to_end_latency_ms,
                "prompt_tokens": st.prompt_tokens, "completion_tokens": st.completion_tokens,
                "prev_record_hash": prev[1] if prev else GENESIS, "created_at": now,
            }
            row["record_hash"] = record_hash(row)
            await c.execute(decisions.insert().values(**row))
        st.ticket_id = str(tid)
        return str(row["decision_id"])

    async def verify_chain(self) -> bool:
        # Input:  none
        # What:   Recomputes every record hash in order and checks each link to its predecessor (any tampering breaks the chain)
        # Output: bool – True when the whole chain is intact
        async with self.engine.connect() as c:
            rows = (await c.execute(sa.select(decisions).order_by(decisions.c.chain_seq))).mappings().all()
        prev = GENESIS
        for row in map(dict, rows):
            if row["prev_record_hash"] != prev or record_hash(row) != row["record_hash"]:
                return False
            prev = row["record_hash"]
        return True

    async def queue_position(self, ticket_id: str) -> int:
        # Input:  ticket_id (str)
        # What:   1 + number of waiting tickets that outrank this one (higher priority, or equal priority but older) – FR-08
        # Output: int – queue position (>= 1)
        tid = uuid.UUID(ticket_id)
        async with self.engine.connect() as c:
            me = (await c.execute(sa.select(tickets.c.priority_score, tickets.c.created_at).where(tickets.c.ticket_id == tid))).first()
            if not me:
                return 1
            ahead = sa.or_(tickets.c.priority_score > me[0], sa.and_(tickets.c.priority_score == me[0], tickets.c.created_at < me[1]))
            q = sa.select(sa.func.count()).select_from(tickets).where(tickets.c.queue_status == "QUEUED", tickets.c.ticket_id != tid, ahead)
            return 1 + (await c.execute(q)).scalar_one()

    async def latest_decision(self, ticket_id: str) -> dict[str, Any] | None:
        # Input:  ticket_id (str)
        # What:   Latest decision for the ticket joined with its sanitised input – everything needed to reconstruct the decision
        # Output: dict | None
        cols = [tickets.c[k] for k in ("external_ticket_ref", "channel", "customer_tier", "language_fluency", "sanitized_subject",
                                       "sanitized_body", "ingest_anomaly_flags", "pii_redacted_at_ingest", "pii_redaction_types")]
        q = sa.select(decisions, *cols).join(tickets, tickets.c.ticket_id == decisions.c.ticket_id) \
            .where(decisions.c.ticket_id == uuid.UUID(ticket_id)).order_by(decisions.c.chain_seq.desc()).limit(1)
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
        return jsonable(row) if row else None

    async def add_feedback(self, ticket_id: str, fb: dict[str, Any]) -> str:
        # Input:  ticket_id (str); fb (dict) – validated FeedbackRequest fields
        # What:   Records an agent / technical-writer correction against a specific decision of that ticket
        # Output: str – feedback_id (raises LookupError when the decision does not belong to the ticket)
        tid, did = uuid.UUID(ticket_id), uuid.UUID(str(fb["decision_id"]))
        async with self.engine.begin() as c:
            ok = (await c.execute(sa.select(decisions.c.decision_id).where(decisions.c.decision_id == did, decisions.c.ticket_id == tid))).first()
            if not ok:
                raise LookupError("decision_id not found for this ticket")
            fid = uuid.uuid4()
            await c.execute(feedback.insert().values(feedback_id=fid, ticket_id=tid, created_at=_now(), **(fb | {"decision_id": did})))
        return str(fid)

    async def export(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        # Input:  trace_ids (list[str]) – decisions produced by one run
        # What:   Reads those audit rows back (in chain order) for the run's decision_audit_log.jsonl export
        # Output: list[dict]
        out: list[dict[str, Any]] = []
        async with self.engine.connect() as c:
            for i in range(0, len(trace_ids), 400):
                q = sa.select(decisions).where(decisions.c.trace_id.in_(trace_ids[i:i + 400])).order_by(decisions.c.chain_seq)
                out += [jsonable(r) for r in (await c.execute(q)).mappings()]
        return sorted(out, key=lambda r: r["chain_seq"])

    async def close(self) -> None:
        await self.engine.dispose()


# Input:  dbapi_conn – raw SQLite connection; _record – pool record (unused)
# What:   Turns on SQLite foreign-key enforcement for every new connection
# Output: None
def _sqlite_fk_on(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
