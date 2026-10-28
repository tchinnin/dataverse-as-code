"""Offline tests for dv_metadata_docs.py — ERD generation, determinism, CI check.

Run:  python3 -m unittest discover -s <skill>/tests -v
Regenerate the example after an intended output change:
      python3 <skill>/scripts/dv_metadata_docs.py erd <plugin>/skills/dvac-metadata/references/samples \
          --out <skill>/references/examples/data-model.md
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
import dv_metadata_docs as docs  # noqa: E402

SAMPLES = SKILL.parent / "dvac-metadata" / "references" / "samples"
EXAMPLE = SKILL / "references" / "examples" / "data-model.md"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = docs.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            err.write(str(e))
    return code, out.getvalue(), err.getvalue()


class ModelCase(unittest.TestCase):
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

    def erd(self, *opts):
        code, out, err = run("erd", str(self.model), *opts)
        self.assertEqual(code, 0, err)
        return out


class TestErd(ModelCase):
    def test_example_is_up_to_date(self):
        code, _, err = run("erd", str(SAMPLES), "--out", str(EXAMPLE), "--check")
        self.assertEqual(code, 0, err)

    def test_deterministic(self):
        self.assertEqual(self.erd(), self.erd())

    def test_entities_keys_and_types(self):
        out = self.erd("--format", "mermaid")
        self.assertTrue(out.startswith("erDiagram\n"))
        self.assertIn("    prfx_crm_Customer {", out)
        self.assertIn('guid prfx_crm_customerid PK "Customer"', out)
        self.assertIn('autonumber prfx_CustomerNumber UK "Customer Number"', out)
        self.assertIn('lookup prfx_CustomerId FK "Customer · required"', out)
        self.assertIn('fx_decimal prfx_AmountWithTax "Amount Incl. Tax"', out)
        self.assertIn("choice prfx_crm_choice_OrderStatus", out)

    def test_primary_column_first(self):
        out = self.erd("--format", "mermaid")
        order = out.split("prfx_crm_Order {")[1].splitlines()
        self.assertIn("prfx_crm_orderid PK", order[1])
        self.assertIn("prfx_OrderNumber", order[2])

    def test_relationship_cardinality_and_cascade(self):
        out = self.erd()
        self.assertIn('prfx_crm_Customer ||--o{ prfx_crm_Order : "prfx_CustomerId (restrict)"', out)
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml",
                  "    cascade: restrict                # explicit relationship behavior — never implicit\n"
                  "    description: Customer who placed the order.\n    required: true",
                  "    cascade: {delete: removelink, share: all}\n"
                  "    description: Customer who placed the order.")
        out = self.erd()
        self.assertIn('prfx_crm_Customer |o--o{ prfx_crm_Order : "prfx_CustomerId ({delete: removelink, share: all})"',
                      out)

    def test_external_target_uses_logical_name(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "target: prfx_crm_Customer", "target: Account")
        self.assertIn("    account ||--o{ prfx_crm_Order", self.erd())

    def test_quotes_in_labels(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Amount\n", "label: 'The \"net\" amount'\n")
        self.assertIn("\"The 'net' amount\"", self.erd())

    def test_tables_filter_and_no_columns(self):
        out = self.erd("--format", "mermaid", "--tables", "PRFX_CRM_ORDER", "--no-columns")
        self.assertEqual(out, "erDiagram\n    prfx_crm_Order {\n    }\n"
                              '    prfx_crm_Customer ||--o{ prfx_crm_Order : "prfx_CustomerId (restrict)"\n')
        code, _, err = run("erd", str(self.model), "--tables", "prfx_Nope")
        self.assertEqual(code, 1)
        self.assertIn("prfx_nope", err)

    def test_language(self):
        (self.model / "_meta.dv.metadata.yaml").write_text(
            (self.model / "_meta.dv.metadata.yaml").read_text(encoding="utf-8") + "languages: [1033, 1036]\n",
            encoding="utf-8")
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Amount\n",
                  "label: {1033: Amount, 1036: Montant}\n")
        self.assertIn('decimal prfx_Amount "Amount"', self.erd())
        out = self.erd("--lang", "1036")
        self.assertIn('decimal prfx_Amount "Montant"', out)
        self.assertIn('datetime prfx_OrderedOn "Ordered On"', out)  # falls back to the base language

    def test_invalid_model_is_refused(self):
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "target: prfx_crm_Customer", "target: prfx_crm_Nope")
        code, out, err = run("erd", str(self.model))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid", err)


class TestOutput(ModelCase):
    def test_write_then_check(self):
        target = self.tmp / "docs" / "data-model.md"
        code, _, _ = run("erd", str(self.model), "--out", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.read_text(encoding="utf-8").startswith(docs.GENERATED))
        self.assertEqual(run("erd", str(self.model), "--out", str(target), "--check")[0], 0)
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "label: Amount\n", "label: Net Amount\n")
        code, _, err = run("erd", str(self.model), "--out", str(target), "--check")
        self.assertEqual(code, 1)
        self.assertIn("STALE", err)

    def test_check_needs_out_and_file(self):
        self.assertEqual(run("erd", str(self.model), "--check")[0], 1)
        self.assertEqual(run("erd", str(self.model), "--out", str(self.tmp / "missing.md"), "--check")[0], 1)


if __name__ == "__main__":
    unittest.main()
