"""Central configuration: environment settings, data-driven policy constants, prompt loader and the frozen threshold artifact."""
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ROOT / "prompts" / "build"


# Input:  data (bytes | str) – any content
# What:   Makes a tamper-evident SHA-256 fingerprint of the content
# Output: str – 64-character hex digest
def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


class Settings(BaseSettings):
    # Input:  environment variables or the git-ignored .env file
    # What:   Typed runtime configuration; secrets only come from the environment, relative paths are anchored at the project root
    # Output: Settings – shared by every component
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    primary_model: str = "openai/gpt-oss-120b"
    fallback_model: str = "openai/gpt-oss-120b"
    llm_reasoning_effort: Literal["", "low", "medium", "high"] = "low"
    llm_reasoning_headroom_tokens: int = 400
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    nli_model: str = "cross-encoder/nli-deberta-v3-small"
    kb_path: Path = Path("data/documentation.json")
    kb_manifest_path: Path = Path("kb_manifest.lock")
    chroma_path: Path = Path("storage/chroma")
    llm_cache_path: Path = Path("storage/llm_cache.sqlite")
    threshold_config_path: Path = Path("artifacts/thresholds_v1.json")
    database_url: str = "sqlite+aiosqlite:///storage/decisions.db"
    routing_mode: Literal["FULL_AUTONOMOUS", "ASSIST_AND_ESCALATE_ONLY", "PASSTHROUGH_QUEUE"] = "FULL_AUTONOMOUS"
    api_key: SecretStr = SecretStr("")
    customer_id_salt: SecretStr = SecretStr("")
    eval_input_path: str = ""
    eval_output_dir: str = ""
    llm_cache_enabled: bool = True
    llm_timeout_s: float = 20.0
    classify_samples: int = Field(3, ge=1, le=5)
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    @field_validator("kb_path", "kb_manifest_path", "chroma_path", "llm_cache_path", "threshold_config_path")
    @classmethod
    def _anchor(cls, p: Path) -> Path:
        # Input:  p (Path) – possibly relative path from the environment
        # What:   Resolves it against the project root so commands work from any working directory
        # Output: Path – absolute path
        return p if p.is_absolute() else (ROOT / p).resolve()

    @field_validator("customer_id_salt")
    @classmethod
    def _reject_placeholder_salt(cls, v: SecretStr) -> SecretStr:
        # Input:  v (SecretStr) – CUSTOMER_ID_SALT from the environment
        # What:   Refuses the documented placeholder so a shipped example value can never become the live pepper
        # Output: SecretStr
        if v.get_secret_value() == "change_me_to_a_long_random_string":
            raise ValueError("CUSTOMER_ID_SALT must not be the documented placeholder; generate a long random value")
        return v

    @model_validator(mode="after")
    def _require_production_secrets(self) -> "Settings":
        # Input:  self – validated settings
        # What:   Fails closed in production: API authentication and the keyed customer-ID salt are mandatory
        # Output: Settings
        if self.app_env == "production":
            if len(self.customer_id_salt.get_secret_value()) < 16:
                raise ValueError("CUSTOMER_ID_SALT must be at least 16 characters when APP_ENV=production")
            if len(self.api_key.get_secret_value()) < 16:
                raise ValueError("API_KEY must be at least 16 characters when APP_ENV=production")
        return self

    @property
    def async_db_url(self) -> str:
        # Input:  DATABASE_URL in sync or async form (sqlite / postgresql)
        # What:   Switches to the async drivers (aiosqlite / asyncpg) and anchors relative SQLite files at the project root
        # Output: str – SQLAlchemy async database URL
        u = re.sub(r"^sqlite(\+\w+)?://", "sqlite+aiosqlite://", self.database_url)
        u = re.sub(r"^postgres(?:ql)?(\+\w+)?://", "postgresql+asyncpg://", u)
        if (m := re.match(r"^sqlite\+aiosqlite:///(?!:memory:)(.+)$", u)) and not Path(m[1]).is_absolute():
            u = f"sqlite+aiosqlite:///{(ROOT / m[1]).resolve().as_posix()}"
        return u


