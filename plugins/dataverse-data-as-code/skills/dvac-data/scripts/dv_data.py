#!/usr/bin/env python3
"""
dv_data.py — Dataverse data as code
===================================

Reads rows described in ``*.dv.data.yaml`` files::

    dataverse/data/
    ├── <table>.dv.data.yaml            # base — every stage
    └── <stage>/<table>.dv.data.yaml    # overlay — one stage (dev, uat, prod…)

and reconciles them with the environment of one stage, Terraform style:

    doctor --stage <s> [data-dir]   check tooling, az session, stage guard, tables and keys
    validate <path> [--stage <s>]   offline structural + semantic validation
    plan <data-dir> --stage <s>     read-only diff: create / update / (de)activate / delete / blocked
    apply <data-dir> --stage <s> --plan-file <f> [--confirm-url <url>] [--confirm-deletes <n>]
                                    execute exactly the saved plan (aborts if stale)

Rows are identified by an alternate key, never by GUID, and the model files are
never written. Rows absent from the files are left alone unless the table declares
``deletion: deactivate | delete`` together with a ``delete_filter``.

Authentication: Azure CLI token (``az login``). Target: the stage named by ``--stage``,
with its URL from ``--env-url/--tenant``, else the ``DATAVERSE_URL/ENVIRONMENT_STAGE/TENANT_ID``
environment variables, else ``<repo-root>/.env.<stage>``. The source must declare the
same stage, and its URL must differ from every other ``.env.<stage>`` file. Outside
``dev``, apply also needs the environment URL typed by the user (``--confirm-url``)
and, when the plan deletes rows, their count (``--confirm-deletes``).

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
import re
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
SCHEMA = "data.schema.json"
REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

EXT = ".dv.data.yaml"
DATA_DIR = "data"
PLAN_DIR = ".dv-data"
PLAN_VERSION = 1
API_VERSION = "v9.2"
DEV = "dev"
STAGE_RE = re.compile(r"^[a-z][a-z0-9-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$")
FILTER_ALL = "all"
KEY_CHUNK = 20  # rows looked up per $filter

# Optional parameters (request headers). CustomAsync does not cover Power Automate
# flows: they have their own parameter, which needs no privilege.
BYPASS_HEADERS = {
    "sync": ("MSCRM.BypassBusinessLogicExecution", "CustomSync"),
    "async": ("MSCRM.BypassBusinessLogicExecution", "CustomAsync"),
    "flows": ("MSCRM.SuppressCallbackRegistrationExpanderJob", "true"),
}
BYPASS_PRIVILEGE = "prvBypassCustomBusinessLogic"
STEP_MESSAGES = ("Create", "Update", "Delete", "SetState", "SetStateDynamicEntity")
STEP_STAGES = {10: "pre-validation", 20: "pre-operation", 40: "post-operation"}

# Column types (AttributeTypeName) the tool writes.
STRING_TYPES = {"StringType", "MemoType"}
INT_TYPES = {"IntegerType", "BigIntType", "PicklistType", "StatusType"}
NUMBER_TYPES = {"DecimalType", "DoubleType", "MoneyType"}
SCALAR_TYPES = STRING_TYPES | INT_TYPES | NUMBER_TYPES | {"BooleanType", "DateTimeType", "MultiSelectPicklistType"}
LOOKUP_TYPES = {"LookupType", "CustomerType", "OwnerType"}
KEY_TYPES = {"StringType", "IntegerType", "BigIntType", "PicklistType", "DecimalType", "DateTimeType"}
OWNER_TARGETS = ("systemuser", "team")
TYPE_HINT = {
    "StringType": "a string (quote numbers)", "MemoType": "a string (quote numbers)",
    "IntegerType": "an integer", "BigIntType": "an integer",
    "PicklistType": "an integer option value", "StatusType": "an integer status value",
    "DecimalType": "a number", "DoubleType": "a number", "MoneyType": "a number",
    "BooleanType": "true or false",
    "DateTimeType": "an ISO date (2024-01-31) or a UTC date-time (2024-01-31T08:00:00Z)",
    "MultiSelectPicklistType": "a list of integer option values",
}
ANNOTATIONS = {"Prefer": 'odata.include-annotations="*"'}
FORMATTED = "@OData.Community.Display.V1.FormattedValue"
TARGET_NAME = "@Microsoft.Dynamics.CRM.lookuplogicalname"

# activate runs first (some tables refuse updates on inactive rows), deletes last.
PHASE = {"activate": 0, "create": 1, "update": 1, "deactivate": 2, "delete": 3}


class GuardError(Exception):
    """Raised when the stage guard refuses to run."""


class ApiError(Exception):
    def __init__(self, status, code, message, path):
        super().__init__(f"{status} {code or ''} {path}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message


class Blocked(Exception):
    """A row value that can't be applied; becomes a '! blocked' plan item."""


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
# Model loading
# --------------------------------------------------------------------------- #
def _require_yaml():
    if yaml is None:
        raise SystemExit(f"ERROR: PyYAML is not installed — pip install -r {REQUIREMENTS}")


