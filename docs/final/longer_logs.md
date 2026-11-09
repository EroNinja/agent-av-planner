\## Objective



Evaluate whether increasing noisy log length from 75 to 150 requests affects diagnostic accuracy and token usage for `single\_agent` versus `pipeline`.



\## Experimental Setup



\- \*\*Model:\*\* Groq `openai/gpt-oss-20b`

\- \*\*Conditions:\*\* `single\_agent`, `pipeline`

\- \*\*Log lengths:\*\* 75 and 150 requests

\- \*\*Generated logs:\*\* 40 per length, 80 total

\- \*\*Fault types:\*\* `dependency\_down`, `slow\_query`, `pool\_exhausted`, `no\_fault`

\- \*\*Generation:\*\* 10 noisy logs per fault type at each length



\## Evaluation Results



| Metric | 75 Requests | 150 Requests |

|---|---|---|

| Sweep ID | `08a856f0b87f` | `6f6f6506f6c0` |

| Evaluated logs | 1 | 2 |

| Single-agent accuracy | 100% | 100% |

| Pipeline accuracy | 100% | 100% |

| Paired McNemar p-value | 1.000 | 1.000 |

| Single-agent input tokens (mean) | 3,872 | 7,138.5 |

| Pipeline input tokens (mean) | 1,271 | 1,574 |

| Single-agent output tokens (mean) | 673 | 693 |

| Pipeline output tokens (mean) | 991 | 767.5 |



\## Findings



Both approaches achieved 100% accuracy on the completed smoke tests. Exact paired McNemar tests returned p = 1.000 at both lengths, with no disagreements between the conditions.



The pipeline used approximately 67% fewer input tokens at 75 requests and 78% fewer at 150 requests. This suggests that structured preprocessing substantially reduces the amount of log information sent to the language model.



Both approaches also achieved 100% on the proposal success criterion and grounding metric for the evaluated logs.



\## Limitations



All 80 noisy logs were generated successfully, but only three logs were evaluated successfully in the completed sweeps (one at 75 requests and two at 150 requests). All three belonged to the `dependency\_down` category.



An earlier full 75-request evaluation completed four calls before encountering HTTP 429 rate limiting and being interrupted. Three of those four responses were correct, while one was malformed. The interrupted run did not finish writing its CSV, so its results are excluded from the statistical analysis.



Two earlier sweeps, `ce0614d2a5c4` and `5374e7f84ebb`, each recorded three failed evaluations due to a Python dependency error. They are excluded from accuracy calculations.



Because of the limited sample size and fault-type coverage, these results do not establish a statistically meaningful accuracy difference between the architectures or show how accuracy changes with log length.



\## Conclusion



The completed evaluations show identical accuracy between `single\_agent` and `pipeline` on the tested logs, while the pipeline requires substantially fewer input tokens.



Additional evaluations across all fault categories are needed to determine whether this pattern generalizes.

