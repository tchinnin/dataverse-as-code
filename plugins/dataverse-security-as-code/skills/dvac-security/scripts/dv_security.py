#!/usr/bin/env python3
"""
dv_security.py — Dataverse security roles as code
=================================================

Reads a model described in ``*.dv.security.yaml`` files::

    dataverse/security/
    ├── _meta.dv.security.yaml          # format version, solution
    └── roles/<file>.dv.security.yaml   # one security role per file

and reconciles it with a Dataverse **dev** environment, Terraform style:

    doctor              check tooling, az session, dev guard, solution
    validate <path>     offline structural + semantic validation
    plan <model-dir>    read-only diff: create / update / grant / depth / revoke /
                        blocked / unmanaged
    apply <model-dir> --plan-file <f>
                        execute exactly the saved plan (aborts if stale)

A role file lists the role's complete privilege set: a privilege the role holds on
the server but that the file doesn't list is planned as "- revoke" (double
confirmation). Roles are never deleted: a role of the solution that the model
doesn't describe is reported as "unmanaged" and left untouched.

Authentication: Azure CLI token (``az login``). Target: the dev environment given
by ``--env-url/--stage/--tenant``, else the ``DATAVERSE_URL/ENVIRONMENT_STAGE/TENANT_ID``
environment variables, else ``<repo-root>/.env.dev``. The script refuses to run
unless the target is declared as stage ``dev`` and holds the target solution as
unmanaged.

Exit codes:
    0  no changes (plan) / success (validate, doctor, apply)
    1  error, validation failure, guard failure, or blocked changes
    2  plan has pending changes

Do not edit this script per project — edit the model files.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # reported by doctor / validate
    yaml = None

try:
    import jsonschema
except ImportError:
    jsonschema = None

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = SKILL_DIR / "references" / "schemas"
REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

EXT = ".dv.security.yaml"
META_FILE = "_meta" + EXT
ROLES_DIR = "roles"
PLAN_DIR = ".dv-security"
PLAN_VERSION = 1
API_VERSION = "v9.2"
# Associates a created solution component with a solution (documented header name).
SOLUTION_HEADER = "MSCRM.SolutionUniqueName"
ROLE_COMPONENT_TYPE = 20

# Access level: model vocabulary (the security role editor's labels) <-> PrivilegeDepth.
DEPTH_TO_API = {"user": "Basic", "business_unit": "Local", "parent_child": "Deep",
                "organization": "Global"}
DEPTH_FROM_API = {v: k for k, v in DEPTH_TO_API.items()}
DEPTH_FROM_NUMBER = {0: "user", 1: "business_unit", 2: "parent_child", 3: "organization"}
# The privilege flag that allows each level (privilege table: canbebasic, …).
DEPTH_FLAG = {"user": "basic", "business_unit": "local", "parent_child": "deep",
              "organization": "global"}
DEPTH_ORDER = ("user", "business_unit", "parent_child", "organization")
# Table privilege verb in the model -> SecurityPrivilegeMetadata.PrivilegeType
VERB_TO_TYPE = {"create": "Create", "read": "Read", "write": "Write", "delete": "Delete",
                "append": "Append", "append_to": "AppendTo", "assign": "Assign", "share": "Share"}
INHERITANCE_TO_API = {"team_only": 0, "direct_and_team": 1}
INHERITANCE_FROM_API = {v: k for k, v in INHERITANCE_TO_API.items()}

# Phase order used to sort plan actions.
PHASE = {"create": 0, "update": 1, "grant": 2, "depth": 2, "revoke": 3}

class GuardError(Exception):
    """Raised when the dev-only guard refuses to run."""


class ApiError(Exception):
    def __init__(self, status, code, message, path):
        super().__init__(f"{status} {code or ''} {path}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def green(t): return _c("92", t)
def yellow(t): return _c("93", t)
def red(t): return _c("91", t)
def grey(t): return _c("90", t)
def bold(t): return _c("1", t)


# --------------------------------------------------------------------------- #
# Validation (offline)
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, where, msg):
        self.errors.append(f"[{where}] {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"[{where}] {msg}")


# --------------------------------------------------------------------------- #
# Dev-only guard
# --------------------------------------------------------------------------- #
ENV_KEYS = ("DATAVERSE_URL", "ENVIRONMENT_STAGE", "TENANT_ID")
ENV_HELP = ("provide the dev environment with --env-url <url> --stage dev [--tenant <id>], "
            "or the DATAVERSE_URL / ENVIRONMENT_STAGE=dev / TENANT_ID environment variables, "
            "or a <repo-root>/.env.dev file with the same keys")


def find_repo_root(start: Path) -> Path:
    """Nearest folder holding .env.dev or .git; the current directory otherwise."""
    p = start.resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".env.dev").is_file() or (candidate / ".git").exists():
            return candidate
    return Path.cwd().resolve()


def read_dotenv(path: Path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        values[k] = v.strip("'\"")
    return values


def _norm_url(u):
    return (u or "").strip().lower().rstrip("/")


def add_env_args(parser):
    g = parser.add_argument_group("dev environment (overrides environment variables and .env.dev)")
    g.add_argument("--env-url", help="Dataverse URL of the dev environment")
    g.add_argument("--stage", help="declared stage of that environment — must be 'dev'")
    g.add_argument("--tenant", help="Entra tenant id used for the az token")


def env_inputs(args):
    return {"url": getattr(args, "env_url", None), "stage": getattr(args, "stage", None),
            "tenant": getattr(args, "tenant", None)}


def _env_source(repo_root: Path, url=None, stage=None, tenant=None):
    """The first source that names a URL wins; URL and stage never mix sources."""
    if url or stage:
        return "command-line flags", {"DATAVERSE_URL": url, "ENVIRONMENT_STAGE": stage, "TENANT_ID": tenant}
    if os.environ.get("DATAVERSE_URL") or os.environ.get("ENVIRONMENT_STAGE"):
        return "environment variables", {k: os.environ.get(k) for k in ENV_KEYS}
    env_path = repo_root / ".env.dev"
    if env_path.is_file():
        return str(env_path), read_dotenv(env_path)
    raise GuardError("no dev environment given — " + ENV_HELP)


def dev_environment(repo_root: Path, url=None, stage=None, tenant=None):
    """Local half of the guard: one source, declared stage=dev, URL distinct from uat/prod."""
    source, env = _env_source(repo_root, url, stage, tenant)
    if (env.get("ENVIRONMENT_STAGE") or "").lower() != "dev":
        raise GuardError(f"the target must be declared as stage 'dev' (found "
                         f"'{env.get('ENVIRONMENT_STAGE') or ''}' in {source}) — refusing to run")
    url = _norm_url(env.get("DATAVERSE_URL"))
    if not url.startswith("https://"):
        raise GuardError(f"DATAVERSE_URL=https://<org>.crm<n>.dynamics.com is missing in {source}")
    for other in (".env.uat", ".env.prod"):
        op = repo_root / other
        if op.is_file() and _norm_url(read_dotenv(op).get("DATAVERSE_URL")) == url:
            raise GuardError(f"the dev URL from {source} is the same as in {other} — refusing to run")
    return {"url": url, "tenant": tenant or env.get("TENANT_ID") or None, "source": source}


# --------------------------------------------------------------------------- #
# Azure CLI auth + Web API client
# --------------------------------------------------------------------------- #
def az_path():
    return shutil.which("az") or shutil.which("az.cmd")


def az_json(args):
    exe = az_path()
    if not exe:
        raise GuardError("Azure CLI (az) not found — install it, then run: az login")
    proc = subprocess.run([exe, *args, "-o", "json"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GuardError("az " + " ".join(args) + " failed: " + (proc.stderr.strip() or proc.stdout.strip())
                         + "\n  → run: az login" + (" --tenant <TENANT_ID>" if "--tenant" in args else ""))
    return json.loads(proc.stdout or "null")


def _debug(method, path, status, started):
    """DV_SECURITY_DEBUG=1 prints every Web API call with its duration (stderr)."""
    if os.environ.get("DV_SECURITY_DEBUG"):
        print(f"    [{(time.monotonic() - started) * 1000:7.0f} ms] {status} {method} {path[:160]}",
              file=sys.stderr, flush=True)


class Api:
    def __init__(self, env, strong=False):
        # strong=True adds "Consistency: Strong" to every request: reads bypass the
        # metadata cache and see changes made just before (apply + post-apply check).
        self.strong = strong
        self.url = env["url"]
        self.tenant = env["tenant"]
        self.base = f"{self.url}/api/data/{API_VERSION}"
        self._token = None
        self._expires = 0.0

    def token(self):
        if self._token and time.time() < self._expires - 120:
            return self._token
        args = ["account", "get-access-token", "--resource", self.url]
        if self.tenant:
            args += ["--tenant", self.tenant]
        data = az_json(args)
        self._token = data["accessToken"]
        exp = data.get("expires_on")
        self._expires = float(exp) if exp else time.time() + 1800
        return self._token

    def _headers(self, extra=None):
        h = {
            "Authorization": f"Bearer {self.token()}",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "User-Agent": "dvac-security",
        }
        if self.strong:
            h["Consistency"] = "Strong"
        h.update(extra or {})
        return h

    def _url(self, path):
        if path.startswith("http"):
            return path
        if "?" in path:
            p, q = path.split("?", 1)
            path = p + "?" + urllib.parse.quote(q, safe="=&$(),'/;")
        return f"{self.base}/{path}"

    def request(self, method, path, body=None, headers=None, return_id=False, fatal=()):
        """Send a Web API request. With return_id, return the MetadataId/GUID from the
        OData-EntityId response header instead of the body (no read-after-write).
        Error codes in fatal fail at once, even when they are usually transient."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(6):
            req = urllib.request.Request(self._url(path), data=data, method=method,
                                         headers=self._headers(headers))
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read().decode("utf-8")
                    _debug(method, path, resp.status, started)
                    if return_id:
                        entity = resp.headers.get("OData-EntityId") or ""
                        return entity.rsplit("(", 1)[-1].rstrip(")") if "(" in entity else None
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                _debug(method, path, e.code, started)
                code, msg = None, raw
                try:
                    err = json.loads(raw).get("error", {})
                    code, msg = err.get("code"), err.get("message", raw)
                except ValueError:
                    pass
                low = (msg or "").lower()
                # "Not found" (404, e.g. 0x80060888) is an answer, never a transient error:
                # retrying it would stall every plan on components that don't exist yet.
                transient = e.code != 404 and (code or "").lower() not in fatal and (
                    e.code == 429
                    or code in ("0x80072560", "0x80040216", "0x80060891")
                    or ("another" in low and ("customization" in low or "running" in low))
                )
                if transient and attempt < 5:
                    wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                    print(grey(f"    … transient error {code or e.code}, retrying in {wait}s"), flush=True)
                    time.sleep(wait)
                    continue
                raise ApiError(e.code, code, msg, path) from None
        raise ApiError(0, None, "retries exhausted", path)

    def get(self, path):
        return self.request("GET", path)

    def get_all(self, path):
        items, data = [], self.get(path)
        while True:
            items.extend(data.get("value", []))
            nxt = data.get("@odata.nextLink")
            if not nxt:
                return items
            data = self.get(nxt)

    def get_or_none(self, path):
        try:
            return self.get(path)
        except ApiError as e:
            if e.status == 404:
                return None
            raise