def _plain(value):
    """YAML dates and date-times → ISO strings, the Web API format."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def load_yaml(path: Path):
    _require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        return _plain(yaml.safe_load(f))


def find_data_root(start: Path):
    """The data folder for a dir (itself) or a file (its folder, or the parent of its stage folder)."""
    p = start.resolve()
    if p.is_dir():
        return p
    stage_dir = p.parent
    if STAGE_RE.match(stage_dir.name) and (stage_dir.parent.name == DATA_DIR or any(stage_dir.parent.glob("*" + EXT))):
        return stage_dir.parent
    return p.parent


def base_files(root: Path):
    return sorted(p for p in root.glob("*" + EXT) if p.is_file())


def stage_dirs(root: Path):
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and STAGE_RE.match(d.name) and any(d.glob("*" + EXT))) if root.is_dir() else []


def overlay_files(root: Path, stage):
    if not stage or not (root / stage).is_dir():
        return []
    return sorted((root / stage).glob("*" + EXT))


def file_table(path: Path):
    return path.name[: -len(EXT)].lower()


def _rel(path: Path, root: Path):
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def key_columns(value):
    if not value:
        return []
    return [c.lower() for c in ([value] if isinstance(value, str) else value)]


def _token_value(v):
    """Alternate keys compare strings case-insensitively; 5 and 5.0 are the same number."""
    if isinstance(v, str):
        return v.lower()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def key_token(values, cols):
    return json.dumps([_token_value(values.get(c)) for c in cols], ensure_ascii=False, sort_keys=True)


def _fmt(v):
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else f'"{v}"'


def row_label(values, cols):
    return ", ".join(f"{c}={_fmt(values.get(c))}" for c in cols)


def merge_table(base, overlay, root):
    """Base file + stage overlay → one table spec. Overlay rows match base rows by key
    and replace their values column by column; other overlay rows are added."""
    b = base[1] if base else {}
    o = overlay[1] if overlay else {}
    spec = {
        "table": (b.get("table") or o.get("table") or "").lower(),
        "key": key_columns(b.get("key")) or key_columns(o.get("key")),
        "deletion": o.get("deletion") or b.get("deletion") or "none",
        "delete_filter": o.get("delete_filter", b.get("delete_filter")),
        "bypass": list(o.get("bypass", b.get("bypass")) or []),
        "files": [_rel(src[0], root) for src in (base, overlay) if src],
        "rows": [],
    }
    index = {}
    for doc in (b, o):
        for r in doc.get("rows") or []:
            if not isinstance(r, dict):
                continue
            r = {str(c).lower(): v for c, v in r.items()}
            token = key_token(r, spec["key"])
            if token in index:
                index[token].update(r)
            else:
                index[token] = dict(r)
                spec["rows"].append(index[token])
    return spec


def compose(root: Path, stage=None):
    """{table: spec} for one stage: every base file merged with that stage's overlays."""
    entries = {}
    for f in base_files(root):
        entries[file_table(f)] = {"base": (f, load_yaml(f) or {}), "overlay": None}
    for f in overlay_files(root, stage):
        entries.setdefault(file_table(f), {"base": None, "overlay": None})["overlay"] = (f, load_yaml(f) or {})
    return {name: merge_table(e["base"], e["overlay"], root) for name, e in sorted(entries.items())}


def references(spec):
    """(column, reference) for every reference value of a table spec."""
    return [(c, v) for r in spec["rows"] for c, v in r.items() if isinstance(v, dict) and "ref" in v]


def ref_id(ref):
    return json.dumps(ref, sort_keys=True, ensure_ascii=False)


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


def _json_keys(data):
    """YAML allows non-string keys; JSON Schema only knows string keys."""
    if isinstance(data, dict):
        return {str(k): _json_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_json_keys(v) for v in data]
    return data


def _structural(report, data, where):
    if jsonschema is None:
        report.warn(where, "jsonschema not installed — structural validation skipped")
        return
    with open(SCHEMAS_DIR / SCHEMA, "r", encoding="utf-8") as f:
        schema = json.load(f)
    validator = jsonschema.Draft7Validator(schema)
    for e in sorted(validator.iter_errors(_json_keys(data)), key=lambda e: list(e.absolute_path)):
        field = " > ".join(str(p) for p in e.absolute_path) or "(root)"
        report.error(where, f"{e.message} (at {field})")


def check_rows(r, doc, cols, where):
    table = (doc.get("table") or "").lower()
    seen = {}
    for i, row in enumerate(doc.get("rows") or []):
        rw = f"{where}:rows[{i}]"
        row = {str(c).lower(): v for c, v in row.items()}
        missing = [c for c in cols if row.get(c) is None]
        if missing:
            r.error(rw, f"every row sets the key column(s) — missing {', '.join(missing)}")
        for c in cols:
            if isinstance(row.get(c), (dict, list)):
                r.error(rw, f"key column {c} takes a plain value")
        if "statecode" in row:
            r.error(rw, "statecode is not set in rows — deletion: deactivate manages the row state")
        if table and f"{table}id" in row:
            r.error(rw, f"{table}id is the primary GUID — rows never carry it, the alternate key identifies the row")
        if missing:
            continue
        token = key_token(row, cols)
        if token in seen:
            r.error(rw, f"duplicate key {row_label(row, cols)} (also rows[{seen[token]}])")
        else:
            seen[token] = i


def validate_root(root: Path, stage=None) -> Report:
    r = Report()
    if not root.is_dir():
        r.error(str(root), "not a folder")
        return r
    stages = [stage] if stage else [d.name for d in stage_dirs(root)]
    files = [(None, f) for f in base_files(root)] + [(s, f) for s in stages for f in overlay_files(root, s)]
    docs = {}
    for st, f in files:
        where = _rel(f, root)
        try:
            doc = load_yaml(f)
        except yaml.YAMLError as e:
            r.error(where, f"invalid YAML: {e}")
            continue
        if not isinstance(doc, dict):
            r.error(where, "root must be a YAML mapping")
            continue
        before = len(r.errors)
        _structural(r, doc, where)
        if len(r.errors) > before:
            continue
        if (doc.get("table") or "").lower() != file_table(f):
            r.error(where, f"the file name must be {(doc.get('table') or '').lower()}{EXT} (one file per table, named after its logical name)")
        if st is None and not doc.get("key"):
            r.error(where, "key is required in a base file — the alternate key columns that identify each row")
        docs[(st, file_table(f))] = (where, doc)

    for (st, name), (where, doc) in docs.items():
        base = docs.get((None, name)) if st else None
        cols = key_columns(doc.get("key"))
        if st and base is None and not cols:
            r.error(where, "an overlay without a base file declares key")
        if base is not None:
            base_cols = key_columns(base[1].get("key"))
            if cols and cols != base_cols:
                r.error(where, f"key {cols} differs from the base file {base_cols} — a table has one key in every stage")
            cols = cols or base_cols
        check_rows(r, doc, cols, where)
    if r.errors:
        return r

    for st in [None] + stages:
        tables = compose(root, st)
        for name, spec in tables.items():
            where = spec["files"][-1]
            if spec["deletion"] != "none" and not spec["delete_filter"]:
                r.error(where, f"deletion: {spec['deletion']} needs delete_filter — an OData filter of the rows this "
                               f"file owns, or '{FILTER_ALL}' for the whole table")
            if spec["deletion"] == "none" and spec["delete_filter"]:
                r.warn(where, "delete_filter is ignored with deletion: none")
            for col, ref in references(spec):
                target = tables.get(ref["ref"].lower())
                values = {str(k).lower(): v for k, v in (ref.get("key") or {}).items()}
                if (target and values and sorted(values) == sorted(target["key"])
                        and key_token(values, target["key"]) not in {key_token(x, target["key"]) for x in target["rows"]}):
                    r.warn(where, f"{col} references {ref['ref']}[{row_label(values, target['key'])}], which is not in "
                                  f"the model{' for stage ' + st if st else ''} — it must already exist on the server")
    r.warnings = list(dict.fromkeys(r.warnings))
    return r


