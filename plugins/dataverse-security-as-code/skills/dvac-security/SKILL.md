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
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch
argument-hint: "<security role change request>"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

`dataverse:dv-security` owns how Dataverse security works (roles, privileges, access levels, business units, teams, role assignment), and `dataverse:dv-solution` owns solutions. Load them before this skill. If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding. This skill adds a declarative model and the tooling that reconciles it. It does not restate the official procedures.

## Best practices

This skill defines the model format and the tooling, **not** how to design security. Role names, the split between roles, access levels and least-privilege reviews are the project's best practices. The validator only enforces platform rules (valid levels, existing tables and privileges, levels each privilege accepts) and tool rules (one file per role, unique names).

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
├── .env.dev                               # optional — one way to describe the dev environment
├── .dv-security/                          # plan files (gitignored)
└── dataverse/
    └── security/                          # SOURCE OF TRUTH — security roles as code
        ├── _meta.dv.security.yaml         # format version, solution
        └── roles/<file>.dv.security.yaml  # one role per file; file name is free
```

## What this skill adds

- **One declarative model.** The YAML files under `dataverse/security/` are the source of truth for the roles they describe.
- **One tool**, `scripts/dv_security.py`, with four subcommands:

| Subcommand | Touches the server | Purpose |
|---|---|---|
| `doctor [model-dir]` | read | Checks deps, the `az` session, the dev guard, the root business unit and that the solution is unmanaged |
| `validate <dir\|file>` | no | Structural (JSON Schema) + semantic checks (unique role names, duplicates) |
| `plan <model-dir> [--json]` | read | Diff → `+ create` / `~ update` / `+ grant` / `~ depth` / `- revoke` / `! blocked` / `? unmanaged` |
| `apply <model-dir> --plan-file <f>` | write | Runs exactly the saved plan. Aborts if the plan is stale. Never deletes a role. |

- **A PostToolUse hook** (Claude Code). It runs `validate` after every edit to a `*.dv.security.yaml` file. On GitHub Copilot, run `validate` explicitly.
- **A planner agent**, `dvac-security-planner`, that runs the whole loop with a user confirmation gate.

Run the script from the plugin's skill folder: `python3 <this-skill>/scripts/dv_security.py …`. Install its dependencies first with `pip install -r <this-skill>/scripts/requirements.txt`, inside a venv.

## Workflow: plan → confirm → apply

Delegate role changes to the **`dvac-security-planner`** agent. It runs in two invocations:

1. **Planning run.** The agent edits the YAML model, validates it, runs `plan`, and returns the target environment and solution, every create, update, grant, level change, revoke, blocked and unmanaged item, and the items that need double confirmation.
2. **The main thread shows that plan to the user.** Ask for explicit confirmation of the environment and of the exact change list. Ask **twice** for every `- revoke` (users holding the role lose that access).
3. **Apply run.** Re-invoke the agent with `approved: <plan file>`. It runs `apply`, then checks that the follow-up plan is empty.

Never run `apply` without a plan the user has just seen. Never apply a plan that contains blocked items.

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

## Common issue: stale plan

`apply` recomputes the plan and aborts if it differs from the saved file (someone changed the model or the roles after `plan`). Re-run `plan` and get the user's confirmation again. See [`references/plan-apply.md`](references/plan-apply.md).

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
| Listing only the privileges to add to an existing role | The file is the complete set — import the role's current privileges first, or everything else is revoked |
| Pointing the tool at UAT/prod by declaring it as `dev` | Only dev holds the unmanaged solution; promote through the ALM pipeline |
| Deleting roles reported as unmanaged | Add them to the model, or remove them manually after a deliberate decision |
| Editing `dv_security.py` or the JSON Schemas in a project | Edit only the model files; propose tooling changes upstream |

## Skill boundaries

- Running a role change end to end → agent `dvac-security-planner`
- Tables, columns, Choices as code → `dvac-metadata`
- Assigning roles to users or teams, business units, column security profiles → `dataverse:dv-security` (outside this model)
- Solution creation, export, pull-to-repo → `dataverse:dv-solution`
- Role design and least-privilege reviews → the project's best practices
- Promotion to UAT/prod → the project's ALM pipeline
