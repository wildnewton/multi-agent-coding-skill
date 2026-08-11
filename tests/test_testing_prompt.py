import unittest
from pathlib import Path


TESTING_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "testing.md"


class TestingPromptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = TESTING_PROMPT.read_text(encoding="utf-8")

    def test_prompt_keeps_critical_test_quality_principles(self):
        for required in (
            "Treat tests as executable specification",
            "Before writing code, briefly list the test cases you intend to cover and why",
            "minimal complete test set",
            "one well-defined behavior per test",
            "A failing test is valid RED only when it fails for the right reason",
            "review existing tests for quality",
        ):
            self.assertIn(required, self.text)

    def test_prompt_preserves_role_and_handoff_boundaries(self):
        for required in (
            "Do not modify production code",
            "Do not choose the next agent",
            "Do not include `next_agent`",
            "return results to Coordinator through Hermes",
            "Hermes, not Testing, publishes the verified PR handoff comment",
        ):
            self.assertIn(required, self.text)

    def test_prompt_stays_concise(self):
        self.assertLessEqual(len(self.text.split()), 450)


if __name__ == "__main__":
    unittest.main()
