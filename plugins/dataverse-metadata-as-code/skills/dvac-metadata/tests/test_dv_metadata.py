"""Offline tests for dv_metadata.py — validator, diff engine, dev guard, hook.

Run:  python3 -m unittest discover -s <skill>/tests -v
"""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import dv_metadata as dv  # noqa: E402

SAMPLES = SKILL / "references" / "samples"


def load_samples():
    meta, choices, tables = dv.load_model(SAMPLES)
    return {"meta": meta, "choices": choices, "tables": tables}


def mirror_server(model):
    """Server state identical to the model (what read_server would return after apply)."""
    choices = {}
    for name, spec in model["choices"].items():
        choices[name.lower()] = {
            "metadata_id": f"c-{name}", "label": spec["label"],
            "description": spec.get("description", ""),
            "options": {o["value"]: {"label": o["label"]} for o in spec["options"]},
        }
    tables = {}
    for t in model["tables"]:
        cols = {}
        primary = ""
        for cname, spec in t["columns"].items():
            want = dv.desired_column(spec)
            have = dict(want)
            have.setdefault("description", "")
            have.setdefault("auditing", True)
            have.setdefault("searchable", True)
            if want["type"] == "lookup":
                have["relationship"] = f"{t['key']}_{cname}"
            cols[cname.lower()] = have
            if spec.get("primary"):
                primary = cname.lower()
        tables[t["key"].lower()] = {
            "metadata_id": f"t-{t['key']}", "label": t["label"],
            "label_plural": t.get("label_plural"), "description": t.get("description", ""),
            "ownership": t.get("ownership", "user"), "table_type": t.get("table_type", "standard"),
            "auditing": t.get("auditing", False), "primary": primary,
            "columns": cols, "unmanaged_columns": [],
            "keys": {k["schema_name"].lower(): {"columns": sorted(c.lower() for c in k["columns"])}
                     for k in t.get("keys") or []},
        }
    components = {c["metadata_id"].lower() for c in choices.values()} | \
                 {t["metadata_id"].lower() for t in tables.values()}
    return {"components": components, "choices": choices, "tables": tables,
            "scope_choices": list(choices), "scope_tables": list(tables)}


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.model = self.tmp / "dataverse" / "metadata"
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
        self.assertEqual(dv.validate_model(SAMPLES).errors, [])

    def test_wrong_table_prefix(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "key: prfx_crm_Order", "key: prfx_Order")
        self.assertIn("must start with 'prfx_crm_'", self.errors())

    def test_column_with_project_code(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "  prfx_Amount:", "  prfx_crm_Amount:")
        self.assertIn("never carry the project code", self.errors())

    def test_missing_choice_reference(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml",
                  "choice: prfx_crm_choice_OrderStatus", "choice: prfx_crm_choice_Missing")
        self.assertIn("is not defined", self.errors())

    def test_two_primary_columns(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    precision: 2", "    precision: 2\n    primary: true")
        self.assertIn("exactly one column", self.errors())

    def test_key_must_match_file_name(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "key: prfx_crm_Order", "key: prfx_crm_Orders")
        self.assertIn("must equal the file name", self.errors())

    def warnings(self):
        return "\n".join(dv.validate_model(self.model).warnings)

    def test_lookup_requires_cascade(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict", "")
        self.assertIn("'cascade' is a required property", self.errors())

    def test_on_delete_is_rejected_with_migration(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict", "    on_delete: removelink")
        self.assertIn("use 'cascade: referential'", self.errors())

    def test_custom_cascade_mapping_is_valid(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict",
                  "    cascade:\n      delete: removelink\n      share: active\n      reparent: userowned")
        self.assertEqual(self.errors(), "")

    def test_invalid_cascade_values(self):
        for body in ("      delete: none", "      delete: restrict\n      assign: removelink",
                     "      share: all", "      delete: restrict\n      merge: all"):
            with self.subTest(body=body):
                self.tearDown()
                self.setUp()
                self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict",
                          "    cascade:\n" + body)
                self.assertIn("prfx_customerid", self.errors().lower())

    def test_parental_delete_warns(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict", "    cascade: parental")
        self.assertIn("cascade delete wipes child rows", self.warnings())

    def test_two_parental_relationships_are_rejected(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict",
                  "    cascade: parental\n  prfx_OwnerCustomerId:\n    label: Owner\n    type: lookup\n"
                  "    target: prfx_crm_Customer\n    description: Owner.\n"
                  "    cascade: {delete: restrict, share: active}")
        self.assertIn("only one parental relationship", self.errors())

    def test_money_precision_needs_fixed_source(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    type: decimal", "    type: money")
        self.assertIn("'precision' is ignored unless 'precision_source: fixed'", self.errors())
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    precision: 2",
                  "    precision: 5\n    precision_source: fixed")
        self.assertIn("money precision cannot exceed 4", self.errors())

    def test_boolean_labels_are_valid(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    default: false",
                  "    default: false\n    true_label: Urgent\n    false_label: Normal")
        self.assertEqual(self.errors(), "")

    def test_display_applies_to_datetime_only(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    behavior: userlocal",
                  "    behavior: userlocal\n    display: date")
        self.assertEqual(self.errors(), "")
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    type: datetime\n    behavior: userlocal",
                  "    type: date")
        self.assertIn("'display' does not apply to type 'date'", self.errors())

    def test_two_primary_images_are_rejected(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    max_size_kb: 10240",
                  "    max_size_kb: 10240\n  prfx_Photo:\n    label: Photo\n    type: image\n    primary_image: true\n"
                  "  prfx_Logo:\n    label: Logo\n    type: image\n    primary_image: true")
        self.assertIn("only one primary image", self.errors())

    def test_primary_image_false_is_rejected(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    max_size_kb: 10240",
                  "    max_size_kb: 10240\n  prfx_Photo:\n    label: Photo\n    type: image\n    primary_image: false")
        self.assertIn("prfx_photo", self.errors().lower())

    def test_active_on_org_owned_table_warns(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "ownership: user", "ownership: org")
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    cascade: restrict",
                  "    cascade: {delete: restrict, share: active}")
        self.assertIn("no effect on an org-owned table", self.warnings())

    def test_non_lookup_ending_in_id_warns(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "  prfx_Amount:", "  prfx_SourceId:")
        warnings = "\n".join(dv.validate_model(self.model).warnings)
        self.assertIn("non-lookup column ending in 'Id'", warnings)

    def test_attribute_not_applicable(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    precision: 2", "    precision: 2\n    max_length: 10")
        self.assertIn("'max_length' does not apply to type 'decimal'", self.errors())


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.model = load_samples()
        self.server = mirror_server(self.model)

    def test_empty_server_creates_everything(self):
        empty = {"components": set(), "choices": {}, "tables": {}, "scope_choices": [], "scope_tables": []}
        plan = dv.diff(self.model, empty)
        kinds = [a["kind"] for a in plan["actions"]]
        self.assertTrue(all(a["op"] == "create" for a in plan["actions"]))
        self.assertEqual(kinds, sorted(kinds, key=lambda k: dv.PHASE[k]))
        self.assertEqual(kinds.count("choice"), 3)
        self.assertEqual(kinds.count("table"), 2)
        self.assertIn("lookup", kinds)
        self.assertEqual(plan["blocked"], [])

    def test_identical_server_has_no_changes(self):
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan, {"actions": [], "blocked": [], "unmanaged": []})

    def test_label_change_is_an_update_with_before_after(self):
        self.server["tables"]["prfx_crm_order"]["columns"]["prfx_amount"]["label"] = "Total"
        plan = dv.diff(self.model, self.server)
        self.assertEqual(len(plan["actions"]), 1)
        a = plan["actions"][0]
        self.assertEqual((a["op"], a["kind"], a["name"]), ("update", "column", "prfx_Amount"))
        self.assertEqual(a["changes"], {"label": [{1033: "Total"}, {1033: "Amount"}]})

    def test_new_option_is_created(self):
        del self.server["choices"]["prfx_crm_choice_orderstatus"]["options"][100000003]
        plan = dv.diff(self.model, self.server)
        self.assertEqual([(a["op"], a["kind"], a["value"]) for a in plan["actions"]],
                         [("create", "option", 100000003)])

    def test_type_change_is_blocked(self):
        self.server["tables"]["prfx_crm_order"]["columns"]["prfx_amount"]["type"] = "integer"
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["blocked"][0]["field"], "type")

    def test_ownership_change_is_blocked(self):
        self.server["tables"]["prfx_crm_order"]["ownership"] = "org"
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan["blocked"][0]["field"], "ownership")

    def test_server_only_components_are_unmanaged(self):
        self.server["tables"]["prfx_crm_order"]["unmanaged_columns"].append({"name": "prfx_legacy", "type": "StringType"})
        self.server["choices"]["prfx_crm_choice_orderstatus"]["options"][100000099] = {"label": "Old"}
        self.server["scope_tables"].append("prfx_crm_legacytable")
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan["actions"], [])
        self.assertEqual(sorted(u["kind"] for u in plan["unmanaged"]), ["column", "option", "table"])

    def lookup_spec(self):
        order = next(t for t in self.model["tables"] if t["key"] == "prfx_crm_Order")
        return order["columns"]["prfx_CustomerId"]

    def test_cascade_change_updates_relationship(self):
        self.server["tables"]["prfx_crm_order"]["columns"]["prfx_customerid"]["cascade"] = \
            dv.expand_cascade("referential")
        plan = dv.diff(self.model, self.server)
        a = plan["actions"][0]
        self.assertEqual((a["kind"], a["changes"]), ("lookup", {"cascade.delete": ["removelink", "restrict"]}))

    def test_mapping_equal_to_preset_has_no_changes(self):
        self.lookup_spec()["cascade"] = {"delete": "restrict", "assign": "none"}
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan["actions"], [])

    def test_custom_cascade_changes_only_differing_actions(self):
        self.lookup_spec()["cascade"] = {"delete": "restrict", "share": "active", "unshare": "active"}
        plan = dv.diff(self.model, self.server)
        self.assertEqual([(a["kind"], a["changes"]) for a in plan["actions"]],
                         [("lookup", {"cascade.share": ["none", "active"], "cascade.unshare": ["none", "active"]})])

    def test_create_lookup_shows_cascade_label(self):
        self.lookup_spec()["cascade"] = {"delete": "removelink", "reparent": "userowned"}
        del self.server["tables"]["prfx_crm_order"]["columns"]["prfx_customerid"]
        plan = dv.diff(self.model, self.server)
        self.assertIn("(cascade {delete: removelink, reparent: userowned})", plan["actions"][0]["detail"])

    def test_missing_solution_membership_is_an_update(self):
        self.server["components"].discard("t-prfx_crm_order")
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan["actions"][0]["changes"], {"solution": ["absent", "present"]})

    def test_datetime_behavior_only_from_userlocal(self):
        col = self.server["tables"]["prfx_crm_order"]["columns"]["prfx_orderedon"]
        col["behavior"] = "dateonly"
        plan = dv.diff(self.model, self.server)
        self.assertEqual(plan["blocked"][0]["field"], "behavior")


