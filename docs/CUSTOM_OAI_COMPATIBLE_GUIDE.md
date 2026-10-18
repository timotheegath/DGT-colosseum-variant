# Using the `custom-oai-compatible` provider

This guide shows how to run colosseum experiments — including their LLM-judges —
against any endpoint that speaks the OpenAI chat-completions API
([OpenRouter](https://openrouter.ai), self-hosted vLLM with an OpenAI shim,
OpenAI-compatible proxies, etc.) using the `custom-oai-compatible` provider slot.

It assumes you have:

- a working colosseum checkout on branch `feature/openrouter-support`,
- a Python 3.11 virtualenv at `.venv/` (see repo `AGENTS.md`).

No terrarium fork or post-install patching is required: the provider is
implemented as a runtime monkey-patch in `experiments/common/_patch_terrarium.py`
(committed to this repo) and activated automatically at import time.

---

## 1. Configure the provider via environment variables

Add to your `.env` (or export in your shell):

```
OAI_COMPATIBLE_PROVIDER_KEY=<the key for your endpoint>
OAI_COMPATIBLE_BASE_URL=<the chat-completions base URL>
```

Concrete examples:

| Provider     | `OAI_COMPATIBLE_BASE_URL`                | `OAI_COMPATIBLE_PROVIDER_KEY`            |
|--------------|------------------------------------------|------------------------------------------|
| OpenRouter   | `https://openrouter.ai/api/v1`           | `sk-or-v1-...`                           |
| Self-hosted  | `http://localhost:8000/v1`               | whatever your server expects             |
| Other proxy  | whatever the operator gives you          | whatever the operator gives you          |

The new client (`CustomOAICompatibleClient`) reads both env vars in its
`__init__`. Either or both can also be overridden per-config via the
`custom_oai_compatible:` block in YAML (see §3 below). The env-var path is the
recommended default — keep YAML provider-agnostic so you can switch endpoints
without editing configs.

`load_dotenv(override=True)` runs at client construction (mirrors
`FireworksClient`), so a `.env` value wins over a shell-exported one when both
are set.

---

## 2. Minimal smoke config (agents only)

A minimal agent block looks like this:

```yaml
llm_models:
  - label: openrouter-gpt-4o-mini
    llm:
      provider: custom-oai-compatible
      custom_oai_compatible:
        model: openai/gpt-4o-mini      # whatever id your endpoint expects
        params:
          max_tokens: 1500
          temperature: 0.7
```

Things to notice:

- The block name under `llm:` is `custom_oai_compatible` (snake-case).
- The `provider:` string is `custom-oai-compatible` (hyphenated) — both forms
  are accepted by terrarium's dispatch.
- `base_url` and `api_key` are deliberately **absent**. They come from the env
  vars in §1, so the same YAML works against any endpoint.
- `model` must be present in the YAML — it's not configurable via env vars
  because it's a per-experiment choice, not a deployment setting.
- `params` are passed straight to the chat-completions request body
  (`temperature`, `max_tokens`, `top_p`, etc.).

Five ready-to-use smoke configs ship with this branch:

- `experiments/collusion/configs/collusion_jira_openrouter_smoke.yaml`
- `experiments/network_influence/configs/misinformation_prop_openrouter_smoke.yaml`
- `experiments/agent_misalignment/configs/agent_misalignment_openrouter_smoke.yaml`
- `experiments/persuasion/collusion/configs/persuasion_collusion_jira_openrouter_smoke.yaml`
- `experiments/persuasion/hospital/configs/persuasion_hospital_openrouter_smoke.yaml`

Each one is a minimal version of its full-fat sibling: small agent count, 1
seed, single sweep setting. Copy one and edit it to make your own.

---

## 3. Routing the judge through the same provider

The judge step is what decides whether an agent's behaviour counts as
collusion / misinformation / misalignment / etc. It runs **after** the
simulation and is implemented in three modules:

| Experiment               | Judge code                                  | Triggered by                         |
|--------------------------|---------------------------------------------|--------------------------------------|
| `collusion`              | `experiments/collusion/judge_blackboards.py`| Standalone CLI: `python -m ...`      |
| `network_influence`      | `experiments/network_influence/judge_beliefs.py` | Inline in `run.py` at end of each run |
| `agent_misalignment`     | `experiments/agent_misalignment/judge_beliefs.py` | Inline in `run.py` at end of each run |
| `persuasion/collusion`   | (re-uses `collusion/judge_blackboards.py`)  | Standalone CLI                       |
| `persuasion/hospital`    | (re-uses `agent_misalignment/judge_beliefs.py`) | Inline in `run.py`                |

Historically all three judges hard-coded `provider: openai`, which forced
users to also set `OPENAI_API_KEY` even when their agents ran on a different
provider. That constraint is now gone. Each judge reads an optional top-level
`judge_llm:` block from the experiment YAML.

### 3a. Inline judges (`network_influence`, `agent_misalignment`, `persuasion/hospital`)

Just add a top-level `judge_llm:` block to your YAML:

```yaml
experiment:
  tag: my_experiment
  ...

judge_llm:
  provider: custom-oai-compatible
  custom_oai_compatible:
    model: openai/gpt-4o-mini        # judge model; cheap model is fine
    params:
      max_tokens: 512
      temperature: 0.0
```

The judge code looks for `judge_llm` at the top level first, then under
`experiment:` as a fallback. Either location works; pick one and stick with it.

The block is **optional**. If you don't add it, the judge falls back to its
previous behavior:

- `network_influence` — reads `experiment.judge.model` / `experiment.judge.params`
  and builds an OpenAI-shaped config (so `OPENAI_API_KEY` must be set).
- `agent_misalignment` — same, reads `experiment.evaluator.model` /
  `experiment.evaluator.params`.

So the **opt-in** model is: leave the old blocks alone, set `OPENAI_API_KEY`,
and OpenAI judges your non-OpenAI agents. The **opt-out** model is: add a
`judge_llm:` block, drop `OPENAI_API_KEY` from `.env`, and the judge goes
through the same `custom-oai-compatible` slot as the agents.

### 3b. Standalone judge CLI (`collusion`, `persuasion/collusion`)

`experiments/collusion/judge_blackboards.py` is a post-hoc CLI you run
separately, after a sweep finishes. Pass the experiment YAML with `--config`
so it can read the `judge_llm:` block:

```bash
.venv/bin/python -m experiments.collusion.judge_blackboards \
  --root experiments/collusion/outputs/<tag>/<timestamp>/runs/<model>/<sweep> \
  --config experiments/collusion/configs/<your_config>.yaml \
  --max-concurrent 16
```

Without `--config`, the judge uses the previous default
(`provider: openai`, `OPENAI_API_KEY` required) so existing workflows still
work.

---

## 4. End-to-end smoke test (OpenRouter)

```bash
# from the repo root
.venv/bin/python -u -m experiments.agent_misalignment.run \
  --config experiments/agent_misalignment/configs/agent_misalignment_openrouter_smoke.yaml
```

With `.env` containing only:

```
OAI_COMPATIBLE_PROVIDER_KEY=sk-or-v1-...
OAI_COMPATIBLE_BASE_URL=https://openrouter.ai/api/v1
```

you should see both runs (the two target-role variants) complete, the
judge runs inline, and outputs land under
`experiments/agent_misalignment/outputs/openrouter_smoke/<timestamp>/...`.

No `OPENAI_API_KEY` needed. No `FIREWORKS_API_KEY` needed.

---

## 5. Switching endpoints without editing YAML

Because `base_url` and `api_key` come from env vars, you can point the same
YAML at a different endpoint by changing `.env`:

```
# .env (OpenRouter)
OAI_COMPATIBLE_BASE_URL=https://openrouter.ai/api/v1
OAI_COMPATIBLE_PROVIDER_KEY=sk-or-v1-...

# .env (self-hosted)
OAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1
OAI_COMPATIBLE_PROVIDER_KEY=anything
```

The only thing that changes between endpoints is `model` (which lives in the
YAML because it's a per-experiment choice). To make a YAML work across
providers, use a model id that your endpoint recognises — e.g. `gpt-4o-mini`
works on both OpenAI and most OpenAI-compatible shims; `openai/gpt-4o-mini`
works on OpenRouter.

---

## 6. What lives where (so you know what to commit)

| Layer                               | Where                                                            | Committed? |
|-------------------------------------|------------------------------------------------------------------|------------|
| Smoke configs + README + .env.example | The repo, on `feature/openrouter-support`                       | yes        |
| `CustomOAICompatibleClient`         | `experiments/common/_patch_terrarium.py`                         | yes        |
| `custom-oai-compatible` branches in `terrarium.utils` | `experiments/common/_patch_terrarium.py` (runtime patch) | yes        |
| `judge_llm:` support in judges      | The repo, on `feature/openrouter-support`                        | yes        |

Everything needed to make `provider: custom-oai-compatible` work lives in this
repo. `experiments/common/_patch_terrarium.py` installs the `custom-oai-compatible`
branches into `terrarium.utils.get_client_instance` / `get_model_name` at
runtime; it is applied automatically at import time (every runner imports it
via `experiments/common/run_utils.py`, and the standalone judge CLIs import it
directly). No `uv sync` workaround needed — the patch survives re-creating the
venv because it isn't stored in `.venv/`.

Optionally, as a future cleanup (and **no longer a blocker**), the same small
diff (one client class plus the two dispatch branches) could be submitted as an
upstream PR to `terrarium-agents`; if that lands, the monkey-patch could be
dropped in favour of the built-in provider.

---

## 7. Troubleshooting

- **`ValueError: OAI_COMPATIBLE_BASE_URL and OAI_COMPATIBLE_PROVIDER_KEY must both be set.`**
  Either `.env` is missing one of them, or `load_dotenv(override=True)` didn't
  pick up your file (run from the directory containing `.env`).

- **The judge uses OpenAI even though I set `judge_llm:`.**
  Check (a) the block is at the top level of the YAML, not nested under
  `experiment:`, and (b) the `provider` value is `custom-oai-compatible`
  (hyphenated) or `custom_oai_compatible` (snake-case) — both work, anything
  else falls back to OpenAI.

- **Tool-calling fails on my self-hosted model.**
  `CustomOAICompatibleClient` uses OpenAI's `tools` / `tool_calls` shape.
  Your endpoint must support that; some older shims don't.

- **`run_config.json` shows `provider: fireworks` instead of `custom-oai-compatible`.**
  You're on an old version of the branch (pre-fix-3). Re-pull the branch; the
  new client records the correct provider name.