"""Offline tests for dv_plugin_docs.py — pipeline order, Custom APIs, determinism, CI check.

Run:  python3 -m unittest discover -s <skill>/tests -v
Regenerate the example after an intended output change:
      python3 <skill>/tests/test_dv_plugin_docs.py --regenerate-example
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
import dv_plugin_docs as docs  # noqa: E402

SAMPLES = SKILL.parent / "dvac-plugins" / "references" / "samples"
EXAMPLE = SKILL / "references" / "examples" / "plugins.md"

UNIT = """version: "1.0"
publisher: {prefix: prfx, solution: prfx_Core}
package: {name: prfx_X, nupkg: bin/*.nupkg}
steps:
  - {plugin: X.Late, message: Update, table: prfx_t, stage: pre-operation, rank: 5}
  - {plugin: X.B, message: Update, table: prfx_t, stage: pre-operation, rank: 2}
  - {plugin: X.A, message: Update, table: prfx_t, stage: pre-operation, rank: 2, description: 'a | b'}
  - {plugin: X.Post, message: Update, table: prfx_t, stage: post-operation, enabled: false}
  - {plugin: X.Queue2, message: Update, table: prfx_t, stage: post-operation, mode: async}
  - {plugin: X.Queue1, message: Update, table: prfx_t, stage: post-operation, mode: async}
  - {plugin: X.Validate, message: Update, table: prfx_t, stage: pre-validation, rank: 9}
  - {plugin: X.OnCreate, message: Create, table: prfx_other, stage: post-operation}
  - {plugin: X.AfterApi, message: prfx_DoIt, stage: post-operation, mode: async}
custom_apis:
  prfx_DoIt:
    plugin: X.DoItApi
    display_name: 'Do "it"'
    binding: global
    is_function: true
    allowed_steps: sync-and-async
    request: {In: {type: string, description: 'x | y'}}
    response: {Out: {type: entity-collection, table: account}}
  prfx_Bound:
    display_name: Bound
    binding: entity-collection
    bound_table: prfx_t
"""


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
    shutil.copytree(SAMPLES, repo)
    (repo / ".git").mkdir()
    return repo


def regenerate_example():
    tmp = Path(tempfile.mkdtemp())
    try:
        return docs.main(["doc", str(samples_repo(tmp)), "--out", str(EXAMPLE)])
    finally:
        shutil.rmtree(tmp)


class DocCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "plugins" / "X").mkdir(parents=True)
        (self.repo / "plugins" / "X" / "X.dv.plugin.yaml").write_text(UNIT, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def doc(self, *opts, path=None):
        code, out, err = run("doc", str(path or self.repo), *opts)
        self.assertEqual(code, 0, err)
        return out

    def section(self, out, title):
        return out.split(f"### {title}\n", 1)[1].split("\n### ", 1)[0]


class TestPipeline(DocCase):
    def test_example_is_up_to_date(self):
        repo = samples_repo(self.tmp)
        code, _, err = run("doc", str(repo), "--out", str(EXAMPLE), "--check")
        self.assertEqual(code, 0, err + " — regenerate with: python3 tests/test_dv_plugin_docs.py --regenerate-example")

    def test_deterministic(self):
        self.assertEqual(self.doc(), self.doc())

    def test_groups_sorted_by_table_then_message(self):
        out = self.doc()
        titles = [l[4:] for l in out.splitlines() if l.startswith("### ")]
        self.assertEqual(titles[:3], ["prfx_DoIt · (any table)", "Create · prfx_other", "Update · prfx_t"])

    def test_execution_order(self):
        sec = self.section(self.doc(), "Update · prfx_t")
        rows = [l for l in sec.splitlines() if l.startswith("| ") and l[2].isdigit()]
        plugins = [r.split(" | ")[5] for r in rows]
        self.assertEqual(plugins, ["`X.Validate`", "`X.A`", "`X.B`", "`X.Late`", "`X.Post`",
                                   "`X.Queue1`", "`X.Queue2`"])

    def test_diagram_links(self):
        sec = self.section(self.doc(), "Update · prfx_t")
        self.assertIn("s2 -.- s3", sec)       # same rank: no guaranteed order
        self.assertIn("s3 --> s4", sec)       # higher rank: runs after
        self.assertNotIn("s6 --> s7", sec)    # async: no order
        self.assertNotIn("s6 -.- s7", sec)
        self.assertIn("stage_pre_validation --> stage_pre_operation --> core --> stage_post_operation "
                      "--> stage_async", sec)
        self.assertIn("class s5 disabled", sec)
        self.assertIn("Dataverse does not guarantee the order of: `X.A: Update of prfx_t`, "
                      "`X.B: Update of prfx_t`", sec)
        self.assertIn("a \\| b", sec)

    def test_custom_api_main_operation(self):
        sec = self.section(self.doc(), "prfx_DoIt · (any table)")
        self.assertIn('core(["Main operation: DoItApi"])', sec)
        self.assertIn("core --> stage_async", sec)
        self.assertIn('core(["Main operation"])', self.section(self.doc(), "Create · prfx_other"))

    def test_tables_filter(self):
        out = self.doc("--tables", "PRFX_OTHER")
        titles = [l[4:] for l in out.splitlines() if l.startswith("### ")]
        self.assertEqual(titles, ["Create · prfx_other"])
        self.assertIn("No Custom API is described.", out)

    def test_invalid_model_is_refused(self):
        f = self.repo / "plugins" / "X" / "X.dv.plugin.yaml"
        f.write_text(UNIT.replace("stage: pre-validation", "stage: pre-validation, mode: async"), encoding="utf-8")
        code, out, err = run("doc", str(self.repo))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid", err)


class TestCustomApis(DocCase):
    def test_overview_diagram(self):
        out = self.doc()
        diagram = out.split("## Custom APIs\n", 1)[1].split("```mermaid\n", 1)[1].split("```", 1)[0]
        self.assertEqual(diagram, 'flowchart LR\n    a1{{"prfx_Bound"}}\n'
                                  '    t1[("prfx_t")] -- collection --> a1\n'
                                  '    a2{{"prfx_DoIt"}}\n    a2 --> c1["X.DoItApi"]\n')

    def test_signature(self):
        sec = self.section(self.doc(), "prfx_DoIt")
        self.assertIn('| Display name | Do "it" |', sec)
        self.assertIn("| Kind | function |", sec)
        self.assertIn("| Binding | global |", sec)
        self.assertIn("| `In` | string | — | no | — | x \\| y |", sec)
        self.assertIn("| `Out` | entity-collection | account | — | — |", sec)

    def test_source_paths_are_relative(self):
        out = self.doc(path=self.repo / "plugins")
        self.assertIn("| `plugins/X/X.dv.plugin.yaml` | package | prfx_X | 9 | 2 |", out)
        self.assertNotIn(str(self.tmp), out)


class TestOutput(DocCase):
    def test_write_then_check(self):
        target = self.tmp / "docs" / "plugins.md"
        self.assertEqual(run("doc", str(self.repo), "--out", str(target))[0], 0)
        self.assertTrue(target.read_text(encoding="utf-8").startswith(docs.GENERATED))
        self.assertEqual(run("doc", str(self.repo), "--out", str(target), "--check")[0], 0)
        f = self.repo / "plugins" / "X" / "X.dv.plugin.yaml"
        f.write_text(UNIT.replace("rank: 5", "rank: 6"), encoding="utf-8")
        code, _, err = run("doc", str(self.repo), "--out", str(target), "--check")
        self.assertEqual(code, 1)
        self.assertIn("STALE", err)

    def test_check_needs_out(self):
        self.assertEqual(run("doc", str(self.repo), "--check")[0], 1)


if __name__ == "__main__":
    if "--regenerate-example" in sys.argv:
        sys.exit(regenerate_example())
    unittest.main()