class TestColumnOptions(unittest.TestCase):
    def setUp(self):
        self.model = load_samples()
        self.server = mirror_server(self.model)
        order = next(t for t in self.model["tables"] if t["key"] == "prfx_crm_Order")
        self.cols = order["columns"]

    def changes(self):
        return [(a["name"], a["changes"]) for a in dv.diff(self.model, self.server)["actions"]]

    def test_boolean_labels_diff(self):
        self.cols["prfx_HasPriority"]["true_label"] = "Urgent"
        self.server["tables"]["prfx_crm_order"]["columns"]["prfx_haspriority"]["true_label"] = "Yes"
        self.assertEqual(self.changes(), [("prfx_HasPriority", {"true_label": [{1033: "Yes"}, {1033: "Urgent"}]})])

    def test_display_diff(self):
        self.cols["prfx_OrderedOn"]["display"] = "date"
        self.server["tables"]["prfx_crm_order"]["columns"]["prfx_orderedon"]["display"] = "datetime"
        self.assertEqual(self.changes(), [("prfx_OrderedOn", {"display": ["datetime", "date"]})])

    def test_primary_image_only_set_when_declared(self):
        self.cols["prfx_Photo"] = {"label": "Photo", "type": "image", "primary_image": True}
        srv = self.server["tables"]["prfx_crm_order"]["columns"]
        srv["prfx_photo"] = {**dv.desired_column(self.cols["prfx_Photo"]), "description": "",
                             "auditing": True, "searchable": True, "primary_image": False}
        self.assertEqual(self.changes(), [("prfx_Photo", {"primary_image": [False, True]})])
        del self.cols["prfx_Photo"]["primary_image"]
        self.assertEqual(self.changes(), [])

    def test_payload(self):
        applier = dv.Applier(None, self.model)
        money = applier.attr_payload("prfx_Price", {"label": "Price", "type": "money",
                                                    "precision_source": "fixed", "precision": 2})
        self.assertEqual((money["PrecisionSource"], money["Precision"]), (0, 2))
        self.assertEqual(applier.attr_payload("prfx_Cost", {"label": "Cost", "type": "money"})["PrecisionSource"], 2)
        flag = applier.attr_payload("prfx_IsUrgent", {"label": "Urgent", "type": "boolean", "true_label": "Urgent"})
        self.assertEqual(flag["OptionSet"]["TrueOption"]["Label"]["LocalizedLabels"][0]["Label"], "Urgent")
        when = applier.attr_payload("prfx_DueOn", {"label": "Due", "type": "datetime",
                                                   "behavior": "timezoneindependent", "display": "date"})
        self.assertEqual((when["Format"], when["DateTimeBehavior"]["Value"]), ("DateOnly", "TimeZoneIndependent"))
        img = applier.attr_payload("prfx_Photo", {"label": "Photo", "type": "image", "primary_image": True})
        self.assertTrue(img["IsPrimaryImage"])

    def test_boolean_label_update_uses_update_option_value(self):
        calls = []

        class FakeApi:
            def get(self, path):
                return {"MetadataId": "m1"}

            def request(self, method, path, body, headers, **kw):
                calls.append((method, path, body))

        applier = dv.Applier(FakeApi(), self.model)
        applier.update_column({"table": "prfx_crm_Order", "name": "prfx_HasPriority", "type": "boolean",
                               "changes": {"false_label": ["No", "Normal"]}})
        self.assertEqual([(m, p) for m, p, _ in calls], [("POST", "UpdateOptionValue")])
        self.assertEqual(calls[0][2]["Value"], 0)


