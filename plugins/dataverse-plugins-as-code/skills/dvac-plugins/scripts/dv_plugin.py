#!/usr/bin/env python3
"""
dv_plugin.py — Dataverse plug-in registrations as code
=======================================================

Reads ``*.dv.plugin.yaml`` files, one per plug-in project, next to its .csproj::

    plugins/<PluginName>/<PluginName>.dv.plugin.yaml

Each file describes a plug-in package (recommended) or a plug-in assembly, its
steps and step images, and the Custom APIs it implements. The tool reconciles
them with a Dataverse **dev** environment, Terraform style:

    doctor [path]       check tooling, az + pac sessions, dev guard, solutions, build output
    validate <path>     offline structural + semantic validation
    plan <path>         read-only diff: create / update / push / delete / blocked / unmanaged
    apply <path> --plan-file <f>
                        execute exactly the saved plan (aborts if stale)
    export --solution <s> (--package <name> | --assembly <name>)
                        print the YAML of existing registrations (adoption)

The first registration uploads the package (or assembly) as base64 through the
Web API and records its id in the YAML. Later content changes go through
``pac plugin push --pluginId <id> --environment <dev url>``. Every component —
package, assemblies, steps, images, Custom APIs, request parameters and response
properties — is created in, or added to, the model's solution.

Deletes only steps and step images that are absent from the model. Packages,
assemblies, Custom APIs and their parameters are never deleted: they are
reported as "unmanaged".

Authentication: Azure CLI token (``az login``) for the Web API, the active PAC
CLI profile for ``pac plugin push``. Target: ``<repo-root>/.env.dev`` only.

Exit codes:
    0  no changes (plan) / success (validate, doctor, apply, export)
    1  error, validation failure, guard failure, or blocked changes
    2  plan has pending changes

Do not edit this script per project — edit the model files.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
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
SCHEMA_FILE = SKILL_DIR / "references" / "schemas" / "plugin.schema.json"
REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

EXT = ".dv.plugin.yaml"
PLAN_DIR = ".dv-plugin"
PLAN_VERSION = 1
API_VERSION = "v9.2"
# Associates a created component with a solution. The documented name is
# MSCRM.SolutionUniqueName — an unknown header is silently ignored by Dataverse and
# the component lands outside the solution.
SOLUTION_HEADER = "MSCRM.SolutionUniqueName"
SKIP_DIRS = {"bin", "obj", "node_modules", ".git", ".venv"}

STAGE_TO_API = {"pre-validation": 10, "pre-operation": 20, "post-operation": 40}
STAGE_FROM_API = {v: k for k, v in STAGE_TO_API.items()}
MODE_TO_API = {"sync": 0, "async": 1}
MODE_FROM_API = {v: k for k, v in MODE_TO_API.items()}
IMAGE_TYPE_TO_API = {"pre": 0, "post": 1, "both": 2}
IMAGE_TYPE_FROM_API = {v: k for k, v in IMAGE_TYPE_TO_API.items()}
# Message property an image is taken from, per message (lowercase). Other messages
# need an explicit message_property.
MESSAGE_PROPERTY = {
    "create": "Id", "update": "Target", "delete": "Target", "assign": "Target",
    "merge": "Target", "route": "Target", "setstate": "EntityMoniker",
    "setstatedynamicentity": "EntityMoniker", "send": "EmailId",
    "deliverincoming": "EmailId", "deliverpromote": "EmailId",
}
BINDING_TO_API = {"global": 0, "entity": 1, "entity-collection": 2}
BINDING_FROM_API = {v: k for k, v in BINDING_TO_API.items()}
ALLOWED_STEPS_TO_API = {"none": 0, "async-only": 1, "sync-and-async": 2}
ALLOWED_STEPS_FROM_API = {v: k for k, v in ALLOWED_STEPS_TO_API.items()}
PARAM_TYPE_TO_API = {
    "boolean": 0, "datetime": 1, "decimal": 2, "entity": 3, "entity-collection": 4,
    "entity-reference": 5, "float": 6, "integer": 7, "money": 8, "picklist": 9,
    "string": 10, "string-array": 11, "guid": 12,
}
PARAM_TYPE_FROM_API = {v: k for k, v in PARAM_TYPE_TO_API.items()}
ENTITY_PARAM_TYPES = {"entity", "entity-collection", "entity-reference"}

# Solution components managed by the tool. Plug-in types are not solution
# components: they travel with their assembly.
COMPONENT_TABLE = {
    "package": "pluginpackage", "assembly": "pluginassembly",
    "step": "sdkmessageprocessingstep", "image": "sdkmessageprocessingstepimage",
    "customapi": "customapi", "request": "customapirequestparameter",
    "response": "customapiresponseproperty",
}
# Fixed component type codes; the others are resolved at run time from
# solutioncomponentdefinitions, never hard-coded.
FIXED_COMPONENT_TYPE = {"pluginassembly": 91, "sdkmessageprocessingstep": 92,
                        "sdkmessageprocessingstepimage": 93}
STEP_FIELD_TO_API = {"configuration": "configuration", "description": "description",
                     "async_auto_delete": "asyncautodelete"}
IMAGE_FIELD_TO_API = {"alias": "entityalias", "message_property": "messagepropertyname"}
API_FIELD_TO_API = {"name": "name", "display_name": "displayname", "description": "description",
                    "is_private": "isprivate", "execute_privilege": "executeprivilegename"}
PARAM_FIELD_TO_API = {"name": "name", "display_name": "displayname", "description": "description"}

# Apply order. Deletes run first: a package update that removes a type still used
# by a step is rejected by Dataverse.
PHASES = [
    ("delete", "image"), ("delete", "step"),
    ("create", "package"), ("create", "assembly"), ("update", "package"), ("update", "assembly"),
    ("push", "package"), ("push", "assembly"), ("create", "plugintype"),
    ("create", "customapi"), ("update", "customapi"), ("create", "request"), ("update", "request"),
    ("create", "response"), ("update", "response"),
    ("create", "step"), ("update", "step"), ("create", "image"), ("update", "image"),
]
PHASE = {p: i for i, p in enumerate(PHASES)}
# Solution membership runs last, parents before children.
SOLUTION_ORDER = ["package", "assembly", "customapi", "request", "response", "step", "image"]


def phase_of(a):
    if a.get("component"):
        return len(PHASES) + SOLUTION_ORDER.index(a["kind"])
    return PHASE[(a["op"], a["kind"])]


# --- shared: dev guard + Web API client (keep identical with dvac-metadata/scripts/dv_metadata.py) ---
class GuardError(Exception):
    """Raised when the dev-only guard refuses to run."""


class ApiError(Exception):
    def __init__(self, status, code, message, path):
        super().__init__(f"{status} {code or ''} {path}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message


_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def green(t): return _c("92", t)
def yellow(t): return _c("93", t)
def red(t): return _c("91", t)
def grey(t): return _c("90", t)
def bold(t): return _c("1", t)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, where, msg):
        self.errors.append(f"[{where}] {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"[{where}] {msg}")


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".env.dev").is_file() or (candidate / ".git").exists():
            return candidate
    raise GuardError("cannot find the repository root (no .env.dev or .git above the model)")


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


def dev_environment(repo_root: Path):
    """Local half of the guard: .env.dev only, stage=dev, URL distinct from uat/prod."""
    env_path = repo_root / ".env.dev"
    if not env_path.is_file():
        raise GuardError(f"{env_path} not found — this tool only runs against the dev environment "
                         f"described in .env.dev (see ppbp-init)")
    env = read_dotenv(env_path)
    if (env.get("ENVIRONMENT_STAGE") or "").lower() != "dev":
        raise GuardError(f".env.dev must declare ENVIRONMENT_STAGE=dev (found "
                         f"'{env.get('ENVIRONMENT_STAGE', '')}') — refusing to run")
    url = _norm_url(env.get("DATAVERSE_URL"))
    if not url.startswith("https://"):
        raise GuardError(".env.dev must declare DATAVERSE_URL=https://<org>.crm<n>.dynamics.com")
    for other in (".env.uat", ".env.prod"):
        op = repo_root / other
        if op.is_file() and _norm_url(read_dotenv(op).get("DATAVERSE_URL")) == url:
            raise GuardError(f"DATAVERSE_URL in .env.dev is the same as in {other} — refusing to run")
    return {"url": url, "tenant": env.get("TENANT_ID") or None, "env_file": str(env_path)}


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
    """DV_PLUGIN_DEBUG=1 prints every Web API call with its duration (stderr)."""
    if os.environ.get("DV_PLUGIN_DEBUG"):
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
            "User-Agent": "dvac-plugins",
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

    def request(self, method, path, body=None, headers=None, return_id=False):
        """Send a Web API request. With return_id, return the MetadataId/GUID from the
        OData-EntityId response header instead of the body (no read-after-write)."""
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
                transient = e.code != 404 and (
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
# --- end shared ---


def cyan(t): return _c("96", t)


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


def _skipped(path: Path, base: Path):
    return any(part in SKIP_DIRS for part in path.relative_to(base).parts[:-1])


def find_files(path: Path):
    """The model files: the file itself, or every *.dv.plugin.yaml below a folder."""
    p = path.resolve()
    if p.is_file():
        return [p]
    if not p.is_dir():
        return []
    return [f for f in sorted(p.rglob("*" + EXT)) if not _skipped(f, p)]


def unit_kind(data):
    return "package" if "package" in data else "assembly"


def step_name(step):
    """Step identity. Default = the Plugin Registration Tool format, so existing
    registrations are adopted as-is."""
    if step.get("name"):
        return step["name"]
    base = f"{step['plugin']}: {step['message']}"
    return f"{base} of {step['table']}" if step.get("table") else base


def message_property(step, image):
    return image.get("message_property") or MESSAGE_PROPERTY.get(str(step.get("message", "")).lower())


def desired_image(step, image):
    return {"name": image["name"], "alias": image.get("alias", image["name"]), "type": image["type"],
            "attributes": sorted(a.lower() for a in image["attributes"]),
            "message_property": message_property(step, image)}


def desired_step(step):
    """Normalised step. Optional fields left out of the YAML are not managed."""
    d = {"name": step_name(step), "plugin": step["plugin"], "message": step["message"],
         "table": (step.get("table") or "").lower() or None, "stage": step["stage"],
         "mode": step.get("mode", "sync"), "rank": step.get("rank", 1),
         "filtering_attributes": sorted(a.lower() for a in step.get("filtering_attributes") or []),
         "enabled": step.get("enabled", True)}
    for field in STEP_FIELD_TO_API:
        if field in step:
            d[field] = step[field]
    d["images"] = {i["name"].lower(): desired_image(step, i) for i in step.get("images") or []}
    return d


def desired_param(pname, spec, response=False):
    d = {"uniquename": pname, "type": spec["type"], "table": (spec.get("table") or "").lower() or None}
    if not response:
        d["optional"] = bool(spec.get("optional", False))
    for field in PARAM_FIELD_TO_API:
        if field in spec:
            d[field] = spec[field]
    return d


def desired_api(uname, spec):
    d = {"uniquename": uname, "binding": spec["binding"],
         "bound_table": (spec.get("bound_table") or "").lower() or None,
         "is_function": bool(spec.get("is_function", False)),
         "allowed_steps": spec.get("allowed_steps", "none"),
         "workflow_enabled": bool(spec.get("workflow_enabled", False)),
         "display_name": spec["display_name"], "is_private": bool(spec.get("is_private", False))}
    for field in ("name", "description", "execute_privilege", "plugin"):
        if field in spec:
            d[field] = spec[field]
    d["request"] = {p.lower(): desired_param(p, s or {}) for p, s in (spec.get("request") or {}).items()}
    d["response"] = {p.lower(): desired_param(p, s or {}, response=True)
                     for p, s in (spec.get("response") or {}).items()}
    return d


# --------------------------------------------------------------------------- #
# Build output (local artifact)
# --------------------------------------------------------------------------- #
def nuspec_info(nupkg: Path):
    with zipfile.ZipFile(nupkg) as z:
        name = next((n for n in z.namelist() if n.endswith(".nuspec") and "/" not in n), None)
        if not name:
            raise ValueError(f"{nupkg.name}: no .nuspec at the package root")
        root = ET.fromstring(z.read(name))

    def find(tag):
        for el in root.iter():
            if el.tag.split("}")[-1] == tag:
                return (el.text or "").strip()
        return ""
    return {"id": find("id"), "version": find("version")}


def _version_key(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-+]", v or "0"))


def locate_artifact(unit_file: Path, data):
    """The build output the model points to, with its SHA-256 — None when not built yet."""
    kind = unit_kind(data)
    pattern = data[kind]["nupkg" if kind == "package" else "dll"]
    base = unit_file.parent
    if any(c in pattern for c in "*?["):
        matches = [m for m in base.glob(pattern) if m.is_file()]
    else:
        matches = [base / pattern] if (base / pattern).is_file() else []
    if not matches:
        return None
    if kind == "package":
        infos = {m: nuspec_info(m) for m in matches}
        best = max(matches, key=lambda m: (_version_key(infos[m]["version"]), m.stat().st_mtime))
        info = infos[best]
    else:
        best = max(matches, key=lambda m: m.stat().st_mtime)
        info = {"id": best.stem, "version": ""}
    return {"path": str(best), "sha256": hashlib.sha256(best.read_bytes()).hexdigest(),
            "version": info["version"], "nuspec_id": info["id"]}


# --------------------------------------------------------------------------- #
# Source scan (Python cannot reflect over a .NET assembly)
# --------------------------------------------------------------------------- #
CLASS_RE = re.compile(r"\bclass\s+(\w+)(?:\s*<[^>{]*>)?\s*(?::\s*([^{]+?))?\s*(?:where\b[^{]*)?\{", re.S)
NS_RE = re.compile(r"^\s*namespace\s+([\w.]+)\s*[;{]", re.M)


def scan_sources(folder: Path):
    """Map 'Namespace.Class' → base type names declared in the project's .cs files (best effort)."""
    classes = {}
    for f in sorted(folder.rglob("*.cs")):
        if _skipped(f, folder):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
        ns = NS_RE.search(text)
        for m in CLASS_RE.finditer(text):
            bases = [b.strip().split("<")[0].split(".")[-1] for b in (m.group(2) or "").split(",") if b.strip()]
            classes[f"{ns.group(1)}.{m.group(1)}" if ns else m.group(1)] = bases
    return classes


