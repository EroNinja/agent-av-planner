# No-Hint Ablation: Preliminary Evaluation

## Objective and setup

This experiment tests whether removing the prompt's spread-out-versus-clustered error hint changes fault-diagnosis performance for the single-agent baseline and multi-agent pipeline. No application code was modified for these runs. Both sweeps used `groq:openai/gpt-oss-20b`, the same eight noisy logs, and both conditions (`single_agent` and `pipeline`). The no-hint sweep passed `--no-hint`; the hint-enabled sweep omitted that flag. Each used `--limit 8`.

## Completed sweeps and accuracy

| Hint setting | Sweep ID | Architecture | Successful calls / attempted | Correct / successful | Accuracy | Strict accuracy |
|---|---|---|---:|---:|---:|---:|
| Enabled | `faf1bd3e8766` | `single_agent` | 8/8 | 8/8 | 100% | 100% |
| Enabled | `faf1bd3e8766` | `pipeline` | 8/8 | 8/8 | 100% | 100% |
| Disabled | `820ec032e0f8` | `single_agent` | 8/8 | 8/8 | 100% | 100% |
| Disabled | `820ec032e0f8` | `pipeline` | 8/8 | 8/8 | 100% | 100% |

Each sweep completed 16 model calls across 8 distinct logs; all 32 calls across both sweeps succeeded. Both CSV files have zero API errors and zero malformed responses. There was no difference in accuracy or strict accuracy between hint settings or architectures (0 percentage-point change).

## Paired statistical comparisons

Both sweep files contain the same eight log names, paired by `log_name` and `condition`. For each comparison, all eight paired outcomes were correct in both arms, with zero discordant pairs. Two-sided exact McNemar p-values (conventionally 1.000 when no discordant pairs exist) were:

| Paired comparison | Pairs | Discordant pairs | Exact p-value |
|---|---:|---:|---:|
| Single-agent: hint vs. no hint | 8 | 0 | 1.000 |
| Pipeline: hint vs. no hint | 8 | 0 | 1.000 |
| Hint enabled: pipeline vs. single-agent | 8 | 0 | 1.000 |
| Hint disabled: pipeline vs. single-agent | 8 | 0 | 1.000 |

No accuracy difference was observed. This does **not** establish that the hint has no effect on other fault types or larger datasets.

## Fault-type results

All eight selected logs were `dependency_down` cases (runs 1–7 and 10). Both architectures achieved 8/8 correct with and without the hint: **no fault-type accuracy changes were observed among the cases actually tested**. The CSVs contain no `slow_query`, `pool_exhausted`, or `no_fault` examples, so no finding is possible for those fault types.

## Evidence and limitations

The evidence score was 1.000 for every hint-enabled result and for every no-hint pipeline result. For the no-hint single-agent condition, seven scores were 1.000 and one (`dependency_down_run7_noisy`) was approximately 0.9091; its diagnosis was nonetheless correct. No model outputs were malformed, and no API calls failed.

Because `--limit 8` selected the first eight logs rather than a stratified sample, the test covered only one fault type. The sample is small, and all outcomes were correct, leaving no ability to distinguish the architectures by accuracy. To assess changes across fault types, conduct another paired sweep on a fixed balanced noisy dataset (for example, two logs from each fault type) with and without `--no-hint` and report its sweep IDs separately.

## Reproducibility

Run from `backend/` against the same pre-generated `logs/` directory:

```bash
python evaluate.py --arms groq:openai/gpt-oss-20b --limit 8 --output outputs/with_hint.csv
python evaluate.py --arms groq:openai/gpt-oss-20b --limit 8 --no-hint --output outputs/without_hint.csv
```

This report contains only results present in those two exported CSV files.