def check_solution(api, solution):
    """Server half of the guard: the solution exists and is unmanaged."""
    rows = api.get_all("solutions?$select=solutionid,uniquename,ismanaged"
                       f"&$filter=uniquename eq '{solution}'")
    if not rows:
        raise GuardError(f"solution '{solution}' not found in {api.url} — create it first "
                         f"(official skill: dataverse:dv-solution)")
    if rows[0].get("ismanaged"):
        raise GuardError(f"solution '{solution}' is MANAGED in {api.url} — this is not a dev "
                         f"environment. Metadata changes are only allowed on an unmanaged dev solution.")
    return rows[0]["solutionid"]



# --------------------------------------------------------------------------- #
# Model loading
# --------------------------------------------------------------------------- #
def _require_yaml():
    if yaml is None:
        raise SystemExit(f"ERROR: PyYAML is not installed — pip install -r {REQUIREMENTS}")


def load_yaml(path: Path):
    _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_model_dir(start: Path):
    """Return the folder holding _meta.dv.security.yaml for a dir or file path."""
    p = start.resolve()
    if p.is_file():
        p = p.parent
    for candidate in [p, *p.parents][:4]:
        if (candidate / META_FILE).is_file():
            return candidate
    return None


def role_files(model_dir: Path):
    rdir = model_dir / ROLES_DIR
    return sorted(rdir.glob("*" + EXT)) if rdir.is_dir() else []


