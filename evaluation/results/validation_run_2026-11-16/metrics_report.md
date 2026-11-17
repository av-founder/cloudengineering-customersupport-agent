# Evaluation Metrics Report - validation_tickets.json

Run: 2026-11-16T08:12:50.097107+00:00 -> 2026-11-16T08:17:49.288168+00:00 (299.19 s)

## Volume
| Metric | Value |
|---|---|
| tickets_processed | 80 |
| answered_automatically | 32 |
| escalated | 48 |
| blocked_by_guardrails | 6 |

| Routing action | Count |
|---|---|
| AUTO_REPLY | 32 |
| BLOCK_SAFETY_GUARDRAIL_FAIL | 6 |
| ESCALATE_MANDATORY_INTENT | 24 |
| ESCALATE_NO_GROUNDED_DOC | 18 |

## Business
| Metric | Value |
|---|---|
| first_contact_resolution_rate | 40.00% |
| escalation_rate | 60.00% |
| response_time_mean_ms | 2744.7 |
| response_time_median_ms | 2598.0 |
| response_time_mean_s | 2.745 |
| response_time_median_s | 2.598 |

## Technical
| Metric | Value |
|---|---|
| classification_accuracy | 100.00% |
| classification_macro_precision | 100.00% |
| classification_macro_recall | 100.00% |
| retrieval_hit_rate | 67.50% |
| retrieval_hit_rate_all_answerable | 50.94% |
| latency_median_p50_ms | 2598.0 |
| latency_p95_ms | 6260.1 |

| Intent | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| account_access | 1.00 | 1.00 | 1.00 | 4 |
| api_key_issue | 1.00 | 1.00 | 1.00 | 6 |
| api_usage_question | 1.00 | 1.00 | 1.00 | 4 |
| authentication_failure | 1.00 | 1.00 | 1.00 | 3 |
| billing_query | 1.00 | 1.00 | 1.00 | 10 |
| compliance_request | 1.00 | 1.00 | 1.00 | 1 |
| configuration_help | 1.00 | 1.00 | 1.00 | 3 |
| data_export | 1.00 | 1.00 | 1.00 | 1 |
| data_residency | 1.00 | 1.00 | 1.00 | 6 |
| database_issue | 1.00 | 1.00 | 1.00 | 1 |
| deployment_failure | 1.00 | 1.00 | 1.00 | 5 |
| feature_request | 1.00 | 1.00 | 1.00 | 3 |
| integration_help | 1.00 | 1.00 | 1.00 | 3 |
| onboarding | 1.00 | 1.00 | 1.00 | 4 |
| performance_degradation | 1.00 | 1.00 | 1.00 | 3 |
| quota_or_overage | 1.00 | 1.00 | 1.00 | 2 |
| rate_limit | 1.00 | 1.00 | 1.00 | 1 |
| rollback_request | 1.00 | 1.00 | 1.00 | 6 |
| security_incident | 1.00 | 1.00 | 1.00 | 4 |
| sso_configuration | 1.00 | 1.00 | 1.00 | 1 |
| unclear_request | 1.00 | 1.00 | 1.00 | 6 |
| webhook_issue | 1.00 | 1.00 | 1.00 | 3 |

Wall clock: 80 tickets in 299.2s (2.79s per ticket after 76.0s of model loading/indexing) at concurrency 1; 334 live LLM calls, 0 cache hits, 0 provider failovers, 0.0s rate-limit backoff.

## Governance
| Metric | Value |
|---|---|
| decisions_logged | 80 |
| decision_log_coverage_rate | 100.00% |
| guardrail: outbound_pii_or_secret_block | 0 |
| guardrail: nli_hallucination_entailment_block | 6 |
| guardrail: citation_integrity_block | 0 |
| guardrail: disclaimer_or_tone_block | 0 |
| private data: ingest_redactions_total | 0 |
| private data: ingest_redactions_by_entity_type | {} |
| private data: outbound_blocked_detections_total | 0 |
| private data: outbound_leaked_occurrences | 0 |

## Safety (vs labels)
| Metric | Value |
|---|---|
| labeled_tickets | 80 |
| must_not_auto_respond_violations | 0 |
| expected_route_accuracy | 57.50% |
| auto_resolution_precision | 71.88% |
| false_escalation_rate | 52.08% |
| label_implied_max_fcr | 60.00% |

## Fairness (fluent vs non_fluent)
| Metric | fluent | non_fluent | abs delta |
|---|---|---|---|
| tickets | 61 | 19 | n/a |
| fcr | 36.07% | 52.63% | 16.56% |
| intent_accuracy | 100.00% | 100.00% | 0.00% |
| retrieval_hit_rate | 50.00% | 55.56% | 5.56% |
| auto_resolution_precision | 81.82% | 50.00% | 31.82% |
| false_escalation_rate | 55.00% | 37.50% | 17.50% |

FCR two-proportion z-test p = 0.1981; bootstrap 95% CI of FCR delta = [-0.4186, 0.094]

## LLM usage
| Metric | Value |
|---|---|
| calls | 334 |
| prompt_tokens | 588534 |
| completion_tokens | 38505 |
| pipeline_errors | 0 |

## Invariants
- processed_equals_answered_plus_escalated: **True**
- full_audit_coverage: **True**
