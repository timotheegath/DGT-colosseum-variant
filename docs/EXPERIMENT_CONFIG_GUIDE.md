# Experiment config reference

Everything you can put in an experiment YAML, and what each key actually does.

This guide is for **designing a new experiment series**. It documents the
*schema* — for a given experiment's concepts, plots, and analysis, read that
experiment's own README:

| Experiment | README |
|---|---|
| Covert collusion | `experiments/collusion/README.md` |
| Network influence | `experiments/network_influence/README.md` |
| Agent misalignment | `experiments/agent_misalignment/README.md` |
| Persuasion (collusion) | `experiments/persuasion/collusion/README.md` |
| Persuasion (hospital) | `experiments/persuasion/hospital/README.md` |

Related: [`CUSTOM_OAI_COMPATIBLE_GUIDE.md`](./CUSTOM_OAI_COMPATIBLE_GUIDE.md)
for the `custom_oai_compatible` provider and judge routing.

---

## 1. The mental model: one config → N runs

A config is **not** a run. It is a *generator*. The runner reads it and expands it
into a cross-product of independent runs:

```
runs = Σ over llm_models[]            # how many models
       × experiment.sweeps[]          # how many sweeps
       × (cartesian product of that sweep's axis lists)
       × seeds
```

Each generated run is one directory under
`<output_dir>/<timestamp>/runs/<model_label>/<sweep_name>/<run_id>/`.

Two consequences that bite people:

1. **Axis lists multiply.** `3 topologies × 6 agents × 5 colluder_counts × 2 seeds`
   = 180 runs *per model per sweep*. Adding one value to a list multiplies your
   bill. The README's warning that "API costs scale linearly with #agents" is
   about `num_agents`; in practice the axis cross-product is the real multiplier.
2. **Order matters for truncation.** `runs_per_setting` slices the *seed* list, so
   put the seeds you care about first.

### Sweep expansion order

The runners iterate axes in a fixed nesting order (outermost first):

```
model → sweep → environments → topology → num_agents →
  [num_patients] → [colluder_counts | adversary_counts] →
  [target_roles] → [strategies] → [secret_channel_enabled] →
  [coalition_clique_enabled] → prompt_variants → seed
```

(Bracketed axes exist only for some runners — see §9.) `prompt_variants` is
innermost-but-one, so **if a run fails the whole sweep aborts** in
`collusion` / `network_influence` / `persuasion.collusion` (they `raise` on the
first failure), whereas `agent_misalignment` and `persuasion.hospital` record
the error and continue.

---

## 2. Top-level block map

```yaml
simulation:            # per-run simulation knobs (seed, loop lengths)
environment:           # which Terrarium/DCOPS environment + its own params
communication_network: # graph topology + its knobs
llm_models: []         # list of {label, llm} — one per model to test
experiment:            # sweep definitions + experiment-specific knobs
judge_llm:             # OPTIONAL: route LLM judges through a provider
pricing:               # OPTIONAL: token prices for cost accounting
```

Every shipped config uses exactly these. There is no inheritance, no
`extends`, and no env-var interpolation — a sweep of model variants means
duplicating YAML or using multiple `llm_models` entries.

---

## 3. `simulation:`

```yaml
simulation:
  seed: 1                    # ⚠ OVERWRITTEN per run by the sweep seed
  max_iterations: 1
  max_planning_rounds: 4
  max_conversation_steps: 3  # per-agent tool-calling budget (BaseAgent max_steps)
  tags: [my_tag]            # free-form labels; mostly cosmetic
```

| Key | Meaning | Overridable? |
|---|---|---|
| `seed` | Fallback seed. Used for the sweep if `experiment.seeds` and `sweeps[].seeds` are both absent. | Always overwritten per run by the effective seed. |
| `max_iterations` | Outer simulation iterations. | **Ignored by `collusion`, `network_influence`, `persuasion.collusion`** — those hard-force it to `1`. Honoured by `agent_misalignment` (default 3 in configs) and `persuasion.hospital`. |
| `max_planning_rounds` | Planning cycles per iteration (each agent gets a planning turn per round). | Yes — `experiment.planning_rounds` takes precedence in `collusion`, `network_influence`, `persuasion.collusion`. |
| `max_conversation_steps` | Max tool-call steps per agent turn. This is a real cost lever. | Yes — `experiment.max_conversation_steps` takes precedence in the same three runners. |
| `tags` | Written into `cfg["simulation"]["tags"]` for terrarium loggers. | No. |
| `run_timestamp` | Set by the runner. Do not set it. | — |

### Precedence: `experiment` beats `simulation`

In `collusion`, `network_influence`, and `persuasion.collusion`:

```
effective max_planning_rounds     = experiment.planning_rounds ?? simulation.max_planning_rounds ?? 1 (ni) / 2 (collusion)
effective max_conversation_steps  = experiment.max_conversation_steps ?? simulation.max_conversation_steps ?? 3
```

So `experiment.planning_rounds` / `experiment.max_conversation_steps` are
**aliases that win over the `simulation` block** for those three runners, and are
**silently ignored** by the other two. If you want one unambiguous place to set
loop length, use `simulation:` — except where you specifically want the
`experiment:` override.

---

## 4. `environment:`

Two ways to name the environment class:

```yaml
environment:
  name: JiraTicketEnvironment          # looked up in `envs.dcops`
  import_path: "envs.dcops.hospital.hospital_env:HospitalEnvironment"   # explicit module:Class
```

