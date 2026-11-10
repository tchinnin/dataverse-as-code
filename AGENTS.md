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
- **Best practices at runtime.** Skills and agents apply the conventions already in their context (the project's instructions, loaded skills, or what the caller passes to an agent) and never search for them. When there are none, they may suggest a best-practice marketplace — `powerplatform-bestpractices` is the one we recommend — but must work without it.
- **Official plugin.** Every skill strongly recommends installing the official `dataverse` plugin, and loads an official skill only when it needs know-how the format doesn't give. No skill or agent spends a step checking that it is installed.

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
        ├── dvac-<domain>-docs/           ← optional documentation skill (see below)
        │   ├── SKILL.md
        │   ├── references/               ← <output>.md, examples/ (generated from the model samples)
        │   ├── scripts/                  ← dv_<domain>_docs.py, requirements.txt
        │   └── tests/                    ← required (stdlib unittest, offline)
        └── dvac-<domain>-solution/       ← optional solution skill (see below)
            ├── SKILL.md
            ├── references/               ← solution-mapping.md
            ├── scripts/                  ← dv_<domain>_solution.py (import, compare), requirements.txt
            └── tests/                    ← required, with fixtures/ (a synthetic unpacked solution)
```

One plugin = one model family = **one skill** (`dvac-<domain>`) + its optional planner agent + its hook. A plugin has no separate overview skill: the skill itself carries `## Connection` and `## Repository layout` for the files it owns. Add a second skill to a plugin only for a genuinely distinct concern of the same model; add a new plugin for a new model family (e.g. `dataverse-security-as-code` next to `dataverse-metadata-as-code`).

### Documentation skills

A plugin may add one documentation skill, `dvac-<domain>-docs`, that generates documentation (Markdown, Mermaid) from the model files. It has no agent and no hook.

- **Offline and read-only.** It never contacts an environment and never writes the model; the only file it writes is the `--out` the user gives. It owns no project path.
- **Deterministic.** Same model, same bytes: stable sort order, no date, source paths relative to the repo root. It offers `--out <file> --check` (exit 1 when the file is stale) for CI.
- **It reuses the model skill of the same plugin.** Its script imports `../dvac-<domain>/scripts/dv_<domain>.py` to load and validate the model, and refuses an invalid model. This is the one allowed import: both skills ship in the same plugin. Never import across plugins.
- **Convention-neutral.** It shows the model as written and states only platform facts (execution order, cardinality). No design or naming judgement.
- `references/examples/` holds the output generated from the model skill's samples; a test fails when it is stale.

### Solution skills

A plugin may add one solution skill, `dvac-<domain>-solution`, that works between the model files and solutions unpacked in the repo (`pac solution unpack` XML layout). Its tool has two subcommands: `import` regenerates or aligns the model files from the solutions, `compare` lists what differs between them. It has no agent and no hook.

- **The unpacked solution is read only.** No subcommand writes, moves, reformats or creates a file in a solution folder given as input. A write target inside one (`--model`, `--projects-dir`, the computed path of a file) is refused with `STATUS: refused` and nothing is written; a model file found inside one is ignored. A difference is fixed in the model, or in dev followed by a new export — never in the XML.
- **Offline.** It reads the unpacked solution and the model files, never an environment. Exporting and unpacking belong to the official `dataverse:dv-solution` skill.
- **`compare` writes nothing.** It reports, in this order: `+` only in the solution, `-` only in the model, `~` differs (`<where>.<field>: model=… solution=…`), then for information `·` values the solution holds that the model doesn't declare (the format leaves them unmanaged) and `!` what the format cannot describe or the tool cannot compare offline. Only `+`, `-` and `~` are drift. Fields are compared as `plan` compares them with the server (reuse the model skill's normalisation, never a second set of rules). It ends with `STATUS: in-sync` or `STATUS: drift` (+ `REASON`, `FIX`, and `NEXT`: the import without `--write`).
- **`import` is diff first.** Without `--write` it writes nothing: it prints new, changed (with a diff) and unchanged files, the model entries the solution doesn't hold and the components the format cannot describe. `--write` writes only after the whole result passes the model skill's validator.
- **Never reduces the model.** Entries only in the model are kept and listed, never removed — a removed step would be deleted, a removed privilege revoked by the next `apply`.
- **Keeps what the solution doesn't carry** (ids, build paths, state, optional fields the file leaves unmanaged) and never writes a GUID from the solution.
- **Minimal rewrites.** A file whose data doesn't change is not rewritten, so its comments survive.
- **No silent loss.** Every component it cannot describe is listed with its reason.
- It imports the model skill of the same plugin, like a documentation skill. Its tests run on a synthetic fixture in `tests/fixtures/` that mirrors the model skill's samples: the import of that fixture into the samples must report no change, `compare` must report `in-sync`, and a `TestSolutionReadOnly` checks that the fixture is byte-identical after every subcommand and that a write target inside it is refused. Never commit an export from a real project.


