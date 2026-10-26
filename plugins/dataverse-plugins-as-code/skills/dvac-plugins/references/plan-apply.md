# Plan / apply semantics

`dv_plugin.py` works like Terraform. `plan` compares the model with the dev environment and saves the result in `<repo-root>/.dv-plugin/plan.json`, which is gitignored. `apply` runs exactly that result.

## Plan output

```
Dataverse plug-in plan
  env      : https://contoso-dev.crm4.dynamics.com (/repo/.env.dev — or command-line flags / environment variables)
  az user  : dev@contoso.com (tenant 0000…)
  package  : prfx_Contoso.Crm.Plugins → prfx_crm_Core (unmanaged)  plugins/Contoso.Crm.Plugins/Contoso.Crm.Plugins.dv.plugin.yaml
             plugins/Contoso.Crm.Plugins/bin/Release/Contoso.Crm.Plugins.1.0.3.nupkg  sha256 1f0c9a7e5b21

  - delete  step       Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order  Create prfx_crm_order pre-operation sync
  ↑ push    package    prfx_Contoso.Crm.Plugins
              content: "sha256:8be1c2d3e4f5" → "sha256:1f0c9a7e5b21"
  ~ update  step       Contoso.Crm.Plugins.OrderStatusChanged: Update of prfx_crm_order
              filtering_attributes: ["prfx_statuscode"] → ["prfx_amount", "prfx_statuscode"]
  + create  image      Contoso.Crm.Plugins.OrderStatusChanged: Update of prfx_crm_order / PostImage  post (prfx_amount)
  ~ update  customapi  prfx_crm_RecalculateOrder
              solution: "absent" → "prfx_crm_Core"

Needs double confirmation:
  !! delete step Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order

Summary: 1 to create, 2 to update, 1 to push, 1 to delete, 0 blocked, 0 unmanaged.
```

`--json` prints the same plan as JSON with a `plan_file` key. It also includes `double_confirm`, the list of items to confirm twice, and `notes`.

## Action kinds

| Kind | `+ create` | `~ update` | Other |
|---|---|---|---|
| package | `POST pluginpackages` with the `.nupkg` as base64; the `id` is recorded in the YAML | `id` recorded in the YAML when missing | `↑ push` when the content differs: `pac plugin push --pluginId <id> --pluginFile <nupkg> --type Nuget --environment <dev url>` |
| assembly | `POST pluginassemblies` (base64, sandbox, database) | same as package | `↑ push` with `--type Assembly` |
| plugintype | assembly only — one per referenced class missing on the server | — | — |
| customapi | deep insert with its request parameters and response properties | `display_name`, `name`, `description`, `is_private`, `execute_privilege`, `plugin` | never deleted |
| request / response | new parameter or property on an existing API | `display_name`, `name`, `description` | never deleted |
| step | new step | `plugin`, `message`, `table`, `stage`, `mode`, `rank`, `filtering_attributes`, `enabled`, and `configuration`, `description`, `async_auto_delete` when declared | `- delete` when absent from the model |
| image | new image | `alias`, `type`, `attributes`, `message_property` | `- delete` when absent from the model |

Every kind can also have an `~ update … solution: absent → <solution>` action (see below).

## Apply order

`apply` stops at the **first failure**. Because the plan is a diff, the next `plan` holds only what is left to do.

1. `- delete` images, then `- delete` steps. Dataverse rejects a package update that removes a class a step still uses, so deletes come first.
2. Package or assembly: `+ create`, `~ update` (id), `↑ push`. After an upload or a push, the tool:
   - reads the package's assemblies and plug-in types again
   - for an assembly unit, adds the assembly to the solution if the server created it outside it. A package's assemblies are never added: the package carries them.
3. `+ create plugintype` (assembly only)
4. Custom APIs, then their request parameters and response properties
5. Steps: the message and filter are resolved from `sdkmessages` / `sdkmessagefilters`, and the handler is bound with `eventhandler_plugintype`. A step with `enabled: false` is disabled right after creation.
6. Images
7. Solution membership, parents before children:
   - package unit: package → Custom APIs → parameters → properties → steps → images
   - assembly unit: assembly → Custom APIs → parameters → properties → steps → images
8. Post-apply check: `plan` runs again with `Consistency: Strong` and must be empty. Because the read bypasses the cache, a remaining difference is real: investigate it rather than retrying.

Updates use `PATCH` with `If-Match: *`, so an update never creates a record by accident. Transient errors are retried as in `dv_metadata.py`: lock contention, the metadata cache, and HTTP 429. Set `DV_PLUGIN_DEBUG=1` to print every Web API call with its duration.

## Solution membership

- **On create**, every `POST` of a solution component sends `MSCRM.SolutionUniqueName: <publisher.solution>`. This is the documented header name. Dataverse silently ignores an unknown header, and the component then lands outside the solution.
- **The server creates some components itself.** These are the parameters of a deep-inserted Custom API, and the assembly of a standalone assembly unit. The tool checks them right after the create and adds the missing ones.
- **A package's assemblies are never solution components.** Only the `pluginpackage` is one, and it carries its assemblies and plug-in types. Dataverse refuses `AddSolutionComponent` on such an assembly (`0x8004023b`, "part of a Plugin Package"), so `plan` never proposes it. If `apply` still gets that error, it shows a note and continues.
- **For existing components**, `plan` reads `solutioncomponents` of the solution. A component outside it gets `~ update <kind> <name>  solution: "absent" → "<solution>"`, which `apply` runs as `AddSolutionComponent` with:
  - `AddRequiredComponents: false`
  - `DoNotIncludeSubcomponents: false`. Dataverse accepts `true` only for a table (type 1); for any other type it fails with `0x80040216` ("DoNotIncludeSubcomponents can not be set to true on non Entity root"). Each child is still checked and added explicitly.
  - `0x80040216` is a validation error here, so `apply` fails at once instead of retrying it as a metadata cache error.