# Input:  none
# What:   Builds the settings once per process (cached)
# Output: Settings
@lru_cache
def get_settings() -> Settings:
    return Settings()


# Input:  none
# What:   One logging format for every entry point; silences per-request chatter from HTTP clients and model loaders
# Output: None
def setup_logging() -> None:
    import logging
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpx2", "sentence_transformers", "huggingface_hub", "chromadb"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)  # language-pack notices for unused recognizers


# ---------------------------------------------------------------------------------------------------------------------
# Policy constants. Where the spec and the real data disagree, the real data wins (approved decision).
# ---------------------------------------------------------------------------------------------------------------------

# Input:  none – taxonomy taken from data/development_tickets.json (22 labels)
# What:   Every intent the classifier may output, with the plain-English meaning shown to the LLM (FR-04)
# Output: dict[str, str] – intent -> description
INTENTS: dict[str, str] = {
    "account_access": "team members, roles and permissions: a user cannot see a project they were granted, removing a departing member's access",
    "api_key_issue": "API keys: a key suddenly returns 401, writes denied by the key's scope, replacing a leaked key without downtime",
    "api_usage_question": "using the API: pagination and cursors, duplicate or missing records while paging, page-size limits, very large extracts",
    "authentication_failure": "signing in: 'invalid credentials' with a correct password, console lock-out, authenticator / MFA codes rejected",
    "billing_query": "invoices and charges: a higher invoice, unknown charges, billing period vs calendar month, plan changes and proration",
    "compliance_request": "audit or compliance evidence: access records for auditors, proving access is logged, retention required by policy",
    "configuration_help": "environment variables or secrets not picked up, config values leaking between environments, where to store secrets",
    "data_export": "exporting account data: an export stuck in the queue, records missing from an export snapshot, scheduled extracts",
    "data_residency": "where data is physically stored: regions, keeping data inside the EU, backups replicated outside the primary region",
    "database_issue": "managed database: connection pool exhaustion, slow or stalled restores, backups and point-in-time restore",
    "deployment_failure": "deployments or builds failing: health-check timeouts, deploy rolls back after reaching running, dependency resolution errors",
    "feature_request": "asking for a capability that does not exist yet (roadmap questions, 'it would be useful if', 'are you considering')",
    "integration_help": "CI pipelines and third-party tools: log forwarding stopped, monitoring metrics lagging, connecting external systems",
    "onboarding": "getting started: where to begin after sign-up, a first deployment, migrating an existing application to CloudServe",
    "performance_degradation": "slowness: latency climbing, p95 response times up, autoscaling behaviour and instance limits",
    "quota_or_overage": "usage limits and spend: throttled after a traffic increase, a spend cap stopped a workload, unexpected overage charges",
    "rate_limit": "HTTP 429 / API rate limiting without a traffic increase, some endpoints limited differently, throughput capped",
    "rollback_request": "reverting a bad release: getting back to the previous version or revision quickly",
    "security_incident": "suspected compromise: unknown activity in the audit log, a former employee still has access, credentials exposed in a breach",
    "sso_configuration": "single sign-on / SAML: bounced to an error page after the identity provider, works for one group only, users not provisioned",
    "unclear_request": "too vague to tell what the problem is, or a follow-up without details ('it is not working', 'any update?', 'call us')",
    "webhook_issue": "webhooks: endpoint stopped receiving events, retries and duplicate deliveries, signature verification failures",
}
FALLBACK_INTENT, IDK = "unclassified_fallback", "I Don't Know"

# Input:  none – approved policy: these five intents always go to a human (must_not_auto_respond)
# What:   Splits them by the human queue that owns them (Tier-2 specialists vs Tier-1 agents)
# Output: frozenset[str] groups
MANDATORY_TIER2 = frozenset({"security_incident", "compliance_request", "billing_query"})
MANDATORY_TIER1 = frozenset({"feature_request", "unclear_request"})
MANDATORY_INTENTS = MANDATORY_TIER2 | MANDATORY_TIER1

