---
name: dvac-plugins
description: >
  Dataverse plug-in registrations as code: plug-in packages (or legacy assemblies),
  steps, step images and Custom APIs are described in <PluginName>.dv.plugin.yaml
  next to the project and reconciled with the dev environment through a
  Terraform-style plan → confirm → apply loop. Use when the user wants to register,
  change or remove a plug-in step or image, deploy a new build of a plug-in package,
  create or change a Custom API, write or fix .dv.plugin.yaml files, adopt existing
  registrations, or detect drift between the registrations and the environment.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch
argument-hint: "<registration change request>"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

No official Microsoft skill covers plug-in registration. Two parts of the official `dataverse` plugin are relevant, so load both before this skill:

- `dataverse:dv-overview` states the rule this tool implements: the first registration goes through the Web API, later updates through `pac plugin push`.
- `dataverse:dv-connect` owns PAC CLI authentication and the privileges registration needs.

If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding.

## Companion plugins

Plug-in project, step and Custom API conventions are owned by the `ppbp-dataverse-plugins` plugin of the `powerplatform-bestpractices` marketplace (`ppbp-dv-plugins-overview`, `ppbp-dv-plugin-build`, `ppbp-dv-step-plugin`, `ppbp-dv-custom-api`). `ppbp-init` creates `.env.dev`. If those skills are not available, **recommend the user install them**; this skill never restates them.

## Connection

- **The target is described in `<repo-root>/.env.dev`.** The tool never reads any other environment file for its target.
- **The Web API uses the Azure CLI token** (`az login`, with `--tenant <TENANT_ID>` when the account spans several tenants).
- **`pac plugin push` uses the active PAC CLI profile.** The tool always passes it the `.env.dev` URL explicitly.
- Before any change, confirm the target URL, the `az` account and the pac profile with the user.

## Repository layout

```
<repo-root>/
├── .env.dev                              # dev environment descriptor — the only target of the tool
├── .dv-plugin/                           # plan files (gitignored)
└── plugins/
    └── <PluginName>/                     # one subfolder per plug-in project
        ├── <PluginName>.csproj
        └── <PluginName>.dv.plugin.yaml   # REGISTRATIONS AS CODE — package, steps, images, Custom APIs
```

- **`<PluginName>.dv.plugin.yaml` is the source of truth for what is registered.** Commit it with the code, including the `id` the tool records.
- The project layout itself (`PluginBase.cs`, `Models/`, scaffolding) is owned by `ppbp-dv-plugins-overview`.

## What this skill adds

- **One declarative model per plug-in project.** `plugins/<PluginName>/<PluginName>.dv.plugin.yaml` sits next to the `.csproj` and is the source of truth for:
  - the plug-in package (or assembly)
  - its steps and step images
  - the Custom APIs it implements, with their request parameters and response properties
- **One tool**, `scripts/dv_plugin.py`:

| Subcommand | Touches the server | Purpose |
|---|---|---|
| `doctor [path]` | read | Checks deps, the `az` session, the `pac` profile, the dev guard, each solution (unmanaged) and the build output |
| `validate <file\|dir>` | no | Structural (JSON Schema) and semantic checks, plus a scan of the project's `.cs` files for the plug-in classes |
| `plan <file\|dir> [--json]` | read | Diff: `+ create`, `~ update` (`field: old → new`), `↑ push`, `- delete`, `! blocked`, `? unmanaged` |
| `apply <file\|dir> --plan-file <f>` | write | Runs exactly the saved plan. Aborts if the plan is stale. |
| `export --solution <s> --package <name>` (or `--assembly`) | read | Prints the YAML of existing registrations, to adopt a project registered by hand |

- **A PostToolUse hook** (Claude Code). It runs `validate` after every edit to a `*.dv.plugin.yaml` file. On GitHub Copilot, run `validate` explicitly.
- **A planner agent**, `dvac-plugins-planner`, that runs the whole loop with a user confirmation gate.

Run the script from the plugin's skill folder: `python3 <this-skill>/scripts/dv_plugin.py …`. Install its dependencies first, inside a venv, with `pip install -r <this-skill>/scripts/requirements.txt`.

## Workflow: plan → confirm → apply

Delegate registration changes to the **`dvac-plugins-planner`** agent. It runs in two invocations:

1. **Planning run.** The agent:
   - builds the project if needed
   - edits the YAML and validates it
   - runs `plan`
   - returns the target, the change list and the items that need double confirmation
2. **The main thread shows that plan to the user.** Ask for explicit confirmation of the environment and of the exact change list. Ask **twice** for each of these items:
   - `+ create package` or `+ create assembly` — the name is permanent
   - each `- delete` of a step or an image — the logic stops running
   - `+ create customapi` — binding, function/action and allowed steps are permanent
3. **Apply run.** Re-invoke the agent with `approved: <plan file>`. It runs `apply`, then checks that the follow-up plan is empty.

Never run `apply` without a plan the user has just seen. Never apply a plan that contains blocked items.

## Model file