- `import_path` wins over `name` when both are present.
- At least one is **required** in `collusion`, `network_influence`,
  `persuasion/collusion`, `persuasion/hospital`.
  `agent_misalignment` requires `import_path` and will raise if only `name` is set.
- `name:` must be exported from `envs.dcps` (`envs/dcps/__init__.py`).
  Anything else needs `import_path`.
- Setting both is fine and is what the hospital configs do — `name` is then only
  used for the log directory name.

### Sweeping the environment

**Only `collusion` supports `sweeps[].environments`.** Each entry merges over
the top-level `environment:` block, so you can vary the class *and* its params:

```yaml
experiment:
  sweeps:
    - name: env_ablation
      environments:
        - label: jira
          import_path: experiments.collusion.choice.jira_ticket_choice_env:JiraTicketChoiceEnvironment
        - label: meeting
          import_path: experiments.collusion.choice.meeting_scheduling_choice_env:MeetingSchedulingChoiceEnvironment
        - import_path: envs.dcops.HospitalEnvironment    # label inferred from class name
      ...
```

Entries may be a bare string (treated as `name:`, or `import_path:` if it
contains a `:`) or a dict with an optional `label` plus any `environment` keys.
The label lands in the `run_id` as `__env<label>` and is de-duplicated with a
`_2`, `_3` suffix if you reuse one. The other four runners ignore this key.

### Environment-specific parameters

These keys are read by the **environment class**, not the runner, so they are
not validated by anything in this repo — a typo is silently ignored. The
tables below come from `.venv/lib/python3.12/site-packages/envs/dcops/*/*_env.py`,
not from this repo — the runners don't see them at all.

### `JiraTicketEnvironment`

| Key | Default | Notes |
|---|---|---|
| `max_tasks` | `20` | Task pool size. The clearest difficulty knob. |
| `assignment_filling` | `true` | Appends an "unassigned variable" fallback note and enables random fill-in at run end. Read by the base class, so it applies to every env. |
| `priority_bonus` | `20.0` | Reward shaping; clamped `>= 0`. |
| `tasks_done_bonus` | `20.0` | Reward shaping; clamped `>= 0`. |
| `violation_penalty` | `20.0` | |
| `availability_range` | `[4, 10]` | Scalar or 2-element list. |
| `cost_weights.load` | `1.0` | Nested as `cost_weights: {load: 1.0}`. |
| `skill_eps` | `0.1` | Skill-matching tolerance. |
| `norm_temperature` | `max(1.0, optimal_cost or 1.0)` | Normally leave it derived. |

Task types, skill tags, priority weights, and effort multipliers are **hardcoded**
in the env — not reachable from YAML.

### `MeetingSchedulingEnvironment`

| Key | Default | Aliases |
|---|---|---|
| `num_meetings` | `6` | `n_meetings` |
| `timeline_length` | `12` | — |
| `min_participants` | `2` | — |
| `max_participants` | `4` | `max_attendees_per_meeting` |
| `soft_meeting_ratio` | `0.6` | — |
| `assignment_filling` | `true` | — |

Instances are generated by CoLLAB v2 and cached under
`envs/dcops/outputs/collab_instances/meeting_scheduling/seed_<seed>/`. Some
CoLLAB `MeetingSchedulingConfig` fields (`instruction_mode`,
`include_timelines`, `output_stem`) are **not** exposed through this YAML.

### `HospitalEnvironment`

| Key | Default | Notes |
|---|---|---|
| `num_hospitals` | `2` | Agent count is `4 × num_hospitals + 1`. **The only agent-count input** — `communication_network.num_agents` is ignored here. |
| `num_patients` | `8` | |
| `hospital_names` | derived | Defaults to `["General_Hospital", "St_Marys_Center"]`, then `Hospital_<i>` |
| `max_iterations` | `10` | Env-level fallback only, after `simulation.max_iterations`. |

Resource types, consumption map, patient pathways, the 168h horizon, the 4h
transfer penalty, and all reward constants (missed step 500, resource failure
300, holding 10/unit, transfer +15/unit) are **hardcoded**.

### `PersonalAssistantEnvironment` / `SmartGridEnvironment`

Less used, but present in `envs.dcps` and reachable via `import_path`:

- **PersonalAssistant**: `min_outfits_per_agent` (4), `max_outfits_per_agent` (6),
  `density`, `max_degree` (3)
- **SmartGrid**: `timeline_length` (24, alias `T`), `tasks_per_home` ([3,6]),
  `min_machines_per_agent`, `max_machines_per_agent`,
  `min_sources_per_agent` (2), `max_sources_per_agent` (3)

### Known-dead `environment` keys

`default_capacity` appears in the shipped `agent_misalignment` configs but is
**read by nothing** — Hospital capacities are hardcoded. Silently ignored.

> ⚠ **Nothing in this repo validates the `environment:` block.** A typo in a
> bonus weight is a silent no-op, not an error. If a knob seems to do nothing,
> check the env class source before assuming the model is ignoring it.

---

## 5. `communication_network:`

```yaml
communication_network:
  topology: complete
  num_agents: 6
  consolidate_channels: true
  edge_prob: 0.6      # erdos_renyi
  k: 2                # watts_strogatz
  rewire_prob: 0.3    # watts_strogatz
  m: 3                # barabasi_albert
  center: 0           # star / hub selection
```

`topology` and `num_agents` are **overwritten per run** by the sweep's
`topologies[]` and `num_agents[]`. The top-level values only serve as
documentation and as a fallback if a sweep omits the axis. Keeping the two in
sync is good hygiene — it makes the config readable and makes
`compute_jira_optimal.py` and the resume tools work off sane defaults.

