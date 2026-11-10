"""Offline tests for dv_plugin_solution.py — import and compare between the plug-in files and an unpacked solution.

Run:  python3 -m unittest discover -s <skill>/tests -v

tests/fixtures/prfx_crm_Core is a synthetic unpacked solution (SolutionPackager layout) that
describes exactly the dvac-plugins samples: a plug-in package whose types are known by export
key only, and a plug-in assembly whose types are named.
"""
import contextlib
import hashlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import dv_plugin_solution as imp  # noqa: E402

dvp = imp.dvp
SAMPLES = SKILL.parent / "dvac-plugins" / "references" / "samples"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prfx_crm_Core"
CONTOSO = "src/Contoso.Crm.Plugins/Contoso.Crm.Plugins.dv.plugin.yaml"
LEGACY = "src/Legacy.Plugins/Legacy.Plugins.dv.plugin.yaml"
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
        for project in ("Contoso.Crm.Plugins", "Legacy.Plugins", "Other"):  # the .csproj a new file goes next to
            (self.root / "src" / project).mkdir(parents=True)
            (self.root / "src" / project / f"{project}.csproj").write_text("<Project />", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def with_samples(self):
        for name, rel in (("Contoso.Crm.Plugins", CONTOSO), ("Legacy.Plugins", LEGACY)):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SAMPLES / f"{name}.dv.plugin.yaml", target)

    def edit(self, path, old, new):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def imp(self, *opts):
        return run("import", str(self.sol), "--root", str(self.root), *opts)

    def cmp(self, *opts):
        return run("compare", str(self.sol), "--root", str(self.root), *opts)

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
        self.assertEqual(code, 0)
        self.assertIn("STATUS: changes-pending", out)
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
        self.assertEqual(code, 0)
        self.assertIn("STATUS: changes-pending", out)
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
        self.assertIn("+ src/Other/Other.dv.plugin.yaml", out)


class TestErrors(ImportCase):
    def test_invalid_result_writes_nothing(self):
        self.edit(self.sol / CREATE_STEP, "<Mode>0</Mode>", "<Mode>1</Mode>")  # async in pre-operation
        code, _, err = self.imp("--write")
        self.assertEqual(code, 1)
        self.assertIn("async steps run only in post-operation", err)
        self.assertEqual(self.snapshot(), {})

    def test_not_an_unpacked_solution(self):
        code, out, _ = run("import", str(self.tmp), "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("STATUS: error", out)
        self.assertIn("not an unpacked solution", out)


class TestNewFileLocation(ImportCase):
    """The only location rule: a new file sits next to its project's .csproj."""

    def test_new_file_goes_next_to_the_csproj(self):
        shutil.rmtree(self.root / "src" / "Legacy.Plugins")
        (self.root / "app" / "legacy").mkdir(parents=True)
        (self.root / "app" / "legacy" / "Legacy.Plugins.csproj").write_text("<Project />", encoding="utf-8")
        code, out, _ = self.imp()
        self.assertEqual(code, 0, out)
        self.assertIn("+ app/legacy/Legacy.Plugins.dv.plugin.yaml", out)

    def test_no_csproj_stops_with_a_fix(self):
        shutil.rmtree(self.root / "src" / "Legacy.Plugins")
        code, out, _ = self.imp()
        self.assertEqual(code, 1)
        self.assertIn("STATUS: error", out)
        self.assertIn("no Legacy.Plugins.csproj below", out)
        self.assertIn("FIX: pass --projects-dir <folder>", out)

    def test_projects_dir_for_a_project_not_created_yet(self):
        shutil.rmtree(self.root / "src")
        code, out, _ = self.imp("--projects-dir", "dataverse/plugins", "--write")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "dataverse/plugins/Contoso.Crm.Plugins/Contoso.Crm.Plugins.dv.plugin.yaml").is_file())

    def test_existing_files_are_found_wherever_they_are(self):
        moved = self.root / "elsewhere" / "Contoso.Crm.Plugins.dv.plugin.yaml"
        moved.parent.mkdir(parents=True)
        shutil.copy(SAMPLES / "Contoso.Crm.Plugins.dv.plugin.yaml", moved)
        code, out, _ = self.imp()
        self.assertEqual(code, 0, out)
        self.assertIn("= elsewhere/Contoso.Crm.Plugins.dv.plugin.yaml", out)


