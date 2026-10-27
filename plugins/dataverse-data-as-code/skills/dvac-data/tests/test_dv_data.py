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


class TestSharedBlock(unittest.TestCase):
    """The Web API client is copied from dv_metadata.py (plugins are installed
    independently, so no cross-plugin import). Detect drift. The guard is not shared:
    this tool targets any stage."""
    METADATA = SKILL.parents[2] / "dataverse-metadata-as-code" / "skills" / "dvac-metadata" / "scripts"
    SHARED = ("ApiError", "_c", "green", "yellow", "red", "grey", "bold", "Report",
              "find_repo_root", "read_dotenv", "_norm_url", "az_path", "az_json", "_debug", "Api")
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


if __name__ == "__main__":
    unittest.main()
