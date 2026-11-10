"""Offline tests for dv_security_docs.py — matrix, role diagrams, filters, determinism, CI check.

Run:  python3 -m unittest discover -s <skill>/tests -v
Regenerate the example after an intended output change:
      python3 <skill>/tests/test_dv_security_docs.py --regenerate-example
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
import dv_security_docs as docs  # noqa: E402

SAMPLES = SKILL.parent / "dvac-security" / "references" / "samples"
EXAMPLE = SKILL / "references" / "examples" / "security-roles.md"

META = 'version: "1.0"\nsolution: prfx_Core\n'
CLERK = """name: Clerk
description: 'Handles | orders'
member_inheritance: team_only
tables:
  prfx_order: {create: user, read: business_unit, write: user, delete: none}
  account: {read: organization}
  contact: {read: organization}
  prfx_empty: {read: none}
privileges:
  prvExportToExcel: organization
  prvBulkDelete: none
"""
ADMIN = """name: 'Admin "A"'
tables:
  prfx_order: {read: parent_child, write: parent_child}
"""
EMPTY = "name: Nobody\n"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = docs.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            err.write(str(e))
    return code, out.getvalue(), err.getvalue()


def samples_repo(tmp: Path) -> Path:
    """The samples in a repo of their own, so source paths are stable wherever the tests run."""
    repo = tmp / "samples"
    shutil.copytree(SAMPLES, repo / "dataverse" / "security")
    (repo / ".git").mkdir()
    return repo


def regenerate_example():
    tmp = Path(tempfile.mkdtemp())
    try:
        return docs.main(["doc", str(samples_repo(tmp) / "dataverse" / "security"), "--out", str(EXAMPLE)])
    finally:
        shutil.rmtree(tmp)


class DocCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.model = self.repo / "dataverse" / "security"
        (self.model / "roles").mkdir(parents=True)
        (self.model / "_meta.dv.security.yaml").write_text(META, encoding="utf-8")
        for name, body in (("clerk", CLERK), ("admin", ADMIN), ("nobody", EMPTY)):
            (self.model / "roles" / f"{name}.dv.security.yaml").write_text(body, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def doc(self, *opts, path=None):
        code, out, err = run("doc", str(path or self.model), *opts)
        self.assertEqual(code, 0, err)
        return out

    def section(self, out, title):
        return out.split(f"### {title}\n", 1)[1].split("\n### ", 1)[0]


class TestDocument(DocCase):
    def test_example_is_up_to_date(self):
        model = samples_repo(self.tmp) / "dataverse" / "security"
        code, _, err = run("doc", str(model), "--out", str(EXAMPLE), "--check")
        self.assertEqual(code, 0, err + " — regenerate with: python3 tests/test_dv_security_docs.py --regenerate-example")

    def test_deterministic(self):
        self.assertEqual(self.doc(), self.doc())

    def test_summary(self):
        out = self.doc()
        titles = [l[4:] for l in out.splitlines() if l.startswith("### ")]
        self.assertEqual(titles, ["Access levels", 'Admin "A"', "Clerk", "Nobody"])
        self.assertIn("| [Clerk](#clerk) | Handles \\| orders | Team privileges only | 3 | 1 | "
                      "`dataverse/security/roles/clerk.dv.security.yaml` |", out)
        self.assertNotIn(str(self.tmp), out)

    def test_matrix(self):
        out = self.doc()
        matrix = out.split("## Access by table\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn('| Table | Admin "A" | Clerk | Nobody |', matrix)
        self.assertIn("| `prfx_order` | 🔴 read, write | 🔵 read<br>⚪ create, write | - |", matrix)
        self.assertIn("| `account` | - | 🟢 read | - |", matrix)
        self.assertNotIn("prfx_empty", matrix)  # no role has access

    def test_role_diagram(self):
        sec = self.section(self.doc(), "Clerk")
        self.assertIn('role --> l_organization["🟢 All records"]', sec)
        self.assertIn('l_organization --> n1["<b>read</b><br/>account<br/>contact"]', sec)  # same verbs: one node
        self.assertIn('l_organization --> n2["<b>other privileges</b><br/>prvExportToExcel"]', sec)
        self.assertIn('l_business_unit --> n3["<b>read</b><br/>prfx_order"]', sec)
        self.assertIn('l_user --> n4["<b>create, write</b><br/>prfx_order"]', sec)
        self.assertNotIn("prvBulkDelete", sec)  # none = not held
        self.assertNotIn("l_parent_child", sec)
        self.assertIn('role(["Admin #quot;A#quot;"])', self.section(self.doc(), 'Admin "A"'))

    def test_role_grid(self):
        sec = self.section(self.doc(), "Clerk")
        self.assertIn("| `prfx_order` | ⚪ | 🔵 | ⚪ | - | - | - | - | - |", sec)
        self.assertIn("| `prvExportToExcel` | 🟢 Organization |", sec)
        self.assertIn("| Member's privilege inheritance | Team privileges only |", sec)
        self.assertNotIn("prfx_empty", sec)

    def test_empty_role(self):
        sec = self.section(self.doc(), "Nobody")
        self.assertIn('role --> empty["No privilege"]', sec)
        self.assertIn("The role holds no privilege.", sec)
        self.assertIn("| Member's privilege inheritance | not managed by the model |", sec)

    def test_file_path_finds_model(self):
        self.assertEqual(self.doc(path=self.model / "roles" / "clerk.dv.security.yaml"), self.doc())


class TestFilters(DocCase):
    def test_roles_filter(self):
        out = self.doc("--roles", "clerk")
        self.assertIn("| Table | Clerk |", out)
        self.assertNotIn("Nobody", out)

    def test_unknown_role_is_an_error(self):
        code, _, err = run("doc", str(self.model), "--roles", "Clerk,Ghost")
        self.assertEqual(code, 1)
        self.assertIn("ghost", err)

    def test_tables_filter(self):
        sec = self.section(self.doc("--tables", "ACCOUNT"), "Clerk")
        self.assertIn('n1["<b>read</b><br/>account"]', sec)
        self.assertNotIn("prfx_order", sec)
        self.assertIn("prvExportToExcel", sec)  # other privileges are always shown


class TestModel(DocCase):
    def test_invalid_model_is_refused(self):
        (self.model / "roles" / "admin.dv.security.yaml").write_text(
            ADMIN.replace("parent_child", "everyone", 1), encoding="utf-8")
        code, out, err = run("doc", str(self.model))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid", err)

    def test_no_model(self):
        code, _, err = run("doc", str(self.repo / ".git"))
        self.assertEqual(code, 1)
        self.assertIn("_meta.dv.security.yaml", err)

    def test_no_role(self):
        for f in (self.model / "roles").iterdir():
            f.unlink()
        self.assertIn("No security role is described.", self.doc())


class TestOutput(DocCase):
    def test_write_then_check(self):
        target = self.tmp / "docs" / "security-roles.md"
        self.assertEqual(run("doc", str(self.model), "--out", str(target))[0], 0)
        self.assertTrue(target.read_text(encoding="utf-8").startswith(docs.GENERATED))
        self.assertEqual(run("doc", str(self.model), "--out", str(target), "--check")[0], 0)
        f = self.model / "roles" / "admin.dv.security.yaml"
        f.write_text(ADMIN.replace("write: parent_child", "write: user"), encoding="utf-8")
        code, _, err = run("doc", str(self.model), "--out", str(target), "--check")
        self.assertEqual(code, 1)
        self.assertIn("STALE", err)

    def test_check_needs_out(self):
        self.assertEqual(run("doc", str(self.model), "--check")[0], 1)


if __name__ == "__main__":
    if "--regenerate-example" in sys.argv:
        sys.exit(regenerate_example())
    unittest.main()
