---
name: dvac-plugins-solution
description: >
  Works between the plug-in registration files (*.dv.plugin.yaml) and solutions unpacked
  in the repo (pac solution unpack layout), offline and without ever modifying the
  unpacked XML: import regenerates or aligns the files from the solutions (plug-in
  packages and assemblies, steps, step images, Custom APIs) with a diff before any write
  and without removing a step or Custom API that is only in a file, and compare lists
  what differs between the solution and the files, field by field. Use when the user
  wants to adopt existing plug-in registrations as code, reverse-engineer the YAML from an
  exported solution, check what differs between an unpacked solution and the files, or
  bring the files back in line with an export.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep
argument-hint: "import|compare <unpacked solution folder>... [--write]"
metadata:
  author: dataverse-as-code
  version: "2.0.0"
---

## Run

Every step is one command. `DV` means `python3 <base directory>/scripts/dv_plugin_solution.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

- **Compare**: `DV compare <unpacked-dir>... [--root <dir>]`. It writes nothing.
- **Import**: `DV import <unpacked-dir>... [--root <dir>] [--projects-dir <dir>]`. It writes nothing without `--write`.

Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `in-sync` | Done: the files and the solution describe the same registrations. |
| `drift` | Ask the user which side is right. The files: build, plan and apply with `dvac-plugins`, then export and unpack again. The solution: run the `NEXT` line (an import without `--write`). |
| `changes-pending` | Ask the user to confirm the file list and diffs, then run the `NEXT` line exactly as printed. |
| `no-changes` | Done. |
| `written` | Fill in what the import reported as unknown, build, then run the `NEXT` line (`plan` with `dvac-plugins`). |
| `refused` | Do what `FIX` says: the write target is inside an unpacked solution. |
| `error` | Do what `FIX` says (often: ask the user something). Don't investigate further. |

## Official skill

`dataverse:dv-solution` owns exporting and unpacking solutions (`pac solution export`, `pac solution unpack`); the official `dataverse` skills own how plug-ins, steps and Custom APIs work. If the official `dataverse` plugin is not installed, **strongly recommend the user install it**. This skill only maps an unpacked solution onto the `dvac-plugins` format.

## Best practices

This skill is convention-neutral: it writes and compares the step names, ranks and settings the solution holds, as they are. Where unpacked solutions and plug-in projects live is the project's best practice: follow the one already in your context (the project's instructions, loaded skills) — don't search for it. If there is none, ask the user for the solution folder, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads the unpacked solution and the plug-in files only. To read registrations straight from the dev environment instead, `dv_plugin.py export` (skill `dvac-plugins`) does it.

## Repository layout

```
<repo-root>/
├── <unpacked solution>/                      # input, READ ONLY — any folder holding Other/Solution.xml
│   ├── pluginpackages/<name>/pluginpackage.xml
│   ├── PluginAssemblies/<name>-<id>/<name>.dll.data.xml
│   ├── SdkMessageProcessingSteps/{<id>}.xml
│   └── customapis/<name>/customapi.xml (+ request parameters, response properties)
└── <project folder>/                          # wherever the plug-in project lives
    ├── <PluginName>.csproj
    └── <PluginName>.dv.plugin.yaml            # import output — existing file, or created next to the .csproj
