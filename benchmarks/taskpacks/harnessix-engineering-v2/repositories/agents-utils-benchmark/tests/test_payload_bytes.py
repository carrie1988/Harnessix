from __future__ import annotations

import unittest

from src.agent_utils import decode_payload


class DecodePayloadTest(unittest.TestCase):
    def test_decodes_valid_utf8(self) -> None:
        self.assertEqual(decode_payload("你好".encode()), "你好")


if __name__ == "__main__":
    unittest.main()
