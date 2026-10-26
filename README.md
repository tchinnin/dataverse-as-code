# dataverse-as-code

A plugin marketplace for Claude Code and GitHub Copilot that manages Dataverse **as code**: declarative YAML models, committed with the project, reconciled with the dev environment through a Terraform-style **plan → confirm → apply** loop.

## What this is

Each plugin ships a declarative model format (JSON Schemas + samples), a Python tool (`doctor` / `validate` / `plan` / `apply`), a planner agent that runs the loop with a user confirmation gate, and a validation hook.

The tooling only ever targets the **dev** environment's unmanaged solution. UAT and prod receive changes through the managed solution and your ALM pipeline.

The conventions the models follow (naming, table/column/Choice design, plug-in project and step design) are not defined here. They live in the companion [`powerplatform-bestpractices`](https://github.com/tchinnin/powerplatform-bestpractices-skills) marketplace (`ppbp-*` skills).

## Plugins

| Plugin | Skill / agent | Model |
|---|---|---|
| `dataverse-metadata-as-code` | `dvac-metadata` / `dvac-metadata-planner` | `dataverse/metadata/**/*.dv.metadata.yaml` — global Choices, tables, columns, lookups, alternate keys. Never deletes. |
| `dataverse-plugins-as-code` | `dvac-plugins` / `dvac-plugins-planner` | `plugins/<PluginName>/<PluginName>.dv.plugin.yaml` — plug-in package or assembly, steps, images, Custom APIs. First upload via the Web API, then `pac plugin push`. |

## Installation

Claude Code:

```sh
/plugin marketplace add tchinnin/dataverse-as-code
/plugin install dataverse-metadata-as-code@dataverse-as-code
/plugin install dataverse-plugins-as-code@dataverse-as-code
```

GitHub Copilot CLI:

```sh
copilot plugin marketplace add tchinnin/dataverse-as-code
copilot plugin install dataverse-metadata-as-code@dataverse-as-code
copilot plugin install dataverse-plugins-as-code@dataverse-as-code
```

Also install:

- the official Microsoft `dataverse` plugin — the foundation these skills build on;
- the matching `powerplatform-bestpractices` plugins (`ppbp-overview`, `ppbp-dataverse-metadata`, `ppbp-dataverse-plugins`) — the conventions the models follow.

Prerequisites: Python ≥ 3.9, the Azure CLI (`az login`), and the PAC CLI for plug-ins. Details are in each skill's `references/prerequisites.md`.

## Repository map

```
.
├── AGENTS.md                      # Authoring and consistency rules (read by every agent)
├── .claude-plugin/
│   └── marketplace.json           # Marketplace registry (Claude Code + Copilot)
└── plugins/
    └── dataverse-<domain>-as-code/
        ├── .claude-plugin/plugin.json
        ├── agents/dvac-<domain>-planner.agent.md
        ├── hooks/hooks.json
        └── skills/dvac-<domain>/SKILL.md (+ references/, scripts/, tests/)
```

Authoring standards, naming, versioning and commit conventions are in [AGENTS.md](./AGENTS.md).
