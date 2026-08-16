import unittest
from pathlib import Path

class TestV220OvercallGuard(unittest.TestCase):
    def test_fred_burst_is_bounded(self):
        text=Path("src/collectors/global_market.py").read_text(encoding="utf-8")
        self.assertIn("ThreadPoolExecutor(max_workers=2)", text)
        self.assertIn("time.sleep(0.20)", text)
        self.assertIn("fred_series_aware_incremental", text)

if __name__ == "__main__": unittest.main()
