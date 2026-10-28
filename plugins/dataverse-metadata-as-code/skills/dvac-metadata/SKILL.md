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
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch
argument-hint: "<schema change request>"
metadata:
  author: dataverse-as-code
  version: "2.1.1"
---

## Official skill

`dataverse:dv-metadata` owns how Dataverse metadata is created and modified (SDK, Web API, solution headers, publishing, error codes). Load it before this skill. If it is not installed, **strongly recommend the user install it** before proceeding. This skill adds a declarative model and the tooling that reconciles it. It does not restate the official procedures.

## Best practices

This skill defines the model format and the tooling, **not** how to design a schema. Names, column types, ownership, global vs. local design choices and review rules are the project's best practices. The validator only enforces Dataverse platform rules (publisher prefix, types, references, API constraints).

Before writing the model, look for the project's own best practices (README, `AGENTS.md`, installed skills) and follow them. If there are none, work from the user's request and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

- **Azure CLI token only.** Sign in with `az login`, adding `--tenant <TENANT_ID>` when the account spans several tenants.
- **The tool needs the dev environment URL and its declared stage (`dev`)**, plus the tenant when the account spans several. It takes them from one source: `--env-url/--stage/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else a `<repo-root>/.env.dev` file. Details in [`references/prerequisites.md`](references/prerequisites.md#3-dev-environment-inputs).
- If none is available, ask the user for the URL and tenant. Suggest a committed `.env.dev` as a convenient option (for example created by `ppbp-init`), never require it.
- `dataverse:dv-connect` owns environment discovery and connection diagnostics.
- Before any change, confirm the target URL and the active `az` account with the user.

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

- **One declarative model.** The YAML files under `dataverse/metadata/` are the source of truth for the schema.
- **One tool**, `scripts/dv_metadata.py`, with four subcommands:

| Subcommand | Touches the server | Purpose |
|---|---|---|
| `doctor [model-dir]` | read | Checks deps, the `az` session, the dev guard and that the solution is unmanaged |
| `validate <dir\|file>` | no | Structural (JSON Schema) + semantic checks (platform rules, references) |
| `plan <model-dir> [--json]` | read | Diff → `+ create` / `~ update` (field `old → new`) / `! blocked` / `? unmanaged` |
| `apply <model-dir> --plan-file <f>` | write | Runs exactly the saved plan. Aborts if the plan is stale. Never deletes. |

- **A PostToolUse hook** (Claude Code). It runs `validate` after every edit to a `*.dv.metadata.yaml` file and returns errors to the agent right away. On GitHub Copilot, run `validate` explicitly.
- **A planner agent**, `dvac-metadata-planner`, that runs the whole loop with a user confirmation gate.

Run the script from the plugin's skill folder: `python3 <this-skill>/scripts/dv_metadata.py …`. Install its dependencies first with `pip install -r <this-skill>/scripts/requirements.txt`, inside a venv.

## Workflow: plan → confirm → apply

Delegate schema changes to the **`dvac-metadata-planner`** agent. It runs in two invocations:

1. **Planning run.** The agent edits the YAML model, validates it, runs `plan`, and returns:
   - the target environment and solution
   - every create, update, blocked and unmanaged item
   - the items that need double confirmation
2. **The main thread shows that plan to the user.** Ask for explicit confirmation of the environment and of the exact change list. Ask **twice** for each item that needs double confirmation:
   - Organization-owned tables (ownership is permanent)
   - lookups with cascade delete (child rows are deleted with the parent)
3. **Apply run.** Re-invoke the agent with `approved: <plan file>`. It runs `apply`, then checks that the follow-up plan is empty.

Never run `apply` without a plan the user has just seen. Never apply a plan that contains blocked items.

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
- **Azure CLI token.** The tool authenticates with the developer's `az login` session. Prerequisites and fixes are in [`references/prerequisites.md`](references/prerequisites.md).
- **Never destructive.** Changes that would need a delete or recreate are `! blocked`. Server components that aren't in the model are `? unmanaged`, reported only. Removing anything is a manual, deliberate act outside this tool.

## Common issue: stale plan

`apply` recomputes the plan and aborts if it differs from the saved file. This happens when someone changed the model or the environment after `plan`. Re-run `plan` and get the user's confirmation again. See [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: blocked changes

A column type change (including a formula's `result_type`), a change of bound Choice, an ownership change or a primary-column change cannot be applied in place. Either revert the YAML to match the server, or plan a new column plus a data migration. The full list and the alternatives are in [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: changes made in the maker portal

A change made directly in the environment shows up as `~ update` (the model would revert it) or as `? unmanaged`. Bring the change into the YAML **before** applying, or the next apply overwrites it.

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

- Running a schema change end to end → agent `dvac-metadata-planner`
- ERD of the model → `dvac-metadata-docs`
- Plug-in registrations as code → `dvac-plugins`
- Schema design conventions and reviews → the project's best practices
- Forms, views, N:N relationships, calculated/rollup columns → `dataverse:dv-metadata` (outside this model)
- Solution creation, export, pull-to-repo → `dataverse:dv-solution`
- Promotion to UAT/prod → the project's ALM pipeline
