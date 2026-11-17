"""GENERATE (FR-01 / FR-02 / FR-09) + deterministic ResponseFormatter (DEP-03: FR-02 link, FR-03 citation, FR-05 disclaimer)."""
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.config import CONTEXT_DOCS, DISCLAIMER, GENERATE_MAX_TOKENS, IDK, load_prompt, plain_typography
from src.llm import LLMClient, usage_update


class GeneratorLLMOutput(BaseModel):
    # What: the only shape the drafting model may return; the envelope (disclaimer, footer, link) is added by Python
    abstain: bool = False
    draft_body: str = ""
    cited_doc_ids: list[str] = Field(default_factory=list)
    claim_to_doc_map: list[dict[str, Any]] = Field(default_factory=list)


# Input:  none
# What:   Lines the model must not write itself (references or the disclaimer are appended deterministically)
# Output: re.Pattern
_ENVELOPE = re.compile(r"(?im)^\s*(?:references?|sources?)\s*:.*$|\[automated support assistant\][^\n]*")

# Input:  none
# What:   Markup a rendering UI could execute or use to exfiltrate data: scripts/styles, HTML tags, images, model links
# Output: re.Pattern constants
_SCRIPT_STYLE = re.compile(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>")
_HTML = re.compile(
    r"(?is)<!--.*?-->|<![^>]*>|<\?[^>]*\?>|</?(?:a|abbr|article|aside|audio|b|base|bdi|bdo|blockquote|body|br|button|canvas|"
    r"caption|code|col|colgroup|data|datalist|dd|del|details|dfn|dialog|div|dl|dt|em|embed|fieldset|figcaption|figure|footer|"
    r"form|h[1-6]|head|header|hr|html|i|iframe|img|input|ins|kbd|label|legend|li|link|main|map|mark|math|menu|meta|meter|nav|"
    r"noscript|object|ol|optgroup|option|output|p|param|picture|pre|progress|q|rp|rt|ruby|s|samp|script|section|select|slot|"
    r"small|source|span|strong|style|sub|summary|sup|svg|table|tbody|td|template|textarea|tfoot|th|thead|time|title|tr|track|"
    r"u|ul|var|video|wbr)\b[^>]*>")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


# Input:  body (str) – model-written draft
# What:   Strips raw HTML, scripts, images and model-supplied link targets, leaving plain text / safe markdown so a UI cannot
#         execute markup or leak data through a crafted URL. The trusted citation footer is appended afterwards by Python.
# Output: str – sanitised draft body
def sanitize_draft_markdown(body: str) -> str:
    s = _SCRIPT_STYLE.sub("", body or "")
    s = _HTML.sub("", s)
    s = _MD_IMAGE.sub("", s)
    return _MD_LINK.sub(r"\1", s)


# Input:  kb (KnowledgeBase); passages (list[dict]) – retrieved passages >= tau_ret, best first
# What:   Expands the top passages to their full parent articles (at most 2; each is under 1.4K characters) so resolution steps
#         are never cut off, wrapped in <official_kb_context>
# Output: str – XML context block
def kb_context(kb: Any, passages: list[dict[str, Any]]) -> str:
    ids = list(dict.fromkeys(p["doc_id"] for p in passages))[:CONTEXT_DOCS]
    docs = "\n".join(f'<doc id="{d}" title="{kb.registry[d]["title"]}">\n{kb.registry[d]["content"]}\n</doc>' for d in ids)
    return f"<official_kb_context>\n{docs}\n</official_kb_context>"


# Input:  body (str) – grounded draft text; cited (list[str]) – doc ids; kb (KnowledgeBase)
# What:   Deterministically prepends the exact FR-05 disclaimer, appends one exact FR-03 reference per official cited doc and
#         fills the FR-02 Tier-1 UI payload with the clickable link
# Output: dict – final_formatted_response, attached_doc_link, ticket_ui_assist_payload
def format_response(body: str, cited: list[str], kb: Any) -> dict[str, Any]:
    refs = [d for d in cited if d in kb.registry]
    final = f"{DISCLAIMER}\n\n{sanitize_draft_markdown(body).strip()}" + "".join(kb.citation(d) for d in refs)
    top = kb.registry[refs[0]] if refs else {}
    return {"final_formatted_response": final, "attached_doc_link": top.get("doc_link"),
            "ticket_ui_assist_payload": {"draft_response": final, "doc_id": top.get("doc_id"), "article_title": top.get("title"), "doc_link": top.get("doc_link")}}


# Input:  st (TicketState) – routed to GENERATE; llm (LLMClient); kb (KnowledgeBase)
# What:   Temperature-0 grounded drafting with customer text sandboxed in <untrusted_customer_ticket>; enforces the
#         GeneratorLLMOutput schema. "I Don't Know" or a malformed reply becomes an abstention (safe escalation).
# Output: dict – TicketState updates
async def generate_draft(st: Any, *, llm: LLMClient, kb: Any) -> dict[str, Any]:
    meta, tpl = load_prompt("FR-01_grounded_generator")
    msgs = [{"role": "system", "content": tpl.safe_substitute()},
            {"role": "user", "content": f"{kb_context(kb, st.retrieved_passages)}\n\n{st.ticket_block()}"}]
    res = await llm.complete_json(msgs, temperature=float(meta.get("temperature", 0)), max_tokens=int(meta.get("max_tokens", GENERATE_MAX_TOKENS)))
    try:
        out = GeneratorLLMOutput.model_validate(res.data)
    except ValidationError:
        out = GeneratorLLMOutput(abstain=True, draft_body=IDK)
    body = sanitize_draft_markdown(plain_typography(_ENVELOPE.sub("", out.draft_body))).strip()
    abstain = out.abstain or not body or body.rstrip(". ").lower() == IDK.lower()
    upd = usage_update(st, [res]) | {"abstained": abstain, "llm_raw_draft_body": IDK if abstain else body, "claim_to_doc_map": out.claim_to_doc_map,
                                     "cited_doc_ids": [] if abstain else list(dict.fromkeys(map(str, out.cited_doc_ids)))}
    if abstain:
        return upd | {"pending_action": "ESCALATE_NO_GROUNDED_DOC", "pending_reason": "abstained"}
    return upd | format_response(body, upd["cited_doc_ids"], kb)
