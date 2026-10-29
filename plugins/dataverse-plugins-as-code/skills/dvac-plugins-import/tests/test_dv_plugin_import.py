"""Offline tests for dv_plugin_import.py — alignment of the plug-in files with an unpacked solution.

Run:  python3 -m unittest discover -s <skill>/tests -v

tests/fixtures/prfx_crm_Core is a synthetic unpacked solution (SolutionPackager layout) that
describes exactly the dvac-plugins samples: a plug-in package whose types are known by export
key only, and a plug-in assembly whose types are named.
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import dv_plugin_import as imp  # noqa: E402

dvp = imp.dvp
SAMPLES = SKILL.parent / "dvac-plugins" / "references" / "samples"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prfx_crm_Core"
CONTOSO = "plugins/Contoso.Crm.Plugins/Contoso.Crm.Plugins.dv.plugin.yaml"
LEGACY = "plugins/Legacy.Plugins/Legacy.Plugins.dv.plugin.yaml"
STEPS = "SdkMessageProcessingSteps"
CREATE_STEP = f"{STEPS}/{{0b0a0001-0000-4000-8000-000000000011}}.xml"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = imp.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            err.write(str(e))
    return code, out.getvalue(), err.getvalue()


class ImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sol = self.tmp / "solution"
        shutil.copytree(FIXTURE, self.sol)
        self.root = self.tmp / "repo"
        (self.root / ".git").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def with_samples(self):
        for name, rel in (("Contoso.Crm.Plugins", CONTOSO), ("Legacy.Plugins", LEGACY)):
            target = self.root / rel
            target.parent.mkdir(parents=True)
            shutil.copy(SAMPLES / f"{name}.dv.plugin.yaml", target)

    def edit(self, path, old, new):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def imp(self, *opts):
        return run("solution", str(self.sol), "--root", str(self.root), *opts)

    def unit(self, rel):
        return dvp.load_yaml(self.root / rel)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(self.root.rglob("*.yaml"))}


class TestRoundTrip(ImportCase):
    def test_samples_already_match(self):
        self.with_samples()
        before = self.snapshot()
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"= {CONTOSO}", out)
        self.assertIn(f"= {LEGACY}", out)
        self.assertEqual(self.snapshot(), before)

    def test_import_into_empty_repo(self):
        code, out, _ = self.imp()
        self.assertEqual(code, 2)
        self.assertIn(f"+ {CONTOSO}", out)
        self.assertIn(f"+ {LEGACY}", out)
        self.assertEqual(self.snapshot(), {}, "nothing is written without --write")
        code, out, err = self.imp("--write")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(dvp.validate_path(self.root).errors, [])
        contoso = self.unit(CONTOSO)
        self.assertEqual(contoso["package"], {"name": "prfx_Contoso.Crm.Plugins", "nupkg": "bin/Release/*.nupkg",
                                              "id": None})
        steps = {s["plugin"]: s for s in contoso["steps"]}
        self.assertEqual(steps["Contoso.Crm.Plugins.OrderStatusChanged"]["mode"], "async")
        self.assertEqual(steps["Contoso.Crm.Plugins.OrderStatusChanged"]["images"][0]["attributes"],
                         ["prfx_customerid", "prfx_statuscode"])
        self.assertEqual(steps["Contoso.Crm.Plugins.OrderPreCreate"]["description"],
                         "Sets the order number and the default status.")
        api = contoso["custom_apis"]["prfx_crm_RecalculateOrder"]
        self.assertNotIn("plugin", api)
        self.assertIn("custom API prfx_crm_RecalculateOrder: plug-in class unknown", out)
        legacy = self.unit(LEGACY)
        self.assertEqual(legacy["steps"][0]["name"], "Legacy - Account address sync")
        self.assertEqual(legacy["steps"][0]["plugin"], "Legacy.Plugins.AccountAddressSync")
        self.assertEqual(legacy["steps"][0]["configuration"], '{"country": "FR"}')
        # Idempotent.
        before = self.snapshot()
        self.assertEqual(self.imp()[0], 0)
        self.assertEqual(self.snapshot(), before)


class TestAlign(ImportCase):
    def test_values_the_solution_lacks_are_kept(self):
        self.with_samples()
        path = self.root / CONTOSO
        self.edit(path, "  id: null ", "  id: 11111111-2222-4333-8444-555555555555 ")
        self.edit(path, "    description: Sets the order number and the default status.",
                  "    description: Sets the order number and the default status.\n    enabled: false")
        self.edit(self.sol / CREATE_STEP, "<Rank>1</Rank>", "<Rank>3</Rank>")
        code, out, _ = self.imp("--write")
        self.assertEqual(code, 0, out)
        unit = self.unit(CONTOSO)
        self.assertEqual(unit["package"]["id"], "11111111-2222-4333-8444-555555555555")
        step = unit["steps"][0]
        self.assertEqual((step["rank"], step["enabled"]), (3, False))
        self.assertEqual(unit["custom_apis"]["prfx_crm_RecalculateOrder"]["plugin"],
                         "Contoso.Crm.Plugins.RecalculateOrderApi")
        self.assertEqual(unit["custom_apis"]["prfx_crm_RecalculateOrder"]["request"]["Reason"]["display_name"],
                         "Reason")

    def test_model_only_step_is_kept(self):
        self.with_samples()
        (self.sol / CREATE_STEP).unlink()
        code, out, _ = self.imp()
        self.assertEqual(code, 0, out)
        self.assertIn("step Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order (not in the solution)", out)

    def test_class_learned_from_another_step(self):
        """A step with its own name: the class comes from another step of the same export key."""
        text =(self.sol / CREATE_STEP).read_text(encoding="utf-8")
        renamed = text.replace('Name="Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order"',
                               'Name="Order - default values"').replace("0b0a0001", "0b0a0009")
        (self.sol / STEPS / "{0b0a0009-0000-4000-8000-000000000011}.xml").write_text(renamed, encoding="utf-8")
        self.assertEqual(self.imp("--write")[0], 0)
        step = next(s for s in self.unit(CONTOSO)["steps"] if s.get("name") == "Order - default values")
        self.assertEqual((step["plugin"], step["message"]), ("Contoso.Crm.Plugins.OrderPreCreate", "Create"))

    def test_unknown_class_is_skipped(self):
        self.edit(self.sol / CREATE_STEP, 'Name="Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order"',
                  'Name="Order - default values"')
        code, out, _ = self.imp()
        self.assertEqual(code, 2)
        self.assertIn("step Order - default values: plug-in class or message unknown", out)

    def test_two_packages_need_the_existing_files(self):
        other = self.sol / "pluginpackages" / "prfx_Other"
        other.mkdir()
        (other / "pluginpackage.xml").write_text('<pluginpackage uniquename="prfx_Other" />', encoding="utf-8")
        _, out, _ = self.imp()
        self.assertIn("cannot tell which package or assembly holds 'Contoso.Crm.Plugins.OrderPreCreate'", out)
        self.with_samples()
        _, out, _ = self.imp()
        self.assertNotIn("cannot tell", out.split("custom API")[0])
        self.assertIn("+ plugins/Other/Other.dv.plugin.yaml", out)


class TestErrors(ImportCase):
    def test_invalid_result_writes_nothing(self):
        self.edit(self.sol / CREATE_STEP, "<Mode>0</Mode>", "<Mode>1</Mode>")  # async in pre-operation
        code, _, err = self.imp("--write")
        self.assertEqual(code, 1)
        self.assertIn("async steps run only in post-operation", err)
        self.assertEqual(self.snapshot(), {})

    def test_not_an_unpacked_solution(self):
        code, _, err = run("solution", str(self.tmp), "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("not an unpacked solution", err)


if __name__ == "__main__":
    unittest.main()