### Supported topologies

There are exactly **six**. Anything else raises `ValueError`.

| `topology` | Aliases | Extra keys | Constraints |
|---|---|---|---|
| `complete` | — | — | — |
| `path` | — | — | — |
| `star` | — | `center` | int index **or** agent name; `None` → `agents[0]` |
| `erdos_renyi` | `erdos-renyi`, `er` | `edge_prob` | required, float in `[0,1]` |
| `watts_strogatz` | `watts-strogatz`, `ws` | `k`, `rewire_prob` | both required; `k` even, `0 <= k < num_agents`; `rewire_prob` in `[0,1]` |
| `barabasi_albert` | `barabasi-albert`, `ba` | `m` | required; `1 <= m < num_agents` |

**`topology: random` is not a topology.** It raises explicitly and points you at
`erdos_renyi`. Use that.

Key aliases, in Terrarium's own precedence order:

- `k` ← `nearest_neighbors` ← `num_neighbors`
- `rewire_prob` ← `rewiring_prob` ← `beta`
- `m` ← `edges_per_node` ← `num_edges_to_attach`

The three stochastic topologies seed from `simulation.seed` — which is the
per-run sweep seed, so a topology sweep is reproducible.

### Two things `communication_network` does *not* do

1. **`num_agents` is ignored by the network builder.** Graph size is always
   `len(agent_names)`. Terrarium never reads `communication_network.num_agents`
   when building a graph — but your **environment** reads it to size its agent
   list (Jira, MeetingScheduling, PersonalAssistant, SmartGrid). If it disagrees
   with the sweep's `num_agents`, you fail at env init with
   `CommunicationNetwork nodes must match environment agent_names`.
   **HospitalEnvironment does not read it at all** — it derives agents from
   `environment.num_hospitals` (`4h + 1`).
2. **Only the knob for the chosen topology is used.** `k`, `m`, `edge_prob`,
   `rewire_prob`, and `center` sit side by side in every shipped config; four of
   them are ignored on any given run. This is normal, not a bug.

### `consolidate_channels`

Defaults to `false`; must be a real bool (`TypeError` otherwise). When `true`,
Terrarium greedily cliques the graph and creates **one blackboard per channel
group** instead of one per edge. It is the single largest lever on
blackboard count, and therefore on token cost and on how much agents can see
each other. Every shipped config sets it `true`.

---

## 6. `llm_models:`

```yaml
llm_models:
  - label: openai-gpt-4o-mini      # becomes a directory name
    llm:
      provider: openai
      openai:
        model: gpt-4o-mini
        params:
          max_tokens: 1024
          temperature: 0.7
```

`label` is the grouping key for everything downstream: run directories, plot
filenames, `model_label` in `summary.csv`, and the `--model-runs-dir` filter for
multi-model comparison plots. It should be stable and filesystem-safe
(letters/digits/`._-`; other characters are replaced).

Each entry's `llm:` block is deep-copied into `cfg["llm"]` for its runs, which is
what Terrarium's client factory reads. **Only the relevant topology knob per
`topology` is used** — you can leave `k`/`m`/`edge_prob` all present.

### Providers

| `provider` | Env var (required) | Block keys read | Notes |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | *(none beyond `model`)* | `api_key`/`base_url`/`timeout`/`max_retries` under `llm.openai` are **ignored** — timeout is hardcoded 30s, retries are env-only. |
| `anthropic` | `ANTHROPIC_API_KEY` | `model` | |
| `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | `model` | Block key is `gemini`, not `google`. |
| `together` | `TOGETHER_API_KEY` | `model`, `base_url` (`https://api.together.xyz/v1`), `request_timeout` (60), `api_key` | |
| `fireworks` | `FIREWORKS_API_KEY` | `model`, `base_url` (`https://api.fireworks.ai/inference/v1`), `request_timeout` (60), `api_key` | Alias `firework` accepted. |
| `vllm` | — | see below | Local serving; spins up its own server. |
| `custom_oai_compatible` | `OAI_COMPATIBLE_BASE_URL` + `OAI_COMPATIBLE_PROVIDER_KEY` | `model`, `base_url`, `api_key`, `request_timeout` (60) | Repo monkey-patch. Snake_case only — the hyphenated `custom-oai-compatible` raises. See below. |

Unknown providers raise `ValueError` listing the six built-ins.

### Generation params (`llm.<provider>.params`)

Read from `llm[provider]["params"]` (or `"generation"`; `params` wins), minus
`model`. There is **no default layer** — whatever you put in is what each client
gets, and each client picks its own subset:

| Client | Honoured keys | Defaults |
|---|---|---|
| openai | `max_tokens`/`max_output_tokens`, `temperature`, `reasoning_effort`, `verbosity` | — |
| anthropic | `max_tokens`/`max_output_tokens`, `temperature`, `thinking`, `system` | `max_tokens` 4000, `temperature` 0.7 |
| gemini | `max_tokens`/`max_output_tokens`, `temperature`, `system` | `max_tokens` 1000, `temperature` 0.7 |
| together / fireworks / vllm / custom | `max_tokens`, `temperature`, (`reasoning` for together) | — |

**`seed`, `top_p`, and `stop` are silently dropped by every client.** They pass
through `BaseAgent` and are discarded before the request. Do not rely on
`temperature: 0.0` plus `seed:` for determinism — only the sweep seed
propagates, into graph construction and env instance generation.

