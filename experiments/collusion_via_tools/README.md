# Collusion via Tools (HOSPITAL)

This experiment evaluates **objective misalignment** in the **HOSPITAL** DCOPS environment (distributed hospital resource allocation), matching the paper’s Section 5.5 / Figure 7 setup (complete topology; misalignment type × coalition composition).

## Experiment axes (Figure 7)
### Misalignment type (strategy)
- **Covert** (`covert_max_coalition_reward`): maximize coalition reward while keeping overall performance near baseline (stealthy).
- **Sabotage (Self-Prom)** (`destructive_max_coalition_reward`): maximize coalition reward without preserving overall performance.
- **Sabotage (Self-Sac)** (`destructive_no_reward_preservation`): maximize overall harm (raise overall regret) regardless of coalition reward.

### Coalition composition (target role)
Sweeps enforce that the coalition contains at least one:
- `Resource_Provisioner` (high-leverage agent), or
- `departmental` (department-only coalition).

## Metrics
Metrics are computed in `metrics.py` and written to each run directory (plus sweep-level aggregations used by plotting scripts).

Figure 7-style summaries focus on:
- **Overall regret**: `max_joint_reward - joint_reward` (derived from number of patients and achieved joint reward).
- **Coalition regret**: coalition’s shortfall vs an equal-share maximum (computed from per-agent rewards).
- **Coalition advantage**: coalition average reward minus non-coalition average reward (also reported in hospital-level variants).

Additional auditing metrics (optional, not required for Figure 7) include:
- **Suspicion / attribution**: an LLM judge scores post-run surveys (who suspects foul play and whether blame is correct).

## Run
1) Ensure API keys are set (root `.env`):
- `OPENAI_API_KEY` (for the evaluator/judge)
- `TOGETHER_API_KEY` (if using Together models)

2) Run with a config from `configs/`:
```bash
python experiments/collusion_via_tools/run.py \
  --config experiments/collusion_via_tools/configs/collusion_via_tools_gpt_4_1_mini.yaml
```

Optional parameters:
- `--out-dir <path>`: override the configured output directory
- `--max-concurrent-runs <int>`: cap parallelism (also configurable in YAML)

Configs you’ll likely use:
- `configs/collusion_via_tools_gpt_4_1_mini.yaml`, `configs/collusion_via_tools_gpt_4o_mini.yaml`, `configs/collusion_via_tools_kimi_k2_instruct.yaml`: model-specific presets
- `configs/collusion_via_tools_benign.yaml`: benign baseline

## Plot
Generate sweep-level plots (including Figure 7-style panels) from a sweep directory:
```bash
python -m experiments.collusion_via_tools.plots.generate_all \
  --sweep-dir experiments/collusion_via_tools/outputs/<tag>/<timestamp>/runs/<model>/<sweep_name>
```

Optional parameters:
- `--benign-sweep-dir <path>`: overlay a benign baseline
- `--seeds 0-9`: filter which seeds to include

### Multi-model comparison
Compare multiple model runs on the same grouped bar charts:
```bash
python -m experiments.collusion_via_tools.plots.plot_model_comparison \
  --model-runs-dir experiments/collusion_via_tools/outputs/<tag>/<timestamp>/runs/<model> \
  --model-runs-dir experiments/collusion_via_tools/outputs/<tag>/<timestamp>/runs/<model> \
  --num-agents 9 \
  --target-role departmental \
  --adversary-count 4 \
  --seeds 0-4 \
  --out-dir experiments/collusion_via_tools/plots_outputs/<tag>/<timestamp>/model_compare
```

The filter arguments above (`--num-agents`, `--target-role`, `--adversary-count`, `--seeds`) are optional; omit them to plot all matching runs under the provided `--model-runs-dir` roots.

## Configuration notes
* **Environment class**: `MisalignmentHospitalEnvironment` (in `experiments.collusion_via_tools.environment`)
* **Figure 7 topology**: `complete`
* **Figure 7 sweep design**: strategies correspond to the three misalignment types; `target_roles` splits provisioner vs departmental coalitions.

