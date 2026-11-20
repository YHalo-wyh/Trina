import sys
from puzzle import valid

if len(sys.argv) != 2:
    raise SystemExit("usage: python3 check.py '<candidate>'")
print("OK" if valid(sys.argv[1]) else "FAIL")
