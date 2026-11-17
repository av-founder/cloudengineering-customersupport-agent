@component:route @FR-02 @FR-04 @FR-07 @FR-08 @NFR-01 @NFR-05
Feature: Deterministic Two-Stage Routing and Priority Queue Management
  As the Routing Decision Engine
  I want to deterministically decide whether to answer automatically or escalate to a human based on empirically calibrated thresholds loaded from "artifacts/thresholds_v1.json"
  So that high-risk or uncertain tickets are safely escalated with full context while solvable tickets are prioritized appropriately.

  Background:
    Given the Route component operates as a pure Python deterministic function with zero generative randomness
    And the routing thresholds are loaded as static runtime constants from "artifacts/thresholds_v1.json"

  @behaviour:auto-answer-routing @FR-02 @NFR-05
  Scenario: Routing a high-confidence, grounded technical ticket to automated drafting
    Given a "TicketState" classified with intent "deployment_failure" at "calibrated_confidence" 0.91 (above 0.82)
    And "retrieved_passages" contains official article "DOC-DEPLOY-001" with "rerank_score" 0.88 (above 0.75)
    When the Route component evaluates the post-retrieval state
    Then the state transitions to "ROUTED_TO_GENERATE"
    And "routing_reason_manager_text" records a plain-English explanation readable by a support manager stating both thresholds were satisfied

  @behaviour:deterministic-reproducibility @NFR-05
  Scenario: Guaranteeing identical routing decisions and explanations for identical inputs
    Given a fixed "TicketState" snapshot with identical classification confidence, intent, customer tier, and retrieval scores
    When the Route component evaluates the exact same "TicketState" across 100 independent invocations
    Then all 100 invocations yield the exact same "routing_action", "priority_score", and "routing_reason_manager_text"

  @behaviour:hard-escalation-bypass @FR-04 @FR-07 @DEP-01
  Scenario Outline: Immediately escalating sensitive intents at Stage 1 without attempting retrieval or auto-response
    Given a "TicketState" is classified with sensitive intent "<sensitive_intent>" at any confidence level
    When the Route component evaluates the Stage 1 pre-retrieval gate
    Then "routing_action" is immediately set to "ESCALATE_MANDATORY_INTENT"
    And the Generate component is strictly bypassed
    And "escalation_summary_markdown" is populated with the ticket context and policy bypass reason
    And "routing_reason_manager_text" records "<manager_reason>"

    Examples:
      | sensitive_intent   | manager_reason |
      | security_incident  | Escalated automatically to Tier-2 Specialist: Ticket involves restricted category 'security_incident' (Security, Compliance, or Billing). Auto-response strictly prohibited per policy FR-04. |
      | compliance_request | Escalated automatically to Tier-2 Specialist: Ticket involves restricted category 'compliance_request' (Security, Compliance, or Billing). Auto-response strictly prohibited per policy FR-04. |
      | billing_query      | Escalated automatically to Tier-2 Specialist: Ticket involves restricted category 'billing_query' (Security, Compliance, or Billing). Auto-response strictly prohibited per policy FR-04. |
      | feature_request    | Escalated automatically to Tier-1 human agent: Ticket category 'feature_request' (Feature Request or Unclear Request) cannot be resolved from official documentation. Auto-response strictly prohibited per policy FR-04. |
      | unclear_request    | Escalated automatically to Tier-1 human agent: Ticket category 'unclear_request' (Feature Request or Unclear Request) cannot be resolved from official documentation. Auto-response strictly prohibited per policy FR-04. |

  @behaviour:threshold-escalation-with-context @FR-07 @NFR-05
  Scenario Outline: Escalating tickets that fall below classification or retrieval thresholds with drafted summary and sources attached
    Given a "TicketState" has "calibrated_confidence" of <confidence> and top retrieval score of <retrieval_score>
    When the Route component evaluates the state
    Then "routing_action" is set to "<expected_action>"
    And "escalation_summary_markdown" contains a structured Markdown summary with Ticket Context, Attempted Docs, and Failure Reason
    And "routing_reason_manager_text" records why the threshold was not met in manager-readable language

    Examples:
      | confidence | retrieval_score | expected_action          |
      | 0.74       | 0.86            | ESCALATE_LOW_CONFIDENCE  |
      | 0.92       | 0.61            | ESCALATE_NO_GROUNDED_DOC |

  @behaviour:enterprise-priority-routing @FR-08 @NFR-01
  Scenario: Prioritizing Enterprise customer tier and high-urgency deployment failures to the top of the queue
    Given two tickets arrive simultaneously in the processing queue
    And Ticket A has "customer_tier" set to "standard" and "predicted_urgency" set to "low"
    And Ticket B has "customer_tier" set to "enterprise" and "predicted_urgency" set to "high"
    When the Route component computes queue priority via "FR-08" rules
    Then Ticket B is assigned "priority_level: critical", "priority_score: 95", and "queue_position: 1" ahead of Ticket A ("priority_score: 25")
