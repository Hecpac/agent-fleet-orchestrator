import unittest
from sample_stats import stats


class StatsTests(unittest.TestCase):
    def test_regular(self):
        self.assertEqual(stats([1, 2, 3, 4]), {"count": 4, "sum": 10, "mean": 2.5})

    def test_negative_and_fractional(self):
        self.assertEqual(stats([-2, 0.5, 3]), {"count": 3, "sum": 1.5, "mean": 0.5})

    def test_empty(self):
        with self.assertRaises(ValueError):
            stats([])

    def test_invalid(self):
        for values in ([1, "2"], [True, 1], [None]):
            with self.subTest(values=values), self.assertRaises(TypeError):
                stats(values)

    def test_input_not_mutated(self):
        values = [3, 2, 1]
        stats(values)
        self.assertEqual(values, [3, 2, 1])


if __name__ == "__main__":
    unittest.main()
