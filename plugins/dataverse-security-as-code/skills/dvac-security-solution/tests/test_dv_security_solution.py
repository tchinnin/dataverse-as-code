"""Offline tests for dv_security_solution.py — import and compare between the role files and an unpacked solution.

Run:  python3 -m unittest discover -s <skill>/tests -v

tests/fixtures/prfx_crm_Core is a synthetic unpacked solution (SolutionPackager layout) that
describes exactly the dvac-security samples: two roles and the two tables they reference.
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
import dv_security_solution as imp  # noqa: E402

dvs = imp.dvs
SAMPLES = SKILL.parent / "dvac-security" / "references" / "samples"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prfx_crm_Core"
CLERK_XML = "Roles/Order Clerk.xml"
CLERK = "roles/order-clerk.dv.security.yaml"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = imp.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            err.write(str(e))
    return code, out.getvalue(), err.getvalue()


def tree_digest(folder):
    """sha256 and mtime of every file below folder."""
    return {p.relative_to(folder).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sol = self.tmp / "solution"
        shutil.copytree(FIXTURE, self.sol)
        self.model = self.tmp / "dataverse" / "security"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def with_samples(self):
        shutil.copytree(SAMPLES, self.model)

    def edit(self, path, old, new):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def imp(self, *opts):
        return run("import", str(self.sol), "--model", str(self.model), *opts)

    def cmp(self, *opts):
        return run("compare", str(self.sol), "--model", str(self.model), *opts)

    def snapshot(self):
        return {p.relative_to(self.model).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(self.model.rglob("*")) if p.is_file()}

    def role(self, rel=CLERK):
        return dvs.load_yaml(self.model / rel)


class TestImport(Case):
    def test_samples_already_match(self):
        self.with_samples()
        before = self.snapshot()
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("STATUS: no-changes", out)
        self.assertEqual(self.snapshot(), before)

    def test_import_into_empty_model(self):
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("STATUS: changes-pending", out)
        self.assertFalse(self.model.exists(), "nothing is written without --write")
        code, out, err = self.imp("--write")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(dvs.validate_model(self.model).errors, [])
        self.assertEqual(dvs.load_yaml(self.model / "_meta.dv.security.yaml"),
                         {"version": "1.0", "solution": "prfx_crm_Core"})
        clerk = self.role()
        self.assertEqual(clerk["member_inheritance"], "direct_and_team")
        self.assertEqual(clerk["tables"]["prfx_crm_order"]["read"], "business_unit")
        # account is not a table of the solution: its privilege is kept by name.
        self.assertEqual(clerk["privileges"], {"prvExportToExcel": "organization", "prvReadAccount": "organization"})
        before = self.snapshot()
        self.assertIn("STATUS: no-changes", self.imp()[1])
        self.assertEqual(self.snapshot(), before)

    def test_level_change_keeps_the_layout(self):
        self.with_samples()
        self.edit(self.sol / CLERK_XML, 'name="prvReadprfx_crm_Order" level="Local"',
                  'name="prvReadprfx_crm_Order" level="Global"')
        code, out, _ = self.imp("--write")
        self.assertEqual(code, 0, out)
        self.assertIn("-    read: business_unit", out)
        self.assertIn("+    read: organization", out)
        clerk = self.role()
        self.assertEqual(clerk["tables"]["account"], {"read": "organization"})
        self.assertTrue((self.model / CLERK).read_text(encoding="utf-8").startswith("# Example role."))

    def test_model_only_privilege_is_kept(self):
        self.with_samples()
        self.edit(self.sol / CLERK_XML, '    <RolePrivilege name="prvExportToExcel" level="Global" />\n', "")
        code, out, _ = self.imp()
        self.assertEqual(code, 0, out)
        self.assertIn("STATUS: no-changes", out)
        self.assertIn("role Order Clerk: prvExportToExcel: organization (not in the solution)", out)

    def test_new_privilege_on_a_known_table(self):
        self.with_samples()
        self.edit(self.sol / CLERK_XML, "  </RolePrivileges>",
                  '    <RolePrivilege name="prvDeleteprfx_crm_Order" level="Basic" />\n  </RolePrivileges>')
        self.assertEqual(self.imp("--write")[0], 0)
        self.assertEqual(list(self.role()["tables"]["prfx_crm_order"]),
                         ["create", "read", "write", "append", "append_to", "delete"])

    def test_roles_filter(self):
        _, out, _ = self.imp("--roles", "order manager")
        self.assertIn("order-manager.dv.security.yaml", out)
        self.assertNotIn("order-clerk", out)

    def test_not_an_unpacked_solution(self):
        code, out, _ = run("import", str(self.tmp), "--model", str(self.model))
        self.assertEqual(code, 1)
        self.assertIn("not an unpacked solution", out)


class TestCompare(Case):
    def test_samples_are_in_sync(self):
        self.with_samples()
        before = self.snapshot()
        code, out, err = self.cmp("--detailed-exitcode")
        self.assertEqual(code, 0, out + err)
        self.assertIn("STATUS: in-sync", out)
        self.assertNotIn("Order Manager.description", out, "not carried by this role XML: not compared")
        self.assertEqual(self.snapshot(), before, "compare writes nothing")

    def test_differences_by_section(self):
        self.with_samples()
        self.edit(self.sol / CLERK_XML, 'name="prvReadprfx_crm_Order" level="Local"',
                  'name="prvReadprfx_crm_Order" level="Global"')
        self.edit(self.sol / CLERK_XML, "  </RolePrivileges>",
                  '    <RolePrivilege name="prvBulkDelete" level="Global" />\n  </RolePrivileges>')
        self.edit(self.model / CLERK, "    append_to: organization\n", "    append_to: organization\n    write: user\n")
        code, out, _ = self.cmp()
        self.assertEqual(code, 0, out)
        self.assertIn("~ role Order Clerk: prfx_crm_order.read: model=business_unit  solution=organization", out)
        self.assertIn("+ role Order Clerk: prvBulkDelete: organization", out)
        self.assertIn("- role Order Clerk: prfx_crm_customer.write: user", out)
        self.assertIn("STATUS: drift", out)
        self.assertIn("REASON: 3 difference(s)", out)
        self.assertRegex(out, r"NEXT \(only after the user confirms\): .*dv_security_solution\.py import ")
        self.assertEqual(self.cmp("--detailed-exitcode")[0], 2)

    def test_role_only_on_one_side(self):
        self.with_samples()
        (self.sol / "Roles" / "Order Manager.xml").unlink()
        shutil.copy(SAMPLES / "roles" / "order-manager.dv.security.yaml", self.model / "roles" / "auditor.dv.security.yaml")
        self.edit(self.model / "roles" / "auditor.dv.security.yaml", "name: Order Manager", "name: Auditor")
        _, out, _ = self.cmp()
        self.assertIn("- role Auditor", out)
        self.assertIn("- role Order Manager", out)

    def test_table_outside_the_solution_is_not_guessed(self):
        self.with_samples()
        self.edit(self.model / CLERK, "  account:\n    read: organization\n",
                  "  systemuser:\n    read: organization\n")
        self.edit(self.sol / CLERK_XML, 'name="prvReadAccount"', 'name="prvReadUser"')
        _, out, _ = self.cmp()
        self.assertIn("! role Order Clerk: systemuser.read — the privilege name of 'systemuser' is not known", out)
        self.assertIn("! role Order Clerk: prvReadUser: organization — may be one of the table privileges above", out)
        self.assertIn("STATUS: in-sync", out)

    def test_no_model(self):
        code, out, _ = self.cmp()
        self.assertEqual(code, 1)
        self.assertIn("no model to compare", out)


class TestOptionalRoleFields(Case):
    """Description and IsInherited are compared only when the role XML holds them (pac exports
    observed so far carry neither)."""

    def test_undeclared_and_different(self):
        self.with_samples()
        self.edit(self.model / CLERK, "member_inheritance: direct_and_team\n", "")
        self.edit(self.model / CLERK, "description: Creates", "description: Opens")
        _, out, _ = self.cmp()
        self.assertIn("· role Order Clerk.member_inheritance = 'direct_and_team'", out)
        self.assertIn("~ role Order Clerk.description: model='Opens", out)

    def test_not_carried_keeps_the_file(self):
        self.with_samples()
        self.edit(self.model / "roles" / "order-manager.dv.security.yaml", "description: Manages", "description: Runs")
        _, out, _ = self.imp()
        self.assertIn("STATUS: no-changes", out)


class TestSolutionReadOnly(Case):
    def test_no_subcommand_touches_the_solution(self):
        before = tree_digest(self.sol)
        self.imp()
        self.imp("--write")
        self.cmp()
        self.edit(self.model / CLERK, "prvExportToExcel: organization", "prvExportToExcel: organization\n  prvBulkDelete: organization")
        self.cmp()
        self.imp("--write")
        self.assertEqual(tree_digest(self.sol), before)

    def test_model_inside_the_solution_is_refused(self):
        before = tree_digest(self.sol)
        for model in (self.sol, self.sol / "Roles" / "model"):
            code, out, _ = run("import", str(self.sol), "--model", str(model), "--write")
            self.assertEqual(code, 1)
            self.assertIn("STATUS: refused", out)
        self.assertEqual(tree_digest(self.sol), before)


if __name__ == "__main__":
    unittest.main()
