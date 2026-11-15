"""Binary store + stdlib static analysis (v0.5 items 1-2, 7, 10).

The analyzed binary is DATA, never executed: MIMIK reads its bytes
and parses its headers. No Ghidra needed for any of this — the stdlib
pass (ELF + PE headers, sections, imports/exports, strings, entropy)
works on its own; Ghidra (mimik/ghidra.py) adds decompilation,
disassembly, and call graphs on top of the same analysis record.

Layout (all under the data dir, overridable with MIMIK_DATA_DIR):
    binaries/<sha256>/meta.json       what the file is + where it came from
    binaries/<sha256>/analysis.json   the stdlib (+ Ghidra) analysis
    binaries/<sha256>/ghidra/         per-function decompile/disassembly
    binaries/links.json               host -> binary sha256 (manual links)

The binary itself is NOT copied into the store: a 500 MB copy per
analysis would eat the disk. The record keeps the source path; if the
file moves, re-analyze from its new path.
"""
import hashlib
import json
import math
import os
import re
import struct
from collections import Counter
from datetime import datetime

from . import log, store

MAX_BINARY_BYTES = 500 * 1024 * 1024  # 500 MB guard, same shape as assets
MAX_STRINGS = 50000  # strings kept per analysis (the rest: truncated flag)
MIN_STRING_LEN = 4
PACKED_ENTROPY = 7.5  # sections at/above this look packed or encrypted

_ASCII_RUN = re.compile(rb"[\x20-\x7e]{%d,}" % MIN_STRING_LEN)
_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % MIN_STRING_LEN)


class BinaryError(RuntimeError):
    """Anything that stops a binary being identified or analyzed."""


# ---- identification + store -------------------------------------------------

def identify(data):
    """'elf' | 'pe' | 'macho' | 'unknown' from magic bytes.

    PE needs the MZ header AND the PE signature at e_lfanew — a bare
    MZ is a DOS stub, not a Windows binary.
    """
    if len(data) >= 4 and data[:4] == b"\x7fELF":
        return "elf"
    if len(data) >= 2 and data[:2] == b"MZ" and len(data) >= 0x40:
        (pe_off,) = struct.unpack_from("<I", data, 0x3C)
        if 0 <= pe_off <= len(data) - 4 and data[pe_off:pe_off + 4] == b"PE\x00\x00":
            return "pe"
    if len(data) >= 4:
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic in (0xFEEDFACE, 0xFEEDFACF, 0xCAFEBABE, 0xCAFEBABF,
                     0xCEFAEDFE, 0xCFFAEDFE, 0xBEBAFECA):
            return "macho"
    return "unknown"