class TestCompare(ImportCase):
    def test_samples_are_in_sync(self):
        self.with_samples()
        before = self.snapshot()
        code, out, err = self.cmp("--detailed-exitcode")
        self.assertEqual(code, 0, out + err)
        self.assertIn("STATUS: in-sync", out)
        self.assertIn("Not compared (the solution does not carry it)", out)
        self.assertEqual(self.snapshot(), before, "compare writes nothing")

    def test_differences_by_section(self):
        self.with_samples()
        self.edit(self.sol / CREATE_STEP, "<Rank>1</Rank>", "<Rank>3</Rank>")
        (self.sol / STEPS / "{0b0a0003-0000-4000-8000-000000000013}.xml").unlink()
        self.edit(self.root / CONTOSO, "    description: Sets the order number and the default status.\n", "")
        code, out, _ = self.cmp()
        self.assertEqual(code, 0, out)
        self.assertIn("~ prfx_Contoso.Crm.Plugins / step Contoso.Crm.Plugins.OrderPreCreate: Create of "
                      "prfx_crm_order.rank: model=1  solution=3", out)
        self.assertRegex(out, r"- step [^\n]+\(prfx_Contoso\.Crm\.Plugins\)")
        self.assertIn("· prfx_Contoso.Crm.Plugins / step Contoso.Crm.Plugins.OrderPreCreate: Create of "
                      "prfx_crm_order.description = 'Sets the order number and the default status.'", out)
        self.assertIn("STATUS: drift", out)
        self.assertIn("REASON: 2 difference(s)", out)
        self.assertRegex(out, r"NEXT \(only after the user confirms\): .*dv_plugin_solution\.py import ")
        self.assertNotIn("--write", out)
        self.assertEqual(self.cmp("--detailed-exitcode")[0], 2)

    def test_unit_only_in_the_solution(self):
        shutil.copy(SAMPLES / "Contoso.Crm.Plugins.dv.plugin.yaml", self.root / CONTOSO)
        _, out, _ = self.cmp()
        self.assertIn("+ assembly Legacy.Plugins (1 step(s), 0 custom API(s))", out)

    def test_unknown_class_is_not_reported_as_missing(self):
        self.with_samples()
        self.edit(self.sol / CREATE_STEP, 'Name="Contoso.Crm.Plugins.OrderPreCreate: Create of prfx_crm_order"',
                  'Name="Order - default values"')
        _, out, _ = self.cmp()
        self.assertIn("! step Order - default values: plug-in class or message unknown", out)


def tree_digest(folder):
    """sha256 and mtime of every file below folder."""
    return {p.relative_to(folder).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


class TestSolutionReadOnly(ImportCase):
    def test_no_subcommand_touches_the_solution(self):
        before = tree_digest(self.sol)
        self.imp()
        self.imp("--write")
        self.cmp()
        self.edit(self.root / CONTOSO, "    stage: pre-operation\n", "    stage: pre-operation\n    rank: 2\n")
        self.cmp()
        self.imp("--write")
        self.assertEqual(tree_digest(self.sol), before)

    def test_projects_dir_inside_the_solution_is_refused(self):
        shutil.rmtree(self.root / "src")
        sol = self.root / "solutions" / "core"
        shutil.copytree(FIXTURE, sol)
        before = tree_digest(sol)
        code, out, _ = run("import", str(sol), "--root", str(self.root),
                           "--projects-dir", "solutions/core/plugins", "--write")
        self.assertEqual(code, 1)
        self.assertIn("STATUS: refused", out)
        self.assertEqual(tree_digest(sol), before)

    def test_plugin_file_inside_the_solution_is_ignored(self):
        self.with_samples()
        sol = self.root / "solutions" / "core"
        shutil.copytree(FIXTURE, sol)
        stray = sol / "Contoso.Crm.Plugins.dv.plugin.yaml"
        shutil.copy(SAMPLES / "Contoso.Crm.Plugins.dv.plugin.yaml", stray)
        self.edit(self.root / CONTOSO, "    stage: pre-operation\n", "    stage: pre-operation\n    rank: 2\n")
        before = tree_digest(sol)
        code, out, _ = run("import", str(sol), "--root", str(self.root), "--write")
        self.assertEqual(code, 0, out)
        self.assertIn(f"~ {CONTOSO}", out)
        self.assertNotIn("solutions/core/Contoso", out)
        self.assertEqual(tree_digest(sol), before)


if __name__ == "__main__":
    unittest.main()
