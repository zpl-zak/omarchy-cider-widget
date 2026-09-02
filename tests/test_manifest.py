import json
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


class ManifestTests(unittest.TestCase):
    def test_required_plugin_contract(self):
        self.assertEqual(MANIFEST.get("schemaVersion"), 1)
        for field in ("id", "name", "version", "kinds", "entryPoints"):
            self.assertIn(field, MANIFEST)

        plugin_id = MANIFEST["id"]
        self.assertRegex(plugin_id, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
        self.assertNotIn("..", plugin_id)
        self.assertFalse(plugin_id.startswith("omarchy."))
        self.assertIsInstance(MANIFEST["kinds"], list)
        self.assertTrue(MANIFEST["kinds"])
        self.assertIsInstance(MANIFEST["entryPoints"], dict)

    def test_kinds_have_safe_existing_entry_points(self):
        entry_points = MANIFEST["entryPoints"]
        required = {
            "bar": "bar",
            "bar-widget": "barWidget",
            "menu": "menu",
            "overlay": "overlay",
            "panel": "panel",
            "service": "service",
        }
        for kind in MANIFEST["kinds"]:
            if kind in required:
                self.assertIn(required[kind], entry_points)

        for value in entry_points.values():
            self.assertIsInstance(value, str)
            path = PurePosixPath(value)
            self.assertTrue(value)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertNotIn("\n", value)
            self.assertTrue((ROOT / path).is_file(), value)

        section = MANIFEST.get("barWidget", {}).get("defaultSection")
        if section is not None:
            self.assertIn(section, ("left", "center", "right"))

    def test_repository_contains_no_symlinks(self):
        links = [
            path.relative_to(ROOT)
            for path in ROOT.rglob("*")
            if ".git" not in path.relative_to(ROOT).parts and path.is_symlink()
        ]
        self.assertEqual(links, [])

    def test_security_boundary_uses_streaming_output_and_local_artwork(self):
        service = (ROOT / "Service.qml").read_text(encoding="utf-8")
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        self.assertFalse((ROOT / "AGENTS.md").exists())
        self.assertNotIn("StdioCollector", service)
        self.assertIn("SplitParser", service)
        self.assertNotIn("artUrl", panel)
        self.assertIn("artSource", panel)


if __name__ == "__main__":
    unittest.main()
