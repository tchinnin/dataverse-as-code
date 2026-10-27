---
name: dvac-data
description: >
  Dataverse data as code: rows of configuration and reference tables are described
  in dataverse/data/*.dv.data.yaml (a base shared by every stage plus per-stage
  overlays), identified by alternate key, and reconciled with the dev, UAT or prod
  environment through a Terraform-style plan → confirm → apply loop, with opt-in
  deactivate/delete and business-logic bypass. Use when the user wants to seed,
  align or promote Dataverse data rows across environments, write or fix
  .dv.data.yaml files, preview data changes (plan), or apply them.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch
argument-hint: "<data change request>"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

`dataverse:dv-data` owns how Dataverse records are written (Web API, upsert, bulk operations, error codes), and `dataverse:dv-query` owns reads. Alternate keys are metadata, owned by `dataverse:dv-metadata` (or modeled with `dvac-metadata`). Load them before this skill. If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding. This skill adds a declarative model and the tooling that reconciles it. It does not restate the official procedures.

## Best practices

This skill defines the model format and the tooling, **not** what data to manage or how to name it. Which tables are seeded, which rows each stage gets, and how ownership is split between files and users are the project's best practices. The validator only enforces Dataverse platform rules and tool rules (keys, references, filters, safety gates).

Before writing the model, look for the project's own best practices (README, `AGENTS.md`, installed skills) and follow them. If there are none, work from the user's request and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

- **Azure CLI token only.** Sign in with `az login`, adding `--tenant <TENANT_ID>` when the account spans several tenants.
- **Every command names its stage** with `--stage <dev|uat|prod|…>`; it never defaults. The URL comes from one source: `--env-url/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else `<repo-root>/.env.<stage>`. The source must declare the same stage. Details in [`references/prerequisites.md`](references/prerequisites.md).
- If no source is available, ask the user for the URL and tenant. Suggest committed `.env.<stage>` files as a convenient option (for example created by `ppbp-init`), never require them.
- `dataverse:dv-connect` owns environment discovery and connection diagnostics.
- Before any change, confirm the stage, the target URL and the active `az` account with the user.

## Repository layout

```
<repo-root>/
├── .env.dev / .env.uat / .env.prod      # optional — one way to describe each stage
├── .dv-data/<stage>/plan.json           # plan files (gitignored)
└── dataverse/
    └── data/                            # SOURCE OF TRUTH — data as code
        ├── <table>.dv.data.yaml         # base: rows for every stage
        └── <stage>/<table>.dv.data.yaml # overlay: values or rows for one stage
```

One file per table, named after its logical name. An overlay row matches a base row by key and replaces the columns it lists; other overlay rows are added. An overlay may also override `deletion`, `delete_filter` and `bypass`.

## What this skill adds

- **One declarative model** under `dataverse/data/`. The format is in [`references/format.md`](references/format.md), annotated samples in [`references/samples/`](references/samples/).
- **One tool**, `scripts/dv_data.py`:

| Subcommand | Touches the server | Purpose |
|---|---|---|
| `doctor --stage <s> [data-dir]` | read | Deps, `az` session, stage guard, each table and its alternate key, bypass privilege |
| `validate <dir\|file> [--stage <s>]` | no | Structural (JSON Schema) + semantic checks (keys, overlays, filters, references) |
| `plan <data-dir> --stage <s> [--json]` | read | Diff → `+ create` / `~ update` / `~ activate` / `- deactivate` / `- delete` / `! blocked` |
| `apply <data-dir> --stage <s> --plan-file <f> [--confirm-url …] [--confirm-deletes …]` | write | Runs exactly the saved plan. Aborts if stale. |

- **A PostToolUse hook** (Claude Code) that validates after every `*.dv.data.yaml` edit. On GitHub Copilot, run `validate` explicitly.
- **A planner agent**, `dvac-data-planner`, that runs the loop with a user confirmation gate.

Run the script as `python3 <this-skill>/scripts/dv_data.py …`, after `pip install -r <this-skill>/scripts/requirements.txt` in a venv.

## Workflow: plan → confirm → apply

Delegate data changes to the **`dvac-data-planner`** agent. It runs in two invocations:

1. **Planning run.** The agent edits the model, validates it, runs `plan --stage <s>`, and returns the target (stage, URL, organization), every action with its values, the bypassed business logic, the blocked items and the warnings.
2. **The main thread shows that plan to the user** and asks for explicit confirmation of the stage, the environment and the change list.
   - **Deletes** (`- delete`) need a second, separate confirmation, on every stage.
   - **Bypass:** list every step, workflow and flow the plan says will not run, and get the user's consent.
   - **Outside dev:** show the environment URL and ask the user to **type** it (copy-paste is fine), and to type the number of deletes when there are any. Never type them for the user.
3. **Apply run.** Re-invoke the agent with `approved: <plan file>`, plus, outside dev, `confirm-url: <what the user typed>` and `confirm-deletes: <what the user typed>`. It runs `apply`, then checks that the follow-up plan is empty.

Never run `apply` without a plan the user has just seen. Never apply a plan with blocked items.

## Safety model

- **Every stage, guarded.** The tool refuses to run unless the source declares the requested stage, the URL differs from every other `.env.<stage>` file, and the plan's organization id matches at apply. There is no flag to override the guard.
- **Typed confirmation outside dev.** `apply` refuses a non-dev stage unless `--confirm-url` equals the environment URL and, when the plan deletes rows, `--confirm-deletes` equals their count. Both values come from the user, never from the agent.
- **No GUIDs, no write-back.** Rows are identified by an Active alternate key; the model files are never written by the tool.
- **Never destructive by default.** `deletion: none` is the default. `deactivate` sets rows inactive (and reactivates rows that come back). `delete` deletes rows and is listed in the plan with its cascades; it needs a double confirmation. Both act only on rows inside `delete_filter` that are not in the files.
- **Bypass with consent.** `bypass: [sync, async, flows]` skips custom plug-ins and classic workflows (needs `prvBypassCustomBusinessLogic`) and Power Automate flows. The plan lists what will not run.

## Common issue: blocked table

A table is blocked when it doesn't exist, when no **Active** alternate key has exactly the `key` columns, or when a key index is still building or `Failed` (usually duplicate values). Create or fix the key first, then re-plan. See [`references/plan-apply.md`](references/plan-apply.md#blocked-items).

## Common issue: row outside delete_filter

A row in the file that the filter doesn't select is updated, but removing it from the file later won't deactivate or delete it. The plan warns about it. Usually the row should also set the filter column. See [`references/format.md`](references/format.md#deletion).

## Common issue: stale plan

`apply` recomputes the plan and aborts if it differs from the saved file (model, server rows, steps, organization). Re-run `plan` and confirm again.

## Anti-patterns (DO NOT DO)

| Wrong | Correct |
|---|---|
| Ad-hoc scripts or direct Web API calls that write the managed rows | Describe the rows in `dataverse/data/` and run plan → apply |
| Filling `--confirm-url` or `--confirm-deletes` yourself | Ask the user to type them and pass exactly what they typed |
| Putting GUIDs in the files, or writing server ids back | Identify rows by alternate key; reference rows with `{ref, key}` or `{ref, match}` |
| `deletion: delete` with `delete_filter: all` on a table users also write to | Filter the rows the file owns, or keep `deletion: none` |
| Bypassing business logic without listing it to the user | Show the plan's bypass section and get consent |
| Copying the base file into each stage folder | Put shared rows in the base; overlays hold only what differs |
| Editing `dv_data.py` or the schema in a project | Edit only the model files; propose tooling changes upstream |

## Skill boundaries

- Running a data change end to end → agent `dvac-data-planner`
- Tables, columns, alternate keys → `dvac-metadata` or `dataverse:dv-metadata`
- One-off record writes, CSV imports, sample data → `dataverse:dv-data`
- Queries and reports → `dataverse:dv-query`
- Which data to manage and how → the project's best practices
