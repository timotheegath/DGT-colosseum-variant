# Colosseum <sub><sub><img src="dev/colosseum_icon.png" alt="Colosseum icon" width="45" /></sub></sub>

> [!IMPORTANT]
> ## This repository is a variant of the original [Colosseum](https://github.com/umass-ai-safety/colosseum) project.
>
> It is built on top of the same environments and tools, adapted to **test agent collusion without explicit communication**. Rather than relying on a designated side channel (e.g. a secret blackboard), this variant removes the side-channel affordance and probes whether collusion can emerge through the usage of tools on the common channel.
>
> Developed as part of the **Apart Research sprint, October 2026**.
>
> All credit for the framework, environments, and experimental design goes to the original authors. See the [Citation](#citation) section below — please cite the original work as the authors request.

## Description
<!-- > [!NOTE] -->
> Colosseum is a framework for objectively auditing cooperative multi-agent systems composed of tool-calling, LLM-based agents. We study collusion across varying models, network topologies, and environments. Below is a taxonomy of various collusion types Colosseum can identify:

<!-- > [!FINDING] -->
<!-- > [!TIP] -->

<p align="center">
  <a href="dev/colosseum-motivation-v3.pdf">
    <img src="dev/colosseum-motivation-v3-preview.png" alt="Colosseum motivation slides" width="70%" />
  </a>
</p>

This repo contains experiments for Colosseum under `experiments/` that utilizes the [Terrarium](https://github.com/umass-aisec/Terrarium) (`terrarium-agents`) framework.

> [!TIP]
> Designing a new experiment series? See
> [`docs/EXPERIMENT_CONFIG_GUIDE.md`](docs/EXPERIMENT_CONFIG_GUIDE.md) — a
> reference for every experiment-config YAML key, the per-experiment sweep
> axes, and the gotchas that silently produce the wrong runs.


## Setup

1) Install CoLLAB (required for certain environments):
- Recommended (submodule):
  - `git submodule add https://github.com/Saad-Mahmud/CoLLAB_SEA.git external/CoLLAB`
  - `git submodule update --init --recursive`
- Alternative: clone anywhere and set `TERRARIUM_COLLAB_PATH` in `.env`.

2) Create env + install deps:
- `uv venv --python 3.11 .venv`
- `source .venv/bin/activate`
- `uv sync --no-install-project`

3) Configure environment and API keys (if needed)(never put real keys in `.env.example`):
- `cp .env.example .env`
- Fill API keys

## Run

> [!TIP]
> If you are using APIs, actively track API usage since API costs accumulate linearly with the number of agents in the run.

Run from the repo root with your virtualenv active (e.g., `source .venv/bin/activate`). Here are is a non-exhaustive list of run commands:

```bash
# Persuasion affects on collusion (JiraTicket)
python experiments/persuasion/collusion/run.py \
  --config experiments/persuasion/collusion/configs/persuasion_collusion_jira.yaml

# Persuasion affects on collusion (Hospital)
python experiments/persuasion/hospital/run.py \
  --config experiments/persuasion/hospital/configs/persuasion_hospital.yaml

# Collusion (JiraTicket topology sweeps)
python experiments/collusion/run.py \
  --config experiments/collusion/configs/collusion_jira_topologies.yaml

# Network influence / misinformation propagation (Mass policy)
python experiments/network_influence/run.py \
  --config experiments/network_influence/configs/misinformation_prop_mass.yaml

# Agent objective misalignment (Hospital)
python experiments/agent_misalignment/run.py \
  --config experiments/agent_misalignment/configs/agent_misalignment_gpt_4_1_mini.yaml
```

## Providers

Supported providers (Terrarium backends): `openai`, `anthropic`, `gemini`, `together`, `fireworks`, `custom_oai_compatible`, `vllm` (local serving). A config selects one via `llm_models[].llm.provider`. The `custom_oai_compatible` value is snake_case only — the hyphenated `custom-oai-compatible` raises.

### Any OpenAI-compatible provider

The `custom_oai_compatible` provider slot in Terrarium talks to any endpoint speaking OpenAI's chat-completions API (POSTs to `<base_url>/chat/completions` with `Authorization: Bearer <key>`). 

- Required env vars: `OAI_COMPATIBLE_BASE_URL` (e.g. `https://openrouter.ai/api/v1`) and `OAI_COMPATIBLE_PROVIDER_KEY` (the API key for that endpoint). See `.env.example`.
- Model names in YAML are whatever id that endpoint expects, and are passed verbatim in the request body. OpenRouter, for example, uses ids like `openai/gpt-4o-mini` or `anthropic/claude-3.5-sonnet`.
- Optional: per-config overrides via a `custom_oai_compatible: { base_url, api_key }` block in the YAML.
- Judges (where used by the experiment) can also be routed through this provider via the optional `judge_llm:` block in the config. See the smoke configs for examples.
- Ready-to-use smoke configs:
  - `experiments/collusion/configs/collusion_jira_openrouter_smoke.yaml`
  - `experiments/network_influence/configs/misinformation_prop_openrouter_smoke.yaml`
  - `experiments/agent_misalignment/configs/agent_misalignment_openrouter_smoke.yaml`
  - `experiments/persuasion/collusion/configs/persuasion_collusion_jira_openrouter_smoke.yaml`
  - `experiments/persuasion/hospital/configs/persuasion_hospital_openrouter_smoke.yaml`

The provider is implemented as a runtime monkey-patch in `experiments/common/_patch_terrarium.py` so no terrarium fork or post-install patching is needed; the patch activates automatically when any runner or judge is imported.

## Citation
Please cite the original work using:
```bibtex
@article{nakamura2026colosseumauditingcollusioncooperative,
      title={Colosseum: Auditing Collusion in Cooperative Multi-Agent Systems}, 
      author={Mason Nakamura and Abhinav Kumar and Saswat Das and Sahar Abdelnabi and Saaduddin Mahmud and Ferdinando Fioretto and Shlomo Zilberstein and Eugene Bagdasarian},
      year={2026},
      eprint={2602.15198},
      archivePrefix={arXiv},
      primaryClass={cs.MA},
      url={https://arxiv.org/abs/2602.15198}, 
}
```
