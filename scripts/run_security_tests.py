"""Run security acceptance tests, rejecting empty discovery as a failure."""

import argparse
import json
from pathlib import Path
import sys
import unittest


# Keep repository imports available when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def registered_tests(registry_path):
    """Check each enabled security registration before running any tests.

    Discovering the whole directory alone hides missing individual suites when
    other test files remain. Read legacy unittest commands without modifying or
    executing them, and enforce nonempty discovery for each registration.
    """
    registry_path = Path(registry_path).resolve()
    root = registry_path.parent.parent
    registry = json.loads(registry_path.read_text())
    combined = unittest.TestSuite()
    invalid = False
    suites = [suite for suite in registry["suites"]
              if suite.get("enabled") and suite.get("origin") == "adversarial-security"]
    for suite in suites:
        name = suite["id"]
        missing = [filename for filename in suite["requirements"]["files"]
                   if not (root / filename).is_file()]
        if missing:
            print(f"{name}: missing required files: {', '.join(missing)}", file=sys.stderr)
            invalid = True
            continue
        command = suite["command"]
        if command[:4] == ["python3", "-m", "unittest", "discover"]:
            arguments = command[4:]
        elif command[:2] == ["python3", "scripts/run_security_tests.py"]:
            arguments = command[2:]
        else:
            print(f"{name}: unsupported security discovery command", file=sys.stderr)
            invalid = True
            continue
        if len(arguments) != 4 or arguments[::2] != ["-s", "-p"]:
            print(f"{name}: expected explicit -s and -p discovery arguments", file=sys.stderr)
            invalid = True
            continue
        loader = unittest.TestLoader()
        discovered = loader.discover(str(root / arguments[1]), pattern=arguments[3])
        if discovered.countTestCases() == 0:
            print(f"{name}: No security tests were collected", file=sys.stderr)
            invalid = True
        combined.addTests(discovered)
    return None if invalid else combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--start-directory")
    parser.add_argument("-p", "--pattern")
    parser.add_argument("--registry", type=Path,
                        help="validate and run all enabled security registrations (default)")
    args = parser.parse_args()
    if args.registry and (args.start_directory or args.pattern):
        parser.error("--registry cannot be combined with -s or -p")
    if args.start_directory or args.pattern:
        suite = unittest.TestLoader().discover(
            args.start_directory or "tests/adversarial/security",
            pattern=args.pattern or "test_*.py",
        )
    else:
        registry = args.registry or Path(__file__).resolve().parents[1] / ".swarm/tests.json"
        suite = registered_tests(registry)
        if suite is None:
            return 2
    if suite.countTestCases() == 0:
        print("No security tests were collected; refusing an empty acceptance suite.", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
