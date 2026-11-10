"""Offline tests for dv_data.py — validator, overlays, stage guard, diff engine, apply, hook.

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
import urllib.parse
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import dv_data as dv  # noqa: E402

SAMPLES = SKILL / "references" / "samples"
DEV_URL = "https://contoso-dev.crm4.dynamics.com"
PROD_URL = "https://contoso.crm4.dynamics.com"


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def errors_of(root, stage=None):
    return dv.validate_root(root, stage).errors


# --------------------------------------------------------------------------- #
# Fake server state (what read_server returns)
# --------------------------------------------------------------------------- #
def info(logical, set_name, attrs, key=("prfx_code",), status="Active", nav=None):
    return {"logical": logical, "set": set_name, "pk": f"{logical}id",
            "attrs": {f"{logical}id": {"type": "UniqueidentifierType", "create": True, "update": False},
                      **{n: {"type": t, "create": True, "update": True} for n, t in attrs.items()}},
            "keys": [{"name": f"{logical}_key", "columns": list(key), "status": status}],
            "nav": nav or {}}


REGION = info("prfx_region", "prfx_regions", {"prfx_code": "StringType", "prfx_name": "StringType"})
PARAMETER = info("prfx_parameter", "prfx_parameters",
                 {"prfx_code": "StringType", "prfx_value": "StringType", "prfx_origin": "PicklistType",
                  "prfx_regionid": "LookupType", "ownerid": "OwnerType", "statecode": "StateType",
                  "statuscode": "StatusType", "prfx_count": "IntegerType", "prfx_rate": "DecimalType",
                  "prfx_since": "DateTimeType", "prfx_tags": "MultiSelectPicklistType", "prfx_on": "BooleanType",
                  "prfx_image": "ImageType"},
                 nav={"prfx_regionid": {"prfx_region": "prfx_RegionId"}})
TEAM = info("team", "teams", {"name": "StringType"}, key=())


def table_state(rows=(), filtered=None, state=None, error=None, cascade=(), bypass=None, key=("prfx_code",)):
    return {"rows": {dv.key_token(r, list(key)): r for r in rows}, "filtered": filtered,
            "state": state, "cascade": list(cascade), "bypass": bypass, "error": error}


def model_of(**tables):
    return {"stage": "dev", "tables": tables}


def spec(table, rows, key=("prfx_code",), deletion="none", delete_filter=None, bypass=()):
    return {"table": table, "key": list(key), "deletion": deletion, "delete_filter": delete_filter,
            "bypass": list(bypass), "files": [f"{table}{dv.EXT}"], "rows": [dict(r) for r in rows]}


def server_of(tables, refs=None, privilege=True):
    return {"tables": tables, "infos": {"prfx_region": REGION, "prfx_parameter": PARAMETER, "team": TEAM},
            "refs": refs or {}, "privilege": privilege}


EU = {"ref": "prfx_region", "key": {"prfx_code": "EU"}}
ACTIVE = {"active": [0, 1], "inactive": [1, 2]}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
class TestValidate(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "dataverse" / "data"
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root.parent.parent)

    def test_samples_are_valid(self):
        report = dv.validate_root(SAMPLES)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_yaml_dates_become_iso_strings(self):
        f = write(self.root, "prfx_event.dv.data.yaml",
                  "table: prfx_event\nkey: prfx_code\nrows:\n  - {prfx_code: A, prfx_on: 2024-01-31, "
                  "prfx_at: 2024-01-31 08:00:00}\n")
        self.assertEqual(errors_of(self.root), [])
        row = dv.load_yaml(f)["rows"][0]
        self.assertEqual((row["prfx_on"], row["prfx_at"]), ("2024-01-31", "2024-01-31T08:00:00Z"))

    def test_base_needs_key(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nrows:\n  - {prfx_code: EU}\n")
        self.assertTrue(any("key is required" in e for e in errors_of(self.root)))

    def test_file_name_is_the_table(self):
        write(self.root, "region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\n")
        self.assertTrue(any("file name must be prfx_region.dv.data.yaml" in e for e in errors_of(self.root)))

    def test_logical_names_are_lowercase(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\nrows:\n  - {prfx_Code: EU}\n")
        self.assertTrue(errors_of(self.root))

    def test_row_must_set_the_key(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\nrows:\n  - {prfx_name: X}\n")
        self.assertTrue(any("missing prfx_code" in e for e in errors_of(self.root)))

    def test_duplicate_keys_ignore_case(self):
        write(self.root, "prfx_region.dv.data.yaml",
              "table: prfx_region\nkey: prfx_code\nrows:\n  - {prfx_code: EU}\n  - {prfx_code: eu}\n")
        self.assertTrue(any("duplicate key" in e for e in errors_of(self.root)))

    def test_no_guid_and_no_statecode(self):
        write(self.root, "prfx_region.dv.data.yaml",
              "table: prfx_region\nkey: prfx_code\nrows:\n"
              "  - {prfx_code: EU, prfx_regionid: 00000000-0000-0000-0000-000000000001, statecode: 1}\n")
        errs = errors_of(self.root)
        self.assertTrue(any("primary GUID" in e for e in errs))
        self.assertTrue(any("statecode is not set" in e for e in errs))

    def test_deletion_needs_a_filter(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\ndeletion: delete\n")
        self.assertTrue(any("needs delete_filter" in e for e in errors_of(self.root)))
        write(self.root, "prfx_region.dv.data.yaml",
              "table: prfx_region\nkey: prfx_code\ndeletion: delete\ndelete_filter: all\n")
        self.assertEqual(errors_of(self.root), [])

    def test_overlay_can_turn_deletion_on_only_with_a_filter(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\n")
        write(self.root, "prod/prfx_region.dv.data.yaml", "table: prfx_region\ndeletion: deactivate\n")
        self.assertTrue(any("needs delete_filter" in e for e in errors_of(self.root)))

    def test_filter_without_deletion_is_a_warning(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\ndelete_filter: all\n")
        self.assertTrue(any("ignored" in w for w in dv.validate_root(self.root).warnings))

    def test_unknown_deletion_value(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\ndeletion: purge\n")
        self.assertTrue(errors_of(self.root))

    def test_overlay_key_must_match_base(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\n")
        write(self.root, "uat/prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_name\n")
        self.assertTrue(any("differs from the base file" in e for e in errors_of(self.root)))

    def test_overlay_without_base_needs_key(self):
        write(self.root, "uat/prfx_region.dv.data.yaml", "table: prfx_region\nrows:\n  - {prfx_code: EU}\n")
        self.assertTrue(any("without a base file declares key" in e for e in errors_of(self.root)))

    def test_reference_value_shape(self):
        write(self.root, "prfx_city.dv.data.yaml",
              "table: prfx_city\nkey: prfx_code\nrows:\n  - {prfx_code: P, prfx_regionid: {ref: prfx_region}}\n")
        self.assertTrue(errors_of(self.root))

    def test_reference_to_a_row_missing_from_the_model_warns(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\nrows:\n  - {prfx_code: EU}\n")
        write(self.root, "prfx_city.dv.data.yaml",
              "table: prfx_city\nkey: prfx_code\nrows:\n"
              "  - {prfx_code: P, prfx_regionid: {ref: prfx_region, key: {prfx_code: AS}}}\n")
        report = dv.validate_root(self.root)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("must already exist on the server" in w for w in report.warnings))

    def test_validate_path_finds_the_data_root_from_a_stage_file(self):
        write(self.root, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\n")
        f = write(self.root, "prod/prfx_region.dv.data.yaml", "table: prfx_region\nrows:\n  - {prfx_code: EU}\n")
        self.assertEqual(dv.validate_path(f).errors, [])


class TestCompose(unittest.TestCase):
    def test_overlay_merges_rows_by_key(self):
        tables = dv.compose(SAMPLES, "prod")
        rows = {r["prfx_code"]: r for r in tables["prfx_parameter"]["rows"]}
        self.assertEqual(rows["SUPPORT_EMAIL"]["prfx_value"], "support@contoso.com")
        self.assertEqual(rows["SUPPORT_EMAIL"]["prfx_origin"], 100000000)  # kept from base
        self.assertEqual(rows["SUPPORT_EMAIL"]["ownerid"], {"ref": "team", "match": {"name": "Config Admins"}})
        self.assertEqual(tables["prfx_parameter"]["deletion"], "deactivate")  # inherited
        self.assertEqual(tables["prfx_parameter"]["files"],
                         ["prfx_parameter.dv.data.yaml", "prod/prfx_parameter.dv.data.yaml"])

    def test_other_stages_get_the_base_only(self):
        rows = {r["prfx_code"]: r for r in dv.compose(SAMPLES, "uat")["prfx_parameter"]["rows"]}
        self.assertEqual(rows["SUPPORT_EMAIL"]["prfx_value"], "support-test@contoso.com")
        self.assertNotIn("ownerid", rows["SUPPORT_EMAIL"])

    def test_overlay_adds_rows_and_overrides_settings(self):
        root = Path(tempfile.mkdtemp())
        try:
            write(root, "prfx_region.dv.data.yaml",
                  "table: prfx_region\nkey: prfx_code\ndeletion: delete\ndelete_filter: all\nrows:\n  - {prfx_code: EU}\n")
            write(root, "prod/prfx_region.dv.data.yaml",
                  "table: prfx_region\ndeletion: deactivate\nrows:\n  - {prfx_code: AS}\n")
            t = dv.compose(root, "prod")["prfx_region"]
            self.assertEqual([r["prfx_code"] for r in t["rows"]], ["EU", "AS"])
            self.assertEqual((t["deletion"], t["delete_filter"]), ("deactivate", "all"))
        finally:
            shutil.rmtree(root)


# --------------------------------------------------------------------------- #
# Stage guard
# --------------------------------------------------------------------------- #
class TestStageGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in dv.ENV_KEYS},
                                   clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.root)

    def dotenv(self, stage, url, declared=None):
        write(self.root, f".env.{stage}", f"ENVIRONMENT_STAGE={declared or stage}\nDATAVERSE_URL={url}\n")

    def test_stage_is_required(self):
        with self.assertRaisesRegex(dv.GuardError, "--stage is required"):
            dv.target_environment(self.root, None)

    def test_reads_the_stage_dotenv(self):
        self.dotenv("dev", DEV_URL)
        self.dotenv("prod", PROD_URL)
        env = dv.target_environment(self.root, "prod")
        self.assertEqual((env["url"], env["stage"]), (PROD_URL, "prod"))
        self.assertTrue(env["source"].endswith(".env.prod"))

    def test_declared_stage_must_match(self):
        self.dotenv("prod", PROD_URL, declared="dev")
        with self.assertRaisesRegex(dv.GuardError, "declared as stage 'prod'"):
            dv.target_environment(self.root, "prod")

    def test_url_used_by_another_stage_is_refused(self):
        self.dotenv("dev", PROD_URL)
        self.dotenv("prod", PROD_URL + "/")
        with self.assertRaisesRegex(dv.GuardError, "same as in .env.dev"):
            dv.target_environment(self.root, "prod")

    def test_flags_win_and_carry_the_stage(self):
        self.dotenv("uat", "https://contoso-uat.crm4.dynamics.com")
        env = dv.target_environment(self.root, "uat", url="https://other-uat.crm4.dynamics.com")
        self.assertEqual((env["url"], env["source"]), ("https://other-uat.crm4.dynamics.com", "command-line flags"))

    def test_environment_variables_must_declare_the_stage(self):
        with mock.patch.dict(os.environ, {"DATAVERSE_URL": DEV_URL, "ENVIRONMENT_STAGE": "dev"}):
            with self.assertRaisesRegex(dv.GuardError, "declared as stage 'prod'"):
                dv.target_environment(self.root, "prod")
            self.assertEqual(dv.target_environment(self.root, "dev")["url"], DEV_URL)

    def test_missing_source(self):
        with self.assertRaisesRegex(dv.GuardError, "no environment given for stage 'uat'"):
            dv.target_environment(self.root, "uat")


class TestConfirmation(unittest.TestCase):
    def plan(self, stage, deletes=0):
        return {"stage": stage, "env_url": PROD_URL,
                "actions": [{"op": "create"}] + [{"op": "delete"}] * deletes}

    def test_dev_needs_no_typed_confirmation(self):
        self.assertIsNone(dv.confirmation_error(self.plan("dev", 2), None, None))

    def test_outside_dev_the_url_is_required(self):
        self.assertIn("typed by the user", dv.confirmation_error(self.plan("prod"), None, None))
        self.assertIn("typed by the user", dv.confirmation_error(self.plan("prod"), DEV_URL, None))
        self.assertIsNone(dv.confirmation_error(self.plan("prod"), PROD_URL.upper() + "/", None))

    def test_outside_dev_deletes_need_their_count(self):
        self.assertIn("deletes 3 row(s)", dv.confirmation_error(self.plan("uat", 3), PROD_URL, None))
        self.assertIn("deletes 3 row(s)", dv.confirmation_error(self.plan("uat", 3), PROD_URL, 2))
        self.assertIsNone(dv.confirmation_error(self.plan("uat", 3), PROD_URL, 3))


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
class TestDiff(unittest.TestCase):
    def run_diff(self, rows, server_rows=(), refs=None, deletion="none", delete_filter=None, bypass=(),
                 filtered=None, state=None, cascade=(), bypass_state=None, privilege=True):
        tables = {"prfx_parameter": spec("prfx_parameter", rows, deletion=deletion, delete_filter=delete_filter,
                                         bypass=bypass)}
        tstate = table_state(server_rows, filtered=filtered, state=state, cascade=cascade, bypass=bypass_state)
        return dv.diff(model_of(**tables), server_of({"prfx_parameter": tstate}, refs, privilege))

    def test_create(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_value": "1", "prfx_tags": [2, 1], "prfx_count": None}])
        (a,) = out["actions"]
        self.assertEqual(a["op"], "create")
        self.assertEqual(a["row"], 'prfx_code="A"')
        self.assertEqual(a["body"], {"prfx_code": "A", "prfx_value": "1", "prfx_tags": "2,1"})

    def test_no_change(self):
        server = {"prfx_parameterid": "g1", "prfx_code": "a", "prfx_value": "1", "prfx_rate": 2.0,
                  "prfx_since": "2024-01-31T00:00:00Z", "prfx_tags": "1,2", "prfx_on": False}
        out = self.run_diff([{"prfx_code": "a", "prfx_value": "1", "prfx_rate": 2, "prfx_since": "2024-01-31",
                              "prfx_tags": [2, 1], "prfx_on": False}], [server])
        self.assertEqual(out["actions"], [])

    def test_update_lists_old_and_new(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_value": "2"}],
                            [{"prfx_parameterid": "g1", "prfx_code": "A", "prfx_value": "1"}])
        (a,) = out["actions"]
        self.assertEqual((a["op"], a["id"], a["changes"], a["body"]),
                         ("update", "g1", {"prfx_value": ['"1"', '"2"']}, {"prfx_value": "2"}))

    def test_key_matches_case_insensitively(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_value": "1"}],
                            [{"prfx_parameterid": "g1", "prfx_code": "a", "prfx_value": "1"}])
        self.assertEqual([a["op"] for a in out["actions"]], ["update"])  # only the key's case differs

    def test_reference_to_an_existing_row_is_bound_by_guid(self):
        refs = {dv.ref_id(EU): {"id": "R-EU"}}
        out = self.run_diff([{"prfx_code": "A", "prfx_regionid": EU}], refs=refs)
        self.assertEqual(out["actions"][0]["body"]["prfx_RegionId@odata.bind"], "/prfx_regions(R-EU)")
        same = self.run_diff([{"prfx_code": "A", "prfx_regionid": EU}],
                             [{"prfx_parameterid": "g1", "prfx_code": "A", "_prfx_regionid_value": "r-eu"}], refs=refs)
        self.assertEqual(same["actions"], [])

    def test_reference_to_a_row_this_plan_creates_is_linked_after(self):
        tables = {"prfx_region": spec("prfx_region", [{"prfx_code": "EU"}]),
                  "prfx_parameter": spec("prfx_parameter", [{"prfx_code": "A", "prfx_regionid": EU}])}
        server = server_of({"prfx_region": table_state(), "prfx_parameter": table_state()},
                           {dv.ref_id(EU): {"id": None}})
        out = dv.diff(model_of(**tables), server)
        self.assertEqual([a["table"] for a in out["actions"]], ["prfx_region", "prfx_parameter"])
        a = out["actions"][1]
        self.assertEqual(a["links"], {"prfx_RegionId": "/prfx_regions(prfx_code='EU')"})
        self.assertIn("created by this plan", a["display"]["prfx_regionid"])

    def test_missing_reference_is_blocked(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_regionid": EU}], refs={dv.ref_id(EU): {"id": None}})
        self.assertIn("not found", out["blocked"][0]["reason"])

    def test_owner_by_match(self):
        team = {"ref": "team", "match": {"name": "Config Admins"}}
        out = self.run_diff([{"prfx_code": "A", "ownerid": team}], refs={dv.ref_id(team): {"id": "T1"}})
        self.assertEqual(out["actions"][0]["body"]["ownerid@odata.bind"], "/teams(T1)")
        bad = self.run_diff([{"prfx_code": "A", "ownerid": EU}], refs={dv.ref_id(EU): {"id": "R"}})
        self.assertIn("systemuser or team", bad["blocked"][0]["reason"])

    def test_clearing_a_lookup_removes_the_reference(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_regionid": None}],
                            [{"prfx_parameterid": "g1", "prfx_code": "A", "_prfx_regionid_value": "r1",
                              "_prfx_regionid_value@OData.Community.Display.V1.FormattedValue": "Europe"}])
        a = out["actions"][0]
        self.assertEqual((a["unset"], a["changes"]["prfx_regionid"]), (["prfx_RegionId"], ["Europe", "null"]))

    def test_value_errors_are_blocked(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_count": "3", "prfx_unknown": 1, "prfx_image": "x",
                              "prfx_regionid": "EU", "prfx_value": {"ref": "prfx_region", "key": {"prfx_code": "EU"}}}],
                            refs={dv.ref_id(EU): {"id": "R"}})
        reasons = {b["field"]: b["reason"] for b in out["blocked"]}
        self.assertIn("expects an integer", reasons["prfx_count"])
        self.assertIn("not found on prfx_parameter", reasons["prfx_unknown"])
        self.assertIn("not supported", reasons["prfx_image"])
        self.assertIn("takes a reference", reasons["prfx_regionid"])
        self.assertIn("needs a lookup column", reasons["prfx_value"])

    def test_table_errors_block_the_table(self):
        tables = {"prfx_parameter": spec("prfx_parameter", [{"prfx_code": "A"}])}
        server = server_of({"prfx_parameter": table_state(error="no alternate key")})
        out = dv.diff(model_of(**tables), server)
        self.assertEqual((out["actions"], out["blocked"][0]["reason"]), ([], "no alternate key"))

    def test_deactivate_rows_missing_from_the_file(self):
        server_rows = [{"prfx_parameterid": "g1", "prfx_code": "A", "statecode": 1}]
        filtered = [{"prfx_parameterid": "g1", "prfx_code": "A", "statecode": 1},
                    {"prfx_parameterid": "g2", "prfx_code": "OLD", "statecode": 0},
                    {"prfx_parameterid": "g3", "prfx_code": "GONE", "statecode": 1}]
        out = self.run_diff([{"prfx_code": "A"}], server_rows, deletion="deactivate", delete_filter="all",
                            filtered=filtered, state=ACTIVE)
        ops = [(a["op"], a["row"], a["body"]) for a in out["actions"]]
        self.assertEqual(ops, [("activate", 'prfx_code="A"', {"statecode": 0, "statuscode": 1}),
                               ("deactivate", 'prfx_code="OLD"', {"statecode": 1, "statuscode": 2})])

    def test_delete_rows_missing_from_the_file(self):
        cascade = [{"table": "prfx_child", "column": "prfx_parameterid", "behavior": "cascade"}]
        out = self.run_diff([], deletion="delete", delete_filter="all", cascade=cascade,
                            filtered=[{"prfx_parameterid": "g2", "prfx_code": "OLD"}])
        (a,) = out["actions"]
        self.assertEqual((a["op"], a["id"], a["cascade"]), ("delete", "g2", cascade))

    def test_row_outside_the_filter_warns(self):
        out = self.run_diff([{"prfx_code": "A", "prfx_value": "1"}],
                            [{"prfx_parameterid": "g1", "prfx_code": "A", "prfx_value": "1"}],
                            deletion="delete", delete_filter="prfx_origin eq 1", filtered=[])
        self.assertEqual(out["actions"], [])
        self.assertIn("will not delete it", out["warnings"][0]["message"])

    def test_bypass_is_reported_and_needs_the_privilege(self):
        steps = {"steps": [{"name": "Validate", "message": "Create", "mode": "sync", "stage": "pre-operation"}],
                 "workflows": [], "flows": []}
        out = self.run_diff([{"prfx_code": "A"}], bypass=["sync"], bypass_state=steps)
        self.assertEqual(out["bypass"][0]["steps"], steps["steps"])
        self.assertEqual(out["blocked"], [])
        denied = self.run_diff([{"prfx_code": "A"}], bypass=["sync"], bypass_state=steps, privilege=False)
        self.assertIn("prvBypassCustomBusinessLogic", denied["blocked"][0]["reason"])
        flows = self.run_diff([{"prfx_code": "A"}], bypass=["flows"], bypass_state=steps, privilege=False)
        self.assertEqual(flows["blocked"], [])  # flows need no privilege

    def test_no_bypass_section_without_changes(self):
        out = self.run_diff([], bypass=["sync"], bypass_state={"steps": [], "workflows": [], "flows": []})
        self.assertEqual(out["bypass"], [])

    def test_deletes_run_last_and_children_first(self):
        tables = {"prfx_region": spec("prfx_region", [], deletion="delete", delete_filter="all"),
                  "prfx_parameter": spec("prfx_parameter", [{"prfx_code": "A", "prfx_regionid": EU}],
                                         deletion="delete", delete_filter="all")}
        server = server_of({"prfx_region": table_state(filtered=[{"prfx_regionid": "r1", "prfx_code": "EU"}]),
                            "prfx_parameter": table_state(filtered=[{"prfx_parameterid": "p9", "prfx_code": "Z"}])},
                           {dv.ref_id(EU): {"id": "r1"}})
        out = dv.diff(model_of(**tables), server)
        self.assertEqual([(a["op"], a["table"]) for a in out["actions"]],
                         [("create", "prfx_parameter"), ("delete", "prfx_parameter"), ("delete", "prfx_region")])


class FakeApi:
    """Answers the reads of read_server from canned metadata and rows."""
    base = f"{DEV_URL}/api/data/v9.2"

    def __init__(self, entities, rows):
        self.entities, self.rows, self.paths = entities, rows, []

    def _rows(self, path):
        set_name, _, query = path.replace(self.base + "/", "").partition("?")
        query = urllib.parse.unquote(query)
        return [r for r in self.rows.get(set_name, []) if "$filter" not in query or f"'{r['prfx_code']}'" in query]

    def request(self, method, path, body=None, headers=None, return_id=False):
        self.paths.append(path)
        return {"value": self._rows(path)}

    def get(self, path):
        self.paths.append(path)
        if path.startswith("EntityDefinitions"):
            name = path.split("'")[1]
            return self.entities.get(name)
        return {}

    def get_or_none(self, path):
        if path.startswith("EntityDefinitions"):
            return self.get(path)
        self.paths.append(path)
        set_name, key = path.split("(", 1)
        code = key.split("'")[1]
        return next((r for r in self.rows.get(set_name, []) if r["prfx_code"] == code), None)

    def get_all(self, path):
        return []


def entity(logical, set_name, attrs, rels=()):
    return {"LogicalName": logical, "EntitySetName": set_name, "PrimaryIdAttribute": f"{logical}id",
            "Attributes": [{"LogicalName": n, "AttributeTypeName": {"Value": t}, "IsValidForCreate": True,
                            "IsValidForUpdate": True} for n, t in attrs.items()],
            "Keys": [{"LogicalName": f"{logical}_code", "KeyAttributes": ["prfx_code"], "EntityKeyIndexStatus": "Active"}],
            "ManyToOneRelationships": [{"ReferencingAttribute": a, "ReferencedEntity": e,
                                        "ReferencingEntityNavigationPropertyName": n} for a, e, n in rels]}


class TestReadServer(unittest.TestCase):
    def test_reads_rows_and_references_then_diffs(self):
        entities = {
            "prfx_region": entity("prfx_region", "prfx_regions", {"prfx_code": "StringType", "prfx_name": "StringType"}),
            "prfx_parameter": entity("prfx_parameter", "prfx_parameters",
                                     {"prfx_code": "StringType", "prfx_value": "StringType", "prfx_origin": "PicklistType",
                                      "prfx_regionid": "LookupType"},
                                     [("prfx_regionid", "prfx_region", "prfx_RegionId")]),
        }
        rows = {"prfx_regions": [{"prfx_regionid": "R-EU", "prfx_code": "EU", "prfx_name": "Europe"}],
                "prfx_parameters": [{"prfx_parameterid": "P1", "prfx_code": "MAX_RETRY", "prfx_value": "3",
                                     "prfx_origin": 100000000, "_prfx_regionid_value": "r-eu"}]}
        model = {"stage": "dev", "tables": dv.compose(SAMPLES, "dev")}
        model["tables"]["prfx_parameter"].update(deletion="none", bypass=[])
        api = FakeApi(entities, rows)
        server = dv.read_server(api, model, "U1")
        self.assertEqual(server["refs"][dv.ref_id(EU)], {"id": "R-EU"})
        out = dv.diff(model, server)
        self.assertEqual(out["blocked"], [])
        self.assertEqual([(a["op"], a["table"], a["row"]) for a in out["actions"]],
                         [("create", "prfx_region", 'prfx_code="NA"'),
                          ("create", "prfx_parameter", 'prfx_code="SUPPORT_EMAIL"')])
        self.assertTrue(any("prfx_regions(prfx_code='EU')" in p for p in api.paths))
        selects = [urllib.parse.parse_qs(urllib.parse.urlsplit(p).query).get("$select", [""])[0].split(",")
                   for p in api.paths if "/prfx_parameters?" in p]
        self.assertTrue(selects)
        for select in selects:
            self.assertNotIn("prfx_regionid", select)
        self.assertTrue(any("_prfx_regionid_value" in s for s in selects))

    def test_missing_table_and_inactive_key_block(self):
        entities = {"prfx_region": entity("prfx_region", "prfx_regions", {"prfx_code": "StringType"})}
        entities["prfx_region"]["Keys"][0]["EntityKeyIndexStatus"] = "Failed"
        model = {"stage": "dev", "tables": dv.compose(SAMPLES, "dev")}
        server = dv.read_server(FakeApi(entities, {}), model, "U1")
        self.assertIn("Failed, not Active", server["tables"]["prfx_region"]["error"])
        self.assertIn("not found in the environment", server["tables"]["prfx_parameter"]["error"])


class TestHelpers(unittest.TestCase):
    def test_key_segment_quotes_and_encodes(self):
        self.assertEqual(dv.key_segment({"prfx_code": "O'Neil & co"}, ["prfx_code"], REGION),
                         "prfx_code='O''Neil%20%26%20co'")

    def test_query_options_are_encoded(self):
        api = dv.Api({"url": DEV_URL, "tenant": None})
        url = dv._q(api, "prfx_regions", select="a,b", filter="prfx_code eq 'A&B'")
        self.assertEqual(url, f"{DEV_URL}/api/data/v9.2/prfx_regions?$select=a,b&$filter=prfx_code%20eq%20'A%26B'")

    def test_bypass_headers_combine(self):
        self.assertEqual(dv.bypass_headers(["sync", "async", "flows"]),
                         {"MSCRM.BypassBusinessLogicExecution": "CustomSync,CustomAsync",
                          "MSCRM.SuppressCallbackRegistrationExpanderJob": "true"})

    def test_datetimes_compare_in_utc(self):
        self.assertTrue(dv.same_scalar("2024-01-31T09:00:00+01:00", "2024-01-31T08:00:00Z", "DateTimeType"))
        self.assertFalse(dv.same_scalar("2024-01-31T09:00:00Z", "2024-01-31T08:00:00Z", "DateTimeType"))
        self.assertTrue(dv.same_scalar(None, "", "StringType"))


class TestApplier(unittest.TestCase):
    def test_creates_then_links_then_removals_with_bypass_headers(self):
        calls = []

        class Api:
            def request(self, method, path, body=None, headers=None, return_id=False):
                calls.append((method, path, body, headers))
                return "NEW" if return_id else {}

        plan = {"bypass": [{"table": "prfx_parameter", "modes": ["sync"]}], "actions": [
            {"op": "create", "table": "prfx_parameter", "set": "prfx_parameters", "row": "A",
             "body": {"prfx_code": "A"}, "links": {"prfx_RegionId": "/prfx_regions(prfx_code='EU')"}},
            {"op": "delete", "table": "prfx_region", "set": "prfx_regions", "row": "X", "id": "r9"},
        ]}
        applier = dv.Applier(Api(), plan)
        for op, a in applier.steps(plan["actions"]):
            getattr(applier, op)(a)
        bypass = {"MSCRM.BypassBusinessLogicExecution": "CustomSync"}
        self.assertEqual(calls, [
            ("POST", "prfx_parameters", {"prfx_code": "A"}, bypass),
            ("PATCH", "prfx_parameters(NEW)", {"prfx_RegionId@odata.bind": "/prfx_regions(prfx_code='EU')"},
             {**bypass, "If-Match": "*"}),
            ("DELETE", "prfx_regions(r9)", None, {}),
        ])


class BulkApi:
    """Fake Web API for apply: answers the bulk support queries, records every write.
    fail(method, path, body) returns an ApiError (or OSError) to raise, or None."""
    base = "https://x/api/data/v9.2"

    def __init__(self, messages=("CreateMultiple", "UpdateMultiple"), table_type="Standard", fail=None):
        self.messages, self.table_type, self.fail = messages, table_type, fail or (lambda *_: None)
        self.calls = []
        self.n = 0

    def get(self, path):
        if path.startswith("EntityDefinitions"):
            logical = path.split("'")[1]
            return {"PrimaryIdAttribute": f"{logical}id", "TableType": self.table_type}
        return {"value": [{"sdkmessageid": {"name": m}} for m in self.messages]}

    def request(self, method, path, body=None, headers=None, return_id=False):
        err = self.fail(method, path, body)
        self.calls.append((method, path.split("/Microsoft.Dynamics.CRM.")[-1] if "Multiple" in path else path,
                           [t.get("prfx_code") or t.get("prfx_parameterid") for t in body["Targets"]]
                           if body and "Targets" in body else body))
        if err:
            raise err
        if path.endswith("CreateMultiple"):
            ids = []
            for _t in body["Targets"]:
                self.n += 1
                ids.append(f"id{self.n}")
            return {"Ids": ids}
        if return_id:
            self.n += 1
            return f"id{self.n}"
        return {}


def creates(*codes, links=None):
    return [{"op": "create", "table": "prfx_parameter", "set": "prfx_parameters", "row": c,
             "body": {"prfx_code": c}, "links": dict(links or {})} for c in codes]


def bad_row(code, status=400):
    """Fail every request whose body holds the row `code`."""
    def fail(_method, _path, body):
        rows = [t.get("prfx_code") for t in body["Targets"]] if body and "Targets" in body else [(body or {}).get("prfx_code")]
        return dv.ApiError(status, "0x80040237", f"duplicate {code}", "x") if code in rows else None
    return fail


def apply_all(api, actions, batch_size=100):
    applier = dv.Applier(api, {"bypass": [], "actions": actions}, batch_size)
    done = []
    applier.run(applier.steps(actions), lambda op, a: done.append((op, a["row"])))
    return applier, done


class TestBulkApply(unittest.TestCase):
    def test_creates_go_in_one_create_multiple_then_links_in_one_update_multiple(self):
        api = BulkApi()
        acts = creates("A", "B", "C", links={"prfx_RegionId": "/prfx_regions(prfx_code='EU')"})
        applier, done = apply_all(api, acts)
        self.assertEqual(api.calls[0], ("POST", "CreateMultiple", ["A", "B", "C"]))
        method, msg, ids = api.calls[1]
        self.assertEqual((method, msg, ids), ("POST", "UpdateMultiple", ["id1", "id2", "id3"]))
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(applier.requests, 2)
        self.assertEqual([r for _, r in done], ["A", "B", "C", "A", "B", "C"])

    def test_targets_carry_the_odata_type_and_the_primary_key(self):
        api = BulkApi()
        applier = dv.Applier(api, {"bypass": [{"table": "prfx_parameter", "modes": ["sync"]}], "actions": []})
        seen = []
        api.request = lambda m, p, body=None, headers=None, return_id=False: seen.append((p, body, headers)) or {}
        acts = [{"op": "update", "table": "prfx_parameter", "set": "prfx_parameters", "row": r, "id": f"g{r}",
                 "body": {"prfx_value": r}, "links": {}, "unset": []} for r in ("A", "B")]
        applier.run(applier.steps(acts), lambda *_: None)
        path, body, headers = seen[0]
        self.assertEqual(path, "prfx_parameters/Microsoft.Dynamics.CRM.UpdateMultiple")
        self.assertEqual(body["Targets"][0], {"@odata.type": "Microsoft.Dynamics.CRM.prfx_parameter",
                                              "prfx_parameterid": "gA", "prfx_value": "A"})
        self.assertEqual(headers, {"MSCRM.BypassBusinessLogicExecution": "CustomSync"})

    def test_a_failed_batch_is_halved_until_the_failing_row_is_alone(self):
        api = BulkApi(fail=bad_row("C"))
        with self.assertRaises(dv.RowFailure) as ctx:
            apply_all(api, creates("A", "B", "C", "D"))
        self.assertEqual(ctx.exception.action["row"], "C")
        self.assertFalse(ctx.exception.unknown)
        self.assertEqual(api.calls, [
            ("POST", "CreateMultiple", ["A", "B", "C", "D"]),   # rolled back
            ("POST", "CreateMultiple", ["A", "B"]),             # written
            ("POST", "CreateMultiple", ["C", "D"]),             # rolled back
            ("POST", "prfx_parameters", {"prfx_code": "C"}),    # alone: the real error, then stop
        ])

    def test_rows_after_the_failing_one_are_not_sent(self):
        api = BulkApi(fail=bad_row("A"))
        with self.assertRaises(dv.RowFailure):
            apply_all(api, creates("A", "B", "C", "D"))
        self.assertNotIn(("POST", "prfx_parameters", {"prfx_code": "B"}), api.calls)

    def test_batches_follow_the_batch_size(self):
        api = BulkApi()
        apply_all(api, creates(*"ABCDE"), batch_size=2)
        self.assertEqual(api.calls, [("POST", "CreateMultiple", ["A", "B"]), ("POST", "CreateMultiple", ["C", "D"]),
                                     ("POST", "prfx_parameters", {"prfx_code": "E"})])

    def test_batch_size_one_is_row_by_row(self):
        api = BulkApi()
        apply_all(api, creates("A", "B"), batch_size=1)
        self.assertEqual([c[1] for c in api.calls], ["prfx_parameters", "prfx_parameters"])

    def test_unsupported_or_elastic_tables_go_row_by_row(self):
        for api in (BulkApi(messages=()), BulkApi(table_type="Elastic")):
            apply_all(api, creates("A", "B"))
            self.assertEqual([c[1] for c in api.calls], ["prfx_parameters", "prfx_parameters"])

    def test_a_message_that_never_works_in_bulk_is_dropped(self):
        api = BulkApi(fail=lambda m, p, b: dv.ApiError(400, None, "bulk refused", p) if "Multiple" in p else None)
        _applier, done = apply_all(api, creates(*"ABCDEF"), batch_size=2)
        self.assertEqual(len(done), 6)
        self.assertEqual(sum(1 for c in api.calls if c[1] == "CreateMultiple"), 1)

    def test_no_answer_stops_without_splitting(self):
        for err in (OSError("timed out"), dv.ApiError(504, None, "gateway timeout", "x"),
                    dv.ApiError(0, None, "retries exhausted", "x")):
            api = BulkApi(fail=lambda m, p, b, err=err: err if "Multiple" in p else None)
            with self.assertRaises(dv.UnknownOutcome) as ctx:
                apply_all(api, creates("A", "B", "C"))
            self.assertEqual(len(ctx.exception.actions), 3)
            self.assertEqual(len(api.calls), 1)

    def test_lookup_removals_and_deletes_stay_row_by_row(self):
        api = BulkApi()
        acts = [{"op": "update", "table": "prfx_parameter", "set": "prfx_parameters", "row": r, "id": f"g{r}",
                 "body": {"prfx_value": r} if r != "C" else {}, "links": {}, "unset": ["prfx_RegionId"]}
                for r in ("A", "B", "C")]
        acts += [{"op": "delete", "table": "prfx_region", "set": "prfx_regions", "row": r, "id": f"r{r}"}
                 for r in ("X", "Y")]
        _applier, done = apply_all(api, acts)
        self.assertEqual(api.calls, [
            ("DELETE", "prfx_parameters(gC)/prfx_RegionId/$ref", None),
            ("POST", "UpdateMultiple", ["gA", "gB"]),
            ("DELETE", "prfx_parameters(gA)/prfx_RegionId/$ref", None),
            ("DELETE", "prfx_parameters(gB)/prfx_RegionId/$ref", None),
            ("DELETE", "prfx_regions(rX)", None),
            ("DELETE", "prfx_regions(rY)", None),
        ])
        self.assertEqual(len(done), 5)

    def test_groups_keep_the_apply_order(self):
        steps = [("activate", {"table": "t1"}), ("create", {"table": "t1"}), ("update", {"table": "t1"}),
                 ("create", {"table": "t1"}), ("create", {"table": "t2"}), ("link", {"table": "t1"}),
                 ("delete", {"table": "t2"})]
        got = [(op, t, len(items)) for op, t, items in dv.Applier.groups(steps)]
        self.assertEqual(got, [("activate", "t1", 1), ("create", "t1", 2), ("update", "t1", 1),
                               ("create", "t2", 1), ("link", "t1", 1), ("delete", "t2", 1)])


class TestSharedBlock(unittest.TestCase):
    """The Web API client is copied from dv_metadata.py (plugins are installed
    independently, so no cross-plugin import). Detect drift. The guard is not shared:
    this tool targets any stage."""
    METADATA = SKILL.parents[2] / "dataverse-metadata-as-code" / "skills" / "dvac-metadata" / "scripts"
    SHARED = ("ApiError", "_c", "green", "yellow", "red", "grey", "bold", "Report",
              "find_repo_root", "read_dotenv", "_norm_url", "az_path", "az_json", "_debug", "Api",
              "DEPENDENCIES", "venv_dir", "venv_python", "ensure_deps", "token_account", "command_line",
              "print_status", "print_error")
    ALLOWED = {"dvac-data": "dvac-metadata", "DV_DATA_DEBUG": "DV_METADATA_DEBUG"}

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
        return subprocess.run([sys.executable, str(self.HOOK)], input=payload, capture_output=True, text=True)

    def test_ignores_other_files(self):
        res = self.run_hook("/somewhere/table.dv.metadata.yaml")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_blocks_invalid_model(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = write(tmp, "prfx_region.dv.data.yaml", "table: prfx_region\nkey: prfx_code\ndeletion: delete\n")
            res = self.run_hook(f)
            self.assertEqual(res.returncode, 2)
            self.assertIn("needs delete_filter", res.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_valid_sample_passes(self):
        res = self.run_hook(SAMPLES / "prod" / "prfx_parameter.dv.data.yaml")
        self.assertEqual((res.returncode, res.stderr), (0, ""))


class TestPlanFooter(unittest.TestCase):
    """The NEXT command never carries the values only the user may type."""

    def run_plan(self, stage, actions):
        import contextlib, io
        plan = {"stage": stage, "actions": actions, "blocked": [], "unmanaged": [], "env_url": "https://a.crm4.dynamics.com",
                "env_source": "flags", "double_confirmation": dv.double_confirmation(actions)}
        tmp = Path(tempfile.mkdtemp())
        out = io.StringIO()
        try:
            with mock.patch.object(dv, "compute_plan", return_value=(None, None, plan)), \
                    mock.patch.object(dv, "render_plan"), contextlib.redirect_stdout(out):
                code = dv.main(["plan", str(tmp), "--stage", stage, "--out", str(tmp / "p.json")])
        finally:
            shutil.rmtree(tmp)
        return code, out.getvalue()

    def test_outside_dev_the_confirmations_are_placeholders(self):
        code, out = self.run_plan("prod", [{"op": "delete", "table": "prfx_region", "row": "FR"}])
        self.assertEqual(code, 0)
        self.assertIn("DOUBLE CONFIRMATION:\n  - delete prfx_region[FR]", out)
        self.assertIn("--stage prod", out)
        self.assertIn("--confirm-url <URL TYPED BY THE USER> --confirm-deletes <COUNT TYPED BY THE USER>", out)

    def test_dev_needs_no_typed_confirmation(self):
        _, out = self.run_plan("dev", [{"op": "create", "table": "prfx_region", "row": "FR"}])
        self.assertIn("STATUS: changes-pending", out)
        self.assertNotIn("--confirm-url", out)
        self.assertIn("DOUBLE CONFIRMATION: none", out)


if __name__ == "__main__":
    unittest.main()
