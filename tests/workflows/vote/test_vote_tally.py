import unittest
from robert_agent.workflows.vote import tally as vote_tally


class VoteTallyTests(unittest.TestCase):
    def test_equal_votes_abstention_and_majority_denominator(self):
        spec = {
            "participants": ["one", "two", "three", "four"],
            "options": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
        }
        for votes, outcome in [
            (["A", "A", "A", None], "majority"),
            (["A", "A", "B", None], "plurality_only"),
            (["A", "A", "B", "B"], "tie"),
            ([None] * 4, "all_abstain"),
        ]:
            with self.subTest(votes=votes):
                result = vote_tally.tally(spec, [{"choice_id": v} for v in votes])
                self.assertEqual(result["outcome"], outcome)
        with self.assertRaises(ValueError):
            vote_tally.tally(spec, [{"choice_id": "A"}])
