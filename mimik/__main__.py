"""python3 -m mimik — the same entry point as the `mimik` command.

(v0.8 B8: the package used to refuse direct execution —
"'mimik' is a package and cannot be directly executed".)
"""
import sys

from .cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
