# Threshold Trade-off Report

Generated 2026-11-16T05:38:23+00:00 | calibration version `cal_v1_2026_11` | model chain `groq:openai/gpt-oss-120b -> openrouter:openai/gpt-oss-120b` | k = 3 samples | runtime 683s

Development set: `development_tickets.json` (500 tickets, sha256 `5a0d8912238ee9fde07b4b793bda4672261fbf3ab965bf3eb418ef6e87f95d78`); hidden validation files were never read.

## Ceiling implied by the labels
311/500 tickets (62.2%) are labelled `auto_respond`, so a system that only auto-answers those can reach at most 62.2% FCR and must escalate at least 37.8%. The approved policy also sends every billing_query to a human. The 65% FCR and 30% escalation business targets are therefore not reachable on this data. The escalation target is treated as a preference, not a hard constraint.

## Label noise and how it is handled
The 500 tickets contain only 343 distinct texts (same subject and body), and identical texts often carry contradictory labels. For example, one health-check body is labelled answerable 7 times and not answerable 3 times. Majority voting per text relabelled these tickets: {'answerable_from_docs': 39, 'expected_route': 31}. Ties go to the conservative label, and the expected articles are pooled. Every constraint below is measured against these denoised labels. Noise also remains across templates that differ only in wording ("on Monday" vs "over the weekend"): within such template families, 17.5% of the answerability labels contradict the family majority. Some articles are also mislabelled; for example, 'dependency resolution' tickets expect the health-check article. That floor makes a <= 5% label-based wrong-answer rate unattainable for any content-based system. Against the raw labels, the wrong-answer rate at the selected point is 26.1%.

## Confidence calibration (temperature-scaled Platt, 5-fold CV stratified by intent x fluency)
Parameters: T = 0.4798, a = 0.8709, b = 0.2947 | out-of-fold ECE = **0.0091** | per-fold ECE = [0.0278, 0.0058, 0.0158, 0.0076, 0.0163]

| Confidence bin | n | Mean confidence | Accuracy | Gap |
|---|---|---|---|---|
| 0.5-0.6 | 3 | 0.551 | 1.000 | 0.449 |
| 0.6-0.7 | 1 | 0.626 | 1.000 | 0.374 |
| 0.7-0.8 | 2 | 0.719 | 1.000 | 0.281 |
| 0.8-0.9 | 2 | 0.889 | 1.000 | 0.111 |
| 0.9-1.0 | 492 | 0.996 | 0.992 | 0.004 |

## Selected operating point
**tau_class = 0.95, tau_ret = 0.52** (249 of 500 development tickets answered automatically)

| Constraint | Target | Observed | Met |
|---|---|---|---|
| Intent precision (auto-answered) | >= 0.85 | 0.9839 | yes |
| Wrong-answer (hallucination) rate | <= 0.05 (relaxed to 0.2535) | 0.2490 | NO |
| Out-of-fold ECE | <= 0.05 | 0.0091 | yes |
| Fluent vs non_fluent parity | gap <= 0.05 or not significant (p >= 0.05) | 0.0800 | yes |
| Escalation rate (target) | <= 0.30 | 0.5020 | NO |

| Parity metric | fluent | non_fluent | abs gap | two-proportion z-test p |
|---|---|---|---|---|
| auto_resolution_precision | 0.770 | 0.690 | 0.080 | 0.217 |
| false_escalation_rate | 0.370 | 0.430 | 0.060 | 0.343 |
| retrieval_hit_rate_top1 | 0.719 | 0.684 | 0.035 | 0.566 |

Grid points meeting the original bounds: 0; meeting the bounds used for selection: 215 of 2116. Top 10 by FCR:

| FCR | Precision | Wrong-answer | Cohort delta | tau_class | tau_ret |
|---|---|---|---|---|---|
| 0.506 | 0.984 | 0.253 | 0.077 | 0.57 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.56 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.55 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.54 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.53 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.52 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.51 | 0.52 |
| 0.506 | 0.984 | 0.253 | 0.077 | 0.50 | 0.52 |
| 0.500 | 0.984 | 0.252 | 0.092 | 0.93 | 0.52 |
| 0.500 | 0.984 | 0.252 | 0.092 | 0.92 | 0.52 |

## Pipeline outcomes on the development set
- Mandatory human-review intents: 111 | unclassifiable (fallback): 0 | FR-06 'I Don't Know': 0
- Draft outcomes: {'passed': 254, 'skipped': 217, 'blocked': 29} | guardrail failures by check: {'nli_hallucination_entailment_block': 29}
- Intent accuracy overall: 99.2%; per intent: account_access 100%, api_key_issue 100%, api_usage_question 88%, authentication_failure 100%, billing_query 100%, compliance_request 100%, configuration_help 100%, data_export 100%, data_residency 100%, database_issue 96%, deployment_failure 100%, feature_request 100%, integration_help 100%, onboarding 100%, performance_degradation 100%, quota_or_overage 100%, rate_limit 100%, rollback_request 100%, security_incident 100%, sso_configuration 100%, unclear_request 100%, webhook_issue 100%

## Trade-off justification
Selected tau_class=0.95, tau_ret=0.52 (bounds unattainable on the development labels were relaxed to best achievable + 2%: {'max_hallucination_rate': 0.2535}; the most conservative thresholds within 1% of the best FCR): FCR 49.8%, escalation 50.2%, intent precision 98.4%, wrong-answer rate 24.9%, max parity gap 0.080 (not significant: True). Alternatives: tau_ret 0.57 -> FCR 46.8%, intent precision 98.3%, wrong-answer rate 25.2%; tau_ret 0.50 -> FCR 50.0%, intent precision 98.4%, wrong-answer rate 25.2%; tau_class 0.90 -> FCR 50.2%, intent precision 98.4%, wrong-answer rate 25.5%. The wrong-answer rate barely moves with the thresholds (label-noise floor), so the selection keeps auto-answers high while preferring stricter thresholds. Extra escalations cost Tier-1 time, but wrong automated answers reach paying customers unread.

## Method notes
- Thresholds are fitted offline only and loaded read-only at start-up from the artifact. Nothing is hardcoded or adapted at runtime.
- Each draft is generated once, using passages scoring at or above 0.50. For a higher tau_ret, a ticket only counts as auto-answered if every cited article still clears that threshold. This is a conservative approximation.
- Wrong-answer proxy: the ticket is labelled must_not_auto_respond or not answerable from docs, the cited article is not an expected article, or the draft makes a must_not_claim statement.
- Parity: for auto-resolution precision, false-escalation rate and top-1 retrieval hit rate, a fluent vs non_fluent gap violates the 0.05 bound only when it is also statistically significant (two-sided two-proportion z-test, p < 0.05). The non_fluent cohort is small (120 tickets), so gaps of 5-8 points are within sampling noise.
- Bounds are applied in priority order: intent precision, then parity, then wrong-answer rate. A bound no remaining grid point can meet is relaxed to best achievable + 2%. Grid points with fewer than max(10, 2% of tickets) auto-answers are ignored.
- LLM usage: 0 live calls, 2173 cache hits, 0 failovers, 0.0s rate-limit backoff.
