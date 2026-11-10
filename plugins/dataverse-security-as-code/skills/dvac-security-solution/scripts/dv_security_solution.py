#!/usr/bin/env python3
"""
dv_security_solution.py — the security roles model and unpacked solutions
==========================================================================

Reads one or more solutions unpacked in the repo (``pac solution unpack`` /
SolutionPackager XML layout: ``Other/Solution.xml``, ``Roles/*.xml``,
``Entities/*/``) and, against the ``*.dv.security.yaml`` model of the
dvac-security skill::

    import  <unpacked-dir>... [--model <dir>] [--roles <a,b>] [--write]
    compare <unpacked-dir>... [--model <dir>] [--roles <a,b>]

``import`` regenerates the role files from the solutions. Without ``--write``
nothing is written: it prints what would change — ``+`` new file, ``~``
changed file (unified diff), ``=`` unchanged — then the privileges the
solutions don't hold (kept) and what could not be read (skipped). ``--write``
writes the new and changed files, after validating the whole model.

The model is never reduced: a privilege only in a role file is kept, because
``apply`` would revoke a privilege the file leaves out. A file whose data does
not change is not rewritten, so its comments survive.

``compare`` writes nothing. It lists what is only in the solution (+), only in
the model (-), what differs (~), and, for information, the values the solution
holds that the model doesn't declare (·) and what cannot be compared offline
(!). Privileges are compared by name and access level.

A table privilege is named ``prv<Verb><table>``. Offline, a privilege goes under
``tables:`` only when ``<table>`` is a table the role file already lists or a
table of the solution (``Entities/``); any other goes under ``privileges:`` by
name, which the format accepts as well.

The unpacked solutions are read only: no subcommand writes in them, and a
write target inside one is refused. Offline: reads files only, never the
environment.

Exit codes:
    0  done (import: nothing to change or --write succeeded; compare: computed)
    1  error, refused, or the imported model would be invalid
    2  --detailed-exitcode: files would change (import), or drift (compare)

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
sys.path.insert(0, str(SKILL_DIR.parent / "dvac-security" / "scripts"))
import dv_security as dvs  # noqa: E402

# Longest verb first, so that prvAppendTo… is not read as prvAppend + "To…".
VERBS = sorted(dvs.VERB_TO_TYPE.items(), key=lambda kv: -len(kv[1]))
VERB_ORDER = list(dvs.VERB_TO_TYPE)
NEW_HEADER = ("# Imported from the unpacked solution by dv_security_solution.py import.\n"
              "# This file is the role's COMPLETE privilege set: a privilege it doesn't list is revoked by apply.\n")


class ImportError_(Exception):
    """A solution the tool cannot read consistently."""


# --------------------------------------------------------------------------- #
# Reading the unpacked solution
# --------------------------------------------------------------------------- #
def xml_root(path: Path):
    return ET.parse(path).getroot()


def read_solution(sol_dir: Path):
    sol_xml = sol_dir / "Other" / "Solution.xml"
    if not sol_xml.is_file():
        raise ImportError_(f"{sol_dir}: no Other/Solution.xml — not an unpacked solution")
    manifest = xml_root(sol_xml).find("SolutionManifest")
    sol = {"dir": sol_dir, "unique_name": manifest.findtext("UniqueName"), "tables": set(), "roles": {},
           "skipped": []}
    for f in sorted(sol_dir.glob("Entities/*/Entity.xml")):
        name = (xml_root(f).findtext("Name") or f.parent.name).strip()
        sol["tables"].add(name.lower())
    for f in sorted(sol_dir.glob("Roles/*.xml")):
        root = xml_root(f)
        role = root if root.tag == "Role" else root.find(".//Role")
        if role is None or not (role.get("name") or "").strip():
            sol["skipped"].append((f"Roles/{f.name}", "no <Role name=…> element"))
            continue
        name = role.get("name").strip()
        privileges = {}
        for p in role.iter("RolePrivilege"):
            pname, level = p.get("name") or "", dvs.DEPTH_FROM_API.get(p.get("level") or "")
            if not pname:
                continue
            if level is None:
                sol["skipped"].append((f"role {name}", f"privilege {pname}: level '{p.get('level')}' unknown"))
                continue
            privileges[pname] = level
        inherited = role.findtext("IsInherited")
        sol["roles"][dvs.role_key(name)] = {
            "name": name, "privileges": privileges,
            # Read only when the role XML holds them; otherwise they are not carried.
            "description": role.findtext("Description"),
            "member_inheritance": dvs.INHERITANCE_FROM_API.get(int(inherited))
            if (inherited or "").strip().isdigit() else None,
        }
    return sol


def merge_solutions(sols):
    """One view of several solutions; a role described differently twice is an error."""
    merged = {"tables": set(), "roles": {}, "skipped": []}
    for sol in sols:
        merged["tables"] |= sol["tables"]
        merged["skipped"] += sol["skipped"]
        for key, role in sol["roles"].items():
            if key in merged["roles"] and merged["roles"][key] != role:
                raise ImportError_(f"role '{role['name']}' is described differently by two solutions")
            merged["roles"].setdefault(key, role)
    return merged


def split_privilege(name, known_tables):
    """prv<Verb><table> → (table, verb) when <table> is a known table, else None."""
    low = name.lower()
    for verb, api in VERBS:
        head = "prv" + api.lower()
        if low.startswith(head) and low[len(head):] in known_tables:
            return low[len(head):], verb
    return None


def privilege_name(table, verb):
    return f"prv{dvs.VERB_TO_TYPE[verb]}{table}"


# --------------------------------------------------------------------------- #
# Existing model
# --------------------------------------------------------------------------- #
def solution_folder_of(path, sol_dirs):
    """The input solution folder (one holding Other/Solution.xml) that holds ``path`` or is it."""
    p = Path(path).resolve()
    for d in sol_dirs:
        d = Path(d).resolve()
        if (d / "Other" / "Solution.xml").is_file() and (p == d or d in p.parents):
            return d
    return None


def existing_roles(model_dir: Path):
    """{role key: (path, text, data)} of the role files."""
    out = {}
    for f in dvs.role_files(model_dir):
        data = dvs.load_yaml(f)
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            out[dvs.role_key(data["name"])] = (f, f.read_text(encoding="utf-8"), data)
    return out


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def role_document(role, old, known_tables, kept):
    """The role file of a solution role, keeping what the existing file decides."""
    doc = {"name": old.get("name", role["name"])}
    if role["description"]:
        doc["description"] = role["description"]
    elif "description" in old:
        doc["description"] = old["description"]
    if role["member_inheritance"] and ("member_inheritance" in old or not old):
        doc["member_inheritance"] = role["member_inheritance"]
    elif "member_inheritance" in old:
        doc["member_inheritance"] = old["member_inheritance"]

    old_tables = {t: dict(v or {}) for t, v in (old.get("tables") or {}).items()}
    old_named = dict(old.get("privileges") or {})
    place = {}
    for t, verbs in old_tables.items():
        for verb in verbs:
            place[privilege_name(t, verb).lower()] = ("table", t, verb)
    for n in old_named:
        place[n.lower()] = ("named", n)
    known = known_tables | {t.lower() for t in old_tables}

    tables = {t: {} for t in old_tables}
    named, seen = {}, set()
    for pname, level in sorted(role["privileges"].items(), key=lambda kv: kv[0].lower()):
        seen.add(pname.lower())
        where = place.get(pname.lower())
        if where is None:
            split = split_privilege(pname, known)
            where = ("table", *split) if split else ("named", pname)
        if where[0] == "table":
            tables.setdefault(where[1], {})[where[2]] = level
        else:
            named[where[1]] = level
    where_role = f"role {doc['name']}"
    for t, verbs in old_tables.items():
        for verb, level in verbs.items():
            if privilege_name(t, verb).lower() not in seen:
                tables[t][verb] = level
                if level != "none":
                    kept.append((where_role, f"{t}.{verb}: {level} (not in the solution)"))
    for n, level in old_named.items():
        if n.lower() not in seen:
            named[n] = level
            if level != "none":
                kept.append((where_role, f"{n}: {level} (not in the solution)"))

    def verb_order(t, verbs):
        old_order = list(old_tables.get(t) or {})
        return dict(sorted(verbs.items(), key=lambda kv: (old_order.index(kv[0]) if kv[0] in old_order
                                                          else len(old_order), VERB_ORDER.index(kv[0]))))

    new_tables = sorted((t for t in tables if t not in old_tables))
    tables = {t: verb_order(t, tables[t]) for t in [*old_tables, *new_tables] if tables[t]}
    if tables:
        doc["tables"] = tables
    if named:
        doc["privileges"] = {**{n: named[n] for n in old_named if n in named},
                             **{n: named[n] for n in sorted(named, key=str.lower) if n not in old_named}}
    order = [k for k in old if k in doc] + [k for k in doc if k not in old]
    return {k: doc[k] for k in order}


def dump(doc, header=""):
    return header + dvs.yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False,
                                       width=120)


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


def build(sol_dirs, model_dir: Path, roles_filter=None):
    """Planned file changes: [(path, old_text or None, new_text)], plus kept and skipped."""
    dvs._require_yaml()
    sols = [read_solution(Path(d)) for d in sol_dirs]
    merged = merge_solutions(sols)
    existing = existing_roles(model_dir)
    wanted = {dvs.role_key(r) for r in roles_filter} if roles_filter else None
    known = set(merged["tables"])
    for _f, _t, data in existing.values():
        known |= {t.lower() for t in data.get("tables") or {}}
    kept, changes = [], []
    if not (model_dir / dvs.META_FILE).is_file():
        changes.append((model_dir / dvs.META_FILE, None,
                        dump({"version": "1.0", "solution": sols[0]["unique_name"]})))
    taken = {f.name.lower() for f, _t, _d in existing.values()}
    for key in sorted(merged["roles"]):
        if wanted is not None and key not in wanted:
            continue
        role = merged["roles"][key]
        path, old_text, old = existing.get(key, (None, None, {}))
        doc = role_document(role, old, known, kept)
        if path is not None:
            same = dvs.yaml.safe_load(old_text) == doc
            changes.append((path, old_text, old_text if same else dump(doc, header_of(old_text))))
            continue
        path = model_dir / dvs.ROLES_DIR / f"{dvs.role_slug(role['name'])}{dvs.EXT}"
        if path.name.lower() in taken or path.exists():
            raise ImportError_(f"{path} already describes another role — rename that file, then import again")
        taken.add(path.name.lower())
        changes.append((path, None, dump(doc, NEW_HEADER)))
    for key, (_f, _t, data) in sorted(existing.items()):
        if key not in merged["roles"] and (wanted is None or key in wanted):
            kept.append((f"role {data['name']}", "not in the solution — file left as is"))
    return changes, kept, merged["skipped"]


def validate_result(model_dir: Path, changes):
    """Validate the model as it would be after the write, in a scratch copy."""
    tmp = Path(tempfile.mkdtemp())
    try:
        scratch = tmp / "security"
        if model_dir.is_dir():
            shutil.copytree(model_dir, scratch)
        for path, _old, new in changes:
            target = scratch / path.relative_to(model_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new, encoding="utf-8")
        return dvs.validate_model(scratch)
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------------------------------- #
# Compare (writes nothing)
# --------------------------------------------------------------------------- #
class Comparison:
    """Differences between the role files and the solutions, in five sections."""

    def __init__(self):
        self.only_solution = []   # "+"
        self.only_model = []      # "-"
        self.differs = []         # "~"
        self.undeclared = []      # "·" informational
        self.skipped = []         # "!" informational

    @property
    def drift(self):
        return len(self.only_solution) + len(self.only_model) + len(self.differs)


def compare(sol_dirs, model_dir: Path, roles_filter=None):
    dvs._require_yaml()
    if not (model_dir / dvs.META_FILE).is_file():
        raise ImportError_(f"no {dvs.META_FILE} in {model_dir} — there is no model to compare (run import)")
    result = dvs.validate_model(model_dir)
    if result.errors:
        raise ImportError_(f"the model is invalid ({len(result.errors)} error(s): {result.errors[0]}) — "
                           "run validate with dvac-security")
    merged = merge_solutions([read_solution(Path(d)) for d in sol_dirs])
    existing = existing_roles(model_dir)
    wanted = {dvs.role_key(r) for r in roles_filter} if roles_filter else None
    cmp = Comparison()
    cmp.skipped = [f"{where}: {why}" for where, why in merged["skipped"]]
    for key in sorted(set(merged["roles"]) | set(existing)):
        if wanted is not None and key not in wanted:
            continue
        role, model = merged["roles"].get(key), existing.get(key)
        if model is None:
            cmp.only_solution.append(f"role {role['name']} ({len(role['privileges'])} privilege(s))")
            continue
        spec = model[2]
        name = spec["name"]
        if role is None:
            cmp.only_model.append(f"role {name}")
            continue
        for field in ("description", "member_inheritance"):
            have = role[field]
            if have is None or (field == "description" and not have and field not in spec):
                continue  # not carried by the role XML
            if field in spec and spec[field] != have:
                cmp.differs.append(f"role {name}.{field}: model={spec[field]!r}  solution={have!r}")
            elif field not in spec:
                cmp.undeclared.append(f"role {name}.{field} = {have!r}")
        sol_privs = {n.lower(): (n, level) for n, level in role["privileges"].items()}
        unresolved_verbs = set()
        for source, table, verb, pname, level in dvs.declared_privileges(spec):
            expected = (pname or privilege_name(table, verb)).lower()
            found = sol_privs.pop(expected, None)
            if found is None:
                if table and table not in merged["tables"]:
                    unresolved_verbs.add(dvs.VERB_TO_TYPE[verb].lower())
                    cmp.skipped.append(f"role {name}: {source} — the privilege name of '{table}' is not known "
                                       f"offline (looked for {privilege_name(table, verb)}); plan compares it")
                else:
                    cmp.only_model.append(f"role {name}: {source}: {level}")
            elif found[1] != level:
                cmp.differs.append(f"role {name}: {source}: model={level}  solution={found[1]}")
        for _low, (pname, level) in sorted(sol_privs.items()):
            verb = next((api.lower() for _v, api in VERBS if pname.lower().startswith("prv" + api.lower())), None)
            if verb in unresolved_verbs:
                cmp.skipped.append(f"role {name}: {pname}: {level} — may be one of the table privileges above")
            else:
                cmp.only_solution.append(f"role {name}: {pname}: {level}")
    return cmp


def print_comparison(cmp):
    sections = (("Only in the solution", "+", cmp.only_solution, dvs.green),
                ("Only in the model", "-", cmp.only_model, dvs.red),
                ("Differs", "~", cmp.differs, dvs.yellow),
                ("In the solution, not declared in the model (informational)", "·", cmp.undeclared, dvs.grey),
                ("Not comparable offline (informational)", "!", cmp.skipped, dvs.grey))
    for title, mark, items, colour in sections:
        if items:
            print(dvs.bold(f"{title}:"))
            for item in items:
                print(colour(f"  {mark} {item}"))
            print()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def rel(path, base):
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def report(changes, kept, skipped, model_dir, show_diff=True):
    pending = 0
    for path, old, new in sorted(changes, key=lambda c: str(c[0])):
        name = rel(path, model_dir)
        if old is None:
            pending += 1
            print(dvs.green(f"+ {name}"))
        elif old != new:
            pending += 1
            print(dvs.yellow(f"~ {name}"))
            if show_diff:
                for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2):
                    if line.startswith(("---", "+++")):
                        continue
                    print("    " + (dvs.green(line) if line.startswith("+") else
                                    dvs.red(line) if line.startswith("-") else dvs.grey(line)))
        else:
            print(dvs.grey(f"= {name}"))
    if kept:
        print(dvs.bold("\nOnly in the model (kept):"))
        for where, what in kept:
            print(f"  · {where}: {what}")
    if skipped:
        print(dvs.bold("\nIn the solution but not readable (skipped):"))
        for where, why in skipped:
            print(f"  · {where}: {why}")
    return pending


def refuse_inside_solution(paths, sol_dirs):
    """Unpacked solutions are read only: refuse any write target inside one. True when refused."""
    for path in paths:
        folder = solution_folder_of(path, sol_dirs)
        if folder is not None:
            dvs.print_status("refused", reason=f"{path} is inside the unpacked solution {folder} — "
                                                "an unpacked solution is read only",
                             fix="pass a --model folder outside every unpacked solution")
            return True
    return False


def import_command(args, write):
    """The import of the same solutions, every path absolute."""
    return dvs.command_line(__file__, "import", *[Path(d).resolve() for d in args.solutions],
                            "--model", Path(args.model).resolve(),
                            *(["--roles", args.roles] if args.roles else []), *(["--write"] if write else []))


def cmd_import(args):
    model_dir = Path(args.model)
    if refuse_inside_solution([model_dir], args.solutions):
        return 1
    try:
        changes, kept, skipped = build(args.solutions, model_dir, _csv(args.roles))
    except ImportError_ as e:
        dvs.print_status("error", reason=str(e), fix="fix what the reason names, then run the import again")
        return 1
    if refuse_inside_solution([c[0] for c in changes], args.solutions):
        return 1
    pending = report(changes, kept, skipped, model_dir, show_diff=not args.no_diff)
    result = validate_result(model_dir, changes)
    for w in result.warnings:
        print(dvs.yellow("  ⚠ ") + w)
    if result.errors:
        for e in result.errors:
            print(dvs.red("  ✗ ") + e, file=sys.stderr)
        print(dvs.red(f"\nThe imported model would be invalid ({len(result.errors)} error(s)) — nothing written."),
              file=sys.stderr)
        dvs.print_status("error", reason="the imported model would be invalid — nothing written",
                         fix="fix the errors listed above, then run the import again")
        return 1
    if not pending:
        print(dvs.green("\nThe model already matches the solution."))
        dvs.print_status("no-changes")
        return 0
    if not args.write:
        print(dvs.bold(f"\n{pending} file(s) to write — review, then run again with --write."))
        dvs.print_status("changes-pending", next_cmd=import_command(args, write=True))
        return 2 if args.detailed_exitcode else 0
    for path, old, new in changes:
        if old != new:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
    print(dvs.green(f"\nWritten: {pending} file(s). Next: plan."))
    dvs.print_status("written", next_cmd=dvs.command_line(dvs.__file__, "plan", model_dir.resolve()))
    return 0


def cmd_compare(args):
    model_dir = Path(args.model)
    try:
        cmp = compare(args.solutions, model_dir, _csv(args.roles))
    except ImportError_ as e:
        dvs.print_status("error", reason=str(e), fix="fix what the reason names, then run compare again")
        return 1
    print_comparison(cmp)
    if not cmp.drift:
        print(dvs.green("The model and the solution describe the same roles."))
        dvs.print_status("in-sync")
        return 0
    dvs.print_status(
        "drift", reason=f"{cmp.drift} difference(s) between the model and the solution",
        fix="to align the model on the solution, review the import below; to align the solution on the "
            "model, plan and apply with dvac-security, then export and unpack again (dataverse:dv-solution) "
            "— never edit the unpacked solution",
        next_cmd=import_command(args, write=False))
    return 2 if args.detailed_exitcode else 0


def _csv(value):
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dv_security_solution.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("import", "align the role files with one or more unpacked solutions"),
                        ("compare", "compare the role files with unpacked solutions (writes nothing)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("solutions", nargs="+", help="unpacked solution folder(s) (holding Other/Solution.xml)")
        p.add_argument("--model", default="dataverse/security", help="model dir (default: dataverse/security)")
        p.add_argument("--roles", help="comma-separated role names (default: all)")
        p.add_argument("--detailed-exitcode", action="store_true",
                       help="CI: exit 2 when " + ("files would change" if name == "import" else
                                                  "the model and the solution differ") + " (default: 0)")
        if name == "import":
            p.add_argument("--write", action="store_true", help="write the new and changed files")
            p.add_argument("--no-diff", action="store_true", help="list the files without their diff")
    args = ap.parse_args(argv)
    dvs.ensure_deps(dvs.REQUIREMENTS)
    return {"import": cmd_import, "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