def validate_path(path: Path, stage=None) -> Report:
    return validate_root(find_data_root(Path(path)), stage)


# --------------------------------------------------------------------------- #
# Stage guard
# --------------------------------------------------------------------------- #
ENV_KEYS = ("DATAVERSE_URL", "ENVIRONMENT_STAGE", "TENANT_ID")
ENV_HELP = ("provide the target with --stage <stage> --env-url <url> [--tenant <id>], "
            "or the DATAVERSE_URL / ENVIRONMENT_STAGE / TENANT_ID environment variables, "
            "or a <repo-root>/.env.<stage> file with the same keys")


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
    parser.add_argument("--stage", required=True, help="target stage (dev, uat, prod…) — never defaults")
    g = parser.add_argument_group("target environment (overrides environment variables and .env.<stage>)")
    g.add_argument("--env-url", help="Dataverse URL of the stage's environment")
    g.add_argument("--tenant", help="Entra tenant id used for the az token")


def env_inputs(args):
    return {"url": getattr(args, "env_url", None), "tenant": getattr(args, "tenant", None)}


def _env_source(repo_root: Path, stage, url=None, tenant=None):
    """The first source that names a URL wins; URL and declared stage never mix sources."""
    if url:
        return "command-line flags", {"DATAVERSE_URL": url, "ENVIRONMENT_STAGE": stage, "TENANT_ID": tenant}
    if os.environ.get("DATAVERSE_URL") or os.environ.get("ENVIRONMENT_STAGE"):
        return "environment variables", {k: os.environ.get(k) for k in ENV_KEYS}
    env_path = repo_root / f".env.{stage}"
    if env_path.is_file():
        return str(env_path), read_dotenv(env_path)
    raise GuardError(f"no environment given for stage '{stage}' — " + ENV_HELP)


def target_environment(repo_root: Path, stage, url=None, tenant=None):
    """Local half of the guard: one source, declaring the requested stage, with a URL no
    other stage uses."""
    stage = (stage or "").lower()
    if not STAGE_RE.match(stage):
        raise GuardError("--stage is required (dev, uat, prod…) — the target stage never defaults")
    source, env = _env_source(repo_root, stage, url, tenant)
    declared = (env.get("ENVIRONMENT_STAGE") or "").lower()
    if declared != stage:
        raise GuardError(f"the target must be declared as stage '{stage}' (found '{declared}' in {source}) "
                         f"— refusing to run")
    url = _norm_url(env.get("DATAVERSE_URL"))
    if not url.startswith("https://"):
        raise GuardError(f"DATAVERSE_URL=https://<org>.crm<n>.dynamics.com is missing in {source}")
    for other in sorted(repo_root.glob(".env.*")):
        other_stage = other.name[len(".env."):]
        if other_stage == stage or not STAGE_RE.match(other_stage) or not other.is_file():
            continue
        if _norm_url(read_dotenv(other).get("DATAVERSE_URL")) == url:
            raise GuardError(f"the {stage} URL from {source} is the same as in {other.name} — refusing to run")
    return {"url": url, "tenant": tenant or env.get("TENANT_ID") or None, "source": source, "stage": stage}


def confirmation_error(plan, confirm_url, confirm_deletes):
    """Outside dev, the user types the environment URL (and the delete count) — never the agent."""
    if plan["stage"] == DEV:
        return None
    deletes = sum(1 for a in plan["actions"] if a["op"] == "delete")
    ask = (f"--confirm-url <URL typed by the user>" + (" --confirm-deletes <count typed by the user>" if deletes else ""))
    if _norm_url(confirm_url) != plan["env_url"]:
        return (f"stage '{plan['stage']}' is not dev — apply needs the environment URL typed by the user. "
                f"Show them {plan['env_url']}, ask them to type it, and pass exactly what they typed: {ask}. "
                f"Never fill it in yourself.")
    if deletes and confirm_deletes != deletes:
        return (f"this plan deletes {deletes} row(s) in stage '{plan['stage']}' — apply needs that count typed by "
                f"the user: --confirm-deletes <count typed by the user>. Never fill it in yourself.")
    return None


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
    """DV_DATA_DEBUG=1 prints every Web API call with its duration (stderr)."""
    if os.environ.get("DV_DATA_DEBUG"):
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
            "User-Agent": "dvac-data",
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


def _q(api, path, **params):
    """Absolute URL with each query option percent-encoded (filters may hold & or #)."""
    query = "&".join(f"${k}={urllib.parse.quote(str(v), safe=chr(39) + '(),$/=:')}" for k, v in params.items() if v)
    return f"{api.base}/{path}" + (f"?{query}" if query else "")


def fetch_all(api, url):
    """Every page of a query, with annotations (formatted values, lookup targets)."""
    items = []
    while url:
        data = api.request("GET", url, headers=ANNOTATIONS)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


def odata_literal(value, atype=None):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and atype != "DateTimeType":
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def key_segment(values, cols, info):
    """Alternate-key address `col='a',col2=2`, URL-encoded."""
    return ",".join(f"{c}={urllib.parse.quote(odata_literal(values[c], info['attrs'][c]['type']), safe=chr(39))}"
                    for c in cols)


def bypass_headers(modes):
    headers = {}
    for m in modes:
        name, value = BYPASS_HEADERS[m]
        headers[name] = f"{headers[name]},{value}" if name in headers else value
    return headers


# --------------------------------------------------------------------------- #
# Server state
# --------------------------------------------------------------------------- #
def normalise_info(raw):
    attrs = {a["LogicalName"]: {"type": (a.get("AttributeTypeName") or {}).get("Value") or "",
                                "create": bool(a.get("IsValidForCreate")),
                                "update": bool(a.get("IsValidForUpdate"))}
             for a in raw.get("Attributes") or []}
    keys = [{"name": k["LogicalName"], "columns": [c.lower() for c in k.get("KeyAttributes") or []],
             "status": k.get("EntityKeyIndexStatus")} for k in raw.get("Keys") or []]
    nav = {}
    for rel in raw.get("ManyToOneRelationships") or []:
        nav.setdefault(rel["ReferencingAttribute"], {})[rel["ReferencedEntity"]] = \
            rel["ReferencingEntityNavigationPropertyName"]
    return {"logical": raw["LogicalName"], "set": raw["EntitySetName"], "pk": raw["PrimaryIdAttribute"],
            "attrs": attrs, "keys": keys, "nav": nav}


