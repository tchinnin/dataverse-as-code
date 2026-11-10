# Solution → plug-in file mapping

`dv_plugin_solution.py` reads the SolutionPackager XML layout produced by `pac solution unpack` — read only, it never writes in the unpacked folder. This page lists what it reads and how each element becomes a `.dv.plugin.yaml` field, for `import` and `compare` alike. The format itself is described in the `dvac-plugins` skill (`references/format.md`).

## Files read

| File | Content used |
|---|---|
| `Other/Solution.xml` | `UniqueName`, `Publisher/CustomizationPrefix` — the `publisher` block of a new file |
| `pluginpackages/<name>/pluginpackage.xml` | A plug-in package (`uniquename`) |
| `PluginAssemblies/**/<name>.dll.data.xml` | A plug-in assembly (`FullName`) and its types (`PluginType Name`, `PluginTypeId`) |
| `SdkMessageProcessingSteps/{id}.xml` | Steps and their images |
| `customapis/<name>/customapi.xml` | Custom APIs |
| `customapis/<name>/customapirequestparameters/*/…xml`, `customapiresponseproperties/*/…xml` | Request parameters and response properties |

Each package and assembly becomes one file. Nothing else in the solution is read.

## Steps

| XML | Field |
|---|---|
| `Name` | `name` — left out when it equals `<plugin>: <message> of <table>` |
| class (see below) | `plugin` |
| message (see below) | `message` |
| `PrimaryEntity` | `table` (`none` → no table) |
| `Stage` (10, 20, 40) | `stage` |
| `Mode` (0, 1) | `mode` |
| `Rank` | `rank` — left out when 1 |
| `FilteringAttributes` | `filtering_attributes` |
| `Configuration` | `configuration` (unsecure configuration) |
| `Description` | `description` |
| `AsyncAutoDelete` | `async_auto_delete` (async steps) |
| `SdkMessageProcessingStepImage` | `images`: `Name`, `EntityAlias` → `alias`, `ImageType` (0 pre, 1 post, 2 both), `Attributes`, `MessagePropertyName` → `message_property` when it differs from the message's default |

The step state is not in the solution: `enabled` is kept from the existing file.

### Messages

The message comes from the step name (`<class>: <message> of <table>`), else from `SdkMessageId` for the platform messages whose id is the same in every environment (Create, Update, Delete), else from the existing file.

### Plug-in classes

- **Assembly**: the step names its type (`PluginTypeName`, or `PluginTypeId` resolved through the assembly's `PluginTypes`).
- **Package**: the step holds only `PluginTypeExportKey`, a key that cannot be turned back into a class name. The class comes from:
  1. the step name, when it follows `<class>: <message> of <table>`;
  2. another step with the same export key whose name gives the class;
  3. the step of the same name in the existing file.

  Otherwise the step is skipped and listed.

### Which file a step goes to

The assembly named by the step; else the only package or assembly of the solution(s); else, for an export key, the only package; else the file that already lists the class or holds the project's source for it. Otherwise the step is skipped and listed.

## Custom APIs

| XML | Field |
|---|---|
| `uniquename` | key |
| `name` | `name` — when it differs from the unique name |
| `displayname` | `display_name` |
| `description` | `description` |
| `bindingtype` (0, 1, 2) | `binding` (`global`, `entity`, `entity-collection`) |
| `boundentitylogicalname` | `bound_table` |
| `isfunction`, `isprivate`, `workflowsdkstepenabled` | `is_function`, `is_private`, `workflow_enabled` |
| `allowedcustomprocessingsteptype` (0, 1, 2) | `allowed_steps` (`none`, `async-only`, `sync-and-async`) |
| `executeprivilegename` | `execute_privilege` |
| `plugintypeid` | `plugin` — resolved like a step's class; else kept from the existing file; else left out and listed |

Request parameters and response properties: `uniquename` → key, `type` (0–12) → `type`, `logicalentityname` → `table`, `isoptional` → `optional`, `displayname` and `description` when they differ from the name.

## Kept from the existing file

- `id`, `nupkg` / `dll` of the package or assembly (a new file gets `id: null` and `bin/Release/*.nupkg` or `bin/Release/net48/<name>.dll`);
- `publisher`;
- steps and Custom APIs that are not in the solution (listed as kept);
- `enabled`, and fields declared with their default value;
- the order of steps, Custom APIs and keys; column lists that hold the same columns in another order.

## Merging

Several solutions are read together. A step or Custom API present in two solutions is an error.

## Compare

`compare` resolves the solution's steps and Custom APIs exactly as `import` does, then compares them with the files, normalised as `plan` normalises them:

- **Units** (package or assembly) by name, **steps** by name (the Plugin Registration Tool default when the file has none), **images** by name, **Custom APIs** and their parameters by unique name.
- **`~` differs** — step `plugin`, `message`, `table`, `stage`, `mode`, `rank`, `filtering_attributes` and the declared `configuration`, `description`, `async_auto_delete`; image `alias`, `type`, `attributes`, `message_property`; Custom API `binding`, `bound_table`, `is_function`, `allowed_steps`, `workflow_enabled`, `display_name`, `is_private` and the declared `name`, `description`, `execute_privilege`, `plugin`; parameter `type`, `table`, `optional` and the declared `name`, `display_name`, `description`. Defaults apply as in the format (`mode: sync`, `rank: 1`…).
- **`·` not declared** — a step's `description`, `configuration` or `async_auto_delete`, a Custom API's `description` or `execute_privilege`, held by the solution and left out of the file.
- **`!`** — what `import` would skip (class unknown, unit unknown); such a step is not reported as `-`.
- Not compared: the package or assembly `id`, the build output path, `enabled`, and a plug-in class the solution knows by export key only.