def role_key(name):
    """Dataverse compares role names without regard to case."""
    return (name or "").strip().lower()


def load_model(model_dir: Path):
    meta = load_yaml(model_dir / META_FILE) or {}
    roles = []
    for rf in role_files(model_dir):
        data = load_yaml(rf)
        if isinstance(data, dict):
            roles.append({"file": f"{ROLES_DIR}/{rf.name}", "spec": data})
    return {"meta": meta, "roles": roles}


def declared_privileges(spec):
    """Every privilege a role file grants: (source, table, verb, privilege, level). 'none' is skipped."""
    out = []
    for table, verbs in sorted((spec.get("tables") or {}).items()):
        for verb, level in sorted((verbs or {}).items()):
            if level != "none":
                out.append((f"{table}.{verb}", table.lower(), verb, None, level))
    for name, level in sorted((spec.get("privileges") or {}).items()):
        if level != "none":
            out.append((name, None, None, name, level))
    return out


# --------------------------------------------------------------------------- #
# Validation (offline)
# --------------------------------------------------------------------------- #
def _structural(report, data, schema_name, where):
    if jsonschema is None:
        report.warn(where, "jsonschema not installed — structural validation skipped")
        return
    with open(SCHEMAS_DIR / schema_name, "r", encoding="utf-8") as f:
        schema = json.load(f)
    validator = jsonschema.Draft7Validator(schema)
    for e in sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.absolute_path))):
        field = " > ".join(str(p) for p in e.absolute_path) or "(root)"
        report.error(where, f"{e.message} (at {field})")


def validate_role(r, data, where):
    _structural(r, data, "role.schema.json", where)
    name = data.get("name")
    if isinstance(name, str) and name != name.strip():
        r.error(where, "name must not start or end with spaces")
    seen = {}
    for prv in data.get("privileges") or {}:
        low = str(prv).lower()
        if low in seen:
            r.error(where, f"privilege '{prv}' is listed twice ('{seen[low]}' — names are case-insensitive)")
        seen[low] = prv
    tables = {}
    for table in data.get("tables") or {}:
        low = str(table).lower()
        if low in tables:
            r.error(where, f"table '{table}' is listed twice ('{tables[low]}')")
        tables[low] = table


