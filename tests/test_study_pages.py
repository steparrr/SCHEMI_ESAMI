from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from planner import group_study_schemes, materialize_schemes


class StudyPageTests(unittest.TestCase):
    def test_count_scales_with_material_and_all_topics_survive(self):
        cfg = {"schema_page_min": 25, "schema_page_max": 30,
               "token_budget": 60000, "max_chunks": 50, "mode": "completa"}
        units = [{"id": f"u{i}", "tipo": "sbobina", "tokens": 1000} for i in range(12)]
        topics = [{"id": f"t{i}", "titolo": f"Tema {i}", "unita": [f"u{i}"],
                   "punti_essenziali": ["Definizione"], "keywords": []} for i in range(12)]
        small = [{"titolo": f"Tema {i}", "argomenti": [f"t{i}"],
                  "pagine": 9, "keywords": []} for i in range(12)]
        compact = group_study_schemes(small, {t["id"]: t for t in topics},
                                      {u["id"]: u for u in units}, cfg)
        self.assertEqual(len(compact), 4)
        self.assertEqual([t for item in compact for t in item["argomenti"]],
                         [f"t{i}" for i in range(12)])
        rendered = materialize_schemes({"schemi": small}, topics, units, cfg)
        self.assertEqual(len(rendered), 4)
        self.assertEqual([u for item in rendered for u in item["chunk_ids"]],
                         [f"u{i}" for i in range(12)])
        self.assertTrue(all(item["page_min"] == 25 and item["page_budget"] == 30
                            for item in rendered))

    def test_source_capacity_stops_an_unsafe_merge(self):
        schemes = [{"titolo": "Uno", "argomenti": ["t1"], "pagine": 13, "keywords": []},
                   {"titolo": "Due", "argomenti": ["t2"], "pagine": 13, "keywords": []}]
        topics = {"t1": {"unita": ["u1"]}, "t2": {"unita": ["u2"]}}
        units = {"u1": {"tipo": "sbobina", "tokens": 20000},
                 "u2": {"tipo": "sbobina", "tokens": 20000}}
        cfg = {"schema_page_min": 25, "schema_page_max": 30,
               "token_budget": 30000, "max_chunks": 50}
        with self.assertRaisesRegex(ValueError, "senza ometterle"):
            group_study_schemes(schemes, topics, units, cfg)


if __name__ == "__main__":
    unittest.main()
