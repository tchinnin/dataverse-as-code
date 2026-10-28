---
name: dvac-plugins-docs
description: >
  Generates documentation of the Dataverse plug-in registrations: the execution
  pipeline of each table and message (steps in the order Dataverse runs them,
  around the main operation) and the Custom APIs with their signatures, as
  Markdown with Mermaid diagrams, built offline and deterministically from every
  *.dv.plugin.yaml file. Use when the user wants to see or document which
  plug-in steps run on a table, in what order, which Custom APIs exist, or wants
  to keep that documentation in sync with the model (CI check).
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Bash, Grep, Glob
argument-hint: "[path] [--out <file>]"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

The official `dataverse` plugin owns how plug-ins, steps and Custom APIs work (`dataverse:dv-overview` routes to the right skill). Check that it is installed; if it is not, **strongly recommend the user install it**. This skill only documents the model of `dvac-plugins` — it does not restate the platform.

## Best practices

This skill is convention-neutral: the document shows the registrations as written and only states platform facts (execution order, steps whose order is not guaranteed). It never judges step design. Where the documentation lives and how it is reviewed are the project's best practices: look for them (README, `AGENTS.md`, installed skills) and follow them. If there are none, suggest `docs/dataverse/plugins.md` (see [Suggested location](#suggested-location)) or ask the user for a path, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads only the model files: no environment, no `az login`, no `pac`, no `.env.<stage>`. Steps registered outside the model (other solutions, ISVs) are not in the document.

## Repository layout

```
<repo-root>/
└── plugins/
    └── <PluginName>/
        └── <PluginName>.dv.plugin.yaml   # input — the dvac-plugins model (read only)
```

The tool owns no project path. It reads every `*.dv.plugin.yaml` below the given path (usually the repo root) and writes to stdout, or to the file given with `--out`, wherever the project keeps its documentation.

### Suggested location

When the project has no documentation location of its own, store the document next to the ERD of `dvac-metadata-docs`:

```
<repo-root>/
└── docs/
    └── dataverse/
        ├── data-model.md  # dvac-metadata-docs
        └── plugins.md     # dv_plugin_docs.py doc . --out docs/dataverse/plugins.md
```

The document is Markdown, like the ERD: it holds several diagrams and the detail tables, and GitHub, Azure DevOps and VS Code render its `mermaid` blocks. It is a suggestion, not a requirement: the project's own location wins.

## What this skill adds

One tool, `scripts/dv_plugin_docs.py`, with one subcommand:

```sh
python3 <this-skill>/scripts/dv_plugin_docs.py doc <path> [--out <file>] [--check] [--tables <a,b>]
```

| Option | Effect |
|---|---|
| `--out <file>` | Writes the file instead of printing it |
| `--check` | Compares the generation with `--out` and exits 1 if the file is missing or stale (CI) |
| `--tables` | Only the steps and bound Custom APIs of these tables (logical names, comma-separated) |

The document has three parts: the source files, the **execution pipeline** (one Mermaid diagram and one table per table × message) and the **Custom APIs** (a diagram of bound tables → APIs → plug-in classes, then each signature). What each element means is in [`references/doc.md`](references/doc.md); a generated example is in [`references/examples/plugins.md`](references/examples/plugins.md).

It loads and validates the files with the `dvac-plugins` tool of the same plugin, so install that skill's requirements (`pip install -r <plugin>/skills/dvac-plugins/scripts/requirements.txt`, inside a venv). An invalid file is refused; a missing build output is not needed.

## Workflow: generate → review

1. Run `validate` (dvac-plugins) if a file was just edited, then `doc`.
2. Write the result where the project keeps its documentation, or show it to the user. Point out the steps flagged as having no guaranteed order.
3. When the project commits the document, regenerate it in the same change as the model, and suggest `doc --out <file> --check` in CI.

Never edit the generated file by hand: change the model and regenerate.

## Safety model

- **Read-only and offline.** The tool never contacts Dataverse and never writes the model. The only file it writes is `--out`.
- **Deterministic.** The same files give the same bytes: groups and APIs are sorted, source paths are relative to the repo root, and the output has no date. `--check` relies on it.

## Common issue: execution order

Dataverse orders the steps of one table and message by stage, then rank. Sync steps of the same stage and rank have no guaranteed order, and async steps have none at all: the document joins the former with a dotted line and lists them in a note. See [`references/doc.md`](references/doc.md#execution-pipeline).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing the generated document by hand | Change the `*.dv.plugin.yaml` files and regenerate |
| Presenting the document as everything that runs in the environment | It covers the model only; registrations of other solutions are not in it |
| Using the document to register or change steps | Use `dvac-plugins` (plan → confirm → apply) |
| Adding design checks (step count, sync vs. async…) to the generator | Keep it convention-neutral; reviews belong to the project's best practices |

## Skill boundaries

- Writing or changing registrations, plan and apply → `dvac-plugins` / agent `dvac-plugins-planner`
- Documentation of the data model (ERD) → `dvac-metadata-docs` (plugin `dataverse-metadata-as-code`)
- How plug-ins, steps and Custom APIs work → the official `dataverse` skills
- Step design reviews and documentation conventions → the project's best practices
