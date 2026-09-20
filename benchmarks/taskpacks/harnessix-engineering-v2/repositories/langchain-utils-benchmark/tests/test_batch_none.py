from __future__ import annotations

import unittest

from src.string_utils import batch_iterate


class BatchIterateTest(unittest.TestCase):
    def test_none_size_returns_single_batch(self) -> None:
        self.assertEqual(list(batch_iterate(None, [1, 2, 3])), [[1, 2], [3]])


if __name__ == "__main__":
    unittest.main()
