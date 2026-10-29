#!/usr/bin/env python3
"""
dv_metadata_import.py — align the Dataverse metadata model with unpacked solutions
==================================================================================

Reads one or more solutions unpacked in the repo (``pac solution unpack`` /
SolutionPackager layout: ``Other/Solution.xml``, ``Entities/*/Entity.xml``,
``OptionSets/*.xml``, ``Other/Relationships/*.xml``) and regenerates the
``*.dv.metadata.yaml`` model of the dvac-metadata skill from them::

    solution <unpacked-dir>... [--model <dir>] [--tables <a,b>] [--write]

Without ``--write`` nothing is written: the tool prints what would change —
``+`` new file, ``~`` changed file (unified diff), ``=`` unchanged — then the
model entries the solutions don't hold (kept) and the solution components the
format cannot describe (skipped). ``--write`` writes the new and changed files.

The model is never reduced: tables, columns, keys, Choices and options that are
only in the model are kept. A file whose content does not change is not
rewritten, so its comments survive; a rewritten file loses its comments. The
result is validated with the dvac-metadata validator before anything is written.

Offline: reads files only, never the environment.

Exit codes:
    0  nothing to change, or --write succeeded
    1  error, or the result would be an invalid model
    2  changes pending (run again with --write)

Do not edit this script per project — edit the model files.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
# The model loader and validator belong to the sibling skill of the same plugin.
sys.path.insert(0, str(SKILL_DIR.parent / "dvac-metadata" / "scripts"))
import dv_metadata as dvm  # noqa: E402

LCID_LOCALE = {v: k for k, v in dvm.LOCALE_LCID.items()}
OWNERSHIP = {"UserOwned": "user", "OrgOwned": "org"}
REQUIRED = {"none": "none", "recommended": "recommended", "required": "required",
            "applicationrequired": "required", "systemrequired": "required"}
BEHAVIOR = {"1": "userlocal", "2": "dateonly", "3": "timezoneindependent"}
PRECISION_SOURCE = {"0": "fixed", "1": "pricing", "2": "currency"}
STRING_FORMATS = {"text": "text", "email": "email", "url": "url", "phone": "phone"}
FORMULA_RESULT = {"decimal": "decimal", "int": "integer", "nvarchar": "text", "bit": "boolean",
                  "datetime": "datetime", "picklist": "choice"}
INT32 = (-2147483648, 2147483647)
# Column fields the format manages only when they are declared. A column already in the
# model keeps its own choice of them (description is added when the solution has one);
# a new column gets every value the solution holds.
OPTIONAL_COLUMN = ("auditing", "searchable", "max_length", "min", "max", "precision", "precision_source",
                   "default", "true_label", "false_label", "behavior", "display", "max_size_kb",
                   "primary_image")


class ImportError_(Exception):
    """A solution the tool cannot read consistently."""


# --------------------------------------------------------------------------- #
# Reading the unpacked solution
# --------------------------------------------------------------------------- #
def xml_root(path: Path):
    return ET.parse(path).getroot()


def texts(parent, tag):
    """{LCID: text} of the localized labels under ``parent`` (empty texts dropped)."""
    out = {}
    if parent is None:
        return out
    for e in parent.iter(tag):
        lcid, text = e.get("languagecode"), e.get("description")
        if lcid and text and text.strip():
            out[int(lcid)] = text
    return out


def _int(v):
    return int(v) if v not in (None, "") else None


def _num(v):
    if v in (None, ""):
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def read_solution(sol_dir: Path):
    """Raw content of one unpacked solution, in model vocabulary where it is direct."""
    sol_xml = sol_dir / "Other" / "Solution.xml"
    if not sol_xml.is_file():
        raise ImportError_(f"{sol_dir}: no Other/Solution.xml — not an unpacked solution")
    root = xml_root(sol_xml)
    manifest = root.find("SolutionManifest")
    sol = {"dir": sol_dir, "unique_name": manifest.findtext("UniqueName"),
           "prefix": (manifest.findtext("Publisher/CustomizationPrefix") or "").lower(),
           "lcid": _int(root.get("languagecode")) or 1033, "languages": [],
           "choices": {}, "tables": {}, "relationships": {}, "no_metadata": []}
    cust = sol_dir / "Other" / "Customizations.xml"
    if cust.is_file():
        sol["languages"] = [int(l.text) for l in xml_root(cust).iter("Language") if (l.text or "").strip()]

    for f in sorted((sol_dir / "OptionSets").glob("*.xml")) if (sol_dir / "OptionSets").is_dir() else []:
        os_ = xml_root(f)
        if os_.findtext("IsGlobal") != "1" or os_.findtext("OptionSetType") != "picklist":
            continue
        sol["choices"][os_.get("Name")] = {
            "label": texts(os_.find("displaynames"), "displayname"),
            "description": texts(os_.find("Descriptions"), "Description"),
            "options": [{"value": int(o.get("value")), "label": texts(o.find("labels"), "label"),
                         "color": o.get("Color") or None}
                        for o in os_.find("options") or []],
        }

    rel_dir = sol_dir / "Other" / "Relationships"
    for f in sorted(rel_dir.glob("*.xml")) if rel_dir.is_dir() else []:
        for r in xml_root(f).iter("EntityRelationship"):
            if r.findtext("EntityRelationshipType") != "OneToMany":
                continue
            key = ((r.findtext("ReferencingEntityName") or "").lower(),
                   (r.findtext("ReferencingAttributeName") or "").lower())
            sol["relationships"].setdefault(key, []).append({
                "name": r.get("Name"), "target": r.findtext("ReferencedEntityName") or "",
                "cascade": {a: dvm.CASCADE_VALUE_FROM_API.get(r.findtext(f"Cascade{a.capitalize()}"), "none")
                            for a in dvm.CASCADE_ACTIONS}})

    for f in sorted((sol_dir / "Entities").glob("*/Entity.xml")) if (sol_dir / "Entities").is_dir() else []:
        root = xml_root(f)
        ent = root.find("EntityInfo/entity")
        if ent is None:
            sol["no_metadata"].append((root.findtext("Name") or f.parent.name).strip())
            continue  # the table travels without metadata (forms, views only)
        formulas = {}
        fdir = f.parent / "Formulas"
        if fdir.is_dir():
            for ff in sorted(fdir.glob("*.yaml")):
                dvm._require_yaml()
                for k, v in (dvm.yaml.safe_load(ff.read_text(encoding="utf-8-sig")) or {}).items():
                    formulas[str(k).lower()] = str(v)
        sol["tables"][ent.get("Name") or (root.findtext("Name") or "").strip()] = {
            "file": f, "entity": ent, "formulas": formulas}
    return sol


# --------------------------------------------------------------------------- #
# Model building
# --------------------------------------------------------------------------- #
class Builder:
    """Turns solution content into model documents, keeping what the solution lacks."""

    def __init__(self, meta, existing_tables, existing_choices):
        self.meta = meta
        self.base = dvm.model_lcid(meta)
        self.languages = dvm.model_languages(meta)
        self.prefix, _scope = dvm.prefixes(meta)
        self.existing_tables = existing_tables      # lower key -> raw YAML dict
        self.existing_choices = existing_choices    # lower name -> raw YAML dict
        self.skipped = []                           # (where, reason)
        self.kept = []                              # (where, what)

    # -- labels ---------------------------------------------------------------
    def label(self, values, existing=None):
        """Model value of a localized text: a string, or an {LCID: text} map when the model
        declares several languages (or the existing value already is a map)."""
        if not values:
            return None
        if len(self.languages) > 1 or isinstance(existing, dict):
            out = {l: values[l] for l in self.languages if l in values}
            if self.base not in out:
                out = {self.base: values.get(self.base) or next(iter(values.values())), **out}
            return out
        return values.get(self.base) or next(iter(values.values()))

    def owned(self, name):
        return name.lower().startswith(self.prefix + "_")

    # -- Choices --------------------------------------------------------------
    def choice(self, name, spec):
        old = self.existing_choices.get(name.lower()) or {}
        doc = {"label": self.label(spec["label"], old.get("label")) or name}
        desc = self.label(spec["description"], old.get("description"))
        if desc:
            doc["description"] = desc
        old_opts = {o.get("value"): o for o in old.get("options") or []}
        options = []
        for o in spec["options"]:
            prev = old_opts.get(o["value"]) or {}
            opt = {"value": o["value"], "label": self.label(o["label"], prev.get("label")) or str(o["value"])}
            color = o["color"] or prev.get("color")  # colors are set at creation only
            if color:
                opt["color"] = color
            options.append(opt)
        seen = {o["value"] for o in options}
        for value, prev in old_opts.items():
            if value not in seen:
                options.append(prev)
                self.kept.append((f"choice {name}", f"option {value} (not in the solution)"))
        doc["options"] = options
        return doc

    # -- Tables ---------------------------------------------------------------
    def table(self, key, raw, relationships, model_keys):
        ent = raw["entity"]
        old = self.existing_tables.get(key.lower()) or {}
        doc = {"key": old.get("key", key)}
        doc["label"] = self.label(texts(ent.find("LocalizedNames"), "LocalizedName"), old.get("label")) or key
        plural = self.label(texts(ent.find("LocalizedCollectionNames"), "LocalizedCollectionName"),
                            old.get("label_plural"))
        if plural and ("label_plural" in old or (not old and plural != _plus_s(doc["label"]))):
            doc["label_plural"] = plural
        desc = self.label(texts(ent.find("Descriptions"), "Description"), old.get("description"))
        if desc:
            doc["description"] = desc
        ownership = OWNERSHIP.get(ent.findtext("OwnershipTypeMask"), "user")
        if ownership != "user" or "ownership" in old:
            doc["ownership"] = ownership
        ttype = "activity" if ent.findtext("IsActivity") == "1" else \
            "elastic" if (ent.findtext("TableType") or "").lower() == "elastic" else "standard"
        if ttype != "standard" or "table_type" in old:
            doc["table_type"] = ttype
        auditing = ent.findtext("IsAuditEnabled") == "1"
        if auditing or "auditing" in old:
            doc["auditing"] = auditing

        columns = {}
        old_cols = {k.lower(): (k, v or {}) for k, v in (old.get("columns") or {}).items()}
        for attr in ent.iter("attribute"):
            name = attr.get("PhysicalName") or attr.findtext("Name") or ""
            if attr.findtext("IsCustomField") != "1" or not self.owned(name):
                continue
            old_name, old_col = old_cols.get(name.lower(), (name, {}))
            col = self.column(key, name, attr, raw["formulas"], relationships, model_keys, old_col)
            if col is not None:
                columns[old_name] = col
        doc["columns"] = self.ordered(columns, old.get("columns") or {}, f"table {key}", "column")

        keys = []
        for k in ent.iter("EntityKey"):
            kname = k.findtext("Name") or k.findtext("LogicalName")
            entry = {"schema_name": kname,
                     "columns": [a.text.lower() for a in k.iter("AttributeName") if a.text]}
            klabel = self.label(texts(k.find("displaynames"), "displayname"))
            if klabel:
                entry["label"] = klabel
            keys.append(entry)
        seen = {k["schema_name"].lower() for k in keys}
        for k in old.get("keys") or []:
            if k.get("schema_name", "").lower() not in seen:
                keys.append(k)
                self.kept.append((f"table {key}", f"key {k.get('schema_name')} (not in the solution)"))
        if keys:
            old_order = [k.get("schema_name", "").lower() for k in old.get("keys") or []]
            keys.sort(key=lambda k: (old_order.index(k["schema_name"].lower())
                                     if k["schema_name"].lower() in old_order else len(old_order),
                                     k["schema_name"].lower()))
            doc["keys"] = keys
        return doc

    def ordered(self, new, old, where, what):
        """Existing order first, new entries after in solution order; entries only in the model are kept."""
        out = {}
        for name, spec in old.items():
            match = next((k for k in new if k.lower() == name.lower()), None)
            if match is not None:
                out[match] = new[match]
            else:
                out[name] = spec
                self.kept.append((where, f"{what} {name} (not in the solution)"))
        for name in new:
            if name not in out:
                out[name] = new[name]
        return out

    def column(self, table, name, attr, formulas, relationships, model_keys, old):
        where = f"{table}.{name}"
        xtype, fmt = attr.findtext("Type"), (attr.findtext("Format") or "").lower()
        source = attr.findtext("SourceType") or "0"
        mask = (attr.findtext("DisplayMask") or "").split("|")
        col = {"label": self.label(texts(attr.find("displaynames"), "displayname"), old.get("label")) or name}

        if source in ("1", "2"):
            return self.skip(where, f"{dvm.SOURCE_NAMES[int(source)]} column — outside the model")
        if source == "3":
            rtype = FORMULA_RESULT.get(xtype)
            formula = formulas.get(name.lower())
            if rtype is None:
                return self.skip(where, f"formula column returning '{xtype}' — not a formula result type")
            if formula is None:
                return self.skip(where, "formula column without a definition in Formulas/")
            col["type"] = "formula"
            col["result_type"] = rtype
            col["formula"] = formula[1:].strip() if formula.startswith("=") else formula.strip()
            ctype = rtype
        elif xtype == "nvarchar":
            if attr.findtext("AutoNumberFormat"):
                ctype = "autonumber"
            elif fmt in STRING_FORMATS:
                ctype = STRING_FORMATS[fmt]
            else:
                return self.skip(where, f"text column with format '{fmt}' — the format has no such type")
            col["type"] = ctype
        else:
            ctype = {"ntext": "multiline", "int": "integer", "decimal": "decimal", "money": "money",
                     "bit": "boolean", "datetime": "datetime", "picklist": "choice",
                     "multiselectpicklist": "multichoice", "lookup": "lookup", "file": "file",
                     "image": "image"}.get(xtype)
            if ctype is None:
                return self.skip(where, f"column type '{xtype}' — the format has no such type")
            if xtype == "int" and fmt not in ("", "none"):
                return self.skip(where, f"whole number with format '{fmt}' — the format has no such type")
            col["type"] = ctype

        if "PrimaryName" in mask:
            col["primary"] = True
        if ctype == "datetime":
            behavior = BEHAVIOR.get(attr.findtext("Behavior") or "1", "userlocal")
            if behavior == "dateonly":
                ctype = "date"
                if col["type"] != "formula":
                    col["type"] = "date"
                else:
                    col["result_type"] = "date"
            else:
                if behavior != "userlocal" or "behavior" in old:
                    col["behavior"] = behavior
                if fmt == "date" or "display" in old:
                    col["display"] = "date" if fmt == "date" else "datetime"

        if ctype in ("choice", "multichoice"):
            global_name = attr.findtext("OptionSetName")
            if not global_name:
                return self.skip(where, "local choice — the format references global Choices only")
            col["choice"] = global_name
        if ctype == "lookup":
            rels = relationships.get((table.lower(), name.lower())) or []
            if len(rels) != 1:
                return self.skip(where, "lookup with " + ("no relationship" if not rels else
                                                          f"{len(rels)} targets (polymorphic)"))
            target = rels[0]["target"]
            col["target"] = model_keys.get(target.lower(), target.lower())
            col["cascade"] = cascade_value(rels[0]["cascade"])

        if col["type"] != "formula":
            level = REQUIRED.get((attr.findtext("RequiredLevel") or "none").lower(), "none")
            if "required_level" in old:
                col["required_level"] = level
            elif level == "required":
                col["required"] = True
            elif level == "recommended":
                col["required_level"] = "recommended"

        desc = self.label(texts(attr.find("Descriptions"), "Description"), old.get("description"))
        if desc:
            col["description"] = desc
        for field, xml_tag in (("auditing", "IsAuditEnabled"),):
            if field in old:
                col[field] = attr.findtext(xml_tag) == "1"
        if "searchable" in old:
            col["searchable"] = "ValidForAdvancedFind" in mask

        if ctype in ("text", "email", "url", "phone", "autonumber", "multiline"):
            length = _int(attr.findtext("MaxLength"))
            if length:
                col["max_length"] = length
        if ctype == "autonumber":
            col["format"] = attr.findtext("AutoNumberFormat")
        if ctype in ("integer", "decimal", "money"):
            lo, hi = _num(attr.findtext("MinValue")), _num(attr.findtext("MaxValue"))
            if lo is not None and (ctype != "integer" or (lo, hi) != INT32 or "min" in old):
                col["min"] = lo
            if hi is not None and (ctype != "integer" or (lo, hi) != INT32 or "max" in old):
                col["max"] = hi
        if ctype == "decimal" and attr.findtext("Accuracy"):
            col["precision"] = _int(attr.findtext("Accuracy"))
        if ctype == "money":
            psource = PRECISION_SOURCE.get(attr.findtext("AccuracySource") or "2", "currency")
            if psource != "currency" or "precision_source" in old:
                col["precision_source"] = psource
            if psource == "fixed":
                col["precision"] = _int(attr.findtext("Accuracy"))
        if ctype == "boolean" and col["type"] != "formula":
            default = attr.findtext("AppDefaultValue")
            if default in ("0", "1"):
                col["default"] = default == "1"
            opts = {o.get("value"): texts(o.find("labels"), "label") for o in attr.iter("option")}
            for field, value in (("true_label", "1"), ("false_label", "0")):
                lab = self.label(opts.get(value) or {}, old.get(field))
                standard = dvm.BOOL_LABELS[LCID_LOCALE.get(self.base, "en")][0 if value == "1" else 1]
                if lab and (field in old or dvm.base_text(lab, self.base) != standard):
                    col[field] = lab
        if ctype == "choice" and col["type"] != "formula":
            default = _int(attr.findtext("AppDefaultValue"))
            if default is not None and default != -1:
                col["default"] = default
        if ctype in ("file", "image"):
            size = _int(attr.findtext("MaxValue"))
            if size and (size != 32768 or "max_size_kb" in old or ctype == "image"):
                col["max_size_kb"] = size
        if ctype == "image" and ("PrimaryImage" in mask or "primary_image" in old):
            col["primary_image"] = "PrimaryImage" in mask
        if old:
            for field in OPTIONAL_COLUMN:
                if field not in old:
                    col.pop(field, None)
        return col

    def skip(self, where, reason):
        self.skipped.append((where, reason))
        return None


def cascade_value(casc):
    """Expanded cascade → preset name, else the shortest mapping (as written by hand)."""
    for name, preset in dvm.CASCADE_PRESETS.items():
        if casc == preset:
            return name
    return {a: v for a, v in casc.items() if a == "delete" or v != "none"}


def _plus_s(label):
    return {k: v + "s" for k, v in label.items()} if isinstance(label, dict) else label + "s"


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #
def merge_solutions(sols):
    """One view of several solutions; a component described differently twice is an error."""
    merged = {"choices": {}, "tables": {}, "relationships": {}}
    conflicts = []
    for sol in sols:
        for section in ("choices", "relationships"):
            for k, v in sol[section].items():
                if k in merged[section] and merged[section][k] != v:
                    conflicts.append(f"{section[:-1]} {k}")
                merged[section].setdefault(k, v)
        for k, v in sol["tables"].items():
            if k in merged["tables"]:
                # The same table in two solutions: columns are added, never redefined.
                ent = merged["tables"][k]["entity"]
                attrs = ent.find("attributes")
                have = {(a.get("PhysicalName") or "").lower(): a for a in ent.iter("attribute")}
                for a in v["entity"].iter("attribute"):
                    prev = have.get((a.get("PhysicalName") or "").lower())
                    if prev is None:
                        attrs.append(a)
                    elif canonical(prev) != canonical(a):
                        conflicts.append(f"column {k}.{a.get('PhysicalName')}")
                merged["tables"][k]["formulas"].update(v["formulas"])
            else:
                merged["tables"][k] = {"file": v["file"], "entity": copy.deepcopy(v["entity"]),
                                       "formulas": dict(v["formulas"])}
    if conflicts:
        raise ImportError_("described differently by two solutions: " + ", ".join(sorted(set(conflicts))))
    return merged


def canonical(e):
    """Comparable form of an XML element, whitespace aside."""
    return (e.tag, sorted(e.attrib.items()), (e.text or "").strip(), [canonical(c) for c in e])


def new_meta(sols, existing_meta):
    if existing_meta:
        return existing_meta
    first = sols[0]
    if first["lcid"] not in LCID_LOCALE:
        raise ImportError_(f"base language {first['lcid']} is not one the format supports "
                           f"({', '.join(sorted(dvm.LOCALE_LCID))})")
    meta = {"version": "1.0", "publisher": {"prefix": first["prefix"], "solution": first["unique_name"]},
            "locale": LCID_LOCALE[first["lcid"]]}
    langs = first["languages"]
    if len(langs) > 1:
        meta["languages"] = [first["lcid"]] + [l for l in langs if l != first["lcid"]]
    return meta


def _dumper(yaml):
    """Block style with indented sequences; lists of scalars inline, as hand-written files do."""
    class Dumper(yaml.SafeDumper):
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    def seq(dumper, data):
        flat = bool(data) and all(not isinstance(x, (dict, list)) for x in data)
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flat)

    Dumper.add_representer(list, seq)
    return Dumper


def dump(doc, header=""):
    return header + dvm.yaml.dump(doc, Dumper=_dumper(dvm.yaml), sort_keys=False, allow_unicode=True, width=120)


def header_of(text):
    """Leading comment block of a YAML file (kept when the file is rewritten)."""
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#") or (lines and not line.strip()):
            lines.append(line)
        else:
            break
    while lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines)


def schema_directive(model_dir: Path, sub, schema):
    """The yaml-language-server line of an existing file of the same kind, if any."""
    for f in sorted((model_dir / sub).glob("*" + dvm.EXT)) if sub else [model_dir / dvm.CHOICES_FILE]:
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("# yaml-language-server:") and schema in line:
                    return line + "\n"
    return ""


def same_data(old_text, new_doc, meta):
    """True when the YAML texts describe the same data (labels compared localized)."""
    old = dvm.yaml.safe_load(old_text)
    a, b = copy.deepcopy(old), copy.deepcopy(new_doc)
    if isinstance(a, dict) and "columns" in a:
        dvm.localize_model(meta, {}, [a])
        dvm.localize_model(meta, {}, [b])
    elif isinstance(a, dict) and "choices" in a:
        dvm.localize_model(meta, a.get("choices") or {}, [])
        dvm.localize_model(meta, b.get("choices") or {}, [])
    return a == b


def build(sol_dirs, model_dir: Path, tables_filter=None):
    """Planned file changes: [(path, old_text or None, new_text)], plus the report."""
    dvm._require_yaml()
    sols = [read_solution(Path(d)) for d in sol_dirs]
    existing_meta = dvm.load_yaml(model_dir / dvm.META_FILE) if (model_dir / dvm.META_FILE).is_file() else None
    meta = new_meta(sols, existing_meta)
    existing_choices = {}
    if (model_dir / dvm.CHOICES_FILE).is_file():
        existing_choices = (dvm.load_yaml(model_dir / dvm.CHOICES_FILE) or {}).get("choices") or {}
    existing_tables = {}
    for f in dvm.table_files(model_dir):
        data = dvm.load_yaml(f)
        if isinstance(data, dict) and data.get("key"):
            existing_tables[data["key"].lower()] = (f, data)

    merged = merge_solutions(sols)
    b = Builder(meta, {k: v for k, (_f, v) in existing_tables.items()},
                {k.lower(): v for k, v in existing_choices.items()})
    wanted = {t.lower() for t in tables_filter} if tables_filter else None

    model_keys = {k.lower(): k for k in merged["tables"] if b.owned(k)}
    model_keys.update({k: d["key"] for k, (_f, d) in existing_tables.items()})
    changes = []
    if existing_meta is None:
        changes.append((model_dir / dvm.META_FILE, None,
                        dump(meta, schema_directive(model_dir, None, "meta.schema.json"))))

    # Choices: only those owned by the publisher.
    new_choices = {}
    for name, spec in sorted(merged["choices"].items(), key=lambda kv: kv[0].lower()):
        if not b.owned(name):
            b.skipped.append((f"choice {name}", "not prefixed with the publisher prefix"))
            continue
        new_choices[name] = b.choice(name, spec)
    if new_choices and wanted is None:
        choices = b.ordered({existing_name(existing_choices, n): v for n, v in new_choices.items()},
                            existing_choices, "choices", "choice")
        target = model_dir / dvm.CHOICES_FILE
        changes.append(file_change(target, {"choices": choices}, meta, "choices.schema.json", model_dir, None))

    # Tables.
    for key in sorted({n for sol in sols for n in sol["no_metadata"]} - set(merged["tables"]), key=str.lower):
        if b.owned(key) and (wanted is None or key.lower() in wanted):
            b.skipped.append((f"table {key}", "the solution holds no metadata for it (forms or views only)"))
    for key in sorted(merged["tables"], key=str.lower):
        if not b.owned(key):
            b.skipped.append((f"table {key}", "not prefixed with the publisher prefix (standard table)"))
            continue
        if wanted is not None and key.lower() not in wanted:
            continue
        doc = b.table(key, merged["tables"][key], merged["relationships"], model_keys)
        f, _old = existing_tables.get(key.lower(), (model_dir / dvm.TABLES_DIR / f"{key}{dvm.EXT}", None))
        changes.append(file_change(f, doc, meta, "table.schema.json", model_dir, dvm.TABLES_DIR))
    for key, (f, data) in sorted(existing_tables.items()):
        if key not in {k.lower() for k in merged["tables"]} and (wanted is None or key in wanted):
            b.kept.append((f"table {data['key']}", "not in the solution — file left as is"))
    return [c for c in changes if c is not None], b, meta


def existing_name(existing, name):
    return next((k for k in existing if k.lower() == name.lower()), name)


def file_change(path: Path, doc, meta, schema, model_dir, sub):
    if path.is_file():
        old = path.read_text(encoding="utf-8")
        if same_data(old, doc, meta):
            return (path, old, old)
        return (path, old, dump(doc, header_of(old)))
    directive = schema_directive(model_dir, sub, schema)
    return (path, None, dump(doc, directive))


def validate_result(model_dir: Path, changes):
    """Validate the model as it would be after the write, in a scratch copy."""
    tmp = Path(tempfile.mkdtemp())
    try:
        scratch = tmp / "metadata"
        if model_dir.is_dir():
            shutil.copytree(model_dir, scratch)
        for path, _old, new in changes:
            target = scratch / path.relative_to(model_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new, encoding="utf-8")
        return dvm.validate_model(scratch)
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def rel(path, base):
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def report(changes, builder, model_dir, show_diff=True):
    """Paths are shown relative to the model dir."""
    base = model_dir
    pending = 0
    for path, old, new in sorted(changes, key=lambda c: str(c[0])):
        name = rel(path, base)
        if old is None:
            pending += 1
            print(dvm.green(f"+ {name}"))
        elif old != new:
            pending += 1
            print(dvm.yellow(f"~ {name}"))
            if show_diff:
                for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2):
                    if line.startswith(("---", "+++")):
                        continue
                    print("    " + (dvm.green(line) if line.startswith("+") else
                                    dvm.red(line) if line.startswith("-") else dvm.grey(line)))
        else:
            print(dvm.grey(f"= {name}"))
    if builder.kept:
        print(dvm.bold("\nOnly in the model (kept):"))
        for where, what in builder.kept:
            print(f"  · {where}: {what}")
    if builder.skipped:
        print(dvm.bold("\nIn the solution but not describable by the format (skipped):"))
        for where, why in builder.skipped:
            print(f"  · {where}: {why}")
    return pending


def cmd_solution(args):
    model_dir = Path(args.model)
    try:
        changes, builder, _meta = build(args.solutions, model_dir, _csv(args.tables))
    except ImportError_ as e:
        print(dvm.red(f"ERROR: {e}"), file=sys.stderr)
        return 1
    pending = report(changes, builder, model_dir, show_diff=not args.no_diff)
    result = validate_result(model_dir, changes)
    for w in result.warnings:
        print(dvm.yellow("  ⚠ ") + w)
    if result.errors:
        for e in result.errors:
            print(dvm.red("  ✗ ") + e, file=sys.stderr)
        print(dvm.red(f"\nThe aligned model would be invalid ({len(result.errors)} error(s)) — nothing written."),
              file=sys.stderr)
        return 1
    if not pending:
        print(dvm.green("\nThe model already matches the solution."))
        return 0
    if not args.write:
        print(dvm.bold(f"\n{pending} file(s) to write — review, then run again with --write."))
        return 2
    for path, old, new in changes:
        if old != new:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
    print(dvm.green(f"\nWritten: {pending} file(s). Next: validate, then plan."))
    return 0


def _csv(value):
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dv_metadata_import.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("solution", help="align the model with one or more unpacked solutions")
    p.add_argument("solutions", nargs="+", help="unpacked solution folder(s) (holding Other/Solution.xml)")
    p.add_argument("--model", default="dataverse/metadata", help="model dir (default: dataverse/metadata)")
    p.add_argument("--tables", help="comma-separated table schema names to align (default: all)")
    p.add_argument("--write", action="store_true", help="write the new and changed files")
    p.add_argument("--no-diff", action="store_true", help="list the files without their diff")
    args = ap.parse_args(argv)
    return {"solution": cmd_solution}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