def plugin_classes(classes):
    """Classes that reach PluginBase or IPlugin through their base types."""
    by_short = {}
    for full in classes:
        by_short.setdefault(full.rsplit(".", 1)[-1], []).append(full)

    def is_plugin(full, seen):
        for b in classes.get(full, []):
            if b in ("IPlugin", "PluginBase"):
                return True
            if any(c not in seen and is_plugin(c, seen | {c}) for c in by_short.get(b, [])):
                return True
        return False
    return {full for full in classes if is_plugin(full, {full})}


# --------------------------------------------------------------------------- #
# Validation (offline)
# --------------------------------------------------------------------------- #
def _structural(report, data, where):
    if jsonschema is None:
        report.warn(where, "jsonschema not installed — structural validation skipped")
        return
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        field = " > ".join(str(p) for p in e.absolute_path) or "(root)"
        message = e.message
        if e.validator == "oneOf" and not e.absolute_path:
            message = "declare exactly one of 'package' or 'assembly'"
        report.error(where, f"{message} (at {field})")


def validate_unit(path: Path, r: Report):
    """Validate one file. Returns its data when it is well-formed, else None."""
    where = path.name
    data = load_yaml(path)
    if not isinstance(data, dict):
        r.error(where, "root must be a YAML mapping")
        return None
    before = len(r.errors)
    _structural(r, data, where)
    if len(r.errors) > before:
        return None  # semantic checks assume a well-formed file
    prefix = data["publisher"]["prefix"].lower()
    kind = unit_kind(data)
    spec = data[kind]
    if not path.name.endswith(EXT):
        r.error(where, f"model files must end with {EXT}")
    if kind == "assembly":
        r.warn(where, "assembly registration — prefer a plug-in package; keep assemblies for existing projects")
    elif not spec["name"].lower().startswith(prefix + "_"):
        r.error(where, f"package name must start with the publisher prefix '{prefix}_'")

    try:
        art = locate_artifact(path, data)
    except (zipfile.BadZipFile, ValueError, OSError, ET.ParseError) as e:
        art = None
        r.error(where, f"cannot read the build output: {e}")
    if art is None:
        r.warn(where, f"no build output matches '{spec.get('nupkg') or spec.get('dll')}' yet — "
                      "build the project before planning")
    elif kind == "package" and art["nuspec_id"] and spec["name"].lower() != f"{prefix}_{art['nuspec_id']}".lower():
        r.warn(where, f"package name '{spec['name']}' differs from '{prefix}_{art['nuspec_id']}' "
                      "(publisher prefix + NuGet package id)")

    classes = scan_sources(path.parent)
    plugins = plugin_classes(classes)

    def check_plugin(w, typename):
        if not classes:
            return  # no sources next to the file: nothing to check against
        if typename not in classes:
            r.error(w, f"plugin type '{typename}' is not a class of this project "
                       "(Namespace.ClassName, case-sensitive)")
        elif typename not in plugins:
            r.warn(w, f"'{typename}' does not derive from PluginBase or implement IPlugin "
                      "(as far as the source scan can tell)")

    apis = {k.lower(): v or {} for k, v in (data.get("custom_apis") or {}).items()}
    seen_steps = set()
    for i, step in enumerate(data.get("steps") or []):
        sname = step_name(step)
        w = f"{where}:steps[{i}] {sname}"
        if sname.lower() in seen_steps:
            r.error(w, "duplicate step name — set an explicit, unique 'name' "
                       "(two steps share plugin, message and table)")
        seen_steps.add(sname.lower())
        check_plugin(w, step["plugin"])
        msg = step["message"].lower()
        stage, mode = step["stage"], step.get("mode", "sync")
        if mode == "async" and stage != "post-operation":
            r.error(w, "async steps run only in post-operation")
        if msg == "update" and not step.get("filtering_attributes"):
            r.error(w, "an Update step without filtering_attributes fires on every update — "
                       "list the columns that trigger it")
        if step.get("filtering_attributes") and msg != "update":
            r.warn(w, "filtering_attributes are used only by Update steps")
        if "async_auto_delete" in step and mode != "async":
            r.error(w, "async_auto_delete applies only to async steps")
        api = apis.get(msg)
        if api is not None:
            allowed = api.get("allowed_steps", "none")
            if allowed == "none":
                r.error(w, f"Custom API '{step['message']}' allows no custom processing steps (allowed_steps: none)")
            elif allowed == "async-only" and mode != "async":
                r.error(w, f"Custom API '{step['message']}' allows async steps only")
        seen_images = set()
        for img in step.get("images") or []:
            iw = f"{w} image {img['name']}"
            if img["name"].lower() in seen_images:
                r.error(iw, "duplicate image name in this step")
            seen_images.add(img["name"].lower())
            itype = img["type"]
            if msg == "create" and itype in ("pre", "both"):
                r.error(iw, "Create has no pre-image")
            if msg == "delete" and itype in ("post", "both"):
                r.error(iw, "Delete has no post-image")
            if itype in ("post", "both") and stage != "post-operation":
                r.error(iw, "post-images exist only in post-operation")
            elif itype in ("post", "both") and mode == "sync":
                r.warn(iw, "post-image on a synchronous step adds a read to every execution — "
                           "keep it only when post-commit values are needed")
            if not message_property(step, img):
                r.error(iw, f"set 'message_property' — no default for message '{step['message']}'")

    for uname, aspec in (data.get("custom_apis") or {}).items():
        aspec = aspec or {}
        w = f"{where}:custom_apis.{uname}"
        if not uname.lower().startswith(prefix + "_"):
            r.error(w, f"Custom API unique name must start with '{prefix}_'")
        binding = aspec["binding"]
        if binding == "global" and aspec.get("bound_table"):
            r.error(w, "bound_table applies only to entity and entity-collection bindings")
        if binding != "global" and not aspec.get("bound_table"):
            r.error(w, f"binding {binding} requires bound_table")
        if aspec.get("plugin"):
            check_plugin(w, aspec["plugin"])
        else:
            r.warn(w, "no plugin — the Custom API has no main operation")
        if not aspec.get("description"):
            r.warn(w, "Custom API has no description")
        if aspec.get("is_function") and not aspec.get("response"):
            r.error(w, "a function must have at least one response property")
        for section in ("request", "response"):
            for pname, ps in (aspec.get(section) or {}).items():
                ps = ps or {}
                if ps.get("table") and ps["type"] not in ENTITY_PARAM_TYPES:
                    r.error(f"{w}.{section}.{pname}",
                            f"'table' applies only to {', '.join(sorted(ENTITY_PARAM_TYPES))}")
    return data