def table_info(api, logical, cache):
    logical = logical.lower()
    if logical not in cache:
        raw = api.get_or_none(
            f"EntityDefinitions(LogicalName='{logical}')?$select=LogicalName,EntitySetName,PrimaryIdAttribute"
            "&$expand=Attributes($select=LogicalName,AttributeTypeName,IsValidForCreate,IsValidForUpdate),"
            "Keys($select=LogicalName,KeyAttributes,EntityKeyIndexStatus),"
            "ManyToOneRelationships($select=ReferencingAttribute,ReferencedEntity,"
            "ReferencingEntityNavigationPropertyName)")
        cache[logical] = normalise_info(raw) if raw else None
    return cache[logical]


def find_key(info, cols):
    return next((k for k in info["keys"] if sorted(k["columns"]) == sorted(cols)), None)


def key_error(info, table, cols):
    """Why `cols` can't address rows of `table` by alternate key, or None."""
    missing = [c for c in cols if c not in info["attrs"]]
    if missing:
        return f"column(s) {', '.join(missing)} not found on {table}"
    for c in cols:
        if info["attrs"][c]["type"] not in KEY_TYPES:
            return (f"key column {c} is a {info['attrs'][c]['type']} — key columns must be text, whole number, "
                    f"decimal, choice or date")
    k = find_key(info, cols)
    if not k:
        return (f"no alternate key on {table} with columns ({', '.join(cols)}) — create it first "
                f"(dvac-metadata `keys`, or dataverse:dv-metadata)")
    if k["status"] != "Active":
        return (f"alternate key {k['name']} on {table} is {k['status']}, not Active — Dataverse builds the index "
                f"asynchronously; Failed usually means duplicate values")
    return None


def table_error(info, spec):
    if info is None:
        return f"table {spec['table']} not found in the environment"
    err = key_error(info, spec["table"], spec["key"])
    if err:
        return err
    if spec["deletion"] == "deactivate" and "statecode" not in info["attrs"]:
        return "deletion: deactivate needs a table with a statecode column — use none or delete"
    return None


def server_value(row, col, atype):
    return row.get(f"_{col}_value") if atype in LOOKUP_TYPES else row.get(col)


def server_token(row, cols, info):
    return key_token({c: server_value(row, c, info["attrs"][c]["type"]) for c in cols}, cols)


def read_rows(api, info, spec):
    """Server rows matching the file's keys, and the rows inside delete_filter."""
    cols = spec["key"]
    managed = sorted({c for r in spec["rows"] for c in r} | set(cols))
    state = ["statecode"] if "statecode" in info["attrs"] else []
    select = ",".join([info["pk"], *[c for c in managed if c in info["attrs"] and c != info["pk"]], *state])
    found = {}
    rows = spec["rows"]
    for i in range(0, len(rows), KEY_CHUNK):
        clause = " or ".join("(" + " and ".join(f"{c} eq {odata_literal(r[c], info['attrs'][c]['type'])}"
                                                for c in cols) + ")" for r in rows[i:i + KEY_CHUNK])
        for row in fetch_all(api, _q(api, info["set"], select=select, filter=clause)):
            found[server_token(row, cols, info)] = row
    filtered = None
    if spec["deletion"] != "none":
        flt = None if spec["delete_filter"] == FILTER_ALL else spec["delete_filter"]
        filtered = fetch_all(api, _q(api, info["set"], select=",".join([info["pk"], *cols, *state]), filter=flt))
    return found, filtered


def state_options(api, logical):
    """(statecode, default statuscode) of the active and the inactive state."""
    raw = api.get(f"EntityDefinitions(LogicalName='{logical}')/Attributes(LogicalName='statecode')"
                  "/Microsoft.Dynamics.CRM.StateAttributeMetadata?$select=LogicalName&$expand=OptionSet")
    opts = sorted((o["Value"], o.get("DefaultStatus")) for o in (raw.get("OptionSet") or {}).get("Options") or [])
    if not opts:
        return None
    return {"active": list(opts[0]), "inactive": list(next((o for o in opts if o[0] != opts[0][0]), opts[0]))}


def delete_cascades(api, logical):
    """Custom 1:N relationships whose delete behavior reaches other rows."""
    rels = api.get_all(f"EntityDefinitions(LogicalName='{logical}')/OneToManyRelationships"
                       "?$select=ReferencingEntity,ReferencingAttribute,CascadeConfiguration,IsCustomRelationship")
    return sorted(({"table": rel["ReferencingEntity"], "column": rel["ReferencingAttribute"],
                    "behavior": (rel.get("CascadeConfiguration") or {}).get("Delete", "").lower()}
                   for rel in rels if rel.get("IsCustomRelationship")
                   and (rel.get("CascadeConfiguration") or {}).get("Delete") in ("Cascade", "RemoveLink", "Restrict")),
                  key=lambda c: (c["table"], c["column"]))


def bypassed_logic(api, logical, modes):
    """Custom steps, classic workflows and flows on the table that the bypass skips."""
    out = {"steps": [], "workflows": [], "flows": []}
    wanted = {m for m in ("sync", "async") if m in modes}
    if wanted:
        steps = api.get_all("sdkmessageprocessingsteps?$select=name,mode,stage"
                            f"&$filter=statecode eq 0 and customizationlevel eq 1"
                            f" and sdkmessagefilterid/primaryobjecttypecode eq '{logical}'"
                            "&$expand=sdkmessageid($select=name)")
        out["steps"] = sorted(({"name": s["name"], "message": (s.get("sdkmessageid") or {}).get("name"),
                                "mode": "async" if s.get("mode") == 1 else "sync",
                                "stage": STEP_STAGES.get(s.get("stage"), str(s.get("stage")))}
                               for s in steps if (s.get("sdkmessageid") or {}).get("name") in STEP_MESSAGES
                               and ("async" if s.get("mode") == 1 else "sync") in wanted),
                              key=lambda s: (s["mode"], s["message"], s["name"]))
        flows = api.get_all("workflows?$select=name,mode"
                            f"&$filter=primaryentity eq '{logical}' and category eq 0 and type eq 1 and statecode eq 1")
        out["workflows"] = sorted(({"name": w["name"], "mode": "sync" if w.get("mode") == 1 else "async"}
                                   for w in flows if ("sync" if w.get("mode") == 1 else "async") in wanted),
                                  key=lambda w: (w["mode"], w["name"]))
    if "flows" in modes:
        try:
            cbs = api.get_all(f"callbackregistrations?$select=name&$filter=entityname eq '{logical}'")
            out["flows"] = sorted({c.get("name") or "?" for c in cbs})
        except ApiError as e:
            out["flows"] = [f"(could not list the flow triggers: {e.message})"]
    return out


