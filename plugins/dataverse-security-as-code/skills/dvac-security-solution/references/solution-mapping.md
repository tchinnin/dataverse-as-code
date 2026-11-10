# Solution → role file mapping

`dv_security_solution.py` reads the SolutionPackager XML layout produced by `pac solution unpack` — read only, it never writes in the unpacked folder. This page lists what it reads and how each element becomes a `.dv.security.yaml` field, for `import` and `compare` alike. The format itself is described in the `dvac-security` skill (`references/format.md`).

## Files read

| File | Content used |
|---|---|
| `Other/Solution.xml` | `UniqueName` — the `solution` of a missing `_meta` |
| `Roles/*.xml` | One security role each: `<Role name="…">`, its `RolePrivileges/RolePrivilege`, and `Description` / `IsInherited` when present |
| `Entities/*/Entity.xml` | Only `<Name>`: the tables of the solution, to tell table privileges apart |

Everything else in the solution (tables, forms, plug-ins…) is ignored here; the other solution skills read it.

## Roles

| XML | Field |
|---|---|
| `Role/@name` | `name` — the identity, compared without regard to case; a new file is `roles/<slug of the name>.dv.security.yaml` |
| `Role/Description` | `description` — when the XML holds it and it is not empty |
| `Role/IsInherited` (0 / 1) | `member_inheritance` (`team_only` / `direct_and_team`) — when the XML holds it; a new file gets it, an existing file only if it already declares it |
| `RolePrivilege/@name`, `@level` | A privilege and its access level |

`Description` and `IsInherited` are read only when the role XML carries them; when it doesn't, the file's values are kept and not compared. The `pac solution unpack` exports checked so far carry neither (a role holds `IsCustomizable`, `IsAutoAssigned` and `RolePrivileges` only, in a file that starts with a UTF-8 BOM), so in practice `description` and `member_inheritance` stay as the file declares them and `plan` compares them with the environment. The role `id` is never written.

## Access levels

| `level` | Model |
|---|---|
| `Basic` | `user` |
| `Local` | `business_unit` |
| `Deep` | `parent_child` |
| `Global` | `organization` |

A privilege with another level is skipped and listed.

## Table privileges

A table privilege is named `prv` + verb + table: `prvCreate`, `prvRead`, `prvWrite`, `prvDelete`, `prvAppend`, `prvAppendTo`, `prvAssign`, `prvShare` (`create`, `read`, `write`, `delete`, `append`, `append_to`, `assign`, `share`). The table part is compared without regard to case.

**Import.** A privilege keeps the place it has in the existing file. A new one goes under `tables.<table>.<verb>` when `<table>` is a table the role files already list or a table of the solution's `Entities/`; otherwise it goes under `privileges:` by its name. Some system tables have privilege names that don't follow the pattern (for example `prvReadUser` for `systemuser`): offline, nothing tells which table they belong to, so they stay under `privileges:`. The format accepts both forms; `dv_security.py adopt` and `plan` read the privilege metadata of the environment.

**Compare.** A `tables.<table>.<verb>` entry is looked for as `prv<Verb><table>`. When it is not found and the table is not a table of the solution, it is listed under `!` (not comparable offline) instead of `-`, and the solution privileges of the same verb that nothing matched are listed under `!` too — they may be the same privilege under another name. `plan` compares them with the environment.

## Merging and kept entries

- The same role in several solutions must be described identically, or the run stops with an error.
- A privilege only in a role file is kept and listed (`apply` would revoke a privilege the file leaves out). A privilege declared `none` is kept as written.
- A role only in the model: the file is left as is (`compare` lists it as `-`).
- Order: existing tables, verbs and privileges keep their order; new ones come after (tables and privileges by name, verbs in format order).
