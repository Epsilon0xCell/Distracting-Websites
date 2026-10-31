"""Tests for tool.py.

The Gemini client and the page fetch are mocked throughout, and the
module imports without beautifulsoup4 or google-genai installed,
which is itself one of the things under test. No network call and no
API call is made by this suite.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tool  # noqa: E402


class FakeModels:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def generate_content(self, model, contents):
        self.calls += 1
        return mock.Mock(text=self.text)


class FakeClient:
    def __init__(self, text):
        self.models = FakeModels(text)


def with_client(text):
    return mock.patch.object(tool, "get_client", return_value=FakeClient(text))


class ImportAndEntryTest(unittest.TestCase):
    def run_cli(self, *args, env_extra=None):
        import os
        env = dict(os.environ)
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(ROOT / "tool.py"), *args],
            capture_output=True, text=True, timeout=20, env=env, stdin=subprocess.DEVNULL,
        )

    def test_help_exits_zero_without_packages_or_key(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)

    def test_unknown_argument_exits_two(self):
        result = self.run_cli("--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    def test_no_key_prints_named_message_and_exits_zero(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertIn("GEMINI_API_KEY", result.stdout)


class NormalizeDomainTest(unittest.TestCase):
    def test_bare_domain(self):
        self.assertEqual(tool.normalize_domain("khanacademy.org"), "khanacademy.org")

    def test_www_and_case_and_port_stripped(self):
        self.assertEqual(tool.normalize_domain("https://www.Example.COM:8080/path"), "example.com")

    def test_subdomain_kept(self):
        self.assertEqual(tool.normalize_domain("classroom.google.com"), "classroom.google.com")

    def test_ensure_scheme(self):
        self.assertEqual(tool.ensure_scheme("example.com"), "https://example.com")
        self.assertEqual(tool.ensure_scheme("http://example.com"), "http://example.com")


class ParseScoreTest(unittest.TestCase):
    def test_plain_and_labelled_scores(self):
        self.assertEqual(tool.parse_score("85"), 85)
        self.assertEqual(tool.parse_score("Score: 85"), 85)
        self.assertEqual(tool.parse_score("72.6"), 72)

    def test_no_number_returns_none(self):
        self.assertIsNone(tool.parse_score("no score here"))
        self.assertIsNone(tool.parse_score(""))
        self.assertIsNone(tool.parse_score(None))

    def test_out_of_range_returns_none(self):
        self.assertIsNone(tool.parse_score("250"))
        self.assertIsNone(tool.parse_score("Score: 101"))


class QuestionTest(unittest.TestCase):
    def test_clean_strips_bold_and_numbering(self):
        self.assertEqual(tool.Question("**1. What topic?**").text, "What topic?")

    def test_ask_returns_stripped_answer(self):
        with mock.patch("builtins.input", return_value="  algebra  "):
            self.assertEqual(tool.Question("What topic?").ask(), "algebra")


class ClientTest(unittest.TestCase):
    def test_missing_key_raises_named_error(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(tool, "_client", None):
            with self.assertRaises(RuntimeError) as ctx:
                tool.get_client()
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))


class SpecificityTest(unittest.TestCase):
    def test_score_at_threshold_passes(self):
        with with_client("80"):
            self.assertTrue(tool.is_task_specific_enough("a detailed task"))

    def test_score_below_threshold_fails(self):
        with with_client("79"):
            self.assertFalse(tool.is_task_specific_enough("vague"))

    def test_unparseable_score_fails_closed(self):
        with with_client("I cannot score this"):
            self.assertFalse(tool.is_task_specific_enough("vague"))


class RelevanceTest(unittest.TestCase):
    def test_known_platform_passes_without_any_fetch(self):
        with mock.patch.object(tool, "get_website_info", side_effect=AssertionError("fetched")):
            self.assertTrue(tool.determine_relevance("learn algebra", "https://www.khanacademy.org"))

    def test_failed_fetch_is_not_productive(self):
        with mock.patch.object(tool, "get_website_info", return_value="URL: example.com\nNote: Could not fetch complete information"):
            self.assertFalse(tool.determine_relevance("learn algebra", "example.com"))

    def test_high_distraction_domain_scales_score_down(self):
        # 80 raw from the model, scaled by 0.8 for youtube.com, lands
        # at 64 and misses the 75 threshold; unscaled it would pass.
        with mock.patch.object(tool, "get_website_info", return_value="Title: Lesson"), with_client("80"):
            self.assertFalse(tool.determine_relevance("learn algebra", "https://www.youtube.com/watch?v=1"))

    def test_regular_domain_uses_raw_score(self):
        with mock.patch.object(tool, "get_website_info", return_value="Title: Lesson"), with_client("80"):
            self.assertTrue(tool.determine_relevance("learn algebra", "https://example.org/lesson"))


class WebsiteInfoTest(unittest.TestCase):
    def test_known_platform_answered_locally(self):
        info = tool.get_website_info("khanacademy.org")
        self.assertIn("Title: Khan Academy", info)
        self.assertIn("Distraction Level: 0.1", info)


class MainLoopTest(unittest.TestCase):
    def test_blank_line_reprompts_and_spaced_exit_quits(self):
        answers = iter(["", " example.com ", " exit "])
        seen = []

        def fake_relevance(task, website):
            seen.append(website)
            return True

        with mock.patch.object(tool, "get_client", return_value=object()), \
                mock.patch.object(tool, "get_user_task", return_value="study algebra"), \
                mock.patch.object(tool, "determine_relevance", side_effect=fake_relevance), \
                mock.patch("builtins.input", side_effect=lambda prompt="": next(answers)), \
                mock.patch("builtins.print") as printed:
            tool.main()
        self.assertEqual(seen, ["example.com"])
        text = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("Enter a website URL", text)


if __name__ == "__main__":
    unittest.main()