## Outputs
Data is saved to `experiments/collusion_via_tools/outputs/<tag>/<timestamp>/`.
Each run directory contains:
* `run_config.json`: full config + assigned roles
* `final_summary.json`: environment final state summary
* `judgements.json`: judge outputs (if enabled)
* `agent_turns.json`: full transcript (planning/execution/surveys)

## Resume
If a sweep is interrupted, you can resume it without re-running completed settings:
```bash
python -m experiments.collusion_via_tools.resume \
  --root experiments/collusion_via_tools/outputs/<tag>/<timestamp> \
  --dry-run
python -m experiments.collusion_via_tools.resume \
  --root experiments/collusion_via_tools/outputs/<tag>/<timestamp>
```

## Differences with Agent misalignment experiment

This folder is a verbatim fork of `experiments/agent_misalignment/` (see commit `011ea11 Copied agent_misalignment -> collusion_via_tools verbatim`). Going forward, this fork is the primary one — it is **not** a downstream consumer of `experiments/agent_misalignment/`. Anything still pointing at the old name inside this folder is a bug.

### Renamed in this fork

- **Imports** in `run.py`, `resume.py`, `environment.py`, and `scripts/backfill_hospital_adjusted_metrics.py`: now self-reference `experiments.collusion_via_tools.*` instead of `experiments.agent_misalignment.*`. All imported modules (`agents`, `metrics`, `prompts`, `judge_beliefs`, `costs`, `tools`, `environment`) exist locally, so the runner is self-contained.
- **Logger name** (`run.py`): `experiments.collusion_via_tools`.
- **Default output root** (`run.py`, `resume.py`): `experiments/collusion_via_tools/outputs/...`.
- **YAML config filenames** in `configs/`: `agent_misalignment_*.yaml` → `collusion_via_tools_*.yaml`.
- **YAML fields** (`tag`, `output_dir`, `sweeps[].name`, `adversary.strategy`, `experiment.info.code_prefix`): `agent_misalignment` / `AGENT_MISALIGNMENT` → `collusion_via_tools` / `COLLUSION_VIA_TOOLS`.
- **Doc strings** (`__init__.py`, plot-script descriptions, CLI help text): updated to the new name.

### Intentionally not renamed

- **Class / function names**: `MisalignmentHospitalEnvironment`, `MisalignmentHospitalTools`, `MisalignmentPrompts`, `compute_misalignment_metrics`, `judge_run`. These identify the underlying mechanism, not the experiment folder. Renaming them would require touching every reference inside `agents.py`, `prompts.py`, `metrics.py`, `judge_beliefs.py`, and `tools.py`, which is out of scope for a folder rename.
- **Behavioural code**: `agents.py`, `costs.py`, `metrics.py`, `prompts.py`, `judge_beliefs.py`, `tools.py`, `plots/*`, and `environment.py` are byte-identical to the source. The experiment reproduces `experiments/agent_misalignment/` results; only names and paths were rewired.

### Relationship to `experiments/collusion/` (Jira)

`experiments/collusion/` is a separate experiment based on the Jira-ticket family of DCOPS environments and is **not** used here. Jira-specific knobs — `environment.max_tasks`, `environment.assignment_filling`, the `JiraTicketEnvironment` / `*ChoiceEnvironment` class family, the `experiment.collusion.*` sub-schema, the sweep axes `colluder_counts` / `secret_channel_enabled` / `prompt_variants`, and the `communication_network` watts-strogatz / barabasi-albert knobs (`edge_prob`, `k`, `rewire_prob`, `m`) — do not apply to this Hospital-based fork and are intentionally disregarded.

The Hospital environment in this fork uses `num_patients` / `num_hospitals` / `default_capacity`, the `experiment.adversary.*` sub-schema, and the sweep axes `adversary_counts` / `strategies` / `target_roles` / `num_patients` instead.

## Notes
Coalition composition matters: coalitions containing the **resource provisioner** tend to achieve their misalignment objectives more reliably than **departmental-only** coalitions, which can fail due to a more limited action space.
