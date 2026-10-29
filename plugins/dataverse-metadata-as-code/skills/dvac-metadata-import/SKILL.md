---
name: dvac-metadata-import
description: >
  Regenerates or aligns the Dataverse metadata model (dataverse/metadata/*.dv.metadata.yaml)
  from one or more solutions unpacked in the repo (pac solution unpack layout): global
  Choices, tables, columns, formula columns, lookups and alternate keys, offline, with a
  diff before any write and without ever removing what is only in the model. Use when the
  user wants to adopt an existing environment as code, reverse-engineer the YAML from an
  exported solution, or bring the model back in line with what was changed on the server
  and exported.
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

`dataverse:dv-solution` owns exporting and unpacking solutions (`pac solution export`, `pac solution unpack`); `dataverse:dv-metadata` owns how metadata works. Check that the official `dataverse` plugin is installed; if it is not, **strongly recommend the user install it**. This skill only maps an unpacked solution onto the `dvac-metadata` model format.

## Best practices

This skill is convention-neutral: it writes the names, labels and types the solution holds, as they are. Where unpacked solutions live and how often they are refreshed are the project's best practices: look for them (README, `AGENTS.md`, installed skills) and follow them. If there are none, ask the user for the solution folder, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads the unpacked solution and the model files only. Getting a fresh export is the official skill's job (`dataverse:dv-solution`).

## Repository layout

```
<repo-root>/
├── <unpacked solution>/              # input, read only — any folder holding Other/Solution.xml
│   ├── Other/Solution.xml, Other/Customizations.xml, Other/Relationships/*.xml
│   ├── OptionSets/*.xml
│   └── Entities/<Table>/Entity.xml (+ Formulas/*.yaml)
└── dataverse/
    └── metadata/                     # output — the dvac-metadata model
        ├── _meta.dv.metadata.yaml    # written only when missing
        ├── choices.dv.metadata.yaml
        └── tables/<key>.dv.metadata.yaml
```

## What this skill adds

One tool, `scripts/dv_metadata_import.py`:

```sh
python3 <this-skill>/scripts/dv_metadata_import.py solution <unpacked-dir>... \
        [--model dataverse/metadata] [--tables <a,b>] [--write] [--no-diff]
```

- **Without `--write` nothing is written.** It prints `+` new file, `~` changed file with its diff, `=` unchanged, then what is **only in the model** (kept) and what the format **cannot describe** (skipped). Exit code `2` means changes are pending.
- **`--write`** writes the new and changed files, after validating the whole model with the `dvac-metadata` validator. An invalid result is refused and nothing is written.
- **Several solutions** are merged: a table split across solutions gets the columns of each; a component described differently by two solutions is an error.
- It loads and validates with the `dvac-metadata` tool of the same plugin: install that skill's requirements (`pip install -r <plugin>/skills/dvac-metadata/scripts/requirements.txt`, inside a venv).

How each XML element maps to the format, and every skip reason, are in [`references/solution-mapping.md`](references/solution-mapping.md).

## Workflow: import → review → write → plan

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run the tool **without** `--write` and show the user the file list, the diffs, the kept entries and the skipped components.
3. After the user confirms, run it again with `--write`.
4. Run `validate`, then `plan` with `dvac-metadata` (agent `dvac-metadata-planner`). After an import from the same dev environment the plan should be empty; anything left is drift to discuss with the user, never to apply blindly.

## Safety model

- **Offline and local.** The tool never contacts Dataverse. It writes only the model files of `--model`, and only with `--write`.
- **Never reduces the model.** Tables, columns, keys, Choices and options that are only in the model are kept and listed. Removing them is a deliberate, manual act.
- **Keeps what the solution does not decide.** `_meta` is never rewritten. A column already in the model keeps its choice of optional fields (`max_length`, `min`, `auditing`…): the format does not manage what the file leaves out, so the import does not start managing it.
- **Minimal rewrites.** A file whose data does not change is not rewritten, so its comments survive. A rewritten file keeps its header comment but loses inline comments — the diff shows it.
- **No silent loss.** Every solution component the format cannot hold is listed with its reason.

## Common issue: skipped components

Local choices, text columns with a format the model has no type for (such as `textarea`), calculated and rollup columns, polymorphic lookups and columns of standard tables are skipped. A formula that reads a skipped column, or a lookup to a table of another solution, makes the result invalid: import the other solution too, or narrow the run with `--tables`. See [`references/solution-mapping.md`](references/solution-mapping.md#skipped).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Running `--write` without showing the diff to the user | Run without `--write`, review, then write |
| Deleting model entries the import lists as kept | Keep them, or remove them deliberately after checking the server |
| Running `apply` right after an import without a plan | `validate`, then `plan`; an import from dev should give an empty plan |
| Editing the unpacked solution to make the import pass | Fix the model, or change the component in dev and export again |
| Importing a managed solution exported from UAT or prod | Import the unmanaged solution of the dev environment |

## Skill boundaries

- Plan and apply the model → `dvac-metadata` / agent `dvac-metadata-planner`
- Plug-in registrations from the same solution → `dvac-plugins-import` (plugin `dataverse-plugins-as-code`)
- Exporting and unpacking solutions → `dataverse:dv-solution`
- Naming and design reviews of what was imported → the project's best practices
