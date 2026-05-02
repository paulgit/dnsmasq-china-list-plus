# AGENTS.md

## Project Overview
Simple Python script development environment. Scripts are standalone, focused tools that do one thing well.

## Commands

### Run a script
python script_name.py

### Run with arguments
python script_name.py --arg value

### Install dependencies
pip install package_name

### Run tests
python -m pytest tests/ -v

### Check types
mypy script_name.py

### Lint
ruff check .

## Code Style

- **Python version**: 3.11+
- **Formatter**: ruff format (88 char line length)
- **Linter**: ruff
- **Type hints**: required on all function signatures
- **Docstrings**: Google style, required on all public functions

## Script Structure

Each script should follow this pattern:

```python
#!/usr/bin/env python3
"""One-line description of what this script does."""

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    # ... logic here
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## Dependencies
- Keep dependencies minimal
- Pin versions in requirements.txt
- Prefer stdlib over third-party when reasonable

## Error Handling
- Use sys.exit(1) for fatal errors
- Print errors to stderr: print("error", file=sys.stderr)
- Catch specific exceptions, not bare except

## Testing
- One test file per script: test_scriptname.py
- Use pytest fixtures for setup
- Aim for happy path + at least one error case

## What NOT to do
- No global mutable state
- No hardcoded paths (use pathlib / argparse)
- No silent failures
- No print debugging left in final code
