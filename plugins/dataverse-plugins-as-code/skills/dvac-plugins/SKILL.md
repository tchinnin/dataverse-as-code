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
allowed-tools: Read, Write, Edit, Bash, Grep, WebSearch
argument-hint: "<registration change request>"
metadata:
  author: dataverse-as-code
  version: "3.0.1"
---

## Run

Every step is one command; your only job is the YAML. `DV` means `python3 <base directory>/scripts/dv_plugin.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

1. Edit `<project>/<PluginName>.dv.plugin.yaml` next to the plug-in's `.csproj` ([format](references/format.md)). When the plug-in code changed, build it with the project's build command first.
2. Run `DV plan <project folder>`, adding `--env-url <url> --stage dev [--tenant <id>]` when the user gave the environment.
3. Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `changes-pending` | Ask the user to confirm the change list — twice for each `DOUBLE CONFIRMATION` line. Then run the `NEXT` line exactly as printed. |
| `no-changes` | Done. |
| `blocked` | Fix the YAML ([blocked changes](references/plan-apply.md)), back to step 2. |
| `error` | Do what `FIX` says (often: ask the user something). Don't investigate further. |

4. After `apply`: `applied` → done, remind the user to commit the `.dv.plugin.yaml` (including the `id` the tool recorded). `stale-plan` → back to step 2. `error` → show `REASON` and `FIX`, stop.

To adopt a project registered by hand, run `DV export --solution <s> --package <name> --out <project folder>/<PluginName>.dv.plugin.yaml` first — never write existing registrations from memory.

Not in this loop: `validate` (plan validates; in Claude Code the hook also validates on save), `doctor` (only to diagnose an `error`), `--json`, and any other read of Dataverse or ad-hoc script — the plan is the only read of the environment.

**Large changes.** Delegate the YAML to the `dvac-plugins-planner` agent. Its prompt must hold `DV=<the absolute command>`, the request and the project conventions to apply. It returns the plan output; you run steps 3–4.

## Official skill

No official Microsoft skill covers plug-in registration. Two parts of the official `dataverse` plugin are relevant, load them only when you need that know-how:

- `dataverse:dv-overview` states the rule this tool implements: the first registration goes through the Web API, later updates through `pac plugin push`.
- `dataverse:dv-connect` owns PAC CLI authentication and the privileges registration needs.

If the `dataverse` plugin is not installed, **strongly recommend the user install it** before proceeding.

## Best practices

This skill defines the registration format and the tooling, **not** how to design or build plug-ins. Project structure, base classes, step and image design, Custom API design, versioning and build are the project's best practices. The validator only enforces Dataverse platform rules (publisher prefix, stages, images, Custom API constraints) and checks that the registered classes exist.

Apply the conventions already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, follow the user's request and the format rules; best-practice skills such as the `powerplatform-bestpractices` marketplace (`ppbp-*`) can be added.

## Connection

- **The tool needs the dev environment URL and its declared stage (`dev`)**, plus the tenant when the account spans several. It takes them from one source: `--env-url/--stage/--tenant` flags, else the `DATAVERSE_URL` / `ENVIRONMENT_STAGE` / `TENANT_ID` environment variables, else a `<repo-root>/.env.dev` file. Details in [`references/prerequisites.md`](references/prerequisites.md#3-dev-environment-inputs).
- If none is available, ask the user for the URL and tenant. Suggest a committed `.env.dev` as a convenient option (for example created by `ppbp-init`), never require it.
- **The Web API uses the Azure CLI token** (`az login`, with `--tenant <TENANT_ID>` when the account spans several tenants).
- **`pac plugin push` uses the active PAC CLI profile.** The tool always passes it the dev URL explicitly.

## Repository layout

```
<repo-root>/
├── .env.dev                              # optional — one way to describe the dev environment
├── .dv-plugin/                           # plan files (gitignored)
└── …/<PluginName>/                       # the plug-in project, anywhere in the repo
        ├── <PluginName>.csproj
        └── <PluginName>.dv.plugin.yaml   # REGISTRATIONS AS CODE — package, steps, images, Custom APIs
```

- **`<PluginName>.dv.plugin.yaml` is the source of truth for what is registered.** Commit it with the code, including the `id` the tool records.
- **The only location rule: the `.dv.plugin.yaml` sits in the same folder as the plug-in's `.csproj`.** Where plug-in projects live is the project's choice; the tool takes any file or folder and only needs the `.csproj` folder and its build output.

## What this skill adds

A declarative model (`<PluginName>.dv.plugin.yaml` next to each plug-in project), one tool (`scripts/dv_plugin.py`; every subcommand is in [`references/plan-apply.md`](references/plan-apply.md#subcommands)), a validation hook for Claude Code, and an optional planner agent for large YAML changes.

## Workflow: plan → confirm → apply

The loop is the one in [Run](#run). The user confirms the target (environment, `az` account, solution) and the exact change list, and **twice** each `DOUBLE CONFIRMATION` line:

- `create package` or `create assembly` — the name is permanent
- each `delete` of a step or an image — the logic stops running
- `create customapi` — binding, function/action and allowed steps are permanent

Never run `apply` without a plan the user has just seen, and never edit the `NEXT` line.

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
- **The plan deletes steps the model doesn't list.** A step of the package that isn't in the YAML is planned as `- delete`. To adopt an existing project, run `export` first (or `dvac-plugins-import` from an unpacked solution) so that every live step is in the model.
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

- Large registration changes → agent `dvac-plugins-planner` (optional; apply stays in the main thread)
- Documentation of the execution pipeline and the Custom APIs → `dvac-plugins-docs`
- Registrations from an unpacked solution (adoption, realignment) → `dvac-plugins-import`
- Dataverse schema as code → `dvac-metadata`
- Project scaffolding, plug-in code, step and Custom API design, versioning and build → the project's best practices
- PAC CLI authentication, connection diagnostics → `dataverse:dv-connect`
- Solution creation, export → `dataverse:dv-solution`
- Promotion to UAT/prod → the project's ALM pipeline

## Migration from 2.x

- `plan` exits 0 once the plan is computed and ends with a `STATUS:` footer. CI that relied on exit code 2 must pass `--detailed-exitcode`.
- The planner agent no longer applies (`approved: <plan file>` is gone): the main thread runs the printed `NEXT` command.
- The tool installs its Python dependencies in a cached venv on its first run.
