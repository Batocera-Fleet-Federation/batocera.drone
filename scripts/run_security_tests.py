"""Run security acceptance tests, rejecting empty discovery as a failure."""

import argparse
from pathlib import Path
import sys
import unittest


# Keep repository imports available when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--start-directory", default="tests/adversarial/security")
    parser.add_argument("-p", "--pattern", default="test_*.py")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(args.start_directory, pattern=args.pattern)
    if suite.countTestCases() == 0:
        print("No security tests were collected; refusing an empty acceptance suite.", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
