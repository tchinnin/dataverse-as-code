# Model format — `*.dv.security.yaml`

The JSON Schemas in [`schemas/`](schemas/) are the formal definition. This page explains each attribute. Annotated samples are in [`samples/`](samples/).

## Files

```
dataverse/security/
├── _meta.dv.security.yaml          # required
└── roles/<file>.dv.security.yaml   # one role per file
```

- `_meta.dv.security.yaml` names the format version and the solution.
- Each file under `roles/` describes **one** security role. The file name is free (role names contain spaces); the role's identity is its `name`.
- A `*.dv.security.yaml` file elsewhere in the model folder is an error, so a misplaced role is never silently ignored.

## `_meta.dv.security.yaml`

| Key | Required | Meaning |
|---|---|---|
| `version` | yes | Format version, `"1.0"` |
| `solution` | yes | Unique name of the unmanaged solution in dev. Every role of the model is added to it (component type 20). |

## Role file

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Role name in the root business unit. Unique across the model, compared without regard to case. No leading or trailing spaces. |
| `description` | no | Role description. Left out = not managed (the server value is kept). |
| `member_inheritance` | no | Member's privilege inheritance (`isinherited`): `team_only` (0) or `direct_and_team` (1). Left out = not managed. |
| `tables` | no | Table privileges, keyed by table **logical name** (lowercase). |
| `privileges` | no | Any other privilege, keyed by its name (`prv…`): miscellaneous and privacy-related privileges, or a table privilege you prefer to name directly. |

### Table privileges

```yaml
tables:
  prfx_crm_order:
    create: user
    read: business_unit
    write: user
    delete: none          # same as leaving it out
    append: user
    append_to: user
    assign: user
    share: user
```

Verbs map to the table's privileges (`PrivilegeType` in the table metadata): `create` → Create, `read` → Read, `write` → Write, `delete` → Delete, `append` → Append, `append_to` → AppendTo, `assign` → Assign, `share` → Share. Organization-owned tables have no `assign` or `share`.

### Access levels

| Model | Role editor | API (`PrivilegeDepth`) |
|---|---|---|
| `none` | None | (privilege not held) |
| `user` | User | `Basic` |
| `business_unit` | Business Unit | `Local` |
| `parent_child` | Parent: Child Business Units | `Deep` |
| `organization` | Organization | `Global` |

Each privilege accepts only some levels (its `canbebasic`, `canbelocal`, `canbedeep`, `canbeglobal` flags). Organization-owned tables and most miscellaneous privileges accept only `organization`. A level the privilege doesn't accept is blocked at plan time.

## A complete privilege set

A role file lists **every** privilege the role holds. Anything else the role holds in dev is planned as `- revoke`. Consequences:

- To bring an existing role under the model, run `dv_security.py adopt <model-dir> "<role name>"`: it writes the role file with every privilege the role holds (read-only on the server), so the next plan is empty. Then make your change.
- A new role is created with exactly the listed privileges (`ReplacePrivilegesRole`), so the default privileges Dataverse gives a new role are not kept unless listed.
- To open a model-driven app, users usually need privileges on system tables besides the project's own. The official skill `dataverse:dv-security` owns that list; write them in the role file like any other privilege.
- The same privilege may not be declared twice (for example `account.read` in `tables` and `prvReadAccount` in `privileges`): the plan blocks it.
- Record-filter privileges are not managed: the tool neither reads nor revokes them.

## Identity and renames

The tool finds a role by its `name` in the root business unit and never writes its GUID in the model. Renaming a role in the YAML therefore plans a **new** role; the old one shows up as `? unmanaged`. To rename, rename the role in dev first (the role editor), then update the YAML so the plan is empty.

Roles in child business units are copies Dataverse maintains from the root role; the tool only reads and writes the root role.