# Input:  none – spec keyword trapdoor minus 'data residency' (an answerable intent in the real data)
# What:   Deterministic safety net that forces a human review even if the LLM misclassifies the ticket
# Output: dict[str, re.Pattern] – policy category -> pattern
TRAPDOOR: dict[str, re.Pattern] = {
    "compliance_request": re.compile(r"(?i)\b(gdpr|soc\s?2|hipaa)\b"),
    "security_incident": re.compile(r"(?i)\b(security\s+breach|unauthori[sz]ed\s+access|compromised\s+(?:api\s+)?keys?)\b"),
    "billing_query": re.compile(r"(?i)\b(invoice\s+dispute|overcharged?|refund\s+request|sla\s+credit)\b"),
}

# Input:  none – enums observed in the real data (spec aliases kept for compatibility)
# What:   Canonical channels, tiers, cohorts, urgency and priority levels
# Output: tuples / dict used for validation and defaults
CHANNELS = ("email", "chat", "forum", "docs_comment")
CHANNEL_ALIASES = {"live_chat": "chat", "doc_comments": "docs_comment", "doc_comment": "docs_comment", "docs_comments": "docs_comment", "web_form": "docs_comment"}
TIERS = ("standard", "business", "enterprise")
FLUENCY = ("fluent", "non_fluent", "unknown")
URGENCY = ("low", "medium", "high")
PRIORITY_LEVELS = ("low", "medium", "high", "critical")
EMPTY_BODY = "[EMPTY_BODY]"

# Input:  none – spec routing actions and FR-08 priority rules
# What:   Final actions, which of them count as "answered", and queue priority scores
# Output: tuples / dict
ROUTING_ACTIONS = ("AUTO_REPLY", "TIER1_DRAFT_ASSIST", "ESCALATE_MANDATORY_INTENT", "ESCALATE_LOW_CONFIDENCE", "ESCALATE_NO_GROUNDED_DOC", "BLOCK_SAFETY_GUARDRAIL_FAIL")
ANSWERED_ACTIONS = frozenset({"AUTO_REPLY", "TIER1_DRAFT_ASSIST"})
PRIORITY_SCORES = {"enterprise_high": 95, "enterprise_other": 85, "high": 85, "medium": 50, "low": 25}
GUARDRAIL_CHECKS = ("outbound_pii_or_secret_block", "nli_hallucination_entailment_block", "citation_integrity_block", "disclaimer_or_tone_block")

# Input:  none – spec manager_reason_templates plus variants needed by the data-driven policy
# What:   Plain-English explanations a support manager or auditor can read for every routing outcome
# Output: dict[str, str] – str.format templates
REASONS = {
    "AUTO_REPLY": "Answered automatically: Intent '{intent}' classified with high calibrated confidence ({conf:.2f} >= {tau_class:.2f}), grounded in official article [{doc_id}: {title}] (relevance {ret:.2f} >= {tau_ret:.2f}), and passed all safety guardrails.",
    "TIER1_DRAFT_ASSIST": "Draft prepared for Tier-1 review (assist-only mode, outward auto-reply disabled): Intent '{intent}' classified with high calibrated confidence ({conf:.2f} >= {tau_class:.2f}), grounded in official article [{doc_id}: {title}] (relevance {ret:.2f} >= {tau_ret:.2f}), and passed all safety guardrails.",
    "MANDATORY_TIER2": "Escalated automatically to Tier-2 Specialist: Ticket involves restricted category '{intent}' (Security, Compliance, or Billing). Auto-response strictly prohibited per policy FR-04.",
    "MANDATORY_TIER1": "Escalated automatically to Tier-1 human agent: Ticket category '{intent}' (Feature Request or Unclear Request) cannot be resolved from official documentation. Auto-response strictly prohibited per policy FR-04.",
    "ESCALATE_LOW_CONFIDENCE": "Escalated to human agent with context: Intent classification confidence ({conf:.2f}) fell below the {tau_class:.2f} empirical safety threshold (top candidate: '{intent}').",
    "ESCALATE_NO_GROUNDED_DOC": "Escalated to human agent with context: No official knowledge base article met the {tau_ret:.2f} grounding threshold (highest candidate score: {max_rejected:.2f}).",
    "NO_TITLE": "Escalated to human agent with context: The semantic title matcher (FR-06) could not map the ticket to any official article title ('I Don't Know').",
    "ABSTAINED": "Escalated to human agent with context: The drafting model abstained ('I Don't Know') because the retrieved official articles do not state the specific answer requested.",
    "BLOCK_SAFETY_GUARDRAIL_FAIL": "Blocked and escalated to human agent: Pre-release safety guardrail '{failed_check}' blocked the candidate draft ({failure_detail}).",
    "PASSTHROUGH": "Queued directly for a human agent: PASSTHROUGH_QUEUE incident mode is active, so no model was consulted (deterministic FR-08 priority only).",
    "ERROR": "Escalated to human agent with context: An internal processing error occurred at step '{node}', so the ticket was routed to a human instead of risking an unverified reply.",
}

