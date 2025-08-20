import unittest
from tdr_plots.live_plot import save_csv
import pandas as pd


class TestPlots(unittest.TestCase):
    def test_read_write_csv(self):
        fname = "fname.csv"
        save_csv(fname, [1,2,3], [4,5,6], [[1,1,1], [2,2,2], [3,3,3]])
        df = pd.read_csv(fname)
        self.assertEqual(tuple(df["rxdac (dac)"]), (1,2,3))
        self.assertEqual(tuple(df["time (ps)"]), (4,5,6))
        self.assertEqual(tuple(df["Trace_0"]), (1,1,1))
        self.assertEqual(tuple(df["Trace_1"]), (2,2,2))



if __name__ == "__main__":
    unittest.main()
