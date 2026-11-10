"""Offline tests for dv_metadata_solution.py — import and compare between the model and an unpacked solution.

Run:  python3 -m unittest discover -s <skill>/tests -v

tests/fixtures/prfx_crm_Core is a synthetic unpacked solution (SolutionPackager layout) that
describes exactly the dvac-metadata samples, plus a standard table the format cannot hold.
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
import dv_metadata_solution as imp  # noqa: E402

dvm = imp.dvm
SAMPLES = SKILL.parent / "dvac-metadata" / "references" / "samples"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prfx_crm_Core"


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
        self.model = self.tmp / "dataverse" / "metadata"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def with_samples(self):
        shutil.copytree(SAMPLES, self.model)

    def edit(self, rel, old, new, root=None):
        p = (root or self.sol) / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def imp(self, *opts, sols=None):
        return run("import", *[str(s) for s in (sols or [self.sol])], "--model", str(self.model), *opts)

    def cmp(self, *opts):
        return run("compare", str(self.sol), "--model", str(self.model), *opts)

    def snapshot(self):
        return {p.relative_to(self.model).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(self.model.rglob("*")) if p.is_file()}

    def table(self, key):
        return dvm.load_yaml(self.model / "tables" / f"{key}.dv.metadata.yaml")

    def add_attribute(self, table, xml):
        self.edit(f"Entities/{table}/Entity.xml", "      </attributes>", xml + "\n      </attributes>")


def attribute(phys, typ, label, body=""):
    return (f'<attribute PhysicalName="{phys}"><Type>{typ}</Type><Name>{phys.lower()}</Name>'
            f'<LogicalName>{phys.lower()}</LogicalName><RequiredLevel>none</RequiredLevel>'
            f'<DisplayMask>ValidForAdvancedFind|ValidForForm|ValidForGrid</DisplayMask>'
            f'<IsCustomField>1</IsCustomField>{body}<displaynames>'
            f'<displayname description="{label}" languagecode="1033" /></displaynames></attribute>')


class TestRoundTrip(ImportCase):
    def test_samples_already_match(self):
        self.with_samples()
        before = self.snapshot()
        code, out, err = self.imp()
        self.assertEqual(code, 0, out + err)
        self.assertIn("already matches", out)
        self.assertNotIn("~ ", out)
        self.assertEqual(self.snapshot(), before)

    def test_import_into_empty_model(self):
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("STATUS: changes-pending", out)
        self.assertFalse(self.model.exists(), "nothing is written without --write")
        for name in ("_meta.dv.metadata.yaml", "choices.dv.metadata.yaml",
                     "tables/prfx_crm_Customer.dv.metadata.yaml", "tables/prfx_crm_Order.dv.metadata.yaml"):
            self.assertIn(f"+ ", out)
            self.assertIn(name, out)
        code, out, err = self.imp("--write")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(dvm.validate_model(self.model).errors, [])
        meta = dvm.load_yaml(self.model / "_meta.dv.metadata.yaml")
        self.assertEqual(meta, {"version": "1.0", "publisher": {"prefix": "prfx", "solution": "prfx_crm_Core"},
                                "locale": "en"})
        order = self.table("prfx_crm_Order")
        self.assertEqual(list(order["columns"])[0], "prfx_OrderNumber")
        self.assertEqual(order["columns"]["prfx_AmountWithTax"]["formula"], "If(prfx_amount > 0, prfx_amount * 1.2)")
        self.assertEqual(order["columns"]["prfx_CustomerId"]["cascade"], "restrict")
        self.assertEqual(order["columns"]["prfx_OrderedOn"], {"label": "Ordered On", "type": "datetime"})
        customer = self.table("prfx_crm_Customer")
        self.assertEqual(customer["columns"]["prfx_FoundedOn"], {"label": "Founded On", "type": "date"})
        self.assertEqual(customer["columns"]["prfx_IsActive"]["default"], True)
        self.assertNotIn("true_label", customer["columns"]["prfx_IsActive"])
        self.assertEqual(customer["keys"][0]["columns"], ["prfx_customernumber"])
        # Idempotent.
        before = self.snapshot()
        self.assertEqual(self.imp()[0], 0)
        self.assertEqual(self.snapshot(), before)

    def test_new_file_order_and_style(self):
        self.imp("--write")
        text = (self.model / "tables" / "prfx_crm_Customer.dv.metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("keys:\n  - schema_name: prfx_crm_customer_customernumber_key\n"
                      "    columns: [prfx_customernumber]\n", text)


class TestAlign(ImportCase):
    def test_changed_file_shows_diff_and_keeps_others(self):
        self.with_samples()
        self.edit("Entities/prfx_crm_Order/Entity.xml", 'description="Amount" languagecode', 'description="Net Amount" languagecode')
        customer_before = (self.model / "tables" / "prfx_crm_Customer.dv.metadata.yaml").read_text(encoding="utf-8")
        code, out, _ = self.imp()
        self.assertEqual(code, 0)
        self.assertIn("STATUS: changes-pending", out)
        self.assertIn("~ tables/prfx_crm_Order.dv.metadata.yaml", out)
        self.assertIn("-    label: Amount", out)
        self.assertIn("+    label: Net Amount", out)
        self.assertEqual(self.imp("--write")[0], 0)
        self.assertEqual(self.table("prfx_crm_Order")["columns"]["prfx_Amount"]["label"], "Net Amount")
        # An unchanged file is not rewritten: its comments survive.
        self.assertEqual((self.model / "tables" / "prfx_crm_Customer.dv.metadata.yaml").read_text(encoding="utf-8"),
                         customer_before)
        # The rewritten file keeps its header.
        self.assertTrue((self.model / "tables" / "prfx_crm_Order.dv.metadata.yaml").read_text(encoding="utf-8")
                        .startswith("# yaml-language-server: $schema=../../schemas/table.schema.json\nkey:"))

    def test_model_only_entries_are_kept(self):
        self.with_samples()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "  prfx_Attachment:",
                  "  prfx_Extra:\n    label: Extra\n    type: text\n\n  prfx_Attachment:", root=self.model)
        self.edit("choices.dv.metadata.yaml", "      - {value: 100000003, label: Cancelled, color: \"#E81123\"}",
                  "      - {value: 100000003, label: Cancelled, color: \"#E81123\"}\n"
                  "      - {value: 100000004, label: Archived}", root=self.model)
        code, out, _ = self.imp()
        self.assertEqual(code, 0, out)
        self.assertIn("table prfx_crm_Order: column prfx_Extra (not in the solution)", out)
        self.assertIn("choice prfx_crm_choice_OrderStatus: option 100000004 (not in the solution)", out)

    def test_existing_column_keeps_its_unmanaged_fields(self):
        """max_length is in the solution but left out of the sample: it stays unmanaged."""
        self.with_samples()
        self.imp("--write")
        self.assertNotIn("max_length", self.table("prfx_crm_Order")["columns"]["prfx_OrderNumber"])

    def test_new_column_in_existing_table(self):
        self.with_samples()
        self.add_attribute("prfx_crm_Order", attribute("prfx_Reference", "nvarchar", "Reference",
                                                       "<Format>text</Format><MaxLength>40</MaxLength>"))
        self.assertEqual(self.imp("--write")[0], 0)
        cols = self.table("prfx_crm_Order")["columns"]
        self.assertEqual(list(cols)[-1], "prfx_Reference")
        self.assertEqual(cols["prfx_Reference"], {"label": "Reference", "type": "text", "max_length": 40})

    def test_tables_filter(self):
        code, out, _ = self.imp("--tables", "prfx_crm_customer")
        self.assertIn("prfx_crm_Customer.dv.metadata.yaml", out)
        self.assertNotIn("prfx_crm_Order.dv.metadata.yaml", out)
        self.assertNotIn("choices.dv.metadata.yaml", out)


class TestSkipped(ImportCase):
    def test_components_the_format_cannot_hold(self):
        self.with_samples()
        self.add_attribute("prfx_crm_Order", attribute("prfx_Memo", "nvarchar", "Memo", "<Format>textarea</Format>"))
        self.add_attribute("prfx_crm_Order", attribute("prfx_Local", "picklist", "Local",
                                                       '<optionset Name="prfx_local"><OptionSetType>picklist'
                                                       '</OptionSetType></optionset>'))
        self.add_attribute("prfx_crm_Order", attribute("prfx_Total", "decimal", "Total",
                                                       "<SourceType>2</SourceType>"))
        code, out, _ = self.imp()
        self.assertEqual(code, 0, out)
        self.assertIn("prfx_crm_Order.prfx_Memo: text column with format 'textarea'", out)
        self.assertIn("prfx_crm_Order.prfx_Local: local choice", out)
        self.assertIn("prfx_crm_Order.prfx_Total: rollup column", out)
        self.assertIn("table Account: not prefixed with the publisher prefix", out)

    def test_polymorphic_lookup(self):
        self.edit("Other/Relationships/prfx_crm_Customer.xml", "</EntityRelationships>",
                  '<EntityRelationship Name="prfx_crm_Order_prfx_CustomerId_account">'
                  "<EntityRelationshipType>OneToMany</EntityRelationshipType>"
                  "<ReferencingEntityName>prfx_crm_Order</ReferencingEntityName>"
                  "<ReferencedEntityName>Account</ReferencedEntityName>"
                  "<ReferencingAttributeName>prfx_CustomerId</ReferencingAttributeName>"
                  "</EntityRelationship></EntityRelationships>")
        _, out, _ = self.imp()
        self.assertIn("prfx_crm_Order.prfx_CustomerId: lookup with 2 targets (polymorphic)", out)


class TestErrors(ImportCase):
    def test_invalid_result_writes_nothing(self):
        self.edit("Other/Relationships/prfx_crm_Customer.xml",
                  "<ReferencedEntityName>prfx_crm_Customer</ReferencedEntityName>",
                  "<ReferencedEntityName>prfx_crm_Missing</ReferencedEntityName>")
        code, _, err = self.imp("--write")
        self.assertEqual(code, 1)
        self.assertIn("is not a table of the model", err)
        self.assertFalse(self.model.exists())

    def test_not_an_unpacked_solution(self):
        code, out, _ = run("import", str(self.tmp), "--model", str(self.model))
        self.assertEqual(code, 1)
        self.assertIn("STATUS: error", out)
        self.assertIn("not an unpacked solution", out)

    def test_two_solutions_that_disagree(self):
        other = self.tmp / "other"
        shutil.copytree(FIXTURE, other)
        self.edit("OptionSets/prfx_crm_choice_OrderStatus.xml", 'description="Draft"', 'description="New"', root=other)
        code, out, _ = self.imp(sols=[self.sol, other])
        self.assertEqual(code, 1)
        self.assertIn("choice prfx_crm_choice_OrderStatus", out)

    def test_two_solutions_add_columns(self):
        other = self.tmp / "other"
        shutil.copytree(FIXTURE, other)
        self.add_attribute("prfx_crm_Order", attribute("prfx_Reference", "nvarchar", "Reference",
                                                       "<Format>text</Format><MaxLength>40</MaxLength>"))
        self.with_samples()
        code, out, err = self.imp("--write", sols=[other, self.sol])
        self.assertEqual(code, 0, out + err)
        self.assertIn("prfx_Reference", self.table("prfx_crm_Order")["columns"])


class TestLanguages(ImportCase):
    def test_labels_become_maps_with_several_languages(self):
        self.edit("Other/Customizations.xml", "<Language>1033</Language>",
                  "<Language>1033</Language><Language>1036</Language>")
        self.edit("Entities/prfx_crm_Order/Entity.xml", '<LocalizedName description="Order" languagecode="1033" />',
                  '<LocalizedName description="Order" languagecode="1033" />'
                  '<LocalizedName description="Commande" languagecode="1036" />')
        self.assertEqual(self.imp("--write")[0], 0)
        self.assertEqual(dvm.load_yaml(self.model / "_meta.dv.metadata.yaml")["languages"], [1033, 1036])
        order = self.table("prfx_crm_Order")
        self.assertEqual(order["label"], {1033: "Order", 1036: "Commande"})
        self.assertEqual(order["columns"]["prfx_Amount"]["label"], {1033: "Amount"})


class TestCompare(ImportCase):
    def test_samples_are_in_sync(self):
        self.with_samples()
        before = self.snapshot()
        code, out, err = self.cmp("--detailed-exitcode")
        self.assertEqual(code, 0, out + err)
        self.assertIn("STATUS: in-sync", out)
        self.assertNotIn("NEXT", out)
        # Values the solution holds that the sample leaves unmanaged are informational.
        self.assertIn("· prfx_crm_Order.prfx_OrderNumber.max_length = 100", out)
        self.assertIn("! table Account: not prefixed with the publisher prefix", out)
        self.assertEqual(self.snapshot(), before, "compare writes nothing")

    def test_differences_by_section(self):
        self.with_samples()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    label: Amount\n", "    label: Net Amount\n",
                  root=self.model)
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "  prfx_Attachment:",
                  "  prfx_Extra:\n    label: Extra\n    type: text\n\n  prfx_Attachment:", root=self.model)
        self.add_attribute("prfx_crm_Order", attribute("prfx_Reference", "nvarchar", "Reference",
                                                       "<Format>text</Format><MaxLength>40</MaxLength>"))
        code, out, _ = self.cmp()
        self.assertEqual(code, 0, out)
        self.assertIn("+ column prfx_crm_Order.prfx_Reference (text)", out)
        self.assertIn("- column prfx_crm_Order.prfx_Extra", out)
        self.assertIn("~ prfx_crm_Order.prfx_Amount.label: model='Net Amount'  solution='Amount'", out)
        self.assertIn("STATUS: drift", out)
        self.assertIn("REASON: 3 difference(s)", out)
        self.assertIn("never edit the unpacked solution", out)
        self.assertRegex(out, r"NEXT \(only after the user confirms\): .*dv_metadata_solution\.py import .*--model ")
        self.assertNotIn("--write", out)
        self.assertEqual(self.cmp("--detailed-exitcode")[0], 2)

    def test_table_only_on_one_side(self):
        self.with_samples()
        (self.model / "tables" / "prfx_crm_Customer.dv.metadata.yaml").unlink()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "    target: prfx_crm_Customer", "    target: account",
                  root=self.model)
        _, out, _ = self.cmp()
        self.assertIn("+ table prfx_crm_customer", out)
        self.assertIn("~ prfx_crm_Order.prfx_CustomerId.target: model='account'  solution='prfx_crm_customer'", out)

    def test_value_the_model_declares_differently(self):
        self.with_samples()
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", '    format: "ORD-{SEQNUM:6}"\n',
                  '    format: "ORD-{SEQNUM:6}"\n    max_length: 50\n',
                  root=self.model)
        _, out, _ = self.cmp()
        self.assertIn("~ prfx_crm_Order.prfx_OrderNumber.max_length: model=50  solution=100", out)
        self.assertNotIn("· prfx_crm_Order.prfx_OrderNumber.max_length", out)

    def test_choice_option(self):
        self.with_samples()
        self.edit("OptionSets/prfx_crm_choice_OrderStatus.xml", 'description="Draft"', 'description="New"')
        _, out, _ = self.cmp()
        self.assertIn("~ prfx_crm_choice_OrderStatus:100000000.label: model='Draft'  solution='New'", out)

    def test_tables_filter(self):
        self.with_samples()
        self.edit("OptionSets/prfx_crm_choice_OrderStatus.xml", 'description="Draft"', 'description="New"')
        _, out, _ = self.cmp("--tables", "prfx_crm_Customer")
        self.assertIn("STATUS: in-sync", out)

    def test_no_model(self):
        code, out, _ = self.cmp()
        self.assertEqual(code, 1)
        self.assertIn("STATUS: error", out)
        self.assertIn("no model to compare", out)


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
        self.edit("tables/prfx_crm_Order.dv.metadata.yaml", "  prfx_Attachment:",
                  "  prfx_Extra:\n    label: Extra\n    type: text\n\n  prfx_Attachment:", root=self.model)
        self.cmp()
        self.imp("--write")
        self.assertEqual(tree_digest(self.sol), before)

    def test_model_inside_the_solution_is_refused(self):
        before = tree_digest(self.sol)
        for model in (self.sol, self.sol / "Other" / "model"):
            code, out, _ = run("import", str(self.sol), "--model", str(model), "--write")
            self.assertEqual(code, 1)
            self.assertIn("STATUS: refused", out)
            self.assertIn("read only", out)
        self.assertEqual(tree_digest(self.sol), before)


if __name__ == "__main__":
    unittest.main()