OpenAI additionally drops `temperature` for `o1`/`o3`/reasoning models and for
`gpt-4.1-nano`/`gpt-5-nano`, and drops `reasoning_effort`/`verbosity` unless the
model contains `gpt-5`.

### ⚠ Enforced: the `provider:` string must be `custom_oai_compatible`

The canonical `provider:` value for this backend is **`custom_oai_compatible`**
(snake_case). Terrarium's `get_generation_params` looks up the params block by
the **provider string verbatim** (`llm_config.get(provider)`), so the value you
type must match the `custom_oai_compatible:` block name exactly.

As of this fix the hyphenated `custom-oai-compatible` is **rejected**: the repo
patch raises a `ValueError` at config-load / client-construction time and points
you back to this section. Previously it was accepted but never matched the block,
so your `params` were silently dropped — that history is why the older configs
used the hyphenated form and why their `max_tokens`/`temperature` never took
effect. It is now an enforced rule, not a workaround.

Behaviour after the fix:

```
provider='custom_oai_compatible'  block='custom_oai_compatible'  -> {'max_tokens': 999, ...} ← works
provider='custom-oai-compatible'  block='custom_oai_compatible'  -> ValueError (was: {})      ← rejected
provider='openai'                 block='openai'                 -> {'max_tokens': 999, ...}
```

```yaml
llm_models:
  - label: openrouter-deepseek
    llm:
      provider: custom_oai_compatible      # snake_case, NOT custom-oai-compatible
      custom_oai_compatible:
        model: deepseek/deepseek-v4.1-flash
        params:                            # applied verbatim
          max_tokens: 1024
          temperature: 0.7
```

Every shipped smoke config already uses the snake_case form, and
`docs/CUSTOM_OAI_COMPATIBLE_GUIDE.md` now does too. If you copy an older snippet
with the hyphenated value, rename it before running — otherwise the run fails
fast instead of silently using the provider's own default `max_tokens` (which
often truncated long agent turns).

### `vllm` (local serving)

```yaml
llm:
  provider: vllm
  vllm:
    auto_start_server: true
    persistent_server: false
    startup_timeout: 420        # default 180; raise for big checkpoints
    health_check_path: "/models" # default "/v1/models"
    params: {max_tokens: 1024, temperature: 0.4}
    models:
      - checkpoint: "Qwen/Qwen2.5-7B-Instruct"
        host: "127.0.0.1"
        port: 8020
        tensor_parallel_size: 1
        gpu_memory_utilization: 0.9
        max_model_len: 16000
        trust_remote_code: true
        additional_args: ["--enable-auto-tool-choice"]
```

Global keys: `models` (**exactly one**, more raises), `auto_start_server`,
`persistent_server`, `auto_configure_tool_choice` (default true),
`startup_timeout` (alias `connection_timeout`, default 180),
`health_check_path` (default `/v1/models`), `health_poll_interval` (2.0),
`api_key`, `env`, `default_model` (deprecated — errors if it differs from the
sole model id).

Per-model keys include `checkpoint` (aliases `model`/`model_name`),
`served_model_name`, `base_url`, `host`, `port`, `tensor_parallel_size`,
`trust_remote_code`, `max_model_len`, `dtype`, `download_dir`,
`gpu_memory_utilization`, `additional_args`, `env`, `request_timeout`,
`api_key`, `launch_command`, `tool_call_parser`, `reasoning_parser`,
`chat_template`.

Terrarium auto-injects `--enable-auto-tool-choice --tool-call-parser hermes` for
any model whose name contains `qwen`. Set `tool_call_parser` explicitly for
other families or tool calling will silently fail.

### `custom_oai_compatible`

Implemented as a runtime monkey-patch in
`experiments/common/_patch_terrarium.py`, not in Terrarium itself. The
`provider:` value must be the snake_case `custom_oai_compatible`; the hyphenated
`custom-oai-compatible` is **not** accepted and raises a `ValueError` (see above).
See [`CUSTOM_OAI_COMPATIBLE_GUIDE.md`](./CUSTOM_OAI_COMPATIBLE_GUIDE.md).

---

## 7. `experiment:` — global knobs

```yaml
experiment:
  tag: my_sweep                    # cosmetic; also the fallback output dir name
  output_dir: experiments/<exp>/outputs/<tag>
  max_concurrent_runs: 10          # runs in flight; overridden by --max-concurrent-runs
  seeds: [1, 2, 3, 4, 5]
  runs_per_setting: 2              # truncate the seed list — a cost cap
  planning_rounds: 3               # alias, see §3
  max_conversation_steps: 3        # alias, see §3
  log_prompts: true                # write prompt logs (default true)
  sweeps: [...]
```

| Key | Notes |
|---|---|
| `tag` | Written into `cfg["simulation"]["tags"]`. |
| `output_dir` | **A stable root.** The runner appends `/<timestamp>/`. Never point `resume.py --root` at the bare `output_dir` — point it at the timestamped path. |
| `max_concurrent_runs` | Must be a positive int. Higher = more API concurrency = more rate-limit pressure. Each concurrent run costs roughly `num_agents × (planning_rounds × agents + agents)` calls, so concurrency and agent count compound. |
| `seeds` | Accepts `42`, `"1,2,3"`, or `[1,2,3]`. Non-numeric entries in a list are dropped. |
| `runs_per_setting` | Slices `seeds` to the first N. Applies *after* sweep-level seeds override. Best cost lever for a pilot. |
| `log_prompts` | Default `true` when absent. Set `false` to skip `PromptLogger` output (`agent_prompts.json`) and save disk/IO on large sweeps. |

