import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class ShortcodeLockerTests(unittest.TestCase):
    def test_classify_values(self):
        self.assertEqual(app.classify_value("https://example.com"), "url")
        self.assertEqual(app.classify_value("file:///mnt/storage"), "uri")
        self.assertEqual(app.classify_value("Spare keys by door"), "text")

    def test_code_validation_uses_alphabet(self):
        alphabet = app.DEFAULT_ALPHABET
        self.assertEqual(app.validate_code("I2b", alphabet), "I2b")
        with self.assertRaises(ValueError):
            app.validate_code("l2b", alphabet)
        with self.assertRaises(ValueError):
            app.validate_code("ABCD", alphabet)

    def test_save_and_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "codes.json"
            entries = {
                "ABC": app.Entry(code="ABC", value="https://example.com", label="Example"),
                "I2b": app.Entry(code="I2b", value="Shelf note", kind="text"),
            }
            app.save_entries(data, entries)
            loaded = app.load_entries(data, app.DEFAULT_ALPHABET)
            self.assertEqual(loaded["ABC"].resolved_kind(), "url")
            self.assertEqual(loaded["I2b"].value, "Shelf note")
            self.assertTrue(json.loads(data.read_text())["ABC"]["value"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