def has_bypass_privilege(api, user_id):
    try:
        resp = api.get(f"systemusers({user_id})/Microsoft.Dynamics.CRM.RetrieveUserPrivilegeByPrivilegeName"
                       f"(PrivilegeName='{BYPASS_PRIVILEGE}')")
    except ApiError:
        return None
    return bool(resp.get("RolePrivileges"))


def resolve_ref(api, ref, infos):
    """{"id": guid | None} or {"error": reason} for one reference."""
    table = ref["ref"].lower()
    target = table_info(api, table, infos)
    if target is None:
        return {"error": f"table {table} not found in the environment"}
    values = {str(k).lower(): v for k, v in (ref.get("key") or ref.get("match")).items()}
    if "key" in ref:
        err = key_error(target, table, list(values))
        if err:
            return {"error": err}
        row = api.get_or_none(f"{target['set']}({key_segment(values, list(values), target)})?$select={target['pk']}")
        return {"id": row[target["pk"]] if row else None}
    missing = [c for c in values if c not in target["attrs"]]
    if missing:
        return {"error": f"column(s) {', '.join(missing)} not found on {table}"}
    flt = " and ".join(f"{c} eq {odata_literal(v, target['attrs'][c]['type'])}" for c, v in values.items())
    rows = fetch_all(api, _q(api, target["set"], select=target["pk"], filter=flt, top=2))
    if len(rows) != 1:
        return {"error": f"match on {table} found {'no row' if not rows else 'several rows'} — it must find exactly one"}
    return {"id": rows[0][target["pk"]]}


def read_server(api, model, user_id):
    infos = {}
    server = {"tables": {}, "infos": infos, "refs": {}, "privilege": True}
    for name, spec in model["tables"].items():
        t = {"rows": {}, "filtered": None, "state": None, "cascade": [], "bypass": None, "error": None}
        server["tables"][name] = t
        info = table_info(api, name, infos)
        t["error"] = table_error(info, spec)
        if t["error"]:
            continue
        try:
            t["rows"], t["filtered"] = read_rows(api, info, spec)
        except ApiError as e:
            if e.status != 400:
                raise
            t["error"] = f"Dataverse rejected the query (check delete_filter and column names): {e.message}"
            continue
        if spec["deletion"] == "deactivate":
            t["state"] = state_options(api, name)
        if spec["deletion"] == "delete":
            t["cascade"] = delete_cascades(api, name)
        if spec["bypass"]:
            t["bypass"] = bypassed_logic(api, name, spec["bypass"])
        for _col, ref in references(spec):
            if ref_id(ref) not in server["refs"]:
                server["refs"][ref_id(ref)] = resolve_ref(api, ref, infos)
    if any({"sync", "async"} & set(s["bypass"]) for s in model["tables"].values()):
        server["privilege"] = has_bypass_privilege(api, user_id)
    return server


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def scalar_error(value, atype):
    if value is None:
        return None
    if atype in STRING_TYPES:
        ok = isinstance(value, str)
    elif atype in INT_TYPES:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif atype in NUMBER_TYPES:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif atype == "BooleanType":
        ok = isinstance(value, bool)
    elif atype == "DateTimeType":
        ok = isinstance(value, str) and bool(DATE_RE.match(value))
    else:  # MultiSelectPicklistType
        ok = isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    return None if ok else f"expects {TYPE_HINT[atype]}, got {_fmt(value)}"


def encode_scalar(value, atype):
    if atype == "MultiSelectPicklistType":
        return ",".join(str(v) for v in value) if value else None
    return value


def _utc(text):
    d = _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).astimezone(_dt.timezone.utc)


def same_scalar(model, server, atype):
    if atype in STRING_TYPES:
        return (model or "") == (server or "")
    if atype == "MultiSelectPicklistType":
        have = sorted(int(v) for v in str(server or "").split(",") if v.strip())
        return sorted(model or []) == have
    if model is None or server is None:
        return model is None and server is None
    if atype in NUMBER_TYPES:
        return abs(float(model) - float(server)) <= 1e-9 * max(1.0, abs(float(model)))
    if atype == "DateTimeType":
        if len(model) == 10:
            return str(server)[:10] == model
        try:
            return _utc(model) == _utc(server)
        except ValueError:
            return model == server
    return model == server


def is_pending(model, ref):
    """A key reference to a row this plan creates (it is in the model, not on the server)."""
    spec = model["tables"].get(ref["ref"].lower())
    values = {str(k).lower(): v for k, v in (ref.get("key") or {}).items()}
    if not spec or not values or sorted(values) != sorted(spec["key"]):
        return False
    return key_token(values, spec["key"]) in {key_token(r, spec["key"]) for r in spec["rows"]}


def bind_target(col, ref, attr, info, server, model):
    """(navigation property, target GUID or None, bind path, display) for a reference."""
    target = ref["ref"].lower()
    if attr["type"] == "OwnerType":
        if target not in OWNER_TARGETS:
            raise Blocked(f"{col} references systemuser or team, not {target}")
        nav = col
    else:
        targets = info["nav"].get(col) or {}
        nav = targets.get(target)
        if not nav:
            raise Blocked(f"{col} cannot reference {target} (targets: {', '.join(sorted(targets)) or 'none'})")
    res = server["refs"].get(ref_id(ref)) or {"error": "reference not resolved"}
    if res.get("error"):
        raise Blocked(res["error"])
    values = {str(k).lower(): v for k, v in (ref.get("key") or ref.get("match")).items()}
    display = f"{target}[{row_label(values, list(values))}]"
    tinfo = server["infos"][target]
    if res.get("id"):
        return nav, res["id"].lower(), f"/{tinfo['set']}({res['id']})", display
    if "key" in ref and is_pending(model, ref):
        return nav, None, f"/{tinfo['set']}({key_segment(values, list(values), tinfo)})", \
            display + " (created by this plan)"
    raise Blocked(f"referenced row {display} not found")


def _clear_nav(col, attr, info, srow):
    """Navigation property to remove when a lookup is set to null."""
    if attr["type"] == "OwnerType":
        raise Blocked(f"{col} cannot be cleared — every row has an owner")
    targets = info["nav"].get(col) or {}
    nav = targets.get(srow.get(f"_{col}_value{TARGET_NAME}")) or (next(iter(targets.values())) if len(targets) == 1 else None)
    if not nav:
        raise Blocked(f"cannot tell which relationship of {col} to clear")
    return nav


