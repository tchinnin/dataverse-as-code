"""Offline tests for dv_security.py — validator, diff engine, apply batching, dev guard, hook.

Run:  python3 -m unittest discover -s <skill>/tests -v
"""
import copy
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import dv_security as dv  # noqa: E402

SAMPLES = SKILL / "references" / "samples"
ALL = ["user", "business_unit", "parent_child", "organization"]
ORG_ONLY = ["organization"]


def table_privs(table, levels=ALL, verbs=dv.VERB_TO_TYPE):
    """Privilege metadata of a table, as read_server normalises it."""
    out = {}
    for verb in verbs:
        api = dv.VERB_TO_TYPE[verb]
        name = f"prv{api}{table}"
        out[api] = {"id": f"id-{name}".lower(), "name": name, "levels": list(levels)}
    return out


def server_for(model, existing=False):
    """A server that knows every table and privilege of the model.
    existing=True: the roles exist, in the solution, with exactly the model's privileges."""
    tables, named = {}, {}
    for role in model["roles"]:
        for _src, table, _verb, prv, _lvl in dv.declared_privileges(role["spec"]):
            if table:
                tables[table] = table_privs(table)
            else:
                named[prv.lower()] = {"id": f"id-{prv}".lower(), "name": prv, "levels": ORG_ONLY}
    server = {"business_unit": "bu-root", "roles": {}, "solution_roles": set(),
              "tables": tables, "named": named}
    if existing:
        for i, role in enumerate(model["roles"]):
            spec = role["spec"]
            desired, blocked = dv.resolve_privileges(spec, server)
            assert not blocked, blocked
            rid = f"role-{i}"
            server["roles"][dv.role_key(spec["name"])] = [{
                "id": rid, "name": spec["name"], "description": spec.get("description", ""),
                "member_inheritance": spec.get("member_inheritance", "direct_and_team"),
                "managed": False, "customizable": True,
                "privileges": {pid: {"name": p["name"], "depth": p["depth"]} for pid, p in desired.items()},
            }]
            server["solution_roles"].add(rid)
    return server


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.model = self.tmp / "dataverse" / "security"
        shutil.copytree(SAMPLES, self.model)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit(self, rel, old, new):
        p = self.model / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def errors(self):
        return "\n".join(dv.validate_model(self.model).errors)

    def test_samples_are_valid(self):
        r = dv.validate_model(SAMPLES)
        self.assertEqual((r.errors, r.warnings), ([], []))

    def test_duplicate_role_name_case_insensitive(self):
        self.edit("roles/order-manager.dv.security.yaml", "name: Order Manager", "name: order CLERK")
        self.assertIn("already described", self.errors())

    def test_unknown_level(self):
        self.edit("roles/order-clerk.dv.security.yaml", "read: business_unit", "read: global")
        self.assertIn("global", self.errors())

    def test_unknown_verb(self):
        self.edit("roles/order-clerk.dv.security.yaml", "    write: user", "    update: user")
        self.assertIn("update", self.errors())

    def test_table_key_must_be_a_logical_name(self):
        self.edit("roles/order-clerk.dv.security.yaml", "  account:", "  Account:")
        self.assertIn("Account", self.errors())

    def test_privilege_name_pattern(self):
        self.edit("roles/order-clerk.dv.security.yaml", "prvExportToExcel", "ExportToExcel")
        self.assertIn("ExportToExcel", self.errors())

    def test_privilege_listed_twice_with_other_case(self):
        self.edit("roles/order-clerk.dv.security.yaml", "  prvExportToExcel: organization",
                  "  prvExportToExcel: organization\n  prvexporttoexcel: organization")
        self.assertIn("listed twice", self.errors())

    def test_name_with_spaces_around(self):
        self.edit("roles/order-clerk.dv.security.yaml", "name: Order Clerk", 'name: " Order Clerk"')
        self.assertIn("spaces", self.errors())

    def test_role_file_outside_roles_dir(self):
        shutil.copy(self.model / "roles" / "order-clerk.dv.security.yaml", self.model / "stray.dv.security.yaml")
        self.assertIn("belong in roles/", self.errors())

    def test_single_file_without_meta(self):
        f = self.tmp / "lonely.dv.security.yaml"
        shutil.copy(SAMPLES / "roles" / "order-clerk.dv.security.yaml", f)
        r = dv.validate_path(f)
        self.assertEqual(r.errors, [])
        self.assertIn("cross-file checks skipped", r.warnings[0])


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.model = dv.load_model(SAMPLES)

    def ops(self, plan):
        return [(a["op"], a.get("role") or a["name"], a["name"]) for a in plan["actions"]]

    def test_new_roles_are_created_with_their_full_privilege_set(self):
        plan = dv.diff(self.model, server_for(self.model))
        self.assertEqual([a["op"] for a in plan["actions"]], ["create", "create"])
        clerk = next(a for a in plan["actions"] if a["name"] == "Order Clerk")
        self.assertEqual(len(clerk["privileges"]), 9)
        self.assertIn({"id": "id-prvreadaccount", "name": "prvReadaccount", "depth": "organization",
                       "source": "account.read"}, clerk["privileges"])
        self.assertEqual((clerk["description"], clerk["member_inheritance"]),
                         ("Creates and follows up the orders of their business unit.", "direct_and_team"))

    def test_mirror_is_empty(self):
        plan = dv.diff(self.model, server_for(self.model, existing=True))
        self.assertEqual(plan, {"actions": [], "blocked": [], "unmanaged": []})

    def test_grant_depth_and_revoke(self):
        server = server_for(self.model, existing=True)
        clerk = server["roles"]["order clerk"][0]
        clerk["privileges"].pop("id-prvreadaccount")                                   # → grant
        clerk["privileges"]["id-prvwriteprfx_crm_order"]["depth"] = "organization"     # → depth
        clerk["privileges"]["id-prvdeleteaccount"] = {"name": "prvDeleteAccount", "depth": "user"}  # → revoke
        plan = dv.diff(self.model, server)
        self.assertEqual(self.ops(plan), [("grant", "Order Clerk", "prvReadaccount"),
                                          ("depth", "Order Clerk", "prvWriteprfx_crm_order"),
                                          ("revoke", "Order Clerk", "prvDeleteAccount")])
        self.assertEqual(plan["actions"][1]["changes"], {"depth": ["organization", "user"]})
        self.assertEqual(dv.double_confirmation(plan), ["revoke Order Clerk: prvDeleteAccount (was user)"])

    def test_none_level_means_no_access(self):
        server = server_for(self.model, existing=True)
        self.model["roles"][0]["spec"]["tables"]["account"]["read"] = "none"
        plan = dv.diff(self.model, server)
        self.assertEqual(self.ops(plan), [("revoke", "Order Clerk", "prvReadaccount")])

    def test_role_update_and_solution_membership(self):
        server = server_for(self.model, existing=True)
        clerk = server["roles"]["order clerk"][0]
        clerk["description"] = "old"
        clerk["member_inheritance"] = "team_only"
        server["solution_roles"].discard(clerk["id"])
        plan = dv.diff(self.model, server)
        self.assertEqual(plan["actions"][0]["changes"], {
            "description": ["old", "Creates and follows up the orders of their business unit."],
            "member_inheritance": ["team_only", "direct_and_team"],
            "solution": ["absent", "prfx_crm_Core"]})

    def test_unmanaged_attributes_are_left_alone(self):
        server = server_for(self.model, existing=True)
        manager = server["roles"]["order manager"][0]
        manager["member_inheritance"] = "team_only"   # not in the file → not managed
        self.assertEqual(dv.diff(self.model, server)["actions"], [])

    def test_unknown_table_and_privilege_are_blocked(self):
        server = server_for(self.model)
        server["tables"]["account"] = None
        del server["named"]["prvbulkdelete"]
        blocked = dv.diff(self.model, server)["blocked"]
        self.assertEqual([(b["role"], b["name"], b["reason"]) for b in blocked], [
            ("Order Clerk", "account", "table not found in the environment"),
            ("Order Manager", "prvBulkDelete", "privilege not found in the environment")])

    def test_level_not_supported_is_blocked(self):
        server = server_for(self.model)
        server["tables"]["account"] = table_privs("account", levels=ORG_ONLY)
        self.model["roles"][0]["spec"]["tables"]["account"]["read"] = "user"
        blocked = dv.diff(self.model, server)["blocked"]
        self.assertIn("does not accept level 'user' (allowed: organization)", blocked[0]["reason"])

    def test_missing_verb_on_table_is_blocked(self):
        server = server_for(self.model)
        server["tables"]["prfx_crm_order"] = table_privs("prfx_crm_order", verbs=["create", "read", "write"])
        blocked = dv.diff(self.model, server)["blocked"]
        self.assertTrue(any("has no append privilege" in b["reason"] for b in blocked))

    def test_same_privilege_declared_twice_is_blocked(self):
        server = server_for(self.model)
        self.model["roles"][0]["spec"]["privileges"]["prvReadaccount"] = "organization"
        server["named"]["prvreadaccount"] = {"id": "id-prvreadaccount", "name": "prvReadaccount", "levels": ALL}
        blocked = dv.diff(self.model, server)["blocked"]
        self.assertIn("already granted by account.read", blocked[0]["reason"])

    def test_managed_role_is_blocked(self):
        server = server_for(self.model, existing=True)
        server["roles"]["order clerk"][0]["managed"] = True
        plan = dv.diff(self.model, server)
        self.assertEqual(plan["actions"], [])
        self.assertIn("managed", plan["blocked"][0]["reason"])

    def test_duplicate_server_names_are_blocked(self):
        server = server_for(self.model, existing=True)
        twin = copy.deepcopy(server["roles"]["order clerk"][0])
        twin["id"] = "role-twin"
        server["roles"]["order clerk"].append(twin)
        self.assertIn("2 roles have this name", dv.diff(self.model, server)["blocked"][0]["reason"])

    def test_solution_roles_outside_the_model_are_unmanaged(self):
        server = server_for(self.model, existing=True)
        server["roles"]["auditor"] = [{"id": "role-x", "name": "Auditor", "description": "",
                                       "member_inheritance": None, "managed": False, "customizable": True,
                                       "privileges": None}]
        server["solution_roles"].add("role-x")
        plan = dv.diff(self.model, server)
        self.assertEqual((plan["actions"], plan["unmanaged"]), ([], [{"kind": "role", "name": "Auditor"}]))


