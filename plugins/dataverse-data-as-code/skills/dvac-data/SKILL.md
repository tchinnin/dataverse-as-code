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
allowed-tools: Read, Write, Edit, Bash, Grep, WebSearch
argument-hint: "<data change request>"
metadata:
  author: dataverse-as-code
  version: "2.0.0"
---

## Run

Every step is one command; your only job is the YAML. `DV` means `python3 <base directory>/scripts/dv_data.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

1. Edit the YAML under `dataverse/data/` ([format](references/format.md)).
2. Run `DV plan dataverse/data --stage <stage>`, adding `--env-url <url> [--tenant <id>]` when the user gave the environment. The stage never defaults: ask the user if it isn't clear.
3. Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `changes-pending` | Ask the user to confirm the stage, the environment and the change list — twice for each `DOUBLE CONFIRMATION` line — and list the bypassed business logic the plan shows. Then run the `NEXT` line exactly as printed, except for the placeholders below. |
| `no-changes` | Done. |
| `blocked` | Fix the YAML ([blocked changes](references/plan-apply.md#blocked-items)), back to step 2. |
| `error` | Do what `FIX` says (often: ask the user something). Don't investigate further. |

4. After `apply`: `applied` → done, remind the user to commit `dataverse/data/`. `stale-plan` → back to step 2. `refused` or `error` → show `REASON` and `FIX`, stop. After `outcome unknown`, never re-apply the same plan: back to step 2.

**Outside dev**, the `NEXT` line holds `<URL TYPED BY THE USER>` (and `<COUNT TYPED BY THE USER>` when rows are deleted). Show the environment URL, ask the user to type it (and the delete count), and put exactly what they typed in place of the placeholders. Never fill them in yourself.

Not in this loop: `validate` (plan validates; in Claude Code the hook also validates on save), `doctor` (only to diagnose an `error`), `--json`, and any other read of Dataverse or ad-hoc script — the plan is the only read of the environment.

**Large changes.** Delegate the YAML to the `dvac-data-planner` agent. Its prompt must hold `DV=<the absolute command>`, the stage, the request and the project conventions to apply. It returns the plan output; you run steps 3–4.

## Official skill

`dataverse:dv-data` owns how Dataverse records are written (Web API, upsert, bulk operations, error codes), and `dataverse:dv-query` owns reads. Alternate keys are metadata, owned by `dataverse:dv-metadata` (or modeled with `dvac-metadata`). Load it only when you need Dataverse know-how the format doesn't give (procedures, error codes). If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding. This skill adds a declarative model and the tooling that reconciles it. It does not restate the official procedures.

## Best practices

This skill defines the model format and the tooling, **not** what data to manage or how to name it. Which tables are seeded, which rows each stage gets, and how ownership is split between files and users are the project's best practices. The validator only enforces Dataverse platform rules and tool rules (keys, references, filters, safety gates).

Apply the conventions already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, follow the user's request and the format rules; best-practice skills such as the `powerplatform-bestpractices` marketplace (`ppbp-*`) can be added.

## Connection

- **Azure CLI token only.** Sign in with `az login`, adding `--tenant <TENANT_ID>` when the account spans several tenants.
- **Every command names its stage** with `--stage <dev|uat|prod|…>`; it never defaults. The URL comes from one source: `--env-url/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else `<repo-root>/.env.<stage>`. The source must declare the same stage. Details in [`references/prerequisites.md`](references/prerequisites.md).
- If no source is available, ask the user for the URL and tenant. Suggest committed `.env.<stage>` files as a convenient option (for example created by `ppbp-init`), never require them.

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

A declarative model (`dataverse/data/**/*.dv.data.yaml`), one tool (`scripts/dv_data.py`; every subcommand is in [`references/plan-apply.md`](references/plan-apply.md#subcommands)), a validation hook for Claude Code, and an optional planner agent for large YAML changes.

## Workflow: plan → confirm → apply

The loop is the one in [Run](#run). The user confirms the stage, the environment and the exact change list, and:

- **twice** each `DOUBLE CONFIRMATION` line (`deactivate` and `delete`, on every stage)
- the bypassed business logic the plan lists (steps, workflows, flows that will not run)
- **outside dev**, by typing the environment URL and, when the plan deletes rows, their count

Never run `apply` without a plan the user has just seen, and never edit the `NEXT` line except for the typed placeholders.

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

## Common issue: failed or unanswered batch

A failed batch writes nothing; `apply` halves it until the failing row is alone and reports that row's own error, then stops. When Dataverse gives no answer (timeout, gateway error), the batch may or may not be written: `apply` stops with `outcome unknown` — never re-apply that plan, re-run `plan`. See [`references/plan-apply.md`](references/plan-apply.md#batches).

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

- Large data changes → agent `dvac-data-planner` (optional; apply stays in the main thread)
- Tables, columns, alternate keys → `dvac-metadata` or `dataverse:dv-metadata`
- One-off record writes, CSV imports, sample data → `dataverse:dv-data`
- Queries and reports → `dataverse:dv-query`
- Which data to manage and how → the project's best practices

## Migration from 1.x

- `plan` exits 0 once the plan is computed and ends with a `STATUS:` footer. CI that relied on exit code 2 must pass `--detailed-exitcode`.
- The planner agent no longer applies (`approved:`, `confirm-url:` and `confirm-deletes:` in its prompt are gone): the main thread runs the printed `NEXT` command with the values the user typed.
- The tool installs its Python dependencies in a cached venv on its first run.
