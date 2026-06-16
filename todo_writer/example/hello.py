"""Example greeting module.

This module demonstrates a simple ``greet`` function which prints a
personalised greeting.  The original implementation was missing
documentation, type hints and a proper ``if __name__ == "__main__"`` block.

The refactored version below adds those features making the code
readable and safer to import.
"""

from __future__ import annotations

from typing import NoReturn


def greet(name: str) -> None:
    """Print a greeting message to ``stdout``.

    Parameters
    ----------
    name:
        The name of the person to greet.
    """
    message: str = f"Hello, {name}"
    print(message)


def _main() -> NoReturn:
    """Entry point used when the module is executed as a script.

    This simply calls :func:`greet` with a sample name.  A separate
    function is used to keep the global namespace clean and to give
    an explicit type hint for ```returntype``.
    """
    greet("Claude")
    raise SystemExit()


if __name__ == "__main__":  # pragma: no cover
    _main()
