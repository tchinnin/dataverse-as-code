# `.dv.plugin.yaml` format reference

[`schemas/plugin.schema.json`](schemas/plugin.schema.json) is authoritative, and this page explains it. Annotated examples are in [`samples/`](samples/):

- a package with steps, images and a Custom API
- an assembly

Start every file with the `yaml-language-server` directive, so VS Code validates it as you type:

```yaml
# yaml-language-server: $schema=<relative path to the skill>/references/schemas/plugin.schema.json
```

## File

- **One file per plug-in project**, next to its `.csproj`: `plugins/<PluginName>/<PluginName>.dv.plugin.yaml`.
- **`dv_plugin.py <command> plugins/`** reads every `*.dv.plugin.yaml` below the folder, skipping `bin/` and `obj/`.
- **The `package.name` / `assembly.name` must be unique** across files.
- **Names are compared case-insensitively.** The YAML keeps the case Dataverse displays.

## Top level

| Key | Required | Notes |
|---|---|---|
| `version` | yes | `"1.0"` |
| `publisher.prefix` | yes | Customization prefix of the solution publisher |
| `publisher.solution` | yes | Unique name of the **unmanaged** dev solution that receives every component |
| `package` / `assembly` | exactly one | See below |
| `steps` | no | List of steps |
| `custom_apis` | no | Map keyed by Custom API unique name |

## `package`

| Key | Required | Notes |
|---|---|---|
| `name` | yes | Unique name of the package on the server: `{prefix}_{NuGet package id}`. The validator warns when it differs from the `.nuspec` id. **Permanent once created.** |
| `nupkg` | yes | Path or glob of the build output, relative to the file. With a glob, the highest `.nuspec` version wins. |
| `id` | — | Written by `apply` after the first upload. Commit it, never edit it. |

## `assembly`

| Key | Required | Notes |
|---|---|---|
| `name` | yes | Assembly name, without `.dll`. **Permanent once created.** |
| `dll` | yes | Path or glob of the built `.dll`, relative to the file |
| `id` | — | Same as for a package |

An assembly is registered in sandbox isolation, stored in the database. The tool creates its plug-in types for every class a step or Custom API references. A package doesn't need this: the server creates its types on upload.

## Step

| Key | Default | Managed after creation |
|---|---|---|
| `name` | `<plugin>: <message> of <table>` (`<plugin>: <message>` without a table) | Identity — renaming is a delete plus a create. Several steps of one class on the same message and table need explicit, distinct names. |
| `plugin` | required | yes — full type name `Namespace.ClassName` |
| `message` | required | yes — SDK message, or the unique name of a Custom API |
| `table` | none | yes — logical name; omit for global messages |
| `stage` | required | yes — `pre-validation`, `pre-operation`, `post-operation`. Main operation (30) is refused: Dataverse registers that step for a Custom API — set `custom_apis.<name>.plugin` |
| `mode` | `sync` | yes — `async` requires `post-operation` |
| `rank` | `1` | yes — execution order among the steps on the same message, table and stage ("Execution Order" in the Plugin Registration Tool); lower runs first |
| `filtering_attributes` | none (fires on every column) | yes — `Update` only; compared as a set |
| `configuration` | not managed | when declared — unsecure configuration, never secrets |
| `description` | not managed | when declared |
| `async_auto_delete` | not managed | when declared — async steps only |
| `enabled` | `true` | yes — `false` disables the step (statecode) without deleting it |
| `images` | none | yes — images missing from the list are deleted |

Secure configuration is not part of the model. A step's secure configuration is left untouched.

**`export` and step names.** `export` writes `name` only when the server name differs from the default `<plugin>: <message> of <table>`. It writes `async_auto_delete` only when it is `true` on an async step. Because the name is the identity, a hand-written YAML whose step names don't match the server plans a `- delete` plus a `+ create` for each step. To adopt existing steps, start from `export`, or copy the exact server names.

## Image

| Key | Default | Notes |
|---|---|---|
| `name` | required | Identity within the step |
| `alias` | `name` | Key of the image in the execution context |
| `type` | required | `pre`, `post`, `both` |
| `attributes` | all columns | Columns captured in the image; omitted or empty captures every column |
| `message_property` | derived | `Create` → `Id` · `Update`, `Delete`, `Assign`, `Merge`, `Route` → `Target` · `SetState`, `SetStateDynamicEntity` → `EntityMoniker` · `Send`, `DeliverIncoming`, `DeliverPromote` → `EmailId`. Required for any other message. |

The validator rejects these:

- a pre-image on `Create`
- a post-image on `Delete`
- a post-image outside `post-operation`

## Custom API (`custom_apis.<unique name>`)

The unique name must start with `{prefix}_`.

| Key | Default | After creation |
|---|---|---|
| `plugin` | none | updatable — full type name of the main operation |
| `display_name` | required | updatable |
| `name` | unique name | updatable when declared |
| `description` | display name | updatable when declared |
| `binding` | required | **immutable** — `global`, `entity`, `entity-collection` |
| `bound_table` | none | **immutable** — required for `entity` and `entity-collection` |
| `is_function` | `false` | **immutable** |
| `allowed_steps` | `none` | **immutable** — `none`, `async-only`, `sync-and-async` |
| `workflow_enabled` | `false` | **immutable** |
| `is_private` | `false` | updatable |
| `execute_privilege` | none | updatable when declared |
| `request` | none | map of request parameters |
| `response` | none | map of response properties |

Steps registered on the Custom API's message must respect `allowed_steps`.

### Request parameter / response property

| Key | Default | After creation |
|---|---|---|
| `type` | required | **immutable** — `boolean`, `datetime`, `decimal`, `entity`, `entity-collection`, `entity-reference`, `float`, `integer`, `money`, `picklist`, `string`, `string-array`, `guid` |
| `optional` (request only) | `false` | **immutable** |
| `table` | none | **immutable** — only for the three entity types |
| `name` | `<api unique name>.<parameter unique name>` | updatable when declared |
| `display_name` | unique name | updatable when declared |
| `description` | display name | updatable when declared |

A bound Custom API gets its `Target` parameter from Dataverse: don't declare it.

## Source scan

When `.cs` files sit next to the YAML (`bin/` and `obj/` excluded), the validator maps every `namespace` + `class` and its base types:

- An error when a referenced `plugin` is not a class of the project.
- A warning when the class doesn't reach `PluginBase` or `IPlugin` through its base types.

This is a best-effort text scan, because Python can't reflect over a .NET assembly.
