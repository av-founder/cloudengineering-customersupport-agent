@component:cross-cutting @NFR-02 @NFR-05 @NFR-06 @FR-09
Feature: Cross-Cutting Audit Immutability, Unattended Batch Evaluation, Provider Failover, and Knowledge Base Integrity
  As the Principal AI Architect and Compliance Officer
  I want every automated decision to be cryptographically reconstructable, unattended evaluation runs to process 100% of tickets and compute all Metrics.md figures in code, provider outages to fail over gracefully, and unofficial snippet files to be rejected at build time
  So that CloudServe passes its autumn compliance review, satisfies the hidden evaluation harness, and maintains 99.5%+ availability.

  @behaviour:unattended-end-to-end-harness @NFR-01 @NFR-02 @NFR-05
  Scenario: Executing a complete unattended evaluation run from a single command and computing all required Metrics.md figures by code
    Given an input file of N support tickets is supplied via "python -m evaluation.harness --input input_path --output output_path"
    And static routing thresholds are loaded from "artifacts/thresholds_v1.json"
    And the input batch includes normal answerable tickets, mandatory escalation tickets, malformed empty tickets, and tickets containing API keys
    When the evaluation harness runs to completion without human intervention, manual retries, or restarts
    Then every single ticket in the input file is processed with zero silently dropped records ("tickets_processed == answered_automatically + escalated")
    And every ticket produces a corresponding immutable entry in "decision_audit_log" ("decisions_logged == tickets_processed")
    And the harness programmatically writes "metrics_report.json" and "metrics_report.md" containing Volume, Business, Technical, and Governance figures

  @behaviour:immutable-audit-log @NFR-05
  Scenario: Reconstructing an automated decision and blocking any mutation of the audit log
    Given a ticket has completed processing through the LangGraph pipeline and written a record to "decision_audit_log"
    When a compliance auditor queries "/api/v1/governance/audit/{ticket_id}"
    Then the returned record contains the sanitized input, predicted intent, calibrated confidence, considered alternatives, retrieved sources, routing action, plain-English manager reason, and validation receipt
    And when a database user attempts an "UPDATE" or "DELETE" statement on "decision_audit_log"
    Then the database trigger "trg_immutable_decision_audit_log" aborts the transaction and raises a security violation exception

  @behaviour:provider-failover-resilience @NFR-02
  Scenario: Failing over automatically from Groq to OpenRouter when the primary model provider experiences an outage
    Given an Enterprise deployment failure ticket arrives during a primary provider ("Groq") HTTP 503 outage
    When the Classify or Generate component invokes the model client
    Then the Tenacity circuit breaker trips after 2 retries within 800ms and routes the request to the fallback provider ("OpenRouter")
    And the ticket completes processing within the 3.0-second p95 latency target without dropping the ticket

  @behaviour:ci-corpus-contamination-lock @FR-01 @FR-09
  Scenario: Failing the CI/CD build if historical tickets or private agent snippet files are added to the vector ingestion path
    Given an engineer attempts to index records from "development_tickets.json" or a private agent ".txt" snippet file into ChromaDB
    When the GitHub Actions CI pipeline runs the "kb_manifest.lock" SHA-256 verification check
    Then the ingestion pipeline halts with a fatal "FR-09_UNOFFICIAL_DATA_CONTAMINATION" error before the container image can be built
