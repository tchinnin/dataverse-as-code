# Plan / apply semantics

`dv_data.py` works like Terraform, one stage at a time. `plan` compares the model of a stage (base + overlays) with that stage's environment and saves the result. `apply` runs exactly that result.

## Plan output

```
Dataverse data plan
  env      : https://contoso.crm4.dynamics.com (/repo/.env.prod)
  stage    : prod — organization Contoso (0000…)
  az user  : admin@contoso.com (tenant 0000…)

  ~ activate    prfx_parameter[prfx_code="MAX_RETRY"]
                statecode: 1 → 0
  + create      prfx_region[prfx_code="AS"]
                prfx_code: "AS"
                prfx_name: "Asia"
  ~ update      prfx_parameter[prfx_code="SUPPORT_EMAIL"]
                prfx_value: "support-test@contoso.com" → "support@contoso.com"
                ownerid: Sales Team → team[name="Config Admins"]
  - deactivate  prfx_parameter[prfx_code="LEGACY_MODE"]
  - delete      prfx_region[prfx_code="OLD"]
                cascade restrict: related prfx_city rows (prfx_regionid)

Bypassed business logic — will NOT run during apply (the user must consent):
  prfx_parameter  bypass: sync
    step      sync  Update   pre-operation   Contoso.Plugins.ValidateParameter
  Plug-ins and workflows from solutions published by Microsoft always run. Power Automate flows run unless 'flows' is bypassed.

Summary: 1 to create, 1 to update, 1 to activate, 1 to deactivate, 1 to delete, 0 blocked.
Stage 'prod' is not dev: apply needs --confirm-url with the URL typed by the user, and --confirm-deletes 1 typed by the user.
```

- Rows are shown as `table[key=value]`. References are shown as `table[col=value]`, with `(created by this plan)` when the referenced row is created by the same apply.
- `⚠` lines are warnings: a row outside `delete_filter`, or a privilege that couldn't be checked. They don't block.
- `--json` prints the same plan as JSON, with a `plan_file` key added, for agents to read.
- The plan file goes to `<repo-root>/.dv-data/<stage>/plan.json` (or `--out`).

Exit code: `0` no change, `2` changes pending, `1` error or blocked items.

## Actions

| Action | When | Web API |
|---|---|---|
| `+ create` | A row of the model has no server row with its key | `POST <set>` with the row's columns; then the lookups to rows created by this plan |
| `~ update` | A listed column differs | `PATCH <set>(<id>)` with the changed columns only (`If-Match: *`); `DELETE …/<nav>/$ref` to clear a lookup |
| `~ activate` | `deletion: deactivate` and a row of the model is inactive | `PATCH` `statecode`/`statuscode` to the active state |
| `- deactivate` | `deletion: deactivate`, an active row inside `delete_filter`, not in the model | `PATCH` `statecode`/`statuscode` to the inactive state's default status |
| `- delete` | `deletion: delete`, a row inside `delete_filter`, not in the model | `DELETE <set>(<id>)` |

Values compare by type: text ignores `null` vs empty, numbers compare numerically, multi-select choices ignore order, date-only values compare on the date, date-times compare in UTC.

## Apply order

1. Activations, then creates and updates, table by table, referenced tables first.
2. Lookups to rows created in step 1.
3. Deactivations.
4. Deletes, tables that reference others first.

Apply stops at the first failure. Earlier writes stay. Fix the cause, re-plan and apply the new plan.

## Blocked items

A blocked item is never run. The plan exits with `1`, and `apply` refuses any plan that contains one.

| Cause | What to do |
|---|---|
| Table not found | Create it (dvac-metadata or `dataverse:dv-metadata`), or fix `table` |
| No alternate key with exactly the `key` columns | Create the key, or change `key` |
| Alternate key not `Active` | Wait for the index job (`Pending`, `InProgress`); `Failed` usually means duplicate values on the server — fix the data, then reactivate the key |
| Unsupported key column type (lookup…) | Use another key |
| Column not found, unsupported type, not valid for create/update | Fix the column name or remove it from the rows |
| Value of the wrong type (`"3"` for a whole number…) | Fix the value — see [format.md](format.md#rows) |
| Reference target not found, or `match` finding zero or several rows | Fix the reference, or add the referenced row to the model |
| Lookup cannot reference that table | Use one of the lookup's targets |
| `ownerid` cleared, or referencing another table than `systemuser`/`team` | Set a user or a team |
| `deletion: deactivate` on a table without `statecode` | Use `none` or `delete` |
| `delete_filter` rejected by Dataverse | Fix the OData syntax or the column names |
| `bypass: sync/async` without `prvBypassCustomBusinessLogic` | An admin grants the privilege, or remove `sync`/`async` |

## Stale plan

`apply` recomputes the plan and compares its stage, environment URL, organization id, actions, blocked items and bypass list with the saved file. If anything differs (the model, a server row, a registered step, the target), it aborts. Run `plan` again, have the user confirm again, and apply the new file.

## Confirmation outside dev

When the stage is not `dev`, `apply` also requires:

- `--confirm-url <url>` equal to the environment URL of the plan (case and trailing slash ignored);
- `--confirm-deletes <n>` equal to the number of `- delete` actions, when there is at least one.

Both are typed by the user. An agent shows the URL so the user can copy it, and passes exactly what the user typed. In `dev`, deletes still need a double confirmation in the conversation, but no typed value.

## Post-apply check

After a successful apply, the tool recomputes the plan. It must be empty. If not, it prints the remaining differences and exits with `1` — usually a plug-in or a flow changed a value after the write, or the value is normalised by Dataverse (trimmed text, rounded decimal). Align the model or the logic, then re-plan.
