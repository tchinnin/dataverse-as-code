# Plan / apply semantics

`dv_metadata.py` works like Terraform without `destroy`. `plan` compares the model with the dev environment and saves the result. `apply` runs exactly that result.

## Plan output

```
Dataverse metadata plan
  env      : https://contoso-dev.crm4.dynamics.com (/repo/.env.dev — or command-line flags / environment variables)
  solution : prfx_crm_Core (unmanaged)
  az user  : dev@contoso.com (tenant 0000…)

  + create  choice  prfx_crm_choice_OrderStatus  4 options
  + create  table   prfx_crm_Order  primary prfx_OrderNumber: autonumber
  + create  lookup  prfx_crm_Order.prfx_CustomerId  → prfx_crm_Customer (cascade restrict)
  ~ update  column  prfx_crm_Customer.prfx_Name
              max_length: 100 → 160
  ~ update  option  prfx_crm_choice_CustomerSegment:100000002
              label: "VIP" → "Key Account"

Blocked — never applied; align the model with the server or change it manually:
  ! column  prfx_crm_Customer.prfx_Score  type: "integer" → "decimal"
              column type cannot be changed in place (needs a new column + data migration)

Unmanaged — on the server but not in the model (reported only, never deleted):
  ? column  prfx_crm_customer.prfx_legacycode  StringType
  ? option  prfx_crm_choice_OrderStatus:100000009  Archived

Summary: 3 to create, 2 to update, 1 blocked, 2 unmanaged.
```

`--json` prints the same plan as JSON, with a `plan_file` key added, for agents to read.

## Action kinds

| Kind | `+ create` | `~ update` (applied in place) |
|---|---|---|
| choice | new global Choice, with its options and colors, created directly in the solution (`MSCRM.SolutionUniqueName` header) | `label`, `description`; added to the solution if missing |
| option | new value (`InsertOptionValue`) | `label` (`UpdateOptionValue`, labels in other languages are kept) |
| table | new table + primary column, created directly in the solution (`MSCRM.SolutionUniqueName` header) | `label`, `label_plural`, `description`, `auditing`; added to the solution if missing |
| column | new column | `label`, `description`, `required_level`, `auditing`, `searchable`, `max_length`, `min`, `max`, `precision`, `precision_source`, `format`, `default`, `display`, `true_label` / `false_label` (`UpdateOptionValue`), `primary_image` (only to `true`), `behavior` (once, from `userlocal`) |
| lookup | new N:1 relationship + lookup column | `cascade.<action>` for delete, assign, share, unshare and reparent — only the changed actions are written; Merge and RollupView are kept as on the server. Label and the other fields go through `column` |
| key | new alternate key | — |

Labels are written with `MSCRM.MergeLabels: true`, so translations in other languages are kept. A label change lists only the languages that differ (e.g. `label: 1036="Ordre" → 1036="Commande"`). Only the components the run touched are published (`PublishXml`).

## Blocked changes

A blocked change is never run. The plan exits with code 1, and `apply` refuses any plan that contains one.

| Change | Why | What to do |
|---|---|---|
| A label in a language not provisioned in the environment | Dataverse rejects labels in a language it doesn't have | Ask an admin to enable the language, or remove it from the model |
| Column `type` | Dataverse cannot convert a column in place | Revert the YAML, or add a new column, migrate the data, and remove the old one manually |
| Choice bound to a column | The binding is fixed at creation | Same as above |
| Lookup `target` | The relationship is fixed at creation | New lookup column |
| `max_size_kb` (file/image) | Fixed at creation | Revert the YAML |
| `behavior` other than a first change from `userlocal` | Dataverse allows a single, one-way change | Revert the YAML |
| Table `ownership` / `table_type` | Fixed at creation | New table + migration |
| Primary column | Fixed at creation | Revert the YAML |
| Alternate key `columns` | Keys are immutable | Add a new key; remove the old one manually |

## Unmanaged

Scope is the components whose names start with `publisher.scope` (default `{prefix}_`):

- global Choices and custom tables in that scope
- `{prefix}_` custom columns on modeled tables, excluding helper columns such as `…name` and `…_base`
- alternate keys on modeled tables
- option values of modeled Choices

The tool only reports these, so the decision stays a human one. Either add the item to the model, or delete it manually after a deliberate decision.

## Stale plan

`apply` recomputes the plan and compares its environment, solution, actions and blocked items with the saved file. If anything differs, it aborts. Run `plan` again, have the user confirm again, and apply the new file. A change in the unmanaged list alone does not make a plan stale.

## Apply behavior

- Runs the actions in phase order: Choices → options → tables → columns → lookups → keys.
- Stops at the **first failure**, then publishes whatever succeeded. Fix the cause, re-plan, and apply again. Because the plan is a diff, the next plan contains only what's left to do.
- Retries transient errors automatically: lock contention `0x80072560`, metadata cache `0x80040216` / `0x80060891`, and HTTP 429. A 404 is never retried: "not found" is an answer. Set `DV_METADATA_DEBUG=1` to print every Web API call with its duration.
- Every request made during `apply`, and the post-apply check, sends `Consistency: Strong`, so reads see the changes just written rather than the metadata cache.
- After a successful run it computes the plan again and expects it to be empty. Because that check bypasses the cache, a non-empty result is a **real** difference, not a delay. Investigate it; don't wait and retry.
- Components are associated with the solution through the `MSCRM.SolutionUniqueName` header, the documented name. Dataverse silently ignores an unknown header such as `MSCRM.SolutionName`, so a component created with it lands **outside** the solution. This also applies to global Choices: with the correct header they are in the solution as soon as they're created, so no `AddSolutionComponent` call is needed. `AddSolutionComponent` is used only for an existing component that is missing from the solution.
- Dataverse stores global Choice names in lowercase, and `Name='…'` lookups are case-sensitive, so the tool always queries the lowercase name.

## Exit codes

| Code | `plan` | `apply` / `validate` / `doctor` |
|---|---|---|
| 0 | no changes | success |
| 1 | error, guard refusal, or blocked changes | failure / refusal |
| 2 | changes pending | — |

## Common Dataverse errors during apply

These complement the error table in `dataverse:dv-metadata`.

| Symptom | Likely cause | Fix |
|---|---|---|
| `0x8004431a` publisher prefix | The model `prefix` doesn't match the solution publisher | Align `publisher.prefix` with the solution's publisher |
| `REFUSED: … MANAGED` | The target is not the dev environment | Check the dev environment inputs (flags, environment variables or `.env.dev`); never change a managed solution |
| `REFUSED: solution … not found` | The solution hasn't been created yet | Create it with `dataverse:dv-solution` |
| `az … failed` | No `az` session, or the wrong tenant | `az login --tenant <TENANT_ID>` |
| Lookup creation fails on the target | The target table doesn't exist yet, or it's a system table you don't have access to | Check `target`; add the target table to the model |