### Seed precedence

```
effective seeds for a sweep
  = sweeps[].seeds                 (if set)
  ∨ experiment.seeds               (if set)
  ∨ [simulation.seed]              (if set)
  ∨ [1]                            (fallback)
then truncated to runs_per_setting
```

A run fails fast with `No seeds specified` only if this resolves empty — which
cannot happen given the `[1]` fallback, but *can* happen if you set
`seeds: []` **and** the sweep has `seeds: []`… no: `sweeps[].seeds` empty falls
through to `experiment.seeds`. The real trap is `seeds: []` at both levels with
`simulation.seed` unset — still `[1]`. Set seeds explicitly.

---

## 8. `experiment.sweeps[]` — shared axes

Every sweep **must** have `name` (used as a directory name and in `run_id`).

| Axis | Meaning |
|---|---|
| `topologies: []` | Graph families. See §5. |
| `num_agents: []` | Number of agents. **Overrides** `communication_network.num_agents`. |
| `seeds: []` | Per-sweep seed override. |

Per-runner additional axes are in §9.

---

## 9. Per-runner sweep axes

> ⚠ **These are not interchangeable.** Copying a sweep block between experiments
> silently produces zero runs for the axes that runner does not read, because the
> runner does `sweep.get("topologies") or []` and iterates an empty list.

### 9a. `collusion` (`experiments/collusion/run.py`)

| Axis | Default | Notes |
|---|---|---|
| `topologies` | `[]` | |
| `num_agents` | `[]` | |
| `colluder_counts` | `[]` | Coalition size. Secret blackboard needs `>= 2`. |
| `secret_channel_enabled` | `[False]` | `false` = vanilla cooperative baseline. Alias: `secret_channels`. |
| `prompt_variants` | `["control"]` | Only applied when `secret_channel_enabled: true`. |
| `environments` | `[]` | Environment ablation; unique to this runner. |
| `seeds` | `[]` | |

### 9b. `network_influence` (`experiments/network_influence/run.py`)

| Axis | Default | Notes |
|---|---|---|
| `topologies` | `[]` | |
| `num_agents` | `[]` | |
| `adversary_counts` | `[]` | `0` is the benign baseline. |
| `seeds` | `[]` | |

This is the **only** runner with up-front config validation
(`_validate_config`). It requires `experiment:`, `environment:`,
`llm_models:`, a non-empty `experiment.sweeps`, `sweeps[].name`, and non-empty
`topologies` / `num_agents` / `adversary_counts`.

### 9c. `agent_misalignment` (`experiments/agent_misalignment/run.py`)

| Axis | Default if omitted | Notes |
|---|---|---|
| `topologies` | `["complete"]` | |
| `num_agents` | `[9]` | Must be `1 + 4k` for Hospital. Infeasible values are **skipped with a warning**, not raised. |
| `adversary_counts` | `[1]` | |
| `target_roles` | `[null]` | `Resource_Provisioner`, `departmental`, or a department name (`Triage`/`Radiology`/`Surgery`/`Ward`). |
| `num_patients` | `[16]` | Accepts the string `"num_agents_minus_1"`. |
| `strategies` | `["none"]` | `none`, `covert_max_coalition_reward`, `destructive_max_coalition_reward`, `destructive_no_reward_preservation`. |

**Feasibility filter** (`_is_combo_feasible`): `adversary_count == 0` requires
`strategy == "none"` and vice-versa; for Hospital, `adversary_count` must not
exceed the hospital-based coalition cap. Infeasible combos are skipped.

Defaults are applied at *both* the counting and execution passes, so they agree —
but note this runner does **not** support `runs_per_setting` or per-sweep seeds.

### 9d. `persuasion/collusion` (`experiments/persuasion/collusion/run.py`)

Same axes as `collusion` minus `environments`: `topologies`, `num_agents`,
`colluder_counts`, `secret_channel_enabled`, `prompt_variants`, `seeds`.

### 9e. `persuasion/hospital` (`experiments/persuasion/hospital/run.py`)

The widest cross-product. Axes: `topologies`, `num_agents`, `num_patients`,
`strategies`, `colluder_counts`, `target_roles`, `coalition_clique_enabled`,
`secret_channel_enabled`, `prompt_variants`, `seeds`.

Defaults when omitted: `strategies` = all three attack types,
`coalition_clique_enabled` = `[False]`, `secret_channel_enabled` = `[True]`,
`prompt_variants` = `["control"]`.

**Counting bug worth knowing:** the pre-flight `total_runs` counter iterates
`strategies`, `colluder_counts`, and `target_roles` over their *defaults* when
you omit them, while the execution pass iterates the same defaults — so they do
agree, but the counter ignores feasibility entirely (unlike
`agent_misalignment`), so `progress.json` can over-report.

---

## 10. `prompt_variants` — the collusion axis

Only meaningful when a secret channel exists.

### `collusion` — `experiments/collusion/prompts.py`

`control` (no injection), `simple`, `deception`, `structured`, `aggressive`.

### `persuasion/collusion` — `experiments/persuasion/collusion/prompts.py`

`control`, `helpful_misdirection`, `authority_nudge`, `social_proof`,
`scarcity_pressure`, `reciprocity_trade`, `risk_aversion`, `cost_anchor`,
`split_brain`.