def validate_model(model_dir: Path) -> Report:
    r = Report()
    meta = load_yaml(model_dir / META_FILE)
    if not isinstance(meta, dict):
        r.error(META_FILE, "root must be a YAML mapping")
        return r
    _structural(r, meta, "meta.schema.json", META_FILE)
    names = {}
    for rf in role_files(model_dir):
        where = f"{ROLES_DIR}/{rf.name}"
        data = load_yaml(rf)
        if not isinstance(data, dict):
            r.error(where, "root must be a YAML mapping")
            continue
        validate_role(r, data, where)
        key = role_key(data.get("name")) if isinstance(data.get("name"), str) else ""
        if key:
            if key in names:
                r.error(where, f"role '{data['name']}' is already described in {names[key]} "
                               f"(role names are unique, case-insensitive)")
            else:
                names[key] = where
    stray = sorted(p.name for p in model_dir.glob("*" + EXT) if p.name != META_FILE)
    for name in stray:
        r.error(name, f"role files belong in {ROLES_DIR}/ — this file is ignored by plan")
    return r


def validate_path(path: Path) -> Report:
    model_dir = find_model_dir(path)
    if model_dir is not None:
        return validate_model(model_dir)
    r = Report()
    data = load_yaml(path)
    name = path.name
    if not isinstance(data, dict):
        r.error(name, "root must be a YAML mapping")
        return r
    if "solution" in data:
        _structural(r, data, "meta.schema.json", name)
    elif "name" in data:
        validate_role(r, data, name)
    else:
        r.error(name, "unrecognised file (neither _meta nor a role)")
    r.warn(name, f"no {META_FILE} found above this file — cross-file checks skipped")
    return r


# --------------------------------------------------------------------------- #
# Server state (normalised to the model vocabulary)
# --------------------------------------------------------------------------- #
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


def api_depth(value):
    if isinstance(value, int):
        return DEPTH_FROM_NUMBER.get(value, str(value))
    return DEPTH_FROM_API.get(str(value), str(value))


def _levels(row, metadata=False):
    """Access levels a privilege accepts, from its CanBe* (metadata) or canbe* (privilege row) flags."""
    key = (lambda f: "CanBe" + f.capitalize()) if metadata else (lambda f: "canbe" + f)
    return [lvl for lvl in DEPTH_ORDER if row.get(key(DEPTH_FLAG[lvl]))]


def root_business_unit(api):
    rows = api.get_all("businessunits?$select=businessunitid,name&$filter=_parentbusinessunitid_value eq null")
    if not rows:
        raise RuntimeError("root business unit not found")
    return rows[0]["businessunitid"]


def role_privileges(api, role_id):
    """{privilege id: {name, depth}} held by a role. Record-filter privileges are left out."""
    resp = api.get(f"roles({role_id})/Microsoft.Dynamics.CRM.RetrieveRolePrivilegesRole()") or {}
    privs = {}
    for p in resp.get("RolePrivileges") or []:
        if (p.get("RecordFilterId") or EMPTY_GUID) != EMPTY_GUID:
            continue
        privs[p["PrivilegeId"].lower()] = {"name": p.get("PrivilegeName"), "depth": api_depth(p.get("Depth"))}
    for pid, p in privs.items():
        if not p["name"]:
            row = api.get_or_none(f"privileges({pid})?$select=name") or {}
            p["name"] = row.get("name") or pid
    return privs


def read_server(api, model, solution_id):
    bu = root_business_unit(api)
    roles = {}
    for row in api.get_all("roles?$select=roleid,name,description,isinherited,ismanaged,iscustomizable"
                           f"&$filter=_businessunitid_value eq {bu}"):
        roles.setdefault(role_key(row.get("name")), []).append({
            "id": row["roleid"].lower(), "name": row.get("name"),
            "description": row.get("description") or "",
            "member_inheritance": INHERITANCE_FROM_API.get(row.get("isinherited")),
            "managed": bool(row.get("ismanaged")),
            "customizable": (row.get("iscustomizable") or {}).get("Value", True),
            "privileges": None,
        })
    solution_roles = {r["objectid"].lower() for r in api.get_all(
        "solutioncomponents?$select=objectid"
        f"&$filter=_solutionid_value eq {solution_id} and componenttype eq {ROLE_COMPONENT_TYPE}")}

    table_names, prv_names = set(), set()
    for role in model["roles"]:
        matches = roles.get(role_key(role["spec"].get("name"))) or []
        if len(matches) == 1:
            matches[0]["privileges"] = role_privileges(api, matches[0]["id"])
        for _src, table, _verb, prv, _lvl in declared_privileges(role["spec"]):
            if table:
                table_names.add(table)
            else:
                prv_names.add(prv.lower())

    tables = {}
    for t in sorted(table_names):
        row = api.get_or_none(f"EntityDefinitions(LogicalName='{t}')?$select=LogicalName,Privileges")
        tables[t] = None if row is None else {
            p["PrivilegeType"]: {"id": p["PrivilegeId"].lower(), "name": p["Name"], "levels": _levels(p, metadata=True)}
            for p in row.get("Privileges") or []}
    named = {}
    ordered = sorted(prv_names)
    for i in range(0, len(ordered), 15):
        flt = " or ".join(f"name eq '{n}'" for n in ordered[i:i + 15])
        for row in api.get_all("privileges?$select=privilegeid,name,canbebasic,canbelocal,canbedeep,canbeglobal"
                               f"&$filter={flt}"):
            named[row["name"].lower()] = {"id": row["privilegeid"].lower(), "name": row["name"],
                                          "levels": _levels(row)}
    return {"business_unit": bu, "roles": roles, "solution_roles": solution_roles,
            "tables": tables, "named": named}


