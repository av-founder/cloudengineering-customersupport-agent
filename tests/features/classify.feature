@component:classify @FR-04 @FR-06 @FR-08 @NFR-05 @NFR-06 @NFR-09
Feature: Calibrated Intent and Urgency Classification with Considered Alternatives
  As the Triage Engine
  I want to predict intent and urgency with a calibrated probability score, record considered alternatives, and safely fall back on ambiguity
  So that downstream routing decisions rest on reliable probabilities and every classification is reconstructable for compliance.

  Background:
    Given the Classify component is initialized with representative few-shot examples and temperature scaling calibration loaded from "artifacts/thresholds_v1.json"

  @behaviour:calibrated-prediction @FR-08 @NFR-05
  Scenario: Assigning intent, urgency, calibrated confidence, and considered alternatives to a standard ticket
    Given a normalized "TicketState" describing a container deployment failing with a 502 Bad Gateway error
    When the Classify component transitions state from "INGESTED_NORMALIZED" to "CLASSIFIED"
    Then "predicted_intent" is set to "deployment_failure" and "predicted_urgency" is set to "high"
    And "calibrated_confidence" is populated with a numeric score between 0.0000 and 1.0000 within 5 percentage points of empirical accuracy
    And "considered_alternatives" records at least the top 3 alternative intent categories considered with their probabilities

  @behaviour:mandatory-escalation-triage @FR-04
  Scenario Outline: Identifying sensitive security, compliance, billing, feature-request and unclear tickets for immediate human review
    Given a normalized "TicketState" containing customer phrasing related to "<customer_topic>"
    When the Classify component evaluates the ticket
    Then "predicted_intent" is set to the exact escalation intent "<matched_intent>"
    And "is_mandatory_escalation" is set to true
    And "considered_alternatives" is recorded in the decision state

    Examples:
      | customer_topic                                                  | matched_intent     |
      | unauthorized SSH login attempts and suspected token compromise  | security_incident  |
      | GDPR data residency requirements and EU-only storage guarantees | compliance_request |
      | unexpected charges on the monthly enterprise invoice            | billing_query      |
      | a roadmap request for staged rollouts                           | feature_request    |
      | it is not working please call us                                | unclear_request    |

  @behaviour:fallback-on-ambiguity @FR-04 @NFR-02
  Scenario: Returning a defined fallback rather than raising an exception when a ticket is unclassifiable or novel
    Given a normalized "TicketState" containing ambiguous, contradictory, empty, or novel zero-day failure text
    When the Classify component cannot map the ticket to a known intent category or outputs "I Don't Know"
    Then the Classify component does not raise a runtime exception
    And "predicted_intent" is set to the defined fallback "unclassified_fallback"
    And "calibrated_confidence" is set to 0.0000 (below the automated routing threshold)
    And "considered_alternatives" and the ambiguity reason are preserved for the audit log

  @behaviour:linguistic-fairness @FR-06 @NFR-06 @NFR-09
  Scenario: Maintaining classification accuracy and calibration parity for non-fluent English phrasing
    Given a normalized "TicketState" written in broken English using non-technical synonyms for a build dependency failure
    And "language_fluency" is tagged as "non_fluent"
    When the Classify component evaluates the ticket
    Then "predicted_intent" is accurately assigned to "deployment_failure"
    And "calibrated_confidence" exhibits less than a 0.05 variance compared to an equivalent "fluent" cohort ticket
