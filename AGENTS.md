# AGENTS.md — Dataverse as Code

These are the authoring and consistency rules for this repository. Every coding agent reads this file. There is no `CLAUDE.md` and no `.github/copilot-instructions.md`, and none should be added.

## What This Repo Is

A plugin marketplace that manages Dataverse **as code**. Each plugin ships a declarative YAML model, the Python tool that reconciles it with the dev environment (plan → confirm → apply), a planner agent and a validation hook.

This repo is a **convention-neutral implementation**. It owns the model formats, the tooling and the safety model — never design conventions. Users model Dataverse with their own best practices, or none.

**Suggested companion.** We recommend the `powerplatform-bestpractices` marketplace (`ppbp-*` skills, https://github.com/tchinnin/powerplatform-bestpractices-skills) for best practices, and its `ppbp-init` skill to create `.env.dev`. Suggest it to users who have no best practices of their own — but nothing in this repo may depend on it.

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
| Naming and design conventions | The user's best practices — never this repo |
| Authoring rules | This file |

Never copy skills or manifests to another place (no per-plugin `AGENTS.md`/`CLAUDE.md`, no symlinks, no mirrored manifests).

## Separation of concerns

1. **Official Microsoft skills** (`dataverse:dv-metadata`, `dataverse:dv-overview`, `dataverse:dv-connect`, `dataverse:dv-solution`) own how Dataverse works: APIs, CLI commands, procedures, error codes.
2. **`dvac-*` skills** (this repo) own the model format, the tool, the plan/apply workflow and the safety model.
3. **Best practices** (naming, design, reviews, project structure, ALM) belong to the user — their own, a best-practice marketplace such as `powerplatform-bestpractices`, or none.

Rules:

- **Additive only.** A `dvac-*` skill never restates what an official skill specifies. Point to it instead: *"The official skill owns <topic> — load it."*
- **Convention-neutral.** Validators, schemas, skills and agents enforce only Dataverse platform rules, tool rules (file identity, references) and safety gates (irreversible or destructive changes). Never encode a naming or design convention — no suffix rules, no required descriptions, no "prefer X" warnings. Samples may illustrate one scheme, labelled as an example.
- **Best practices at runtime.** Skills and agents look for the project's own best practices (README, `AGENTS.md`, installed skills) and follow them. When there are none, they may suggest a best-practice marketplace — `powerplatform-bestpractices` is the one we recommend — but must work without it.
- **Runtime checks.** Every skill and agent checks that the official `dataverse` plugin is available and strongly recommends installing it if not.

## Plugin Architecture

```
marketplace: dataverse-as-code
└── plugin: dataverse-<domain>-as-code/
    ├── .claude-plugin/plugin.json        ← the only manifest (Claude Code and Copilot)
    ├── agents/dvac-<domain>-planner.agent.md
    ├── hooks/hooks.json
    └── skills/
        ├── dvac-<domain>/
        │   ├── SKILL.md
        │   ├── references/               ← format.md, plan-apply.md, prerequisites.md, schemas/, samples/
        │   ├── scripts/                  ← dv_<domain>.py, hook_validate.py, requirements.txt
        │   └── tests/                    ← required (stdlib unittest, offline)
        └── dvac-<domain>-docs/           ← optional documentation skill (see below)
            ├── SKILL.md
            ├── references/               ← <output>.md, examples/ (generated from the model samples)
            ├── scripts/                  ← dv_<domain>_docs.py, requirements.txt
            └── tests/                    ← required (stdlib unittest, offline)
```

One plugin = one model family = **one skill** (`dvac-<domain>`) + its planner agent + its hook. A plugin has no separate overview skill: the skill itself carries `## Connection` and `## Repository layout` for the files it owns. Add a second skill to a plugin only for a genuinely distinct concern of the same model; add a new plugin for a new model family (e.g. a future `dataverse-security-as-code`).

### Documentation skills

A plugin may add one documentation skill, `dvac-<domain>-docs`, that generates documentation (Markdown, Mermaid) from the model files. It has no agent and no hook.

- **Offline and read-only.** It never contacts an environment and never writes the model; the only file it writes is the `--out` the user gives. It owns no project path.
- **Deterministic.** Same model, same bytes: stable sort order, no date, source paths relative to the repo root. It offers `--out <file> --check` (exit 1 when the file is stale) for CI.
- **It reuses the model skill of the same plugin.** Its script imports `../dvac-<domain>/scripts/dv_<domain>.py` to load and validate the model, and refuses an invalid model. This is the one allowed import: both skills ship in the same plugin. Never import across plugins.
- **Convention-neutral.** It shows the model as written and states only platform facts (execution order, cardinality). No design or naming judgement.
- `references/examples/` holds the output generated from the model skill's samples; a test fails when it is stale.


### Skills

Required sections: `## Official skill`, `## Best practices` (states that the skill is convention-neutral and how to pick up the project's practices), `## Connection`, `## Repository layout` (only the as-code files the tool owns), `## Workflow: plan → confirm → apply`, `## Safety model`, `## Anti-patterns (DO NOT DO)` (Wrong/Correct table), `## Skill boundaries`. A documentation skill has `## Workflow: generate → review` instead, and its `## Connection` states that it needs none.

Implementation pitfalls use the two-level pattern: a short `## Common issue: <topic>` section in SKILL.md (2–3 sentences) pointing to the full treatment in `references/<topic>.md`.

### Agents

- File `agents/dvac-<domain>-planner.agent.md`. The `.agent.md` suffix is what GitHub Copilot CLI expects; Claude Code takes the name from frontmatter.
- Frontmatter: `name` (= file name without `.agent.md`), `description` (what it does plus when to use it), `tools`, `model: sonnet`.
- An agent can't talk to the user. It works in two invocations: a planning run that returns the plan and stops, and an apply run that starts only on `approved: <plan file>`. Never apply a change the user hasn't confirmed. Outside dev, the data agent's apply run also needs the values the user typed (`confirm-url`, `confirm-deletes`); an agent never fills them in.

### Scripts

- Python ≥ 3.9, dependencies in `scripts/requirements.txt`, offline tests in `tests/` (stdlib `unittest`).
- Authenticate with the Azure CLI token (`az login`).
- Take the environment (URL, stage, tenant) from **one** source, first match wins: `--env-url/--stage/--tenant` flags, then the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, then `<repo-root>/.env.<stage>`. URL and stage never mix sources; `--tenant` may override any source. `.env.<stage>` files are one option, never a requirement.
- **Customization tools are dev-only** (`dv_metadata.py`, `dv_plugin.py`): refuse to run unless the stage is declared `dev`, the URL differs from `.env.uat`/`.env.prod` when those files exist, and the target solution exists and is unmanaged. UAT and prod receive customizations only through the managed solution.
- **Data tools target a named stage** (`dv_data.py`): data is not solution-aware, so the tool runs against dev, UAT or prod. `--stage` is always required and never defaults; the source must declare that stage; the URL must differ from every other `.env.<stage>` file; the organization id is pinned in the plan and checked at apply. Outside `dev`, `apply` refuses unless the user — never the agent — typed the environment URL (`--confirm-url`) and, when the plan deletes rows, their count (`--confirm-deletes`).
- There is never a flag to override a guard.
- **Shared code is copied, not imported** — plugins install independently. The dev guard and Web API client in `dv_plugin.py`, and the Web API client in `dv_data.py`, must stay identical to `dv_metadata.py` (except the names allowed by each `TestSharedBlock`). Change every copy in the same commit.
- Never destructive by default. Any delete the tool can perform must be listed in the skill's safety model and require double confirmation.
- Tools never write GUIDs into model files unless the format has no other identity. `dv_data.py` identifies rows by alternate key and never writes the model.

### Hooks

- `hooks/hooks.json` in Claude Code format; reference scripts with `${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/…`.
- A hook exits immediately (code 0) for files it doesn't own and never blocks a developer whose environment isn't set up.
- Copilot CLI's hook format is not guaranteed to match, so a skill never *depends* on its hook: the skill and the agent also run `validate` explicitly.

### Compatibility

The model file names (`*.dv.<domain>.yaml`), project paths (`dataverse/metadata/`, `dataverse/data/[<stage>/]`, `plugins/<Name>/`), plan directories (`.dv-<domain>/`), the environment inputs (flags, variables, `.env.<stage>` keys) and the plan file format are a public contract with the projects that use them. Changing one is a MAJOR version bump with a migration note in the skill.

## Skill Authoring Standards

- **Language:** English everywhere.
- **Naming:** skills `dvac-<domain>` (documentation skills `dvac-<domain>-docs`), agents `dvac-<domain>-planner`, plugins `dataverse-<domain>-as-code`. Lowercase, hyphens only; directory name = `name`.
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
python3 -m unittest discover -s plugins/dataverse-data-as-code/skills/dvac-data/tests
python3 -m unittest discover -s plugins/dataverse-metadata-as-code/skills/dvac-metadata-docs/tests
python3 -m unittest discover -s plugins/dataverse-plugins-as-code/skills/dvac-plugins-docs/tests
```

Every suite must pass, and each `TestSharedBlock` must run (not skip).

## Commit Conventions

Use prefixes: `feat:`, `fix:`, `refactor:`, `add:`, or `docs:` (no scopes).
