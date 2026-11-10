---
name: dvac-security
description: >
  Dataverse security as code: security roles and their complete privilege set
  (table privileges per access level, miscellaneous privileges) are described in
  dataverse/security/roles/*.dv.security.yaml and reconciled with the dev
  environment's unmanaged solution through a Terraform-style plan → confirm → apply
  loop that grants, changes levels and revokes privileges but never deletes a role.
  Use when the user wants to create or change Dataverse security roles or their
  privileges, write or fix .dv.security.yaml files, preview security changes (plan),
  apply the model, or detect drift between the roles and the environment.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep, WebSearch
argument-hint: "<security role change request>"
metadata:
  author: dataverse-as-code
  version: "1.1.1"
---

## Run

Every step is one command; your only job is the YAML. `DV` means `python3 <base directory>/scripts/dv_security.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

1. Edit the role files under `dataverse/security/roles/` ([format](references/format.md)). To bring an existing role into the model, run `DV adopt dataverse/security "<role name>"` first — it writes the role's complete privilege set. Never write an existing role from memory.
2. Run `DV plan dataverse/security`, adding `--env-url <url> --stage dev [--tenant <id>]` when the user gave the environment.
3. Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `changes-pending` | Ask the user to confirm the change list — twice for each `DOUBLE CONFIRMATION` line (revokes). Then run the `NEXT` line exactly as printed. |
| `no-changes` | Done. |
| `blocked` | Fix the YAML ([blocked changes](references/plan-apply.md#blocked-changes)), back to step 2. |
| `error` | Do what `FIX` says (often: ask the user something). Don't investigate further. |

4. After `apply`: `applied` → done, remind the user to commit `dataverse/security/`. `stale-plan` → back to step 2. `error` → show `REASON` and `FIX`, stop.

Not in this loop: `validate` (plan validates; in Claude Code the hook also validates on save), `doctor` (only to diagnose an `error`), `--json`, and any other read of Dataverse or ad-hoc script — the plan is the only read of the environment.

**Large changes.** Delegate the YAML to the `dvac-security-planner` agent. Its prompt must hold `DV=<the absolute command>`, the request and the project conventions to apply. It returns the plan output; you run steps 3–4.

## Official skill

`dataverse:dv-security` owns how Dataverse security works (roles, privileges, access levels, business units, teams, role assignment), and `dataverse:dv-solution` owns solutions. Load it only when you need Dataverse know-how the format doesn't give (procedures, error codes). If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding. This skill adds a declarative model and the tooling that reconciles it. It does not restate the official procedures.

## Best practices

This skill defines the model format and the tooling, **not** how to design security. Role names, the split between roles, access levels and least-privilege reviews are the project's best practices. The validator only enforces platform rules (valid levels, existing tables and privileges, levels each privilege accepts) and tool rules (one file per role, unique names).

Apply the conventions already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, follow the user's request and the format rules; best-practice skills such as the `powerplatform-bestpractices` marketplace (`ppbp-*`) can be added.

## Connection

- **Azure CLI token only.** Sign in with `az login`, adding `--tenant <TENANT_ID>` when the account spans several tenants.
- **The tool needs the dev environment URL and its declared stage (`dev`)**, plus the tenant when the account spans several. It takes them from one source: `--env-url/--stage/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else a `<repo-root>/.env.dev` file. Details in [`references/prerequisites.md`](references/prerequisites.md#3-dev-environment-inputs).
- If none is available, ask the user for the URL and tenant. Suggest a committed `.env.dev` as a convenient option (for example created by `ppbp-init`), never require it.

## Repository layout

```
<repo-root>/
├── .env.dev                               # optional — one way to describe the dev environment
├── .dv-security/                          # plan files (gitignored)
└── dataverse/
    └── security/                          # SOURCE OF TRUTH — security roles as code
        ├── _meta.dv.security.yaml         # format version, solution
        └── roles/<file>.dv.security.yaml  # one role per file; file name is free
```

## What this skill adds

A declarative model (`dataverse/security/roles/*.dv.security.yaml`), one tool (`scripts/dv_security.py`; every subcommand is in [`references/plan-apply.md`](references/plan-apply.md#subcommands)), a validation hook for Claude Code, and an optional planner agent for large YAML changes.

## Workflow: plan → confirm → apply

The loop is the one in [Run](#run). The user confirms the target (environment and solution) and the exact change list, and **twice** each `DOUBLE CONFIRMATION` line: every `- revoke`, because users holding the role lose that access.

Never run `apply` without a plan the user has just seen, and never edit the `NEXT` line.

## Model files

```yaml
# dataverse/security/roles/order-clerk.dv.security.yaml
name: Order Clerk                 # identity: role name in the root business unit
description: Creates and follows up orders   # optional — left out = not managed
member_inheritance: direct_and_team          # optional — team_only | direct_and_team
tables:                           # key = table logical name
  prfx_crm_order: {create: user, read: business_unit, write: user, append: user, append_to: user}
  account:        {read: organization}
privileges:                       # other privileges, by name
  prvExportToExcel: organization
```

- Levels: `none`, `user`, `business_unit`, `parent_child`, `organization`. Verbs: `create`, `read`, `write`, `delete`, `append`, `append_to`, `assign`, `share`.
- **A role file is the role's complete privilege set.** A verb or privilege left out means no access: if the role holds it in dev, the plan revokes it.
- Every attribute is in [`references/format.md`](references/format.md); annotated samples in [`references/samples/`](references/samples/).

## Safety model

- **Dev only, enforced by the tool.** It refuses to run unless the target is declared as stage `dev`, its URL differs from `.env.uat` / `.env.prod` when those files exist, and the model's solution exists **and is unmanaged**. Roles reach UAT and prod only inside the managed solution. There is no flag to override the guard.
- **Revokes need double confirmation.** `- revoke` is the only reducing change the tool can make. The plan lists each one under `double_confirmation`.
- **Never deletes a role.** Roles of the solution that the model doesn't describe are `? unmanaged`, reported only. Managed or non-customizable roles are `! blocked`: the tool only manages roles created in dev.

## Common issue: blocked privileges

An unknown table or privilege, a verb the table doesn't have, or a level the privilege doesn't accept (organization-owned tables accept only `organization`) is blocked. Fix the YAML and re-plan. The full list is in [`references/plan-apply.md`](references/plan-apply.md#blocked-changes).

## Common issue: changes made in the maker portal

A privilege changed in the role editor shows up as `+ grant`, `~ depth` or `- revoke` — the model would revert it. Bring the change into the YAML **before** applying, or the next apply undoes it.

## Common issue: users can't open the app

A role holding only the project's table privileges is often not enough to open a model-driven app: Dataverse also needs privileges on system tables. List them explicitly in the role file — the official skill `dataverse:dv-security` owns which ones. See [`references/format.md`](references/format.md#a-complete-privilege-set).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing roles in the maker portal, or with ad-hoc scripts | Describe the role in `dataverse/security/roles/` and run plan → apply |
| Running `apply` without showing the plan, or reusing an old plan | Plan, show it, get confirmation (twice for revokes), then apply that plan file |
| Listing only the privileges to add to an existing role, or writing it from memory | Run `adopt` first: it writes the complete set, so the plan starts empty |
| Pointing the tool at UAT/prod by declaring it as `dev` | Only dev holds the unmanaged solution; promote through the ALM pipeline |
| Deleting roles reported as unmanaged | Add them to the model, or remove them manually after a deliberate decision |
| Editing `dv_security.py` or the JSON Schemas in a project | Edit only the model files; propose tooling changes upstream |

## Skill boundaries

- Large role changes → agent `dvac-security-planner` (optional; apply stays in the main thread)
- Functional documentation of the roles (access matrix, diagram per role) → `dvac-security-docs`
- Tables, columns, Choices as code → `dvac-metadata`
- Assigning roles to users or teams, business units, column security profiles → `dataverse:dv-security` (outside this model)
- Solution creation, export, pull-to-repo → `dataverse:dv-solution`
- Role design and least-privilege reviews → the project's best practices
- Promotion to UAT/prod → the project's ALM pipeline
