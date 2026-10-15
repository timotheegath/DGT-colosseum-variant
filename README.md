# Colosseum <sub><sub><img src="dev/colosseum_icon.png" alt="Colosseum icon" width="45" /></sub></sub>

> [!NOTE]
> :tada: **Colosseum has been accepted to the NeurIPS 2026 Evaluations and Datasets Track!** Read the paper on [arXiv](https://arxiv.org/abs/2602.15198).

If you find the repo or paper useful or interesting please :star: it!
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

Supported providers (Terrarium backends): `openai`, `anthropic`, `gemini`, `together`, `fireworks`, `vllm` (local serving). A config selects one via `llm_models[].llm.provider`.

### OpenRouter (passthrough)

OpenRouter is supported by routing through the existing `fireworks` provider slot with `base_url: https://openrouter.ai/api/v1`. The `fireworks` client is OpenAI-compatible (POSTs to `<base_url>/chat/completions` with `Authorization: Bearer <key>`), so no changes to Terrarium are required.

- Model names in YAML are OpenRouter model IDs, e.g. `openai/gpt-4o-mini`, `anthropic/claude-3.5-sonnet`, `google/gemini-2.0-flash-001`; they are passed verbatim in the request body.
- Your `.env` must set `OPENROUTER_API_KEY=<key>` **and** `FIREWORKS_API_KEY=<same key>`: the `fireworks` client falls back to the `FIREWORKS_API_KEY` env var when no `api_key` is given in the config.
- Why this design: it keeps the change zero-edits to terrarium. Note that `provider` recorded in `run_config.json` will be `fireworks` (the underlying transport), while `model` will be the OpenRouter model ID.
- Ready-to-use smoke configs:
  - `experiments/collusion/configs/collusion_jira_openrouter_smoke.yaml`
  - `experiments/network_influence/configs/misinformation_prop_openrouter_smoke.yaml`
  - `experiments/agent_misalignment/configs/agent_misalignment_openrouter_smoke.yaml`
  - `experiments/persuasion/collusion/configs/persuasion_collusion_jira_openrouter_smoke.yaml`
  - `experiments/persuasion/hospital/configs/persuasion_hospital_openrouter_smoke.yaml`
- Judges (where used by the experiment) can also be routed through OpenRouter via an optional `judge_llm:` block in the config. See the smoke configs for examples.

## Citation
Please cite us using:
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
