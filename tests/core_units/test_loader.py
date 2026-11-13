"""Exercise rendering and YAML parsing with real temporary configuration files."""

import contextlib
import io

import yaml
from jinja2 import TemplateNotFound, TemplateSyntaxError

from seq_quince.loader import j2_yaml_loader

from ._support import CoreTestCase


class LoaderTests(CoreTestCase):
    def load(self, path, replacements=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return j2_yaml_loader(str(path), replacements)

    def test_renders_both_placeholder_styles_and_preserves_yaml_types(self):
        path = self.temp_dir / "config.yaml"
        path.write_text(
            'pv: "${IOC}:motor"\nposition: {{ POSITION }}\n'
            'enabled: true\naxes: [x, y]\nlabel: "{{ LABEL }}"\n',
            encoding="utf-8",
        )
        replacements = {"IOC": "TEST", "POSITION": "1.5", "LABEL": "zero order"}
        original = dict(replacements)
        self.assertEqual(
            self.load(path, replacements),
            {
                "pv": "TEST:motor",
                "position": 1.5,
                "enabled": True,
                "axes": ["x", "y"],
                "label": "zero order",
            },
        )
        self.assertEqual(replacements, original)

    def test_plain_yaml_needs_no_replacements(self):
        path = self.temp_dir / "plain.yaml"
        path.write_text("points: [1, 2]\nenabled: false\n", encoding="utf-8")
        self.assertEqual(self.load(path), {"points": [1, 2], "enabled": False})

    def test_loading_does_not_print_rendered_configuration(self):
        path = self.temp_dir / "quiet.yaml"
        path.write_text('pv: "${IOC}:motor"\n', encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = j2_yaml_loader(str(path), {"IOC": "TEST"})
        self.assertEqual(result, {"pv": "TEST:motor"})
        self.assertEqual(output.getvalue(), "")

    def test_unsafe_python_yaml_tags_are_rejected(self):
        path = self.temp_dir / "unsafe.yaml"
        for tag in (
            "!!python/object/apply:builtins.print ['unsafe YAML executed']",
            "!!python/object/new:builtins.dict {}",
            "!!python/tuple [1, 2]",
        ):
            with self.subTest(tag=tag), self.assertRaises(
                yaml.constructor.ConstructorError
            ):
                path.write_text(tag + "\n", encoding="utf-8")
                self.load(path)

    def test_include_search_order_and_parent_search_paths(self):
        beamlines = self.temp_dir / "beamlines"
        current = beamlines / "line"
        current.mkdir(parents=True)
        for directory, value in (
            (self.temp_dir, "grandparent"),
            (beamlines, "parent"),
            (current, "current"),
        ):
            (directory / "shared.yaml").write_text(
                "selected: " + value + "\n\n", encoding="utf-8"
            )
        (beamlines / "parent.yaml").write_text("from_parent: 2\n\n", encoding="utf-8")
        (self.temp_dir / "grandparent.yaml").write_text(
            "from_grandparent: 3\n\n", encoding="utf-8"
        )
        config = current / "config.yaml"
        config.write_text(
            '{% include "shared.yaml" %}\n{% include "parent.yaml" %}\n'
            '{% include "grandparent.yaml" %}\n',
            encoding="utf-8",
        )
        self.assertEqual(
            self.load(config),
            {
                "selected": "current",
                "from_parent": 2,
                "from_grandparent": 3,
            },
        )
        (current / "shared.yaml").unlink()
        self.assertEqual(self.load(config)["selected"], "parent")
        (beamlines / "shared.yaml").unlink()
        self.assertEqual(self.load(config)["selected"], "grandparent")

    def test_missing_files_and_directories_are_rejected_with_the_path(self):
        for path in (self.temp_dir / "missing.yaml", self.temp_dir):
            with self.subTest(path=path), self.assertRaises(
                FileNotFoundError
            ) as caught:
                self.load(path)
            self.assertEqual(caught.exception.args, (str(path),))

    def test_invalid_yaml_and_templates_propagate_errors(self):
        cases = [
            ("values: [1,\n", yaml.YAMLError),
            ("{% if %}\n", TemplateSyntaxError),
            ('{% include "missing.yaml" %}\n', TemplateNotFound),
        ]
        path = self.temp_dir / "invalid.yaml"
        for text, error in cases:
            with self.subTest(text=text), self.assertRaises(error):
                path.write_text(text, encoding="utf-8")
                self.load(path)