### Skills

Required sections: `## Run` (first, see below), `## Official skill`, `## Best practices` (states that the skill is convention-neutral and how to pick up the project's practices), `## Connection`, `## Repository layout` (only the as-code files the tool owns), `## Workflow: plan → confirm → apply`, `## Safety model`, `## Anti-patterns (DO NOT DO)` (Wrong/Correct table), `## Skill boundaries`. A documentation skill has `## Workflow: generate → review` instead, a solution skill `## Workflow: compare → review` and `## Workflow: import → review → write → plan`; the `## Connection` of both states that they need none.

### Deterministic run

The agent's only job is the YAML; every other step is one command with a fixed output. This is what keeps runs cheap and predictable, on GitHub Copilot first and on Claude Code.

- **The script path comes from the loaded skill.** Commands are written `python3 <base directory>/scripts/…`, where `<base directory>` is the absolute path the agent receives when the skill loads (`Base directory for this skill: …` on Copilot CLI, VS Code and Claude Code). No `${…}` variable in SKILL.md: Copilot doesn't expand them. Never tell an agent to search the disk (no `Glob` in `allowed-tools` or `tools`): stale copies exist.
- **Dependencies install themselves.** `ensure_deps()` (shared block) re-runs the script in a cached venv when PyYAML or jsonschema is missing. Skills never ask for `pip install`.
- **The loop is `plan` → show → confirm → `NEXT`.** `plan` validates and checks the guard, so `validate` and `doctor` are not steps of the loop (`doctor` diagnoses an `error`). The plan is the agent's only read of the environment: no ad-hoc queries or scripts.
- **Fixed footer.** `plan`, `apply`, `import` and `compare` end with `STATUS:` (`changes-pending`, `no-changes`, `blocked`, `error`, `applied`, `stale-plan`, `refused`, `written`, `in-sync`, `drift`) and, as relevant, `REASON:`, `FIX:`, `PLAN FILE:`, `DOUBLE CONFIRMATION:` and `NEXT (only after the user confirms): <exact command>`. The script computes the double-confirmation list; the agent copies the output unchanged and never rebuilds a command.
- **Exit codes.** `plan` exits 0 once computed (VS Code shows any non-zero code as a failure); `--detailed-exitcode` gives CI 2 = pending, 1 = blocked.
- **Writing rule.** No instruction may ask an agent to search, guess or reformat. When a step needs judgement, the script should print the answer instead (a `FIX:` line, an `adopt`/`export` command).

Implementation pitfalls use the two-level pattern: a short `## Common issue: <topic>` section in SKILL.md (2–3 sentences) pointing to the full treatment in `references/<topic>.md`.

### Agents

- File `agents/dvac-<domain>-planner.agent.md`. The `.agent.md` suffix is what GitHub Copilot CLI expects; Claude Code takes the name from frontmatter.
- Frontmatter: `name` (= file name without `.agent.md`), `description` (what it does plus when to use it), `tools`, `model: sonnet`.
- The planner agent is **optional**: it writes the YAML for large changes and runs `plan`, then returns the files it changed and the plan output unchanged. It never applies — the main thread confirms with the user and runs the `NEXT` command. Copilot gives a sub-agent only its prompt, so the caller passes `DV=<absolute command>`, the request and the conventions to apply; without `DV=` the agent asks, it never searches. Outside dev the values the user typed (`--confirm-url`, `--confirm-deletes`) are put in by the main thread; an agent never fills them in.

### Scripts

- Python ≥ 3.9, dependencies in `scripts/requirements.txt`, offline tests in `tests/` (stdlib `unittest`).
- Authenticate with the Azure CLI token (`az login`).
- Take the environment (URL, stage, tenant) from **one** source, first match wins: `--env-url/--stage/--tenant` flags, then the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, then `<repo-root>/.env.<stage>`. URL and stage never mix sources; `--tenant` may override any source. `.env.<stage>` files are one option, never a requirement.
- **Customization tools are dev-only** (`dv_metadata.py`, `dv_plugin.py`, `dv_security.py`): refuse to run unless the stage is declared `dev`, the URL differs from `.env.uat`/`.env.prod` when those files exist, and the target solution exists and is unmanaged. UAT and prod receive customizations only through the managed solution.
- **Data tools target a named stage** (`dv_data.py`): data is not solution-aware, so the tool runs against dev, UAT or prod. `--stage` is always required and never defaults; the source must declare that stage; the URL must differ from every other `.env.<stage>` file; the organization id is pinned in the plan and checked at apply. Outside `dev`, `apply` refuses unless the user — never the agent — typed the environment URL (`--confirm-url`) and, when the plan deletes rows, their count (`--confirm-deletes`).
- There is never a flag to override a guard.
- **Shared code is copied, not imported** — plugins install independently. The dev guard, the Web API client and the agent-facing runtime (`ensure_deps`, `print_status`, `print_error`, `command_line`, `token_account`) in `dv_plugin.py` and `dv_security.py`, and the Web API client and runtime in `dv_data.py`, must stay identical to `dv_metadata.py` (except the names allowed by each `TestSharedBlock`). Change every copy in the same commit.
- Never destructive by default. Any delete the tool can perform must be listed in the skill's safety model and require double confirmation.
- Tools never write GUIDs into model files unless the format has no other identity. `dv_data.py` identifies rows by alternate key and never writes the model.

### Hooks

- `hooks/hooks.json` in Claude Code format; reference scripts with `${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/…`.
- A hook exits immediately (code 0) for files it doesn't own and never blocks a developer whose environment isn't set up.
- Copilot CLI's hook format is not guaranteed to match, so a skill never *depends* on its hook: `plan` validates the model before anything else.
- When the dependencies are missing, the hook reuses the tool's cached venv if it exists; it never creates it (time limit).

### Compatibility

The model file names (`*.dv.<domain>.yaml`), project paths (`dataverse/metadata/`, `dataverse/data/[<stage>/]`, `dataverse/security/roles/`, a `<Name>.dv.plugin.yaml` in the same folder as its `.csproj`), plan directories (`.dv-<domain>/`), the environment inputs (flags, variables, `.env.<stage>` keys) the plan file format, the solution tool subcommands (`import`, `compare`), and the `plan` exit codes and status footer are a public contract with the projects that use them. Changing one is a MAJOR version bump with a migration note in the skill.

## Skill Authoring Standards

- **Language:** English everywhere.
- **Naming:** skills `dvac-<domain>` (documentation skills `dvac-<domain>-docs`, solution skills `dvac-<domain>-solution`), agents `dvac-<domain>-planner`, plugins `dataverse-<domain>-as-code`. Lowercase, hyphens only; directory name = `name`.
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
python3 -m unittest discover -s plugins/dataverse-security-as-code/skills/dvac-security/tests
python3 -m unittest discover -s plugins/dataverse-metadata-as-code/skills/dvac-metadata-docs/tests
python3 -m unittest discover -s plugins/dataverse-plugins-as-code/skills/dvac-plugins-docs/tests
python3 -m unittest discover -s plugins/dataverse-security-as-code/skills/dvac-security-docs/tests
python3 -m unittest discover -s plugins/dataverse-security-as-code/skills/dvac-security-docs/tests
python3 -m unittest discover -s plugins/dataverse-metadata-as-code/skills/dvac-metadata-solution/tests
python3 -m unittest discover -s plugins/dataverse-plugins-as-code/skills/dvac-plugins-solution/tests
python3 -m unittest discover -s plugins/dataverse-security-as-code/skills/dvac-security-solution/tests
```

Every suite must pass, and each `TestSharedBlock` must run (not skip).

## Commit Conventions

Use prefixes: `feat:`, `fix:`, `refactor:`, `add:`, or `docs:` (no scopes).
