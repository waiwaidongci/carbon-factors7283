"""默认快速测试入口（不含百万行规模测试）。

用法：python3 -m tests.run_fast
规模测试单独执行：python3 -m unittest tests.test_scale
"""
import unittest

from tests import test_reconciliation, test_service


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromModule(test_reconciliation))
    suite.addTests(loader.loadTestsFromModule(test_service))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
