# Evaluation results

Each evaluation job's report, versioned by its dataset and version: `<dataset>/<version>/<job>.json` and `.md`, written
by `make eval` or `make eval-report JOB=...` (M12.8, EVL-005). A report gives every EVL-007 dimension, the SRS 10.7
gates the job decides, and every version the runs depended on, with their time, tokens and cost.

Only the reports of benchmarks and phase gates are kept here. Any other job's report can be written again from the
Evaluation Service at no cost. See [docs/evaluation-strategy.md](../../docs/evaluation-strategy.md).
