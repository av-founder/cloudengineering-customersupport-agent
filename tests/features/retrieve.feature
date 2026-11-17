@component:retrieve @FR-01 @FR-03 @FR-06 @FR-09 @NFR-06 @NFR-09
Feature: Grounded Hybrid Knowledge Base Retrieval with Strict Relevance Cutoff
  As the Knowledge Retrieval Engine
  I want to search exclusively across the 29 official CloudServe documentation articles and apply the calibrated relevance threshold
  So that downstream generation is grounded in verifiable corpus identifiers and irrelevant passages are suppressed.

  Background:
    Given ChromaDB collection "cloudserve_official_kb_v1" is indexed exclusively with the 29 official articles from "documentation.json" using 512-token heading-aware chunks
    And historical ticket resolutions and private agent snippet files are strictly excluded from the index
    And the minimum cross-encoder relevance threshold is loaded from "artifacts/thresholds_v1.json"

  @behaviour:verifiable-ranked-retrieval @FR-01 @FR-03
  Scenario: Returning ranked passages with verifiable corpus identifiers for an answerable query
    Given a classified "TicketState" asking why a container deployment rolls back after health check timeouts
    When the Retrieve component transitions state from "PRE_ROUTED_FOR_RETRIEVAL" to "RETRIEVED"
    Then "retrieved_passages" contains a non-empty list ordered by descending "rerank_score"
    And every passage in "retrieved_passages" has a "rerank_score" greater than or equal to 0.75
    And every passage contains verifiable "doc_id", "article_title", "section_heading", and "doc_link" resolving to "kb_articles_registry"

  @behaviour:semantic-bridging @FR-06 @NFR-06 @NFR-09
  Scenario: Bridging non-technical synonyms and broken English to official documentation titles
    Given a classified "TicketState" from a "non_fluent" speaker asking "how to get all data? page size big is reject. small is very slow."
    When the Retrieve component runs "FR-06" semantic title matching and hybrid search
    Then "semantic_matched_title" resolves to the official "Pagination and handling large result sets" article title
    And "retrieved_passages" returns the grounded official "DOC-API-002" passages with "rerank_score" >= 0.75

  @behaviour:relevance-threshold-cutoff @FR-01 @NFR-03
  Scenario: Returning nothing rather than something irrelevant when candidate passages fall below threshold
    Given a classified "TicketState" asking about a newly released third-party service mesh plugin not covered in the 29 official articles
    And the highest-scoring candidate passage achieves a "rerank_score" of 0.54 (below 0.75)
    When the Retrieve component searches the official documentation corpus
    Then the Retrieve component filters out all candidates and sets "retrieved_passages" to an empty list "[]"
    And "highest_rejected_retrieval_score" is recorded as 0.54 in the state for auditability

  @behaviour:unofficial-data-exclusion @FR-01 @FR-09
  Scenario: Guaranteeing zero retrieval from historical tickets or private agent snippet files
    Given an answer to a customer's legacy configuration question exists only in an agent's private snippet file or a historical ticket
    And that answer does not exist within the 29 official articles in "documentation.json"
    When the Retrieve component queries the vector store with filter "source_origin == 'OFFICIAL_KB_ONLY'"
    Then zero passages from historical tickets or private agent text files are returned
    And "retrieved_passages" is empty "[]"
