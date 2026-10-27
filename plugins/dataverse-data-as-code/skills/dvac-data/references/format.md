# `*.dv.data.yaml` format

JSON Schema: [`schemas/data.schema.json`](schemas/data.schema.json). Samples: [`samples/`](samples/) (names follow one example scheme — use your own).

## Files and stages

```
dataverse/data/
├── prfx_region.dv.data.yaml          # base — every stage
├── prfx_parameter.dv.data.yaml       # base — every stage
└── prod/
    └── prfx_parameter.dv.data.yaml   # overlay — merged over the base for --stage prod
```

- One file per table. The file name is the table's **logical name** plus `.dv.data.yaml`, and equals `table`.
- **Base files** sit directly in `dataverse/data/` and apply to every stage.
- **Overlays** sit in `dataverse/data/<stage>/`. `plan --stage <stage>` merges each overlay over its base file:
  - a row with the same key replaces the base values of the columns it lists, and keeps the others;
  - a row with a new key is added for that stage only;
  - `deletion`, `delete_filter` and `bypass` replace the base values when set.
- An overlay without a base file is a table managed in that stage only; it declares `key`.
- A stage without a folder gets the base files unchanged. Stage names are lowercase (`dev`, `uat`, `prod`, `test-2`…).

## Table attributes

| Attribute | Required | Meaning |
|---|---|---|
| `table` | yes | Logical name of the table |
| `key` | in a base file | Column, or list of columns, of an **Active alternate key** of the table. It identifies each row in every environment. An overlay inherits it, and may only repeat the same value. |
| `deletion` | no | `none` (default), `deactivate` or `delete` — see [Deletion](#deletion) |
| `delete_filter` | when `deletion` is not `none` | OData `$filter` of the rows this file owns, or `all` |
| `bypass` | no | List of `sync`, `async`, `flows` — see [Bypass](#bypass) |
| `rows` | no | The rows, as maps of column logical name → value |

## Rows

```yaml
rows:
  - prfx_code: MAX_RETRY                  # key column — required in every row
    prfx_value: "3"                       # text: quote numbers
    prfx_origin: 100000000                # choice: the option value
    prfx_tags: [100000000, 100000002]     # multi-select choice: list of option values
    prfx_enabled: true                    # yes/no
    prfx_since: 2024-01-31                # date (or 2024-01-31T08:00:00Z for a UTC date-time)
    prfx_rate: 1.5                        # decimal, float, currency
    prfx_regionid: {ref: prfx_region, key: {prfx_code: EU}}   # lookup — see References
    prfx_note: null                       # clears the column
```

- **Only the columns a row lists are managed.** Other columns are never read for the diff and never written. `null` clears a column.
- Column names are logical names (lowercase).
- Every row sets every key column with a plain value. Key values are unique within the file; Dataverse compares text keys without case, and so does the tool.
- **No GUIDs.** A row never carries the table's primary id column. Rows are found by key, and the GUIDs Dataverse generates stay on the server.
- **No `statecode`.** The row state is managed by `deletion: deactivate`. `statuscode` may be set to a status of the active state.
- Supported column types: text, multiline text, whole number, big integer, decimal, float, currency, yes/no, date and time, choice, multi-select choice, status reason, and lookups (lookup, customer, owner). Other types (file, image, calculated…) are blocked at plan.
- Key columns may be text, whole number, decimal, choice or date columns. Lookup columns in a key are not supported.

## References

A lookup value names a row of another table, never its GUID:

| Form | Use | How it is resolved |
|---|---|---|
| `{ref: <table>, key: {<col>: <value>, …}}` | Any table with an Active alternate key on exactly these columns | At plan, by alternate key. If the row doesn't exist yet but this plan creates it (it is in the model), the lookup is set right after the creates. |
| `{ref: <table>, match: {<col>: <value>, …}}` | Tables without a suitable alternate key, e.g. `systemuser` (`domainname`, `azureactivedirectoryobjectid`), `team` (`name`), `businessunit` (`name`), `transactioncurrency` (`isocurrencycode`) | At plan, with a query that must return **exactly one** row; otherwise the row is blocked |

- `ownerid` takes a `systemuser` or `team` reference. Changing it reassigns the row. It can't be cleared.
- Owners usually differ per stage: set `ownerid` in the stage overlays.
- References between rows of the model need no ordering: referenced tables are written first, and lookups to rows created by the same plan are set after every create.

## Deletion

`deletion` decides what happens to rows that are **inside `delete_filter`** and **not in the files** (base + overlay) for the stage:

| `deletion` | Effect |
|---|---|
| `none` (default) | Nothing. Rows not in the files are never read. |
| `deactivate` | Active rows are deactivated (`statecode` → inactive, default status). Rows in the files that are inactive are reactivated. Needs a table with `statecode`. |
| `delete` | Rows are deleted. The plan lists each one with the custom relationships whose delete behavior reaches other rows (cascade, remove link, restrict). |

`delete_filter` is required as soon as `deletion` is not `none`, so a table is never emptied by accident:

```yaml
deletion: delete
delete_filter: "prfx_origin eq 100000000"   # only the rows this file owns
# delete_filter: all                       # every row of the table
```

The filter uses OData `$filter` syntax on the table's columns. A row of the file that the filter does not select is still created or updated, but removing it from the file later will not deactivate or delete it: the plan warns about such rows. Setting the filter column in the row (above: `prfx_origin: 100000000`) brings it inside the filter.

## Bypass

```yaml
bypass: [sync, async, flows]
```

| Value | Skips | Sent as |
|---|---|---|
| `sync` | custom synchronous plug-in steps and real-time workflows | `MSCRM.BypassBusinessLogicExecution: CustomSync` |
| `async` | custom asynchronous plug-in steps and background workflows | `MSCRM.BypassBusinessLogicExecution: CustomAsync` |
| `flows` | Power Automate flows triggered by Dataverse | `MSCRM.SuppressCallbackRegistrationExpanderJob: true` |

- `sync` and `async` need the `prvBypassCustomBusinessLogic` privilege (System Administrator by default). The plan blocks the table when the `az` user lacks it.
- Logic from solutions published by Microsoft always runs. `async` does not skip flows: add `flows`.
- The plan lists every enabled custom step, classic workflow and flow trigger on the table that will not run, so the user can consent. The official reference is the Dataverse documentation on bypassing custom business logic and Power Automate flows.
