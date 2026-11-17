# Evaluation Metrics Report - development_tickets.json

Run: 2026-11-16T06:19:48.549437+00:00 -> 2026-11-16T06:22:17.557600+00:00 (149.01 s)

## Volume
| Metric | Value |
|---|---|
| tickets_processed | 40 |
| answered_automatically | 20 |
| escalated | 20 |
| blocked_by_guardrails | 4 |

| Routing action | Count |
|---|---|
| AUTO_REPLY | 20 |
| BLOCK_SAFETY_GUARDRAIL_FAIL | 4 |
| ESCALATE_MANDATORY_INTENT | 9 |
| ESCALATE_NO_GROUNDED_DOC | 7 |

## Business
| Metric | Value |
|---|---|
| first_contact_resolution_rate | 50.00% |
| escalation_rate | 50.00% |
| response_time_mean_ms | 2762.1 |
| response_time_median_ms | 2759.0 |
| response_time_mean_s | 2.762 |
| response_time_median_s | 2.759 |

## Technical
| Metric | Value |
|---|---|
| classification_accuracy | 100.00% |
| classification_macro_precision | 100.00% |
| classification_macro_recall | 100.00% |
| retrieval_hit_rate | 86.36% |
| retrieval_hit_rate_all_answerable | 67.86% |
| latency_median_p50_ms | 2759.0 |
| latency_p95_ms | 5638.3 |

| Intent | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| account_access | 1.00 | 1.00 | 1.00 | 3 |
| api_key_issue | 1.00 | 1.00 | 1.00 | 2 |
| api_usage_question | 1.00 | 1.00 | 1.00 | 3 |
| authentication_failure | 1.00 | 1.00 | 1.00 | 2 |
| billing_query | 1.00 | 1.00 | 1.00 | 1 |
| compliance_request | 1.00 | 1.00 | 1.00 | 4 |
| configuration_help | 1.00 | 1.00 | 1.00 | 3 |
| data_export | 1.00 | 1.00 | 1.00 | 2 |
| database_issue | 1.00 | 1.00 | 1.00 | 2 |
| deployment_failure | 1.00 | 1.00 | 1.00 | 3 |
| feature_request | 1.00 | 1.00 | 1.00 | 2 |
| integration_help | 1.00 | 1.00 | 1.00 | 2 |
| rate_limit | 1.00 | 1.00 | 1.00 | 2 |
| rollback_request | 1.00 | 1.00 | 1.00 | 4 |
| security_incident | 1.00 | 1.00 | 1.00 | 2 |
| sso_configuration | 1.00 | 1.00 | 1.00 | 3 |

Wall clock: 40 tickets in 149.0s (2.83s per ticket after 35.8s of model loading/indexing) at concurrency 1; 175 live LLM calls, 0 cache hits, 0 provider failovers, 0.0s rate-limit backoff.

## Governance
| Metric | Value |
|---|---|
| decisions_logged | 40 |
| decision_log_coverage_rate | 100.00% |
| guardrail: outbound_pii_or_secret_block | 0 |
| guardrail: nli_hallucination_entailment_block | 4 |
| guardrail: citation_integrity_block | 0 |
| guardrail: disclaimer_or_tone_block | 0 |
| private data: ingest_redactions_total | 0 |
| private data: ingest_redactions_by_entity_type | {} |
| private data: outbound_blocked_detections_total | 0 |
| private data: outbound_leaked_occurrences | 0 |

## Safety (vs labels)
| Metric | Value |
|---|---|
| labeled_tickets | 40 |
| must_not_auto_respond_violations | 0 |
| expected_route_accuracy | 77.50% |
| auto_resolution_precision | 85.00% |
| false_escalation_rate | 26.09% |
| label_implied_max_fcr | 57.50% |

## Fairness (fluent vs non_fluent)
| Metric | fluent | non_fluent | abs delta |
|---|---|---|---|
| tickets | 33 | 7 | n/a |
| fcr | 48.48% | 57.14% | 8.66% |
| intent_accuracy | 100.00% | 100.00% | 0.00% |
| retrieval_hit_rate | 63.64% | 83.33% | 19.69% |
| auto_resolution_precision | 81.25% | 100.00% | 18.75% |
| false_escalation_rate | 23.53% | 33.33% | 9.80% |

FCR two-proportion z-test p = 0.6773; bootstrap 95% CI of FCR delta = [-0.4632, 0.3203]

## LLM usage
| Metric | Value |
|---|---|
| calls | 175 |
| prompt_tokens | 302270 |
| completion_tokens | 20868 |
| pipeline_errors | 0 |

## Invariants
- processed_equals_answered_plus_escalated: **True**
- full_audit_coverage: **True**
