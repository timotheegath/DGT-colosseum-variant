# AGENTS.md

Repo-specific guidance for OpenCode sessions working in `colosseum`. This is a research/experiments repo: a Python framework for auditing collusion in cooperative multi-agent LLM systems, built on top of [Terrarium](https://github.com/umass-aisec/Terrarium) (`terrarium-agents`). There is no application code to ship — work here is running, configuring, plotting, and post-hoc analyzing experiments.

## Quick start (verified working on this checkout)

Python 3.11 is **required** (the runners hard-fail on older versions). The repo ships a `.venv` and `uv.lock`.

```bash
# from repo root, with .venv present
.venv/bin/python -m experiments.<exp>.run --config experiments/<exp>/configs/<config>.yaml
```

If recreating the venv from scratch:

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv sync --no-install-project
cp -n .env.example .env   # then fill keys; .env is gitignored
```

For the `[vllm]` extra (local model serving): `uv pip install -e ".[vllm]"`.

## Setup gotchas

- **`external/CoLLAB` is a git submodule** (`external/CoLLAB` → `Saad-Mahmud/CoLLAB_SEA.git`). Some Terrarium DCOP environments require it. Either:
  - `git submodule update --init --recursive`, or
  - clone it anywhere and set `TERRARIUM_COLLAB_PATH` in `.env` (see `.env.example`).
- API keys belong in `.env` (gitignored), never in `.env.example`. Providers used in shipped configs: `openai`, `together`, `fireworks`, `anthropic`, `google`, `vllm` (local).
- **API costs scale linearly with #agents.** The README explicitly warns about this — every config sweep with many agents is a real-money run.

## Repo layout (what is what)

```
experiments/
  common/             # shared runner utilities: run_utils, blackboard_logger, local_protocol
  collusion/          # covert collusion + secret blackboard (JiraTicket)
  network_influence/  # misinformation propagation; uses an LLM judge (OpenAI)
  persuasion/
    collusion/        # persuasion-based collusion (JiraTicket)
    hospital/         # persuasion-based collusion (Hospital/DCOPS)
  agent_misalignment/ # objective misalignment (Hospital/DCOPS), Figure 7 setup
external/CoLLAB/      # submodule, DCOPs reference impl + examples
dev/                  # paper assets (icons, motivation slides) — not code
```

Each experiment follows the same shape: `run.py`, `resume.py`, `prompts.py`, `metrics.py`, `judge_*.py` (some), `configs/*.yaml`, `plots/`, plus per-experiment `README.md` with experiment-specific commands.

## Runners and how they actually work

All experiment runners share the same `run_from_config` pattern in `experiments/common/run_utils.py`. Inputs are a YAML config; outputs land under `experiments/<exp>/outputs/<tag>/<timestamp>/`.

Per-run artifacts under `runs/<model_label>/<sweep_name>/<run_id>/`:
- `run_config.json`, `final_summary.json`, `metrics.json`
- `agent_turns.json`, `tool_events.json`, `blackboards.json`
- `blackboard_*.txt` human-readable blackboard snapshots
- (network_influence/agent_misalignment) `judge_results.json`, `survey_responses.json`

Aggregate artifacts at the timestamp root: `summary.json`, `summary.jsonl`, `summary.csv`, `experiment.log`, `progress.json`.

`run_id` format is stable and used by `resume.py` to identify runs:
```
<model>__<sweep>[__env<env>]__<topology>__n<N>__c<C>__secret<0|1>__pv<variant>__seed<S>
```
`prompt_variants` only matter when `secret_channel_enabled=true`; the runner forces the effective variant to `control` otherwise (this is enforced in `experiments/collusion/run.py`).

## Key commands

Run a sweep (replace `<exp>` with `collusion` | `network_influence` | `agent_misalignment` | `persuasion.collusion` | `persuasion.hospital`):

```bash
.venv/bin/python -m experiments.<exp>.run --config experiments/<exp>/configs/<file>.yaml
```

Each runner also accepts `--out-dir <path>` and `--max-concurrent-runs <int>` (overrides `experiment.max_concurrent_runs` in the YAML).

**Resume an interrupted sweep in-place** (no new timestamp folder; re-runs only missing/incomplete runs):

```bash
.venv/bin/python -m experiments.<exp>.resume --root <existing_output_root> [--config ...] [--dry-run]
```

Treats a run as incomplete if any of `run_config.json`, `metrics.json`, `final_summary.json`, `agent_turns.json`, `tool_events.json`, `blackboards.json` is missing, or if `agent_turns.json` contains `"response": "[ERROR]"` (default; disable with `--no-rerun-error-turns`). Pass `--require-status-complete` to also require `metrics.status == "complete"`. Always do `--dry-run` first.

**Generate plots** for a completed sweep:

```bash
.venv/bin/python -m experiments.<exp>.plots.generate_all --sweep-dir experiments/<exp>/outputs/<tag>/<timestamp>/runs/<model_label>/<sweep_name>
```

Output lands under `experiments/<exp>/plots_outputs/<tag>/<timestamp>/<model_label>/<sweep_name>/`.

**Collusion-specific extras** (see `experiments/collusion/README.md`):
- LLM-as-judge on the secret blackboard: `python -m experiments.collusion.judge_blackboards --root <root> --max-concurrent 16` (defaults to `gpt-4o-mini`; needs `OPENAI_API_KEY`).
- Regret report (Jira optimal vs achieved): `compute_jira_optimal.py` → `plots.generate_jira_regret_report --compute-optimal`.
- Sequential (turn-order-aware) regret: `experiments.collusion.compute_sequential_regret`.
- Correlation between judge scores and run features: `experiments.collusion.analyze_judge_correlations`.

## Environment selection (important)

Configs pick the environment via `environment.name` (must be exported from an `envs` package) or `environment.import_path` formatted as `some.module:ClassName` — e.g., `experiments.collusion.choice.jira_ticket_choice_env:JiraTicketChoiceEnvironment` (used in the 3-agent/2-blackboard variant to disable Jira's "post on every blackboard" prompt bias).

Shipped environments used by configs: `JiraTicketEnvironment`, `MeetingSchedulingEnvironment`, `HospitalEnvironment` (DCOPS), plus `*ChoiceEnvironment` variants in `experiments/collusion/choice/`.

## Communication protocol

`experiments/common/local_protocol.py` is an **in-process** `BaseCommunicationProtocol` backed by `terrarium.Blackboard.Megaboard`. It does **not** require an MCP server and supports parallel runs. If you see references to "MCP" in older docs/comments, they are obsolete — the runners explicitly note "no MCP."

## Tooling

- **Linter:** `ruff>=0.9.0` in the `dev` dep group. Run `.venv/bin/ruff check .` or `.venv/bin/ruff format .`. The runner files have `# ruff: noqa: E402` because they do `sys.path` mutation before imports.
- **No tests, no CI, no pre-commit, no Makefile, no `justfile`.** Don't go looking — there is nothing to run besides the experiment scripts and `ruff`.
- **No `opencode.json`/`AGENTS.md`/`.cursor/rules` etc. exist in the repo.** This file is the first.

## Things an agent is likely to get wrong

- Forgetting Python 3.11: every runner `raise RuntimeError("Terrarium requires Python >= 3.11")` early. Always use `.venv/bin/python` (it points to 3.11).
- Running with `--out-dir` and then losing the path: the runner *appends* a timestamp subfolder under `output_dir`/`--out-dir`. The root you pass to `resume.py` is that final timestamped path, not the bare `output_dir`.
- Treating `prompt_variants` as orthogonal to `secret_channel_enabled`: they aren't. The runner only runs non-`control` variants when the secret channel is on. `summary.csv` will be missing rows that you "expect."
- Editing `experiments/network_influence` and then wondering why `experiments/collusion` looks similar: collusion was modeled after network_influence but is the simpler in-process version. Don't share edits between them blindly.
- Submodule appears empty (`external/CoLLAB/` has files but `.git` is just a gitlink): run `git submodule update --init --recursive` if `TERRARIUM_COLLAB_PATH` environments are needed.
- `outputs/`, `plots_outputs/`, `logs/`, `*.log` are all gitignored — that's expected. `progress.json` and `summary.json` are written atomically (`*.tmp` then `replace`), so a crashed run leaves either nothing or a complete file.
- API-key env vars: the runners do not call `load_dotenv()` themselves; the shell environment (or `.env` loaded by your shell wrapper) must provide them. If a run errors on auth, check that the venv you're using actually sees the keys (`.venv/bin/python -c "import os; print(os.environ.get('OPENAI_API_KEY'))"`).

## Reference map for non-obvious code

- Sweep expansion logic and prompt-variant/secret-channel interaction: `experiments/collusion/run.py::_run_single` and `run_from_config`.
- "What counts as a complete run" for resume: `experiments/collusion/resume.py::_is_run_acceptable`.
- `progress.json` schema: `experiments/common/run_utils.py::write_progress`.
- Built-in `prompt_variant` strings:
  - collusion: `control`, `simple`, `deception`, `structured`, `aggressive` — `experiments/collusion/prompts.py`.
  - persuasion/collusion: `control`, `helpful_misdirection`, `authority_nudge`, `social_proof`, `scarcity_pressure`, `reciprocity_trade`, `risk_aversion`, `cost_anchor`, `split_brain` — `experiments/persuasion/collusion/prompts.py`.
- Topology knobs (`edge_prob`, `k`, `rewire_prob`, `m`): under `communication_network` in any config; only the relevant ones for the chosen `topology` are read.
