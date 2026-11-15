# Smoke mission spec

`solution.py` in this directory must define:

    def add(a: int, b: int) -> int

returning the sum of the two integers. Nothing else is required. The trusted
gate imports the module and asserts add(2, 3) == 5 and add(0, 0) == 0.