def sha256_file(path):
    """SHA-256 of a file, streamed (never loads the whole file)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def binaries_dir(base=None):
    return os.path.join(base or store.data_dir(), "binaries")


def analysis_dir(sha, base=None):
    return os.path.join(binaries_dir(base), sha)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def store_binary(path, base=None):
    """Identify + register a binary. Returns (sha256, analysis_dir).

    Size guard: files over 500 MB are refused with a plain error.
    Writes meta.json (0600, atomic); the analysis itself is written
    by analyze(). Re-registering the same file refreshes the meta.
    """
    base = base or store.data_dir()
    if not os.path.isfile(path):
        raise BinaryError(f"no such file: {path}")
    size = os.path.getsize(path)
    if size > MAX_BINARY_BYTES:
        raise BinaryError(
            f"{os.path.basename(path)} is {store.format_size(size)}, "
            f"over the {store.format_size(MAX_BINARY_BYTES)} limit")
    with open(path, "rb") as f:
        head = f.read(4096)
    fmt = identify(head)
    sha = sha256_file(path)
    adir = analysis_dir(sha, base)
    store.ensure_private_dir(binaries_dir(base))
    store.ensure_private_dir(adir)
    meta = {
        "sha256": sha,
        "source_path": os.path.abspath(path),
        "source_name": os.path.basename(path),
        "size": size,
        "format": fmt,
        "stored_at": _now_iso(),
    }
    store.atomic_write(os.path.join(adir, "meta.json"),
                       json.dumps(meta, indent=2))
    return sha, adir


def _read_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_meta(sha, base=None):
    """The meta.json for a sha256, or None."""
    return _read_json(os.path.join(analysis_dir(sha, base), "meta.json"))


def load_analysis(sha, base=None):
    """The analysis.json for a sha256, or None."""
    return _read_json(
        os.path.join(analysis_dir(sha, base), "analysis.json"))


def resolve_binary(identifier, base=None):
    """A sha256 (or its unique prefix) or a source basename -> sha256,
    or None. Ambiguous prefixes / names resolve to nothing: the caller
    must say which one."""
    base = base or store.data_dir()
    ident = (identifier or "").strip()
    if not ident:
        return None
    found = list_binaries(base)
    exact = [b for b in found if b["sha256"] == ident]
    if exact:
        return exact[0]["sha256"]
    if all(c in "0123456789abcdef" for c in ident.lower()) and len(ident) >= 6:
        pref = [b for b in found
                if b["sha256"].startswith(ident.lower())]
        if len(pref) == 1:
            return pref[0]["sha256"]
    by_name = [b for b in found if b["source_name"] == ident]
    if len(by_name) == 1:
        return by_name[0]["sha256"]
    return None


def list_binaries(base=None):
    """[{sha256, source_path, source_name, format, size, stored_at,
    analyzed_at, has_ghidra}] — newest stored first."""
    base = base or store.data_dir()
    bdir = binaries_dir(base)
    if not os.path.isdir(bdir):
        return []
    out = []
    for sha in sorted(os.listdir(bdir)):
        meta = load_meta(sha, base)
        if meta is None:
            continue
        analysis = load_analysis(sha, base)
        out.append({
            "sha256": sha,
            "source_path": meta.get("source_path", ""),
            "source_name": meta.get("source_name", sha[:12]),
            "format": meta.get("format", "unknown"),
            "size": meta.get("size", 0),
            "stored_at": meta.get("stored_at", ""),
            "analyzed_at": (analysis or {}).get("analyzed_at", ""),
            "has_ghidra": bool((analysis or {}).get("ghidra")),
        })
    return sorted(out, key=lambda b: b["stored_at"], reverse=True)


def delete_binary(sha, base=None):
    """Delete one binary's analysis dir. Returns True when something
    was deleted. The source file itself is never touched."""
    import shutil

    adir = analysis_dir(sha, base)
    if not os.path.isdir(adir):
        return False
    shutil.rmtree(adir)
    return True


# ---- strings + entropy ------------------------------------------------------

def section_entropy(data):
    """Shannon entropy of bytes, 0.0-8.0. Empty data scores 0.0."""
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    entropy = 0.0
    for count in counts.values():
        p = count / total
        entropy -= p * math.log2(p)
    return round(entropy, 2)


def extract_strings(data, min_len=MIN_STRING_LEN, limit=MAX_STRINGS):
    """(strings, truncated): [{"offset", "text", "encoding"}] — ASCII
    runs and UTF-16LE runs, in file order. Runs shorter than min_len
    are skipped; beyond `limit` the rest are dropped and truncated
    is True. Offsets are file offsets."""
    found = []
    for m in _ASCII_RUN.finditer(data):
        found.append((m.start(), m.group().decode("ascii"), "ascii"))
    for m in _UTF16_RUN.finditer(data):
        text = m.group().decode("utf-16-le")
        found.append((m.start(), text, "utf-16"))
    found.sort(key=lambda item: item[0])
    truncated = len(found) > limit
    return ([{"offset": off, "text": text, "encoding": enc}
             for off, text, enc in found[:limit]], truncated)


# ---- ELF --------------------------------------------------------------------

_ELF_MACHINES = {
    3: "x86", 62: "x86-64", 183: "AArch64", 40: "ARM", 243: "RISC-V",
    8: "MIPS", 20: "PowerPC", 21: "PowerPC64", 50: "IA-64",
}
_ELF_TYPES = {0: "none", 1: "relocatable", 2: "executable",
              3: "shared object", 4: "core"}
_ELF_PH_TYPES = {0: "NULL", 1: "LOAD", 2: "DYNAMIC", 3: "INTERP",
                 4: "NOTE", 5: "SHLIB", 6: "PHDR", 7: "TLS"}
_ELF_SH_TYPES = {0: "NULL", 1: "PROGBITS", 2: "SYMTAB", 3: "STRTAB",
                 4: "RELA", 5: "HASH", 6: "DYNAMIC", 7: "NOTE",
                 8: "NOBITS", 9: "REL", 11: "DYNSYM", 14: "INIT_ARRAY",
                 15: "FINI_ARRAY", 16: "PREINIT_ARRAY"}


def _elf_cstr(blob, offset):
    """A null-terminated string at offset in blob, or ''."""
    if offset < 0 or offset >= len(blob):
        return ""
    end = blob.find(b"\x00", offset)
    raw = blob[offset:] if end < 0 else blob[offset:end]
    return raw.decode("utf-8", "replace")


def _ph_flags_text(flags):
    return "".join(c for c, bit in (("R", 4), ("W", 2), ("X", 1))
                   if flags & bit) or "-"


def parse_elf(data):
    """Parse ELF headers/sections/symbols from bytes.

    Returns a dict; a malformed file yields what could be parsed plus
    parse_notes naming what failed — never an exception for bad bytes
    (the caller already knows the format is ELF from the magic).
    """
    notes = []
    result = {"class": None, "data": None, "type": None, "type_name": None,
              "machine": None, "entry_point": None, "program_headers": [],
              "dynamic": {"needed": []}}
    sections = []
    imports, exports = [], []
    if len(data) < 64:
        notes.append("file too short for an ELF header")
        return result, sections, imports, exports, notes
    ei_class, ei_data = data[4], data[5]
    is64 = ei_class == 2
    endian = "<" if ei_data != 2 else ">"
    result["class"] = 64 if is64 else 32
    result["data"] = "little" if ei_data != 2 else "big"
    try:
        if is64:
            (e_type, e_machine, _ver, e_entry, e_phoff, e_shoff,
             _flags, _ehsize, e_phentsize, e_phnum, e_shentsize,
             e_shnum, e_shstrndx) = struct.unpack_from(
                endian + "HHIQQQIHHHHHH", data, 16)
        else:
            (e_type, e_machine, _ver, e_entry, e_phoff, e_shoff,
             _flags, _ehsize, e_phentsize, e_phnum, e_shentsize,
             e_shnum, e_shstrndx) = struct.unpack_from(
                endian + "HHIIIIIHHHHHH", data, 16)
    except struct.error:
        notes.append("could not read the ELF header")
        return result, sections, imports, exports, notes
    result["type"] = _ELF_TYPES.get(e_type, f"type {e_type}")
    result["type_name"] = result["type"]
    result["machine"] = _ELF_MACHINES.get(e_machine, f"machine {e_machine}")
    result["entry_point"] = e_entry

    # Program headers.
    ph_size = 56 if is64 else 32
    for i in range(min(e_phnum, 256)):
        off = e_phoff + i * (e_phentsize or ph_size)
        try:
            if is64:
                (p_type, p_flags, p_offset, p_vaddr, _paddr,
                 p_filesz, _memsz, _align) = struct.unpack_from(
                    endian + "IIQQQQQQ", data, off)
            else:
                (p_type, p_offset, p_vaddr, _paddr, p_filesz,
                 _memsz, p_flags, _align) = struct.unpack_from(
                    endian + "IIIIIIII", data, off)
        except struct.error:
            notes.append(f"program header {i} is truncated")
            break
        result["program_headers"].append({
            "type": _ELF_PH_TYPES.get(p_type, f"type {p_type}"),
            "flags": _ph_flags_text(p_flags),
            "offset": p_offset, "vaddr": p_vaddr, "filesz": p_filesz,
        })

    # Section headers + names.
    sh_size = 64 if is64 else 40
    raw_sections = []
    for i in range(min(e_shnum, 512)):
        off = e_shoff + i * (e_shentsize or sh_size)
        try:
            if is64:
                (sh_name, sh_type, _flags, sh_addr, sh_offset, sh_size_,
                 sh_link, _info, _align, sh_entsize) = struct.unpack_from(
                    endian + "IIQQQQIIQQ", data, off)
            else:
                (sh_name, sh_type, _flags, sh_addr, sh_offset, sh_size_,
                 sh_link, _info, _align, sh_entsize) = struct.unpack_from(
                    endian + "IIIIIIIIII", data, off)
        except struct.error:
            notes.append(f"section header {i} is truncated")
            break
        raw_sections.append({
            "name_off": sh_name, "type": sh_type, "addr": sh_addr,
            "offset": sh_offset, "size": sh_size_, "link": sh_link,
            "entsize": sh_entsize,
        })
    shstr = b""
    if 0 <= e_shstrndx < len(raw_sections):
        s = raw_sections[e_shstrndx]
        shstr = data[s["offset"]:s["offset"] + s["size"]]
    for s in raw_sections:
        name = _elf_cstr(shstr, s["name_off"])
        body = data[s["offset"]:s["offset"] + s["size"]] \
            if s["type"] != 8 else b""  # NOBITS has no file bytes
        ent = section_entropy(body)
        sections.append({
            "name": name or "(unnamed)",
            "type": _ELF_SH_TYPES.get(s["type"], f"type {s['type']}"),
            "address": s["addr"], "offset": s["offset"],
            "size": s["size"], "entropy": ent,
            "packed": ent >= PACKED_ENTROPY and s["size"] >= 256,
        })
    by_name = {s["name"]: s for s in sections}
    raw_by_name = {}
    for raw, sec in zip(raw_sections, sections):
        raw_by_name[sec["name"]] = raw

    # Dynamic symbols: undefined = imports, defined globals = exports.
    dynsym = raw_by_name.get(".dynsym")
    if dynsym is not None:
        link = dynsym["link"]
        strtab = b""
        if 0 <= link < len(raw_sections):
            st = raw_sections[link]
            strtab = data[st["offset"]:st["offset"] + st["size"]]
        entsize = dynsym["entsize"] or (24 if is64 else 16)
        count = dynsym["size"] // entsize if entsize else 0
        for i in range(min(count, 100000)):
            off = dynsym["offset"] + i * entsize
            try:
                if is64:
                    (st_name, st_info, _other, st_shndx,
                     _value, _size) = struct.unpack_from(
                        endian + "IBBHQQ", data, off)
                else:
                    (st_name, _value, _size, st_info, _other,
                     st_shndx) = struct.unpack_from(
                        endian + "IIIBBH", data, off)
            except struct.error:
                break
            name = _elf_cstr(strtab, st_name)
            if not name:
                continue
            bind = st_info >> 4
            if st_shndx == 0:
                if name not in imports:
                    imports.append(name)
            elif bind != 0:  # defined, non-local
                if name not in exports:
                    exports.append(name)

    # DT_NEEDED libraries from .dynamic.
    dynamic = raw_by_name.get(".dynamic")
    dynstr = raw_by_name.get(".dynstr")
    if dynamic is not None and dynstr is not None:
        strtab = data[dynstr["offset"]:dynstr["offset"] + dynstr["size"]]
        entsize = dynamic["entsize"] or (16 if is64 else 8)
        count = dynamic["size"] // entsize if entsize else 0
        for i in range(min(count, 10000)):
            off = dynamic["offset"] + i * entsize
            try:
                if is64:
                    (tag, val) = struct.unpack_from(endian + "qQ", data, off)
                else:
                    (tag, val) = struct.unpack_from(endian + "iI", data, off)
            except struct.error:
                break
            if tag == 0:
                break
            if tag == 1:  # DT_NEEDED
                lib = _elf_cstr(strtab, val)
                if lib and lib not in result["dynamic"]["needed"]:
                    result["dynamic"]["needed"].append(lib)
    return result, sections, imports, exports, notes


# ---- PE ---------------------------------------------------------------------

_PE_MACHINES = {
    0x14C: "x86", 0x8664: "x86-64", 0xAA64: "ARM64", 0x1C0: "ARM",
    0x1C4: "ARMv7", 0x200: "IA-64",
}
_PE_SUBSYSTEMS = {
    1: "native", 2: "GUI", 3: "console", 5: "OS/2 console",
    6: "POSIX console", 9: "Windows CE GUI", 10: "EFI application",
}


def _pe_cstr(data, offset):
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\x00", offset)
    raw = data[offset:] if end < 0 else data[offset:end]
    return raw.decode("utf-8", "replace")


def parse_pe(data):
    """Parse PE headers/sections/imports/exports from bytes.

    Same contract as parse_elf: partial results + parse_notes on
    malformed input, never an exception for bad bytes.
    """
    notes = []
    result = {"machine": None, "characteristics": [],
              "entry_point": None, "image_base": None, "subsystem": None}
    sections = []
    imports, exports = [], []
    import_libs = []
    if len(data) < 0x40:
        notes.append("file too short for a DOS header")
        return result, sections, imports, exports, import_libs, notes
    (pe_off,) = struct.unpack_from("<I", data, 0x3C)
    if pe_off + 24 > len(data):
        notes.append("PE header offset is past the end of the file")
        return result, sections, imports, exports, import_libs, notes
    try:
        (machine, n_sections, _stamp, _symtab, _nsyms,
         opt_size, _chars) = struct.unpack_from("<HHIIIHH", data, pe_off + 4)
    except struct.error:
        notes.append("could not read the COFF header")
        return result, sections, imports, exports, import_libs, notes
    result["machine"] = _PE_MACHINES.get(machine, f"machine 0x{machine:04x}")
    opt_off = pe_off + 24
    if opt_size < 2 or opt_off + opt_size > len(data):
        notes.append("optional header is truncated")
        return result, sections, imports, exports, import_libs, notes
    (magic,) = struct.unpack_from("<H", data, opt_off)
    is64 = magic == 0x20B
    if magic not in (0x10B, 0x20B):
        notes.append(f"unknown optional-header magic 0x{magic:04x}")
        return result, sections, imports, exports, import_libs, notes
    try:
        (entry,) = struct.unpack_from("<I", data, opt_off + 16)
        if is64:
            (image_base,) = struct.unpack_from("<Q", data, opt_off + 24)
            (subsystem,) = struct.unpack_from("<H", data, opt_off + 68)
            (n_dirs,) = struct.unpack_from("<I", data, opt_off + 108)
            dirs_off = opt_off + 112
        else:
            (image_base,) = struct.unpack_from("<I", data, opt_off + 28)
            (subsystem,) = struct.unpack_from("<H", data, opt_off + 68)
            (n_dirs,) = struct.unpack_from("<I", data, opt_off + 92)
            dirs_off = opt_off + 96
    except struct.error:
        notes.append("could not read the optional header")
        return result, sections, imports, exports, import_libs, notes
    result["entry_point"] = entry
    result["image_base"] = image_base
    result["subsystem"] = _PE_SUBSYSTEMS.get(subsystem,
                                             f"subsystem {subsystem}")

    def _dir(index):
        """(rva, size) of data directory `index`, or (0, 0)."""
        if index >= n_dirs:
            return 0, 0
        try:
            return struct.unpack_from("<II", data, dirs_off + index * 8)
        except struct.error:
            return 0, 0

    # Section headers.
    sec_off = opt_off + opt_size
    raw_sections = []
    for i in range(min(n_sections, 96)):
        off = sec_off + i * 40
        try:
            (name_raw, vsize, vaddr, raw_size, raw_ptr,
             _a, _b, _c, _d, _chars) = struct.unpack_from(
                "<8sIIIIIIHHI", data, off)
        except struct.error:
            notes.append(f"section header {i} is truncated")
            break
        name = name_raw.split(b"\x00")[0].decode("utf-8", "replace")
        raw_sections.append({
            "name": name, "vaddr": vaddr, "vsize": vsize,
            "raw_size": raw_size, "raw_ptr": raw_ptr,
        })
        body = data[raw_ptr:raw_ptr + raw_size]
        ent = section_entropy(body)
        sections.append({
            "name": name or "(unnamed)", "type": "section",
            "address": (image_base or 0) + vaddr, "offset": raw_ptr,
            "size": raw_size, "entropy": ent,
            "packed": ent >= PACKED_ENTROPY and raw_size >= 256,
        })

    def _rva_to_off(rva):
        """File offset for an RVA, or None."""
        for s in raw_sections:
            span = max(s["vsize"], s["raw_size"])
            if s["vaddr"] <= rva < s["vaddr"] + span:
                return s["raw_ptr"] + (rva - s["vaddr"])
        if rva < sec_off:
            return rva  # headers map 1:1
        return None

    # Imports (directory 1).
    imp_rva, _imp_size = _dir(1)
    if imp_rva:
        off = _rva_to_off(imp_rva)
        if off is None:
            notes.append("import directory points outside the file")
        else:
            for i in range(512):
                try:
                    (oft, _stamp, _fwd, name_rva,
                     ft) = struct.unpack_from("<IIIII", data, off + i * 20)
                except struct.error:
                    break
                if oft == 0 and name_rva == 0 and ft == 0:
                    break
                name_off = _rva_to_off(name_rva)
                dll = _pe_cstr(data, name_off) if name_off is not None else ""
                thunk_rva = oft or ft
                thunk_off = _rva_to_off(thunk_rva)
                funcs = []
                if thunk_off is not None:
                    step = 8 if is64 else 4
                    for j in range(100000):
                        try:
                            if is64:
                                (val,) = struct.unpack_from(
                                    "<Q", data, thunk_off + j * step)
                            else:
                                (val,) = struct.unpack_from(
                                    "<I", data, thunk_off + j * step)
                        except struct.error:
                            break
                        if val == 0:
                            break
                        ordinal_bit = (1 << 63) if is64 else (1 << 31)
                        if val & ordinal_bit:
                            funcs.append(f"#{val & 0xFFFF}")
                        else:
                            hn_off = _rva_to_off(val & 0x7FFFFFFF)
                            if hn_off is not None:
                                fname = _pe_cstr(data, hn_off + 2)
                                if fname:
                                    funcs.append(fname)
                if dll:
                    import_libs.append(dll)
                    for fn in funcs:
                        imports.append(f"{dll}!{fn}")

    # Exports (directory 0).
    exp_rva, _exp_size = _dir(0)
    if exp_rva:
        off = _rva_to_off(exp_rva)
        if off is None:
            notes.append("export directory points outside the file")
        else:
            try:
                (_c, _t, _maj, _min, _name, _base, _nfuncs,
                 n_names, _addrs, names_rva, _ords) = struct.unpack_from(
                    "<IIHHIIIIIII", data, off)
            except struct.error:
                n_names, names_rva = 0, 0
            names_off = _rva_to_off(names_rva) if names_rva else None
            if names_off is not None:
                for i in range(min(n_names, 100000)):
                    try:
                        (name_ptr,) = struct.unpack_from(
                            "<I", data, names_off + i * 4)
                    except struct.error:
                        break
                    str_off = _rva_to_off(name_ptr)
                    if str_off is not None:
                        name = _pe_cstr(data, str_off)
                        if name:
                            exports.append(name)
    return result, sections, imports, exports, import_libs, notes


# ---- analysis ---------------------------------------------------------------

def analyze(path, base=None, progress=None):
    """Run the stdlib pass on a binary and save analysis.json.

    progress(text) is called at each stage (GUI worker). Returns the
    analysis dict. Raises BinaryError for a missing/oversize file.
    The binary is read, never executed.
    """
    def _say(text):
        if progress is not None:
            progress(text)

    _say("identifying the file")
    sha, adir = store_binary(path, base)
    base = base or store.data_dir()
    with open(path, "rb") as f:
        data = f.read()
    meta = load_meta(sha, base)
    fmt = meta["format"]
    analysis = {
        "format": fmt,
        "sha256": sha,
        "source_path": meta["source_path"],
        "source_name": meta["source_name"],
        "size": meta["size"],
        "analyzed_at": _now_iso(),
        "sections": [],
        "imports": [],
        "import_libraries": [],
        "exports": [],
        "strings": [],
        "strings_total": 0,
        "strings_truncated": False,
        "parse_notes": [],
        "ghidra": None,
    }
    _say("parsing headers")
    if fmt == "elf":
        elf, sections, imports, exports, notes = parse_elf(data)
        analysis["elf"] = elf
        analysis["sections"] = sections
        analysis["imports"] = imports
        analysis["exports"] = exports
        analysis["import_libraries"] = elf["dynamic"]["needed"]
        analysis["parse_notes"] = notes
    elif fmt == "pe":
        pe, sections, imports, exports, libs, notes = parse_pe(data)
        analysis["pe"] = pe
        analysis["sections"] = sections
        analysis["imports"] = imports
        analysis["exports"] = exports
        analysis["import_libraries"] = libs
        analysis["parse_notes"] = notes
    elif fmt == "macho":
        analysis["parse_notes"] = [
            "Mach-O headers need Ghidra: run the full analysis for "
            "sections, imports, and exports.",
        ]
    else:
        analysis["parse_notes"] = [
            "Unknown format: no headers to parse. Strings below are "
            "still real.",
        ]
    _say("extracting strings")
    strings, truncated = extract_strings(data)
    analysis["strings"] = strings
    analysis["strings_total"] = len(strings)
    analysis["strings_truncated"] = truncated
    if truncated:
        analysis["parse_notes"].append(
            f"strings capped at {MAX_STRINGS}: the rest were dropped")
    store.atomic_write(os.path.join(adir, "analysis.json"),
                       json.dumps(analysis, indent=2))
    log.log_info("binary analyze",
                 f"{meta['source_name']} ({fmt}, "
                 f"{store.format_size(meta['size'])}): "
                 f"{len(analysis['sections'])} sections, "
                 f"{len(analysis['imports'])} imports, "
                 f"{len(strings)} strings")
    return analysis


def update_ghidra(sha, ghidra_result, base=None):
    """Attach a Ghidra result to an existing analysis (item 4-6).
    Returns the updated analysis, or None when there is none."""
    base = base or store.data_dir()
    analysis = load_analysis(sha, base)
    if analysis is None:
        return None
    analysis["ghidra"] = ghidra_result
    store.atomic_write(
        os.path.join(analysis_dir(sha, base), "analysis.json"),
        json.dumps(analysis, indent=2))
    return analysis


def function_files(sha, address, name, base=None):
    """(c_path, asm_path) for one exported function, or (None, None)
    per file when it is not there. Filenames match MimikExport.py:
    <addr without 0x>_<safe name>.c / .asm in the analysis dir's
    functions/ folder."""
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name)
    addr = address[2:] if address.startswith("0x") else address
    stem = os.path.join(analysis_dir(sha, base), "functions",
                        f"{addr}_{safe}")
    c_path = stem + ".c"
    asm_path = stem + ".asm"
    return (c_path if os.path.isfile(c_path) else None,
            asm_path if os.path.isfile(asm_path) else None)


# ---- links to captures (item 10) --------------------------------------------

def _links_path(base=None):
    return os.path.join(binaries_dir(base), "links.json")


def get_links(base=None):
    """{host: sha256} manual links, or {}."""
    data = _read_json(_links_path(base))
    if not data:
        return {}
    links = data.get("links")
    return links if isinstance(links, dict) else {}


def link_binary(host, sha, base=None):
    """Link a host's captures to an analyzed binary (manual)."""
    base = base or store.data_dir()
    store.ensure_private_dir(binaries_dir(base))
    links = get_links(base)
    links[host] = sha
    store.atomic_write(_links_path(base),
                       json.dumps({"links": links}, indent=2))
    return links


def unlink_binary(host, base=None):
    """Drop a host's manual binary link. Returns True when one went."""
    base = base or store.data_dir()
    links = get_links(base)
    if host not in links:
        return False
    del links[host]
    store.atomic_write(_links_path(base),
                       json.dumps({"links": links}, indent=2))
    return True


def binary_for_host(host, base=None):
    """The sha256 linked to a host's captures, or None.

    Manual link first; otherwise auto-match: the capture's recorded
    launch target path equals a binary's source path, or its basename
    matches exactly one analyzed binary. A basename shared by two
    binaries matches nothing — the user links it by hand.
    """
    base = base or store.data_dir()
    links = get_links(base)
    if host in links and load_meta(links[host], base) is not None:
        return links[host]
    meta = store.capture_meta(host, base)
    launched = meta.get("launched_file") or ""
    if not launched:
        return None
    found = list_binaries(base)
    for b in found:
        if b["source_path"] == os.path.abspath(launched):
            return b["sha256"]
    name = os.path.basename(launched)
    by_name = [b for b in found if b["source_name"] == name]
    if len(by_name) == 1:
        return by_name[0]["sha256"]
    return None
