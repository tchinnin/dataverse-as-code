---
name: dvac-plugins-import
description: >
  Regenerates or aligns the plug-in registration files (*.dv.plugin.yaml) from one or more
  solutions unpacked in the repo (pac solution unpack layout): plug-in packages and
  assemblies, steps, step images and Custom APIs, offline, with a diff before any write
  and without ever removing a step or Custom API that is only in a file. Use when the user
  wants to adopt existing plug-in registrations as code, reverse-engineer the YAML from an
  exported solution, or bring the files back in line with what was registered on the
  server and exported.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
argument-hint: "<unpacked solution folder>... [--write]"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

`dataverse:dv-solution` owns exporting and unpacking solutions (`pac solution export`, `pac solution unpack`); the official `dataverse` skills own how plug-ins, steps and Custom APIs work. Check that the official `dataverse` plugin is installed; if it is not, **strongly recommend the user install it**. This skill only maps an unpacked solution onto the `dvac-plugins` format.

## Best practices

This skill is convention-neutral: it writes the step names, ranks and settings the solution holds, as they are. Where unpacked solutions and plug-in projects live is the project's best practice: look for it (README, `AGENTS.md`, installed skills) and follow it. If there is none, ask the user for the solution folder, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads the unpacked solution and the plug-in files only. To read registrations straight from the dev environment instead, `dv_plugin.py export` (skill `dvac-plugins`) does it.

## Repository layout

```
<repo-root>/
├── <unpacked solution>/                      # input, read only — any folder holding Other/Solution.xml
│   ├── pluginpackages/<name>/pluginpackage.xml
│   ├── PluginAssemblies/<name>-<id>/<name>.dll.data.xml
│   ├── SdkMessageProcessingSteps/{<id>}.xml
│   └── customapis/<name>/customapi.xml (+ request parameters, response properties)
└── plugins/
    └── <PluginName>/<PluginName>.dv.plugin.yaml   # output — existing file, or created here
```

An existing file is found wherever it is below `--root` (default: the repo root). A new file goes to `plugins/<Project>/<Project>.dv.plugin.yaml`, where `<Project>` is the package name without the publisher prefix, or the assembly name.

## What this skill adds

One tool, `scripts/dv_plugin_import.py`:

```sh
python3 <this-skill>/scripts/dv_plugin_import.py solution <unpacked-dir>... [--root <dir>] [--write] [--no-diff]
```

- **Without `--write` nothing is written.** It prints `+` new file, `~` changed file with its diff, `=` unchanged, then what is **only in the files** (kept) and what could **not be imported** (skipped). Exit code `2` means changes are pending.
- **`--write`** writes the new and changed files, after validating them with the `dvac-plugins` validator. An invalid result is refused and nothing is written.
- It loads and validates with the `dvac-plugins` tool of the same plugin: install that skill's requirements (`pip install -r <plugin>/skills/dvac-plugins/scripts/requirements.txt`, inside a venv).

How each XML element maps to the format, and how plug-in classes are found, are in [`references/solution-mapping.md`](references/solution-mapping.md).

## Workflow: import → review → write → plan

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run the tool **without** `--write` and show the user the file list, the diffs, the kept entries and the skipped components.
3. After the user confirms, run it again with `--write`. Fill in what the tool reports as unknown (typically `custom_apis.<name>.plugin`).
4. Build the project, then `validate` and `plan` with `dvac-plugins` (agent `dvac-plugins-planner`). After an import from the same dev environment the plan should hold no step change; anything left is drift to discuss with the user.

## Safety model

- **Offline and local.** The tool never contacts Dataverse. It writes only `*.dv.plugin.yaml` files, and only with `--write`.
- **Never removes a step.** `apply` deletes the server steps a file leaves out, so the import never drops a step or Custom API from a file: entries only in a file are kept and listed. Removing them is a deliberate act, followed by a plan the user confirms.
- **Keeps what the solution does not carry**: the package or assembly `id`, the `nupkg`/`dll` build path, `enabled`, and a Custom API's `plugin` when the solution knows it by export key only. No GUID is written from the solution.
- **Minimal rewrites.** A file whose data does not change is not rewritten, so its comments survive. Values a file declares explicitly (`rank: 1`, `mode: sync`…) and its key order are kept.

## Common issue: plug-in class unknown

A solution names the types of a plug-in **package** by an export key, not by class. The tool takes the class from the step name when it follows the Plugin Registration Tool format (`<class>: <message> of <table>`), from another step of the same export key, or from the existing file. A step with a custom name and no other source is skipped; a Custom API is imported without `plugin`. Describe them once by hand, and later imports keep them. See [`references/solution-mapping.md`](references/solution-mapping.md#plug-in-classes).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Running `--write` without showing the diff to the user | Run without `--write`, review, then write |
| Deleting a step from a file because the import lists it as kept | Check the server first; a removed step is deleted by the next `apply` |
| Running `apply` right after an import without a plan | Build, `validate`, then `plan` |
| Guessing a Custom API's plug-in class | Read it from the project or the Plugin Registration Tool, then set it |
| Importing a managed solution exported from UAT or prod | Import the unmanaged solution of the dev environment |

## Skill boundaries

- Plan and apply registrations → `dvac-plugins` / agent `dvac-plugins-planner`
- Registrations read from the dev environment → `dv_plugin.py export` (skill `dvac-plugins`)
- Tables, columns and Choices from the same solution → `dvac-metadata-import` (plugin `dataverse-metadata-as-code`)
- Exporting and unpacking solutions → `dataverse:dv-solution`
- Step and Custom API design reviews → the project's best practices