def table_order(tables):
    """Referenced tables first; cycles are broken (links are applied after every create)."""
    deps = {n: {ref["ref"].lower() for _c, ref in references(s)} & set(tables) - {n} for n, s in tables.items()}
    out, seen = [], set()

    def visit(n, stack):
        if n in seen or n in stack:
            return
        stack.add(n)
        for d in sorted(deps[n]):
            visit(d, stack)
        seen.add(n)
        out.append(n)

    for n in sorted(tables):
        visit(n, set())
    return out


def diff_row(name, spec, row, srow, info, server, model, block):
    """create / update action for one model row, or None when it matches the server."""
    cols = spec["key"]
    label = row_label(row, cols)
    body, links, unset, display, changes = {}, {}, [], {}, {}
    for col, value in row.items():
        attr = info["attrs"].get(col)
        try:
            if attr is None:
                raise Blocked(f"column not found on {name}")
            atype = attr["type"]
            if isinstance(value, dict):
                if atype not in LOOKUP_TYPES:
                    raise Blocked(f"a reference needs a lookup column — {col} is a {atype}")
                nav, guid, path, shown = bind_target(col, value, attr, info, server, model)
                if srow is not None:
                    have = (server_value(srow, col, atype) or "").lower() or None
                    if guid is not None and guid == have:
                        continue
                    if not attr["update"]:
                        raise Blocked(f"{col} is not valid for update")
                    changes[col] = [srow.get(f"_{col}_value{FORMATTED}") or _fmt(have), shown]
                elif not attr["create"]:
                    raise Blocked(f"{col} is not valid for create")
                display[col] = shown
                if guid is None:
                    links[nav] = path
                else:
                    body[f"{nav}@odata.bind"] = path
                continue
            if atype in LOOKUP_TYPES:
                if value is not None:
                    raise Blocked(f"{col} is a lookup — it takes a reference {{ref: <table>, key: {{…}}}} or null")
                if srow is None or server_value(srow, col, atype) is None:
                    continue
                if not attr["update"]:
                    raise Blocked(f"{col} is not valid for update")
                unset.append(_clear_nav(col, attr, info, srow))
                changes[col] = [srow.get(f"_{col}_value{FORMATTED}") or _fmt(server_value(srow, col, atype)), "null"]
                continue
            if atype not in SCALAR_TYPES:
                raise Blocked(f"{col} is a {atype or 'virtual'} column — not supported by this tool")
            err = scalar_error(value, atype)
            if err:
                raise Blocked(f"{col} {err}")
            if srow is not None:
                have = server_value(srow, col, atype)
                if same_scalar(value, have, atype):
                    continue
                if not attr["update"]:
                    raise Blocked(f"{col} is not valid for update")
                changes[col] = [_fmt(have), _fmt(value)]
            elif not attr["create"]:
                raise Blocked(f"{col} is not valid for create")
            elif value is None:
                continue
            display[col] = _fmt(value)
            body[col] = encode_scalar(value, atype)
        except Blocked as e:
            block(name, label, col, str(e))
    base = {"table": name, "set": info["set"], "row": label, "body": body, "links": links}
    if srow is None:
        return {"op": "create", **base, "display": display}
    if changes:
        return {"op": "update", **base, "id": srow[info["pk"]], "changes": changes, "unset": unset}
    return None


def diff(model, server):
    actions, blocked, warnings, bypass = [], [], [], []

    def block(table, row, field, reason):
        blocked.append({"table": table, "row": row, "field": field, "reason": reason})

    order = table_order(model["tables"])
    rank = {n: i for i, n in enumerate(order)}
    for name in order:
        spec, t = model["tables"][name], server["tables"][name]
        if t["error"]:
            block(name, "", "table", t["error"])
            continue
        info = server["infos"][name]
        cols, pk = spec["key"], info["pk"]
        filtered = {r[pk] for r in t["filtered"]} if t["filtered"] is not None else None
        verb = "deactivate" if spec["deletion"] == "deactivate" else "delete"
        before = len(actions)
        in_file = set()
        for row in spec["rows"]:
            srow = t["rows"].get(key_token(row, cols))
            label = row_label(row, cols)
            if srow is not None:
                in_file.add(srow[pk])
                if filtered is not None and srow[pk] not in filtered:
                    warnings.append({"table": name, "row": label, "message":
                                     f"outside delete_filter — removing it from the file later will not {verb} it"})
                if t["state"] and srow.get("statecode") != t["state"]["active"][0]:
                    actions.append({"op": "activate", "table": name, "set": info["set"], "row": label,
                                    "id": srow[pk],
                                    "body": {"statecode": t["state"]["active"][0],
                                             "statuscode": t["state"]["active"][1]},
                                    "changes": {"statecode": [_fmt(srow.get("statecode")),
                                                              _fmt(t["state"]["active"][0])]}})
            a = diff_row(name, spec, row, srow, info, server, model, block)
            if a:
                actions.append(a)
        for srow in t["filtered"] or []:
            if srow[pk] in in_file:
                continue
            label = row_label({c: server_value(srow, c, info["attrs"][c]["type"]) for c in cols}, cols)
            if spec["deletion"] == "deactivate":
                if not t["state"] or srow.get("statecode") != t["state"]["active"][0]:
                    continue
                actions.append({"op": "deactivate", "table": name, "set": info["set"], "row": label, "id": srow[pk],
                                "body": {"statecode": t["state"]["inactive"][0],
                                         "statuscode": t["state"]["inactive"][1]}})
            else:
                actions.append({"op": "delete", "table": name, "set": info["set"], "row": label, "id": srow[pk],
                                "cascade": t["cascade"]})
        if spec["bypass"] and len(actions) > before:
            bypass.append({"table": name, "modes": spec["bypass"], **(t["bypass"] or {})})
            if {"sync", "async"} & set(spec["bypass"]):
                if server.get("privilege") is False:
                    block(name, "", "bypass", f"the az user lacks {BYPASS_PRIVILEGE} — an admin must grant it, "
                                              f"or remove sync/async from bypass")
                elif server.get("privilege") is None:
                    warnings.append({"table": name, "row": "", "message":
                                     f"could not check {BYPASS_PRIVILEGE} — apply fails if the az user lacks it"})
    actions.sort(key=lambda a: (PHASE[a["op"]], -rank[a["table"]] if a["op"] == "delete" else rank[a["table"]]))
    return {"actions": actions, "blocked": blocked, "warnings": warnings, "bypass": bypass}


# --------------------------------------------------------------------------- #
# Plan rendering
# --------------------------------------------------------------------------- #
OP_LABEL = {"create": "+ create", "update": "~ update", "activate": "~ activate",
            "deactivate": "- deactivate", "delete": "- delete"}


def _op_color(op):
    return {"create": green, "update": yellow, "activate": yellow}.get(op, red)


