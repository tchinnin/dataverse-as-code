# dataverse-as-code

A plugin marketplace for Claude Code and GitHub Copilot that manages Dataverse **as code**: declarative YAML models, committed with the project, reconciled with your environments through a Terraform-style **plan → confirm → apply** loop.

## What this is

Each plugin ships a declarative model format (JSON Schemas + samples), a Python tool (`plan` / `apply`, plus `validate` / `doctor`) whose output ends with a fixed `STATUS` / `NEXT` footer, a validation hook, and an optional planner agent for large YAML changes. Your coding agent only writes the YAML: planning and applying are commands it runs as printed, after your confirmation.

Customizations (metadata, plug-ins, security roles) only ever target the **dev** environment's unmanaged solution; UAT and prod receive them through the managed solution and your ALM pipeline. Data is not part of a solution, so the data tooling targets a named stage — dev, UAT or prod — and, outside dev, applies only after the user has typed the environment URL.

## Your conventions, not ours

dataverse-as-code is an implementation, not a set of best practices. The validators only enforce Dataverse platform rules (publisher prefix, types, references, API constraints). How you name tables and columns, design steps or structure plug-in projects is up to you: write the model following your own best practices, or none.

## Recommended companion

We suggest pairing this marketplace with [`powerplatform-bestpractices`](https://github.com/tchinnin/powerplatform-bestpractices-skills) (`ppbp-*` skills). It provides opinionated conventions for Dataverse schema and plug-ins, and its `ppbp-init` skill creates the `.env.dev` file this tooling can read. It is optional: nothing here depends on it.

## Plugins

| Plugin | Skill / agent | Model |
|---|---|---|
| `dataverse-metadata-as-code` | `dvac-metadata` / `dvac-metadata-planner`, `dvac-metadata-docs`, `dvac-metadata-solution` | `dataverse/metadata/**/*.dv.metadata.yaml` — global Choices, tables, columns, lookups, alternate keys. Never deletes. `dvac-metadata-docs` generates a Mermaid ERD of the model; `dvac-metadata-solution` imports the model from an unpacked solution or compares the two. |
| `dataverse-plugins-as-code` | `dvac-plugins` / `dvac-plugins-planner`, `dvac-plugins-docs`, `dvac-plugins-solution` | `<PluginName>.dv.plugin.yaml`, in the same folder as the plug-in's `.csproj` — plug-in package or assembly, steps, images, Custom APIs. First upload via the Web API, then `pac plugin push`. `dvac-plugins-docs` documents the execution pipeline and the Custom APIs with Mermaid diagrams; `dvac-plugins-solution` imports the files from an unpacked solution or compares the two. |
| `dataverse-data-as-code` | `dvac-data` / `dvac-data-planner` | `dataverse/data/<table>.dv.data.yaml` + `dataverse/data/<stage>/` overlays — configuration and reference rows, identified by alternate key, for dev, UAT or prod. Deactivates or deletes only on opt-in, within a `delete_filter`. |
| `dataverse-security-as-code` | `dvac-security` / `dvac-security-planner`, `dvac-security-docs`, `dvac-security-solution` | `dataverse/security/roles/*.dv.security.yaml` — security roles of the root business unit, each with its complete privilege set (table privileges per access level, miscellaneous privileges). Grants, changes levels and revokes (double confirmation); never deletes a role. `dvac-security-docs` documents the roles functionally: a tables × roles access matrix and a Mermaid diagram per role. `dvac-security-solution` imports the roles from an unpacked solution or compares the two. |

## Example

A short walk through both plugins: describe the change in YAML, review the plan, apply it. You normally just ask your agent (*"add an Order table linked to Customer"*); with the skill loaded it writes the YAML, runs `plan`, shows you the result and runs the printed `apply` command only after you confirm. The commands below are what it runs. Names follow one example scheme — use your own.

### 1. Describe the metadata

`dataverse/metadata/_meta.dv.metadata.yaml` — the publisher and the dev solution:

```yaml
version: "1.0"
publisher:
  prefix: prfx
  solution: prfx_crm_Core
locale: en
```

`dataverse/metadata/choices.dv.metadata.yaml` — global Choices:

```yaml
choices:
  prfx_crm_choice_OrderStatus:
    label: Order Status
    options:
      - {value: 100000000, label: Draft,     color: "#9EA6B3"}
      - {value: 100000001, label: Confirmed, color: "#1B8ED4"}
      - {value: 100000002, label: Shipped,   color: "#37B400"}
```

`dataverse/metadata/tables/prfx_crm_Order.dv.metadata.yaml` — a table, its columns and a lookup to an existing `prfx_crm_Customer` table:

```yaml
key: prfx_crm_Order
label: Order
label_plural: Orders
ownership: user
table_type: standard

columns:
  prfx_OrderNumber:
    label: Order Number
    type: autonumber
    primary: true
    format: "ORD-{SEQNUM:6}"

  prfx_CustomerId:
    label: Customer
    type: lookup
    target: prfx_crm_Customer
    cascade: restrict
    required: true

  prfx_StatusCode:
    label: Status
    type: choice
    choice: prfx_crm_choice_OrderStatus
    default: 100000000

  prfx_Amount:
    label: Amount
    type: decimal
    min: 0
    max: 1000000000
    precision: 2
```

### 2. Describe the plug-ins

`dataverse/plugins/Contoso.Crm.Plugins/Contoso.Crm.Plugins.dv.plugin.yaml` — next to the `.csproj` (the only location rule; the folder is an example):

```yaml
version: "1.0"
publisher:
  prefix: prfx
  solution: prfx_crm_Core
package:
  name: prfx_Contoso.Crm.Plugins
  nupkg: bin/Release/*.nupkg
  id: null                           # written by apply after the first upload

steps:
  - plugin: Contoso.Crm.Plugins.OrderPreCreate
    message: Create
    table: prfx_crm_order
    stage: pre-operation

  - plugin: Contoso.Crm.Plugins.OrderStatusChanged
    message: Update
    table: prfx_crm_order
    stage: post-operation
    mode: async
    filtering_attributes: [prfx_statuscode]
    images:
      - name: PreImage
        type: pre
        attributes: [prfx_statuscode, prfx_customerid]
```

### 3. Plan

The metadata plan is a read-only diff against the dev environment, saved to `.dv-metadata/plan.json`:

```
$ python3 dv_metadata.py plan dataverse/metadata
Dataverse metadata plan
  env      : https://contoso-dev.crm4.dynamics.com (/repo/.env.dev)
  solution : prfx_crm_Core (unmanaged)
  az user  : dev@contoso.com (tenant 0000…)

  + create  choice  prfx_crm_choice_OrderStatus  3 options
  + create  table   prfx_crm_Order  primary prfx_OrderNumber: autonumber
  + create  column  prfx_crm_Order.prfx_StatusCode  choice
  + create  column  prfx_crm_Order.prfx_Amount  decimal
  + create  lookup  prfx_crm_Order.prfx_CustomerId  → prfx_crm_Customer (cascade restrict)

Summary: 5 to create, 0 to update, 0 blocked, 0 unmanaged.

STATUS: changes-pending
PLAN FILE: /repo/.dv-metadata/plan.json
DOUBLE CONFIRMATION: none
NEXT (only after the user confirms): python3 /…/dv_metadata.py apply /repo/dataverse/metadata --plan-file /repo/.dv-metadata/plan.json
```

Every `plan` ends with this footer. The agent shows the output to you as is, asks for your confirmation (twice for each `DOUBLE CONFIRMATION` line) and then runs the `NEXT` line — no interpretation, no rebuilt command.

The plug-in plan compares the build output and the registrations, saved to `.dv-plugin/plan.json`:

```
$ python3 dv_plugin.py plan dataverse/plugins/Contoso.Crm.Plugins
Dataverse plug-in plan
  env      : https://contoso-dev.crm4.dynamics.com (/repo/.env.dev)
  az user  : dev@contoso.com (tenant 0000…)
  package  : prfx_Contoso.Crm.Plugins → prfx_crm_Core (unmanaged)  dataverse/plugins/Contoso.Crm.Plugins/Contoso.Crm.Plugins.dv.plugin.yaml
             dataverse/plugins/Contoso.Crm.Plugins/bin/Release/Contoso.Crm.Plugins.1.0.0.nupkg  sha256 1f0c9a7e5b21

  + create  package    prfx_Contoso.Crm.Plugins  Contoso.Crm.Plugins.1.0.0.nupkg version 1.0.0
  + create  step       Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order  Create prfx_crm_order pre-operation sync
  + create  step       Contoso.Crm.Plugins.OrderStatusChanged: Update of prfx_crm_order  Update prfx_crm_order post-operation async
  + create  image      Contoso.Crm.Plugins.OrderStatusChanged: Update of prfx_crm_order / PreImage  pre (prfx_statuscode, prfx_customerid)

Summary: 4 to create, 0 to update, 0 to push, 0 to delete, 0 blocked, 0 unmanaged.

STATUS: changes-pending
PLAN FILE: /repo/.dv-plugin/plan.json
DOUBLE CONFIRMATION:
  - create package prfx_Contoso.Crm.Plugins
NEXT (only after the user confirms): python3 /…/dv_plugin.py apply /repo/dataverse/plugins/Contoso.Crm.Plugins --plan-file /repo/.dv-plugin/plan.json
```

A plan can also show `~ update` (changed in place), `↑ push` (new package content), `- delete` (a step or image removed from the model — asks for a second confirmation), `!` blocked changes that Dataverse cannot make in place, and `?` unmanaged components that are reported but never deleted. See each skill's `references/plan-apply.md`.

### 4. Apply

Once you have confirmed the plan, `apply` runs exactly that plan — it aborts if the model or the server changed since — then plans again and expects no difference:

```
$ python3 dv_metadata.py apply dataverse/metadata --plan-file .dv-metadata/plan.json
Applying 5 change(s) to https://contoso-dev.crm4.dynamics.com (prfx_crm_Core)
  ✓ create choice  prfx_crm_choice_OrderStatus
  ✓ create table   prfx_crm_Order
  ✓ create column  prfx_crm_Order.prfx_StatusCode
  ✓ create column  prfx_crm_Order.prfx_Amount
  ✓ create lookup  prfx_crm_Order.prfx_CustomerId
  ✓ publish

5/5 change(s) applied.
Post-apply plan is empty — the environment matches the model.

STATUS: applied
```

```
$ python3 dv_plugin.py apply dataverse/plugins/Contoso.Crm.Plugins --plan-file .dv-plugin/plan.json
Applying 4 change(s) to https://contoso-dev.crm4.dynamics.com
  ✓ create package    prfx_Contoso.Crm.Plugins
  ✓ create step       Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order
  ✓ create step       Contoso.Crm.Plugins.OrderStatusChanged: Update of prfx_crm_order
  ✓ create image      Contoso.Crm.Plugins.OrderStatusChanged: Update of prfx_crm_order / PreImage

4/4 change(s) applied.
Post-apply plan is empty — the environment matches the model.

STATUS: applied
```

After the first upload, `apply` writes the package `id` into the YAML; commit it with the model. Every component lands in `prfx_crm_Core`, ready to export with your ALM pipeline.

## Installation

Claude Code:

```sh
/plugin marketplace add tchinnin/dataverse-as-code
/plugin install dataverse-metadata-as-code@dataverse-as-code
/plugin install dataverse-plugins-as-code@dataverse-as-code
/plugin install dataverse-data-as-code@dataverse-as-code
/plugin install dataverse-security-as-code@dataverse-as-code
```

GitHub Copilot CLI:

```sh
copilot plugin marketplace add tchinnin/dataverse-as-code
copilot plugin install dataverse-metadata-as-code@dataverse-as-code
copilot plugin install dataverse-plugins-as-code@dataverse-as-code
copilot plugin install dataverse-data-as-code@dataverse-as-code
copilot plugin install dataverse-security-as-code@dataverse-as-code
```

Also install the official Microsoft `dataverse` plugin — the foundation these skills build on. Optionally, add the recommended companion:

```sh
/plugin marketplace add tchinnin/powerplatform-bestpractices-skills
/plugin install ppbp-overview@powerplatform-bestpractices
```

## Prerequisites

- Python ≥ 3.9 (the tools install their own dependencies), the Azure CLI (`az login`), and the PAC CLI for plug-ins.
- The dev environment, given in any one of these ways:
  - command-line flags: `--env-url <url> --stage dev [--tenant <id>]`
  - environment variables: `DATAVERSE_URL`, `ENVIRONMENT_STAGE=dev`, `TENANT_ID`
  - a `<repo-root>/.env.dev` file with the same keys (for example created by `ppbp-init`)
- An unmanaged solution in that environment.

The metadata, plug-in and security tools refuse to run unless the target is declared as `dev` and the solution is unmanaged. The data tool takes `--stage <stage>` and reads `<repo-root>/.env.<stage>` (or the flags or variables); the source must declare that stage. Details are in each skill's `references/prerequisites.md`.

## Fewer permission prompts

A plugin can't ship permission rules, so allow the read-only commands yourself — `plan`, `validate`, `doctor` — and keep the prompt on `apply`: it is one more barrier before a write.

- **Claude Code** (`.claude/settings.json` or `~/.claude/settings.json`):

  ```json
  {"permissions": {"allow": ["Bash(python3 *dv_*.py plan *)", "Bash(python3 *dv_*.py validate *)", "Bash(python3 *dv_*.py doctor*)"]}}
  ```
- **GitHub Copilot CLI:** approve `plan` once with "always allow" when prompted, or start with `--allow-tool 'shell(python3:*)'` only if you accept every `python3` command (apply included).
- **VS Code** (`settings.json`):

  ```json
  {"chat.tools.terminal.autoApprove": {"/dv_\\w+\\.py (plan|validate|doctor)\\b/": true}}
  ```

The Python dependencies need no setup: on its first run each tool installs them in a cached venv shared by every dataverse-as-code tool.

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
        └── skills/
            ├── dvac-<domain>/SKILL.md (+ references/, scripts/, tests/)
            ├── dvac-<domain>-docs/SKILL.md (+ references/, scripts/, tests/)     # optional, offline documentation
            └── dvac-<domain>-solution/SKILL.md (+ references/, scripts/, tests/) # optional, import/compare with an unpacked solution
```

Authoring standards, naming, versioning and commit conventions are in [AGENTS.md](./AGENTS.md).
