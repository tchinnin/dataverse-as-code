# Plug-in documentation reference

`dv_plugin_docs.py doc` reads every `*.dv.plugin.yaml` below a path and writes one Markdown document with Mermaid [flowcharts](https://mermaid.js.org/syntax/flowchart.html). GitHub, Azure DevOps and VS Code (with a Mermaid extension) render it.

## Source files

A table of the files read: path (relative to the repo root), `package` or `assembly`, its name, and its number of steps and Custom APIs.

## Execution pipeline

One section per **table × message**, sorted by table then message. Steps without a table (a global Custom API message, for example) are listed under `(any table)`.

The diagram follows the order Dataverse runs the steps:

| Block | Content |
|---|---|
| Pre-validation | sync steps, stage 10 |
| Pre-operation | sync steps, stage 20 |
| Main operation | the platform operation; for a Custom API message, the plug-in class that implements it |
| Post-operation | sync steps, stage 40 |
| Post-operation · async | async steps (always post-operation) |

Inside a block, steps are sorted by rank, then by name:

- `-->` between two steps: the second has a higher rank, so it runs after the first.
- `-.-` between two sync steps: same stage and rank — Dataverse does not guarantee their order. The section also lists them in a note.
- No line between async steps: they are queued after the operation and their order is not guaranteed.

A disabled step (`enabled: false`) is drawn dashed and marked `disabled`. Only blocks that have steps are drawn, plus the main operation.

The table under the diagram gives, for each step in that order: stage, mode, rank, step name (its identity), full plug-in class, filtering attributes, images (`name (type: columns)`, or `all columns`), enabled, and description.

## Custom APIs

A diagram links each bound table to its Custom APIs (`-- collection -->` for an entity-collection binding) and each API to the plug-in class that implements it. Then, per API sorted by unique name: description, display name, kind (`action` or `function`), binding, plug-in, allowed custom steps, workflow enabled, private, execute privilege, source file, and the request parameters and response properties.

## Filtering

`--tables a,b` keeps the steps on those tables and the Custom APIs bound to them. The main operation of a Custom API message is still named when the API itself is filtered out.

## CI check

```sh
python3 <skill>/scripts/dv_plugin_docs.py doc . --out docs/dataverse/plugins.md --check
```

Exit code `0` — the file is up to date. Exit code `1` — the file is missing or differs, or a model file is invalid. The path `docs/dataverse/plugins.md` is the suggested location; use the project's own when it has one. Run the check from the same path and with the same options as the command that wrote the file.