def render_plan(plan):
    print(bold("Dataverse data plan"))
    print(f"  env      : {plan['env_url']} ({plan['env_source']})")
    print(f"  stage    : {plan['stage']} — organization {plan.get('org_name') or '?'} ({plan.get('org_id')})")
    if plan.get("az_account"):
        print(f"  az user  : {plan['az_account']}")
    row_warnings = {}
    for w in plan["warnings"]:
        row_warnings.setdefault((w["table"], w["row"]), []).append(w["message"])
    acts = plan["actions"]
    if acts:
        print()
    pad = " " * 16
    for a in acts:
        print(f"  {_op_color(a['op'])(OP_LABEL[a['op']].ljust(12))}  {a['table']}[{a['row']}]")
        for col, value in (a.get("display") or {}).items():
            print(f"{pad}{col}: {value}")
        for col, (old, new) in (a.get("changes") or {}).items():
            print(f"{pad}{col}: {old} → {new}")
        for c in a.get("cascade") or []:
            print(grey(f"{pad}cascade {c['behavior']}: related {c['table']} rows ({c['column']})"))
        for msg in row_warnings.pop((a["table"], a["row"]), []):
            print(yellow(f"{pad}⚠ {msg}"))
    for (table, row), msgs in row_warnings.items():
        for msg in msgs:
            print(yellow(f"  ⚠ {table}" + (f"[{row}]" if row else "") + f"  {msg}"))
    if plan["bypass"]:
        print("\n" + yellow("Bypassed business logic — will NOT run during apply (the user must consent):"))
        for b in plan["bypass"]:
            print(f"  {b['table']}  bypass: {', '.join(b['modes'])}")
            for s in b.get("steps") or []:
                print(f"    step      {s['mode']:<5} {s['message']:<8} {s['stage']:<15} {s['name']}")
            for w in b.get("workflows") or []:
                print(f"    workflow  {w['mode']:<5} {w['name']}")
            for f in b.get("flows") or []:
                print(f"    flow      {f}")
            if not any(b.get(k) for k in ("steps", "workflows", "flows")):
                print(grey("    (nothing registered on this table today)"))
        print(grey("  Plug-ins and workflows from solutions published by Microsoft always run. Power Automate "
                   "flows run unless 'flows' is bypassed."))
    if plan["blocked"]:
        print("\n" + red("Blocked — never applied; fix the model or the environment, then re-plan:"))
        for b in plan["blocked"]:
            where = b["table"] + (f"[{b['row']}]" if b["row"] else "") + (f".{b['field']}" if b["field"] != "table" else "")
            print(f"  {red('!')} {where}")
            print(grey(f"      {b['reason']}"))
    counts = {op: sum(1 for a in acts if a["op"] == op) for op in OP_LABEL}
    print(f"\nSummary: {counts['create']} to create, {counts['update']} to update, {counts['activate']} to activate, "
          f"{counts['deactivate']} to deactivate, {counts['delete']} to delete, {len(plan['blocked'])} blocked.")
    if not acts and not plan["blocked"]:
        print(green("No changes — the environment matches the model."))
    elif acts and not plan["blocked"] and plan["stage"] != DEV:
        print(yellow(f"Stage '{plan['stage']}' is not dev: apply needs --confirm-url with the URL typed by the user"
                     + (f", and --confirm-deletes {counts['delete']} typed by the user" if counts["delete"] else "")
                     + "."))


