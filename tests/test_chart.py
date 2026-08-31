import sys
import os
import unittest
import time

# Add parent dir to path so we can import services
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.chart_generator import generate_node_load_chart

class TestChartGenerator(unittest.TestCase):
    def test_generate_empty_chart(self):
        """Verifies chart generation handles empty list of metrics gracefully."""
        image_bytes = generate_node_load_chart("Test Node", [])
        self.assertIsInstance(image_bytes, bytes)
        self.assertTrue(len(image_bytes) > 0)
        # Check standard PNG header (first 8 bytes)
        self.assertEqual(image_bytes[:8], b'\x89PNG\r\n\x1a\n')

    def test_generate_populated_chart(self):
        """Verifies chart generation renders standard timeseries data correctly."""
        now = int(time.time())
        # Add a series of data points over the last 6 hours
        metrics = [
            (now - 3600 * 5, 12.5, 45.2, 5),
            (now - 3600 * 4, 15.0, 46.0, 7),
            (now - 3600 * 3, 90.5, 52.1, 12),
            (now - 3600 * 2, 45.0, 49.8, 8),
            (now - 3600 * 1, 20.2, 47.5, 6),
            (now, 18.0, 47.0, 5),
        ]
        image_bytes = generate_node_load_chart("Hyper-Core 01", metrics)
        self.assertIsInstance(image_bytes, bytes)
        self.assertTrue(len(image_bytes) > 0)
        self.assertEqual(image_bytes[:8], b'\x89PNG\r\n\x1a\n')

if __name__ == '__main__':
    unittest.main()