# --------------------------------------------------------------------------- #
# Diff engine
# --------------------------------------------------------------------------- #
def _block(role, kind, name, reason):
    return {"kind": kind, "role": role, "name": name, "reason": reason}


def resolve_privileges(spec, server):
    """The role's desired privileges {id: {name, depth, source}} and what blocks them."""
    role = spec["name"]
    desired, blocked, missing_tables = {}, [], set()
    for src, table, verb, prv, level in declared_privileges(spec):
        if table:
            tmeta = server["tables"].get(table)
            if tmeta is None:
                if table not in missing_tables:
                    missing_tables.add(table)
                    blocked.append(_block(role, "table", table, "table not found in the environment"))
                continue
            p = tmeta.get(VERB_TO_TYPE[verb])
            if p is None:
                blocked.append(_block(role, "privilege", src, f"table '{table}' has no {verb} privilege"))
                continue
        else:
            p = server["named"].get(prv.lower())
            if p is None:
                blocked.append(_block(role, "privilege", src, "privilege not found in the environment"))
                continue
        if p["id"] in desired:
            blocked.append(_block(role, "privilege", src,
                                  f"{p['name']} is already granted by {desired[p['id']]['source']}"))
            continue
        if level not in p["levels"]:
            blocked.append(_block(role, "privilege", src,
                                  f"{p['name']} does not accept level '{level}' "
                                  f"(allowed: {', '.join(p['levels']) or 'none'})"))
            continue
        desired[p["id"]] = {"name": p["name"], "depth": level, "source": src}
    return desired, blocked


def diff(model, server):
    actions, blocked, unmanaged = [], [], []
    solution = model["meta"]["solution"]
    modeled = set()
    for role in model["roles"]:
        spec = role["spec"]
        name = spec["name"]
        modeled.add(role_key(name))
        desired, blk = resolve_privileges(spec, server)
        blocked += blk
        matches = server["roles"].get(role_key(name)) or []
        if len(matches) > 1:
            blocked.append(_block(name, "role", name, f"{len(matches)} roles have this name in the root "
                                                      "business unit — rename the duplicates manually"))
            continue
        if not matches:
            actions.append({
                "op": "create", "kind": "role", "name": name,
                "description": spec.get("description"), "member_inheritance": spec.get("member_inheritance"),
                "privileges": [{"id": pid, "name": p["name"], "depth": p["depth"], "source": p["source"]}
                               for pid, p in sorted(desired.items(), key=lambda kv: kv[1]["name"].lower())],
            })
            continue
        have = matches[0]
        if have["managed"] or not have["customizable"]:
            blocked.append(_block(name, "role", name, "role is managed or not customizable — this tool only "
                                                      "manages roles created in the dev environment"))
            continue
        changes = {}
        if "description" in spec and (spec.get("description") or "") != have["description"]:
            changes["description"] = [have["description"], spec.get("description") or ""]
        if "member_inheritance" in spec and spec["member_inheritance"] != have["member_inheritance"]:
            changes["member_inheritance"] = [have["member_inheritance"], spec["member_inheritance"]]
        if have["id"] not in server["solution_roles"]:
            changes["solution"] = ["absent", solution]
        if changes:
            actions.append({"op": "update", "kind": "role", "name": name, "role_id": have["id"],
                            "changes": changes})
        current = have["privileges"] or {}
        for pid, want in desired.items():
            cur = current.get(pid)
            base = {"kind": "privilege", "role": name, "role_id": have["id"], "name": want["name"],
                    "privilege_id": pid, "source": want["source"]}
            if cur is None:
                actions.append({"op": "grant", **base, "depth": want["depth"]})
            elif cur["depth"] != want["depth"]:
                actions.append({"op": "depth", **base, "depth": want["depth"],
                                "changes": {"depth": [cur["depth"], want["depth"]]}})
        for pid, cur in current.items():
            if pid not in desired:
                actions.append({"op": "revoke", "kind": "privilege", "role": name, "role_id": have["id"],
                                "name": cur["name"], "privilege_id": pid, "depth": cur["depth"]})
    by_id = {r["id"]: r for rs in server["roles"].values() for r in rs}
    for rid in sorted(server["solution_roles"]):
        r = by_id.get(rid)
        if r and role_key(r["name"]) not in modeled:
            unmanaged.append({"kind": "role", "name": r["name"]})
    unmanaged.sort(key=lambda u: u["name"].lower())
    actions.sort(key=lambda a: (PHASE[a["op"]], role_key(a.get("role") or a["name"]), a["name"].lower()))
    return {"actions": actions, "blocked": blocked, "unmanaged": unmanaged}