def _model_hash(root: Path, stage):
    h = hashlib.sha256()
    for f in [*base_files(root), *overlay_files(root, stage)]:
        h.update(_rel(f, root).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _canonical(plan):
    return json.dumps({k: plan.get(k) for k in ("env_url", "org_id", "stage", "actions", "blocked", "bypass")},
                      sort_keys=True, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _validated_model(data_arg, stage):
    root = find_data_root(Path(data_arg))
    if not root.is_dir():
        raise SystemExit(f"ERROR: {data_arg} is not a data folder")
    report = validate_root(root, stage)
    if report.errors:
        for e in report.errors:
            print(red("  ✗ ") + e, file=sys.stderr)
        raise SystemExit(f"ERROR: the model is invalid ({len(report.errors)} error(s)) — run validate and fix it first")
    return root, {"stage": stage, "tables": compose(root, stage)}


def compute_plan(data_arg, stage, env_args=None):
    stage = (stage or "").lower()
    env = target_environment(find_repo_root(Path(data_arg)), stage, **(env_args or {}))
    root, model = _validated_model(data_arg, stage)
    api = Api(env)
    account = az_json(["account", "show"])
    who = api.get("WhoAmI")
    org = api.get_or_none(f"organizations({who['OrganizationId']})?$select=name") or {}
    server = read_server(api, model, who["UserId"])
    plan = {
        "plan_version": PLAN_VERSION,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "stage": stage, "env_url": env["url"], "env_source": env["source"],
        "org_id": who["OrganizationId"], "org_name": org.get("name"),
        "az_account": f"{(account or {}).get('user', {}).get('name', '?')} (tenant {(account or {}).get('tenantId', '?')})",
        "data_dir": str(root), "model_hash": _model_hash(root, stage),
        **diff(model, server),
    }
    return model, api, plan


def plan_exit_code(plan):
    if plan["blocked"]:
        return 1
    return 2 if plan["actions"] else 0


def cmd_validate(args):
    report = validate_path(Path(args.path), (args.stage or "").lower() or None)
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
    _model, _api, plan = compute_plan(args.data, args.stage, env_inputs(args))
    out = (Path(args.out) if args.out
           else find_repo_root(Path(args.data)) / PLAN_DIR / plan["stage"] / "plan.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps({**plan, "plan_file": str(out)}, indent=2, ensure_ascii=False, default=str))
    else:
        render_plan(plan)
        print(grey(f"\nPlan saved to {out}"))
        if plan["actions"] and not plan["blocked"]:
            print(grey(f"Apply it with: dv_data.py apply {args.data} --stage {plan['stage']} --plan-file {out}"
                       + (" --confirm-url <typed by the user>" if plan["stage"] != DEV else "")))
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

    print(bold("dv_data doctor"))
    line(sys.version_info >= (3, 9), f"Python ≥ 3.9 ({sys.version.split()[0]})", "install Python 3.9+")
    line(yaml is not None, "pyyaml installed", f"pip install -r {REQUIREMENTS}")
    line(jsonschema is not None, "jsonschema installed", f"pip install -r {REQUIREMENTS}")
    line(az_path() is not None, "Azure CLI (az) on PATH", "install the Azure CLI")
    stage = args.stage.lower()
    root = find_data_root(Path(args.data)) if args.data else None
    try:
        env = target_environment(find_repo_root(root or Path.cwd()), stage, **env_inputs(args))
        line(True, f"stage guard: {stage} — {env['source']} → {env['url']}")
    except GuardError as e:
        line(False, "stage guard", str(e))
        return 1
    try:
        account = az_json(["account", "show"])
        line(True, f"az session: {account.get('user', {}).get('name')} (tenant {account.get('tenantId')})")
        if env["tenant"] and account.get("tenantId", "").lower() != env["tenant"].lower():
            print(yellow(f"      ⚠ active az tenant differs from the configured TENANT_ID — the token is requested for {env['tenant']}"))
        api = Api(env)
        api.token()
        line(True, f"access token for the {stage} environment")
        who = api.get("WhoAmI")
        line(True, f"WhoAmI: user {who.get('UserId')}, organization {who.get('OrganizationId')}")
        if root and root.is_dir():
            infos = {}
            tables = compose(root, stage)
            for name, spec in tables.items():
                err = table_error(table_info(api, name, infos), spec)
                line(not err, f"{name}: table and alternate key ({', '.join(spec['key'])}) ready", err or "")
            if any({"sync", "async"} & set(s["bypass"]) for s in tables.values()):
                has = has_bypass_privilege(api, who["UserId"])
                line(has is not False, f"{BYPASS_PRIVILEGE} for bypass" + (" (could not check)" if has is None else ""),
                     "an admin must grant it, or remove sync/async from bypass")
    except (GuardError, ApiError) as e:
        line(False, "Dataverse access", str(e))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
class Applier:
    def __init__(self, api, plan):
        self.api = api
        self.headers = {b["table"]: bypass_headers(b["modes"]) for b in plan["bypass"]}
        self.created = {}

    def _send(self, a, method, path, body=None, extra=None, return_id=False):
        headers = {**self.headers.get(a["table"], {}), **(extra or {})}
        return self.api.request(method, path, body, headers=headers, return_id=return_id)

    def create(self, a):
        self.created[(a["table"], a["row"])] = self._send(a, "POST", a["set"], a["body"], return_id=True)

    def update(self, a):
        if a["body"]:
            self._send(a, "PATCH", f"{a['set']}({a['id']})", a["body"], {"If-Match": "*"})
        for nav in a.get("unset") or []:
            self._send(a, "DELETE", f"{a['set']}({a['id']})/{nav}/$ref")

    def activate(self, a):
        self._send(a, "PATCH", f"{a['set']}({a['id']})", a["body"], {"If-Match": "*"})

    deactivate = activate

    def delete(self, a):
        self._send(a, "DELETE", f"{a['set']}({a['id']})")

    def link(self, a):
        rid = a.get("id") or self.created[(a["table"], a["row"])]
        self._send(a, "PATCH", f"{a['set']}({rid})",
                   {f"{nav}@odata.bind": path for nav, path in a["links"].items()}, {"If-Match": "*"})

    def steps(self, actions):
        """(handler name, action): writes, then the links to rows created meanwhile, then removals."""
        writes = [a for a in actions if PHASE[a["op"]] <= 1]
        return ([(a["op"], a) for a in writes] + [("link", a) for a in writes if a.get("links")]
                + [(a["op"], a) for a in actions if PHASE[a["op"]] > 1])


def cmd_apply(args):
    saved = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if saved.get("plan_version") != PLAN_VERSION:
        print(red("ERROR: unsupported plan file version — re-run plan."), file=sys.stderr)
        return 1
    if saved.get("stage") != args.stage.lower():
        print(red(f"ERROR: the plan file is for stage '{saved.get('stage')}', not '{args.stage}'."), file=sys.stderr)
        return 1
    _model, api, fresh = compute_plan(args.data, args.stage, env_inputs(args))
    if _canonical(fresh) != _canonical(saved):
        print(red("ERROR: stale plan — the model or the environment changed since it was computed.\n"
                  "Re-run plan, review it again, and apply the new plan file."), file=sys.stderr)
        return 1
    if fresh["blocked"]:
        print(red("ERROR: the plan contains blocked changes — fix them, re-plan, then apply."), file=sys.stderr)
        return 1
    if not fresh["actions"]:
        print(green("No changes to apply."))
        return 0
    err = confirmation_error(fresh, args.confirm_url, args.confirm_deletes)
    if err:
        print(red(f"REFUSED: {err}"), file=sys.stderr)
        return 1

    applier = Applier(api, fresh)
    steps = applier.steps(fresh["actions"])
    done, failure = 0, None
    print(bold(f"Applying {len(steps)} write(s) to {fresh['env_url']} (stage {fresh['stage']})"))
    for op, a in steps:
        try:
            getattr(applier, op)(a)
            done += 1
            print(f"  {green('✓')} {op:<10} {a['table']}[{a['row']}]", flush=True)
        except (ApiError, KeyError) as e:
            failure = e
            print(f"  {red('✗')} {op:<10} {a['table']}[{a['row']}]\n      {e}", flush=True)
            break
    print(f"\n{done}/{len(steps)} write(s) applied.")
    if failure:
        print(red("Stopped at the first failure. Fix the cause, re-run plan, then apply the new plan."))
        return 1
    _, _, after = compute_plan(args.data, args.stage, env_inputs(args))
    if after["actions"] or after["blocked"]:
        print(red("Post-apply check FAILED — the environment still differs from the model. Remaining differences:"))
        render_plan(after)
        return 1
    print(green("Post-apply plan is empty — the environment matches the model."))
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(prog="dv_data.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor", help="check tooling, az session, stage guard, tables and keys")
    p.add_argument("data", nargs="?", help="data dir (enables the table and key checks)")
    add_env_args(p)
    p = sub.add_parser("validate", help="offline validation of the data model")
    p.add_argument("path")
    p.add_argument("--stage", help="validate the base files and this stage only (default: every stage)")
    p = sub.add_parser("plan", help="read-only diff against the stage's environment")
    p.add_argument("data")
    p.add_argument("--out", help=f"plan file (default <repo-root>/{PLAN_DIR}/<stage>/plan.json)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    add_env_args(p)
    p = sub.add_parser("apply", help="execute a saved plan (aborts if stale)")
    p.add_argument("data")
    p.add_argument("--plan-file", required=True)
    p.add_argument("--confirm-url", help="outside dev: the environment URL, typed by the user")
    p.add_argument("--confirm-deletes", type=int, help="outside dev: the number of deletes, typed by the user")
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


if __name__ == "__main__":
    sys.exit(main())