- **Component type codes.** Assembly 91 and step 92 are fixed. The codes for `pluginpackage`, `customapi`, `customapirequestparameter` and `customapiresponseproperty` are read from `solutioncomponentdefinitions`. When an environment doesn't know one of them, `plan` shows a note and skips that membership check.
- **Plug-in types and step images are not solution components.** They travel with their assembly and their step: the tool creates them without the header and never adds them to the solution.

## Change detection for the package

`plan` compares the SHA-256 of the local build output with the server content:

1. It reads the `content` column first.
2. For a package whose content lives in file storage, it falls back to downloading the `package` file column.
3. When neither can be read, the plan notes it:
   - for a package, it compares the `.nuspec` version with the package version
   - for an assembly, content changes go undetected

Dataverse does not use the version to decide an update, and the name and version of a package can't change after it's created. Never bump a version to force a deployment: rebuild instead.

## Blocked changes

A blocked change is never run. The plan exits with code 1, and `apply` refuses any plan that contains one.

| Change | Why | What to do |
|---|---|---|
| YAML `id` ≠ the server id of the same name | The YAML points to another package/assembly | Fix the id (the name is the identity) |
| YAML `id` set but no package/assembly of that name | It was deleted, or the name changed | Remove `id:` only to register a new one deliberately |
| A step or Custom API references a class missing from the package, and no push is planned | The class isn't in the uploaded code | Build the code that contains it; the next plan pushes it |
| Custom API `binding`, `bound_table`, `is_function`, `allowed_steps`, `workflow_enabled` | Fixed at creation | Revert the YAML, or create a new Custom API and migrate callers |
| Parameter `type`, `optional`, `table` | Fixed at creation | Revert the YAML, or add a new parameter |
| Several steps of the package (or several images of one step) share a name on the server | The name is the identity, so the tool can't tell them apart. The Plugin Registration Tool gives the same default name to steps that differ only by stage or mode. | Rename all but one on the server — a one-off adoption fix — then `export` and `plan` again |

## Deletes and unmanaged

- **Deletes.** Steps of the package/assembly that are absent from the model are deleted, and so are images absent from a modeled step. Deleting a step also removes its images. Every delete needs double confirmation.
- **Unmanaged** (reported only, never deleted):
  - other Custom APIs implemented by the package's classes
  - request parameters and response properties of a modeled Custom API that are absent from the model

  Removing one of these breaks callers, so the decision stays a human one.
- **Adopting an existing project.** Run `export` first. Otherwise every live step is planned as a delete.

## Stale plan

`apply` recomputes the plan and compares it with the saved file. The comparison covers the environment, the units (with the SHA-256 of the build output), the actions and the blocked items; any difference aborts the run. Rebuilding after `plan` therefore makes the plan stale. Re-run `plan`, have the user confirm again, and apply the new file.

## Exit codes

| Code | `plan` | `apply` / `validate` / `doctor` / `export` |
|---|---|---|
| 0 | no changes | success |
| 1 | error, guard refusal, or blocked changes | failure / refusal |
| 2 | changes pending | — |

## Common errors during apply

| Symptom | Likely cause | Fix |
|---|---|---|
| `pac plugin push failed` … types used by steps | The new code removes a class still used by a step or Custom API | Remove those steps from the YAML in the same change (deletes run before the push) |
| `pac plugin push failed` … authentication | The pac profile has no access to the dev environment | Authenticate the PAC CLI (`dataverse:dv-connect`), then re-plan |
| `pac plugin push failed` after a major or minor version change of an assembly | Dataverse treats it as a different assembly | Go back to the registered major.minor; bump build or revision only |
| `plugin type '…' not found … after upload` | Namespace or class name wrong, or the class doesn't implement `IPlugin` | Fix the YAML or the code, rebuild, re-plan |
| `message '…' is not available on table '…'` | The message doesn't support that table | Check the message/table pair |
| Package create fails on the name | The name doesn't carry the solution publisher's prefix | Use `{prefix}_{NuGet id}` |
| `REFUSED: … MANAGED` | The target is not the dev environment | Check the dev environment inputs (flags, environment variables or `.env.dev`); never change a managed solution |
| `az … failed` | No `az` session, or the wrong tenant | `az login --tenant <TENANT_ID>` |

## To verify on the first real run

These behaviors are documented by Microsoft only partly. Check them on the first run against a dev environment, then update this page:

- **Package content.** Is it readable through `content`, or only through the `package` file column?
- **Assembly create.** Does `POST pluginassemblies` with `name`, `content`, `isolationmode` and `sourcetype` fill version, culture and public key token from the content?
- **Package naming.** Does a package's unique name have to carry the publisher prefix?
- **Changing a step's message or table.** Can an existing step's `sdkmessageid` or `sdkmessagefilterid` be changed? If not, report the change as blocked.
- **Solution header on implicit children.** Does `MSCRM.SolutionUniqueName` cover the server-created assemblies and deep-inserted parameters? Either way, the explicit check covers them.
- **Push and membership.** Does `pac plugin push` keep the package's solution membership?