def validate_path(path: Path) -> Report:
    r = Report()
    files = find_files(path)
    if not files:
        r.error(str(path), f"no *{EXT} file found")
        return r
    names = {}
    for f in files:
        data = validate_unit(f, r)
        if data is None:
            continue
        name = data[unit_kind(data)]["name"].lower()
        if name in names:
            r.error(f.name, f"'{name}' is already described by {names[name]}")
        names[name] = f.name
    return r


# --------------------------------------------------------------------------- #
# Server state (normalised to the model vocabulary)
# --------------------------------------------------------------------------- #
STEP_SELECT = ("sdkmessageprocessingstepid,name,description,stage,mode,rank,filteringattributes,"
               "configuration,asyncautodelete,statecode,_eventhandler_value")
STEP_EXPAND = "sdkmessageid($select=name),sdkmessagefilterid($select=primaryobjecttypecode)"
IMAGE_SELECT = ("sdkmessageprocessingstepimageid,name,entityalias,imagetype,attributes,"
                "messagepropertyname,_sdkmessageprocessingstepid_value")
API_SELECT = ("customapiid,uniquename,name,displayname,description,bindingtype,boundentitylogicalname,"
              "isfunction,isprivate,allowedcustomprocessingsteptype,workflowsdkstepenabled,"
              "executeprivilegename,_plugintypeid_value")
PARAM_TABLES = {"request": ("customapirequestparameters", "customapirequestparameterid"),
                "response": ("customapiresponseproperties", "customapiresponsepropertyid")}


def _chunks(seq, n=15):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _or_filter(field, ids):
    return "(" + " or ".join(f"{field} eq {i}" for i in ids) + ")"


def _attrs(text):
    return sorted(a.strip().lower() for a in (text or "").split(",") if a.strip())


def _low(v):
    return (v or "").lower()


def component_types(api):
    """Solution component type code per kind (None when the environment doesn't know it)."""
    wanted = [t for t in COMPONENT_TABLE.values() if t not in FIXED_COMPONENT_TYPE]
    rows = api.get_all("solutioncomponentdefinitions?$select=solutioncomponenttype,primaryentityname"
                       "&$filter=" + " or ".join(f"primaryentityname eq '{t}'" for t in wanted))
    codes = dict(FIXED_COMPONENT_TYPE)
    for row in rows:
        codes[row["primaryentityname"]] = row["solutioncomponenttype"]
    return {kind: codes.get(table) for kind, table in COMPONENT_TABLE.items()}


def solution_members(api, solution_id, ctypes):
    codes = sorted({c for c in ctypes.values() if c is not None})
    if not codes:
        return set()
    rows = api.get_all("solutioncomponents?$select=objectid,componenttype"
                       f"&$filter=_solutionid_value eq {solution_id} and "
                       + _or_filter("componenttype", codes))
    return {row["objectid"].lower() for row in rows}


def download(api, path):
    """Raw GET of a binary body (file column) — the shared client decodes JSON only."""
    req = urllib.request.Request(api._url(path), headers=api._headers({"Accept": "*/*"}))
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (204, 400, 404):
            return None
        raise ApiError(e.code, None, e.read().decode("utf-8", "replace"), path) from None


def server_content_sha(api, kind, rid):
    """SHA-256 of what the server holds, or None when the content is not readable."""
    entity_set = "pluginpackages" if kind == "package" else "pluginassemblies"
    row = api.get(f"{entity_set}({rid})?$select=content")
    if row.get("content"):
        return hashlib.sha256(base64.b64decode(row["content"])).hexdigest()
    if kind == "package":  # package content lives in file storage
        raw = download(api, f"pluginpackages({rid})/package/$value")
        if raw:
            return hashlib.sha256(raw).hexdigest()
    return None


def read_assemblies(api, kind, rid, name):
    if kind == "assembly":
        return [{"id": rid, "name": name}]
    return [{"id": r["pluginassemblyid"], "name": r.get("name") or ""}
            for r in api.get_all("pluginassemblies?$select=pluginassemblyid,name"
                                 f"&$filter=_packageid_value eq {rid}")]


def read_types(api, assemblies):
    types = {}
    for a in assemblies:
        for row in api.get_all("plugintypes?$select=plugintypeid,typename"
                               f"&$filter=_pluginassemblyid_value eq {a['id']}"):
            types[row["typename"]] = row["plugintypeid"]
    return types


def normalise_step(row, type_by_id):
    flt = (row.get("sdkmessagefilterid") or {}).get("primaryobjecttypecode")
    return {"id": row["sdkmessageprocessingstepid"], "name": row["name"],
            "plugin": type_by_id.get(_low(row.get("_eventhandler_value"))),
            "message": (row.get("sdkmessageid") or {}).get("name") or "",
            "table": None if flt in (None, "", "none") else flt.lower(),
            "stage": STAGE_FROM_API.get(row.get("stage"), str(row.get("stage"))),
            "mode": MODE_FROM_API.get(row.get("mode"), str(row.get("mode"))),
            "rank": row.get("rank"), "filtering_attributes": _attrs(row.get("filteringattributes")),
            "configuration": row.get("configuration") or "", "description": row.get("description") or "",
            "async_auto_delete": bool(row.get("asyncautodelete")), "enabled": row.get("statecode") == 0,
            "images": {}, "duplicate_images": []}


def normalise_image(row):
    return {"id": row["sdkmessageprocessingstepimageid"], "name": row["name"],
            "alias": row.get("entityalias"), "type": IMAGE_TYPE_FROM_API.get(row.get("imagetype")),
            "attributes": _attrs(row.get("attributes")), "message_property": row.get("messagepropertyname")}


def read_params(api, api_id, section):
    entity_set, id_field = PARAM_TABLES[section]
    select = f"{id_field},uniquename,name,displayname,description,type,logicalentityname"
    if section == "request":
        select += ",isoptional"
    out = {}
    for p in api.get_all(f"{entity_set}?$select={select}&$filter=_customapiid_value eq {api_id}"):
        d = {"id": p[id_field], "uniquename": p["uniquename"], "type": PARAM_TYPE_FROM_API.get(p.get("type")),
             "table": _low(p.get("logicalentityname")) or None, "name": p.get("name") or "",
             "display_name": p.get("displayname") or "", "description": p.get("description") or ""}
        if section == "request":
            d["optional"] = bool(p.get("isoptional"))
        out[p["uniquename"].lower()] = d
    return out


