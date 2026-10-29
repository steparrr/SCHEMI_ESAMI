from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
import json
import io

import fitz
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from visual_qc import (export_visual_report, inspect_visual, load_visual_reviews,
                       mark_visual_review)


class VisualQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sample.pdf"

    def tearDown(self):
        self.temp.cleanup()

    def make_pdf(self, position, opacity=1):
        doc = fitz.open()
        page = doc.new_page()
        text = "Questo testo medico sintetico deve rimanere leggibile nel documento."
        box = fitz.Rect(45, 73, 560, 105)
        if position == "before":
            page.draw_rect(box, fill=(.9, .7, .4), color=None, fill_opacity=opacity)
        page.insert_text((55, 96), text, fontsize=12)
        if position == "after":
            page.draw_rect(box, fill=(.9, .7, .4), color=None, fill_opacity=opacity)
        doc.save(self.path)
        doc.close()
        return self.path.read_bytes()

    def test_solid_overlay_after_text_is_flagged_without_pdf_mutation(self):
        before = self.make_pdf("after")
        warnings = inspect_visual(self.path)["pages"][0]["warnings"]
        self.assertTrue(any("sopra il testo" in warning for warning in warnings))
        self.assertEqual(self.path.read_bytes(), before)

    def test_background_and_transparent_highlight_are_not_flagged_as_overlay(self):
        self.make_pdf("before")
        self.assertFalse(any("sopra il testo" in warning for warning in inspect_visual(self.path)["pages"][0]["warnings"]))

    def test_large_low_resolution_image_is_a_warning_only(self):
        def make_image_pdf(pixels):
            picture = Image.new("RGB", (pixels, pixels), "#476782")
            stream = io.BytesIO()
            picture.save(stream, format="PNG")
            document = fitz.open()
            page = document.new_page()
            page.insert_text((40, 70), "Immagine di esempio sintetica per il controllo locale.")
            page.insert_image(fitz.Rect(80, 110, 450, 500), stream=stream.getvalue())
            document.save(self.path)
            document.close()

        make_image_pdf(64)
        before = self.path.read_bytes()
        self.assertTrue(any("sotto 90 dpi" in warning for warning in inspect_visual(self.path)["pages"][0]["warnings"]))
        self.assertEqual(self.path.read_bytes(), before)
        make_image_pdf(1200)
        self.assertFalse(any("sotto 90 dpi" in warning for warning in inspect_visual(self.path)["pages"][0]["warnings"]))
        self.make_pdf("after", opacity=.2)
        self.assertFalse(any("sopra il testo" in warning for warning in inspect_visual(self.path)["pages"][0]["warnings"]))

    def test_local_report_tracks_pdf_hash_and_rejects_changed_or_outside_pdf(self):
        root = Path(self.temp.name) / "project"
        target = root / "output/pdf/sample.pdf"
        target.parent.mkdir(parents=True)
        self.make_pdf("after")
        target.write_bytes(self.path.read_bytes())
        result = inspect_visual(target)
        before = target.read_bytes()
        report = export_visual_report(root, target, result)
        body = json.loads(report.read_text())
        self.assertEqual(body["pages_checked"], 1)
        self.assertEqual(body["pdf"], "output/pdf/sample.pdf")
        self.assertEqual(target.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "materia"):
            export_visual_report(root, self.path, inspect_visual(self.path))
        target.write_bytes(before + b"changed")
        with self.assertRaisesRegex(ValueError, "cambiato"):
            export_visual_report(root, target, result)

    def test_visual_decisions_are_version_bound_and_never_modify_the_pdf(self):
        root = Path(self.temp.name) / "project"
        target = root / "output/pdf/sample.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.make_pdf("after"))
        before = target.read_bytes()
        result = inspect_visual(target)
        self.assertEqual(load_visual_reviews(root, target, result), {})
        with self.assertRaisesRegex(ValueError, "motivo"):
            mark_visual_review(root, target, result, 1, "false_positive", "")
        reviewed = mark_visual_review(root, target, result, 1, "verified")
        self.assertEqual(reviewed["decision"], "verified")
        ignored = mark_visual_review(root, target, result, 1, "false_positive", "Il riquadro è intenzionale.")
        self.assertEqual(ignored["decision"], "false_positive")
        self.assertEqual(load_visual_reviews(root, target, result)["1"]["decision"], "false_positive")
        self.assertEqual(target.read_bytes(), before)
        target.write_bytes(before + b"modified")
        with self.assertRaisesRegex(ValueError, "cambiato"):
            mark_visual_review(root, target, result, 1, "verified")


if __name__ == "__main__":
    unittest.main()
