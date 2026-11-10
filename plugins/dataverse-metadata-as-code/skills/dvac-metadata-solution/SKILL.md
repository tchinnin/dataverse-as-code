---
name: dvac-metadata-solution
description: >
  Works between the Dataverse metadata model (dataverse/metadata/*.dv.metadata.yaml) and
  solutions unpacked in the repo (pac solution unpack layout), offline and without ever
  modifying the unpacked XML: import regenerates or aligns the model from the solutions
  (global Choices, tables, columns, formula columns, lookups, alternate keys) with a diff
  before any write, and compare lists what differs between the solution and the model,
  field by field. Use when the user wants to adopt an existing solution as code,
  reverse-engineer the YAML from an exported solution, check what differs between an
  unpacked solution and the model, or bring the model back in line with an export.
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

Every step is one command. `DV` means `python3 <base directory>/scripts/dv_metadata_solution.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

- **Compare**: `DV compare <unpacked-dir>... [--model dataverse/metadata] [--tables a,b]`. It writes nothing.
- **Import**: `DV import <unpacked-dir>... [--model dataverse/metadata] [--tables a,b]`. It writes nothing without `--write`.

Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `in-sync` | Done: the model and the solution describe the same metadata. |
| `drift` | Ask the user which side is right. The model: plan and apply with `dvac-metadata`, then export and unpack again. The solution: run the `NEXT` line (an import without `--write`). |
| `changes-pending` | Ask the user to confirm the file list and diffs, then run the `NEXT` line exactly as printed. |
| `no-changes` | Done. |
| `written` | Run the `NEXT` line (`plan` with `dvac-metadata`). |
| `refused` | Do what `FIX` says: the write target is inside an unpacked solution. |
| `error` | Do what `FIX` says. Don't investigate further. |

## Official skill

`dataverse:dv-solution` owns exporting and unpacking solutions (`pac solution export`, `pac solution unpack`); `dataverse:dv-metadata` owns how metadata works. If the official `dataverse` plugin is not installed, **strongly recommend the user install it**. This skill only maps an unpacked solution onto the `dvac-metadata` model format.

## Best practices

This skill is convention-neutral: it writes and compares the names, labels and types the solution holds, as they are. Where unpacked solutions live and how often they are refreshed are the project's best practices: follow the ones already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, ask the user for the solution folder, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads the unpacked solution and the model files only. Getting a fresh export is the official skill's job (`dataverse:dv-solution`).

## Repository layout

```
<repo-root>/
├── <unpacked solution>/              # input, READ ONLY — any folder holding Other/Solution.xml
│   ├── Other/Solution.xml, Other/Customizations.xml, Other/Relationships/*.xml
│   ├── OptionSets/*.xml
│   └── Entities/<Table>/Entity.xml (+ Formulas/*.yaml)
└── dataverse/
    └── metadata/                     # import output — the dvac-metadata model
        ├── _meta.dv.metadata.yaml    # written only when missing
        ├── choices.dv.metadata.yaml
        └── tables/<key>.dv.metadata.yaml
```

## What this skill adds

One tool, `scripts/dv_metadata_solution.py`, with two subcommands.

**`compare`** writes nothing. The solution plays the server of a plan, so a difference means here what it means in `plan`. It prints five sections:

| Mark | Section | Counts as drift |
|---|---|---|
| `+` | only in the solution (table, column, key, Choice, option) | yes |
| `-` | only in the model | yes |
| `~` | differs: `<where>.<field>: model=… solution=…` | yes |
| `·` | a value the solution holds that the model doesn't declare (`max_length`, `min`…) — the format leaves it unmanaged | no |
| `!` | a component the format cannot describe | no |

It ends with `STATUS: in-sync` or `STATUS: drift` (+ `REASON`, `FIX`, `NEXT`), exits 0 (`--detailed-exitcode`: 2 on drift). It needs a valid model (`_meta` present).

**`import`** regenerates the model:

- **Without `--write` nothing is written.** It prints `+` new file, `~` changed file with its diff, `=` unchanged, then what is **only in the model** (kept) and what the format **cannot describe** (skipped). The output ends with `STATUS:` (`changes-pending`, `no-changes` or `error`) and, when files would change, the exact `NEXT` command with `--write`. It exits 0 (`--detailed-exitcode`: 2 when files would change).
- **`--write`** writes the new and changed files, after validating the whole model with the `dvac-metadata` validator. An invalid result is refused and nothing is written.

Both take several solutions: a table split across solutions gets the columns of each; a component described differently by two solutions is an error. `--tables` restricts both to those tables (and leaves the Choices out). They load and validate with the `dvac-metadata` tool of the same plugin.

How each XML element maps to the format, and every skip reason, are in [`references/solution-mapping.md`](references/solution-mapping.md).

## Workflow: compare → review

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run `compare` and show the user the sections.
3. On `drift`, the user decides which side is right — never pick one yourself. Model right: `plan` and `apply` with `dvac-metadata`, then export and unpack again. Solution right: the import workflow below.

## Workflow: import → review → write → plan

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run `import` **without** `--write` and show the user the file list, the diffs, the kept entries and the skipped components.
3. After the user confirms, run the `NEXT` line exactly as printed.
4. Run `plan` with `dvac-metadata` (agent `dvac-metadata-planner`). After an import from the same dev environment the plan should be empty; anything left is drift to discuss with the user, never to apply blindly.

## Safety model

- **The unpacked solution is read only.** No subcommand writes, moves or reformats a file in it. A `--model` folder or a written file inside an unpacked solution is refused (`STATUS: refused`). A difference is fixed in the model, or in dev followed by a new export — never in the XML.
- **Offline and local.** The tool never contacts Dataverse. `compare` writes nothing; `import` writes only the model files of `--model`, and only with `--write`.
- **Never reduces the model.** Tables, columns, keys, Choices and options that are only in the model are kept and listed. Removing them is a deliberate, manual act.
- **Keeps what the solution does not decide.** `_meta` is never rewritten. A column already in the model keeps its choice of optional fields (`max_length`, `min`, `auditing`…): the format does not manage what the file leaves out, so the import does not start managing it — `compare` shows those values under `·`.
- **Minimal rewrites.** A file whose data does not change is not rewritten, so its comments survive. A rewritten file keeps its header comment but loses inline comments — the diff shows it.
- **No silent loss.** Every solution component the format cannot hold is listed with its reason.

## Common issue: skipped components

Local choices, text columns with a format the model has no type for (such as `textarea`), calculated and rollup columns, polymorphic lookups and columns of standard tables are skipped (`!` in `compare`). A formula that reads a skipped column, or a lookup to a table of another solution, makes the import invalid: import the other solution too, or narrow the run with `--tables`. See [`references/solution-mapping.md`](references/solution-mapping.md#skipped).

## Migration from `dvac-metadata-import` (2.0.0)

The skill `dvac-metadata-import` is now `dvac-metadata-solution`, and `dv_metadata_import.py solution <dir>…` is now `dv_metadata_solution.py import <dir>…` (same options, same output). `compare` is new.

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing, reformatting or "fixing" the unpacked solution's XML | Fix the model, or change the component in dev and export again |
| Choosing on your own which side of a `drift` is right | Show the sections and let the user decide |
| Running `--write` without showing the diff to the user | Run without `--write`, review, then write |
| Deleting model entries the import lists as kept | Keep them, or remove them deliberately after checking the server |
| Running `apply` right after an import without a plan | `plan` first; an import from dev should give an empty plan |
| Importing a managed solution exported from UAT or prod | Import the unmanaged solution of the dev environment |

## Skill boundaries

- Plan and apply the model → `dvac-metadata` / agent `dvac-metadata-planner`
- Plug-in registrations from the same solution → `dvac-plugins-solution` (plugin `dataverse-plugins-as-code`)
- Security roles from the same solution → `dvac-security-solution` (plugin `dataverse-security-as-code`)
- Exporting and unpacking solutions → `dataverse:dv-solution`
- The YAML source control format (`solutions/*/solution.yml`) → not read; unpack in the XML layout
- Naming and design reviews of what was imported → the project's best practices