def normalise_api(api, row, type_by_id):
    aid = row["customapiid"]
    return {"id": aid, "uniquename": row["uniquename"], "name": row.get("name") or "",
            "display_name": row.get("displayname") or "", "description": row.get("description") or "",
            "binding": BINDING_FROM_API.get(row.get("bindingtype")),
            "bound_table": _low(row.get("boundentitylogicalname")) or None,
            "is_function": bool(row.get("isfunction")), "is_private": bool(row.get("isprivate")),
            "allowed_steps": ALLOWED_STEPS_FROM_API.get(row.get("allowedcustomprocessingsteptype")),
            "workflow_enabled": bool(row.get("workflowsdkstepenabled")),
            "execute_privilege": row.get("executeprivilegename") or None,
            "plugin": type_by_id.get(_low(row.get("_plugintypeid_value"))),
            "request": read_params(api, aid, "request"), "response": read_params(api, aid, "response")}


def read_unit(api, unit):
    """Server state of one package/assembly: its types, steps, images and Custom APIs."""
    kind, name = unit["kind"], unit["name"]
    srv = {"id": None, "version": None, "sha256": None, "assemblies": [], "types": {},
           "steps": {}, "duplicate_steps": [], "custom_apis": {}, "scope_apis": []}
    if kind == "package":
        rows = api.get_all("pluginpackages?$select=pluginpackageid,uniquename,version"
                           f"&$filter=uniquename eq '{name}'")
        id_field = "pluginpackageid"
    else:
        rows = api.get_all("pluginassemblies?$select=pluginassemblyid,name,version"
                           f"&$filter=name eq '{name}'")
        id_field = "pluginassemblyid"
    if rows:
        srv["id"], srv["version"] = rows[0][id_field], rows[0].get("version")
        srv["sha256"] = server_content_sha(api, kind, srv["id"])
        srv["assemblies"] = read_assemblies(api, kind, srv["id"], name)
    srv["types"] = read_types(api, srv["assemblies"])
    type_by_id = {v.lower(): k for k, v in srv["types"].items()}

    for chunk in _chunks(srv["types"].values()):
        for row in api.get_all(f"sdkmessageprocessingsteps?$select={STEP_SELECT}&$expand={STEP_EXPAND}"
                               f"&$filter={_or_filter('_eventhandler_value', chunk)}"):
            key = row["name"].lower()
            if key in srv["steps"]:
                # Names are the identity: two steps sharing one cannot be told apart.
                # The Plugin Registration Tool gives the same default name to steps that
                # differ only by stage or mode.
                if key not in {d.lower() for d in srv["duplicate_steps"]}:
                    srv["duplicate_steps"].append(row["name"])
                continue
            srv["steps"][key] = normalise_step(row, type_by_id)
    by_id = {s["id"].lower(): s for s in srv["steps"].values()}
    for chunk in _chunks(by_id):
        for row in api.get_all(f"sdkmessageprocessingstepimages?$select={IMAGE_SELECT}"
                               f"&$filter={_or_filter('_sdkmessageprocessingstepid_value', chunk)}"):
            step = by_id.get(_low(row.get("_sdkmessageprocessingstepid_value")))
            if step is None:
                continue
            ikey = row["name"].lower()
            if ikey in step["images"]:
                if ikey not in {d.lower() for d in step["duplicate_images"]}:
                    step["duplicate_images"].append(row["name"])
                continue
            step["images"][ikey] = normalise_image(row)

    # Custom APIs: the modeled ones by unique name, plus any other API that one of
    # the unit's types implements (reported as unmanaged).
    for key, want in unit["custom_apis"].items():
        rows = api.get_all(f"customapis?$select={API_SELECT}&$filter=uniquename eq '{want['uniquename']}'")
        if rows:
            srv["custom_apis"][key] = normalise_api(api, rows[0], type_by_id)
    for chunk in _chunks(srv["types"].values()):
        for row in api.get_all("customapis?$select=customapiid,uniquename"
                               f"&$filter={_or_filter('_plugintypeid_value', chunk)}"):
            if row["uniquename"].lower() not in unit["custom_apis"]:
                srv["scope_apis"].append(row["uniquename"])
    return srv


def read_server(api, units):
    ctypes = component_types(api)
    solutions = {}
    for u in units:
        if u["solution"] not in solutions:
            sid = check_solution(api, u["solution"])
            solutions[u["solution"]] = {"id": sid, "members": solution_members(api, sid, ctypes)}
    return {"component_types": ctypes, "solutions": solutions,
            "units": {u["name"].lower(): read_unit(api, u) for u in units}}


# --------------------------------------------------------------------------- #
# Diff (pure — unit-testable with hand-built dicts)
# --------------------------------------------------------------------------- #
def _step_detail(s):
    return f"{s['message']} {s['table'] or '(any table)'} {s['stage']} {s['mode']}"


