"""The CRD field generator: how it reads a Go API package, and parity with the CRD YAML.

``scripts/gen_crd_docs.go`` turns the operator's API types into the model
``generate_docs.py`` renders as the three field reference pages. The first
half drives it over a fixture package and holds it to the rules the pages
rely on: inline embeds flatten, ``json:"-"`` drops, pointers are transparent,
slices and maps carry their element, a named scalar resolves, markers give
required, default and constraints, a type from another package is a leaf, and
a description is the first paragraph without citations plus a deprecation
notice. The second half runs it over the committed package and checks every
path, ``required`` list, type, constraint and default against the committed
CRD schemas in both directions, so a page can neither list a field the API
server does not know nor miss one it does, nor describe one differently.

Run:
  python3 -m unittest discover -s scripts -p 'test_gen_crd_docs.py' -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

import generate_docs

CRD_DIR = generate_docs.REPO / "k8s-operator/config/crd/bases"
CRD_FILES = {
    "PlatformAgent": CRD_DIR / "kubeagents.x-k8s.io_platformagents.yaml",
    "AgentProfile": CRD_DIR / "kubeagents.x-k8s.io_agentprofiles.yaml",
    "AgentPlugin": CRD_DIR / "kubeagents.x-k8s.io_agentplugins.yaml",
}

FIXTURE = textwrap.dedent(
    '''\
    package v1alpha1

    import (
    	corev1 "k8s.io/api/core/v1"
    	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    )

    // Mode is how the widget runs; its CEL rule stays off the page wherever
    // the type is used.
    // +kubebuilder:validation:Enum=fast;slow
    // +kubebuilder:validation:XValidation:rule="self != 'root'",message="root is reserved"
    type Mode string

    // Speed carries a default and an enum of its own, which a field overrides.
    // +kubebuilder:validation:Enum=a;b
    // +kubebuilder:default:="a"
    type Speed string

    // WidgetCommon is embedded inline into the spec.
    type WidgetCommon struct {
    	// Owner names who holds the widget.
    	// +optional
    	Owner string `json:"owner,omitempty"`
    }

    // Part is one component of a widget.
    type Part struct {
    	// Name is the part's name.
    	Name string `json:"name"`
    	// Weight is the part's weight in grams.
    	// +kubebuilder:validation:Minimum=0
    	// +optional
    	Weight *int64 `json:"weight,omitempty"`
    }

    // WidgetSpec defines the desired state of Widget.
    type WidgetSpec struct {
    	WidgetCommon `json:",inline"`

    	// Hidden is not part of the API.
    	Hidden string `json:"-"`

    	// Name is the widget's name (#123, "Naming"). See gke-labs#45 for the
    	// history; the rule is in #678.
    	//
    	// This second paragraph is an implementation note and stays off the page.
    	Name string `json:"name"`

    	// Alias is the widget's name in §2 of the spec — its optional spelling.
    	// +optional
    	Alias string `json:"alias"`

    	// Replicas is how many widgets run.
    	// +kubebuilder:validation:Minimum=1
    	// +kubebuilder:validation:Maximum=10
    	// +kubebuilder:default=3
    	// +optional
    	Replicas *int32 `json:"replicas,omitempty"`

    	// Mode selects the speed.
    	// +kubebuilder:default:="fast"
    	// +optional
    	Mode Mode `json:"mode,omitempty"`

    	// Tags label the widget; the hand-written items rule stays off the page.
    	// +kubebuilder:validation:MaxItems=4
    	// +kubebuilder:validation:items:Pattern=`^[a-z]+$`
    	// +kubebuilder:validation:items:XValidation:rule="self != 'none'",message="none is reserved"
    	// +optional
    	Tags []string `json:"tags,omitempty"`

    	// Parts are the widget's components.
    	// +optional
    	Parts []Part `json:"parts,omitempty"`

    	// Labels are free-form labels.
    	// +optional
    	Labels map[string]string `json:"labels,omitempty"`

    	// Spares are parts by slot.
    	// +optional
    	Spares map[string]Part `json:"spares,omitempty"`

    	// Primary is the main part, required although the tag says omitempty.
    	// +required
    	Primary *Part `json:"primary,omitempty"`

    	// Env is passed to the widget's container.
    	// +optional
    	Env []corev1.EnvVar `json:"env,omitempty"`

    	// Resources bound the widget's container.
    	// +optional
    	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`

    	// Pull says whether to pull the image.
    	// +kubebuilder:validation:Enum=Always;Never
    	// +optional
    	Pull *corev1.PullPolicy `json:"pull,omitempty"`

    	// Kind introduces the example below it:
    	//
    	//   kind: Widget
    	// +optional
    	Kind string `json:"kind,omitempty"`

    	// Legacy is the old way to name the widget.
    	//
    	// This paragraph is an implementation note.
    	//
    	// Deprecated: use Name instead. It will be removed in a future version.
    	//
    	// And this one is another note.
    	// +optional
    	Legacy string `json:"legacy,omitempty"`

    	// Levels are modes, so the type's enum applies to each item.
    	// +kubebuilder:validation:MaxItems=2
    	// +optional
    	Levels []Mode `json:"levels,omitempty"`

    	// Ranks are modes by slot, so the type's enum applies to each value.
    	// +kubebuilder:validation:MaxProperties=3
    	// +optional
    	Ranks map[string]*Mode `json:"ranks,omitempty"`

    	// Speed overrides its type's default and enum, the enum in the colon spelling.
    	// +kubebuilder:default:="c"
    	// +kubebuilder:validation:Enum:=b;c
    	// +optional
    	Speed Speed `json:"speed,omitempty"`

    	// Since is when the widget started, with a format the schema also
    	// writes for integer widths without any marker.
    	// +kubebuilder:validation:Format=date-time
    	// +optional
    	Since string `json:"since,omitempty"`

    	// Step is the widget's increment.
    	// +kubebuilder:validation:MultipleOf=5
    	// +optional
    	Step *int32 `json:"step,omitempty"`

    	// Notes are bounded in number.
    	// +kubebuilder:validation:MinProperties=1
    	// +kubebuilder:validation:MaxProperties=8
    	// +optional
    	Notes map[string]string `json:"notes,omitempty"`

    	// Listen is the address the widget binds; its unquoted default opens
    	// with a colon.
    	// +kubebuilder:default=:8080
    	// +optional
    	Listen string `json:"listen,omitempty"`

    	// Slug is bounded by a pattern written in double quotes.
    	// +kubebuilder:validation:Pattern="^[a-z]+\\\\.[a-z]+$"
    	// +optional
    	Slug string `json:"slug,omitempty"`

    	Extra Part `json:"extra"`
    }

    // WidgetStatus defines the observed state of Widget.
    type WidgetStatus struct {
    	// Conditions are the widget's conditions.
    	// +optional
    	Conditions []metav1.Condition `json:"conditions,omitempty"`
    	// LastSeen is when the widget was last observed.
    	// +optional
    	LastSeen *metav1.Time `json:"lastSeen,omitempty"`
    }

    // Widget is the Schema for the widgets API.
    type Widget struct {
    	metav1.TypeMeta   `json:",inline"`
    	metav1.ObjectMeta `json:"metadata,omitempty"`

    	Spec   WidgetSpec   `json:"spec,omitempty"`
    	Status WidgetStatus `json:"status,omitempty"`
    }
    '''
)

# A test file and a generated file each redeclare WidgetSpec. Were either
# parsed, its declaration would replace the real one (files are read in name
# order, and both sort after widget_types.go) and `wrong` would be a row.
DECOY = textwrap.dedent(
    """\
    package v1alpha1

    type WidgetSpec struct {
    	Wrong string `json:"wrong"`
    }
    """
)

INLINE_EXTERNAL = textwrap.dedent(
    """\
    package v1alpha1

    import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

    type GadgetSpec struct {
    	metav1.ObjectMeta `json:",inline"`
    }

    type Gadget struct {
    	Spec GadgetSpec `json:"spec,omitempty"`
    }
    """
)


def write_fixture(root: Path) -> None:
    (root / "widget_types.go").write_text(FIXTURE, encoding="utf-8")
    (root / "widget_types_test.go").write_text(DECOY, encoding="utf-8")
    (root / "zz_generated.deepcopy.go").write_text(DECOY, encoding="utf-8")


# The child interpreter the locale test runs: UTF-8 mode off and the C
# locale left uncoerced, so its preferred encoding is ASCII, the codec a
# subprocess read without an explicit encoding falls back to.
NON_UTF8_LOCALE_ENV = {"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
NON_UTF8_LOCALE_CHILD = textwrap.dedent(
    """\
    import json, sys
    from pathlib import Path
    import generate_docs
    model = generate_docs.run_crd_generator(Path(sys.argv[1]), ("Widget",))
    rows = {field["path"]: field["description"] for field in model["kinds"][0]["spec"]}
    sys.stdout.buffer.write(json.dumps(rows["alias"]).encode("utf-8"))
    """
)


@unittest.skipIf(shutil.which(generate_docs.GO_BINARY) is None, "needs a Go toolchain")
class FixturePackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        write_fixture(root)
        model = generate_docs.run_crd_generator(root, ("Widget",))
        (cls.kind,) = model["kinds"]
        cls.spec = {field["path"]: field for field in cls.kind["spec"]}
        cls.status = {field["path"]: field for field in cls.kind["status"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_two_trees_are_the_kind_s_spec_and_status(self):
        self.assertEqual(self.kind["kind"], "Widget")
        self.assertEqual(set(self.status), {"conditions", "lastSeen"})
        self.assertNotIn("metadata", self.spec)
        self.assertNotIn("apiVersion", self.spec)

    def test_an_inline_embed_flattens_into_its_parent(self):
        self.assertIn("owner", self.spec)
        self.assertEqual(self.spec["owner"]["description"], "Owner names who holds the widget.")
        self.assertNotIn("widgetCommon", self.spec)

    def test_a_dash_tag_drops_the_field(self):
        self.assertNotIn("hidden", self.spec)
        self.assertFalse(any(path.startswith("-") for path in self.spec))

    def test_test_and_generated_files_are_not_read(self):
        self.assertNotIn("wrong", self.spec)

    def test_required_follows_controller_gen(self):
        # No marker and no omitempty: required.
        self.assertTrue(self.spec["name"]["required"])
        # +optional wins over a tag without omitempty.
        self.assertFalse(self.spec["alias"]["required"])
        # omitempty alone: optional.
        self.assertFalse(self.spec["replicas"]["required"])
        # +required wins over omitempty.
        self.assertTrue(self.spec["primary"]["required"])
        # A nested struct's fields follow the same rule.
        self.assertTrue(self.spec["parts[].name"]["required"])
        self.assertFalse(self.spec["parts[].weight"]["required"])

    def test_pointers_scalars_and_named_types_resolve(self):
        self.assertEqual(self.spec["replicas"]["type"], "integer")
        self.assertEqual(self.spec["replicas"]["kind"], "scalar")
        self.assertEqual(self.spec["name"]["type"], "string")
        self.assertEqual(self.spec["mode"]["type"], "string")
        # The named type's enum reaches the field that uses it.
        self.assertEqual(
            self.spec["mode"]["constraints"], [{"name": "Enum", "value": "fast;slow"}]
        )

    def test_slices_and_maps_carry_their_element(self):
        self.assertEqual(self.spec["tags"]["type"], "[]string")
        self.assertEqual(self.spec["tags"]["kind"], "list")
        self.assertEqual(self.spec["parts"]["type"], "[]object")
        self.assertIn("parts[].name", self.spec)
        self.assertIn("parts[].weight", self.spec)
        self.assertEqual(self.spec["labels"]["type"], "map[string]string")
        self.assertEqual(self.spec["labels"]["kind"], "map")
        self.assertEqual(self.spec["spares"]["type"], "map[string]object")
        self.assertIn("spares{}.name", self.spec)
        self.assertEqual(self.spec["primary"]["type"], "object")
        self.assertIn("primary.name", self.spec)

    def test_defaults_and_constraints_come_from_the_markers(self):
        self.assertEqual(self.spec["replicas"]["default"], "3")
        self.assertEqual(
            self.spec["replicas"]["constraints"],
            [{"name": "Minimum", "value": "1"}, {"name": "Maximum", "value": "10"}],
        )
        # The `default:=` spelling, unquoted.
        self.assertEqual(self.spec["mode"]["default"], "fast")
        self.assertEqual(
            self.spec["tags"]["constraints"],
            [{"name": "MaxItems", "value": "4"}, {"name": "items:Pattern", "value": "^[a-z]+$"}],
        )
        self.assertEqual(self.spec["name"]["default"], "")
        self.assertEqual(self.spec["name"]["constraints"], [])

    def test_a_default_keeps_a_leading_colon_and_a_quoted_pattern_loses_its_quotes(self):
        # `default=` and `default:=` differ by one prefix character, so the
        # colon that opens an unquoted value is not part of the spelling.
        self.assertEqual(self.spec["listen"]["default"], ":8080")
        # A pattern in double quotes is read the way controller-gen reads it,
        # escapes included, so the row shows the regular expression itself.
        self.assertEqual(self.spec["slug"]["constraints"], [{"name": "Pattern", "value": r"^[a-z]+\.[a-z]+$"}])

    def test_every_marker_the_generator_emits_is_one_the_page_and_the_parity_test_name(self):
        # The page labels a marker and the parity test maps it to its schema
        # keyword through the one table; a marker outside it would render as
        # its bare name and fail the parity test, so the fixture carries the
        # less common ones too.
        self.assertEqual(self.spec["since"]["constraints"], [{"name": "Format", "value": "date-time"}])
        self.assertEqual(self.spec["step"]["constraints"], [{"name": "MultipleOf", "value": "5"}])
        self.assertEqual(
            self.spec["notes"]["constraints"],
            [{"name": "MinProperties", "value": "1"}, {"name": "MaxProperties", "value": "8"}],
        )
        emitted = {
            constraint["name"].rsplit(":", 1)[-1]
            for tree in (self.spec, self.status)
            for row in tree.values()
            for constraint in row["constraints"]
        }
        self.assertEqual(sorted(emitted - set(generate_docs.CONSTRAINT_MARKERS)), [])
        prefixes = {
            constraint["name"].rsplit(":", 1)[0] + ":"
            for tree in (self.spec, self.status)
            for row in tree.values()
            for constraint in row["constraints"]
            if ":" in constraint["name"]
        }
        self.assertEqual(sorted(prefixes - set(generate_docs.CONSTRAINT_PREFIXES)), [])
        self.assertLessEqual(generate_docs.SCHEMA_IMPLIED_MARKERS, set(generate_docs.CONSTRAINT_MARKERS))

    def test_external_types_are_leaves_named_by_their_go_type(self):
        for path, type_name, kind in (
            ("env", "[]corev1.EnvVar", "list"),
            ("resources", "corev1.ResourceRequirements", "external"),
            ("pull", "corev1.PullPolicy", "external"),
        ):
            self.assertEqual(self.spec[path]["type"], type_name, path)
            self.assertTrue(self.spec[path]["external"], path)
            self.assertEqual(self.spec[path]["kind"], kind, path)
        self.assertFalse(any(path.startswith("env[]") for path in self.spec))
        self.assertFalse(any(path.startswith("resources.") for path in self.spec))
        self.assertEqual(self.status["conditions"]["type"], "[]metav1.Condition")
        self.assertEqual(self.status["lastSeen"]["type"], "metav1.Time")
        # Markers on an external-typed field still count.
        self.assertEqual(self.spec["pull"]["constraints"], [{"name": "Enum", "value": "Always;Never"}])

    def test_the_description_is_the_first_paragraph_without_citations(self):
        # A parenthetical citation is cut from its sentence; a sentence that
        # cites an issue bare is dropped whole rather than left as "See for
        # the history".
        self.assertEqual(self.spec["name"]["description"], "Name is the widget's name.")
        self.assertNotIn("implementation note", self.spec["name"]["description"])
        self.assertEqual(self.spec["kind"]["description"], "Kind introduces the example below it.")

    def test_a_deprecation_paragraph_is_kept_and_other_later_paragraphs_are_not(self):
        self.assertEqual(
            self.spec["legacy"]["description"],
            "Legacy is the old way to name the widget. "
            "Deprecated: use Name instead. It will be removed in a future version.",
        )

    def test_a_struct_field_without_a_comment_takes_its_type_s(self):
        self.assertEqual(self.spec["extra"]["description"], "Part is one component of a widget.")
        self.assertEqual(self.spec["extra"]["type"], "object")

    def test_a_field_s_markers_win_over_its_type_s(self):
        self.assertEqual(self.spec["speed"]["default"], "c")
        self.assertEqual(self.spec["speed"]["constraints"], [{"name": "Enum", "value": "b;c"}])

    def test_a_slice_of_a_named_type_carries_the_type_s_enum_on_its_items(self):
        self.assertEqual(self.spec["levels"]["type"], "[]string")
        self.assertEqual(
            self.spec["levels"]["constraints"],
            [{"name": "MaxItems", "value": "2"}, {"name": "items:Enum", "value": "fast;slow"}],
        )

    def test_a_cel_rule_stays_out_under_the_items_and_values_prefixes_too(self):
        # `Mode` carries an XValidation marker beside its Enum, and `tags`
        # a hand-written `items:XValidation`; the column lists schema
        # bounds only, so none of the four rows carries the rule under any
        # spelling.
        for path in ("mode", "levels", "ranks", "tags"):
            names = [constraint["name"] for constraint in self.spec[path]["constraints"]]
            self.assertFalse(any("XValidation" in name for name in names), (path, names))
        self.assertIn({"name": "items:Enum", "value": "fast;slow"}, self.spec["levels"]["constraints"])
        self.assertIn({"name": "values:Enum", "value": "fast;slow"}, self.spec["ranks"]["constraints"])

    def test_a_map_of_a_named_type_carries_the_type_s_enum_on_its_values(self):
        # controller-gen writes the value type's validation under the map's
        # additionalProperties, as it writes a slice's under items; the
        # pointer in the value type is transparent as it is on a field.
        self.assertEqual(self.spec["ranks"]["type"], "map[string]string")
        self.assertEqual(
            self.spec["ranks"]["constraints"],
            [{"name": "MaxProperties", "value": "3"}, {"name": "values:Enum", "value": "fast;slow"}],
        )

    def test_rows_keep_source_order(self):
        paths = [field["path"] for field in self.kind["spec"]]
        self.assertEqual(paths[:4], ["owner", "name", "alias", "replicas"])
        self.assertLess(paths.index("parts"), paths.index("parts[].name"))
        self.assertEqual(paths[-3:], ["extra", "extra.name", "extra.weight"])


@unittest.skipIf(shutil.which(generate_docs.GO_BINARY) is None, "needs a Go toolchain")
class GeneratorFailureTest(unittest.TestCase):
    def test_an_inline_embed_from_another_package_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gadget_types.go").write_text(INLINE_EXTERNAL, encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                generate_docs.run_crd_generator(Path(tmp), ("Gadget",))
        self.assertIn("metav1.ObjectMeta", str(caught.exception))

    def test_an_unknown_kind_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(Path(tmp))
            with self.assertRaises(SystemExit) as caught:
                generate_docs.run_crd_generator(Path(tmp), ("Gizmo",))
        self.assertIn("Gizmo", str(caught.exception))


@unittest.skipIf(shutil.which(generate_docs.GO_BINARY) is None, "needs a Go toolchain")
class NonUTF8LocaleTest(unittest.TestCase):
    def test_the_model_is_read_as_utf_8_whatever_the_locale_says(self):
        # The doc comments carry section signs and em dashes, so the
        # generator's output is multibyte UTF-8; a read that took the
        # locale's codec would fail (ASCII) or write mojibake (latin-1)
        # under a developer's non-UTF-8 locale.
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(Path(tmp))
            result = subprocess.run(
                [sys.executable, "-X", "utf8=0", "-c", NON_UTF8_LOCALE_CHILD, tmp],
                cwd=Path(__file__).resolve().parent,
                env={**os.environ, **NON_UTF8_LOCALE_ENV},
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            json.loads(result.stdout.decode("utf-8")),
            "Alias is the widget's name in §2 of the spec — its optional spelling.",
        )


class MissingToolchainTest(unittest.TestCase):
    def test_a_missing_go_names_the_toolchain(self):
        with mock.patch.object(generate_docs.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit) as caught:
                generate_docs.run_crd_generator(generate_docs.CRD_API_DIR, ("AgentPlugin",))
        self.assertIn("Go toolchain", str(caught.exception))


def schema_paths(
    schema: dict, external: set[str], prefix: str, paths: set[str], required: dict[str, list[str]]
) -> None:
    """Collect every property path under ``schema`` and each object's ``required``.

    Descent stops at a path the generator marked external: the schema holds the
    upstream type's own properties there, which the page names rather than lists.
    """
    required[prefix] = sorted(schema.get("required") or [])
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        paths.add(path)
        if path in external:
            continue
        items = sub.get("items")
        values = sub.get("additionalProperties")
        if "properties" in sub:
            schema_paths(sub, external, path, paths, required)
        elif isinstance(items, dict) and "properties" in items:
            schema_paths(items, external, f"{path}[]", paths, required)
        elif isinstance(values, dict) and "properties" in values:
            schema_paths(values, external, f"{path}{{}}", paths, required)


def parent_of(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


@unittest.skipIf(shutil.which(generate_docs.GO_BINARY) is None, "needs a Go toolchain")
class CRDSchemaParityTest(unittest.TestCase):
    """The generated rows and the committed CRD schemas describe the same fields."""

    @classmethod
    def setUpClass(cls):
        cls.model = generate_docs.crd_model()

    def schema_tree(self, kind: str, tree: str) -> dict:
        crd = yaml.safe_load(CRD_FILES[kind].read_text(encoding="utf-8"))
        (version,) = crd["spec"]["versions"]
        return version["schema"]["openAPIV3Schema"]["properties"][tree]

    def test_every_kind_is_modelled(self):
        self.assertEqual(set(self.model), set(CRD_FILES))

    def test_paths_match_in_both_directions(self):
        for kind in CRD_FILES:
            for tree, _ in generate_docs.CRD_TREES:
                with self.subTest(kind=kind, tree=tree):
                    rows = self.model[kind][tree]
                    generated = {row["path"] for row in rows}
                    external = {row["path"] for row in rows if row["external"]}
                    expected: set[str] = set()
                    schema_paths(self.schema_tree(kind, tree), external, "", expected, {})
                    self.assertEqual(
                        sorted(generated - expected), [], "rows the schema does not carry"
                    )
                    self.assertEqual(
                        sorted(expected - generated), [], "schema paths with no row"
                    )

    def test_required_lists_match(self):
        for kind in CRD_FILES:
            for tree, _ in generate_docs.CRD_TREES:
                with self.subTest(kind=kind, tree=tree):
                    rows = self.model[kind][tree]
                    external = {row["path"] for row in rows if row["external"]}
                    required: dict[str, list[str]] = {}
                    schema_paths(self.schema_tree(kind, tree), external, "", set(), required)
                    for parent, names in required.items():
                        generated = sorted(
                            row["name"]
                            for row in rows
                            if row["required"] and parent_of(row["path"]) == parent
                        )
                        self.assertEqual(generated, names, f"required under {parent or tree!r}")

    def test_types_match(self):
        for kind in CRD_FILES:
            for tree, _ in generate_docs.CRD_TREES:
                with self.subTest(kind=kind, tree=tree):
                    rows = self.model[kind][tree]
                    external = {row["path"] for row in rows if row["external"]}
                    schemas: dict[str, dict] = {}
                    collect_schemas(self.schema_tree(kind, tree), external, "", schemas)
                    for row in rows:
                        if row["external"]:
                            # metav1.Time is a string, resource.Quantity an
                            # int-or-string: the page names the Go type instead.
                            continue
                        self.assertEqual(
                            schema_type(schemas[row["path"]]), row["type"], row["path"]
                        )

    def test_constraints_match(self):
        for kind in CRD_FILES:
            for tree, _ in generate_docs.CRD_TREES:
                with self.subTest(kind=kind, tree=tree):
                    rows = self.model[kind][tree]
                    external = {row["path"] for row in rows if row["external"]}
                    schemas: dict[str, dict] = {}
                    collect_schemas(self.schema_tree(kind, tree), external, "", schemas)
                    for row in rows:
                        generated = sorted((c["name"], c["value"]) for c in row["constraints"])
                        expected = sorted(schema_constraints(schemas[row["path"]], generated))
                        if row["external"]:
                            # An upstream type brings its own validation
                            # (resource.Quantity's pattern); the page names the
                            # type, so only the field's own markers must show.
                            self.assertEqual(sorted(set(generated) - set(expected)), [], row["path"])
                        else:
                            self.assertEqual(generated, expected, row["path"])

    def test_defaults_match(self):
        for kind in CRD_FILES:
            for tree, _ in generate_docs.CRD_TREES:
                with self.subTest(kind=kind, tree=tree):
                    rows = self.model[kind][tree]
                    external = {row["path"] for row in rows if row["external"]}
                    defaults: dict[str, str] = {}
                    collect_defaults(self.schema_tree(kind, tree), external, "", defaults)
                    generated = {row["path"]: row["default"] for row in rows if row["default"]}
                    self.assertEqual(generated, defaults)


def schema_constraints(schema: dict, generated: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The constraints a schema node carries, spelled as the generator spells them.

    The marker table is the generator's own, so a marker the page labels is one
    this reads back, and so is the prefix table: a list's items are read under
    `items` and a map's values under `additionalProperties`. A keyword
    controller-gen writes without a marker (`format`
    from an integer's Go width) counts only where the generator emitted that
    marker on the same node, so the schema's implied entries are not demanded
    of the page.
    """
    emitted = {name for name, _ in generated}
    nodes = [(schema, "")] + [
        (schema.get(keyword) or {}, prefix)
        for prefix, (_, keyword) in generate_docs.CONSTRAINT_PREFIXES.items()
    ]
    out: list[tuple[str, str]] = []
    for node, prefix in nodes:
        for marker, (_, key) in generate_docs.CONSTRAINT_MARKERS.items():
            if key not in node:
                continue
            if marker in generate_docs.SCHEMA_IMPLIED_MARKERS and prefix + marker not in emitted:
                continue
            value = node[key]
            value = ";".join(value) if isinstance(value, list) else str(value)
            out.append((prefix + marker, value))
    return out