```

An existing file is found wherever it is below `--root` (default: the repo root), except inside an unpacked solution (such a file is ignored). A new file goes next to `<Project>.csproj` — the format's only location rule — found below `--root`; `<Project>` is the package name without the publisher prefix, or the assembly name. When no such `.csproj` exists (the project isn't in the repo yet), the import stops with `STATUS: error`; `--projects-dir <folder>` then creates the file in `<folder>/<Project>/`.

## What this skill adds

One tool, `scripts/dv_plugin_solution.py`, with two subcommands.

**`compare`** writes nothing. Fields are compared as `plan` compares them with the server. It prints five sections:

| Mark | Section | Counts as drift |
|---|---|---|
| `+` | only in the solution (package or assembly, step, image, Custom API, parameter) | yes |
| `-` | only in the files | yes |
| `~` | differs: `<where>.<field>: model=… solution=…` | yes |
| `·` | a value the solution holds that the file doesn't declare (a step's `description`, `configuration`…) | no |
| `!` | what cannot be compared (a step whose plug-in class is unknown…) | no |

The package or assembly `id`, the build output path and `enabled` are not compared: the solution does not carry them. It ends with `STATUS: in-sync` or `STATUS: drift` (+ `REASON`, `FIX`, `NEXT`) and exits 0 (`--detailed-exitcode`: 2 on drift).

**`import`** regenerates the files:

- **Without `--write` nothing is written.** It prints `+` new file, `~` changed file with its diff, `=` unchanged, then what is **only in the files** (kept) and what could **not be imported** (skipped). The output ends with `STATUS:` (`changes-pending`, `no-changes` or `error`) and, when files would change, the exact `NEXT` command with `--write`. It exits 0 (`--detailed-exitcode`: 2 when files would change).
- **`--write`** writes the new and changed files, after validating them with the `dvac-plugins` validator. An invalid result is refused and nothing is written.

Both load and validate with the `dvac-plugins` tool of the same plugin. How each XML element maps to the format, and how plug-in classes are found, are in [`references/solution-mapping.md`](references/solution-mapping.md).

## Workflow: compare → review

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run `compare` and show the user the sections.
3. On `drift`, the user decides which side is right — never pick one yourself. Files right: build, `plan` and `apply` with `dvac-plugins`, then export and unpack again. Solution right: the import workflow below.

## Workflow: import → review → write → plan

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run `import` **without** `--write` and show the user the file list, the diffs, the kept entries and the skipped components.
3. After the user confirms, run the `NEXT` line exactly as printed. Fill in what the tool reports as unknown (typically `custom_apis.<name>.plugin`).
4. Build the project, then `plan` with `dvac-plugins` (agent `dvac-plugins-planner`). After an import from the same dev environment the plan should hold no step change; anything left is drift to discuss with the user.

## Safety model

- **The unpacked solution is read only.** No subcommand writes, moves or reformats a file in it. A file the import would write inside an unpacked solution (through `--projects-dir` or a `.csproj` found there) is refused (`STATUS: refused`). A difference is fixed in the files, or in dev followed by a new export — never in the XML.
- **Offline and local.** The tool never contacts Dataverse. `compare` writes nothing; `import` writes only `*.dv.plugin.yaml` files, and only with `--write`.
- **Never removes a step.** `apply` deletes the server steps a file leaves out, so the import never drops a step or Custom API from a file: entries only in a file are kept and listed. Removing them is a deliberate act, followed by a plan the user confirms.
- **Keeps what the solution does not carry**: the package or assembly `id`, the `nupkg`/`dll` build path, `enabled`, and a Custom API's `plugin` when the solution knows it by export key only. No GUID is written from the solution.
- **Minimal rewrites.** A file whose data does not change is not rewritten, so its comments survive. Values a file declares explicitly (`rank: 1`, `mode: sync`…) and its key order are kept.

## Common issue: plug-in class unknown

A solution names the types of a plug-in **package** by an export key, not by class. The tool takes the class from the step name when it follows the Plugin Registration Tool format (`<class>: <message> of <table>`), from another step of the same export key, or from the existing file. A step with a custom name and no other source is skipped (`!` in `compare`); a Custom API is imported without `plugin`. Describe them once by hand, and later runs keep them. See [`references/solution-mapping.md`](references/solution-mapping.md#plug-in-classes).

## Migration from `dvac-plugins-import` (2.0.0)

The skill `dvac-plugins-import` is now `dvac-plugins-solution`, and `dv_plugin_import.py solution <dir>…` is now `dv_plugin_solution.py import <dir>…` (same options, same output). `compare` is new.

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing, reformatting or "fixing" the unpacked solution's XML | Fix the files, or change the registration in dev and export again |
| Choosing on your own which side of a `drift` is right | Show the sections and let the user decide |
| Running `--write` without showing the diff to the user | Run without `--write`, review, then write |
| Deleting a step from a file because the import lists it as kept | Check the server first; a removed step is deleted by the next `apply` |
| Running `apply` right after an import without a plan | Build, then `plan` |
| Guessing a Custom API's plug-in class | Read it from the project or the Plugin Registration Tool, then set it |
| Importing a managed solution exported from UAT or prod | Import the unmanaged solution of the dev environment |

## Skill boundaries

- Plan and apply registrations → `dvac-plugins` / agent `dvac-plugins-planner`
- Registrations read from the dev environment → `dv_plugin.py export` (skill `dvac-plugins`)
- Tables, columns and Choices from the same solution → `dvac-metadata-solution` (plugin `dataverse-metadata-as-code`)
- Security roles from the same solution → `dvac-security-solution` (plugin `dataverse-security-as-code`)
- Exporting and unpacking solutions → `dataverse:dv-solution`
- The YAML source control format (`solutions/*/solution.yml`) → not read; unpack in the XML layout
- Step and Custom API design reviews → the project's best practices
