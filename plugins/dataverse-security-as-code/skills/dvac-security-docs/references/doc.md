# Security role documentation reference

`dv_security_docs.py doc` reads the security model (`_meta.dv.security.yaml` and every `roles/*.dv.security.yaml`) and writes one Markdown document with Mermaid [flowcharts](https://mermaid.js.org/syntax/flowchart.html). GitHub, Azure DevOps and VS Code (with a Mermaid extension) render it.

## Roles

A summary table, sorted by role name: description, member's privilege inheritance, number of tables with at least one privilege, number of other privileges, and the source file (relative to the repo root). Each role name links to its section.

Then the access level legend:

| Symbol | Model value | Scope of records |
|---|---|---|
| 🟢 | `organization` | all records |
| 🔴 | `parent_child` | the user's business unit and every business unit below it |
| 🔵 | `business_unit` | the user's business unit |
| ⚪ | `user` | records the user owns or that are shared with them, and those of their teams |
| - | `none` or left out | no access |

## Access by table

One matrix: a row per table that at least one role can access, a column per role. A cell lists the actions the role can perform, grouped by access level, widest first — for example `🔵 read` then `⚪ create, write`. It answers "who can do what on this table".

## Role details

One section per role:

- **Diagram.** `role → scope of records → actions → tables`. Each scope node (coloured like its emoji: green, red, blue, white) leads to one node per set of actions, listing the tables on which the role holds exactly those actions at that scope. A table whose actions have different levels appears under each of them (e.g. read on the business unit, write on own records). The other privileges of a scope are grouped in one `other privileges` node.
- **Table privileges.** The role-editor grid: one row per table, one column per action (create, read, write, delete, append, append to, assign, share), the level emoji in each cell, `-` for no access.
- **Other privileges.** Each named privilege (`prv…`) and its level, as written in the model.
- **Properties.** Member's privilege inheritance (`not managed by the model` when the file leaves it out) and the source file.

A privilege named in `privileges:` is shown under its name even when it is a table privilege (`prvReadAccount`): the document shows the model as written.

## Filtering

- `--roles a,b` keeps these roles (names compared without regard to case). An unknown name is an error, so a typo never yields an incomplete document silently.
- `--tables a,b` keeps these tables in the matrix, the diagrams and the grids. Other privileges are always shown. Useful when roles hold many system tables.

## What the document does not say

- **A user's effective access.** A user holds the union of the privileges of all their roles, including the roles of their teams (depending on member's privilege inheritance). Business units, sharing, record ownership, column security profiles and record-filter privileges also apply.
- **Roles outside the model.** System roles, roles of other solutions and roles not yet adopted are not read.
- **Who holds a role.** Role assignment is not part of the model.

## CI check

```sh
python3 <skill>/scripts/dv_security_docs.py doc dataverse/security --out docs/dataverse/security-roles.md --check
```

Exit code `0` — the file is up to date. Exit code `1` — the file is missing or differs, or a model file is invalid. The path `docs/dataverse/security-roles.md` is the suggested location; use the project's own when it has one. Run the check with the same options as the command that wrote the file.
