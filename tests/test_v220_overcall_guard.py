import unittest
from pathlib import Path

class TestV220OvercallGuard(unittest.TestCase):
    def test_fred_burst_is_bounded(self):
        text=Path("src/collectors/global_market.py").read_text(encoding="utf-8")
        self.assertIn("max_workers=min(2, len(FRED_GROUPS))", text)
        self.assertIn("time.sleep(0.25)", text)

if __name__ == "__main__": unittest.main()
