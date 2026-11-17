# Evaluation Metrics Report - development_tickets.json

Run: 2026-11-16T06:23:12.555002+00:00 -> 2026-11-16T06:30:21.105372+00:00 (428.55 s)

## Volume
| Metric | Value |
|---|---|
| tickets_processed | 500 |
| answered_automatically | 247 |
| escalated | 253 |
| blocked_by_guardrails | 29 |

| Routing action | Count |
|---|---|
| AUTO_REPLY | 247 |
| BLOCK_SAFETY_GUARDRAIL_FAIL | 29 |
| ESCALATE_LOW_CONFIDENCE | 8 |
| ESCALATE_MANDATORY_INTENT | 111 |
| ESCALATE_NO_GROUNDED_DOC | 105 |

## Business
| Metric | Value |
|---|---|
| first_contact_resolution_rate | 49.40% |
| escalation_rate | 50.60% |
| response_time_mean_ms | 725.9 |
| response_time_median_ms | 876.5 |
| response_time_mean_s | 0.726 |
| response_time_median_s | 0.876 |

## Technical
| Metric | Value |
|---|---|
| classification_accuracy | 99.20% |
| classification_macro_precision | 99.41% |
| classification_macro_recall | 99.26% |
| retrieval_hit_rate | 69.51% |
| retrieval_hit_rate_all_answerable | 59.38% |
| latency_median_p50_ms | 876.5 |
| latency_p95_ms | 1284.0 |

| Intent | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| account_access | 1.00 | 1.00 | 1.00 | 22 |
| api_key_issue | 1.00 | 1.00 | 1.00 | 18 |
| api_usage_question | 1.00 | 0.88 | 0.93 | 24 |
| authentication_failure | 1.00 | 1.00 | 1.00 | 20 |
| billing_query | 1.00 | 1.00 | 1.00 | 24 |
| compliance_request | 1.00 | 1.00 | 1.00 | 26 |
| configuration_help | 1.00 | 1.00 | 1.00 | 17 |
| data_export | 0.91 | 1.00 | 0.95 | 29 |
| data_residency | 1.00 | 1.00 | 1.00 | 29 |
| database_issue | 1.00 | 0.96 | 0.98 | 26 |
| deployment_failure | 0.96 | 1.00 | 0.98 | 27 |
| feature_request | 1.00 | 1.00 | 1.00 | 20 |
| integration_help | 1.00 | 1.00 | 1.00 | 21 |
| onboarding | 1.00 | 1.00 | 1.00 | 22 |
| performance_degradation | 1.00 | 1.00 | 1.00 | 23 |
| quota_or_overage | 1.00 | 1.00 | 1.00 | 23 |
| rate_limit | 1.00 | 1.00 | 1.00 | 13 |
| rollback_request | 1.00 | 1.00 | 1.00 | 28 |
| security_incident | 1.00 | 1.00 | 1.00 | 26 |
| sso_configuration | 1.00 | 1.00 | 1.00 | 26 |
| unclear_request | 1.00 | 1.00 | 1.00 | 15 |
| webhook_issue | 1.00 | 1.00 | 1.00 | 21 |

Wall clock: 500 tickets in 428.6s (0.79s per ticket after 33.6s of model loading/indexing) at concurrency 1; 0 live LLM calls, 2165 cache hits, 0 provider failovers, 0.0s rate-limit backoff.

## Governance
| Metric | Value |
|---|---|
| decisions_logged | 500 |
| decision_log_coverage_rate | 100.00% |
| guardrail: outbound_pii_or_secret_block | 0 |
| guardrail: nli_hallucination_entailment_block | 29 |
| guardrail: citation_integrity_block | 0 |
| guardrail: disclaimer_or_tone_block | 0 |
| private data: ingest_redactions_total | 0 |
| private data: ingest_redactions_by_entity_type | {} |
| private data: outbound_blocked_detections_total | 0 |
| private data: outbound_leaked_occurrences | 0 |

## Safety (vs labels)
| Metric | Value |
|---|---|
| labeled_tickets | 500 |
| must_not_auto_respond_violations | 0 |
| expected_route_accuracy | 64.00% |
| auto_resolution_precision | 76.52% |
| false_escalation_rate | 39.23% |
| label_implied_max_fcr | 62.20% |

## Fairness (fluent vs non_fluent)
| Metric | fluent | non_fluent | abs delta |
|---|---|---|---|
| tickets | 380 | 120 | n/a |
| fcr | 50.26% | 46.67% | 3.59% |
| intent_accuracy | 99.21% | 99.17% | 0.04% |
| retrieval_hit_rate | 59.26% | 59.77% | 0.51% |
| auto_resolution_precision | 75.92% | 78.57% | 2.65% |
| false_escalation_rate | 37.23% | 45.00% | 7.77% |

FCR two-proportion z-test p = 0.4921; bootstrap 95% CI of FCR delta = [-0.0597, 0.1342]

## LLM usage
| Metric | Value |
|---|---|
| calls | 2165 |
| prompt_tokens | 3757907 |
| completion_tokens | 261078 |
| pipeline_errors | 0 |

## Invariants
- processed_equals_answered_plus_escalated: **True**
- full_audit_coverage: **True**
