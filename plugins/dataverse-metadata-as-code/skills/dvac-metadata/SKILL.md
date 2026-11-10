---
name: dvac-metadata
description: >
  Dataverse metadata as code: global Choices, tables, columns (including Power Fx
  formula columns), lookups and alternate keys are described in
  dataverse/metadata/*.dv.metadata.yaml and reconciled with the dev environment
  through a Terraform-style plan → confirm → apply loop that never deletes. Use
  when the user wants to create or change Dataverse tables, columns, formula
  columns, Choices, lookups or keys, write or fix .dv.metadata.yaml files, preview schema
  changes (plan), apply the model, or detect drift between the model and the
  environment.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep, WebSearch
argument-hint: "<schema change request>"
metadata:
  author: dataverse-as-code
  version: "3.0.0"
---

## Run

Every step is one command; your only job is the YAML. `DV` means `python3 <base directory>/scripts/dv_metadata.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

1. Edit the YAML under `dataverse/metadata/` ([format](references/format.md)).
2. Run `DV plan dataverse/metadata`, adding `--env-url <url> --stage dev [--tenant <id>]` when the user gave the environment.
3. Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `changes-pending` | Ask the user to confirm the change list — twice for each `DOUBLE CONFIRMATION` line. Then run the `NEXT` line exactly as printed. |
| `no-changes` | Done. |
| `blocked` | Fix the YAML ([blocked changes](references/plan-apply.md#blocked-changes)), back to step 2. |
| `error` | Do what `FIX` says (often: ask the user something). Don't investigate further. |

4. After `apply`: `applied` → done, remind the user to commit `dataverse/metadata/`. `stale-plan` → back to step 2. `error` → show `REASON` and `FIX`, stop.

To bring existing tables into the model, use `dvac-metadata-import` on a solution exported and unpacked with `dataverse:dv-solution` — never write the YAML of an existing table from memory.

Not in this loop: `validate` (plan validates; in Claude Code the hook also validates on save), `doctor` (only to diagnose an `error`), `--json`, and any other read of Dataverse or ad-hoc script — the plan is the only read of the environment.

**Large changes.** Delegate the YAML to the `dvac-metadata-planner` agent. Its prompt must hold `DV=<the absolute command>`, the request and the project conventions to apply. It returns the plan output; you run steps 3–4.

## Official skill

`dataverse:dv-metadata` owns how Dataverse metadata is created and modified (SDK, Web API, solution headers, publishing, error codes). Load it only when you need Dataverse know-how the format doesn't give (procedures, error codes). If it is not installed, **strongly recommend the user install it** before proceeding. This skill adds a declarative model and the tooling that reconciles it. It does not restate the official procedures.

## Best practices

This skill defines the model format and the tooling, **not** how to design a schema. Names, column types, ownership, global vs. local design choices and review rules are the project's best practices. The validator only enforces Dataverse platform rules (publisher prefix, types, references, API constraints).

Apply the conventions already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, follow the user's request and the format rules; best-practice skills such as the `powerplatform-bestpractices` marketplace (`ppbp-*`) can be added.

## Connection

- **Azure CLI token only.** Sign in with `az login`, adding `--tenant <TENANT_ID>` when the account spans several tenants.
- **The tool needs the dev environment URL and its declared stage (`dev`)**, plus the tenant when the account spans several. It takes them from one source: `--env-url/--stage/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else a `<repo-root>/.env.dev` file. Details in [`references/prerequisites.md`](references/prerequisites.md#3-dev-environment-inputs).
- If none is available, ask the user for the URL and tenant. Suggest a committed `.env.dev` as a convenient option (for example created by `ppbp-init`), never require it.

## Repository layout

```
<repo-root>/
├── .env.dev                              # optional — one way to describe the dev environment
├── .dv-metadata/                         # plan files (gitignored)
└── dataverse/
    └── metadata/                         # SOURCE OF TRUTH — metadata as code
        ├── _meta.dv.metadata.yaml        # publisher prefix, solution, locale
        ├── choices.dv.metadata.yaml      # every global Choice
        └── tables/<key>.dv.metadata.yaml # one table per file; file name = key
```

- **`dataverse/metadata/` is the source of truth.** If the project keeps a human-readable view of the model (ERD, design docs), update it in the same change — `dvac-metadata-docs` regenerates a Mermaid ERD from the YAML; the YAML wins on conflict.
- The `*.dv.metadata.yaml` suffix leaves room for future `dataverse/*` model families that follow the same pattern.

## What this skill adds

A declarative model (`dataverse/metadata/*.dv.metadata.yaml`), one tool (`scripts/dv_metadata.py`; every subcommand is in [`references/plan-apply.md`](references/plan-apply.md#subcommands)), a validation hook for Claude Code, and an optional planner agent for large YAML changes.

## Workflow: plan → confirm → apply

The loop is the one in [Run](#run). The user confirms the target (environment and solution, at the top of the plan) and the exact change list, and **twice** each `DOUBLE CONFIRMATION` line:

- an organization-owned table (ownership is permanent)
- a lookup with cascade delete (child rows are deleted with the parent)

Never run `apply` without a plan the user has just seen, and never edit the `NEXT` line.

## Model files

```yaml
# dataverse/metadata/_meta.dv.metadata.yaml
version: "1.0"
publisher:
  prefix: prfx
  scope: prfx_crm_         # optional — limits the "? unmanaged" report to prfx_crm_… components
  solution: prfx_crm_Core  # unmanaged solution in dev
locale: en
```

- `choices.dv.metadata.yaml` holds every global Choice.
- `tables/<key>.dv.metadata.yaml` holds one table per file, and the file name must equal `key`.
- Labels are plain strings in the base language, or `{LCID: text}` maps when `languages` is set in `_meta` — see [`references/format.md`](references/format.md#localization).
- YAML keys are **Schema Names**. They must start with the publisher prefix; the rest follows the project's best practices.
- Every attribute, per column type, is listed in [`references/format.md`](references/format.md). Annotated samples are in [`references/samples/`](references/samples/).
- Attributes you leave out of the YAML are **not managed**. The tool never resets them on the server. The exceptions are `label`, `type`, `required`/`required_level` and table `auditing`, which always have a managed default.

## Safety model

- **Dev only, enforced by the tool.** Whatever the source of the environment inputs, the tool refuses to run unless all of these hold:
  - the target is declared as stage `dev`
  - its URL differs from the ones in `.env.uat` and `.env.prod`, when those files exist
  - the model's solution exists **and is unmanaged** (checked on the server)

  UAT and prod receive changes only as managed solution imports, through the project's ALM pipeline. There is no flag to override the guard.
- **Never destructive.** Changes that would need a delete or recreate are `! blocked`. Server components that aren't in the model are `? unmanaged`, reported only. Removing anything is a manual, deliberate act outside this tool.

## Common issue: blocked changes

A column type change (including a formula's `result_type`), a change of bound Choice, an ownership change or a primary-column change cannot be applied in place. Either revert the YAML to match the server, or plan a new column plus a data migration. The full list and the alternatives are in [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: changes made in the maker portal

A change made directly in the environment shows up as `~ update` (the model would revert it) or as `? unmanaged`. Bring the change into the YAML **before** applying, or the next apply overwrites it — `dvac-metadata-import` does it from the exported, unpacked solution.

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Writing ad-hoc Python/PowerShell scripts that create tables or columns | Describe the change in `dataverse/metadata/` and run plan → apply |
| Running `apply` without showing the plan to the user, or reusing an old plan | Plan, show it, get confirmation, then apply that plan file |
| Pointing the tool at UAT/prod by declaring it as `dev` | Only dev holds the unmanaged solution; promote through the ALM pipeline |
| Deleting server components that the plan reports as unmanaged | Either add them to the model or remove them manually after a deliberate decision — never from the agent |
| Editing `dv_metadata.py` or the JSON Schemas in a project | Edit only the model files; propose tooling changes upstream |
| Inline option lists on a choice column | The format has none — reference a global Choice with `choice:` |

## Skill boundaries

- Large YAML changes → agent `dvac-metadata-planner` (optional; apply stays in the main thread)
- ERD of the model → `dvac-metadata-docs`
- Model from an unpacked solution (adoption, realignment) → `dvac-metadata-import`
- Plug-in registrations as code → `dvac-plugins`
- Schema design conventions and reviews → the project's best practices
- Forms, views, N:N relationships, calculated/rollup columns → `dataverse:dv-metadata` (outside this model)
- Solution creation, export, pull-to-repo → `dataverse:dv-solution`
- Promotion to UAT/prod → the project's ALM pipeline

## Migration from 2.x

- `plan` exits 0 once the plan is computed and ends with a `STATUS:` footer. CI that relied on exit code 2 must pass `--detailed-exitcode`.
- The planner agent no longer applies (`approved: <plan file>` is gone): the main thread runs the printed `NEXT` command.
- The tool installs its Python dependencies in a cached venv on its first run.
