#!/usr/bin/env python3
"""
dv_plugin_import.py — align the plug-in registrations with unpacked solutions
=============================================================================

Reads one or more solutions unpacked in the repo (``pac solution unpack`` /
SolutionPackager layout: ``pluginpackages/``, ``PluginAssemblies/``,
``SdkMessageProcessingSteps/``, ``customapis/``) and regenerates the
``*.dv.plugin.yaml`` files of the dvac-plugins skill from them::

    solution <unpacked-dir>... [--root <dir>] [--write]

Without ``--write`` nothing is written: the tool prints what would change —
``+`` new file, ``~`` changed file (unified diff), ``=`` unchanged — then the
model entries the solutions don't hold (kept) and what could not be imported
(skipped). ``--write`` writes the new and changed files.

The model is never reduced: steps and Custom APIs that are only in a file are
kept, so a later ``apply`` cannot delete them by surprise. Values the solution
does not carry are kept from the existing file: the package or assembly id, the
build output path, ``enabled``, and a Custom API's plug-in class when the
solution only holds its export key. A file whose content does not change is
not rewritten, so its comments survive. The result is validated with the
dvac-plugins validator before anything is written.

A solution identifies plug-in types of a package by an export key, not by class
name. The class comes from the step name when it follows the Plugin
Registration Tool format ("<class>: <message> of <table>"), from another step
of the same type, or from the existing file.

Offline: reads files only, never the environment.

Exit codes:
    0  nothing to change, or --write succeeded
    1  error, or the result would be invalid
    2  changes pending (run again with --write)

Do not edit this script per project — edit the model files.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
# The model loader and validator belong to the sibling skill of the same plugin.
sys.path.insert(0, str(SKILL_DIR.parent / "dvac-plugins" / "scripts"))
import dv_plugin as dvp  # noqa: E402

# Step names written by the Plugin Registration Tool: "<class>: <message> of <table>".
PRT_NAME = re.compile(r"^(?P<plugin>[A-Za-z_][\w.]*): (?P<message>[A-Za-z_]\w*)(?: of (?P<table>\w+))?$")
# sdkmessageid of platform messages — identical in every environment.
KNOWN_MESSAGES = {
    "9ebdbb1b-ea3e-db11-86a7-000a3a5473e8": "Create",
    "20bebb1b-ea3e-db11-86a7-000a3a5473e8": "Update",
    "a1bdbb1b-ea3e-db11-86a7-000a3a5473e8": "Delete",
}
HEADER = ("# yaml-language-server: $schema=<path to dvac-plugins>/references/schemas/plugin.schema.json\n"
          "# Imported from the unpacked solution — review it, then run validate and plan.\n")


class ImportError_(Exception):
    """A solution the tool cannot read consistently."""


# --------------------------------------------------------------------------- #
# Reading the unpacked solution
# --------------------------------------------------------------------------- #
def xml_root(path: Path):
    return ET.parse(path).getroot()


def _int(v, default=None):
    return int(v) if v not in (None, "") else default


def _guid(v):
    return (v or "").strip().strip("{}").lower()


def _attrs(v):
    return sorted(a.strip().lower() for a in (v or "").split(",") if a.strip())


def label_of(e):
    """Text of a <displayname default="…"> / <description> element (base language)."""
    if e is None:
        return ""
    if e.get("default") is not None:
        return e.get("default")
    lab = e.find("label")
    return lab.get("description", "") if lab is not None else (e.text or "")


def read_params(folder: Path, section):
    tag = "customapirequestparameter" if section == "request" else "customapiresponseproperty"
    out = {}
    for f in sorted(folder.glob(f"*/{tag}.xml")) if folder.is_dir() else []:
        p = xml_root(f)
        uname = p.get("uniquename")
        d = {"uniquename": uname, "type": dvp.PARAM_TYPE_FROM_API.get(_int(p.findtext("type"))),
             "table": (p.findtext("logicalentityname") or "").lower() or None,
             "name": p.findtext("name") or "", "display_name": label_of(p.find("displayname")),
             "description": label_of(p.find("description"))}
        if section == "request":
            d["optional"] = p.findtext("isoptional") == "1"
        out[uname.lower()] = d
    return out


def read_solution(sol_dir: Path):
    sol_xml = sol_dir / "Other" / "Solution.xml"
    if not sol_xml.is_file():
        raise ImportError_(f"{sol_dir}: no Other/Solution.xml — not an unpacked solution")
    manifest = xml_root(sol_xml).find("SolutionManifest")
    sol = {"dir": sol_dir, "unique_name": manifest.findtext("UniqueName"),
           "prefix": (manifest.findtext("Publisher/CustomizationPrefix") or "").lower(),
           "units": {}, "type_ids": {}, "steps": [], "apis": {}}

    for f in sorted(sol_dir.glob("pluginpackages/*/pluginpackage.xml")):
        name = xml_root(f).get("uniquename") or f.parent.name
        sol["units"][name.lower()] = {"kind": "package", "name": name}
    for f in sorted(sol_dir.glob("PluginAssemblies/**/*.dll.data.xml")):
        root = xml_root(f)
        name = (root.get("FullName") or f.name[: -len(".dll.data.xml")]).split(",")[0].strip()
        sol["units"][name.lower()] = {"kind": "assembly", "name": name}
        for t in root.iter("PluginType"):
            if t.get("PluginTypeId") and t.get("Name"):
                sol["type_ids"][_guid(t.get("PluginTypeId"))] = (t.get("Name"), name)

    for f in sorted(sol_dir.glob("SdkMessageProcessingSteps/*.xml")):
        r = xml_root(f)
        type_name = (r.findtext("PluginTypeName") or "").split(",")
        step = {
            "name": r.get("Name") or "", "message_id": _guid(r.findtext("SdkMessageId")),
            "export_key": (r.findtext("PluginTypeExportKey") or "").upper() or None,
            "type_id": _guid(r.findtext("PluginTypeId")) or None,
            "type_name": type_name[0].strip() or None,
            "type_assembly": type_name[1].strip() if len(type_name) > 1 else None,
            "table": None if (r.findtext("PrimaryEntity") or "none").lower() == "none"
            else r.findtext("PrimaryEntity").lower(),
            "stage": dvp.STAGE_FROM_API.get(_int(r.findtext("Stage")), r.findtext("Stage")),
            "mode": dvp.MODE_FROM_API.get(_int(r.findtext("Mode"), 0), r.findtext("Mode")),
            "rank": _int(r.findtext("Rank"), 1),
            "filtering_attributes": _attrs(r.findtext("FilteringAttributes")),
            "configuration": r.findtext("Configuration") or "",
            "description": r.findtext("Description") or "",
            "async_auto_delete": r.findtext("AsyncAutoDelete") == "1",
            "images": {},
        }
        for i in r.iter("SdkMessageProcessingStepImage"):
            step["images"][(i.get("Name") or "").lower()] = {
                "name": i.get("Name"), "alias": i.findtext("EntityAlias") or i.get("Name"),
                "type": dvp.IMAGE_TYPE_FROM_API.get(_int(i.findtext("ImageType"))),
                "attributes": _attrs(i.findtext("Attributes")),
                "message_property": i.findtext("MessagePropertyName")}
        sol["steps"].append(step)

    for f in sorted(sol_dir.glob("customapis/*/customapi.xml")):
        r = xml_root(f)
        uname = r.get("uniquename")
        sol["apis"][uname.lower()] = {
            "uniquename": uname, "name": r.findtext("name") or "",
            "display_name": label_of(r.find("displayname")), "description": label_of(r.find("description")),
            "binding": dvp.BINDING_FROM_API.get(_int(r.findtext("bindingtype"), 0)),
            "bound_table": (r.findtext("boundentitylogicalname") or "").lower() or None,
            "is_function": r.findtext("isfunction") == "1", "is_private": r.findtext("isprivate") == "1",
            "allowed_steps": dvp.ALLOWED_STEPS_FROM_API.get(_int(r.findtext("allowedcustomprocessingsteptype"), 0)),
            "workflow_enabled": r.findtext("workflowsdkstepenabled") == "1",
            "execute_privilege": r.findtext("executeprivilegename") or None,
            "export_key": (r.findtext("plugintypeid/plugintypeexportkey") or "").upper() or None,
            "type_id": _guid(r.findtext("plugintypeid/plugintypeid")) or None,
            "plugin": None,
            "request": read_params(f.parent / "customapirequestparameters", "request"),
            "response": read_params(f.parent / "customapiresponseproperties", "response"),
        }
    return sol


# --------------------------------------------------------------------------- #
# Existing model
# --------------------------------------------------------------------------- #
def existing_units(root: Path):
    """{lower unit name: {file, text, data, classes}} for every *.dv.plugin.yaml below root."""
    out = {}
    for f in dvp.find_files(root):
        data = dvp.load_yaml(f)
        if not isinstance(data, dict) or not ("package" in data or "assembly" in data):
            continue
        kind = dvp.unit_kind(data)
        name = (data[kind] or {}).get("name")
        if not name:
            continue
        classes = set(dvp.scan_sources(f.parent))
        classes |= {s.get("plugin") for s in data.get("steps") or [] if s.get("plugin")}
        classes |= {a.get("plugin") for a in (data.get("custom_apis") or {}).values() if (a or {}).get("plugin")}
        out[name.lower()] = {"file": f, "text": f.read_text(encoding="utf-8"), "data": data, "classes": classes}
    return out


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #
class Aligner:
    def __init__(self, sols, existing):
        self.sols = sols
        self.existing = existing
        self.skipped = []   # (where, reason)
        self.kept = []      # (where, what)
        self.units = {}
        for sol in sols:
            for k, u in sol["units"].items():
                self.units.setdefault(k, dict(u, sol=sol, steps={}, apis={}))
        self.type_ids = {k: v for sol in sols for k, v in sol["type_ids"].items()}
        self.key_class = self._export_keys()

    def _export_keys(self):
        """Export key → class, learned from the steps whose name gives the class."""
        found, clash = {}, set()
        for sol in self.sols:
            for s in sol["steps"]:
                m = PRT_NAME.match(s["name"])
                if s["export_key"] and m:
                    if found.setdefault(s["export_key"], m["plugin"]) != m["plugin"]:
                        clash.add(s["export_key"])
        return {k: v for k, v in found.items() if k not in clash}

    def existing_step(self, name):
        for u in self.existing.values():
            for s in u["data"].get("steps") or []:
                if dvp.step_name(s).lower() == name.lower():
                    return s
        return None

    def unit_for(self, plugin, assembly=None, api=None, export_key=None):
        """The unit a class (or a Custom API) belongs to, or None when it cannot be told."""
        if assembly and assembly.lower() in self.units:
            return self.units[assembly.lower()]
        if len(self.units) == 1:
            return next(iter(self.units.values()))
        packages = [u for u in self.units.values() if u["kind"] == "package"]
        if export_key and len(packages) == 1:
            return packages[0]  # only package types are identified by an export key
        owners = [k for k, u in self.existing.items() if k in self.units and
                  ((plugin and plugin in u["classes"]) or
                   (api and api.lower() in {a.lower() for a in (u["data"].get("custom_apis") or {})}))]
        return self.units[owners[0]] if len(owners) == 1 else None

    def resolve_steps(self):
        for sol in self.sols:
            for s in sol["steps"]:
                where = f"step {s['name']}"
                m = PRT_NAME.match(s["name"])
                prev = self.existing_step(s["name"]) or {}
                typed = self.type_ids.get(s["type_id"]) if s["type_id"] else None
                plugin = s["type_name"] or (typed and typed[0]) or (m and m["plugin"]) or \
                    self.key_class.get(s["export_key"]) or prev.get("plugin")
                message = (m and m["message"]) or KNOWN_MESSAGES.get(s["message_id"]) or prev.get("message")
                if not plugin or not message:
                    self.skipped.append((where, "plug-in class or message unknown — the name does not follow "
                                                "'<class>: <message> of <table>' and no other source names it"))
                    continue
                unit = self.unit_for(plugin, s["type_assembly"] or (typed and typed[1]),
                                     export_key=s["export_key"])
                if unit is None:
                    self.skipped.append((where, f"cannot tell which package or assembly holds '{plugin}' — "
                                                "describe the step in its file once, then import again"))
                    continue
                step = {k: v for k, v in s.items()
                        if k not in ("message_id", "export_key", "type_id", "type_name", "type_assembly")}
                step.update(plugin=plugin, message=message, enabled=prev.get("enabled", True))
                unit["steps"][s["name"].lower()] = step

    def resolve_apis(self):
        for sol in self.sols:
            for uname, a in sol["apis"].items():
                typed = self.type_ids.get(a["type_id"]) if a["type_id"] else None
                prev = next((spec or {} for u in self.existing.values()
                             for k, spec in (u["data"].get("custom_apis") or {}).items()
                             if k.lower() == uname), {})
                plugin = (typed and typed[0]) or self.key_class.get(a["export_key"]) or prev.get("plugin")
                if not plugin and (a["export_key"] or a["type_id"]):
                    self.skipped.append((f"custom API {a['uniquename']}", "plug-in class unknown (export key "
                                         "only) — set custom_apis.<name>.plugin in the file"))
                unit = self.unit_for(plugin, typed and typed[1], api=uname, export_key=a["export_key"])
                if unit is None:
                    self.skipped.append((f"custom API {a['uniquename']}",
                                         "cannot tell which package or assembly implements it"))
                    continue
                unit["apis"][a["uniquename"]] = dict(a, plugin=plugin)

    def document(self, unit):
        """The aligned YAML document of one unit, and the path of its file."""
        prev = self.existing.get(unit["name"].lower())
        old = (prev or {}).get("data") or {}
        sol = unit["sol"]
        pub = old.get("publisher") or {"prefix": sol["prefix"], "solution": sol["unique_name"]}
        srv = {"id": None, "steps": unit["steps"]}
        doc = dvp.export_document(unit["kind"], unit["name"], srv, unit["apis"], pub["solution"], pub["prefix"])
        doc["publisher"] = copy.deepcopy(pub)
        kind = unit["kind"]
        if old.get(kind):
            doc[kind] = {**doc[kind], **{k: v for k, v in old[kind].items() if k != "name"}}
        where = f"{kind} {unit['name']}"
        old_steps = {dvp.step_name(s).lower(): s for s in old.get("steps") or []}
        for st in doc.get("steps") or []:
            raw = unit["steps"][dvp.step_name(st).lower()]
            prev_step = old_steps.get(dvp.step_name(st).lower())
            if prev_step:
                prev_images = {i["name"].lower(): i for i in prev_step.get("images") or []}
                for img in st.get("images") or []:
                    if img["name"].lower() in prev_images:
                        restore(prev_images[img["name"].lower()], img, raw["images"][img["name"].lower()],
                                ("alias", "attributes", "message_property"))
                restore(prev_step, st, raw, ("name", "rank", "mode", "async_auto_delete", "enabled",
                                             "configuration", "description", "filtering_attributes"))
        old_apis = {k.lower(): v or {} for k, v in (old.get("custom_apis") or {}).items()}
        for name, spec in (doc.get("custom_apis") or {}).items():
            raw, prev_api = unit["apis"][name], old_apis.get(name.lower())
            if prev_api:
                for section in ("request", "response"):
                    prev_params = {k.lower(): v or {} for k, v in (prev_api.get(section) or {}).items()}
                    for pname, ps in (spec.get(section) or {}).items():
                        if pname.lower() in prev_params:
                            restore(prev_params[pname.lower()], ps, raw[section][pname.lower()],
                                    ("display_name", "description", "optional", "table"))
                restore(prev_api, spec, raw, ("name", "description", "workflow_enabled", "is_private",
                                              "execute_privilege", "plugin", "is_function", "allowed_steps"))
        doc_steps = doc.get("steps") or []
        new_steps = {dvp.step_name(s).lower(): s for s in doc_steps}
        steps = []
        for s in old.get("steps") or []:
            key = dvp.step_name(s).lower()
            if key in new_steps:
                steps.append(new_steps.pop(key))
            else:
                steps.append(s)
                self.kept.append((where, f"step {dvp.step_name(s)} (not in the solution)"))
        steps += [s for s in doc_steps if dvp.step_name(s).lower() in new_steps]
        if steps:
            doc["steps"] = steps
        apis = {}
        new_apis = doc.get("custom_apis") or {}
        for name, spec in (old.get("custom_apis") or {}).items():
            match = next((k for k in new_apis if k.lower() == name.lower()), None)
            if match is None:
                apis[name] = spec
                self.kept.append((where, f"custom API {name} (not in the solution)"))
            else:
                apis[match] = new_apis[match]
        for name, spec in new_apis.items():
            apis.setdefault(name, spec)
        if apis:
            doc["custom_apis"] = apis
        ordered = {k: doc[k] for k in ("version", "publisher", kind, "steps", "custom_apis") if k in doc}
        if prev:
            return prev["file"], ordered
        project = unit["name"][len(pub["prefix"]) + 1:] if kind == "package" and \
            unit["name"].lower().startswith(pub["prefix"] + "_") else unit["name"]
        return Path("plugins") / project / f"{project}{dvp.EXT}", ordered


# Values the format applies when a field is left out.
DEFAULTS = {"mode": "sync", "rank": 1, "enabled": True, "is_function": False, "allowed_steps": "none",
            "optional": False}


def restore(old, new, raw, fields):
    """Align the new spec on the existing one where they mean the same: fields the existing file
    declares with their default value (the export leaves defaults out), defaults it leaves out
    (the export writes some), and column lists in another order. Then follow its key order."""
    for f in fields:
        if f in old and f not in new and raw.get(f) not in (None, ""):
            new[f] = raw[f]
    for f, default in DEFAULTS.items():
        if f in new and f not in old and new[f] == default:
            del new[f]
    for f in ("attributes", "filtering_attributes"):
        if isinstance(old.get(f), list) and isinstance(new.get(f), list) and \
                sorted(a.lower() for a in old[f]) == sorted(new[f]):
            new[f] = old[f]
    order = [k for k in old if k in new] + [k for k in new if k not in old]
    items = [(k, new[k]) for k in order]
    new.clear()
    new.update(items)


def merge_check(sols):
    seen = {}
    for sol in sols:
        for k in list(sol["apis"]) + [s["name"].lower() for s in sol["steps"]]:
            if k in seen and seen[k] != sol["unique_name"]:
                raise ImportError_(f"'{k}' is in two solutions ({seen[k]}, {sol['unique_name']})")
            seen[k] = sol["unique_name"]


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


def dump(doc, header):
    return header + dvp.yaml.dump(doc, Dumper=_dumper(dvp.yaml), sort_keys=False, allow_unicode=True, width=120)


def header_of(text):
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#") or (lines and not line.strip()):
            lines.append(line)
        else:
            break
    while lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines)


def build(sol_dirs, root: Path):
    """Planned file changes: [(path, old_text or None, new_text)], plus the aligner (report)."""
    dvp._require_yaml()
    sols = [read_solution(Path(d)) for d in sol_dirs]
    merge_check(sols)
    existing = existing_units(root)
    al = Aligner(sols, existing)
    if not al.units:
        raise ImportError_("no plug-in package or assembly in the solution(s)")
    al.resolve_steps()
    al.resolve_apis()
    changes = []
    for key in sorted(al.units):
        path, doc = al.document(al.units[key])
        path = path if path.is_absolute() else root / path
        prev = existing.get(key)
        if prev:
            old = prev["text"]
            same = dvp.yaml.safe_load(old) == doc
            changes.append((path, old, old if same else dump(doc, header_of(old))))
        else:
            changes.append((path, None, dump(doc, HEADER)))
    for key, prev in sorted(existing.items()):
        if key not in al.units:
            al.kept.append((prev["data"][dvp.unit_kind(prev["data"])]["name"], "not in the solution — file left as is"))
    return changes, al


def validate_result(changes):
    """Validate the written files in a scratch folder (no sources or build output: those checks
    run later with validate and plan)."""
    tmp = Path(tempfile.mkdtemp())
    try:
        for i, (path, _old, new) in enumerate(changes):
            d = tmp / str(i)
            d.mkdir()
            (d / path.name).write_text(new, encoding="utf-8")
        report = dvp.validate_path(tmp)
        report.warnings = [w for w in report.warnings if "no build output" not in w]
        return report
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


def report(changes, al, root, show_diff=True):
    pending = 0
    for path, old, new in sorted(changes, key=lambda c: str(c[0])):
        name = rel(path, root)
        if old is None:
            pending += 1
            print(dvp.green(f"+ {name}"))
        elif old != new:
            pending += 1
            print(dvp.yellow(f"~ {name}"))
            if show_diff:
                for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2):
                    if line.startswith(("---", "+++")):
                        continue
                    print("    " + (dvp.green(line) if line.startswith("+") else
                                    dvp.red(line) if line.startswith("-") else dvp.grey(line)))
        else:
            print(dvp.grey(f"= {name}"))
    if al.kept:
        print(dvp.bold("\nOnly in the model (kept):"))
        for where, what in al.kept:
            print(f"  · {where}: {what}")
    if al.skipped:
        print(dvp.bold("\nIn the solution but not imported (skipped):"))
        for where, why in al.skipped:
            print(f"  · {where}: {why}")
    return pending


def cmd_solution(args):
    root = Path(args.root).resolve() if args.root else dvp.find_repo_root(Path.cwd())
    try:
        changes, al = build(args.solutions, root)
    except ImportError_ as e:
        print(dvp.red(f"ERROR: {e}"), file=sys.stderr)
        return 1
    pending = report(changes, al, root, show_diff=not args.no_diff)
    result = validate_result(changes)
    for w in result.warnings:
        print(dvp.yellow("  ⚠ ") + w)
    if result.errors:
        for e in result.errors:
            print(dvp.red("  ✗ ") + e, file=sys.stderr)
        print(dvp.red(f"\nThe aligned files would be invalid ({len(result.errors)} error(s)) — nothing written."),
              file=sys.stderr)
        return 1
    if not pending:
        print(dvp.green("\nThe plug-in files already match the solution."))
        return 0
    if not args.write:
        print(dvp.bold(f"\n{pending} file(s) to write — review, then run again with --write."))
        return 2
    for path, old, new in changes:
        if old != new:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
    print(dvp.green(f"\nWritten: {pending} file(s). Next: validate, then plan."))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dv_plugin_import.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("solution", help="align the *.dv.plugin.yaml files with one or more unpacked solutions")
    p.add_argument("solutions", nargs="+", help="unpacked solution folder(s) (holding Other/Solution.xml)")
    p.add_argument("--root", help="folder searched for *.dv.plugin.yaml files and where new ones go "
                                  "under plugins/<Project>/ (default: the repo root)")
    p.add_argument("--write", action="store_true", help="write the new and changed files")
    p.add_argument("--no-diff", action="store_true", help="list the files without their diff")
    args = ap.parse_args(argv)
    return {"solution": cmd_solution}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
