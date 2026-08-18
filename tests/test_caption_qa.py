import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "helpers"))
import caption_qa  # noqa: E402


GLOSSARY = {
    "replacements": [
        {"from": "DGD", "to": "dig.D"},
        {"from": "área", "to": "arte", "prev_any": ["entrega"]},
        {"from": "area", "to": "arte", "prev_any": ["entrega"]},
    ],
    "emph": ["arte", "sistema"],
    "never_highlight": ["um", "que", "a"],
}


def words(*pairs: tuple[str, str, float]) -> list[dict]:
    """(type, text, start) — type is 'word' or 'spacing'."""
    out = []
    for kind, text, start in pairs:
        out.append({
            "text": text,
            "start": start,
            "end": start + (0.04 if kind == "spacing" else 0.2),
            "type": kind,
        })
    return out


class CaptionQaTests(unittest.TestCase):
    def test_rewrites_brand_and_homophone(self) -> None:
        ws = words(
            ("word", "entrega", 1.0),
            ("spacing", " ", 1.2),
            ("word", "a", 1.25),
            ("spacing", " ", 1.3),
            ("word", "área", 1.35),
            ("spacing", " ", 1.55),
            ("word", "DGD", 1.6),
        )
        changes = caption_qa.apply_replacements(ws, GLOSSARY["replacements"])
        texts = [w["text"] for w in ws if w["type"] == "word"]
        self.assertIn("arte", texts)
        self.assertIn("dig.D", texts)
        self.assertTrue(any("área" in c and "arte" in c for c in changes))

    def test_area_without_entrega_stays(self) -> None:
        ws = words(("word", "área", 0.0), ("word", "útil", 0.3))
        caption_qa.apply_replacements(ws, GLOSSARY["replacements"])
        self.assertEqual(ws[0]["text"], "área")

    def test_drop_sequence_removes_run(self) -> None:
        ws = words(
            ("word", "Que", 4.3),
            ("spacing", " ", 4.4),
            ("word", "a", 4.45),
            ("spacing", " ", 4.5),
            ("word", "DGD", 4.66),
            ("spacing", " ", 4.82),
            ("word", "não", 4.9),
            ("spacing", " ", 5.1),
            ("word", "é", 5.2),
            ("spacing", " ", 5.4),
            ("word", "e", 5.52),
            ("word", "por", 5.64),
        )
        n = caption_qa.drop_sequence(ws, ["Que", "a", "DGD", "não", "é"])
        self.assertEqual(n, 5)
        leftover = [w["text"] for w in ws if w.get("type") == "word"]
        self.assertEqual(leftover, ["e", "por"])

    def test_scan_cues_flags_stop_word_accent(self) -> None:
        cues = [{
            "i": 19, "startMs": 19075,
            "lineStyles": [3, 1, 2],
            "lines": [
                [{"text": "Quer"}],
                [{"text": "saber"}],
                [{"text": "um"}],
            ],
        }]
        flags = caption_qa.scan_cues(cues, {"um", "que", "a"})
        self.assertEqual(len(flags), 1)
        self.assertIn("um", flags[0])

    def test_write_roundtrip(self) -> None:
        payload = {
            "text": "entrega a área DGD",
            "words": words(
                ("word", "entrega", 1.0),
                ("spacing", " ", 1.2),
                ("word", "a", 1.25),
                ("spacing", " ", 1.3),
                ("word", "área", 1.35),
                ("spacing", " ", 1.55),
                ("word", "DGD", 1.6),
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tr = root / "cut.json"
            caps = root / "captions.json"
            tr.write_text(json.dumps(payload), encoding="utf-8")
            rc = caption_qa.run(
                transcript=tr,
                captions=caps,
                glossary=GLOSSARY,
                drop_sequences=[],
                cues_path=None,
                write=True,
            )
            self.assertEqual(rc, 0)
            written = json.loads(caps.read_text(encoding="utf-8"))
            texts = [c["text"] for c in written]
            self.assertEqual(texts, ["entrega", "a", "arte", "dig.D"])


if __name__ == "__main__":
    unittest.main()