def diff(units, server):
    """Compare the model units with the normalised server state.

    Returns {"actions", "blocked", "unmanaged", "notes"}.
    """
    actions, blocked, unmanaged, notes = [], [], [], []
    ctypes = server.get("component_types") or {}
    for kind, code in ctypes.items():
        if code is None:
            notes.append(f"solution component type of '{COMPONENT_TABLE[kind]}' unknown in this "
                         f"environment — solution membership of {kind} components is not checked")

    def act(op, kind, unit, name, **extra):
        a = {"op": op, "kind": kind, "unit": unit["name"], "name": name}
        a.update(extra)
        actions.append(a)

    def membership(unit, kind, name, rid, **extra):
        code = ctypes.get(kind)
        members = server["solutions"][unit["solution"]]["members"]
        if rid and code is not None and rid.lower() not in members:
            act("update", kind, unit, name, id=rid, component=code,
                changes={"solution": ["absent", unit["solution"]]}, **extra)

    def block(kind, name, field, have, want, reason):
        blocked.append({"kind": kind, "name": name, "field": field, "server": have, "model": want,
                        "reason": reason})

    for unit in units:
        srv = server["units"][unit["name"].lower()]
        kind, art = unit["kind"], unit["artifact"]
        pushing = False

        # ── Package / assembly ───────────────────────────────────────────────
        if srv["id"] is None:
            if unit["id"]:
                block(kind, unit["name"], "id", None, unit["id"],
                      f"no {kind} named '{unit['name']}' on the server, but the YAML holds an id — "
                      f"remove 'id:' only to register a new {kind} deliberately")
                continue
            detail = Path(art["path"]).name + (f" version {art['version']}" if art["version"] else "")
            act("create", kind, unit, unit["name"], detail=detail)
        else:
            if unit["id"] and unit["id"].lower() != srv["id"].lower():
                block(kind, unit["name"], "id", srv["id"], unit["id"],
                      f"the YAML id points to another {kind} — the name is the identity; fix the id")
                continue
            if not unit["id"]:
                act("update", kind, unit, unit["name"], id=srv["id"], changes={"id": [None, srv["id"]]})
            if srv["sha256"] is not None:
                pushing = srv["sha256"] != art["sha256"]
                if pushing:
                    act("push", kind, unit, unit["name"], id=srv["id"],
                        changes={"content": [f"sha256:{srv['sha256'][:12]}", f"sha256:{art['sha256'][:12]}"]})
            elif kind == "package":
                notes.append(f"{unit['name']}: server content not readable — compared the package version")
                pushing = (srv["version"] or "") != (art["version"] or "")
                if pushing:
                    act("push", kind, unit, unit["name"], id=srv["id"],
                        changes={"version": [srv["version"], art["version"]]})
            else:
                notes.append(f"{unit['name']}: server content not readable — content changes are not detected")
            membership(unit, kind, unit["name"], srv["id"])
            if kind == "package":
                for asm in srv["assemblies"]:
                    membership(unit, "assembly", asm["name"] or asm["id"], asm["id"])

        # ── Plug-in types ────────────────────────────────────────────────────
        referenced = [s["plugin"] for s in unit["steps"]] + \
                     [a["plugin"] for a in unit["custom_apis"].values() if a.get("plugin")]
        resolved_later = srv["id"] is None or pushing
        for typename in dict.fromkeys(referenced):
            if typename in srv["types"]:
                continue
            if kind == "assembly":
                act("create", "plugintype", unit, typename)
            elif not resolved_later:
                block("plugintype", typename, "plugin", None, typename,
                      "this class is not in the package on the server — build the code that contains "
                      "it; the next plan pushes the new package")

        # ── Custom APIs ──────────────────────────────────────────────────────
        for key, want in unit["custom_apis"].items():
            have = srv["custom_apis"].get(key)
            uname = want["uniquename"]
            if have is None:
                act("create", "customapi", unit, uname,
                    detail=want["binding"] + (f" {want['bound_table']}" if want["bound_table"] else "")
                    + (" function" if want["is_function"] else " action")
                    + f", {len(want['request'])} request / {len(want['response'])} response")
                continue
            immutable = False
            for field in ("binding", "bound_table", "is_function", "allowed_steps", "workflow_enabled"):
                if have.get(field) != want[field]:
                    block("customapi", uname, field, have.get(field), want[field],
                          "cannot be changed once the Custom API exists — revert the YAML, or create "
                          "a new Custom API and migrate its callers")
                    immutable = True
            if not immutable:
                changes = {f: [have.get(f), want[f]] for f in ("display_name", "is_private", "name",
                                                              "description", "execute_privilege", "plugin")
                           if f in want and have.get(f) != want[f]}
                if changes:
                    act("update", "customapi", unit, uname, id=have["id"], changes=changes)
            membership(unit, "customapi", uname, have["id"])
            for section in ("request", "response"):
                fixed = ("type", "table", "optional") if section == "request" else ("type", "table")
                for pkey, pw in want[section].items():
                    ph = have[section].get(pkey)
                    pname = f"{uname}.{pw['uniquename']}"
                    extra = {"api": uname, "param": pw["uniquename"]}
                    if ph is None:
                        act("create", section, unit, pname, detail=pw["type"], **extra)
                        continue
                    pblocked = False
                    for field in fixed:
                        if ph.get(field) != pw.get(field):
                            block(section, pname, field, ph.get(field), pw.get(field),
                                  "cannot be changed once created — add a new parameter instead")
                            pblocked = True
                    changes = {f: [ph.get(f), pw[f]] for f in PARAM_FIELD_TO_API
                               if f in pw and ph.get(f) != pw[f]}
                    if changes and not pblocked:
                        act("update", section, unit, pname, id=ph["id"], changes=changes, **extra)
                    membership(unit, section, pname, ph["id"], **extra)
                for pkey in sorted(set(have[section]) - set(want[section])):
                    unmanaged.append({"kind": section, "name": f"{uname}.{have[section][pkey]['uniquename']}",
                                      "detail": "never deleted — removing it breaks callers"})
        for uname in srv["scope_apis"]:
            unmanaged.append({"kind": "customapi", "name": uname, "detail": f"implemented by {unit['name']}"})

        # ── Steps and images ─────────────────────────────────────────────────
        duplicates = {n.lower() for n in srv.get("duplicate_steps", [])}
        for n in srv.get("duplicate_steps", []):
            block("step", n, "name", "shared by several steps", "unique",
                  f"several steps of {unit['name']} carry this name on the server and cannot be told apart — "
                  "rename all but one on the server, then run export and plan again")
        model_steps = set()
        for want in unit["steps"]:
            key = want["name"].lower()
            model_steps.add(key)
            if key in duplicates:
                continue
            have = srv["steps"].get(key)
            if have is None:
                act("create", "step", unit, want["name"], detail=_step_detail(want))
                for wi in want["images"].values():
                    act("create", "image", unit, f"{want['name']} / {wi['name']}", step=want["name"],
                        image=wi["name"], detail=f"{wi['type']} ({', '.join(wi['attributes'])})")
                continue
            changes = {}
            for field in ("plugin", "message", "table", "stage", "mode", "rank", "filtering_attributes",
                          "enabled", *STEP_FIELD_TO_API):
                if field not in want:
                    continue
                h, w = have.get(field), want[field]
                if field == "message":
                    h, w = _low(h), _low(w)
                if h != w:
                    changes[field] = [have.get(field), want[field]]
            if changes:
                act("update", "step", unit, want["name"], id=have["id"], changes=changes)
            membership(unit, "step", want["name"], have["id"])
            dup_images = {n.lower() for n in have.get("duplicate_images", [])}
            for n in have.get("duplicate_images", []):
                block("image", f"{want['name']} / {n}", "name", "shared by several images", "unique",
                      "several images of this step carry this name on the server — rename all but one "
                      "on the server, then run export and plan again")
            for ikey, wi in want["images"].items():
                if ikey in dup_images:
                    continue
                hi = have["images"].get(ikey)
                iname = f"{want['name']} / {wi['name']}"
                extra = {"step": want["name"], "image": wi["name"]}
                if hi is None:
                    act("create", "image", unit, iname, detail=f"{wi['type']} ({', '.join(wi['attributes'])})", **extra)
                    continue
                ichanges = {f: [hi.get(f), wi[f]] for f in ("alias", "type", "attributes", "message_property")
                            if hi.get(f) != wi[f]}
                if ichanges:
                    act("update", "image", unit, iname, id=hi["id"], changes=ichanges, **extra)
                membership(unit, "image", iname, hi["id"], **extra)
            for ikey in sorted(set(have["images"]) - set(want["images"]) - dup_images):
                hi = have["images"][ikey]
                act("delete", "image", unit, f"{want['name']} / {hi['name']}", id=hi["id"],
                    detail=f"{hi['type']} ({', '.join(hi['attributes'])})")
        for key in sorted(set(srv["steps"]) - model_steps - duplicates):
            hs = srv["steps"][key]
            images = f", and its {len(hs['images'])} image(s)" if hs["images"] else ""
            act("delete", "step", unit, hs["name"], id=hs["id"], detail=_step_detail(hs) + images)

    actions.sort(key=phase_of)  # stable: keeps model order within a phase
    return {"actions": actions, "blocked": blocked, "unmanaged": unmanaged, "notes": notes}


def double_confirm(actions):
    """Actions the user must confirm twice: permanent names, immutable APIs, deletes."""
    return [f"{a['op']} {a['kind']} {a['name']}" for a in actions
            if a["op"] == "delete" or (a["op"] == "create" and a["kind"] in ("package", "assembly", "customapi"))]


# --------------------------------------------------------------------------- #
# Plan rendering
# --------------------------------------------------------------------------- #
def _fmt(v):
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else f'"{v}"'


def render_plan(plan):
    print(bold("Dataverse plug-in plan"))
    print(f"  env      : {plan['env_url']} ({plan['env_file']})")
    if plan.get("az_account"):
        print(f"  az user  : {plan['az_account']}")
    for u in plan["units"]:
        print(f"  {u['kind']:<8} : {u['name']} → {u['solution']} (unmanaged)  {grey(u['file'])}")
        print(grey(f"             {u['artifact']['path']}  sha256 {u['artifact']['sha256'][:12]}"))
    acts = plan["actions"]
    if acts:
        print()
    labels = {"create": green("+ create"), "update": yellow("~ update"),
              "push": cyan("↑ push  "), "delete": red("- delete")}
    for a in acts:
        print(f"  {labels[a['op']]}  {a['kind']:<10} {a['name']}" + (f"  {grey(a['detail'])}" if a.get("detail") else ""))
        for field, (old, new) in a.get("changes", {}).items():
            print(f"              {field}: {_fmt(old)} → {_fmt(new)}")
    if plan["blocked"]:
        print("\n" + red("Blocked — never applied; align the model with the server or change it manually:"))
        for b in plan["blocked"]:
            print(f"  {red('!')} {b['kind']:<10} {b['name']}  {b['field']}: {_fmt(b['server'])} → {_fmt(b['model'])}")
            print(grey(f"              {b['reason']}"))
    if plan["unmanaged"]:
        print("\n" + grey("Unmanaged — on the server but not in the model (reported only, never deleted):"))
        for u in plan["unmanaged"]:
            print(f"  ? {u['kind']:<10} {u['name']}" + (f"  {grey(str(u['detail']))}" if u.get("detail") else ""))
    if plan.get("double_confirm"):
        print("\n" + yellow("Needs double confirmation:"))
        for line in plan["double_confirm"]:
            print(f"  {yellow('!!')} {line}")
    for n in plan.get("notes") or []:
        print(grey(f"  note: {n}"))
    count = {op: sum(1 for a in acts if a["op"] == op) for op in ("create", "update", "push", "delete")}
    print(f"\nSummary: {count['create']} to create, {count['update']} to update, {count['push']} to push, "
          f"{count['delete']} to delete, {len(plan['blocked'])} blocked, {len(plan['unmanaged'])} unmanaged.")
    if not acts and not plan["blocked"]:
        print(green("No changes — the environment matches the model."))