class TestLocalization(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.model_dir = self.tmp / "dataverse" / "metadata"
        shutil.copytree(SAMPLES, self.model_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit(self, rel, old, new):
        p = self.model_dir / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def report(self):
        return dv.validate_model(self.model_dir)

    def add_french(self):
        self.edit("_meta.dv.metadata.yaml", "locale: en", "locale: en\nlanguages: [1033, 1036]")

    def test_label_map_is_valid(self):
        self.add_french()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Order\n", "label: {1033: Order, 1036: Commande}\n")
        self.assertEqual(self.report().errors, [])

    def test_missing_translation_is_a_warning(self):
        self.add_french()
        report = self.report()
        self.assertEqual(report.errors, [])
        self.assertTrue(any("missing translations" in w and "label [1036]" in w for w in report.warnings))

    def test_no_translation_warning_without_languages(self):
        self.assertFalse(any("missing translations" in w for w in self.report().warnings))

    def test_map_without_base_language_is_an_error(self):
        self.add_french()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Order\n", "label: {1036: Commande}\n")
        self.assertIn("must include the base language 1033", "\n".join(self.report().errors))

    def test_undeclared_language_warns(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Order\n", "label: {1033: Order, 1031: Auftrag}\n")
        self.assertTrue(any("language 1031" in w for w in self.report().warnings))

    def test_invalid_language_key_is_an_error(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Order\n", "label: {en: Order}\n")
        self.assertTrue(self.report().errors)

    def test_choice_suffix_checks_base_language_only(self):
        self.add_french()
        self.edit("choices.dv.metadata.yaml", "label: Order Status Choice",
                  "label: {1033: Order Status Choice, 1036: Statut de commande}")
        self.assertFalse(any("should end with ' Choice'" in w for w in self.report().warnings))

    def test_load_model_normalises_labels(self):
        self.add_french()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Order\n", "label: {1033: Order, '1036': Commande}\n")
        _meta, _choices, tables = dv.load_model(self.model_dir)
        order = next(t for t in tables if t["key"] == "prfx_crm_Order")
        self.assertEqual(order["label"], {1033: "Order", 1036: "Commande"})
        self.assertEqual(order["columns"]["prfx_Amount"]["label"], {1033: "Amount"})

    def test_diff_compares_declared_languages_only(self):
        self.add_french()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Order\n", "label: {1033: Order, 1036: Commande}\n")
        meta, choices, tables = dv.load_model(self.model_dir)
        model = {"meta": meta, "choices": choices, "tables": tables}
        server = mirror_server(model)
        server["tables"]["prfx_crm_order"]["label"] = {1033: "Order", 1036: "Commande", 1031: "Auftrag"}
        self.assertEqual(dv.diff(model, server)["actions"], [])
        server["tables"]["prfx_crm_order"]["label"] = {1033: "Order", 1036: "Ordre"}
        a = dv.diff(model, server)["actions"][0]
        self.assertEqual(a["changes"], {"label": [{1036: "Ordre"}, {1036: "Commande"}]})
        # A saved plan (JSON, string keys) still matches the fresh one.
        saved = json.loads(json.dumps({"env_url": "u", "solution": "s", "actions": [a], "blocked": []}))
        self.assertEqual(dv._canonical(saved), dv._canonical({"env_url": "u", "solution": "s",
                                                               "actions": [a], "blocked": []}))

    def test_payloads_write_every_language(self):
        self.add_french()
        meta, choices, tables = dv.load_model(self.model_dir)
        applier = dv.Applier(None, {"meta": meta, "choices": choices, "tables": tables})
        label = dv.make_label({1033: "Order", 1036: "Commande"}, 1033)
        self.assertEqual([(l["LanguageCode"], l["Label"]) for l in label["LocalizedLabels"]],
                         [(1033, "Order"), (1036, "Commande")])
        flag = applier.attr_payload("prfx_IsUrgent", {"label": {1033: "Urgent"}, "type": "boolean",
                                                      "true_label": {1033: "Urgent", 1036: "Urgent"}})
        no = {l["LanguageCode"]: l["Label"] for l in flag["OptionSet"]["FalseOption"]["Label"]["LocalizedLabels"]}
        self.assertEqual(no, {1033: "No", 1036: "Non"})

    def test_unprovisioned_language_is_blocked(self):
        self.add_french()
        meta, choices, tables = dv.load_model(self.model_dir)
        blocks = dv.language_blocks({"meta": meta, "choices": choices, "tables": tables}, {1033})
        self.assertEqual([(b["kind"], b["name"]) for b in blocks], [("language", "1036")])


class TestCascade(unittest.TestCase):
    def test_api_round_trip_and_labels(self):
        for preset in dv.CASCADE_PRESETS:
            conf = dv.cascade_to_api(dv.expand_cascade(preset))
            self.assertEqual(conf["Merge"], "NoCascade")
            self.assertEqual(dv.cascade_label(dv.cascade_from_api(conf)), preset)

    def test_parental_detection(self):
        self.assertTrue(dv.is_parental(dv.expand_cascade("parental")))
        self.assertTrue(dv.is_parental(dv.expand_cascade({"delete": "restrict", "share": "userowned"})))
        self.assertFalse(dv.is_parental(dv.expand_cascade("referential")))

    def test_unknown_api_value_is_kept(self):
        casc = dv.cascade_from_api({"Delete": "Restrict", "Assign": "Weird"})
        self.assertEqual(casc["assign"], "weird")


class TestDevGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")

    def test_missing_env_dev(self):
        with self.assertRaises(dv.GuardError):
            dv.dev_environment(self.root)

    def test_stage_must_be_dev(self):
        self.write(".env.dev", "ENVIRONMENT_STAGE=uat\nDATAVERSE_URL=https://a.crm4.dynamics.com\n")
        with self.assertRaisesRegex(dv.GuardError, "ENVIRONMENT_STAGE=dev"):
            dv.dev_environment(self.root)

    def test_same_url_as_prod(self):
        self.write(".env.dev", "ENVIRONMENT_STAGE=dev\nDATAVERSE_URL=https://a.crm4.dynamics.com/\n")
        self.write(".env.prod", "ENVIRONMENT_STAGE=prod\nDATAVERSE_URL=https://A.crm4.dynamics.com\n")
        with self.assertRaisesRegex(dv.GuardError, ".env.prod"):
            dv.dev_environment(self.root)

    def test_valid_dev(self):
        self.write(".env.dev", "# dev\nENVIRONMENT_STAGE=dev\nDATAVERSE_URL=\"https://a-dev.crm4.dynamics.com\"\nTENANT_ID=t1\n")
        self.write(".env.prod", "ENVIRONMENT_STAGE=prod\nDATAVERSE_URL=https://a.crm4.dynamics.com\n")
        env = dv.dev_environment(self.root)
        self.assertEqual((env["url"], env["tenant"]), ("https://a-dev.crm4.dynamics.com", "t1"))

    def test_managed_solution_is_refused(self):
        class FakeApi:
            url = "https://a-dev.crm4.dynamics.com"

            def get_all(self, path):
                return [{"solutionid": "s1", "uniquename": "x", "ismanaged": True}]
        with self.assertRaisesRegex(dv.GuardError, "MANAGED"):
            dv.check_solution(FakeApi(), "x")


class TestApiPerformance(unittest.TestCase):
    """Regression: a 404 must never be retried (it used to stall plan ~150 s per new table)."""

    def setUp(self):
        self.api = dv.Api({"url": "https://a-dev.crm4.dynamics.com", "tenant": None})
        self.api._token, self.api._expires = "t", float("inf")
        self.calls, self.sleeps = [], []
        self._urlopen, self._sleep = dv.urllib.request.urlopen, dv.time.sleep
        dv.time.sleep = self.sleeps.append

    def tearDown(self):
        dv.urllib.request.urlopen, dv.time.sleep = self._urlopen, self._sleep

    def fail_with(self, status, code):
        import io
        import urllib.error

        def fake(req, timeout=None):
            self.calls.append(req.full_url)
            body = json.dumps({"error": {"code": code, "message": "x"}}).encode()
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))
        dv.urllib.request.urlopen = fake

    def test_not_found_is_not_retried(self):
        self.fail_with(404, "0x80060888")
        self.assertIsNone(self.api.get_or_none("EntityDefinitions(LogicalName='prfx_x')"))
        self.assertEqual((len(self.calls), self.sleeps), (1, []))

    def test_lock_contention_is_retried(self):
        self.fail_with(400, "0x80072560")
        with self.assertRaises(dv.ApiError):
            self.api.get("EntityDefinitions")
        self.assertEqual(len(self.calls), 6)

    def test_solution_and_consistency_headers(self):
        sent = []

        class Resp:
            status, headers = 204, {"OData-EntityId": "https://x/api/data/v9.2/EntityDefinitions(abc)"}

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            sent.append({k.lower(): v for k, v in req.header_items()})
            return Resp()
        dv.urllib.request.urlopen = fake
        applier = dv.Applier(self.api, load_samples())
        self.assertEqual(applier.post("EntityDefinitions", {}, return_id=True), "abc")
        self.assertEqual(sent[-1].get("mscrm.solutionuniquename"), "prfx_crm_Core")
        self.assertNotIn("mscrm.solutionname", sent[-1])
        self.assertNotIn("consistency", sent[-1])
        self.api.strong = True
        self.api.get("EntityDefinitions")
        self.assertEqual(sent[-1].get("consistency"), "Strong")

    def test_missing_custom_table_costs_no_request(self):
        model = load_samples()
        requested = []

        class FakeApi:
            url = "https://a-dev.crm4.dynamics.com"

            def get_all(self, path):
                requested.append(path)
                if path.startswith("solutions"):
                    return [{"solutionid": "s1", "ismanaged": False}]
                return []

            def get_or_none(self, path):
                requested.append(path)
                return None

        state = dv.read_server(FakeApi(), model["meta"], model["choices"], model["tables"])
        self.assertEqual(state["tables"], {})
        self.assertEqual(len(requested), 4)  # solution, components, choices list, tables list


class TestHook(unittest.TestCase):
    HOOK = SKILL / "scripts" / "hook_validate.py"

    def run_hook(self, path):
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})
        return subprocess.run([sys.executable, str(self.HOOK)], input=payload,
                              capture_output=True, text=True)

    def test_ignores_other_files(self):
        res = self.run_hook("/somewhere/app.tsx")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_blocks_invalid_model(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            model = tmp / "metadata"
            shutil.copytree(SAMPLES, model)
            f = model / "tables" / "prfx_crm_Order.dv.metadata.yaml"
            f.write_text(f.read_text(encoding="utf-8").replace("key: prfx_crm_Order", "key: wrong_Order"),
                         encoding="utf-8")
            res = self.run_hook(f)
            self.assertEqual(res.returncode, 2)
            self.assertIn("must equal the file name", res.stderr)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
