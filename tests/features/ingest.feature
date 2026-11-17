@component:ingest @NFR-01 @NFR-02 @NFR-04 @NFR-08
Feature: Multi-Channel Ticket Ingestion, Canonical Normalization, and Perimeter Sanitization
  As the Support Automation Platform
  I want to ingest support tickets from email, live chat, documentation comments, and the community forum into a single canonical representation
  So that channel-specific quirks never leak downstream and malformed or sensitive inputs are safely handled at the perimeter.

  Background:
    Given the Ingest Service is online and accepting payloads across all four supported channels
    And the Pre-Persistence PII and Secret Scrubber is active at the ingestion boundary

  @behaviour:channel-normalization @NFR-08
  Scenario Outline: Normalizing heterogeneous tickets from all four intake channels into one internal schema
    Given a customer submits a support request via the "<source_channel>" channel containing "<channel_specific_quirk>"
    When the Ingest component transitions state from "RAW_INGEST" to "INGESTED_NORMALIZED"
    Then a single canonical "TicketState" representation is produced
    And the canonical state preserves the exact original text in "raw_body_preserved"
    And the canonical state explicitly records "<canonical_channel>" in "source_channel"
    And "<channel_specific_quirk>" is stripped from "sanitized_body" passed to downstream components

    Examples:
      | source_channel | canonical_channel | channel_specific_quirk                                      |
      | email          | email             | HTML wrapper tags, MIME headers, and reply-chain signatures |
      | live_chat      | chat              | fragmented multi-message timestamps and typing indicators   |
      | docs_comment   | docs_comment      | inline markdown anchor metadata and page widget breadcrumbs |
      | forum          | forum             | BBCode quote blocks and forum thread pagination metadata    |

  @behaviour:fault-tolerance @NFR-02
  Scenario Outline: Gracefully handling missing fields, unusual characters, and empty bodies without failing
    Given an incoming ticket arrives from "<source_channel>" with "<payload_anomaly>"
    When the Ingest component processes the incoming payload
    Then the system does not raise an unhandled exception or return an HTTP 5xx error
    And a valid "TicketState" representation is created with deterministic default fallbacks
    And "ingest_anomaly_flags" contains "<expected_anomaly_flag>" for downstream routing

    Examples:
      | source_channel | payload_anomaly                                               | expected_anomaly_flag       |
      | email          | an empty body and a populated subject line                    | BODY_FALLBACK_TO_SUBJECT    |
      | live_chat      | a completely empty body and missing subject field             | EMPTY_PAYLOAD_FLAGGED       |
      | forum          | null bytes, zero-width joiners, and malformed UTF-8 sequences | UNICODE_SANITIZED           |
      | docs_comment   | missing customer tier and missing urgency metadata fields     | METADATA_DEFAULTED_STANDARD |

  @behaviour:perimeter-sanitization @NFR-04 @DEP-02
  Scenario: Redacting secrets and private data at ingestion before persistence or model invocation
    Given an incoming ticket contains a live CloudServe key "cs_live_98a7bc65d4e3f2a1b0", an AWS key, and a personal email address
    When the Ingest component normalizes the ticket
    Then "sanitized_body" has all secrets and private identifiers replaced with "<REDACTED_API_KEY>" and "<REDACTED_EMAIL>"
    And no raw secret or personal identifier is transmitted to the Classify or Retrieve layers
    And "pii_redacted_at_ingest" is set to true with "pii_redaction_types" populated in the audit state
