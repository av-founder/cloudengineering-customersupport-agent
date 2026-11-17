@component:validate @FR-01 @FR-03 @FR-05 @NFR-03 @NFR-04 @NFR-05
Feature: Mandatory Synchronous Pre-Release Guardrail Validation and Blocking
  As the Safety and Governance Gatekeeper
  I want to validate every generated response in production for private data, unsupported claims, citation integrity, and disclaimer compliance before release
  So that unsafe or ungrounded drafts are deterministically blocked and escalated, and every check is recorded in the immutable audit log.

  Background:
    Given the Validate component is compiled as a non-bypassable synchronous node prior to response release
    And all four validation checks (Outbound PII, NLI Entailment, Citation Resolution, Disclaimer/Tone) are active

  @behaviour:validation-pass-and-audit @FR-03 @FR-05 @NFR-05
  Scenario: Approving a compliant, grounded response and recording a complete validation receipt
    Given a candidate "final_formatted_response" begins with the mandatory "[Automated Support Assistant]" disclaimer
    And the candidate response contains zero PII entities or credential patterns
    And every claim sentence achieves an NLI entailment score >= 0.85 against retrieved passage "DOC-DEPLOY-001"
    And the trailing citation resolves to "DOC-DEPLOY-001" in "kb_articles_registry"
    When the Validate component inspects the candidate response
    Then "validation_receipt.passed" is set to true
    And the state transitions to "RELEASE_APPROVED_RESPONSE"
    And "decision_audit_log" records the complete "validation_receipt" showing all four checks ran and passed

  @behaviour:block-on-pii-leakage @NFR-04 @NFR-05
  Scenario: Deterministically blocking an outbound response that contains private data or credentials
    Given a candidate draft response inadvertently contains an internal IPv4 address, customer email, or API token string
    When the Validate component executes Check 1 (Outbound PII and Secret Check)
    Then "validation_receipt.passed" is set to false and "validation_receipt.pii_found" is set to true
    And the Validate component blocks the response from outbound release
    And "routing_action" transitions to "BLOCK_SAFETY_GUARDRAIL_FAIL" with an "FR-07" escalation summary for human review
    And "decision_audit_log" records the blocked entity type and guardrail trigger reason

  @behaviour:block-on-hallucination @FR-01 @FR-03 @NFR-03 @NFR-05
  Scenario: Deterministically blocking a fluent draft that makes claims unsupported by the cited documentation
    Given a candidate draft response includes a CLI flag "--force-reset-nat" that does not appear in the retrieved "DOC-DEPLOY-001" passage
    When the Validate component executes Check 2 (NLI Entailment Check) and scores the sentence at 0.31 (below 0.85)
    Then "validation_receipt.passed" is set to false and "validation_receipt.nli_min_score" is recorded as 0.31
    And the Validate component blocks the response from release and transitions to "BLOCK_SAFETY_GUARDRAIL_FAIL"
    And the unsupported sentence is flagged in the escalation summary for the human agent

  @behaviour:block-on-missing-disclaimer-or-unverified-citation @FR-03 @FR-05 @NFR-05
  Scenario: Deterministically blocking a draft that omits the machine disclaimer or cites an unretrieved document
    Given a candidate draft response is missing the leading "[Automated Support Assistant]" disclaimer or cites a "doc_id" not in "retrieved_passages"
    When the Validate component executes Check 3 and Check 4
    Then "validation_receipt.passed" is set to false
    And the response is blocked and routed to "BLOCK_SAFETY_GUARDRAIL_FAIL"
    And the exact structural failure is persisted in "decision_audit_log"
