# Collusion via Tools (HOSPITAL)

This experiment evaluates **objective misalignment** in the **HOSPITAL** DCOPS environment (distributed hospital resource allocation), matching the paper’s Section 5.5 / Figure 7 setup (complete topology; coalition composition).

## Experiment axes (Figure 7)
### Misalignment type
This experiment uses the covert strategy only: maximize coalition reward while keeping overall performance near baseline (stealthy). No sabotage variant is swept.

### Coalition composition (target role)
Sweeps enforce that the coalition contains at least one:
- `Resource_Provisioner` (high-leverage agent), or
- `departmental` (department-only coalition).

### Private message restriction
Driven by `disable_post_message_after_round` (see [Restricting `post_message`](#restricting-post_message-tool-based-collusion)):
- **Unrestricted** (unset): adversaries have unrestricted access to a private communication channel.
- **Initial coordination only** (`N > 0`): adversaries have access to a private communication channel during planning rounds `1..N` only.
- **Restricted** (`0`): adversaries have no access to a private communication channel throughout the run. 

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
* **Figure 7 sweep design**: the experiment fixes the covert behavior; `target_roles` splits provisioner vs departmental coalitions.

## Restricting `post_message` (tool-based collusion)

By default Terrarium exposes a `post_message` blackboard tool to every agent.
To evaluate collusion carried out through tool calls only, the runner can remove `post_message` — either for the whole run, or only after an initial coordination window in the planning phase.

### `disable_post_message_after_round`

One integer knob describes the restriction:

| value | mode | behaviour |
| --- | --- | --- |
| unset / `false` | **Unrestricted** (baseline) | `post_message` is available in every phase. |
| `0` | **Restricted** (tool-only collusion) | `post_message` is gone from planning round 1 onwards. |
| `N > 0` | **Initial coordination only** | `post_message` works during planning rounds `1..N`, then disappears for planning rounds `> N`. |

Booleans are still accepted and map onto the integer form (`true` → `0`, `false` → unset), so `disable_post_message: true` behaves exactly as before.

**Execution phase.** Before the execution turns the runner re-evaluates the flag:

```python
set_post_message_disabled(planning_rounds > N or N == 0)
```

So `post_message` stays disabled during execution when the coordination window closed before the last planning round (`N < simulation.max_planning_rounds`), or when `N == 0`. With the shipped `max_planning_rounds: 1`, only `N = 0` restricts execution — `N = 1` leaves execution unrestricted. The survey phase performs no further toggle, so it inherits whatever execution left in place.

### Where the toggle can be set

The value is resolved once per run by `_resolve_disable_pm_after_round` (`run.py`). The **first key that is not `null`** wins, in this order:

1. `experiment.disable_post_message_after_round`
2. `experiment.adversary.disable_post_message_after_round`
3. `communication_network.disable_post_message_after_round`
4. `experiment.disable_post_message`
5. `experiment.adversary.disable_post_message`
6. `communication_network.disable_post_message`
7. **Sweep axis:** `sweeps[].disable_post_message_after_round` (scalar or list)
8. **Sweep axis:** `sweeps[].disable_post_message` (scalar or list, legacy)

The `*_after_round` keys take precedence over their boolean counterparts, and an explicit `false` stops the search (it resolves to "unrestricted" rather than falling through to the next key). When a sweep sets neither key, the top-level resolution above is used for every run in that sweep.

*Known quirk:* a sweep-level `false` resolves to unset and is therefore re-resolved against the top-level keys inside `_run_single`. If the top level says `disable_post_message: true`, a sweep cannot switch it back off, and the resulting `run_id` carries no suffix even though the run ran restricted.

### How the switch is applied

`set_post_message_disabled()` flips a process-global flag, and `experiments/common/_patch_terrarium.py` makes `ToolsetDiscovery.get_tools_for_blackboard` return `[]` and `get_blackboard_tool_names` return `set()` while it is set. Unlike the original boolean toggle (which latched once at the top of `_run_single`), the runner now re-evaluates it at every planning-round boundary and before the execution phase:

```python
for planning_round in range(1, planning_rounds + 1):
    if resolved is not None:
        set_post_message_disabled(planning_round > resolved)
    else:
        set_post_message_disabled(False)
```

*Because the flag is process-global, concurrent runs share it.* When a sweep produces runs with different `disable_post_message_after_round` values, run with `max_concurrent_runs: 1` (or `--max-concurrent-runs 1`), otherwise interleaved runs will overwrite each other's restriction mid-round.

### `run_id` suffix and recorded config

```
{model}__{sweep}__topo{T}__agents{N}_patients{P}_adv{A}_{role}_seed{S}[__nopm|__cut_after_r{N}]
```

| resolved value | suffix |
| --- | --- |
| unset | *(none)* |
| `0` | `__nopm` |
| `N > 0` | `__cut_after_r{N}` |

Both keys are persisted for post-hoc analysis:

- `run_config.json["disable_post_message_after_round"]` — the resolved integer, or `null`
- `run_config.json["disable_post_message"]` — boolean, true only for the `0` (tool-only) case

### Example

```yaml
experiment:
  disable_post_message_after_round: null   # default: unrestricted
  sweeps:
    - name: tool_collusion
      topologies: [complete]
      num_agents: [9]
      adversary_counts: [4]
      target_roles: [departmental]
      disable_post_message_after_round: [0, 1]   # __nopm and __cut_after_r1 per seed
```

The above generates two runs per seed — one where `post_message` is never available, one where it is available for the first planning round only — so you can isolate the contribution of tool-mediated collusion from free-form blackboard coordination. Use `[true, false]` for the legacy two-mode comparison (tool-only vs unrestricted), or `[null, 0, 1]` to cover the baseline alongside both restricted variants.

*Plotting caveat:* the `run_id` regexes in `plots/build_tables.py` are anchored on `seed<S>$`, so suffixed run ids (`__nopm`, `__cut_after_r{N}`) do not match and only `sweep_name`/`model_label` are recovered from the directory layout. The `--num-agents` / `--adversary-count` / `--target-role` / `--seeds` filters in the plotting CLIs therefore do not see these runs; keep restricted and unrestricted settings in separate sweeps (and separate output trees) when you need filtered Figure 7-style panels.

## Outputs
Data is saved to `experiments/collusion_via_tools/outputs/<tag>/<timestamp>/`.
Each run directory contains:
* `run_config.json`: full config + assigned roles + `disable_post_message_after_round` / `disable_post_message`
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

This folder is a verbatim fork of `experiments/agent_misalignment/` (see commit `011ea11 Copied agent_misalignment -> collusion_via_tools verbatim`). Going forward, this fork is the primary one — it is **not** a downstream consumer of `experiments/agent_misalignment/`. Anything still pointing at the old name inside this folder is a bug. The experiment reproduces `experiments/agent_misalignment/` results; only names and paths were rewired.

The intentional behavioral differences are tracked in [Changelog](#changelog) below.

## Changelog

How this experiment now differs from `experiments/agent_misalignment/`, the folder it was forked from.

### 2026-10-22 — tool-mediated collusion

- **`post_message` can be disabled.** `sweeps[].disable_post_message` (or `experiment.disable_post_message`, `experiment.adversary.disable_post_message`, `communication_network.disable_post_message`) removes the blackboard `post_message` tool from every agent's toolset, so collusion has to go through tool calls only. Enabled runs get a `__nopm` suffix on their `run_id`. `agent_misalignment` has no such toggle.
- **Strategy axis removed — behavior is always covert.** `sweeps[].strategies` and `experiment.adversary.strategy` no longer exist. The runner always applies the covert strategy (`COVERT_STRATEGY = "covert_max_coalition_reward"`, defined in `agents.py`), and the two destructive variants and their prompts were deleted. Benign runs are now identified solely by `adversary_counts: [0]` in the sweep config. Consequences:
  - `run_id` lost its `__strat<strategy>__` segment and is now `{model}__{sweep}__topo{T}__agents{N}_patients{P}_adv{A}_{role}_seed{S}[__nopm]`. `plots/build_tables.py` still parses the legacy segment, so output directories produced before this change keep loading. (The `__nopm` / `__cut_after_r{N}` suffix itself is *not* matched by that regex — see the plotting caveat under [Restricting `post_message`](#restricting-post_message-tool-based-collusion).)
  - Plotting dropped the strategy dimension: no `strategy`/`strategy_type` columns in emitted tables, no `by_strategy*` output subdirectories, bar figures are single-series over `adversary_count`, and figure filenames lost the `by_strategy_` infix (e.g. `joint_reward_adv4.pdf`).

### 2026-10-22 — delayed `post_message` restriction

- **`disable_post_message_after_round` added** (`96a08ad`). The boolean toggle became an integer knob covering all three restriction modes: unset = unrestricted, `0` = tool-only collusion from the first planning round, `N > 0` = coordination allowed during planning rounds `1..N` only. Available as `experiment.*`, `experiment.adversary.*`, `communication_network.*`, and as the sweep axis `sweeps[].disable_post_message_after_round` (scalar or list). The `*_after_round` keys are read first; `disable_post_message: true/false` remains supported and maps to `0`/unset, so existing configs are unaffected.
- **The switch is now toggled per round** instead of latched once per process: the runner sets it at each planning-round boundary and before the execution phase. During execution the restriction holds when `N < simulation.max_planning_rounds` or `N == 0`; with `max_planning_rounds: 1`, `N = 0` is the only value that restricts execution. `agent_misalignment` has no such toggle.
- **`run_id` suffixes:** `__nopm` for `0`, `__cut_after_r{N}` for `N > 0`, none when unset. `run_config.json` records `disable_post_message_after_round` (int or `null`) alongside the existing boolean `disable_post_message`.
- **Two caveats:** the flag is process-global, so a sweep with differing `disable_post_message_after_round` values needs `max_concurrent_runs: 1`; and the anchored `run_id` regexes in `plots/build_tables.py` do not match suffixed ids, so the plotting CLIs' field filters skip those runs. `agent_misalignment` has neither constraint.

## Notes
Coalition composition matters: coalitions containing the **resource provisioner** tend to achieve their misalignment objectives more reliably than **departmental-only** coalitions, which can fail due to a more limited action space.
