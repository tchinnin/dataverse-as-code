# ERD reference

`dv_metadata_docs.py erd` turns the `dvac-metadata` model into a Mermaid [`erDiagram`](https://mermaid.js.org/syntax/entityRelationshipDiagram.html). GitHub, Azure DevOps and VS Code (with a Mermaid extension) render it in Markdown.

## Entities

One entity per table file, named by its `key` (Schema Name). Tables are sorted by key, case-insensitively.

## Attributes

Each line is `<type> <Schema Name> [keys] "<comment>"`:

| Line | Meaning |
|---|---|
| `guid <logicalname>id PK` | The table's primary key, created by Dataverse. Its comment is the table label. |
| `<type> <column>` | A column of the model, in model order; the primary (name) column comes first. `<type>` is the model `type`. |
| `fx_<result_type>` | A Power Fx formula column and the type of the value it returns. |
| `FK` | A lookup column. |
| `UK` | A column of an alternate key. |

The comment holds the column label, then `primary name`, `required` and `choice <global Choice>` when they apply. Double quotes in labels become single quotes (Mermaid cannot escape them).

## Relationships

One relationship per lookup column: `<target> <parent>--o{ <table> : "<column> (<cascade>)"`.

- `||` — the lookup is required: every row has exactly one parent.
- `|o` — the lookup is optional: zero or one parent.
- `<cascade>` is the preset (`parental`, `referential`, `restrict`) or the custom mapping, as in the plan.

A target outside the model (`account`, `systemuser`…) or outside `--tables` appears as an entity without columns. N:N relationships are outside the model, so they are not drawn.

## Languages

Labels are written in the model's base language (`locale` in `_meta`). With `--lang <LCID>`, a label translated into that language is used instead; an untranslated label falls back to the base language.

## CI check

```sh
python3 <skill>/scripts/dv_metadata_docs.py erd dataverse/metadata --out docs/dataverse/data-model.md --check
```

Exit code `0` — the file is up to date. Exit code `1` — the file is missing or differs, or the model is invalid. The path `docs/dataverse/data-model.md` is the suggested location; use the project's own when it has one. The check compares bytes, so use the same options as the command that wrote the file.
