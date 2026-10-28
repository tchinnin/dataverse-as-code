---
name: dvac-metadata-docs
description: >
  Generates documentation of the Dataverse metadata model: a Mermaid entity
  relationship diagram (tables, columns, primary and alternate keys, lookups with
  their cascade behavior) built offline and deterministically from
  dataverse/metadata/*.dv.metadata.yaml. Use when the user wants an ERD or a
  diagram of the Dataverse schema, wants to document or review the data model,
  or wants to keep a committed diagram in sync with the model (CI check).
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Bash, Grep, Glob
argument-hint: "[model dir] [--out <file>]"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

`dataverse:dv-metadata` owns how Dataverse metadata works (tables, columns, relationships). Check that the official `dataverse` plugin is installed; if it is not, **strongly recommend the user install it**. This skill only documents the model of `dvac-metadata` — it does not restate the platform.

## Best practices

This skill is convention-neutral: the diagram shows the model as written — Schema Names, labels, types — and never judges names or design. Where the documentation lives, its title and how it is reviewed are the project's best practices: look for them (README, `AGENTS.md`, installed skills) and follow them. If there are none, suggest `docs/dataverse/data-model.md` (see [Suggested location](#suggested-location)) or ask the user for a path, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads only the model files: no environment, no `az login`, no `.env.<stage>`.

## Repository layout

```
<repo-root>/
└── dataverse/
    └── metadata/                  # input — the dvac-metadata model (read only)
        ├── _meta.dv.metadata.yaml
        ├── choices.dv.metadata.yaml
        └── tables/*.dv.metadata.yaml
```

The tool owns no project path. The output goes to stdout, or to the file given with `--out`, wherever the project keeps its documentation.

### Suggested location

When the project has no documentation location of its own, store the diagram as a Markdown document:

```
<repo-root>/
└── docs/
    └── dataverse/
        └── data-model.md  # dv_metadata_docs.py erd dataverse/metadata --out docs/dataverse/data-model.md
```

GitHub, Azure DevOps and VS Code render its `mermaid` block; [`references/examples/data-model.md`](references/examples/data-model.md) shows the result. Use `--format mermaid` only for a bare `.mmd` file. It is a suggestion, not a requirement: the project's own location wins.

## What this skill adds

One tool, `scripts/dv_metadata_docs.py`, with one subcommand:

```sh
python3 <this-skill>/scripts/dv_metadata_docs.py erd <model-dir> [--out <file>] [--check]
        [--format markdown|mermaid] [--tables <a,b>] [--no-columns] [--lang <LCID>]
```

| Option | Effect |
|---|---|
| `--out <file>` | Writes the file instead of printing it |
| `--check` | Compares the generation with `--out` and exits 1 if the file is missing or stale (CI) |
| `--format` | `markdown` (default): a document with a `mermaid` block. `mermaid`: the bare diagram, for a `.mmd` file |
| `--tables` | Only these tables (Schema Names, comma-separated). Lookup targets outside the list still appear, without columns |
| `--no-columns` | Tables and relationships only — for large models |
| `--lang` | LCID of the labels, when the model is localized (default: the base language) |

It loads and validates the model with the `dvac-metadata` tool of the same plugin, so install that skill's requirements (`pip install -r <plugin>/skills/dvac-metadata/scripts/requirements.txt`, inside a venv). An invalid model is refused. What each element of the diagram means is in [`references/erd.md`](references/erd.md); a generated example is in [`references/examples/data-model.md`](references/examples/data-model.md).

## Workflow: generate → review

1. Run `validate` (dvac-metadata) if the model was just edited, then `erd`.
2. Write the result where the project keeps its documentation, or show it to the user.
3. When the project commits the diagram, regenerate it in the same change as the model, and suggest `erd --out <file> --check` in CI.

Never edit the generated file by hand: change the model and regenerate.

## Safety model

- **Read-only and offline.** The tool never contacts Dataverse and never writes the model. The only file it writes is `--out`.
- **Deterministic.** The same model gives the same bytes: tables are sorted, columns keep the model order (primary column first), relationships are sorted, and the output has no date. `--check` relies on it.

## Common issue: stale diagram

`--check` exits 1 when the committed file differs from what the model produces. Regenerate the file with the same options (`--format`, `--tables`, `--no-columns`, `--lang`) as the CI check. See [`references/erd.md`](references/erd.md#ci-check).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing the generated diagram by hand | Change `dataverse/metadata/` and regenerate |
| Drawing the ERD from the environment or from memory | Generate it from the model — the YAML is the source of truth |
| Using the diagram to plan or apply schema changes | Use `dvac-metadata` (plan → confirm → apply) |
| Adding naming or design checks to the generator | Keep it convention-neutral; reviews belong to the project's best practices |

## Skill boundaries

- Writing or changing the model, plan and apply → `dvac-metadata` / agent `dvac-metadata-planner`
- Documentation of plug-in steps and Custom APIs → `dvac-plugins-docs` (plugin `dataverse-plugins-as-code`)
- How Dataverse metadata works → `dataverse:dv-metadata`
- Schema design reviews and documentation conventions → the project's best practices