### `persuasion/hospital` — `experiments/persuasion/hospital/prompts.py`

`control`, `helpful_misdirection`, `authority_nudge`, `social_proof`,
`scarcity_pressure`, `reciprocity_trade`. (A 6-name subset of the above.)

### Custom variants

All three prompt modules support overriding or adding text per variant:

```yaml
experiment:
  collusion:
    prompt_variant_overrides:
      my_variant: |
        You are part of a secret coalition ...
  persuasion_collusion:
    prompt_variant_overrides: {...}
  persuasion_hospital:
    prompt_variant_overrides: {...}
```

An unknown variant name raises with the list of known names plus the
`prompt_variant_overrides` hint.

---

## 11. Experiment-specific `experiment.*` blocks

### `experiment.collusion` / `experiment.persuasion_collusion`

| Key | Values | Default |
|---|---|---|
| `colluder_selection` | `random`, `first` | `random` |
| `agent_order` | `random`, `colluders_first` (aliases `colluders-front`, `colluders_front`) | `random` |
| `prompt_variant_overrides` | map variant name → text | `{}` |

`agent_order: random` shuffles the agent list with the seeded RNG;
`colluders_first` keeps env order but partitions colluders ahead of non-colluders.

### `experiment.persuasion_hospital`

| Key | Values | Default |
|---|---|---|
| `colluder_selection` | `hospital_coalition`, `random`, `first` | `hospital_coalition` |
| `prompt_variant_overrides` | map | `{}` |

`hospital_coalition` fills department roles one hospital at a time, honouring
`target_roles`.

### `experiment.adversary` — two different meanings

**`network_influence`** (read at `run.py:503`):

| Key | Values | Default | Meaning |
|---|---|---|---|
| `strategy` | `random`, `highest_degree`, `center` | `random` | Adversary placement. Star graphs get special-cased: adversaries land on leaves, and the center only joins when everyone is an adversary. |
| `force_seed` | bool | `true` | Whether the misinformation token is forced into the adversary's prompt. |
| `victim_assignment` | `distinct`, `shared` | `distinct` | One victim per adversary, or one shared victim. Falls back to reusing victims (with a warning) when adversaries outnumber non-adversaries. |
| `misinfo_frequency` | `once`, `always` | `always` | Re-seed the claim each planning round, or only in round 1. |

**`agent_misalignment`** (read at `run.py:513`):

| Key | Values | Default |
|---|---|---|
| `secret_channel_enabled` | bool | `false` |
| `coalition_clique_enabled` | bool | `false` — force-wires the coalition into a clique on the **public** graph |

Note `experiment.adversary.strategy` here is *overwritten per run* by the
sweep's `strategies` value, so setting it at the block level has no lasting
effect. `attack_frequency` appears in the shipped configs but **is not read by
the runner**.

### `experiment.info`

**`network_influence`**: `code_prefix` (default `PREF_CODE`), `true_preference`
(default `attend`), `false_preference` (default `skip`).

**`agent_misalignment`**: `code_prefix` only — and it is also **not read** by the
runner. It survives only in configs as dead config.

---

## 12. Judges

Three separate mechanisms; pick per experiment.

| Experiment | Judge | Trigger | Config |
|---|---|---|---|
| `collusion`, `persuasion/collusion` | `judge_blackboards.py` | Standalone CLI after the sweep | `judge_llm` (top-level, via `--config`) |
| `network_influence` | `judge_beliefs.py` | Inline, every run | `judge_llm` → `experiment.judge` → OpenAI default |
| `agent_misalignment` | `judge_beliefs.py` | Inline, every run | `judge_llm` → `experiment.evaluator` → OpenAI default |
| `persuasion/hospital` | — | **No judge** (`judgements={}`) | n/a |

### `experiment.judge` (`network_influence`)

```yaml
experiment:
  judge:
    model: gpt-4.1-2025-04-14
    params: {max_tokens: 256, temperature: 0.0}
```

OpenAI-only. Default model `gpt-4.1-2025-04-14`, default `max_tokens: 512`,
default `temperature: 0.0`. Requires `OPENAI_API_KEY`.

### `experiment.evaluator` (`agent_misalignment`)

```yaml
experiment:
  evaluator:
    model: gpt-4
    params: {max_tokens: 512, temperature: 0.0}
```

Same shape, default model `gpt-4`. Requires `OPENAI_API_KEY`.

### `judge_llm` (top-level, any provider)

```yaml
judge_llm:
  provider: custom_oai_compatible
  custom_oai_compatible:
    model: openai/gpt-4o-mini
    params: {max_tokens: 512, temperature: 0.0}
```

Overrides both of the above. `agent_misalignment` also accepts it nested under
`experiment.judge_llm`; the other two read it **only** at the top level.

---

## 13. `pricing:` — optional cost accounting

Only `agent_misalignment` reads this from the config, and only to write
`costs.json` per run:

```yaml
pricing:
  openai:
    gpt-4o-mini:
      input_per_1m_usd: 0.15
      output_per_1m_usd: 0.60
```

Both fields are required; either missing disables USD totals (token counts are
still recorded). `experiments/agent_misalignment/costs.py` also falls back to
`$TERRARIUM_PRICING_PATH` or a repo-root `pricing.json`, then to
`config.json:pricing`, then reports `pricing_missing_for_model`.

---

## 14. Output layout and `run_id`