# Input:  none – spec FR-02/03/05 formatter contract and tone policy
# What:   Mandatory disclaimer, citation footer, link pattern (the data has no links), banned tone and commitments
# Output: str / tuple / re.Pattern constants
DISCLAIMER = "[Automated Support Assistant] This message was drafted automatically by CloudServe's AI Support Assistant using our official documentation."
CITATION = "\n\nReference: [{doc_id}: {article_title}]({doc_link})"
DOC_LINK = "https://docs.cloudserve.io/kb/{doc_id}"
PROHIBITED_PHRASES = ("obviously you should", "as i already told you", "read the manual", "user error")
COMMITMENT_RX = re.compile(
    r"(?i)\brefund\s+(?:has\s+been|was|is\s+being|will\s+be)\s+(?:issued|processed|approved|credited)"
    r"|\b(?:has|have)\s+been\s+credited\b|\bcredit\s+(?:has\s+been|was)\s+applied\b"
    r"|\bfixed\s+on\s+our\s+(?:side|end)\b|\bfix\s+will\s+be\s+(?:released|deployed|available|shipped)\s+(?:by|on)\b"
)

# Input:  none – spec retrieval contract (RETRIEVE component)
# What:   Knowledge-base isolation, chunking and hybrid-search parameters
# Output: constants
KB_COLLECTION, KB_ORIGIN, KB_SOURCE_FILE, KB_ARTICLE_COUNT = "cloudserve_official_kb_v1", "OFFICIAL_KB_ONLY", "documentation.json", 29
HEADER_PREFIX = "[Doc ID: {doc_id} | Article: {article_title} | Section: {section_heading}]\n"
CHUNK_MAX_TOKENS, CHUNK_OVERLAP = 512, 64
DENSE_K, BM25_K, RRF_K, TITLE_BOOST, RERANK_KEEP, CONTEXT_DOCS = 10, 10, 60, 0.015, 3, 2
RERANK_CANDIDATES = 12  # fused candidates scored by the cross-encoder (+ the FR-06 article's sections); same top-3 hits as scoring all

# Input:  none – spec LLM settings per prompt role
# What:   Sampling temperatures and output budgets
# Output: constants
CLASSIFY_TEMPERATURE, CLASSIFY_MAX_TOKENS = 0.15, 300
TITLE_MAX_TOKENS, GENERATE_MAX_TOKENS = 120, 600

# Input:  none – spec calibration contract
# What:   Datasets the calibrator must never read, plus the threshold search space and hard constraints
# Output: constants
FORBIDDEN_DATASETS = ("validation_tickets.json",)
GRID_MIN, GRID_MAX, GRID_STEP = 0.50, 0.95, 0.01
HARD_CONSTRAINTS = {"min_intent_precision": 0.85, "max_hallucination_rate": 0.05, "max_ece": 0.05, "max_cohort_quality_delta": 0.05}
TARGET_MAX_ESCALATION = 0.30
RELAX_TOLERANCE = 0.02  # a bound no threshold pair can meet on the dev labels is relaxed to "best achievable + 2 pp" (and reported)
FCR_TOLERANCE = 0.01  # among points within 1 pp of the best FCR, the most conservative thresholds win (robust to a few lucky tickets)


_TYPO = str.maketrans({**dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d", "-"), **dict.fromkeys("\u2018\u2019\u201a\u201b\u2032", "'"),
                       **dict.fromkeys("\u201c\u201d\u201e\u2033", '"'), **dict.fromkeys("\u00a0\u2007\u2009\u202f", " "), "\u2026": "..."})


