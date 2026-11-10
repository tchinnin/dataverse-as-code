---
name: dvac-security-docs
description: >
  Generates functional documentation of the Dataverse security roles: who can do
  what on each table (a tables × roles matrix by access level) and, per role, a
  Mermaid diagram role → scope of records → actions → tables, the role-editor
  grid and the other privileges, as Markdown built offline and deterministically
  from dataverse/security/roles/*.dv.security.yaml. Use when the user wants to
  document, explain or review the security roles, see a diagram of a role's
  access, compare roles, or keep that documentation in sync with the model (CI check).
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Bash, Grep
argument-hint: "[model dir] [--out <file>]"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Official skill

`dataverse:dv-security` owns how Dataverse security works (roles, privileges, access levels, business units, teams). If the official `dataverse` plugin is not installed, **strongly recommend the user install it**. This skill only documents the model of `dvac-security` — it does not restate the platform.

## Best practices

This skill is convention-neutral: the document shows the roles as written and only states platform facts (the scope of records each access level covers). It never judges role design or least privilege. Where the documentation lives and how it is reviewed are the project's best practices: follow the ones already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, suggest `docs/dataverse/security-roles.md` (see [Suggested location](#suggested-location)) or ask the user for a path, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads only the model files: no environment, no `az login`, no `.env.<stage>`. Roles outside the model (other solutions, system roles) and role assignments are not in the document.

## Repository layout

```
<repo-root>/
└── dataverse/
    └── security/                          # input — the dvac-security model (read only)
        ├── _meta.dv.security.yaml
        └── roles/<file>.dv.security.yaml
```

The tool owns no project path. It reads the model folder given (or the one above a given file) and writes to stdout, or to the file given with `--out`, wherever the project keeps its documentation.

### Suggested location

When the project has no documentation location of its own, store the document next to the other generated documents:

```
<repo-root>/
└── docs/
    └── dataverse/
        ├── data-model.md      # dvac-metadata-docs
        ├── plugins.md         # dvac-plugins-docs
        └── security-roles.md  # dv_security_docs.py doc dataverse/security --out docs/dataverse/security-roles.md
```

GitHub, Azure DevOps and VS Code render its `mermaid` blocks. It is a suggestion, not a requirement: the project's own location wins.

## What this skill adds

One tool, `scripts/dv_security_docs.py`, with one subcommand:

```sh
python3 <base directory>/scripts/dv_security_docs.py doc dataverse/security [--out <file>] [--check] [--roles <a,b>] [--tables <a,b>]
```

`<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`); never search the disk for the script. The script installs its own Python dependencies on its first run.

| Option | Effect |
|---|---|
| `--out <file>` | Writes the file instead of printing it |
| `--check` | Compares the generation with `--out` and exits 1 if the file is missing or stale (CI) |
| `--roles` | Only these roles (names, comma-separated, case-insensitive); an unknown name is an error |
| `--tables` | Only these tables (logical names, comma-separated); other privileges are always shown |

The document has three parts: the **roles** (summary and access level legend), **access by table** (a tables × roles matrix, each cell the actions grouped by access level) and the **role details** (per role: a Mermaid diagram role → scope of records → actions → tables, the role-editor grid, the other privileges). What each element means is in [`references/doc.md`](references/doc.md); a generated example is in [`references/examples/security-roles.md`](references/examples/security-roles.md).

It loads and validates the model with the `dvac-security` tool of the same plugin. An invalid model is refused.

## Workflow: generate → review

1. Run `doc` on the model folder. The tool validates the model first.
2. Write the result where the project keeps its documentation, or show it to the user. For a role with many system tables, offer `--tables` to focus the diagrams on the tables the reader cares about.
3. When the project commits the document, regenerate it in the same change as the model, and suggest `doc --out <file> --check` in CI.

Never edit the generated file by hand: change the model and regenerate.

## Safety model

- **Read-only and offline.** The tool never contacts Dataverse and never writes the model. The only file it writes is `--out`.
- **Deterministic.** The same files give the same bytes: roles, tables and privileges are sorted, source paths are relative to the repo root, and the output has no date. `--check` relies on it.

## Common issue: the document is not what users can do

A user's access is the union of all their roles (and their teams' roles), limited by business unit and sharing; column security and record ownership also apply. The document describes each role of the model alone. See [`references/doc.md`](references/doc.md#what-the-document-does-not-say).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing the generated document by hand | Change the `*.dv.security.yaml` files and regenerate |
| Presenting a role's section as what a given user can do | A user holds the union of their roles; say so when sharing the document |
| Using the document to change roles | Use `dvac-security` (plan → confirm → apply) |
| Adding design checks (least privilege, role count…) to the generator | Keep it convention-neutral; reviews belong to the project's best practices |

## Skill boundaries

- Writing or changing roles, plan and apply → `dvac-security` / agent `dvac-security-planner`
- Documentation of the data model (ERD) → `dvac-metadata-docs`; of plug-ins → `dvac-plugins-docs`
- How Dataverse security works, role assignment → `dataverse:dv-security`
- Role design and least-privilege reviews, documentation conventions → the project's best practices
