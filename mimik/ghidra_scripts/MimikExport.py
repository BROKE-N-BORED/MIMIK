# MimikExport.py — Ghidra export script for MIMIK (v0.5 items 5-6).
#
# Runs INSIDE Ghidra (Jython 2.7) via analyzeHeadless -postScript.
# Arguments: getScriptArgs()[0] is the output directory MIMIK reads
# back. Writes:
#   functions.json  — every function: address, name, size, and
#                     whether decompiled C / disassembly were exported
#   functions/<addr>_<name>.c    — decompiled C, one file per function
#   functions/<addr>_<name>.asm  — disassembly, one file per function
#   callgraph.json  — caller -> callee edges + address -> name map
#   xrefs.json      — defined strings + the functions referencing them
#
# Per-function try/except: one bad function never kills the export.
# Capped at 5000 functions so a huge binary cannot fill the disk.
# Python 2/3-compatible syntax (no f-strings, no keyword-only args).

import json
import os
import re

from ghidra.app.decompiler import DecompInterface

MAX_FUNCTIONS = 5000


def _safe_name(name):
    """A function name made safe for a filename."""
    return re.sub(r"[^A-Za-z0-9_.\-]+", "_", name)


def _addr_hex(address):
    return "0x" + address.toString()


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def run():
    args = getScriptArgs()
    if not args:
        print("MimikExport: no output directory given, nothing to do")
        return
    out_dir = args[0]
    func_dir = os.path.join(out_dir, "functions")
    if not os.path.isdir(func_dir):
        os.makedirs(func_dir)

    program = currentProgram
    listing = program.getListing()
    function_manager = program.getFunctionManager()
    ref_manager = program.getReferenceManager()

    decomp = DecompInterface()
    decomp.openProgram(program)

    functions = []
    names = {}
    edges = []
    count = 0
    for func in function_manager.getFunctions(True):
        if count >= MAX_FUNCTIONS:
            print("MimikExport: hit the %d-function cap, stopping"
                  % MAX_FUNCTIONS)
            break
        count += 1
        addr = _addr_hex(func.getEntryPoint())
        name = func.getName()
        names[addr] = name
        base = os.path.join(
            func_dir, "%s_%s" % (addr[2:], _safe_name(name)))
        has_c = False
        has_asm = False
        try:
            result = decomp.decompileFunction(func, 60, monitor)
            if result is not None and result.decompileCompleted():
                decompiled = result.getDecompiledFunction()
                if decompiled is not None:
                    _write(base + ".c", decompiled.getC())
                    has_c = True
        except Exception as exc:  # noqa: BLE001 - one bad function
            print("MimikExport: decompile failed for %s: %s"
                  % (name, exc))
        try:
            lines = []
            it = listing.getInstructions(func.getBody(), True)
            while it.hasNext():
                instr = it.next()
                lines.append("%s: %s"
                             % (_addr_hex(instr.getAddress()),
                                instr.toString()))
            _write(base + ".asm", "\n".join(lines) + "\n")
            has_asm = True
        except Exception as exc:  # noqa: BLE001 - one bad function
            print("MimikExport: disassembly failed for %s: %s"
                  % (name, exc))
        try:
            called = func.getCalledFunctions(monitor)
            for callee in called:
                edges.append([addr, _addr_hex(callee.getEntryPoint())])
        except Exception as exc:  # noqa: BLE001 - one bad function
            print("MimikExport: call graph failed for %s: %s"
                  % (name, exc))
        functions.append({
            "address": addr,
            "name": name,
            "size": func.getBody().getNumAddresses(),
            "decompiled": has_c,
            "disassembly": has_asm,
        })
        if count % 100 == 0:
            print("MimikExport: %d functions exported" % count)

    _write_json(os.path.join(out_dir, "functions.json"), functions)
    _write_json(os.path.join(out_dir, "callgraph.json"),
                {"edges": edges, "names": names})

    strings = []
    try:
        data_it = listing.getData(True)
        while data_it.hasNext():
            data = data_it.next()
            if not data.hasStringValue():
                continue
            value = data.getValue()
            if value is None:
                continue
            text = value.toString()
            if len(text) < 4:
                continue
            addr = _addr_hex(data.getAddress())
            callers = []
            for ref in ref_manager.getReferencesTo(data.getAddress()):
                holder = listing.getFunctionContaining(
                    ref.getFromAddress())
                if holder is not None:
                    callers.append(_addr_hex(holder.getEntryPoint()))
            strings.append({
                "address": addr,
                "text": text,
                "referenced_by": sorted(set(callers)),
            })
    except Exception as exc:  # noqa: BLE001 - strings are best-effort
        print("MimikExport: string xrefs failed: %s" % exc)
    _write_json(os.path.join(out_dir, "xrefs.json"),
                {"strings": strings})

    print("MimikExport: done, %d functions, %d call edges, %d strings"
          % (len(functions), len(edges), len(strings)))


run()
