---
name: dvac-security-solution
description: >
  Works between the security roles model (dataverse/security/roles/*.dv.security.yaml)
  and solutions unpacked in the repo (pac solution unpack layout), offline and without
  ever modifying the unpacked XML: import regenerates or aligns the role files from the
  solution's roles and their privileges with a diff before any write and without removing
  a privilege that is only in the model, and compare lists what differs between the
  solution and the model, privilege by privilege. Use when the user wants to adopt the
  security roles of an existing solution as code, check what differs between an unpacked
  solution and the role files, or bring the role files back in line with an export.
license: MIT
user-invocable: true
model: sonnet
allowed-tools: Read, Write, Edit, Bash, Grep
argument-hint: "import|compare <unpacked solution folder>... [--write]"
metadata:
  author: dataverse-as-code
  version: "1.0.0"
---

## Run

Every step is one command. `DV` means `python3 <base directory>/scripts/dv_security_solution.py`, where `<base directory>` is the absolute path given when this skill was loaded (`Base directory for this skill: …`). **Never search the disk for the script** — other copies may be stale. The script installs its own Python dependencies on its first run.

- **Compare**: `DV compare <unpacked-dir>... [--model dataverse/security] [--roles "a,b"]`. It writes nothing.
- **Import**: `DV import <unpacked-dir>... [--model dataverse/security] [--roles "a,b"]`. It writes nothing without `--write`.

Show the output to the user **unchanged**, then act on its `STATUS:` line:

| STATUS | Do |
|---|---|
| `in-sync` | Done: the role files and the solution describe the same roles. |
| `drift` | Ask the user which side is right. The model: plan and apply with `dvac-security`, then export and unpack again. The solution: run the `NEXT` line (an import without `--write`). |
| `changes-pending` | Ask the user to confirm the file list and diffs, then run the `NEXT` line exactly as printed. |
| `no-changes` | Done. |
| `written` | Run the `NEXT` line (`plan` with `dvac-security`). |
| `refused` | Do what `FIX` says: the write target is inside an unpacked solution. |
| `error` | Do what `FIX` says. Don't investigate further. |

## Official skill

`dataverse:dv-solution` owns exporting and unpacking solutions (`pac solution export`, `pac solution unpack`); `dataverse:dv-security` owns how roles, privileges and access levels work. If the official `dataverse` plugin is not installed, **strongly recommend the user install it**. This skill only maps an unpacked solution onto the `dvac-security` model format.

## Best practices

This skill is convention-neutral: it writes and compares the role names and privileges the solution holds, as they are. Where unpacked solutions live and how roles are designed are the project's best practices: follow the ones already in your context (the project's instructions, loaded skills) — don't search for them. If there are none, ask the user for the solution folder, and offer best-practice skills as an option — for example the `powerplatform-bestpractices` marketplace (`ppbp-*` skills).

## Connection

None. The tool reads the unpacked solution and the role files only. To read a role straight from the dev environment instead, `dv_security.py adopt` (skill `dvac-security`) does it.

## Repository layout

```
<repo-root>/
├── <unpacked solution>/                  # input, READ ONLY — any folder holding Other/Solution.xml
│   ├── Other/Solution.xml
│   ├── Roles/<Role name>.xml
│   └── Entities/<Table>/                 # only the table names are read
└── dataverse/
    └── security/                         # import output — the dvac-security model
        ├── _meta.dv.security.yaml        # written only when missing
        └── roles/<role-slug>.dv.security.yaml
```

## What this skill adds

One tool, `scripts/dv_security_solution.py`, with two subcommands.

**`compare`** writes nothing. Roles are matched by name (case-insensitive), privileges by name and access level. It prints five sections:

| Mark | Section | Counts as drift |
|---|---|---|
| `+` | only in the solution (a role, or a privilege a role holds) | yes |
| `-` | only in the model | yes |
| `~` | differs: an access level, or a declared `description` / `member_inheritance` | yes |
| `·` | a `description` or `member_inheritance` the solution holds that the file doesn't declare (only when the role XML carries them — exports usually don't) | no |
| `!` | what cannot be compared offline (see below) | no |

It ends with `STATUS: in-sync` or `STATUS: drift` (+ `REASON`, `FIX`, `NEXT`) and exits 0 (`--detailed-exitcode`: 2 on drift). It needs a valid model (`_meta` present).

**`import`** regenerates the role files:

- **Without `--write` nothing is written.** It prints `+` new file, `~` changed file with its diff, `=` unchanged, then the privileges **only in the model** (kept) and what could **not be read** (skipped). The output ends with `STATUS:` (`changes-pending`, `no-changes` or `error`) and, when files would change, the exact `NEXT` command with `--write`. It exits 0 (`--detailed-exitcode`: 2 when files would change).
- **`--write`** writes the new and changed files, after validating the whole model with the `dvac-security` validator. An invalid result is refused and nothing is written.

Both take several solutions; a role described differently by two of them is an error. They load and validate with the `dvac-security` tool of the same plugin. The mapping is in [`references/solution-mapping.md`](references/solution-mapping.md).

## Workflow: compare → review

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run `compare` and show the user the sections.
3. On `drift`, the user decides which side is right — never pick one yourself. Model right: `plan` and `apply` with `dvac-security`, then export and unpack again. Solution right: the import workflow below.

## Workflow: import → review → write → plan

1. Make sure the unpacked solution is current (`dataverse:dv-solution` exports and unpacks it).
2. Run `import` **without** `--write` and show the user the file list, the diffs, the kept privileges and the skipped entries.
3. After the user confirms, run the `NEXT` line exactly as printed.
4. Run `plan` with `dvac-security` (agent `dvac-security-planner`). After an import from the same dev environment the plan should be empty; anything left is drift to discuss with the user, never to apply blindly.

## Safety model

- **The unpacked solution is read only.** No subcommand writes, moves or reformats a file in it. A `--model` folder or a written file inside an unpacked solution is refused (`STATUS: refused`). A difference is fixed in the model, or in dev followed by a new export — never in the XML.
- **Offline and local.** The tool never contacts Dataverse. `compare` writes nothing; `import` writes only the files of `--model`, and only with `--write`.
- **Never reduces the model.** A role file is a role's complete privilege set: `apply` revokes what it leaves out. So a privilege only in the model is kept and listed, never removed; a role only in the model is left as is. Removing a privilege is a deliberate act, followed by a plan the user confirms (revokes need double confirmation).
- **Keeps what the solution does not decide.** `_meta` is never rewritten. A privilege keeps its place in the file (`tables:` or `privileges:`). A role file that leaves `member_inheritance` out keeps it out. No GUID is written.
- **Minimal rewrites.** A file whose data does not change is not rewritten, so its comments survive. A rewritten file keeps its header comment but loses inline comments — the diff shows it.

## Common issue: table privilege names

A table privilege is named `prv<Verb><table>`, but some system tables use another name (for example `prvReadUser` for `systemuser`). Offline, the tool puts a privilege under `tables:` only when the table is already in the role file or in the solution's `Entities/`; otherwise under `privileges:` by name, which the format accepts. `compare` lists a table privilege it cannot match by name under `!`, and `plan` compares it with the environment. See [`references/solution-mapping.md`](references/solution-mapping.md#table-privileges).

## Anti-patterns (DO NOT DO)

| Anti-pattern | Correct approach |
|---|---|
| Editing, reformatting or "fixing" the unpacked solution's XML | Fix the model, or change the role in dev and export again |
| Choosing on your own which side of a `drift` is right | Show the sections and let the user decide |
| Running `--write` without showing the diff to the user | Run without `--write`, review, then write |
| Deleting a privilege because the import lists it as kept | Check the server first; a removed privilege is revoked by the next `apply` |
| Moving `privileges:` entries under `tables:` by guessing the table | Use `dv_security.py adopt` or `plan`, which read the privilege metadata |
| Importing a managed solution exported from UAT or prod | Import the unmanaged solution of the dev environment |

## Skill boundaries

- Plan and apply the roles → `dvac-security` / agent `dvac-security-planner`
- A role read from the dev environment → `dv_security.py adopt` (skill `dvac-security`)
- Tables, columns and Choices from the same solution → `dvac-metadata-solution` (plugin `dataverse-metadata-as-code`)
- Plug-in registrations from the same solution → `dvac-plugins-solution` (plugin `dataverse-plugins-as-code`)
- Exporting and unpacking solutions → `dataverse:dv-solution`
- The YAML source control format (`solutions/*/solution.yml`) → not read; unpack in the XML layout
- Role design and least-privilege reviews → the project's best practices
