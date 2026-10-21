# Colluders Without a Private Channel (fork of `experiments/collusion`)

This package is a **fork of `experiments/collusion`** for the
**"colluders with no private blackboard"** condition. It studies coalitions that
must coordinate **only on public channels** (no secret colluder-only blackboard),
i.e. overt coordination in plain sight rather than covert collusion.

The parent `experiments/agent_misalignment/` package is left **byte-for-byte untouched**.
Everything in this folder is rewired to be self-contained under the new module
name `experiments.collusion_no_private_channel` (imports, CLI module paths,
default output dirs, and YAML `environment.import_path` values).