class FakeApi:
    url = "https://a-dev.crm4.dynamics.com"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _match(self, path):
        for key, value in self.responses.items():
            if path.startswith(key):
                return copy.deepcopy(value)
        raise AssertionError(f"unexpected call {path}")

    def get(self, path):
        self.calls.append(("GET", path))
        return self._match(path)

    def get_all(self, path):
        self.calls.append(("GET", path))
        return self._match(path)

    def get_or_none(self, path):
        self.calls.append(("GET", path))
        try:
            return self._match(path)
        except AssertionError:
            return None

    def request(self, method, path, body=None, headers=None, return_id=False, fatal=()):
        self.calls.append((method, path, body, headers))
        return "new-role" if return_id else {}


class TestReadServer(unittest.TestCase):
    def test_normalises_roles_privileges_and_levels(self):
        model = {"meta": {"solution": "s"}, "roles": [{"file": "r", "spec": {
            "name": "Clerk", "tables": {"account": {"read": "organization"}},
            "privileges": {"prvExportToExcel": "organization"}}}]}
        api = FakeApi({
            "businessunits": [{"businessunitid": "BU"}],
            "roles?": [{"roleid": "R1", "name": "Clerk ", "description": None, "isinherited": 0,
                        "ismanaged": False, "iscustomizable": {"Value": True}}],
            "solutioncomponents": [{"objectid": "R1"}],
            "RetrieveRolePrivilegesRole(RoleId=r1)": {"RolePrivileges": [
                {"PrivilegeId": "P1", "PrivilegeName": "prvReadAccount", "Depth": "Local"},
                {"PrivilegeId": "P2", "PrivilegeName": "prvWriteAccount", "Depth": 3},
                {"PrivilegeId": "P3", "PrivilegeName": "prvReadContact", "Depth": "Global",
                 "RecordFilterId": "11111111-1111-1111-1111-111111111111"}]},
            "EntityDefinitions(LogicalName='account')": {"Privileges": [
                {"PrivilegeType": "Read", "PrivilegeId": "P1", "Name": "prvReadAccount",
                 "CanBeBasic": True, "CanBeLocal": True, "CanBeDeep": False, "CanBeGlobal": True}]},
            "privileges?": [{"privilegeid": "P9", "name": "prvExportToExcel", "canbebasic": False,
                             "canbelocal": False, "canbedeep": False, "canbeglobal": True}],
        })
        s = dv.read_server(api, model, "SOL")
        role = s["roles"]["clerk"][0]
        self.assertEqual((role["id"], role["member_inheritance"], role["privileges"]), ("r1", "team_only", {
            "p1": {"name": "prvReadAccount", "depth": "business_unit"},
            "p2": {"name": "prvWriteAccount", "depth": "organization"}}))
        self.assertEqual(s["solution_roles"], {"r1"})
        self.assertEqual(s["tables"]["account"]["Read"]["levels"], ["user", "business_unit", "organization"])
        self.assertEqual(s["named"]["prvexporttoexcel"]["levels"], ["organization"])
        plan = dv.diff(model, s)
        self.assertEqual([(a["op"], a["name"]) for a in plan["actions"]],
                         [("grant", "prvExportToExcel"), ("depth", "prvReadAccount"),
                          ("revoke", "prvWriteAccount")])