def double_confirmation(plan):
    return [f"revoke {a['role']}: {a['name']} (was {a['depth']})" for a in plan["actions"] if a["op"] == "revoke"]


# --------------------------------------------------------------------------- #
# Plan rendering
# --------------------------------------------------------------------------- #
def _fmt(v):
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else f'"{v}"'


def render_plan(plan):
    print(bold("Dataverse security plan"))
    print(f"  env      : {plan['env_url']} ({plan['env_source']})")
    print(f"  solution : {plan['solution']} (unmanaged)")
    if plan.get("az_account"):
        print(f"  az user  : {plan['az_account']}")
    acts = plan["actions"]
    if acts:
        print()
    for a in acts:
        op = a["op"]
        if op == "create":
            print(f"  {green('+ create')}  role       {a['name']}  {grey(str(len(a['privileges'])) + ' privilege(s)')}")
            for f in ("description", "member_inheritance"):
                if a.get(f) is not None:
                    print(f"              {f}: {_fmt(a[f])}")
            for p in a["privileges"]:
                print(f"              {p['name']}: {p['depth']}" + grey(f"  ({p['source']})"))
        elif op == "update":
            print(f"  {yellow('~ update')}  role       {a['name']}")
            for field, (old, new) in a["changes"].items():
                print(f"              {field}: {_fmt(old)} → {_fmt(new)}")
        elif op == "grant":
            print(f"  {green('+ grant ')}  privilege  {a['role']}: {a['name']}  {a['depth']}" + grey(f"  ({a['source']})"))
        elif op == "depth":
            old, new = a["changes"]["depth"]
            print(f"  {yellow('~ depth ')}  privilege  {a['role']}: {a['name']}  {old} → {new}" + grey(f"  ({a['source']})"))
        else:
            print(f"  {red('- revoke')}  privilege  {a['role']}: {a['name']}  " + grey(f"(was {a['depth']})"))
    if plan["blocked"]:
        print("\n" + red("Blocked — never applied; align the model with the server or change it manually:"))
        for b in plan["blocked"]:
            print(f"  {red('!')} {b['kind']:<9} {b['role']}: {b['name']}")
            print(grey(f"              {b['reason']}"))
    if plan["unmanaged"]:
        print("\n" + grey("Unmanaged — roles of the solution that the model doesn't describe (reported only, never deleted):"))
        for u in plan["unmanaged"]:
            print(f"  ? {u['kind']:<9} {u['name']}")
    count = {op: sum(1 for a in acts if a["op"] == op) for op in PHASE}
    print(f"\nSummary: {count['create']} role(s) to create, {count['update']} to update, "
          f"{count['grant']} grant(s), {count['depth']} level change(s), {count['revoke']} revoke(s), "
          f"{len(plan['blocked'])} blocked, {len(plan['unmanaged'])} unmanaged.")
    if count["revoke"]:
        print(yellow(f"{count['revoke']} revoke(s) need a double confirmation from the user."))
    if not acts and not plan["blocked"]:
        print(green("No changes — the environment matches the model."))