def _canonical(plan):
    return json.dumps({"env_url": plan["env_url"], "units": plan["units"],
                       "actions": plan["actions"], "blocked": plan["blocked"]},
                      sort_keys=True, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def build_unit(path: Path, data):
    kind = unit_kind(data)
    spec = data[kind]
    return {"file": path, "kind": kind, "name": spec["name"], "id": spec.get("id"),
            "solution": data["publisher"]["solution"], "prefix": data["publisher"]["prefix"].lower(),
            "artifact": locate_artifact(path, data),
            "steps": [desired_step(s) for s in data.get("steps") or []],
            "custom_apis": {k.lower(): desired_api(k, v or {}) for k, v in (data.get("custom_apis") or {}).items()}}


def _validated_units(path_arg):
    path = Path(path_arg)
    report = validate_path(path)
    if report.errors:
        for e in report.errors:
            print(red("  ✗ ") + e, file=sys.stderr)
        raise SystemExit(f"ERROR: the model is invalid ({len(report.errors)} error(s)) — run validate and fix it first")
    units = [build_unit(f, load_yaml(f)) for f in find_files(path)]
    missing = [u["name"] for u in units if u["artifact"] is None]
    if missing:
        raise SystemExit(f"ERROR: no build output for {', '.join(missing)} — build the project first "
                         "(ppbp-dv-plugin-build)")
    return units


def _rel(path, root):
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return str(path)


def compute_plan(path_arg, strong=False):
    units = _validated_units(path_arg)
    root = find_repo_root(units[0]["file"].parent)
    env = dev_environment(root)
    api = Api(env, strong=strong)
    account = az_json(["account", "show"])
    server = read_server(api, units)
    result = diff(units, server)
    plan = {
        "plan_version": PLAN_VERSION,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "env_url": env["url"], "env_file": env["env_file"],
        "az_account": f"{(account or {}).get('user', {}).get('name', '?')} (tenant {(account or {}).get('tenantId', '?')})",
        "units": [{"file": _rel(u["file"], root), "kind": u["kind"], "name": u["name"], "solution": u["solution"],
                   "artifact": {"path": _rel(u["artifact"]["path"], root), "sha256": u["artifact"]["sha256"],
                                "version": u["artifact"]["version"]}} for u in units],
        **result,
        "double_confirm": double_confirm(result["actions"]),
    }
    return root, units, env, api, server, plan


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
    root, _units, _env, _api, _server, plan = compute_plan(args.path)
    out = Path(args.out) if args.out else root / PLAN_DIR / "plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps({**plan, "plan_file": str(out)}, indent=2, ensure_ascii=False, default=str))
    else:
        render_plan(plan)
        print(grey(f"\nPlan saved to {out}"))
        if plan["actions"] and not plan["blocked"]:
            print(grey(f"Apply it with: dv_plugin.py apply {args.path} --plan-file {out}"))
    return plan_exit_code(plan)


def pac_path():
    return shutil.which("pac") or shutil.which("pac.cmd") or shutil.which("pac.exe")


def pac_push_command(env_url, rid, artifact_path, kind):
    """Always targets the dev URL explicitly — never the active pac environment."""
    return [pac_path() or "pac", "plugin", "push", "--pluginId", rid, "--pluginFile", str(artifact_path),
            "--type", "Nuget" if kind == "package" else "Assembly", "--environment", env_url]


def run_pac_push(env_url, rid, artifact_path, kind):
    if not pac_path():
        raise RuntimeError("PAC CLI (pac) not found — install it and authenticate (official skill: dataverse:dv-connect)")
    proc = subprocess.run(pac_push_command(env_url, rid, artifact_path, kind), capture_output=True, text=True)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or re.search(r"^\s*Error:", out, re.M):
        raise RuntimeError("pac plugin push failed:\n" + out)


def cmd_doctor(args):
    ok = True

    def line(passed, label, fix=""):
        nonlocal ok
        print(("  " + green("✓") if passed else "  " + red("✗")) + f" {label}")
        if not passed:
            ok = False
            if fix:
                print(grey(f"      → {fix}"))

    print(bold("dv_plugin doctor"))
    line(sys.version_info >= (3, 9), f"Python ≥ 3.9 ({sys.version.split()[0]})", "install Python 3.9+")
    line(yaml is not None, "pyyaml installed", f"pip install -r {REQUIREMENTS}")
    line(jsonschema is not None, "jsonschema installed", f"pip install -r {REQUIREMENTS}")
    line(az_path() is not None, "Azure CLI (az) on PATH", "install the Azure CLI")
    line(pac_path() is not None, "PAC CLI (pac) on PATH", "install the Power Platform CLI")
    files = find_files(Path(args.path)) if args.path else []
    try:
        root = find_repo_root(files[0].parent if files else Path.cwd())
        env = dev_environment(root)
        line(True, f"dev guard: {env['env_file']} → {env['url']}")
    except GuardError as e:
        line(False, "dev guard", str(e))
        return 1
    if pac_path():
        proc = subprocess.run([pac_path(), "auth", "who"], capture_output=True, text=True)
        who = [ln.strip() for ln in (proc.stdout or "").splitlines() if ":" in ln][:6]
        line(proc.returncode == 0, "pac auth profile", "authenticate the PAC CLI (official skill: dataverse:dv-connect)")
        for ln in who:
            print(grey(f"      {ln}"))
        print(grey(f"      pac plugin push always receives --environment {env['url']}"))
    try:
        account = az_json(["account", "show"])
        line(True, f"az session: {account.get('user', {}).get('name')} (tenant {account.get('tenantId')})")
        if env["tenant"] and account.get("tenantId", "").lower() != env["tenant"].lower():
            print(yellow(f"      ⚠ active az tenant differs from TENANT_ID in .env.dev — the token is requested for {env['tenant']}"))
        api = Api(env)
        api.token()
        line(True, "access token for the dev environment")
        who = api.get("WhoAmI")
        line(True, f"WhoAmI: user {who.get('UserId')}")
        for f in files:
            data = load_yaml(f) or {}
            sol = (data.get("publisher") or {}).get("solution")
            if sol:
                check_solution(api, sol)
                line(True, f"{f.name}: solution '{sol}' exists and is unmanaged")
    except (GuardError, ApiError) as e:
        line(False, "Dataverse access", str(e))
    for f in files:
        data = load_yaml(f) or {}
        if "package" not in data and "assembly" not in data:
            continue
        try:
            art = locate_artifact(f, data)
        except (zipfile.BadZipFile, ValueError, OSError, ET.ParseError) as e:
            art, why = None, str(e)
        else:
            why = "build the project (ppbp-dv-plugin-build)"
        line(art is not None, f"{f.name}: build output " + (art["path"] if art else "missing"), why)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def write_id(path: Path, kind: str, rid: str):
    """Record the server id in the model — the only edit the tool makes to a YAML file."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{kind}:\s*(#.*)?$", ln.rstrip("\r\n"))), None)
    if start is None:
        raise RuntimeError(f"{path.name}: '{kind}:' must be a block mapping (one key per line) "
                           "for the tool to record the id")
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    for i in range(start + 1, end):
        m = re.match(r"^(\s+id:\s*)([^#\r\n]*?)(\s*#[^\r\n]*)?(\r?\n)?$", lines[i])
        if m:
            lines[i] = f"{m.group(1)}{rid}{m.group(3) or ''}{m.group(4) or ''}"
            break
    else:
        name_i = next((i for i in range(start + 1, end) if re.match(r"^\s+name:", lines[i])), start)
        indent = re.match(r"^(\s*)", lines[name_i]).group(1) if name_i > start else "  "
        lines.insert(name_i + 1, f"{indent}id: {rid}\n")
    path.write_text("".join(lines), encoding="utf-8")


class Applier:
    def __init__(self, api, env, units, server):
        self.api = api
        self.env = env
        self.units = {u["name"].lower(): u for u in units}
        self.state = server["units"]  # updated with the ids of what this run creates
        self.ctypes = server["component_types"]
        self.solution_ids = {name: s["id"] for name, s in server["solutions"].items()}

    # -- helpers --------------------------------------------------------------
    def unit(self, a):
        return self.units[a["unit"].lower()]

    def srv(self, a):
        return self.state[a["unit"].lower()]

    def post(self, unit, path, body, solution=True):
        h = {SOLUTION_HEADER: unit["solution"]} if solution else {}
        rid = self.api.request("POST", path, body, h, return_id=True)
        if not rid:
            raise RuntimeError(f"POST {path}: the server did not return the id of the new record")
        return rid

    def patch(self, path, body):
        self.api.request("PATCH", path, body, {"If-Match": "*"})

    def add_component(self, unit, rid, code):
        self.api.request("POST", "AddSolutionComponent", {
            "ComponentId": rid, "ComponentType": code, "SolutionUniqueName": unit["solution"],
            "AddRequiredComponents": False, "DoNotIncludeSubcomponents": True,
        })

    def ensure_members(self, unit, kind, ids):
        """Add components the server created implicitly (package assemblies, deep-inserted
        Custom API parameters) to the solution when the header did not."""
        code = self.ctypes.get(kind)
        if code is None or not ids:
            return
        members = solution_members(self.api, self.solution_ids[unit["solution"]], {kind: code})
        for rid in ids:
            if rid.lower() not in members:
                self.add_component(unit, rid, code)

    def refresh_types(self, unit):
        s = self.state[unit["name"].lower()]
        s["assemblies"] = read_assemblies(self.api, unit["kind"], s["id"], unit["name"])
        s["types"] = read_types(self.api, s["assemblies"])
        self.ensure_members(unit, "assembly", [x["id"] for x in s["assemblies"]])

    def type_id(self, unit, typename):
        tid = self.state[unit["name"].lower()]["types"].get(typename)
        if not tid:
            raise RuntimeError(f"plugin type '{typename}' not found in {unit['name']} on the server — check the "
                               "namespace and class name, and that the class implements IPlugin")
        return tid

    def step_spec(self, unit, name):
        return next(s for s in unit["steps"] if s["name"].lower() == name.lower())

    def step_id(self, a):
        return self.srv(a)["steps"][a["step"].lower()]["id"]

    def api_id(self, a):
        return self.srv(a)["custom_apis"][a["api"].lower()]["id"]

    def message_ids(self, message, table):
        rows = self.api.get_all(f"sdkmessages?$select=sdkmessageid&$filter=name eq '{message}'")
        if not rows:
            raise RuntimeError(f"SDK message '{message}' not found")
        mid = rows[0]["sdkmessageid"]
        if not table:
            return mid, None
        rows = self.api.get_all("sdkmessagefilters?$select=sdkmessagefilterid"
                                f"&$filter=_sdkmessageid_value eq {mid} and primaryobjecttypecode eq '{table}'")
        if not rows:
            raise RuntimeError(f"message '{message}' is not available on table '{table}'")
        return mid, rows[0]["sdkmessagefilterid"]

    # -- package / assembly ---------------------------------------------------
    def _registered(self, unit, rid):
        self.state[unit["name"].lower()]["id"] = rid
        write_id(unit["file"], unit["kind"], rid)
        unit["id"] = rid
        self.refresh_types(unit)

    def create_package(self, a):
        unit = self.unit(a)
        art = unit["artifact"]
        content = base64.b64encode(Path(art["path"]).read_bytes()).decode("ascii")
        rid = self.post(unit, "pluginpackages", {"name": unit["name"], "uniquename": unit["name"],
                                                 "version": art["version"], "content": content})
        self._registered(unit, rid)

    def create_assembly(self, a):
        unit = self.unit(a)
        content = base64.b64encode(Path(unit["artifact"]["path"]).read_bytes()).decode("ascii")
        rid = self.post(unit, "pluginassemblies", {"name": unit["name"], "content": content,
                                                   "isolationmode": 2, "sourcetype": 0})
        self._registered(unit, rid)

    def update_package(self, a):
        unit = self.unit(a)
        if a.get("component"):
            self.add_component(unit, a["id"], a["component"])
        elif "id" in a["changes"]:
            write_id(unit["file"], unit["kind"], a["id"])
            unit["id"] = a["id"]

    update_assembly = update_package

    def push_package(self, a):
        unit = self.unit(a)
        run_pac_push(self.env["url"], a["id"], unit["artifact"]["path"], unit["kind"])
        self.refresh_types(unit)

    push_assembly = push_package

    def create_plugintype(self, a):
        unit = self.unit(a)
        s = self.srv(a)
        if not s["assemblies"]:
            raise RuntimeError(f"assembly {unit['name']} not found on the server")
        rid = self.post(unit, "plugintypes", {
            "typename": a["name"], "name": a["name"], "friendlyname": a["name"],
            "pluginassemblyid@odata.bind": f"/pluginassemblies({s['assemblies'][0]['id']})",
        }, solution=False)
        s["types"][a["name"]] = rid

    # -- Custom APIs ----------------------------------------------------------
    @staticmethod
    def param_body(api_uname, pw, response):
        body = {"uniquename": pw["uniquename"], "name": pw.get("name", f"{api_uname}.{pw['uniquename']}"),
                "displayname": pw.get("display_name", pw["uniquename"]),
                "description": pw.get("description", pw.get("display_name", pw["uniquename"])),
                "type": PARAM_TYPE_TO_API[pw["type"]]}
        if pw.get("table"):
            body["logicalentityname"] = pw["table"]
        if not response:
            body["isoptional"] = pw["optional"]
        return body

    def create_customapi(self, a):
        unit = self.unit(a)
        w = unit["custom_apis"][a["name"].lower()]
        body = {"uniquename": w["uniquename"], "name": w.get("name", w["uniquename"]),
                "displayname": w["display_name"], "description": w.get("description", w["display_name"]),
                "bindingtype": BINDING_TO_API[w["binding"]], "isfunction": w["is_function"],
                "isprivate": w["is_private"], "workflowsdkstepenabled": w["workflow_enabled"],
                "allowedcustomprocessingsteptype": ALLOWED_STEPS_TO_API[w["allowed_steps"]],
                "CustomAPIRequestParameters": [self.param_body(w["uniquename"], p, False) for p in w["request"].values()],
                "CustomAPIResponseProperties": [self.param_body(w["uniquename"], p, True) for p in w["response"].values()]}
        if w["bound_table"]:
            body["boundentitylogicalname"] = w["bound_table"]
        if w.get("execute_privilege"):
            body["executeprivilegename"] = w["execute_privilege"]
        if w.get("plugin"):
            body["PluginTypeId@odata.bind"] = f"/plugintypes({self.type_id(unit, w['plugin'])})"
        rid = self.post(unit, "customapis", body)
        self.srv(a)["custom_apis"][a["name"].lower()] = {"id": rid}
        for section in ("request", "response"):
            entity_set, id_field = PARAM_TABLES[section]
            ids = [r[id_field] for r in self.api.get_all(f"{entity_set}?$select={id_field}"
                                                         f"&$filter=_customapiid_value eq {rid}")]
            self.ensure_members(unit, section, ids)

    def update_customapi(self, a):
        unit = self.unit(a)
        if a.get("component"):
            self.add_component(unit, a["id"], a["component"])
            return
        body = {}
        for field, (_old, new) in a["changes"].items():
            if field == "plugin":
                body["PluginTypeId@odata.bind"] = f"/plugintypes({self.type_id(unit, new)})"
            else:
                body[API_FIELD_TO_API[field]] = new
        self.patch(f"customapis({a['id']})", body)

    def _create_param(self, a, section):
        unit = self.unit(a)
        w = unit["custom_apis"][a["api"].lower()]
        body = self.param_body(w["uniquename"], w[section][a["param"].lower()], section == "response")
        body["CustomAPIId@odata.bind"] = f"/customapis({self.api_id(a)})"
        self.post(unit, PARAM_TABLES[section][0], body)

    def _update_param(self, a, section):
        unit = self.unit(a)
        if a.get("component"):
            self.add_component(unit, a["id"], a["component"])
            return
        self.patch(f"{PARAM_TABLES[section][0]}({a['id']})",
                   {PARAM_FIELD_TO_API[f]: new for f, (_old, new) in a["changes"].items()})

    def create_request(self, a): self._create_param(a, "request")
    def update_request(self, a): self._update_param(a, "request")
    def create_response(self, a): self._create_param(a, "response")
    def update_response(self, a): self._update_param(a, "response")

    # -- steps and images -----------------------------------------------------
    def create_step(self, a):
        unit = self.unit(a)
        w = self.step_spec(unit, a["name"])
        mid, fid = self.message_ids(w["message"], w["table"])
        body = {"name": w["name"], "stage": STAGE_TO_API[w["stage"]], "mode": MODE_TO_API[w["mode"]],
                "rank": w["rank"], "supporteddeployment": 0,
                "sdkmessageid@odata.bind": f"/sdkmessages({mid})",
                "eventhandler_plugintype@odata.bind": f"/plugintypes({self.type_id(unit, w['plugin'])})"}
        if fid:
            body["sdkmessagefilterid@odata.bind"] = f"/sdkmessagefilters({fid})"
        if w["filtering_attributes"]:
            body["filteringattributes"] = ",".join(w["filtering_attributes"])
        for field, api_field in STEP_FIELD_TO_API.items():
            if field in w:
                body[api_field] = w[field]
        rid = self.post(unit, "sdkmessageprocessingsteps", body)
        self.srv(a)["steps"][w["name"].lower()] = {"id": rid, "images": {}}
        if not w["enabled"]:
            self.patch(f"sdkmessageprocessingsteps({rid})", {"statecode": 1, "statuscode": 2})

    def update_step(self, a):
        unit = self.unit(a)
        if a.get("component"):
            self.add_component(unit, a["id"], a["component"])
            return
        w = self.step_spec(unit, a["name"])
        ch = a["changes"]
        body = {}
        if "message" in ch or "table" in ch:
            mid, fid = self.message_ids(w["message"], w["table"])
            if not fid:
                raise RuntimeError("removing the table of an existing step is not supported — "
                                   "rename the step so the plan recreates it")
            body["sdkmessageid@odata.bind"] = f"/sdkmessages({mid})"
            body["sdkmessagefilterid@odata.bind"] = f"/sdkmessagefilters({fid})"
        for field in ch:
            if field == "plugin":
                body["eventhandler_plugintype@odata.bind"] = f"/plugintypes({self.type_id(unit, w['plugin'])})"
            elif field == "stage":
                body["stage"] = STAGE_TO_API[w["stage"]]
            elif field == "mode":
                body["mode"] = MODE_TO_API[w["mode"]]
            elif field == "rank":
                body["rank"] = w["rank"]
            elif field == "filtering_attributes":
                body["filteringattributes"] = ",".join(w["filtering_attributes"])
            elif field in STEP_FIELD_TO_API:
                body[STEP_FIELD_TO_API[field]] = w[field]
        if body:
            self.patch(f"sdkmessageprocessingsteps({a['id']})", body)
        if "enabled" in ch:
            self.patch(f"sdkmessageprocessingsteps({a['id']})",
                       {"statecode": 0, "statuscode": 1} if w["enabled"] else {"statecode": 1, "statuscode": 2})

    def create_image(self, a):
        unit = self.unit(a)
        wi = self.step_spec(unit, a["step"])["images"][a["image"].lower()]
        self.post(unit, "sdkmessageprocessingstepimages", {
            "name": wi["name"], "entityalias": wi["alias"], "imagetype": IMAGE_TYPE_TO_API[wi["type"]],
            "attributes": ",".join(wi["attributes"]), "messagepropertyname": wi["message_property"],
            "sdkmessageprocessingstepid@odata.bind": f"/sdkmessageprocessingsteps({self.step_id(a)})",
        })

    def update_image(self, a):
        unit = self.unit(a)
        if a.get("component"):
            self.add_component(unit, a["id"], a["component"])
            return
        body = {}
        for field, (_old, new) in a["changes"].items():
            if field == "type":
                body["imagetype"] = IMAGE_TYPE_TO_API[new]
            elif field == "attributes":
                body["attributes"] = ",".join(new)
            else:
                body[IMAGE_FIELD_TO_API[field]] = new
        self.patch(f"sdkmessageprocessingstepimages({a['id']})", body)

    def delete_image(self, a):
        self.api.request("DELETE", f"sdkmessageprocessingstepimages({a['id']})")

    def delete_step(self, a):
        self.api.request("DELETE", f"sdkmessageprocessingsteps({a['id']})")

    def run(self, a):
        getattr(self, f"{a['op']}_{a['kind']}")(a)


def cmd_apply(args):
    saved = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if saved.get("plan_version") != PLAN_VERSION:
        print(red("ERROR: unsupported plan file version — re-run plan."), file=sys.stderr)
        return 1
    _root, units, env, api, server, fresh = compute_plan(args.path)
    if _canonical(fresh) != _canonical(saved):
        print(red("ERROR: stale plan — the model, the build output or the server changed since it was computed.\n"
                  "Re-run plan, review it again, and apply the new plan file."), file=sys.stderr)
        return 1
    if fresh["blocked"]:
        print(red("ERROR: the plan contains blocked changes — fix the model, re-plan, then apply."), file=sys.stderr)
        return 1
    if not fresh["actions"]:
        print(green("No changes to apply."))
        return 0

    # Every read from here on follows our own writes.
    api.strong = True
    applier = Applier(api, env, units, server)
    done, failure = 0, None
    print(bold(f"Applying {len(fresh['actions'])} change(s) to {fresh['env_url']}"))
    for a in fresh["actions"]:
        try:
            applier.run(a)
            done += 1
            print(f"  {green('✓')} {a['op']:<6} {a['kind']:<10} {a['name']}", flush=True)
        except (ApiError, RuntimeError, KeyError, StopIteration, OSError) as e:
            failure = e
            print(f"  {red('✗')} {a['op']:<6} {a['kind']:<10} {a['name']}\n      {e}", flush=True)
            break
    print(f"\n{done}/{len(fresh['actions'])} change(s) applied.")
    if failure:
        print(red("Stopped at the first failure. Fix the cause, re-run plan, then apply the new plan."))
        return 1
    *_, after = compute_plan(args.path, strong=True)
    if after["actions"] or after["blocked"]:
        print(red("Post-apply check FAILED — the environment still differs from the model after apply "
                  "(read with Consistency: Strong). Remaining differences:"))
        render_plan(after)
        return 1
    print(green("Post-apply plan is empty — the environment matches the model."))
    return 0


# --------------------------------------------------------------------------- #
# Export (adoption of existing registrations)
# --------------------------------------------------------------------------- #
def export_document(kind, name, srv, apis, solution, prefix):
    """YAML document describing what the server holds (pure)."""
    doc = {"version": "1.0", "publisher": {"prefix": prefix, "solution": solution},
           kind: {"name": name,
                  "nupkg" if kind == "package" else "dll":
                      "bin/Release/*.nupkg" if kind == "package" else f"bin/Release/net48/{name}.dll",
                  "id": srv["id"]}}
    steps = []
    for s in sorted(srv["steps"].values(), key=lambda s: s["name"].lower()):
        st = {"plugin": s["plugin"], "message": s["message"]}
        if s["table"]:
            st["table"] = s["table"]
        st.update({"stage": s["stage"], "mode": s["mode"]})
        if s["rank"] != 1:
            st["rank"] = s["rank"]
        if s["filtering_attributes"]:
            st["filtering_attributes"] = s["filtering_attributes"]
        if s["configuration"]:
            st["configuration"] = s["configuration"]
        if s["description"]:
            st["description"] = s["description"]
        if s["async_auto_delete"] and s["mode"] == "async":
            st["async_auto_delete"] = True
        if not s["enabled"]:
            st["enabled"] = False
        images = []
        for i in sorted(s["images"].values(), key=lambda i: i["name"].lower()):
            img = {"name": i["name"]}
            if i["alias"] and i["alias"] != i["name"]:
                img["alias"] = i["alias"]
            img.update({"type": i["type"], "attributes": i["attributes"]})
            if i["message_property"] != MESSAGE_PROPERTY.get(s["message"].lower()):
                img["message_property"] = i["message_property"]
            images.append(img)
        if images:
            st["images"] = images
        if s["name"] != step_name(st):
            st = {"name": s["name"], **st}
        steps.append(st)
    if steps:
        doc["steps"] = steps
    custom_apis = {}
    for uname, h in sorted(apis.items()):
        spec = {}
        if h.get("plugin"):
            spec["plugin"] = h["plugin"]
        if h["name"] and h["name"] != uname:
            spec["name"] = h["name"]
        spec["display_name"] = h["display_name"] or uname
        if h["description"]:
            spec["description"] = h["description"]
        spec["binding"] = h["binding"]
        if h["bound_table"]:
            spec["bound_table"] = h["bound_table"]
        spec.update({"is_function": h["is_function"], "allowed_steps": h["allowed_steps"]})
        if h["workflow_enabled"]:
            spec["workflow_enabled"] = True
        if h["is_private"]:
            spec["is_private"] = True
        if h["execute_privilege"]:
            spec["execute_privilege"] = h["execute_privilege"]
        for section in ("request", "response"):
            params = {}
            for p in sorted(h[section].values(), key=lambda p: p["uniquename"].lower()):
                ps = {"type": p["type"]}
                if section == "request" and p["optional"]:
                    ps["optional"] = True
                if p["table"]:
                    ps["table"] = p["table"]
                if p["display_name"] and p["display_name"] != p["uniquename"]:
                    ps["display_name"] = p["display_name"]
                if p["description"] and p["description"] not in (p["display_name"], p["uniquename"]):
                    ps["description"] = p["description"]
                params[p["uniquename"]] = ps
            if params:
                spec[section] = params
        custom_apis[uname] = spec
    if custom_apis:
        doc["custom_apis"] = custom_apis
    return doc


def cmd_export(args):
    _require_yaml()
    root = find_repo_root(Path.cwd())
    env = dev_environment(root)
    api = Api(env)
    check_solution(api, args.solution)
    rows = api.get_all("solutions?$select=uniquename&$expand=publisherid($select=customizationprefix)"
                       f"&$filter=uniquename eq '{args.solution}'")
    prefix = ((rows[0].get("publisherid") or {}).get("customizationprefix") or "").lower()
    kind = "package" if args.package else "assembly"
    name = args.package or args.assembly
    srv = read_unit(api, {"kind": kind, "name": name, "custom_apis": {}, "steps": []})
    if not srv["id"]:
        raise SystemExit(f"ERROR: no {kind} named '{name}' in {env['url']}")
    clashes = srv["duplicate_steps"] + [f"{s['name']} / {i}" for s in srv["steps"].values()
                                        for i in s["duplicate_images"]]
    if clashes:
        raise SystemExit("ERROR: several components share these names on the server, so they cannot be told "
                         "apart — rename all but one of each, then export again:\n  " + "\n  ".join(clashes))
    type_by_id = {v.lower(): k for k, v in srv["types"].items()}
    apis = {}
    for uname in srv["scope_apis"]:
        found = api.get_all(f"customapis?$select={API_SELECT}&$filter=uniquename eq '{uname}'")
        if found:
            apis[uname] = normalise_api(api, found[0], type_by_id)
    doc = export_document(kind, name, srv, apis, args.solution, prefix)
    text = ("# yaml-language-server: $schema=<path to dvac-plugins>/references/schemas/plugin.schema.json\n"
            "# Exported from " + env["url"] + " — review it, then run validate and plan.\n"
            + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(green(f"Written to {args.out}"))
    else:
        print(text, end="")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(prog="dv_plugin.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor", help="check tooling, az + pac sessions, dev guard, solutions, build output")
    p.add_argument("path", nargs="?", help="model file or folder (enables solution and build checks)")
    p = sub.add_parser("validate", help="offline validation of a model file or folder")
    p.add_argument("path")
    p = sub.add_parser("plan", help="read-only diff against the dev environment")
    p.add_argument("path")
    p.add_argument("--out", help=f"plan file (default <repo-root>/{PLAN_DIR}/plan.json)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p = sub.add_parser("apply", help="execute a saved plan (aborts if stale)")
    p.add_argument("path")
    p.add_argument("--plan-file", required=True)
    p = sub.add_parser("export", help="print the YAML of an existing package or assembly (adoption)")
    p.add_argument("--solution", required=True, help="unmanaged dev solution the registrations belong to")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--package", help="plug-in package unique name")
    g.add_argument("--assembly", help="plug-in assembly name")
    p.add_argument("--out", help="write to this file instead of stdout")
    args = ap.parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "validate": cmd_validate, "plan": cmd_plan,
                "apply": cmd_apply, "export": cmd_export}[args.cmd](args)
    except GuardError as e:
        print(red(f"REFUSED: {e}"), file=sys.stderr)
        return 1
    except ApiError as e:
        print(red(f"Dataverse error: {e}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