class TestApply(unittest.TestCase):
    def test_batches_group_grants_of_one_role(self):
        acts = [{"op": "update", "role_id": "a"},
                {"op": "grant", "role_id": "a"}, {"op": "depth", "role_id": "a"},
                {"op": "grant", "role_id": "b"}, {"op": "revoke", "role_id": "a"}]
        self.assertEqual([len(b) for b in dv.batches(acts)], [1, 2, 1, 1])

    def test_create_sets_the_exact_privilege_set(self):
        api = FakeApi({})
        ap = dv.Applier(api, "sol", "bu")
        ap.run([{"op": "create", "kind": "role", "name": "Clerk", "description": "d",
                 "member_inheritance": "team_only",
                 "privileges": [{"id": "p1", "name": "prvReadAccount", "depth": "business_unit", "source": "x"}]}])
        (m1, p1, b1, h1), (m2, p2, b2, _), (m3, p3, b3, _) = api.calls
        self.assertEqual((m1, p1, b1["isinherited"], b1["businessunitid@odata.bind"], h1),
                         ("POST", "roles", 0, "businessunits(bu)", {dv.SOLUTION_HEADER: "sol"}))
        self.assertEqual((p2, b2["ComponentType"], b2["ComponentId"]), ("AddSolutionComponent", 20, "new-role"))
        self.assertEqual((p3, b3), ("roles(new-role)/Microsoft.Dynamics.CRM.ReplacePrivilegesRole",
                                    {"Privileges": [{"PrivilegeId": "p1", "Depth": "Local", "BusinessUnitId": "bu"}]}))

    def test_grant_and_revoke_calls(self):
        api = FakeApi({})
        ap = dv.Applier(api, "sol", "bu")
        ap.run([{"op": "grant", "role_id": "r", "privilege_id": "p1", "depth": "user"},
                {"op": "depth", "role_id": "r", "privilege_id": "p2", "depth": "organization"}])
        ap.run([{"op": "revoke", "role_id": "r", "privilege_id": "p3"}])
        self.assertEqual(api.calls, [
            ("POST", "roles(r)/Microsoft.Dynamics.CRM.AddPrivilegesRole", {"Privileges": [
                {"PrivilegeId": "p1", "Depth": "Basic", "BusinessUnitId": "bu"},
                {"PrivilegeId": "p2", "Depth": "Global", "BusinessUnitId": "bu"}]}, None),
            ("POST", "roles(r)/Microsoft.Dynamics.CRM.RemovePrivilegeRole", {"PrivilegeId": "p3"}, None)])

    def test_stale_plan_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = {"plan_version": dv.PLAN_VERSION, "env_url": "u", "solution": "s",
                     "actions": [{"op": "grant"}], "blocked": []}
            fresh = dict(saved, actions=[{"op": "revoke"}])
            pf = tmp / "plan.json"
            pf.write_text(json.dumps(saved), encoding="utf-8")
            with mock.patch.object(dv, "compute_plan", return_value=(None, None, None, None, {}, fresh)):
                self.assertEqual(dv.main(["apply", str(SAMPLES), "--plan-file", str(pf)]), 1)
        finally:
            shutil.rmtree(tmp)


class TestDevGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        clean = {k: v for k, v in os.environ.items() if k not in dv.ENV_KEYS}
        self._env = mock.patch.dict(os.environ, clean, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.root)

    def write(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")

    def test_stage_must_be_dev(self):
        self.write(".env.dev", "ENVIRONMENT_STAGE=uat\nDATAVERSE_URL=https://a.crm4.dynamics.com\n")
        with self.assertRaisesRegex(dv.GuardError, "stage 'dev'"):
            dv.dev_environment(self.root)

    def test_same_url_as_uat(self):
        self.write(".env.dev", "ENVIRONMENT_STAGE=dev\nDATAVERSE_URL=https://a.crm4.dynamics.com\n")
        self.write(".env.uat", "ENVIRONMENT_STAGE=uat\nDATAVERSE_URL=https://a.crm4.dynamics.com/\n")
        with self.assertRaisesRegex(dv.GuardError, ".env.uat"):
            dv.dev_environment(self.root)

    def test_managed_solution_is_refused(self):
        api = FakeApi({"solutions": [{"solutionid": "s1", "uniquename": "x", "ismanaged": True}]})
        with self.assertRaisesRegex(dv.GuardError, "MANAGED"):
            dv.check_solution(api, "x")


class TestSharedBlock(unittest.TestCase):
    """The dev guard and Web API client are copied from dv_metadata.py (plugins are
    installed independently, so no cross-plugin import). Detect drift."""
    METADATA = SKILL.parents[2] / "dataverse-metadata-as-code" / "skills" / "dvac-metadata" / "scripts"
    SHARED = ("GuardError", "ApiError", "_c", "green", "yellow", "red", "grey", "bold", "Report",
              "ENV_KEYS", "ENV_HELP", "find_repo_root", "read_dotenv", "_norm_url", "add_env_args",
              "env_inputs", "_env_source", "dev_environment", "az_path", "az_json",
              "_debug", "Api", "check_solution",
              "DEPENDENCIES", "venv_dir", "venv_python", "ensure_deps", "token_account", "command_line",
              "print_status", "print_error")
    ALLOWED = {"dvac-security": "dvac-metadata", "DV_SECURITY_DEBUG": "DV_METADATA_DEBUG"}

    def test_shared_code_is_identical(self):
        if not (self.METADATA / "dv_metadata.py").is_file():
            self.skipTest("dataverse-metadata-as-code is not in this checkout")
        sys.path.insert(0, str(self.METADATA))
        try:
            import dv_metadata as meta
        finally:
            sys.path.remove(str(self.METADATA))
        for name in self.SHARED:
            if not callable(getattr(dv, name)):
                self.assertEqual(getattr(dv, name), getattr(meta, name), f"{name} drifted from dv_metadata.py")
                continue
            ours = inspect.getsource(getattr(dv, name))
            for a, b in self.ALLOWED.items():
                ours = ours.replace(a, b)
            self.assertEqual(ours, inspect.getsource(getattr(meta, name)), f"{name} drifted from dv_metadata.py")


class TestHook(unittest.TestCase):
    HOOK = SKILL / "scripts" / "hook_validate.py"

    def run_hook(self, path):
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})
        return subprocess.run([sys.executable, str(self.HOOK)], input=payload,
                              capture_output=True, text=True)

    def test_ignores_other_files(self):
        res = self.run_hook("/somewhere/app.dv.metadata.yaml")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_blocks_invalid_model(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            model = tmp / "security"
            shutil.copytree(SAMPLES, model)
            f = model / "roles" / "order-clerk.dv.security.yaml"
            f.write_text(f.read_text(encoding="utf-8").replace("read: business_unit", "read: everyone"),
                         encoding="utf-8")
            res = self.run_hook(f)
            self.assertEqual(res.returncode, 2)
            self.assertIn("everyone", res.stderr)
        finally:
            shutil.rmtree(tmp)


class TestAdopt(unittest.TestCase):
    RESPONSES = {
        "businessunits": [{"businessunitid": "BU"}],
        "roles?": [{"roleid": "R1", "name": "Order Clerk", "description": "Clerks", "isinherited": 1,
                    "ismanaged": False, "iscustomizable": {"Value": True}}],
        "RetrieveRolePrivilegesRole(RoleId=r1)": {"RolePrivileges": [
            {"PrivilegeId": "P1", "PrivilegeName": "prvReadAccount", "Depth": "Global"},
            {"PrivilegeId": "P2", "PrivilegeName": "prvWriteAccount", "Depth": "Basic"},
            {"PrivilegeId": "P9", "PrivilegeName": "prvExportToExcel", "Depth": "Global"}]},
        "EntityDefinitions?": [{"LogicalName": "account", "Privileges": [
            {"PrivilegeType": "Read", "PrivilegeId": "P1", "Name": "prvReadAccount"},
            {"PrivilegeType": "Write", "PrivilegeId": "P2", "Name": "prvWriteAccount"}]}],
        "EntityDefinitions(LogicalName='account')": {"Privileges": [
            {"PrivilegeType": "Read", "PrivilegeId": "P1", "Name": "prvReadAccount", "CanBeBasic": True,
             "CanBeLocal": True, "CanBeDeep": True, "CanBeGlobal": True},
            {"PrivilegeType": "Write", "PrivilegeId": "P2", "Name": "prvWriteAccount", "CanBeBasic": True,
             "CanBeLocal": True, "CanBeDeep": True, "CanBeGlobal": True}]},
        "privileges?": [{"privilegeid": "P9", "name": "prvExportToExcel", "canbeglobal": True}],
        "solutioncomponents": [{"objectid": "R1"}],
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.model = self.tmp / "dataverse" / "security"
        self.model.mkdir(parents=True)
        shutil.copy(SAMPLES / "_meta.dv.security.yaml", self.model)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def adopt(self, *extra):
        import contextlib, io
        api = FakeApi(self.RESPONSES)
        out = io.StringIO()
        with mock.patch.object(dv, "Api", return_value=api), contextlib.redirect_stdout(out):
            code = dv.main(["adopt", str(self.model), "order clerk", "--env-url", "https://a-dev.crm4.dynamics.com",
                            "--stage", "dev", *extra])
        return code, out.getvalue()

    def test_adopted_file_plans_empty(self):
        code, out = self.adopt()
        self.assertEqual(code, 0, out)
        self.assertIn("STATUS: written", out)
        f = self.model / "roles" / "order-clerk.dv.security.yaml"
        self.assertEqual(dv.load_yaml(f), {
            "name": "Order Clerk", "description": "Clerks", "member_inheritance": "direct_and_team",
            "tables": {"account": {"read": "organization", "write": "user"}},
            "privileges": {"prvExportToExcel": "organization"}})
        self.assertEqual(dv.validate_model(self.model).errors, [])
        model = dv.load_model(self.model)
        plan = dv.diff(model, dv.read_server(FakeApi(self.RESPONSES), model, "SOL"))
        self.assertEqual((plan["actions"], plan["blocked"]), ([], []))

    def test_refuses_to_overwrite(self):
        self.adopt()
        code, out = self.adopt()
        self.assertEqual(code, 1)
        self.assertIn("already described", out)


if __name__ == "__main__":
    unittest.main()
