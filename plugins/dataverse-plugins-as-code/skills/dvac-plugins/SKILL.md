---
name: dvac-plugins
description: >
  Dataverse plug-in registrations as code: plug-in packages or assemblies,
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
  version: "2.0.4"
---

## Official skill

No official Microsoft skill covers plug-in registration. Two parts of the official `dataverse` plugin are relevant, so load both before this skill:

- `dataverse:dv-overview` states the rule this tool implements: the first registration goes through the Web API, later updates through `pac plugin push`.
- `dataverse:dv-connect` owns PAC CLI authentication and the privileges registration needs.

If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding.

## Best practices

This skill defines the registration format and the tooling, **not** how to design or build plug-ins. Project structure, base classes, step and image design, Custom API design, versioning and build are the project's best practices. The validator only enforces Dataverse platform rules (publisher prefix, stages, images, Custom API constraints) and checks that the registered classes exist.

Before writing the model, look for the project's own best practices (README, `AGENTS.md`, installed skills) and follow them. If there are none, work from the user's request and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

- **The tool needs the dev environment URL and its declared stage (`dev`)**, plus the tenant when the account spans several. It takes them from one source: `--env-url/--stage/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else a `<repo-root>/.env.dev` file. Details in [`references/prerequisites.md`](references/prerequisites.md#3-dev-environment-inputs).
- If none is available, ask the user for the URL and tenant. Suggest a committed `.env.dev` as a convenient option (for example created by `ppbp-init`), never require it.
- **The Web API uses the Azure CLI token** (`az login`, with `--tenant <TENANT_ID>` when the account spans several tenants).
- **`pac plugin push` uses the active PAC CLI profile.** The tool always passes it the dev URL explicitly.
- Before any change, confirm the target URL, the `az` account and the pac profile with the user.

## Repository layout

```
<repo-root>/
├── .env.dev                              # optional — one way to describe the dev environment
├── .dv-plugin/                           # plan files (gitignored)
└── plugins/
    └── <PluginName>/                     # the plug-in project (any layout; the YAML sits next to the .csproj)
        ├── <PluginName>.csproj
        └── <PluginName>.dv.plugin.yaml   # REGISTRATIONS AS CODE — package, steps, images, Custom APIs
```

- **`<PluginName>.dv.plugin.yaml` is the source of truth for what is registered.** Commit it with the code, including the `id` the tool records.
- The tool only needs the `.csproj` folder and its build output; the rest of the project layout is up to the project.

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
2. **Every later build.** `plan` compares the SHA-256 of the build output with the server content. When they differ, `apply` runs `pac plugin push --pluginId <id> --environment <dev URL>`.
   - The tool always passes the dev URL to `pac`, whatever environment the active pac profile points to.
   - The package version is **not** a deployment signal. Dataverse does not use it to decide an update, and a package's version cannot be changed after creation.
3. **Solution membership.** Every component lands in `publisher.solution`:
   - **On create**, the request carries the `MSCRM.SolutionUniqueName` header. This covers the package or assembly, steps, the Custom API, its parameters and its response properties.
   - **An existing component outside the solution** is planned as `~ update … solution: absent → <solution>` and added with `AddSolutionComponent`. A step registered through the Plugin Registration Tool is a typical case.
   - **A package unit** adds the package, never its assemblies: Dataverse refuses them, and the package carries them. An assembly unit adds the assembly.
   - **Step images are not solution components.** They travel with their step and are never added to the solution.

The phase order and the exact payloads are in [`references/plan-apply.md`](references/plan-apply.md).

## Safety model

- **Dev only, enforced by the tool.** The guard is the same as for metadata as code. Whatever the source of the environment inputs:
  - the target must be declared as stage `dev`
  - its URL must differ from the ones in `.env.uat` and `.env.prod`, when those files exist
  - every target solution must exist and be unmanaged (checked on the server)

  There is no flag to override it. UAT and prod receive the registrations through the managed solution and the project's ALM pipeline.
- **Two sessions.** The Web API uses the developer's `az login` token, and `pac plugin push` uses the active PAC CLI profile. `doctor` shows both. Setup is in [`references/prerequisites.md`](references/prerequisites.md).
- **Limited deletes.** Only steps and step images are ever deleted, after double confirmation. Packages, assemblies, Custom APIs, request parameters and response properties are never deleted; they are reported as `? unmanaged`.

## Common issue: push rejected because a type is still in use

Dataverse rejects a package update that removes a class still used by a step or a Custom API. Remove those steps from the YAML in the same change: `apply` runs deletes before the push. See [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: blocked Custom API change

Some Custom API fields are fixed at creation. A change to one of them is `! blocked`:

- on the API: binding, bound table, function/action, allowed steps, workflow flag
- on a parameter: type, optional flag, table

Revert the YAML, or create a new Custom API and migrate its callers. The list is in [`references/plan-apply.md`](references/plan-apply.md).

## Common issue: Custom API implementation step

Dataverse registers a stage-30 step (`CustomApi '<name>' implementation`) for every Custom API that has a plug-in type. The tool ignores it: never declare it in `steps:` (`validate` refuses `main-operation`), and never delete it. See [`references/plan-apply.md`](references/plan-apply.md).

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
| Bumping the package version to force a deployment | Rebuild; the tool compares content, not versions |
| Editing `dv_plugin.py` or the JSON Schema in a project | Edit only the model files; propose tooling changes upstream |

## Skill boundaries

- Running a registration change end to end → agent `dvac-plugins-planner`
- Documentation of the execution pipeline and the Custom APIs → `dvac-plugins-docs`
- Dataverse schema as code → `dvac-metadata`
- Project scaffolding, plug-in code, step and Custom API design, versioning and build → the project's best practices
- PAC CLI authentication, connection diagnostics → `dataverse:dv-connect`
- Solution creation, export → `dataverse:dv-solution`
- Promotion to UAT/prod → the project's ALM pipeline