def _model_hash(model_dir: Path):
    h = hashlib.sha256()
    for f in [model_dir / META_FILE, *role_files(model_dir)]:
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _canonical(plan):
    return json.dumps({"env_url": plan["env_url"], "solution": plan["solution"],
                       "actions": plan["actions"], "blocked": plan["blocked"]},
                      sort_keys=True, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _validated_model(model_arg):
    model_dir = find_model_dir(Path(model_arg))
    if model_dir is None:
        raise SystemExit(f"ERROR: no {META_FILE} found at or above {model_arg}")
    report = validate_model(model_dir)
    if report.errors:
        for e in report.errors:
            print(red("  ✗ ") + e, file=sys.stderr)
        raise SystemExit(f"ERROR: the model is invalid ({len(report.errors)} error(s)) — run validate and fix it first")
    return model_dir, load_model(model_dir)


def compute_plan(model_arg, strong=False, env_args=None):
    model_dir, model = _validated_model(model_arg)
    env = dev_environment(find_repo_root(model_dir), **(env_args or {}))
    api = Api(env, strong=strong)
    account = az_json(["account", "show"])
    solution_id = check_solution(api, model["meta"]["solution"])
    state = read_server(api, model, solution_id)
    result = diff(model, state)
    plan = {
        "plan_version": PLAN_VERSION,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "env_url": env["url"], "env_source": env["source"],
        "az_account": f"{(account or {}).get('user', {}).get('name', '?')} (tenant {(account or {}).get('tenantId', '?')})",
        "solution": model["meta"]["solution"],
        "model_dir": str(model_dir), "model_hash": _model_hash(model_dir),
        **result,
    }
    plan["double_confirmation"] = double_confirmation(plan)
    return model_dir, model, env, api, state, plan


def plan_exit_code(plan):
    if plan["blocked"]:
        return 1
    return 2 if plan["actions"] else 0


def cmd_validate(args):
    report = validate_path(Path(args.path))
    for w in report.warnings:
        print(yellow("  ⚠ ") + w)
    for e in report.errors:
        print(red("  ✗ ") + e)
    if report.errors:
        print(red(f"\nInvalid — {len(report.errors)} error(s), {len(report.warnings)} warning(s)."))
        return 1
    print(green(f"Valid — {len(report.warnings)} warning(s)."))
    return 0


def cmd_plan(args):
    model_dir, _model, _env, _api, _state, plan = compute_plan(args.model, env_args=env_inputs(args))
    out = Path(args.out) if args.out else find_repo_root(model_dir) / PLAN_DIR / "plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps({**plan, "plan_file": str(out)}, indent=2, ensure_ascii=False, default=str))
    else:
        render_plan(plan)
        print(grey(f"\nPlan saved to {out}"))
        if plan["actions"] and not plan["blocked"]:
            print(grey(f"Apply it with: dv_security.py apply {args.model} --plan-file {out}"))
    return plan_exit_code(plan)


def cmd_doctor(args):
    ok = True

    def line(passed, label, fix=""):
        nonlocal ok
        print(("  " + green("✓") if passed else "  " + red("✗")) + f" {label}")
        if not passed:
            ok = False
            if fix:
                print(grey(f"      → {fix}"))

    print(bold("dv_security doctor"))
    line(sys.version_info >= (3, 9), f"Python ≥ 3.9 ({sys.version.split()[0]})", "install Python 3.9+")
    line(yaml is not None, "pyyaml installed", f"pip install -r {REQUIREMENTS}")
    line(jsonschema is not None, "jsonschema installed", f"pip install -r {REQUIREMENTS}")
    line(az_path() is not None, "Azure CLI (az) on PATH", "install the Azure CLI")
    model_dir = find_model_dir(Path(args.model)) if args.model else None
    try:
        root = find_repo_root(model_dir or Path.cwd())
        env = dev_environment(root, **env_inputs(args))
        line(True, f"dev guard: {env['source']} → {env['url']}")
    except GuardError as e:
        line(False, "dev guard", str(e))
        return 1
    try:
        account = az_json(["account", "show"])
        line(True, f"az session: {account.get('user', {}).get('name')} (tenant {account.get('tenantId')})")
        if env["tenant"] and account.get("tenantId", "").lower() != env["tenant"].lower():
            print(yellow(f"      ⚠ active az tenant differs from the configured TENANT_ID — the token is requested for {env['tenant']}"))
        api = Api(env)
        api.token()
        line(True, "access token for the dev environment")
        who = api.get("WhoAmI")
        line(True, f"WhoAmI: user {who.get('UserId')}")
        line(True, f"root business unit: {root_business_unit(api)}")
        if model_dir:
            sol = (load_yaml(model_dir / META_FILE) or {})["solution"]
            check_solution(api, sol)
            line(True, f"solution '{sol}' exists and is unmanaged")
    except (GuardError, ApiError, RuntimeError) as e:
        line(False, "Dataverse access", str(e))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
class Applier:
    def __init__(self, api, solution, business_unit):
        self.api = api
        self.solution = solution
        self.bu = business_unit

    def add_to_solution(self, role_id):
        self.api.request("POST", "AddSolutionComponent", {
            "ComponentId": role_id, "ComponentType": ROLE_COMPONENT_TYPE,
            "SolutionUniqueName": self.solution, "AddRequiredComponents": False,
            "DoNotIncludeSubcomponents": False,
        })

    def privileges(self, items):
        return [{"PrivilegeId": p.get("privilege_id") or p["id"], "Depth": DEPTH_TO_API[p["depth"]],
                 "BusinessUnitId": self.bu} for p in items]

    def create(self, a):
        body = {"name": a["name"], "businessunitid@odata.bind": f"businessunits({self.bu})"}
        if a.get("description") is not None:
            body["description"] = a["description"]
        if a.get("member_inheritance"):
            body["isinherited"] = INHERITANCE_TO_API[a["member_inheritance"]]
        role_id = self.api.request("POST", "roles", body, {SOLUTION_HEADER: self.solution}, return_id=True)
        if not role_id:
            raise RuntimeError("the role was created but Dataverse returned no id — re-run plan")
        self.add_to_solution(role_id)
        # A new role starts with a few default privileges: set its exact privilege set.
        self.api.request("POST", f"roles({role_id})/Microsoft.Dynamics.CRM.ReplacePrivilegesRole",
                         {"Privileges": self.privileges(a["privileges"])})

    def update(self, a):
        ch = a["changes"]
        body = {}
        if "description" in ch:
            body["description"] = ch["description"][1]
        if "member_inheritance" in ch:
            body["isinherited"] = INHERITANCE_TO_API[ch["member_inheritance"][1]]
        if body:
            self.api.request("PATCH", f"roles({a['role_id']})", body, {"If-Match": "*"})
        if "solution" in ch:
            self.add_to_solution(a["role_id"])

    def grant(self, batch):
        """Grants and level changes of one role, in one call (AddPrivilegesRole sets the level)."""
        self.api.request("POST", f"roles({batch[0]['role_id']})/Microsoft.Dynamics.CRM.AddPrivilegesRole",
                         {"Privileges": self.privileges(batch)})

    def revoke(self, a):
        self.api.request("POST", f"roles({a['role_id']})/Microsoft.Dynamics.CRM.RemovePrivilegeRole",
                         {"PrivilegeId": a["privilege_id"]})

    def run(self, batch):
        op = batch[0]["op"]
        if op == "create":
            self.create(batch[0])
        elif op == "update":
            self.update(batch[0])
        elif op in ("grant", "depth"):
            self.grant(batch)
        else:
            self.revoke(batch[0])


def batches(actions):
    """One batch per action, except consecutive grants / level changes of a role (one call)."""
    out = []
    for a in actions:
        if (a["op"] in ("grant", "depth") and out and out[-1][0]["op"] in ("grant", "depth")
                and out[-1][0]["role_id"] == a["role_id"]):
            out[-1].append(a)
        else:
            out.append([a])
    return out


def _label(a):
    return f"{a['op']:<7} {a['role']}: {a['name']}" if a.get("role") else f"{a['op']:<7} role {a['name']}"


def cmd_apply(args):
    saved = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if saved.get("plan_version") != PLAN_VERSION:
        print(red("ERROR: unsupported plan file version — re-run plan."), file=sys.stderr)
        return 1
    _model_dir, _model, _env, api, state, fresh = compute_plan(args.model, env_args=env_inputs(args))
    if _canonical(fresh) != _canonical(saved):
        print(red("ERROR: stale plan — the model or the server changed since it was computed.\n"
                  "Re-run plan, review it again, and apply the new plan file."), file=sys.stderr)
        return 1
    if fresh["blocked"]:
        print(red("ERROR: the plan contains blocked changes — fix the model, re-plan, then apply."), file=sys.stderr)
        return 1
    if not fresh["actions"]:
        print(green("No changes to apply."))
        return 0

    api.strong = True
    applier = Applier(api, fresh["solution"], state["business_unit"])
    done, failure = 0, None
    print(bold(f"Applying {len(fresh['actions'])} change(s) to {fresh['env_url']} ({fresh['solution']})"))
    for batch in batches(fresh["actions"]):
        try:
            applier.run(batch)
            done += len(batch)
            for a in batch:
                print(f"  {green('✓')} {_label(a)}", flush=True)
        except (ApiError, RuntimeError, KeyError) as e:
            failure = e
            for a in batch:
                print(f"  {red('✗')} {_label(a)}", flush=True)
            print(f"      {e}", flush=True)
            break

    print(f"\n{done}/{len(fresh['actions'])} change(s) applied.")
    if failure:
        print(red("Stopped at the first failure. Fix the cause, re-run plan, then apply the new plan."))
        return 1
    after = compute_plan(args.model, strong=True, env_args=env_inputs(args))[-1]
    if after["actions"] or after["blocked"]:
        print(red("Post-apply check FAILED — the environment still differs from the model "
                  "after apply (read with Consistency: Strong). Remaining differences:"))
        render_plan(after)
        return 1
    print(green("Post-apply plan is empty — the environment matches the model."))
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(prog="dv_security.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor", help="check tooling, az session, dev guard and solution")
    p.add_argument("model", nargs="?", help="model dir (enables the solution check)")
    add_env_args(p)
    p = sub.add_parser("validate", help="offline validation of the model or one file")
    p.add_argument("path")
    p = sub.add_parser("plan", help="read-only diff against the dev environment")
    p.add_argument("model")
    p.add_argument("--out", help=f"plan file (default <repo-root>/{PLAN_DIR}/plan.json)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    add_env_args(p)
    p = sub.add_parser("apply", help="execute a saved plan (aborts if stale)")
    p.add_argument("model")
    p.add_argument("--plan-file", required=True)
    add_env_args(p)
    args = ap.parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "validate": cmd_validate,
                "plan": cmd_plan, "apply": cmd_apply}[args.cmd](args)
    except GuardError as e:
        print(red(f"REFUSED: {e}"), file=sys.stderr)
        return 1
    except ApiError as e:
        print(red(f"Dataverse error: {e}"), file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(red(f"ERROR: {e}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