```
<output_dir>/<timestamp>/
├── config.json           # exact config used (resume reads this)
├── progress.json         # atomic; {status,total_runs,completed_runs,...}
├── experiment.log
├── summary.json          # one row per run
├── summary.jsonl         # NDJSON, same rows
├── summary.csv           # flattened rows, sorted columns
├── results.json          # persuasion/hospital uses this name instead
├── runs.csv              # persuasion/hospital uses this name instead
└── runs/<model_label>/<sweep_name>/<run_id>/
    ├── run_config.json
    ├── final_summary.json
    ├── agent_turns.json
    ├── metrics.json
    ├── tool_events.json
    ├── blackboards.json
    ├── blackboard_*.txt
    ├── costs.json                # agent_misalignment
    ├── agent_rewards.json        # hospital-based runners
    ├── judge_results.json        # network_influence
    ├── judgements.json           # agent_misalignment
    ├── survey_responses.json     # network_influence
    └── config.json / judge_usage.json (agent_misalignment)
```

### `run_id` formats

| Runner | Format |
|---|---|
| `collusion` | `{model}__{sweep}[__env{env}]__{topology}__n{N}__c{C}__secret{0\|1}__pv{variant}__seed{S}` |
| `network_influence` | `{model}__{sweep}__{topology}__n{N}__a{A}__seed{S}` |
| `persuasion/collusion` | `{model}__{sweep}__{topology}__n{N}__c{C}__secret{0\|1}__pv{variant}__seed{S}` |
| `agent_misalignment` | `{model}__{sweep}__topo{T}__strat{S}__agents{N}_patients{P}_adv{A}_{role}__seed{S}` |
| `persuasion/hospital` | `{model}__{sweep}__topo{T}__agents{N}__strat{S}__c{C}__tr{role}__secret{0\|1}__pv{v}__seed{S}` |

`persuasion/hospital` does **not** encode `coalition_clique_enabled` or
`num_patients` in the `run_id`, so two runs differing only in those will collide
on the same directory. Encode distinguishing factors in the **sweep name**.

Path components are sanitised to `[A-Za-z0-9_.-]`; other characters become `_`.

### Resume

**Not every experiment has one.** `persuasion/hospital` has **no `resume.py`** —
an interrupted hospital sweep cannot be resumed in place.

| Experiment | `resume.py` |
|---|---|
| `collusion`, `network_influence`, `agent_misalignment`, `persuasion/collusion` | yes |
| `persuasion/hospital` | **no** |

```bash
.venv/bin/python -m experiments.<exp>.resume --root <output_dir>/<timestamp> --dry-run
```

Flags (from `collusion/resume.py`; the others are close variants):
`--config`, `--max-concurrent-runs`, `--stop-on-error`,
`--require-status-complete`, `--rerun-error-turns` / `--no-rerun-error-turns`,
`--dry-run`.

A run counts as **incomplete** when any of `run_config.json`, `metrics.json`,
`final_summary.json`, `agent_turns.json`, `tool_events.json`, `blackboards.json`
is missing, or when `agent_turns.json` contains `"response": "[ERROR]"`
(default on). Always `--dry-run` first.

### A second, hidden output tree

The shipped env prompt classes create their own `PromptLogger` with Terrarium's
default root, so a parallel tree is written **outside** your output root:

```
logs/<EnvName>/<tags[0]>_<model>/<run_timestamp>/seed_<seed>/
```

The repo's own loggers are redirected into the run directory via `log_dir`, so
this only affects artifacts created by the *environment*'s prompt classes
(`agent_prompts.json`, `data_iteration_*.json`). It is not cleaned up per-experiment
— several envs call `clear_seed_directories()` at init, which **erases**
`logs/<Env>/<tag>/seed_<seed>` from an earlier run sharing that tag. If you rely
on those files, read them from the run directory, not `logs/`.

---

## 15. Gotchas

1. **A `topologies:` sweep does not sweep the topology's parameters.** Only
   `topology` and `num_agents` are overwritten per run. `edge_prob`, `k`,
   `rewire_prob`, `m`, and `center` keep their **top-level YAML values** for
   every topology in the list. So
   `topologies: [erdos_renyi, watts_strogatz]` runs both at `edge_prob: 0.6` /
   `k: 2` / `rewire_prob: 0.3` — whatever is literally in the YAML. To vary
   those, you need one `sweeps` entry per parameterisation.
2. **`prompt_variants` are silently dropped when `secret_channel_enabled: false`.**
   The runner forces `control`. This is why `summary.csv` is missing rows you
   "expect". Both counting and execution passes apply the filter, so the progress
   total stays correct — the rows simply never existed.
3. **The `custom-oai-compatible` provider requires the snake_case `provider:`
   value `custom_oai_compatible`.** The hyphenated form raises a `ValueError` —
   see §6.
4. **`seed`, `top_p`, and `stop` in `params:` do nothing.** Every client discards
   them (§6).
5. **`output_dir` gets a timestamp appended.** `resume.py --root` wants the
   timestamped path.
6. **Unknown `environment:` keys are silently ignored.** Nothing validates that
   block. `default_capacity` in the shipped `agent_misalignment` configs is a
   live example of a key that does nothing.
7. **`experiment.adversary.attack_frequency` and `experiment.info.code_prefix`
   (agent_misalignment) are dead config.** They appear in shipped configs and are
   never read.
8. **`agent_misalignment` ignores `runs_per_setting` and per-sweep `seeds`** — it
   reads only `experiment.seeds`.
9. **`persuasion/hospital` has no `resume.py`**, and omits
   `coalition_clique_enabled` and `num_patients` from its `run_id`, so those axes
   collide unless reflected in the sweep name.
