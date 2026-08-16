# Agentic Analytics — Spec Kit Package

This directory is a complete Spec Kit design package for an **agent-agnostic, MCP-native analytical runtime** inspired by the architectural lessons of DeepAnalyze while targeting coding agents such as Codex, Claude Code, Antigravity, and future MCP-capable hosts.

## Contents

```text
.specify/memory/constitution.md
specs/001-agent-agnostic-analytical-runtime/
├── spec.md
├── research.md
├── plan.md
├── data-model.md
├── quickstart.md
├── tasks.md
├── contracts/
│   ├── mcp-tools.md
│   └── schemas.json
└── checklists/
    └── requirements.md
```

## Design thesis

The MCP server is the product boundary. Coding-agent integrations are thin optional adapters. Core correctness, execution, evidence provenance, validation, artifacts, and security remain host-neutral.

The runtime deliberately does **not** recreate DeepAnalyze's model-specific XML control protocol or add a separate multi-agent planner. Modern coding agents already plan and call tools; this project supplies the analytical environment and evidence contract they can share.

## Acknowledgements

This design package is informed by **DeepAnalyze: Agentic Large Language Models for Autonomous Data Science** by Shaolei Zhang, Ju Fan, Meihao Fan, Guoliang Li, and Xiaoyong Du (Renmin University of China and Tsinghua University), which motivates several decisions in `specs/001-agent-agnostic-analytical-runtime/research.md`. Agentic Analytics is an independent, MCP-native design informed by that research, not a port of it.

- Paper: [arXiv:2510.16872](https://arxiv.org/abs/2510.16872)
- Code: <https://github.com/ruc-datalab/DeepAnalyze> (MIT-licensed)
- Project page: <https://ruc-deepanalyze.github.io/>

If you build on that work, please cite it:

```bibtex
@misc{deepanalyze,
      title={DeepAnalyze: Agentic Large Language Models for Autonomous Data Science},
      author={Shaolei Zhang and Ju Fan and Meihao Fan and Guoliang Li and Xiaoyong Du},
      year={2025},
      eprint={2510.16872},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2510.16872},
}
```

## Development and AI assistance

This project is developed with AI coding assistants under human direction:

- This specification and design package was produced with **OpenAI Codex** (Spec Kit).
- The runtime implementation (tracked in the feature pull requests) additionally used **Anthropic Claude** (via Claude Code) for review-finding remediation, security/robustness hardening, and documentation.

The maintainer directs the work and is responsible for all changes. AI contributions are attributed per-commit via `Co-Authored-By` trailers.
