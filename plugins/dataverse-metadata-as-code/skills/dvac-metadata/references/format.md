# `.dv.metadata.yaml` format reference

The JSON Schemas in [`schemas/`](schemas/) are authoritative. This page explains them. Annotated examples are in [`samples/`](samples/).

Every file starts with the `yaml-language-server` directive so VS Code validates it as you type:

```yaml
# yaml-language-server: $schema=<relative path to the skill>/references/schemas/table.schema.json
```

## Files

| File | Schema | Content |
|---|---|---|
| `_meta.dv.metadata.yaml` | `meta.schema.json` | `version`, `publisher.{prefix, scope?, solution}`, `locale`, `languages?` |
| `choices.dv.metadata.yaml` | `choices.schema.json` | `choices:` map of global Choices |
| `tables/<key>.dv.metadata.yaml` | `table.schema.json` | One table, with its columns and alternate keys |

The tool finds table files with a glob over `tables/*.dv.metadata.yaml`. There is no manifest, and the tool works out the dependency order itself: Choices → tables → columns → lookups → formula columns → keys.

## Names

Every table, column, global Choice and alternate key name starts with the publisher prefix (`{prefix}_…`) — a Dataverse rule for custom components. The rest of the name is yours: the format and the validator impose no naming convention. Apply your team's best practices; the samples use one possible scheme (`prfx_crm_Order`, `prfx_crm_choice_OrderStatus`, `prfx_CustomerId`).

YAML keys are **Schema Names**, so their casing is kept. The tool compares names case-insensitively, the way Dataverse logical names work. References such as `choice:`, `target:` and key `columns:` may be written in either case.

## `_meta`

