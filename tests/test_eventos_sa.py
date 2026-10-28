import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "atualizar_eventos_sa.py"
spec = importlib.util.spec_from_file_location("atualizar_eventos_sa", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class EventosSATests(unittest.TestCase):
    def test_rejects_non_official_urls(self):
        self.assertEqual(collector.valid_official_url("http://www.sa.playblackdesert.com/News/Detail?groupContentNo=1"), "")
        self.assertEqual(collector.valid_official_url("https://example.com/News/Detail?groupContentNo=1"), "")

    def test_accepts_official_notice_url(self):
        result = collector.normalize_url("/News/Detail?groupContentNo=123")
        self.assertTrue(result.startswith("https://www.sa.playblackdesert.com/News/Detail?"))
        self.assertIn("countryType=pt-BR", result)
        self.assertIn("groupContentNo=123", result)

    def test_days_remaining_parser(self):
        self.assertEqual(collector.parse_days("Evento termina em 9 dias restantes"), 9)
        self.assertIsNone(collector.parse_days("Permanente"))

    def test_card_permanent_badges(self):
        self.assertTrue(collector.card_marks_permanent("POPULAR Evento de benefícios"))
        self.assertTrue(collector.card_marks_permanent("Evento Permanente"))
        self.assertFalse(collector.card_marks_permanent("Evento termina em 9 dias restantes"))

    def test_clean_event_title_removes_ui_badges_and_countdown(self):
        self.assertEqual(collector.clean_event_title("POPULAR Passe Especial Diário com o Demônio 10 dias restantes"), "Passe Especial Diário com o Demônio")
        self.assertEqual(collector.clean_event_title("NOVO Evento de Teste"), "Evento de Teste")
        self.assertEqual(collector.clean_event_title("[Arena Solare] Vitória Ranqueada Semanal!"), "[Arena Solare] Vitória Ranqueada Semanal!")

    def test_date_parser_iso_and_brazilian(self):
        iso = collector.parse_date_tokens("Período: 2026-10-22 03:00 ~ 2026-11-05 02:59")
        self.assertGreaterEqual(len(iso), 2)
        self.assertEqual(iso[-1].date().isoformat(), "2026-11-05")
        br = collector.parse_date_tokens("Período: 22/10/2026 03:00 ~ 05/11/2026 02:59")
        self.assertGreaterEqual(len(br), 2)
        self.assertEqual(br[-1].date().isoformat(), "2026-11-05")

    def test_event_range_uses_end_date_not_footer_date(self):
        text = "Evento: 2026-10-22 03:00~2026-11-05 02:59 Atualizado em 2026-11-20"
        end = collector.parse_event_end_date(text)
        self.assertEqual(end.date().isoformat(), "2026-11-05")

    def test_embedded_snapshot_has_expected_shape(self):
        events = collector.load_snapshot()
        self.assertGreaterEqual(len(events), 5)
        for event in events:
            self.assertTrue(event.get("title"))
            self.assertTrue(collector.normalize_url(event.get("url", "")))


if __name__ == "__main__":
    unittest.main()