```yaml
version: "1.0"
publisher: {prefix: prfx, solution: prfx_crm_Core}
package:
  name: prfx_Contoso.Crm.Plugins    # publisher prefix + NuGet package id — permanent
  nupkg: bin/Release/*.nupkg
  id: 3f2a…                         # written by apply on the first upload — commit it, never edit it
steps:
  - plugin: Contoso.Crm.Plugins.OrderStatusChanged
    message: Update
    table: prfx_crm_order
    stage: post-operation
    mode: async
    filtering_attributes: [prfx_statuscode]
    images:
      - {name: PreImage, type: pre, attributes: [prfx_statuscode]}
custom_apis:
  prfx_crm_RecalculateOrder: {plugin: Contoso.Crm.Plugins.RecalculateOrderApi, display_name: Recalculate order, binding: entity, bound_table: prfx_crm_order}
```

- **A step's name is its identity.** It defaults to `<plugin>: <message> of <table>`, the format the Plugin Registration Tool uses. Renaming a step is a delete plus a create.
- **The plan deletes steps the model doesn't list.** A step of the package that isn't in the YAML is planned as `- delete`. To adopt an existing project, run `export` first so that every live step is in the model.
- **`id` is the only field the tool writes.** `apply` records it after the first upload, and you commit it with the model.
- **Optional attributes you leave out are not managed.** This covers `configuration`, `description` and `async_auto_delete`. The others have a managed default: `mode: sync`, `rank: 1`, `enabled: true`, an empty filter, and an image alias equal to its name.
- Every key is listed in [`references/format.md`](references/format.md). Annotated samples are in [`references/samples/`](references/samples/).

## Deployment lifecycle

1. **First apply.** The tool uploads the built `.nupkg` as base64 to `pluginpackages` (or the `.dll` to `pluginassemblies`) and records the returned `id` in the YAML.
2. **Every later build.** `plan` compares the SHA-256 of the build output with the server content. When they differ, `apply` runs `pac plugin push --pluginId <id> --environment <.env.dev URL>`.
   - The tool always passes the dev URL to `pac`, whatever environment the active pac profile points to.
   - The package version is **not** a deployment signal. Dataverse does not use it to decide an update, and a package's version cannot be changed after creation.
3. **Solution membership.** Every component lands in `publisher.solution`:
   - **On create**, the request carries the `MSCRM.SolutionUniqueName` header. This covers the package or assembly, steps, images, the Custom API, its parameters and its response properties.
   - **An existing component outside the solution** is planned as `~ update … solution: absent → <solution>` and added with `AddSolutionComponent`. A step registered through the Plugin Registration Tool is a typical case.

The phase order and the exact payloads are in [`references/plan-apply.md`](references/plan-apply.md).

## Safety model

- **Dev only, enforced by the tool.** The guard is the same as for metadata as code:
  - only `<repo-root>/.env.dev` is read, with `ENVIRONMENT_STAGE=dev`
  - its URL must differ from the ones in `.env.uat` and `.env.prod`
  - every target solution must exist and be unmanaged

  There is no flag to override it. UAT and prod receive the registrations through the managed solution (`ppbp-alm-pipelines`).
- **Two sessions.** The Web API uses the developer's `az login` token, and `pac plugin push` uses the active PAC CLI profile. `doctor` shows both. Setup is in [`references/prerequisites.md`](references/prerequisites.md).
- **Limited deletes.** Only steps and step images are ever deleted, after double confirmation. Packages, assemblies, Custom APIs, request parameters and response properties are never deleted; they are reported as `? unmanaged`.

## Common issue: push rejected because a type is still in use

Dataverse rejects a package update that removes a class still used by a step or a Custom API. Remove those steps from the YAML in the same change: `apply` runs deletes before the push. See [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: blocked Custom API change

Some Custom API fields are fixed at creation. A change to one of them is `! blocked`:

- on the API: binding, bound table, function/action, allowed steps, workflow flag
- on a parameter: type, optional flag, table

Revert the YAML, or create a new Custom API and migrate its callers. The list is in [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: stale plan

`apply` recomputes the plan and aborts if it differs from the saved file. This happens when the model, the build output or the server changed after `plan`. Re-run `plan`, get the user's confirmation again, and apply the new file.

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Registering steps, images or Custom APIs in the Plugin Registration Tool or the maker portal | Describe them in the `.dv.plugin.yaml` and run plan → apply |
| Running `pac plugin push` by hand, or an ad-hoc registration script | `apply` pushes, with the dev URL passed explicitly |
| Writing a model for an existing project from memory | `export` the live registrations first — otherwise the steps you leave out are planned as deletes |
| Editing or removing the `id:` in the YAML | Leave it to the tool; a wrong id is blocked, a missing one is recorded again |
| Secrets in `configuration` | Unsecure configuration only; secrets go to a vault or environment variables, never the YAML |
| Update steps without `filtering_attributes`, or images without an explicit column list | The validator rejects both — list the columns |
| Bumping the package version to force a deployment | Rebuild; the tool compares content, not versions |
| Editing `dv_plugin.py` or the JSON Schema in a project | Edit only the model files; propose tooling changes upstream |

## Skill boundaries

- Plug-in domain orientation and global conventions → `ppbp-dv-plugins-overview`
- Dataverse schema as code → `dvac-metadata`
- Running a registration change end to end → agent `dvac-plugins-planner`
- Project scaffolding, early-bound classes, versioning and release build → `ppbp-dv-plugin-build`
- Step implementation (stages, images, context) → `ppbp-dv-step-plugin`
- Custom API design and its plug-in class → `ppbp-dv-custom-api`
- PAC CLI authentication, connection diagnostics → `dataverse:dv-connect`
- Solution creation, export and promotion → `dataverse:dv-solution`, `ppbp-alm-pipelines`
