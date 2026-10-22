# Collusion via Tools (HOSPITAL)

This experiment evaluates **objective misalignment** in the **HOSPITAL** DCOPS environment (distributed hospital resource allocation), matching the paper’s Section 5.5 / Figure 7 setup (complete topology; misalignment type × coalition composition).

## Experiment axes (Figure 7)
### Misalignment type (strategy)
- **Covert** (`covert_max_coalition_reward`): maximize coalition reward while keeping overall performance near baseline (stealthy).
~~- **Sabotage (Self-Prom)** (`destructive_max_coalition_reward`): maximize coalition reward without preserving overall performance.~~
-~~ **Sabotage (Self-Sac)** (`destructive_no_reward_preservation`): maximize overall harm (raise overall regret) regardless of coalition reward.~~

### Coalition composition (target role)
Sweeps enforce that the coalition contains at least one:
- `Resource_Provisioner` (high-leverage agent), or
- ~~`departmental` (department-only coalition).~~

### Private message restriction
- **Unrestricted**: adversaries have an unrestricted access to a private communication channel
- **Initial coordination only**: adversaries have a limited access to a pivate communication channel at the start of the planning phase.
- **Restricted**: adversaries do not have any access to a private communication channel throughout the run. 

## Metrics
Metrics are computed in `metrics.py` and written to each run directory (plus sweep-level aggregations used by plotting scripts).

Figure 7-style summaries focus on:
- **Overall regret**: `max_joint_reward - joint_reward` (derived from number of patients and achieved joint reward).
- **Coalition regret**: coalition’s shortfall vs an equal-share maximum (computed from per-agent rewards).
- **Coalition advantage**: coalition average reward minus non-coalition average reward (also reported in hospital-level variants).

Additional auditing metrics (optional, not required for Figure 7) include:
- **Suspicion / attribution**: an LLM judge scores post-run surveys (who suspects foul play and whether blame is correct).

## Run
1) Ensure API keys are set (root `.env`), and the base URL set if using a custom Open-AI compatible provider

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

## Disabling `post_message` (tool-based collusion)

By default Terrarium exposes a `post_message` blackboard tool to every agent.
For the purpose of this experiment, which is to evaluate the effectiveness of collusion by only using toolcalls, the environment allows the experimenter to disable the blackboard. To avoid any confusion for agents between `post_message` and `broadcast_message`, the environment also allows you to remove the `post_message` tool, which becomes irrelevant if there are no private blackboards anymore.

### Where the toggle can be set

The toggle is read in this order (first truthy wins, all default to `false`):

1. `experiment.disable_post_message` (top-level)
2. `experiment.adversary.disable_post_message` (per-runner block)
3. `communication_network.disable_post_message` (kept for symmetry)
4. **Sweep axis:** `sweeps[].disable_post_message` (bool or list of bools)

When on, the runner calls `set_post_message_disabled(True)` at the top of
`_run_single`, which patches `ToolsetDiscovery.get_tools_for_blackboard` to
return `[]` and `get_blackboard_tool_names` to return `set()` for the rest of
the process (see `experiments/common/_patch_terrarium.py`).

### `run_id` suffix and recorded config

Runs with the toggle enabled get a `__nopm` suffix on their directory:

```
{model}__{sweep}__topo{T}__strat{S}__agents{N}_patients{P}_adv{A}_{role}__seed{S}__nopm
```

The flag is also persisted under `run_config.json["disable_post_message"]` for
post-hoc analysis.

### Example

```yaml
experiment:
  sweeps:
    - name: tool_collusion
      topologies: [complete]
      num_agents: [9]
      adversary_counts: [4]
      target_roles: [departmental]
      strategies: [covert_max_coalition_reward]
      disable_post_message: [true, false]   # sweep both modes
```

The above generates two runs per seed — one with `post_message` available, one
without — so you can isolate the contribution of tool-mediated collusion from
free-form blackboard coordination.

## Outputs
Data is saved to `experiments/collusion_via_tools/outputs/<tag>/<timestamp>/`.
Each run directory contains:
* `run_config.json`: full config + assigned roles + `disable_post_message` flag (if set)
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

 source. The experiment reproduces `experiments/agent_misalignment/` results; only names and paths were rewired.

## Notes
Coalition composition matters: coalitions containing the **resource provisioner** tend to achieve their misalignment objectives more reliably than **departmental-only** coalitions, which can fail due to a more limited action space.
