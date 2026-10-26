"""Offline tests for dv_plugin.py — validator, diff engine, solution membership,
apply executors, id recording, pac command, export, dev guard, hook.

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
import zipfile
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import dv_plugin as dv  # noqa: E402

SAMPLES = SKILL / "references" / "samples"
PACKAGE_SAMPLE = "Contoso.Crm.Plugins.dv.plugin.yaml"
ASSEMBLY_SAMPLE = "Legacy.Plugins.dv.plugin.yaml"
SHA = "a" * 64
CTYPES = {"package": 10090, "assembly": 91, "step": 92,
          "customapi": 10027, "request": 10028, "response": 10029}


def load_units(*names):
    units = []
    for name in names or (PACKAGE_SAMPLE, ASSEMBLY_SAMPLE):
        path = SAMPLES / name
        unit = dv.build_unit(path, dv.load_yaml(path))
        unit["artifact"] = {"path": f"/build/{unit['name']}.nupkg", "sha256": SHA, "version": "1.0.0",
                            "nuspec_id": "Contoso.Crm.Plugins"}
        units.append(unit)
    return units


def mirror_server(units):
    """Server state identical to the model, everything in the solution. Records the
    package/assembly id in the units, as apply would have."""
    counter = [0]

    def gid():
        counter[0] += 1
        return f"00000000-0000-0000-0000-{counter[0]:012d}"

    members, out = set(), {}
    for u in units:
        uid = gid()
        u["id"] = uid
        asm = {"id": gid() if u["kind"] == "package" else uid, "name": u["name"]}
        types = {}
        for s in u["steps"]:
            types.setdefault(s["plugin"], gid())
        for a in u["custom_apis"].values():
            if a.get("plugin"):
                types.setdefault(a["plugin"], gid())
        steps = {}
        for s in u["steps"]:
            have = {k: v for k, v in s.items() if k != "images"}
            have.update({"id": gid(), "configuration": s.get("configuration", ""),
                         "description": s.get("description", ""),
                         "async_auto_delete": s.get("async_auto_delete", False)})
            have["images"] = {k: {**i, "id": gid()} for k, i in s["images"].items()}
            steps[s["name"].lower()] = have
            members.add(have["id"].lower())
        apis = {}
        for key, a in u["custom_apis"].items():
            h = {k: v for k, v in a.items() if k not in ("request", "response")}
            h.update({"id": gid(), "name": a.get("name", a["uniquename"]),
                      "description": a.get("description", a["display_name"]),
                      "execute_privilege": a.get("execute_privilege"), "plugin": a.get("plugin")})
            for section in ("request", "response"):
                h[section] = {}
                for pk, p in a[section].items():
                    hp = {**p, "id": gid(), "name": p.get("name", f"{a['uniquename']}.{p['uniquename']}"),
                          "display_name": p.get("display_name", p["uniquename"])}
                    hp["description"] = p.get("description", hp["display_name"])
                    h[section][pk] = hp
                    members.add(hp["id"].lower())
            apis[key] = h
            members.add(h["id"].lower())
        members |= {uid.lower(), asm["id"].lower()}
        out[u["name"].lower()] = {"id": uid, "version": "1.0.0", "sha256": u["artifact"]["sha256"],
                                  "assemblies": [asm], "types": types, "steps": steps,
                                  "custom_apis": apis, "scope_apis": []}
    return {"component_types": dict(CTYPES),
            "solutions": {"prfx_crm_Core": {"id": "s1", "members": members}}, "units": out}


def empty_server(units):
    return {"component_types": dict(CTYPES), "solutions": {"prfx_crm_Core": {"id": "s1", "members": set()}},
            "units": {u["name"].lower(): {"id": None, "version": None, "sha256": None, "assemblies": [],
                                          "types": {}, "steps": {}, "custom_apis": {}, "scope_apis": []}
                      for u in units}}


def ops(plan):
    return [(a["op"], a["kind"], a["name"]) for a in plan["actions"]]


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.model = self.tmp / "plugins"
        shutil.copytree(SAMPLES, self.model)
        self.pkg = self.model / PACKAGE_SAMPLE

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit(self, old, new, path=None):
        p = path or self.pkg
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def errors(self):
        return "\n".join(dv.validate_path(self.model).errors)

    def warnings(self):
        return "\n".join(dv.validate_path(self.model).warnings)

    def test_samples_are_valid(self):
        self.assertEqual(dv.validate_path(SAMPLES).errors, [])

    def test_assembly_is_not_discouraged(self):
        self.assertNotIn("prefer", self.warnings())

    def test_package_needs_prefix(self):
        self.edit("name: prfx_Contoso.Crm.Plugins", "name: Contoso.Crm.Plugins")
        self.assertIn("must start with the publisher prefix 'prfx_'", self.errors())

    def test_exactly_one_of_package_or_assembly(self):
        self.edit("steps:\n", "assembly:\n  name: X\n  dll: x.dll\n\nsteps:\n")
        self.assertIn("exactly one of 'package' or 'assembly'", self.errors())

    def test_async_only_in_post_operation(self):
        self.edit("    stage: post-operation\n    mode: async", "    stage: pre-operation\n    mode: async")
        self.assertIn("async steps run only in post-operation", self.errors())

    def test_update_without_filtering_attributes_is_allowed(self):
        self.edit("    filtering_attributes: [prfx_statuscode]    # only these columns trigger the step\n", "")
        self.assertEqual(self.errors(), "")

    def test_image_rules(self):
        self.edit("      - name: PreImage\n        type: pre\n        attributes: [prfx_ordernumber",
                  "      - name: PreImage\n        type: post\n        attributes: [prfx_ordernumber")
        errors = self.errors()
        self.assertIn("Delete has no post-image", errors)
        self.assertIn("post-images exist only in post-operation", errors)

    def test_image_may_capture_every_column(self):
        self.edit("        attributes: [prfx_statuscode, prfx_customerid]\n", "")
        self.assertEqual(self.errors(), "")
        self.edit("        type: pre\n", "        type: pre\n        attributes: []\n")
        self.assertEqual(self.errors(), "")

    def test_duplicate_step_name(self):
        self.edit("steps:\n", "steps:\n  - plugin: Contoso.Crm.Plugins.OrderPreCreate\n    message: Create\n"
                              "    table: prfx_crm_order\n    stage: post-operation\n")
        self.assertIn("duplicate step name", self.errors())

    def test_explicit_name_solves_duplicates(self):
        self.edit("steps:\n", "steps:\n  - name: Order post create\n    plugin: Contoso.Crm.Plugins.OrderPreCreate\n"
                              "    message: Create\n    table: prfx_crm_order\n    stage: post-operation\n")
        self.assertEqual(self.errors(), "")

    def test_async_auto_delete_needs_async(self):
        self.edit("    description: Sets the order number", "    async_auto_delete: true\n    description: Sets the order number")
        self.assertIn("async_auto_delete applies only to async steps", self.errors())

    def test_custom_api_rules(self):
        self.edit("    bound_table: prfx_crm_order      # immutable once created\n", "")
        self.assertIn("binding entity requires bound_table", self.errors())

    def test_custom_api_prefix(self):
        self.edit("  prfx_crm_RecalculateOrder:", "  crm_RecalculateOrder:")
        self.assertIn("Custom API unique name must start with 'prfx_'", self.errors())

    def test_function_needs_a_response(self):
        self.edit("    is_function: false               # immutable once created\n", "    is_function: true\n")
        self.assertEqual(self.errors(), "")
        self.edit("    response:\n      Total:\n        type: money\n        display_name: Total\n", "")
        self.assertIn("a function must have at least one response property", self.errors())

    def test_param_table_only_for_entity_types(self):
        self.edit("        type: string\n", "        type: string\n        table: account\n")
        self.assertIn("'table' applies only to", self.errors())

    def test_steps_on_custom_api_respect_allowed_steps(self):
        self.edit("steps:\n", "steps:\n  - plugin: Contoso.Crm.Plugins.OrderPreCreate\n"
                              "    message: prfx_crm_RecalculateOrder\n    stage: post-operation\n")
        self.assertIn("allows async steps only", self.errors())
        self.edit("allowed_steps: async-only", "allowed_steps: none")
        self.assertIn("allows no custom processing steps", self.errors())

    def test_duplicate_unit_across_files(self):
        shutil.copy(self.pkg, self.model / "Copy.dv.plugin.yaml")
        self.assertIn("is already described by", self.errors())

    def test_source_scan(self):
        (self.model / "OrderPlugins.cs").write_text(
            "namespace Contoso.Crm.Plugins\n{\n"
            "    public abstract class OrderBase : PluginBase { }\n"
            "    public class OrderPreCreate : OrderBase { }\n"          # plug-in through a base class
            "    public class OrderStatusChanged : IPlugin { }\n"
            "    public class OrderPreDelete : PluginBase { }\n"
            "    public class RecalculateOrderApi { }\n"                 # not a plug-in
            "}\n", encoding="utf-8")
        (self.model / "Legacy.cs").write_text(
            "namespace Legacy.Plugins;\npublic sealed class AccountAddressSync : PluginBase { }\n", encoding="utf-8")
        self.assertEqual(self.errors(), "")
        self.assertIn("'Contoso.Crm.Plugins.RecalculateOrderApi' does not implement IPlugin", self.warnings())
        self.edit("plugin: Contoso.Crm.Plugins.OrderPreDelete", "plugin: Contoso.Crm.Plugins.OrderPreDeleteX")
        self.assertIn("is not a class of this project", self.errors())

    def test_sources_under_bin_are_ignored(self):
        (self.model / "bin").mkdir()
        (self.model / "bin" / "Generated.cs").write_text("namespace X { class Y : PluginBase { } }", encoding="utf-8")
        self.assertEqual(self.errors(), "")

    def test_nupkg_id_mismatch_warns(self):
        make_nupkg(self.model / "bin" / "Release" / "Other.1.0.0.nupkg", "Other", "1.0.0")
        self.assertIn("differs from 'prfx_Other'", self.warnings())


def make_nupkg(path, pkg_id, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{pkg_id}.nuspec",
                   '<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
                   f"<metadata><id>{pkg_id}</id><version>{version}</version></metadata></package>")
        z.writestr(f"lib/net48/{pkg_id}.dll", b"MZ" + version.encode())


class TestArtifact(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / PACKAGE_SAMPLE
        shutil.copy(SAMPLES / PACKAGE_SAMPLE, self.file)
        self.data = dv.load_yaml(self.file)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_missing_build_output(self):
        self.assertIsNone(dv.locate_artifact(self.file, self.data))

    def test_highest_version_wins(self):
        rel = self.tmp / "bin" / "Release"
        make_nupkg(rel / "Contoso.Crm.Plugins.1.0.10.nupkg", "Contoso.Crm.Plugins", "1.0.10")
        make_nupkg(rel / "Contoso.Crm.Plugins.1.0.9.nupkg", "Contoso.Crm.Plugins", "1.0.9")
        art = dv.locate_artifact(self.file, self.data)
        self.assertEqual((art["version"], art["nuspec_id"]), ("1.0.10", "Contoso.Crm.Plugins"))
        self.assertEqual(len(art["sha256"]), 64)


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.units = load_units()
        self.server = mirror_server(self.units)
        self.pkg = self.server["units"]["prfx_contoso.crm.plugins"]
        self.asm = self.server["units"]["legacy.plugins"]

    def plan(self):
        return dv.diff(self.units, self.server)

    def step(self, unit, plugin):
        return next(s for s in unit["steps"].values() if s["plugin"] == plugin)

    def test_identical_server_has_no_changes(self):
        self.assertEqual(self.plan(), {"actions": [], "blocked": [], "unmanaged": [], "notes": []})

    def test_empty_server_creates_everything_in_order(self):
        for u in self.units:
            u["id"] = None
        plan = dv.diff(self.units, empty_server(self.units))
        self.assertEqual(plan["blocked"], [])
        self.assertTrue(all(a["op"] == "create" for a in plan["actions"]))
        kinds = [a["kind"] for a in plan["actions"]]
        self.assertEqual(kinds[:2], ["package", "assembly"])
        self.assertEqual(kinds.count("plugintype"), 1)  # assembly only — a package brings its own types
        self.assertLess(kinds.index("customapi"), kinds.index("step"))
        self.assertLess(kinds.index("step"), kinds.index("image"))
        # parameters of a new Custom API are deep-inserted with it, not planned separately
        self.assertNotIn("request", kinds)
        self.assertNotIn("response", kinds)
        confirm = dv.double_confirm(plan["actions"])
        self.assertIn("create package prfx_Contoso.Crm.Plugins", confirm)
        self.assertIn("create customapi prfx_crm_RecalculateOrder", confirm)

    def test_yaml_without_id_records_it(self):
        self.units[0]["id"] = None
        self.assertEqual(ops(self.plan()), [("update", "package", "prfx_Contoso.Crm.Plugins")])
        self.assertEqual(self.plan()["actions"][0]["changes"], {"id": [None, self.pkg["id"]]})

    def test_id_pointing_elsewhere_is_blocked(self):
        self.units[0]["id"] = "11111111-1111-1111-1111-111111111111"
        self.assertEqual(self.plan()["blocked"][0]["field"], "id")

    def test_id_without_server_package_is_blocked(self):
        self.pkg.update({"id": None, "assemblies": [], "types": {}, "steps": {}, "custom_apis": {}})
        plan = self.plan()
        self.assertEqual(plan["blocked"][0]["field"], "id")
        self.assertFalse([a for a in plan["actions"] if a["unit"] == "prfx_Contoso.Crm.Plugins"])

    def test_new_content_is_pushed(self):
        self.pkg["sha256"] = "b" * 64
        plan = self.plan()
        self.assertEqual(ops(plan), [("push", "package", "prfx_Contoso.Crm.Plugins")])
        self.assertEqual(plan["actions"][0]["changes"]["content"], ["sha256:bbbbbbbbbbbb", "sha256:aaaaaaaaaaaa"])

    def test_unreadable_content_falls_back_to_version(self):
        self.pkg["sha256"] = None
        plan = self.plan()
        self.assertEqual(plan["actions"], [])
        self.assertIn("compared the package version", plan["notes"][0])
        self.pkg["version"] = "0.9.0"
        self.assertEqual(ops(self.plan()), [("push", "package", "prfx_Contoso.Crm.Plugins")])

    def test_step_update_lists_before_after(self):
        self.step(self.asm, "Legacy.Plugins.AccountAddressSync")["rank"] = 1
        plan = self.plan()
        self.assertEqual(ops(plan), [("update", "step", "Legacy - Account address sync")])
        self.assertEqual(plan["actions"][0]["changes"], {"rank": [1, 10]})

    def test_message_compare_is_case_insensitive(self):
        self.step(self.asm, "Legacy.Plugins.AccountAddressSync")["message"] = "update"
        self.assertEqual(self.plan()["actions"], [])

    def test_unmanaged_optional_fields_are_ignored(self):
        self.step(self.pkg, "Contoso.Crm.Plugins.OrderPreDelete")["description"] = "set in the portal"
        self.assertEqual(self.plan()["actions"], [])

    def test_filtering_attributes_compare_as_sets(self):
        step = self.step(self.asm, "Legacy.Plugins.AccountAddressSync")
        step["filtering_attributes"] = ["address1_city"]
        self.assertEqual(self.plan()["actions"][0]["changes"],
                         {"filtering_attributes": [["address1_city"], ["address1_city", "address1_postalcode"]]})

    def test_server_only_step_and_image_are_deleted_first(self):
        extra = copy.deepcopy(self.step(self.pkg, "Contoso.Crm.Plugins.OrderPreCreate"))
        extra.update({"id": "99999999-0000-0000-0000-000000000001", "name": "Old step"})
        self.pkg["steps"]["old step"] = extra
        delete_step = self.step(self.pkg, "Contoso.Crm.Plugins.OrderPreDelete")
        delete_step["images"]["postimage"] = {"id": "99999999-0000-0000-0000-000000000002", "name": "PostImage",
                                              "alias": "PostImage", "type": "post", "attributes": ["x"],
                                              "message_property": "Target"}
        self.step(self.asm, "Legacy.Plugins.AccountAddressSync")["rank"] = 1
        plan = self.plan()
        self.assertEqual([(o, k) for o, k, _ in ops(plan)], [("delete", "image"), ("delete", "step"), ("update", "step")])
        self.assertEqual(len(dv.double_confirm(plan["actions"])), 2)

    def test_renamed_step_is_delete_plus_create(self):
        self.units[1]["steps"][0]["name"] = "Legacy - renamed"
        plan = self.plan()
        self.assertEqual([(o, k) for o, k, _ in ops(plan)],
                         [("delete", "step"), ("create", "step"), ("create", "image")])

    def test_image_update(self):
        img = self.step(self.asm, "Legacy.Plugins.AccountAddressSync")["images"]["preimage"]
        img["attributes"] = ["address1_city"]
        self.assertEqual(ops(self.plan()), [("update", "image", "Legacy - Account address sync / PreImage")])

    def test_custom_api_immutable_field_is_blocked(self):
        self.pkg["custom_apis"]["prfx_crm_recalculateorder"]["is_function"] = True
        plan = self.plan()
        self.assertEqual((plan["blocked"][0]["kind"], plan["blocked"][0]["field"]), ("customapi", "is_function"))

    def test_custom_api_display_name_is_updated(self):
        self.pkg["custom_apis"]["prfx_crm_recalculateorder"]["display_name"] = "Old"
        plan = self.plan()
        self.assertEqual(plan["actions"][0]["changes"], {"display_name": ["Old", "Recalculate order"]})

    def test_parameter_rules(self):
        api = self.pkg["custom_apis"]["prfx_crm_recalculateorder"]
        api["request"]["reason"]["type"] = "integer"
        api["request"]["legacy"] = {"id": "x", "uniquename": "Legacy", "type": "string", "table": None,
                                    "optional": True, "name": "", "display_name": "", "description": ""}
        plan = self.plan()
        self.assertEqual((plan["blocked"][0]["kind"], plan["blocked"][0]["field"]), ("request", "type"))
        self.assertEqual(plan["unmanaged"][0]["name"], "prfx_crm_RecalculateOrder.Legacy")

    def test_other_custom_apis_of_the_package_are_unmanaged(self):
        self.pkg["scope_apis"].append("prfx_crm_Legacy")
        self.assertEqual(self.plan()["unmanaged"][0]["kind"], "customapi")

    def test_missing_type_in_package_is_blocked_unless_pushed(self):
        del self.pkg["types"]["Contoso.Crm.Plugins.OrderPreCreate"]
        self.assertEqual(self.plan()["blocked"][0]["kind"], "plugintype")
        self.pkg["sha256"] = "b" * 64
        plan = self.plan()
        self.assertEqual(plan["blocked"], [])
        self.assertEqual(ops(plan), [("push", "package", "prfx_Contoso.Crm.Plugins")])

    def test_missing_type_in_assembly_is_created(self):
        del self.asm["types"]["Legacy.Plugins.AccountAddressSync"]
        self.assertEqual(ops(self.plan()), [("create", "plugintype", "Legacy.Plugins.AccountAddressSync")])


class TestDuplicateNames(unittest.TestCase):
    """The Plugin Registration Tool names steps '<type>: <message> of <table>', so steps that
    differ only by stage share a name. They must be blocked, never collapsed into one."""
    STEP = "X.Handler: Update of account"

    def step_row(self, rid, stage):
        return {"sdkmessageprocessingstepid": rid, "name": self.STEP, "stage": stage, "mode": 0, "rank": 1,
                "filteringattributes": "name", "statecode": 0, "_eventhandler_value": "t1",
                "sdkmessageid": {"name": "Update"}, "sdkmessagefilterid": {"primaryobjecttypecode": "account"}}

    def test_read_keeps_the_clash_visible(self):
        steps = [self.step_row("s1", 20), self.step_row("s2", 40)]
        images = [{"sdkmessageprocessingstepimageid": i, "name": "PreImage", "entityalias": "PreImage",
                   "imagetype": 0, "attributes": "name", "messagepropertyname": "Target",
                   "_sdkmessageprocessingstepid_value": "s1"} for i in ("i1", "i2")]

        class Api:
            def get(self, path):
                return {"content": "eA=="}

            def get_all(self, path):
                rows = {"pluginpackages": [{"pluginpackageid": "p1", "uniquename": "prfx_X", "version": "1.0.0"}],
                        "pluginassemblies": [{"pluginassemblyid": "a1", "name": "X"}],
                        "plugintypes": [{"plugintypeid": "t1", "typename": "X.Handler"}],
                        "sdkmessageprocessingstepimages": images, "sdkmessageprocessingsteps": steps}
                return next((v for k, v in rows.items() if path.startswith(k)), [])

        srv = dv.read_unit(Api(), {"kind": "package", "name": "prfx_X", "custom_apis": {}, "steps": []})
        self.assertEqual((len(srv["steps"]), srv["duplicate_steps"]), (1, [self.STEP]))
        self.assertEqual(srv["steps"][self.STEP.lower()]["duplicate_images"], ["PreImage"])

    def setUp(self):
        self.units = load_units()
        self.server = mirror_server(self.units)
        self.asm = self.server["units"]["legacy.plugins"]

    def test_duplicate_step_is_blocked_and_never_touched(self):
        self.asm["duplicate_steps"] = ["Legacy - Account address sync", "Orphan"]
        self.asm["steps"]["legacy - account address sync"]["rank"] = 1   # would be an update
        self.asm["steps"]["orphan"] = {**self.asm["steps"]["legacy - account address sync"],
                                       "id": "o1", "name": "Orphan"}     # would be a delete
        plan = dv.diff(self.units, self.server)
        self.assertEqual(plan["actions"], [])
        self.assertEqual(sorted(b["name"] for b in plan["blocked"]), ["Legacy - Account address sync", "Orphan"])

    def test_duplicate_image_is_blocked(self):
        step = self.asm["steps"]["legacy - account address sync"]
        step["duplicate_images"] = ["PreImage"]
        step["images"]["preimage"]["attributes"] = ["address1_city"]  # would be an update
        plan = dv.diff(self.units, self.server)
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["blocked"][0]["name"], "Legacy - Account address sync / PreImage")


class TestSolutionMembership(unittest.TestCase):
    def setUp(self):
        self.units = load_units()
        self.server = mirror_server(self.units)
        self.members = self.server["solutions"]["prfx_crm_Core"]["members"]
        pkg = self.server["units"]["prfx_contoso.crm.plugins"]
        api = pkg["custom_apis"]["prfx_crm_recalculateorder"]
        step = next(s for s in pkg["steps"].values() if s["images"])
        self.ids = {"package": pkg["id"], "assembly": pkg["assemblies"][0]["id"], "step": step["id"],
                    "customapi": api["id"],
                    "request": api["request"]["reason"]["id"], "response": api["response"]["total"]["id"]}

    def test_every_component_kind_is_added_to_the_solution(self):
        for kind, rid in self.ids.items():
            self.members.discard(rid.lower())
        plan = dv.diff(self.units, self.server)
        self.assertEqual([a["kind"] for a in plan["actions"]],
                         ["package", "customapi", "request", "response", "step"])
        for a in plan["actions"]:
            self.assertEqual(a["changes"], {"solution": ["absent", "prfx_crm_Core"]})
            self.assertEqual(a["component"], CTYPES[a["kind"]])
            self.assertEqual(a["id"], self.ids[a["kind"]])

    def test_step_image_is_never_added(self):
        # A step image is not a solution component: it travels with its step.
        self.assertEqual(dv.diff(self.units, self.server)["actions"], [])
        self.assertNotIn("image", dv.COMPONENT_TABLE)

    def test_package_assembly_is_never_added(self):
        # Dataverse refuses it (0x8004023b): the package carries its assemblies.
        self.members.discard(self.ids["assembly"].lower())
        self.assertEqual(dv.diff(self.units, self.server)["actions"], [])

    def test_standalone_assembly_is_added(self):
        asm = self.server["units"]["legacy.plugins"]
        self.members.discard(asm["id"].lower())
        plan = dv.diff(self.units, self.server)
        self.assertEqual(ops(plan), [("update", "assembly", "Legacy.Plugins")])
        self.assertEqual((plan["actions"][0]["component"], plan["actions"][0]["id"]), (91, asm["id"]))

    def test_membership_runs_after_other_changes(self):
        self.members.discard(self.ids["package"].lower())
        self.server["units"]["legacy.plugins"]["steps"]["legacy - account address sync"]["rank"] = 1
        plan = dv.diff(self.units, self.server)
        self.assertEqual([(a["op"], a["kind"], bool(a.get("component"))) for a in plan["actions"]],
                         [("update", "step", False), ("update", "package", True)])

    def test_unknown_component_type_is_a_note(self):
        self.server["component_types"]["customapi"] = None
        self.members.discard(self.ids["customapi"].lower())
        plan = dv.diff(self.units, self.server)
        self.assertEqual(plan["actions"], [])
        self.assertIn("customapi", plan["notes"][0])


class FakeApi:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or {}
        self.strong = True

    def request(self, method, path, body=None, headers=None, return_id=False, fatal=()):
        self.calls.append((method, path, body, headers or {}))
        return f"10000000-0000-0000-0000-{len(self.calls):012d}" if return_id else {}

    def get_all(self, path):
        self.calls.append(("GET", path, None, {}))
        for prefix, rows in self.rows.items():
            if path.startswith(prefix):
                return rows
        return []

    def posts(self):
        return [(p, h) for m, p, _b, h in self.calls if m == "POST"]


class TestApplier(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(SAMPLES / PACKAGE_SAMPLE, self.tmp / PACKAGE_SAMPLE)
        make_nupkg(self.tmp / "bin" / "Release" / "Contoso.Crm.Plugins.1.0.0.nupkg", "Contoso.Crm.Plugins", "1.0.0")
        path = self.tmp / PACKAGE_SAMPLE
        self.unit = dv.build_unit(path, dv.load_yaml(path))
        self.server = empty_server([self.unit])
        self.api = FakeApi({
            "pluginassemblies": [{"pluginassemblyid": "a1", "name": "Contoso.Crm.Plugins"}],
            "plugintypes": [{"plugintypeid": f"t{i}", "typename": t} for i, t in enumerate(
                ["Contoso.Crm.Plugins.OrderPreCreate", "Contoso.Crm.Plugins.OrderStatusChanged",
                 "Contoso.Crm.Plugins.OrderPreDelete", "Contoso.Crm.Plugins.RecalculateOrderApi"])],
            "sdkmessages": [{"sdkmessageid": "m1"}],
            "sdkmessagefilters": [{"sdkmessagefilterid": "f1"}],
            "customapirequestparameters": [{"customapirequestparameterid": "p1"}],
            "customapiresponseproperties": [{"customapiresponsepropertyid": "r1"}],
        })
        self.applier = dv.Applier(self.api, {"url": "https://a-dev.crm4.dynamics.com"}, [self.unit], self.server)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_plan(self):
        plan = dv.diff([self.unit], self.server)
        for a in plan["actions"]:
            self.applier.run(a)
        return plan

    def test_full_creation_uses_the_solution_header(self):
        plan = self.run_plan()
        self.assertEqual(len(plan["actions"]), 7)  # package, customapi, 3 steps, 2 images
        created = {p: h.get(dv.SOLUTION_HEADER) for p, h in self.api.posts() if p != "AddSolutionComponent"}
        for entity_set in ("pluginpackages", "customapis", "sdkmessageprocessingsteps"):
            self.assertEqual(created[entity_set], "prfx_crm_Core", entity_set)
        self.assertIsNone(created["sdkmessageprocessingstepimages"])
        # deep-inserted parameters are added explicitly; the package's assemblies never are
        added = [b["ComponentType"] for m, p, b, _ in self.api.calls if p == "AddSolutionComponent"]
        self.assertEqual(sorted(added), sorted([10028, 10029]))
        self.assertIn("id: 10000000-0000-0000-0000-", (self.tmp / PACKAGE_SAMPLE).read_text(encoding="utf-8"))

    def test_package_upload_is_base64_of_the_nupkg(self):
        self.run_plan()
        body = next(b for m, p, b, _ in self.api.calls if p == "pluginpackages")
        raw = (self.tmp / "bin" / "Release" / "Contoso.Crm.Plugins.1.0.0.nupkg").read_bytes()
        import base64
        self.assertEqual(base64.b64decode(body["content"]), raw)
        self.assertEqual((body["uniquename"], body["version"]), ("prfx_Contoso.Crm.Plugins", "1.0.0"))

    def test_step_and_image_payloads(self):
        self.run_plan()
        steps = [b for m, p, b, _ in self.api.calls if p == "sdkmessageprocessingsteps" and m == "POST"]
        status = next(s for s in steps if s["name"].endswith("Update of prfx_crm_order"))
        self.assertEqual((status["stage"], status["mode"], status["filteringattributes"], status["asyncautodelete"]),
                         (40, 1, "prfx_statuscode", True))
        self.assertEqual(status["eventhandler_plugintype@odata.bind"], "/plugintypes(t1)")
        image = next(b for m, p, b, _ in self.api.calls if p == "sdkmessageprocessingstepimages")
        self.assertEqual((image["imagetype"], image["messagepropertyname"]), (0, "Target"))

    def test_custom_api_payload(self):
        self.run_plan()
        body = next(b for m, p, b, _ in self.api.calls if p == "customapis")
        self.assertEqual((body["bindingtype"], body["boundentitylogicalname"], body["allowedcustomprocessingsteptype"]),
                         (1, "prfx_crm_order", 1))
        self.assertEqual(body["CustomAPIRequestParameters"][0]["isoptional"], True)
        self.assertEqual(body["CustomAPIResponseProperties"][0]["type"], 8)
        self.assertNotIn("isoptional", body["CustomAPIResponseProperties"][0])
        self.assertEqual(body["PluginTypeId@odata.bind"], "/plugintypes(t3)")

    def test_membership_update_adds_component(self):
        # Dataverse accepts DoNotIncludeSubcomponents only for a table (type 1): 0x80040216.
        for kind in ("step", "package"):
            self.applier.run({"op": "update", "kind": kind, "unit": self.unit["name"], "name": "x",
                              "id": "c9", "component": CTYPES[kind], "changes": {"solution": ["absent", "prfx_crm_Core"]}})
            _m, path, body, _h = self.api.calls[-1]
            self.assertEqual((path, body["ComponentType"], body["DoNotIncludeSubcomponents"], body["SolutionUniqueName"]),
                             ("AddSolutionComponent", CTYPES[kind], False, "prfx_crm_Core"))

    def test_add_component_validation_error_is_not_retried(self):
        seen = []
        def refuse(method, path, body=None, headers=None, return_id=False, fatal=()):
            seen.append(fatal)
            raise dv.ApiError(400, "0x80040216", "DoNotIncludeSubcomponents can not be set", path)
        self.api.request = refuse
        with self.assertRaises(dv.ApiError):
            self.applier.add_component(self.unit, "s9", 92)
        self.assertEqual(seen, [("0x80040216",)])

    def test_package_assembly_refused_by_the_server_is_a_note(self):
        def refuse(method, path, body=None, headers=None, return_id=False, fatal=()):
            raise dv.ApiError(405, "0x8004023B", "part of a Plugin Package", path)
        self.api.request = refuse
        self.applier.run({"op": "update", "kind": "assembly", "unit": self.unit["name"], "name": "x",
                          "id": "a1", "component": 91, "changes": {"solution": ["absent", "prfx_crm_Core"]}})
        self.assertEqual(len(self.applier.notes), 1)
        self.assertIn("a1", self.applier.notes[0])

    def test_other_membership_errors_still_fail(self):
        def refuse(method, path, body=None, headers=None, return_id=False, fatal=()):
            raise dv.ApiError(400, "0x80040203", "invalid", path)
        self.api.request = refuse
        with self.assertRaises(dv.ApiError):
            self.applier.run({"op": "update", "kind": "step", "unit": self.unit["name"], "name": "x",
                              "id": "s9", "component": 92, "changes": {"solution": ["absent", "prfx_crm_Core"]}})

    def test_delete_and_disable(self):
        self.applier.run({"op": "delete", "kind": "step", "unit": self.unit["name"], "name": "x", "id": "s9"})
        self.assertEqual(self.api.calls[-1][:2], ("DELETE", "sdkmessageprocessingsteps(s9)"))
        self.unit["steps"][0]["enabled"] = False
        self.applier.run({"op": "update", "kind": "step", "unit": self.unit["name"],
                          "name": self.unit["steps"][0]["name"], "id": "s1", "changes": {"enabled": [True, False]}})
        self.assertEqual(self.api.calls[-1][:3], ("PATCH", "sdkmessageprocessingsteps(s1)", {"statecode": 1, "statuscode": 2}))

    def test_push_runs_pac_against_the_dev_url(self):
        pushed = []
        original = dv.run_pac_push
        dv.run_pac_push = lambda *args: pushed.append(args)
        try:
            self.server["units"]["prfx_contoso.crm.plugins"]["id"] = "p1"
            self.applier.run({"op": "push", "kind": "package", "unit": self.unit["name"],
                              "name": self.unit["name"], "id": "p1"})
        finally:
            dv.run_pac_push = original
        self.assertEqual(pushed[0][:2], ("https://a-dev.crm4.dynamics.com", "p1"))
        self.assertIn("Contoso.Crm.Plugins.OrderPreCreate", self.server["units"]["prfx_contoso.crm.plugins"]["types"])


class TestWriteId(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "x.dv.plugin.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_replaces_null_and_keeps_comment(self):
        shutil.copy(SAMPLES / PACKAGE_SAMPLE, self.file)
        dv.write_id(self.file, "package", "12345678-1234-1234-1234-123456789012")
        text = self.file.read_text(encoding="utf-8")
        self.assertIn("  id: 12345678-1234-1234-1234-123456789012                           # written by apply", text)
        self.assertEqual(dv.load_yaml(self.file)["package"]["id"], "12345678-1234-1234-1234-123456789012")

    def test_inserts_after_name_when_missing(self):
        self.file.write_text("version: \"1.0\"\nassembly:\n  name: A\n  dll: a.dll\nsteps: []\n", encoding="utf-8")
        dv.write_id(self.file, "assembly", "abc")
        self.assertEqual(dv.load_yaml(self.file)["assembly"], {"name": "A", "id": "abc", "dll": "a.dll"})

    def test_flow_style_is_refused(self):
        self.file.write_text("package: {name: prfx_A, nupkg: a.nupkg}\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            dv.write_id(self.file, "package", "abc")


class TestPac(unittest.TestCase):
    def test_push_command_targets_dev_explicitly(self):
        cmd = dv.pac_push_command("https://a-dev.crm4.dynamics.com", "p1", "/b/x.nupkg", "package")
        self.assertEqual(cmd[1:], ["plugin", "push", "--pluginId", "p1", "--pluginFile", "/b/x.nupkg",
                                   "--type", "Nuget", "--environment", "https://a-dev.crm4.dynamics.com"])
        self.assertIn("Assembly", dv.pac_push_command("u", "a1", "/b/x.dll", "assembly"))


class TestExport(unittest.TestCase):
    def test_export_round_trips_to_an_empty_plan(self):
        units = load_units()
        server = mirror_server(units)
        tmp = Path(tempfile.mkdtemp())
        try:
            exported = []
            for u in units:
                srv = server["units"][u["name"].lower()]
                doc = dv.export_document(u["kind"], u["name"], srv, srv["custom_apis"], "prfx_crm_Core", "prfx")
                path = tmp / f"{u['name']}.dv.plugin.yaml"
                path.write_text(dv.yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
                self.assertEqual(dv.validate_path(path).errors, [], doc)
                unit = dv.build_unit(path, dv.load_yaml(path))
                unit["artifact"] = u["artifact"]
                exported.append(unit)
            self.assertEqual(dv.diff(exported, server)["actions"], [])
        finally:
            shutil.rmtree(tmp)


class TestDevGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_stage_must_be_dev(self):
        (self.root / ".env.dev").write_text("ENVIRONMENT_STAGE=prod\nDATAVERSE_URL=https://a.crm4.dynamics.com\n",
                                            encoding="utf-8")
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in dv.ENV_KEYS}, clear=True):
            with self.assertRaisesRegex(dv.GuardError, "stage 'dev'"):
                dv.dev_environment(self.root)

    def test_managed_solution_is_refused(self):
        class Api:
            url = "https://a-dev.crm4.dynamics.com"

            def get_all(self, path):
                return [{"solutionid": "s1", "ismanaged": True}]
        with self.assertRaisesRegex(dv.GuardError, "MANAGED"):
            dv.check_solution(Api(), "x")


class TestSharedBlock(unittest.TestCase):
    """The dev guard and Web API client are copied from dv_metadata.py (plugins are
    installed independently, so no cross-plugin import). Detect drift."""
    METADATA = SKILL.parents[2] / "dataverse-metadata-as-code" / "skills" / "dvac-metadata" / "scripts"
    SHARED = ("GuardError", "ApiError", "_c", "green", "yellow", "red", "grey", "bold", "Report",
              "ENV_KEYS", "ENV_HELP", "find_repo_root", "read_dotenv", "_norm_url", "add_env_args",
              "env_inputs", "_env_source", "dev_environment", "az_path", "az_json",
              "_debug", "Api", "check_solution")
    ALLOWED = {"dvac-plugins": "dvac-metadata", "DV_PLUGIN_DEBUG": "DV_METADATA_DEBUG"}

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


class TestApiRetry(unittest.TestCase):
    """0x80040216 is retried as a metadata cache error, unless the caller marks it fatal."""

    def call(self, fatal):
        import io, urllib.error, urllib.request
        api = dv.Api({"url": "https://a-dev.crm4.dynamics.com", "tenant": None})
        api.token = lambda: "t"
        attempts, sleeps = [], []
        def urlopen(req, timeout=None):
            attempts.append(req.full_url)
            body = json.dumps({"error": {"code": "0x80040216", "message": "boom"}}).encode()
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
        orig = urllib.request.urlopen, dv.time.sleep
        urllib.request.urlopen, dv.time.sleep = urlopen, sleeps.append
        try:
            with self.assertRaises(dv.ApiError):
                api.request("POST", "AddSolutionComponent", {}, fatal=fatal)
        finally:
            urllib.request.urlopen, dv.time.sleep = orig
        return len(attempts), len(sleeps)

    def test_fatal_code_fails_at_once(self):
        self.assertEqual(self.call(("0x80040216",)), (1, 0))

    def test_code_is_retried_by_default(self):
        self.assertEqual(self.call(()), (6, 5))


class TestHook(unittest.TestCase):
    HOOK = SKILL / "scripts" / "hook_validate.py"

    def run_hook(self, path):
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})
        return subprocess.run([sys.executable, str(self.HOOK)], input=payload, capture_output=True, text=True)

    def test_ignores_other_files(self):
        res = self.run_hook("/somewhere/Plugin.cs")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_blocks_invalid_model(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / PACKAGE_SAMPLE
            text = (SAMPLES / PACKAGE_SAMPLE).read_text(encoding="utf-8")
            f.write_text(text.replace("    stage: post-operation\n    mode: async", "    stage: pre-operation\n    mode: async"),
                         encoding="utf-8")
            res = self.run_hook(f)
            self.assertEqual(res.returncode, 2)
            self.assertIn("async steps run only in post-operation", res.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_warnings_are_returned_as_context(self):
        res = self.run_hook(SAMPLES / ASSEMBLY_SAMPLE)
        self.assertEqual(res.returncode, 0)
        self.assertIn("no build output matches", json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
