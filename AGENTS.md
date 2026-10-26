# AGENTS.md — Dataverse as Code

These are the authoring and consistency rules for this repository. Every coding agent reads this file. There is no `CLAUDE.md` and no `.github/copilot-instructions.md`, and none should be added.

## What This Repo Is

A plugin marketplace that manages Dataverse **as code**. Each plugin ships a declarative YAML model, the Python tool that reconciles it with the dev environment (plan → confirm → apply), a planner agent and a validation hook.

This repo is the **implementation**. The **conventions** the models follow (naming, table/column/Choice design, plug-in project, step and Custom API design) live in the companion `powerplatform-bestpractices` marketplace (`ppbp-*` skills, https://github.com/tchinnin/powerplatform-bestpractices-skills).

**Supported agents:** Claude Code and GitHub Copilot (CLI and VS Code). Both read the same `.claude-plugin/` manifests, so one set of files serves both. Do not add manifests for other agents (`.plugin/`, `.github/plugin/`, `.cursor-plugin/`, `.codex-plugin/`, root `marketplace.json`).

## Single source of truth

| Content | Only location |
|---|---|
| Skill (SKILL.md + `references/`) | `plugins/<plugin>/skills/dvac-<domain>/` |
| Tooling (scripts, schemas, samples, tests) | `plugins/<plugin>/skills/dvac-<domain>/{scripts,references,tests}/` |
| Agents | `plugins/<plugin>/agents/dvac-<domain>-planner.agent.md` |
| Hooks | `plugins/<plugin>/hooks/hooks.json` |
| Plugin manifest | `plugins/<plugin>/.claude-plugin/plugin.json` |
| Marketplace registry + marketplace version | `.claude-plugin/marketplace.json` |
| Naming and design conventions | The `ppbp-*` skills of `powerplatform-bestpractices` — never this repo |
| Authoring rules | This file |

Never copy skills, manifests or conventions to another place (no per-plugin `AGENTS.md`/`CLAUDE.md`, no symlinks, no mirrored manifests).

## Layering: official → best practices → as code

1. **Official Microsoft skills** (`dataverse:dv-metadata`, `dataverse:dv-overview`, `dataverse:dv-connect`, `dataverse:dv-solution`) own how Dataverse works: APIs, CLI commands, procedures, error codes.
2. **`ppbp-*` skills** own the team conventions.
3. **`dvac-*` skills** (this repo) own the model format, the tool, the plan/apply workflow and the safety model.

Rules:

- **Additive only.** A `dvac-*` skill never restates what an official skill or a `ppbp-*` skill already specifies. Point to it by name instead: *"The official skill owns <topic> — load it."* / *"Naming follows `ppbp-dv-columns`."*
- **The validator is the only place conventions are encoded.** When it enforces a `ppbp-*` rule, the skill names the rule's owner and does not re-explain it. When a convention changes in `ppbp-*`, update the validator and its tests here.
- **Runtime checks.** Every skill and agent checks that the official `dataverse` plugin is available and strongly recommends installing it if not. It also recommends the `ppbp-*` companion skills if they are missing, but must still work without them (the validator is authoritative).

## Plugin Architecture

```
marketplace: dataverse-as-code
└── plugin: dataverse-<domain>-as-code/
    ├── .claude-plugin/plugin.json        ← the only manifest (Claude Code and Copilot)
    ├── agents/dvac-<domain>-planner.agent.md
    ├── hooks/hooks.json
    └── skills/dvac-<domain>/
        ├── SKILL.md
        ├── references/                   ← format.md, plan-apply.md, prerequisites.md, schemas/, samples/
        ├── scripts/                      ← dv_<domain>.py, hook_validate.py, requirements.txt
        └── tests/                        ← required (stdlib unittest, offline)
```

One plugin = one model family = **one skill** (`dvac-<domain>`) + its planner agent + its hook. A plugin has no separate overview skill: the skill itself carries `## Connection` and `## Repository layout` for the files it owns. Add a second skill to a plugin only for a genuinely distinct concern of the same model; add a new plugin for a new model family (e.g. a future `dataverse-security-as-code`).

### Skills

Required sections: `## Official skill`, `## Companion plugins` (the `ppbp-*` skills that own the conventions), `## Connection`, `## Repository layout` (only the as-code files the tool owns), `## Workflow: plan → confirm → apply`, `## Safety model`, `## Anti-patterns (DO NOT DO)` (Wrong/Correct table), `## Skill boundaries`.

Implementation pitfalls use the two-level pattern: a short `## Common issue: <topic>` section in SKILL.md (2–3 sentences) pointing to the full treatment in `references/<topic>.md`.

### Agents

- File `agents/dvac-<domain>-planner.agent.md`. The `.agent.md` suffix is what GitHub Copilot CLI expects; Claude Code takes the name from frontmatter.
- Frontmatter: `name` (= file name without `.agent.md`), `description` (what it does plus when to use it), `tools`, `model: sonnet`.
- An agent can't talk to the user. It works in two invocations: a planning run that returns the plan and stops, and an apply run that starts only on `approved: <plan file>`. Never apply a change the user hasn't confirmed.

### Scripts

- Python ≥ 3.9, dependencies in `scripts/requirements.txt`, offline tests in `tests/` (stdlib `unittest`).
- Authenticate with the Azure CLI token (`az login`). Read the target only from `<repo-root>/.env.dev`. Refuse to run unless `ENVIRONMENT_STAGE=dev`, the URL differs from `.env.uat`/`.env.prod`, and the target solution exists and is unmanaged. There is never a flag to override the guard.
- **Shared code is copied, not imported** — plugins install independently. The dev guard and Web API client in `dv_plugin.py` must stay identical to `dv_metadata.py` (except the names allowed by `TestSharedBlock`). Change both copies in the same commit.
- Never destructive by default. Any delete the tool can perform must be listed in the skill's safety model and require double confirmation.

### Hooks

- `hooks/hooks.json` in Claude Code format; reference scripts with `${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/…`.
- A hook exits immediately (code 0) for files it doesn't own and never blocks a developer whose environment isn't set up.
- Copilot CLI's hook format is not guaranteed to match, so a skill never *depends* on its hook: the skill and the agent also run `validate` explicitly.

### Compatibility

The model file names (`*.dv.<domain>.yaml`), project paths (`dataverse/metadata/`, `plugins/<Name>/`), plan directories (`.dv-<domain>/`) and `.env.dev` are a public contract with the projects that use them. Changing one is a MAJOR version bump with a migration note in the skill.

## Skill Authoring Standards

- **Language:** English everywhere.
- **Naming:** skills `dvac-<domain>`, agents `dvac-<domain>-planner`, plugins `dataverse-<domain>-as-code`. Lowercase, hyphens only; directory name = `name`.
- **Frontmatter:**

```yaml
---
name: dvac-<domain>
description: >
  One descriptive sentence plus an inline "Use when the user…" clause. ≤1,024 characters.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch
argument-hint: "<change request>"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---
```

- **Token budgets:** description ≤200 tokens; SKILL.md body ≤5,000 tokens; `references/` unlimited.
- **Code examples** are complete and runnable (YAML models validate against the schemas; samples pass `validate`).

## Version Management

- **Skill** — bump `metadata.version` when the skill body changes.
- **Plugin** — bump `version` in `plugin.json` when anything in the plugin changes (skill, agent, hook, script, schema).
- **Marketplace** — bump `metadata.version` in `marketplace.json` when a plugin is added, removed or renamed, or the repo structure changes.

SemVer: **MAJOR** — breaking model format, path or CLI change, skill/agent removal or rename. **MINOR** — new model attribute, subcommand, skill or plugin. **PATCH** — fixes, clarifications, reference additions.

## Verification before committing

```sh
python3 -m unittest discover -s plugins/dataverse-metadata-as-code/skills/dvac-metadata/tests
python3 -m unittest discover -s plugins/dataverse-plugins-as-code/skills/dvac-plugins/tests
```

Both suites must pass, and `TestSharedBlock` must run (not skip).

## Commit Conventions

Use prefixes: `feat:`, `fix:`, `refactor:`, `add:`, or `docs:` (no scopes).