def schema_type(schema: dict) -> str:
    """A schema node's type as the generator renders it."""
    if schema.get("type") == "array":
        items = schema.get("items") or {}
        return "[]" + ("object" if "properties" in items else items.get("type", "object"))
    if schema.get("type") == "object" and "additionalProperties" in schema:
        values = schema["additionalProperties"]
        return "map[string]" + ("object" if "properties" in values else values.get("type", "object"))
    return schema.get("type", "object")


def collect_schemas(schema: dict, external: set[str], prefix: str, schemas: dict[str, dict]) -> None:
    """Every property's schema node by its generated path, stopping at external leaves."""
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        schemas[path] = sub
        if path in external:
            continue
        items = sub.get("items")
        values = sub.get("additionalProperties")
        if "properties" in sub:
            collect_schemas(sub, external, path, schemas)
        elif isinstance(items, dict) and "properties" in items:
            collect_schemas(items, external, f"{path}[]", schemas)
        elif isinstance(values, dict) and "properties" in values:
            collect_schemas(values, external, f"{path}{{}}", schemas)


def collect_defaults(schema: dict, external: set[str], prefix: str, defaults: dict[str, str]) -> None:
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        if "default" in sub:
            value = sub["default"]
            defaults[path] = str(value).lower() if isinstance(value, bool) else str(value)
        if path in external:
            continue
        items = sub.get("items")
        values = sub.get("additionalProperties")
        if "properties" in sub:
            collect_defaults(sub, external, path, defaults)
        elif isinstance(items, dict) and "properties" in items:
            collect_defaults(items, external, f"{path}[]", defaults)
        elif isinstance(values, dict) and "properties" in values:
            collect_defaults(values, external, f"{path}{{}}", defaults)


if __name__ == "__main__":
    unittest.main()