# Input:  s (str) – model-written text
# What:   Replaces typographic dashes, quotes, special spaces and ellipses with plain ASCII. Models emit them, and the NLI
#         tokenizer mishandles them (a non-breaking hyphen dropped entailment of an identical sentence from 0.98 to 0.07)
# Output: str
def plain_typography(s: str) -> str:
    return s.translate(_TYPO)


_STOP = frozenset("a an the and or but if of to in on at for with by from is are was were be been it its this that these those i we you our your "
                  "my me us they them can could should would will do does did not no have has had as so than then there here what when how why".split())


# Input:  s (str)
# What:   Lower-cased content words (stop words removed), shared by BM25 search and evidence matching
# Output: list[str]
def terms(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP]


# ---------------------------------------------------------------------------------------------------------------------
# Prompt register
# ---------------------------------------------------------------------------------------------------------------------

# Input:  name (str) – prompt file stem, e.g. "FR-04_triage_classifier"; folder (str) – "build" or "evaluation"
# What:   Reads a versioned prompt file and splits its front matter (id, version, role, temperature) from the body
# Output: tuple[dict[str, str], Template] – metadata and a $placeholder template
@lru_cache
def load_prompt(name: str, folder: str = "build") -> tuple[dict[str, str], Template]:
    _, head, body = (PROMPTS_DIR.parent / folder / f"{name}.md").read_text(encoding="utf-8").split("---", 2)
    meta = {k.strip(): v.strip() for k, v in (ln.split(":", 1) for ln in head.strip().splitlines() if ":" in ln)}
    return meta, Template(body.strip())


# Input:  none
# What:   Fingerprints every build prompt so each logged decision records exactly which prompt texts produced it
# Output: str – e.g. "pb-1a2b3c4d5e6f"
@lru_cache
def prompt_bundle_version() -> str:
    return "pb-" + sha256("".join(p.read_text(encoding="utf-8") for p in sorted(PROMPTS_DIR.glob("*")) if p.is_file()))[:12]


# ---------------------------------------------------------------------------------------------------------------------
# Frozen calibration artifact (artifacts/thresholds_v1.json) – the ONLY source of runtime thresholds
# ---------------------------------------------------------------------------------------------------------------------

class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlattScaling(_Frozen):
    # What: P(correct) = 1 / (1 + exp(-(a*z + b) / T)) parameters fitted offline
    temperature_T: float = Field(gt=0)
    slope_a: float
    intercept_b: float


class RoutingThresholds(_Frozen):
    # What: every runtime threshold, read-only after start-up
    classification_confidence_min: float = Field(ge=0, le=1.01)
    retrieval_relevance_min: float = Field(ge=0, le=1)
    nli_entailment_min: float = Field(ge=0, le=1)
    nli_contradiction_max: float = Field(ge=0, le=1)


class ThresholdArtifact(_Frozen):
    # What: full calibration artifact schema (spec threshold_artifact.schema)
    model_config = ConfigDict(frozen=True, extra="allow")
    calibration_version: str = Field(max_length=32)
    fitted_timestamp_utc: str
    dev_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_identifier_calibrated: str
    platt_scaling: PlattScaling
    routing_thresholds: RoutingThresholds
    empirical_dev_metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)
    tradeoff_justification_summary: str = ""


class ThresholdConfigError(RuntimeError):
    """Raised when the calibration artifact is missing or invalid – thresholds are never hardcoded."""


# Input:  path (str | None) – explicit artifact path; defaults to THRESHOLD_CONFIG_PATH / artifacts/thresholds_v1.json
# What:   Loads and validates the calibration artifact once; refuses to start without it
# Output: ThresholdArtifact – immutable thresholds and Platt parameters
@lru_cache
def load_thresholds(path: str | None = None) -> ThresholdArtifact:
    p = Path(path).resolve() if path else get_settings().threshold_config_path
    if not p.is_file():
        raise ThresholdConfigError(f"Calibration artifact not found: {p}. Create it with: python -m evaluation.calibrate (see README).")
    try:
        return ThresholdArtifact.model_validate_json(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ThresholdConfigError(f"Calibration artifact {p} is invalid: {e}") from e
