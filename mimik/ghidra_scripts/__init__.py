"""Ghidra scripts shipped with MIMIK (v0.5 items 5-6).

MimikExport.py runs INSIDE Ghidra (Jython 2.7) via analyzeHeadless
-postScript. It is written in Python 2/3-compatible syntax so it
parses under both Jython and CPython (the test suite checks that with
ast.parse). MIMIK passes the script dir with -scriptPath and the
output dir as the first script argument.
"""
