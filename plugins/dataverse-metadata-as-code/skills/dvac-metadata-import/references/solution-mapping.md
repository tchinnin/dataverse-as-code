# Solution → model mapping

`dv_metadata_import.py` reads the SolutionPackager layout produced by `pac solution unpack`. This page lists what it reads and how each element becomes a `.dv.metadata.yaml` field. The format itself is described in the `dvac-metadata` skill (`references/format.md`).

## Files read

| File | Content used |
|---|---|
| `Other/Solution.xml` | `UniqueName`, `Publisher/CustomizationPrefix`, `languagecode` — only to create a missing `_meta` |
| `Other/Customizations.xml` | `<Languages>` — `languages` of a new `_meta` when there are several |
| `OptionSets/*.xml` | Global Choices (`IsGlobal` = 1, `OptionSetType` = picklist) |
| `Entities/<Table>/Entity.xml` | The table and its custom columns and alternate keys |
| `Entities/<Table>/Formulas/*.yaml` | Power Fx definitions of formula columns (`<column>: =<formula>`) |
| `Other/Relationships/*.xml` | Lookup targets and cascade behavior (one-to-many relationships) |

Only components named with the publisher prefix (`{prefix}_…`) are imported. Everything else in the solution (forms, views, apps, roles…) is ignored.

## Labels

Labels and descriptions are read for every language. With one language in the model they are plain strings; with several (`languages` in `_meta`, or an existing map) they are `{LCID: text}` maps restricted to the model's languages. Empty descriptions are left out.

## Tables

| XML | Field |
|---|---|
| `<Name>` / `entity Name` | `key` (file `tables/<key>.dv.metadata.yaml`) |
| `LocalizedNames` | `label` |
| `LocalizedCollectionNames` | `label_plural` — new tables only, when it is not `label + "s"` |
| `Descriptions` | `description` |
| `OwnershipTypeMask` (`UserOwned` / `OrgOwned`) | `ownership` — written when `org` or already declared |
| `IsActivity`, `TableType` | `table_type` — written when not `standard` or already declared |
| `IsAuditEnabled` | `auditing` — written when `true` or already declared |
| `EntityKeys/EntityKey` | `keys` (`Name`, `AttributeName` list, `displaynames`) |

## Columns

A column is imported when `IsCustomField` = 1 and its name has the publisher prefix. The key is `PhysicalName` (the Schema Name).

| `Type` (`Format`) | `type` | Fields |
|---|---|---|
| `nvarchar` (`text`, `email`, `url`, `phone`) | `text`, `email`, `url`, `phone` | `max_length` ← `MaxLength` |
| `nvarchar` with `AutoNumberFormat` | `autonumber` | `format`, `max_length` |
| `ntext` | `multiline` | `max_length` |
| `int` (`none`) | `integer` | `min`, `max` — left out when they are the Int32 bounds |
| `decimal` | `decimal` | `min`, `max`, `precision` ← `Accuracy` |
| `money` | `money` | `min`, `max`, `precision_source` ← `AccuracySource` (0 fixed, 1 pricing, 2 currency), `precision` when fixed |
| `bit` | `boolean` | `default` ← `AppDefaultValue`; `true_label`/`false_label` when they differ from Yes/No |
| `datetime` | `date` when `Behavior` = 2 (DateOnly), else `datetime` | `behavior` (1 userlocal, 3 timezoneindependent), `display: date` when `Format` = date |
| `picklist` / `multiselectpicklist` with `OptionSetName` | `choice` / `multichoice` | `choice`, `default` (choice, when not -1) |
| `lookup` | `lookup` | `target` and `cascade` from the relationship whose `ReferencingAttributeName` is the column |
| `file` / `image` | `file` / `image` | `max_size_kb` ← `MaxValue`; `primary_image` from `DisplayMask` |
| any type with `SourceType` = 3 | `formula` | `result_type` from the type, `formula` from `Formulas/` (leading `=` removed) |

Common fields: `label` ← `displaynames`, `description` ← `Descriptions`, `primary: true` when `DisplayMask` holds `PrimaryName`, `required: true` for `ApplicationRequired`/`SystemRequired`, `required_level: recommended` for `Recommended`.

`cascade` is written as a preset (`parental`, `referential`, `restrict`) when the five actions match one, else as the shortest mapping.

### Optional fields of existing columns

The format manages `auditing`, `searchable`, `max_length`, `min`, `max`, `precision`, `precision_source`, `default`, `true_label`, `false_label`, `behavior`, `display`, `max_size_kb` and `primary_image` only when the file declares them. A column already in the model keeps its choice: these fields are updated when declared and never added. A new column gets every value the solution holds. `description` is added whenever the solution has one.

## Global Choices

`Name` → key, `displaynames` → `label`, `Descriptions` → `description`, each `option`: `value`, `labels` → `label`, `Color` → `color`. An option's color can only be set at creation, so an existing color is kept when the solution has none.

## Skipped

| Component | Reason |
|---|---|
| Table without the publisher prefix (`Account`, `Contact`…) | The format describes custom tables only; their custom columns are skipped with them |
| Table without `EntityInfo` | The solution carries only its forms or views — listed when it has the publisher prefix |
| Local choice (inline `optionset`, no `OptionSetName`) | The format references global Choices only |
| `nvarchar` with format `textarea` (or another format without a model type) | No such type in the format |
| `int` with a format (`duration`, `timezone`, `language`…) | No such type in the format |
| Calculated (`SourceType` 1) and rollup (`SourceType` 2) columns | Outside the model |
| Formula column without a definition in `Formulas/`, or returning a type that is not a formula result type | Cannot be described |
| Lookup with no relationship, or several targets (polymorphic, customer) | The format has single-target lookups only |

When a skipped column is read by a formula, or a lookup targets a custom table of another solution, the validator rejects the result and nothing is written.

## Merging

- The same table in several solutions: columns are added; a column described differently is an error.
- The same Choice or relationship described differently by two solutions is an error.
- Only-in-the-model entries are kept: tables (file untouched), columns, keys, Choices and options.
- Existing order is kept; new columns and keys come after, in solution order.