| Key | Required | Notes |
|---|---|---|
| `version` | yes | `"1.0"` |
| `publisher.prefix` | yes | 2–8 lowercase alphanumeric characters, permanent |
| `publisher.scope` | no | Schema-name prefix (default `{prefix}_`) that limits the `? unmanaged` report to components named `{scope}…`. Never enforced on model names. |
| `publisher.solution` | yes | Unique name of the **unmanaged** dev solution |
| `locale` | yes | `en`, `fr`, `es`, `de`, `it`, `pt` or `nl` — the base language (environment's primary language). A plain-string label is written in it. |
| `languages` | no | LCIDs the labels are translated into, e.g. `[1033, 1036]` — see [Localization](#localization) |

## Localization

Every label can be a string (base language) or a map keyed by **LCID**:

```yaml
# _meta.dv.metadata.yaml
locale: en
languages: [1033, 1036]

# tables/prfx_crm_Customer.dv.metadata.yaml
label: {1033: Customer, 1036: Client}
columns:
  prfx_Name:
    label:
      1033: Name
      1036: Nom
    description: {1033: Customer legal name., 1036: Raison sociale du client.}
```

Localizable fields: table `label`, `label_plural`, `description`; column `label`, `description`, `true_label`, `false_label`; key `label`; Choice `label`, `description` and option `label`.

- A map must contain the base language (`locale`) — Dataverse needs it to create the component.
- A language of `languages` that a label doesn't translate is a **warning**, not an error. Without `languages`, no translation is expected.
- The plan compares only the languages the model declares; `apply` writes them with `MergeLabels`, so other translations on the server are kept.
- Every language used must be **provisioned** in the environment: `doctor` checks it and `plan` blocks otherwise. The same languages must also be enabled in test and production before the solution is imported.

## Global Choice

```yaml
choices:
  prfx_crm_choice_OrderStatus:
    label: Order Status Choice
    description: Lifecycle of a customer order.
    options:
      - {value: 100000000, label: Draft, color: "#9EA6B3"}
      - {value: 100000001, label: Confirmed}
```

- Custom option values start at `100000000`. Values must be unique.
- `color` is applied **when the Choice is created** only. After that the API cannot change an option's color, so change it in the maker portal.
- An option you remove from the YAML is reported as `? unmanaged`. It is never deleted.

## Table

| Key | Default | Managed after creation |
|---|---|---|
| `key` | — (required) | — (identity) |
| `label` | — (required) | yes |
| `label_plural` | `label + "s"` at creation | only if declared |
| `description` | `label` at creation | only if declared |
| `ownership` | `user` | **blocked** if it differs |
| `table_type` | `standard` (`activity`, `elastic`) | **blocked** if it differs |
| `auditing` | `false` | yes |
| `columns` | — (required, exactly one `primary: true`) | see below |
| `keys` | none | create only; a change to a key's columns is **blocked** |

## Columns

Common attributes:

| Attribute | Applies to | Managed when |
|---|---|---|
| `label`, `type` | all | always (`type` change → **blocked**) |
| `required` / `required_level` | all | always; default `none` |
| `description`, `auditing`, `searchable` | all | only if declared |
| `primary` | text, autonumber | exactly one per table; it can't be changed later |

Type-specific attributes:

| `type` | Attributes | Notes |
|---|---|---|
| `text`, `email`, `url`, `phone` | `max_length` | text ≤ 4000 |
| `multiline` | `max_length` | |
| `autonumber` | `format` (required), `max_length` | e.g. `"ORD-{SEQNUM:6}"` |
| `integer` | `min`, `max` | Int32 — use decimal or text for IDs that can exceed it |
| `decimal` | `min`, `max`, `precision` | default type for amounts |
| `money` | `min`, `max`, `precision_source` (`currency` default \| `pricing` \| `fixed`), `precision` (0–4, only with `fixed`) | |
| `boolean` | `default`, `true_label`, `false_label` | labels default to Yes/No in each language |
| `date` | — | date only |
| `datetime` | `behavior`: `userlocal` \| `timezoneindependent`; `display`: `datetime` (default) \| `date` | `display` is the form format only, independent of storage. `userlocal` → another behavior is allowed once; anything else is **blocked** |
| `choice` | `choice` (required), `default` | `default` must be an option value |
| `multichoice` | `choice` (required) | Dataverse doesn't support a default value |
| `lookup` | `target` (required), `cascade` (required) — see [Relationship behavior](#relationship-behavior) | creates the N:1 relationship `{table}_{ColumnName}`; `target` change → **blocked** |
| `file` | `max_size_kb` (default 32768) | size can't be changed later |
| `image` | `max_size_kb`, `primary_image: true` | at most one primary image per table; it can be moved to another column but never unset |
| `formula` | `formula` (required), `result_type` (required), then the attributes of the result type | Power Fx formula column — see [Formula columns](#formula-columns) |

## Relationship behavior

`cascade` mirrors the maker portal's relationship behavior. It takes either a preset or a custom mapping per action.

```yaml
prfx_CustomerId:
  type: lookup
  target: prfx_crm_Customer
  cascade: restrict              # preset

prfx_ProjectId:
  type: lookup
  target: prfx_crm_Project
  cascade:                       # custom — omitted actions are none
    delete: removelink
    share: active
    unshare: active
    reparent: userowned
```

| Preset | Portal | delete | assign, share, unshare, reparent |
|---|---|---|---|
| `parental` | Parental | `all` | `all` |
| `referential` | Referential, Remove Link | `removelink` | `none` |
| `restrict` | Referential, Restrict Delete | `restrict` | `none` |

| Action | Values (portal label) |
|---|---|
| `delete` (required in a mapping) | `all` (Cascade All), `removelink` (Remove Link), `restrict` (Restrict) |
| `assign`, `share`, `unshare`, `reparent` | `none` (Cascade None), `all` (Cascade All), `active` (Cascade Active), `userowned` (Cascade User-owned) |

- Prefer a preset; use a mapping only when a preset doesn't fit. A mapping identical to a preset is equivalent — the plan shows no difference.
- Merge and RollupView are created as Cascade None and never changed by `apply`.
- `validate` rejects a table with more than one parental relationship (any action `all`/`active`/`userowned`, or `delete: all`) and warns on `delete: all` (needs explicit confirmation) and on `active`/`userowned` with `ownership: org`.
- `on_delete` is no longer accepted: `restrict` → `cascade: restrict`, `removelink` → `cascade: referential`, `cascade` → `cascade: {delete: all}`.

## Formula columns

A formula column (Dataverse *fx Formula*, `SourceType` 3) holds a Power Fx formula. Dataverse computes its value.

```yaml
prfx_AmountWithTax:
  label: Amount Incl. Tax
  type: formula
  result_type: decimal        # decimal | integer | text | boolean | datetime | date | choice
  formula: "If(prfx_amount > 0, prfx_amount * 1.2)"
  precision: 2                # attributes of the result type: decimal → min, max, precision
  description: Amount with 20% tax; empty when the amount is 0 or missing.
```

- `result_type` is the type of the value the formula returns. The other attributes follow the rules of that type (`max_length` for text, `min`/`max`/`precision` for decimal, `behavior`/`display` for datetime, `choice` (required) for choice). Money is not a formula result type in Dataverse.
- `primary`, `default`, `required` and `required_level` are rejected (along with `format`, `target`, `cascade`, `max_size_kb`, `primary_image`, `true_label`, `false_label`): Dataverse computes the value.
- The formula references columns of the **same table** by their **logical name** (lowercase), e.g. `prfx_amount`. `validate` checks, on a best-effort basis, that every `{prefix}_…` name it reads is a column of the table. Strings, `'quoted names'` and names after a `.` (related-table columns reached through a lookup) are not checked. A formula can't read its own column, and formulas can't depend on each other in a cycle.
- `apply` creates formula columns after every other column and lookup of the run, and a formula after the formulas it reads.
- The plan compares formulas with whitespace and line breaks collapsed (outside string literals), so a multi-line YAML block (`formula: |`) and a single-line one are the same.
- A change of `formula`, `label`, `description` or a result-type attribute is a `~ update`. A change of `result_type`, or between a simple column and a formula column, is **blocked**.

Calculated and rollup columns, N:N relationships, forms and views are **outside the model**. Handle them with `dataverse:dv-metadata`.

## Alternate keys

```yaml
keys:
  - schema_name: prfx_crm_customer_customernumber_key
    columns: [prfx_customernumber]
    label: Customer Number Key
```

Dataverse builds the index asynchronously after creation.