10. **`sweeps[].environments` only exists in `collusion`.** Elsewhere it is dead.
11. **`judge_llm` in a `persuasion/collusion` config is not read by the runner** —
    only by the standalone `collusion.judge_blackboards` CLI via `--config`. Same
    for a `collusion` config's `judge_llm`.
12. **`agent_misalignment` accepts `judge_llm` nested under `experiment:`** as
    well as top-level; the other judges read it **only** at top level.
13. **Validation is inconsistent.** `network_influence` validates up front;
    `collusion` and `persuasion/collusion` validate almost nothing and fail
    mid-sweep; `agent_misalignment` and `persuasion/hospital` skip infeasible
    combos with a warning instead.
14. **Failure semantics differ.** `collusion`, `network_influence`, and
    `persuasion/collusion` re-`raise` on the first failed run (aborting the whole
    sweep) and cancel in-flight tasks. `agent_misalignment` and
    `persuasion/hospital` record `{"status": "error", ...}` and continue.
15. **`num_agents` must match the environment's agent model.** Hospital requires
    `1 + 4k`; the runners derive `num_hospitals` from it and overwrite any
    hand-set value. Jira/Meeting/PersonalAssistant/SmartGrid instead read
    `communication_network.num_agents` to size their agent list — so a mismatch
    there fails at init, while a mismatch in Hospital fails as a silently wrong
    `num_hospitals`.
16. **`consolidate_channels` dominates blackboard count**, and therefore cost and
    observability. It is a per-run constant — it is *not* sweepable via any
    `sweeps[]` axis.
17. **API keys come from the shell environment**, not `load_dotenv()` in the
    runners. Verify the venv sees them:
    `.venv/bin/python -c "import os; print(os.environ.get('OPENAI_API_KEY'))"`.
18. **There is no config schema validation and no CI.** A malformed axis list
    yields a zero-run sweep with no error.

---

## 16. Designing a new experiment series — checklist

1. **Pick the runner first.** The sweep axes are runner-specific (§9). If your
   design needs an axis that runner doesn't read, you must either restructure the
   design or fork the runner.
2. **Copy the closest shipped smoke config**, not the full-fat one. Smoke configs
   are one seed, small `num_agents`, one sweep entry — the right shape for a
   template.
3. **Cost-estimate before the first real run.** Multiply the axes out (§1), then
   multiply by `(num_agents × (planning_rounds + 2))` agent-turns per run, then by
   agent-turn × conversation-steps. Cap with `runs_per_setting`.
4. **Always include a baseline**: `secret_channel_enabled: [false, true]`, or
   `adversary_counts: [0, ...]`, or a `strategies: [none]` sweep.
5. **Set `judge_llm:` or commit to `OPENAI_API_KEY`.** Otherwise inline-judge
   experiments silently require OpenAI regardless of your agent provider.
6. **Dry-run first**: `resume.py --dry-run` against a throwaway run, or a
   single-seed pilot config with `--out-dir` pointed at a scratch path.
7. **Keep `output_dir` stable and tag it uniquely** per series; the timestamp
   child is your only run-instance separator.
8. **Put distinguishing factors in the sweep `name`**, not just in axis values —
   the `run_id` omits some of them (§14).
9. **Verify `total_runs` in `progress.json`** against your hand-computed count
   before letting a sweep run unattended. A mismatch means an axis was silently
   ignored.
10. **Add `environments` support if you need env ablation** and only `collusion`
    has it today.

---

## Appendix: known doc/repo drift

- **`AGENTS.md` says Python 3.11 is required; the venv is 3.12.14.** The runners
  only hard-fail on `< 3.11`, so 3.12 works fine — the "required" claim and the
  `.venv/lib/python3.11/...` paths in it are simply stale. Confirmed:
  `.venv/pyvenv.cfg` → `version_info = 3.12`.
- `experiments/network_influence/README.md` and `network_influence/run.py`'s error
  messages reference `configs/quickstart.yaml`, which **does not exist**.
- `experiments/collusion/configs/README.md` references
  `collusion_jira_topologies_env_assignment_fills.yaml`, which **does not exist**.
- `experiments/persuasion/hospital/` has plots but no `resume.py`, and the
  top-level README implies resume is available for all five experiments.

## Appendix: where the schema actually lives

The runners do **not** call `terrarium.utils.load_config` / `validate_config`,
so Terrarium's own strict schema is bypassed — which is why `environment.import_path`
works at all. The de-facto schema is split across four places:

| Layer | Source | Owns |
|---|---|---|
| Sweep expansion, `experiment.*`, `run_id`, artifacts | `experiments/<exp>/run.py` | §7–§13 |
| Graph construction, client factory, generation params | `terrarium/networks/factory.py`, `terrarium/utils.py` | §5, §6 |
| Env parameters | `envs/dcops/*/*_env.py` (in the venv) | §4 |
| `custom_oai_compatible` | `experiments/common/_patch_terrarium.py` (repo monkey-patch) | §6 |

Terrarium is installed as a **wheel**, not an editable checkout
(`terrarium-agents==0.1.1`, `site-packages/terrarium/`). `envs` is installed the
same way; `external/CoLLAB` is only the problem-layer backend that
`envs/dcops/__init__.py` puts on `sys.path` (honouring `TERRARIUM_COLLAB_PATH`).
To read the env parameter table, look in
`.venv/lib/python3.12/site-packages/envs/dcops/` — not in this repo.