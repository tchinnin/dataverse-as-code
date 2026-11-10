# Plan / apply semantics

`dv_security.py` works like Terraform without `destroy`. `plan` compares the model with the dev environment and saves the result. `apply` runs exactly that result.

## Plan output

```
Dataverse security plan
  env      : https://contoso-dev.crm4.dynamics.com (/repo/.env.dev)
  solution : prfx_crm_Core (unmanaged)
  az user  : dev@contoso.com (tenant 0000…)

  + create  role       Order Manager  2 privilege(s)
              description: "Manages every order of the organization."
              prvBulkDelete: organization  (prvBulkDelete)
              prvReadprfx_crm_order: organization  (prfx_crm_order.read)
  ~ update  role       Order Clerk
              description: "" → "Creates and follows up orders."
  + grant   privilege  Order Clerk: prvExportToExcel  organization  (prvExportToExcel)
  ~ depth   privilege  Order Clerk: prvWriteprfx_crm_order  organization → user  (prfx_crm_order.write)
  - revoke  privilege  Order Clerk: prvDeleteAccount  (was organization)

Blocked — never applied; align the model with the server or change it manually:
  ! privilege Order Clerk: account.assign
              prvAssignAccount does not accept level 'organization' (allowed: …)

Unmanaged — roles of the solution that the model doesn't describe (reported only, never deleted):
  ? role      Auditor

Summary: 1 role(s) to create, 1 to update, 1 grant(s), 1 level change(s), 1 revoke(s), 1 blocked, 1 unmanaged.
1 revoke(s) need a double confirmation from the user.
```

`--json` prints the same plan as JSON with a `plan_file` key. The `double_confirmation` list names every revoke.

## Action kinds

| Plan | What `apply` does |
|---|---|
| `+ create role` | `POST roles` in the root business unit (with the `MSCRM.SolutionUniqueName` header), `AddSolutionComponent` (type 20), then `ReplacePrivilegesRole` with the role's complete privilege set |
| `~ update role` | `PATCH roles(<id>)` for `description` and `member_inheritance`; `AddSolutionComponent` when the role is missing from the solution |
| `+ grant` / `~ depth` | `AddPrivilegesRole` — one call per role for all its grants and level changes; it sets the level of a privilege the role already holds |
| `- revoke` | `RemovePrivilegeRole` — **double confirmation** |

## Blocked changes

A blocked change is never run. The plan exits with code 1, and `apply` refuses any plan that contains one.

| Change | Why | What to do |
|---|---|---|
| Table not found | No table with that logical name in dev | Fix the name, or create the table first (`dvac-metadata`) |
| Verb missing on the table | Organization-owned tables have no Assign/Share, some system tables have fewer privileges | Remove the verb |
| Privilege not found | No privilege with that name | Fix the name |
| Level not accepted | The privilege's `canbe*` flags don't allow it | Use one of the levels listed in the message |
| Same privilege declared twice | Two entries resolve to one privilege | Keep one |
| Managed or non-customizable role | The role comes from another solution | Leave it out of the model, or create your own role |
| Several roles with the same name in the root business unit | The name no longer identifies one role | Rename the duplicates manually |

## Unmanaged

Roles that are components of the solution but that no role file describes. The tool only reports them. Add them to the model, or remove them manually after a deliberate decision.

## Stale plan

`apply` recomputes the plan and compares its environment, solution, actions and blocked items with the saved file. If anything differs, it aborts. Run `plan` again, have the user confirm again, and apply the new file.

## Apply behavior

- Runs the actions in order: role creations → role updates → grants and level changes → revokes.
- Stops at the **first failure**. Fix the cause, re-plan, and apply again; the next plan contains only what's left.
- Retries transient errors (HTTP 429, lock contention). Set `DV_SECURITY_DEBUG=1` to print every Web API call with its duration.
- After a successful run it computes the plan again with `Consistency: Strong` and expects it to be empty.

## Exit codes

| Code | `plan` | `apply` / `validate` / `doctor` |
|---|---|---|
| 0 | no changes | success |
| 1 | error, guard refusal, or blocked changes | failure / refusal |
| 2 | changes pending | — |

## Common Dataverse errors during apply

These complement the official skill `dataverse:dv-security`.

| Symptom | Likely cause | Fix |
|---|---|---|
| Privilege error on `AddPrivilegesRole` / `ReplacePrivilegesRole` | Dataverse doesn't let a user grant a privilege above the level they hold themselves | Apply with an account that holds System Administrator (or the same privileges) |
| `REFUSED: … MANAGED` | The target is not the dev environment | Check the dev environment inputs; never change a managed solution |
| `REFUSED: solution … not found` | The solution hasn't been created yet | Create it with `dataverse:dv-solution` |
| `az … failed` | No `az` session, or the wrong tenant | `az login --tenant <TENANT_ID>` |
| Post-apply check lists a `~ depth` | The level of a held privilege was not changed in place | Report it upstream; meanwhile revoke then grant in two plans |
