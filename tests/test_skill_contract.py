import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_hermes_publishes_verified_phase_handoffs_to_pr(self):
        self.assertIn("## PR Handoff Comments", self.text)
        self.assertIn("Hermes publishes the handoff comment only after verification", self.text)
        self.assertIn("Agents must not post their own handoff comments", self.text)

    def test_each_phase_has_a_distinct_handoff_comment(self):
        for heading in (
            "### Testing handoff",
            "### Coordinator handoff",
            "### Review handoff",
        ):
            self.assertIn(heading, self.text)

    def test_handoff_comments_include_evidence_and_next_owner(self):
        for required in (
            "Status:",
            "Commit:",
            "Verification:",
            "Next:",
            "Reviewed HEAD:",
        ):
            self.assertIn(required, self.text)

    def test_skill_has_a_concrete_pr_comment_command(self):
        self.assertIn("`gh` is installed and authenticated", self.text)
        self.assertIn("gh pr comment <pr-number>", self.text)


if __name__ == "__main__":
    unittest.main()
