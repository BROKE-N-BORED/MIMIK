"""Tests for mimik/binary.py — the binary store + stdlib static
analysis (v0.5 items 1-2, 7, 10).

Fixtures are GENERATED here, not checked in as binary files: the
GitHub snapshot push cannot carry binary (UTF-8 only), so a checked-in
.tiny-elf/.exe would be missing from a fresh clone and the suite would
fail there. The builders below construct minimal valid ELF + PE bytes
from struct packs — reviewable, deterministic, and push-safe.
"""
import hashlib
import json
import os
import random
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import binary, store


def _align(n, alignment):
    return (n + alignment - 1) // alignment * alignment


def build_tiny_elf(text_content=None):
    """Minimal valid 64-bit LE x86-64 ELF (ET_EXEC).

    Sections: .text (code + the "HelloMIMIK" string), .dynsym (imports
    printf + puts, export myfunc), .dynstr, .dynamic (DT_NEEDED
    libc.so.6), .shstrtab. text_content replaces the .text bytes (the
    packed-section test passes seeded random bytes).
    """
    code = b"\x55\x48\x89\xe5\xb8\x2a\x00\x00\x00\x5d\xc3"
    text = text_content if text_content is not None \
        else code + b"HelloMIMIK\x00"
    dynstr = b"\x00printf\x00puts\x00myfunc\x00libc.so.6\x00"

    def sym(name, info, shndx, value, size):
        return struct.pack("<IBBHQQ", name, info, 0, shndx, value, size)

    dynsym = b"".join([
        sym(0, 0, 0, 0, 0),
        sym(1, 0x12, 0, 0, 0),              # printf (import)
        sym(8, 0x12, 0, 0, 0),              # puts (import)
        sym(13, 0x12, 1, 0x401000, 11),     # myfunc (export)
    ])
    dynamic = struct.pack("<qQ", 1, 20) + struct.pack("<qQ", 0, 0)
    shstr = b"\x00.text\x00.dynsym\x00.dynstr\x00.dynamic\x00.shstrtab\x00"
    # shstrtab name offsets: .text=1, .dynsym=7, .dynstr=15, .dynamic=23,
    # .shstrtab=32.

    text_off, text_vaddr = 0x1000, 0x401000
    cursor = 0x2000
    dynstr_off = cursor
    cursor += len(dynstr)
    dynsym_off = _align(cursor, 8)
    cursor = dynsym_off + len(dynsym)
    dynamic_off = _align(cursor, 8)
    cursor = dynamic_off + len(dynamic)
    shstr_off = cursor
    cursor += len(shstr)
    shoff = _align(cursor, 8)
    total = shoff + 6 * 64

    buf = bytearray(total)
    # ELF header.
    buf[0:16] = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    struct.pack_into("<HHIQQQIHHHHHH", buf, 16,
                     2, 62, 1, 0x401000, 64, shoff, 0, 64, 56, 2, 64, 6, 5)
    # Program headers: PT_LOAD + PT_DYNAMIC.
    struct.pack_into("<IIQQQQQQ", buf, 64,
                     1, 5, 0, 0x400000, 0, total, total, 0x1000)
    struct.pack_into("<IIQQQQQQ", buf, 120,
                     2, 6, dynamic_off, 0x400000 + dynamic_off, 0,
                     len(dynamic), len(dynamic), 8)
    # Section contents.
    buf[text_off:text_off + len(text)] = text
    buf[dynstr_off:dynstr_off + len(dynstr)] = dynstr
    buf[dynsym_off:dynsym_off + len(dynsym)] = dynsym
    buf[dynamic_off:dynamic_off + len(dynamic)] = dynamic
    buf[shstr_off:shstr_off + len(shstr)] = shstr
    # Section headers (name, type, flags, addr, offset, size, link,
    # info, align, entsize).
    headers = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 6, text_vaddr, text_off, len(text), 0, 0, 16, 0),
        (7, 11, 2, 0x400000 + dynsym_off, dynsym_off, len(dynsym),
         3, 1, 8, 24),
        (15, 3, 2, 0x400000 + dynstr_off, dynstr_off, len(dynstr),
         0, 0, 1, 0),
        (23, 6, 3, 0x400000 + dynamic_off, dynamic_off, len(dynamic),
         3, 0, 8, 16),
        (32, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0),
    ]
    for i, h in enumerate(headers):
        struct.pack_into("<IIQQQQIIQQ", buf, shoff + i * 64, *h)
    return bytes(buf)


def build_tiny_pe():
    """Minimal valid 64-bit PE (x86-64, console).

    Sections: .text (code + the "HelloPE!" string), .rdata (export
    directory exporting TestFunc, import directory importing
    ExitProcess from KERNEL32.dll).
    """
    buf = bytearray(0x600)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", buf, 0x84,
                     0x8664, 2, 0, 0, 0, 240, 0x0022)
    opt = 0x98
    struct.pack_into("<H", buf, opt, 0x20B)
    buf[opt + 2] = 14
    struct.pack_into("<I", buf, opt + 4, 0x200)
    struct.pack_into("<I", buf, opt + 8, 0x200)
    struct.pack_into("<I", buf, opt + 12, 0)
    struct.pack_into("<I", buf, opt + 16, 0x1000)
    struct.pack_into("<I", buf, opt + 20, 0x1000)
    struct.pack_into("<Q", buf, opt + 24, 0x140000000)
    struct.pack_into("<I", buf, opt + 32, 0x1000)
    struct.pack_into("<I", buf, opt + 36, 0x200)
    struct.pack_into("<HHHHHH", buf, opt + 40, 6, 0, 0, 0, 6, 0)
    struct.pack_into("<I", buf, opt + 52, 0)
    struct.pack_into("<I", buf, opt + 56, 0x3000)
    struct.pack_into("<I", buf, opt + 60, 0x200)
    struct.pack_into("<I", buf, opt + 64, 0)
    struct.pack_into("<H", buf, opt + 68, 3)
    struct.pack_into("<H", buf, opt + 70, 0)
    struct.pack_into("<QQQQ", buf, opt + 72,
                     0x100000, 0x1000, 0x100000, 0x1000)
    struct.pack_into("<I", buf, opt + 104, 0)
    struct.pack_into("<I", buf, opt + 108, 16)
    struct.pack_into("<II", buf, opt + 112, 0x2000, 0x100)
    struct.pack_into("<II", buf, opt + 120, 0x2100, 0x100)
    struct.pack_into("<8sIIIIIIHHI", buf, 0x188,
                     b".text\x00\x00\x00", 0x100, 0x1000, 0x200, 0x200,
                     0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", buf, 0x188 + 40,
                     b".rdata\x00\x00", 0x200, 0x2000, 0x200, 0x400,
                     0, 0, 0, 0, 0x40000040)
    code = b"\x55\x48\x89\xe5\xb8\x2a\x00\x00\x00\x5d\xc3" + b"HelloPE!\x00"
    buf[0x200:0x200 + len(code)] = code
    # Export directory at file 0x400 (RVA 0x2000).
    struct.pack_into("<IIHHIIIIIII", buf, 0x400,
                     0, 0, 0, 0, 0x2028, 1, 1, 1, 0x2038, 0x203C, 0x2040)
    buf[0x428:0x428 + 9] = b"tiny.dll\x00"
    struct.pack_into("<I", buf, 0x438, 0x1000)
    struct.pack_into("<I", buf, 0x43C, 0x2048)
    struct.pack_into("<H", buf, 0x440, 0)
    buf[0x448:0x448 + 9] = b"TestFunc\x00"
    # Import directory at file 0x500 (RVA 0x2100).
    struct.pack_into("<IIIII", buf, 0x500, 0x2128, 0, 0, 0x2140, 0x2128)
    struct.pack_into("<QQ", buf, 0x528, 0x2150, 0)
    buf[0x540:0x540 + 13] = b"KERNEL32.dll\x00"
    struct.pack_into("<H", buf, 0x550, 0)
    buf[0x552:0x552 + 12] = b"ExitProcess\x00"
    return bytes(buf)


class IdentifyTests(unittest.TestCase):
    def test_elf(self):
        self.assertEqual(binary.identify(build_tiny_elf()), "elf")

    def test_pe(self):
        self.assertEqual(binary.identify(build_tiny_pe()), "pe")

    def test_macho_magics(self):
        for magic in (0xFEEDFACE, 0xFEEDFACF, 0xCAFEBABE, 0xCEFAEDFE):
            self.assertEqual(
                binary.identify(struct.pack("<I", magic) + b"\x00" * 100),
                "macho")

    def test_bare_mz_is_not_pe(self):
        data = bytearray(0x100)
        data[0:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 0x80)
        self.assertEqual(binary.identify(bytes(data)), "unknown")

    def test_unknown(self):
        self.assertEqual(binary.identify(b"hello world, not a binary"),
                         "unknown")
        self.assertEqual(binary.identify(b""), "unknown")
        self.assertEqual(binary.identify(b"\x7fEL"), "unknown")


class StringsTests(unittest.TestCase):
    def test_ascii_runs_with_offsets(self):
        data = b"\x00\x01ABCD\x00\x00longer string here\x00xy"
        strings, truncated = binary.extract_strings(data)
        texts = {s["text"]: s for s in strings}
        self.assertFalse(truncated)
        self.assertEqual(texts["ABCD"]["offset"], 2)
        self.assertEqual(texts["ABCD"]["encoding"], "ascii")
        self.assertEqual(texts["longer string here"]["offset"], 8)
        self.assertNotIn("xy", texts)  # shorter than the minimum

    def test_utf16_runs(self):
        data = b"Test\x00".replace(b"\x00", b"")  # ascii control
        data = "HelloUTF16".encode("utf-16-le") + b"\x00\x00"
        strings, _ = binary.extract_strings(data)
        self.assertEqual(len(strings), 1)
        self.assertEqual(strings[0]["text"], "HelloUTF16")
        self.assertEqual(strings[0]["encoding"], "utf-16")
        self.assertEqual(strings[0]["offset"], 0)

    def test_cap_truncates(self):
        data = b"".join(b"str%d\x00" % i for i in range(10))
        strings, truncated = binary.extract_strings(data, limit=3)
        self.assertTrue(truncated)
        self.assertEqual(len(strings), 3)


class EntropyTests(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(binary.section_entropy(b""), 0.0)

    def test_uniform_is_zero(self):
        self.assertEqual(binary.section_entropy(b"\x00" * 1000), 0.0)

    def test_full_range_is_eight(self):
        self.assertEqual(binary.section_entropy(bytes(range(256)) * 4),
                         8.0)

    def test_text_is_midrange(self):
        ent = binary.section_entropy(
            b"hello world, this is ordinary text. " * 20)
        self.assertGreater(ent, 3.0)
        self.assertLess(ent, 7.5)


class ElfParseTests(unittest.TestCase):
    def setUp(self):
        self.data = build_tiny_elf()

    def test_headers(self):
        elf, _s, _i, _e, notes = binary.parse_elf(self.data)
        self.assertEqual(notes, [])
        self.assertEqual(elf["class"], 64)
        self.assertEqual(elf["data"], "little")
        self.assertEqual(elf["type"], "executable")
        self.assertEqual(elf["machine"], "x86-64")
        self.assertEqual(elf["entry_point"], 0x401000)
        self.assertEqual(
            [p["type"] for p in elf["program_headers"]],
            ["LOAD", "DYNAMIC"])
        self.assertEqual(elf["dynamic"]["needed"], ["libc.so.6"])

    def test_sections(self):
        _elf, sections, _i, _e, _n = binary.parse_elf(self.data)
        names = [s["name"] for s in sections]
        self.assertEqual(names, ["(unnamed)", ".text", ".dynsym", ".dynstr",
                                 ".dynamic", ".shstrtab"])
        text = sections[1]
        self.assertEqual(text["address"], 0x401000)
        self.assertEqual(text["offset"], 0x1000)
        self.assertEqual(text["size"], 22)
        self.assertFalse(text["packed"])

    def test_imports_and_exports(self):
        _elf, _s, imports, exports, _n = binary.parse_elf(self.data)
        self.assertEqual(imports, ["printf", "puts"])
        self.assertEqual(exports, ["myfunc"])

    def test_packed_section_flagged(self):
        rng = random.Random(42)
        blob = bytes(rng.randrange(256) for _ in range(512))
        blob += b"HelloMIMIK\x00"
        _elf, sections, _i, _e, _n = binary.parse_elf(
            build_tiny_elf(text_content=blob))
        text = [s for s in sections if s["name"] == ".text"][0]
        self.assertGreaterEqual(text["entropy"], 7.5)
        self.assertTrue(text["packed"])

    def test_truncated_is_partial_not_fatal(self):
        elf, _s, _i, _e, notes = binary.parse_elf(self.data[:100])
        self.assertTrue(notes)
        self.assertEqual(elf["machine"], "x86-64")


class PeParseTests(unittest.TestCase):
    def setUp(self):
        self.data = build_tiny_pe()

    def test_headers(self):
        pe, _s, _i, _e, _l, notes = binary.parse_pe(self.data)
        self.assertEqual(notes, [])
        self.assertEqual(pe["machine"], "x86-64")
        self.assertEqual(pe["entry_point"], 0x1000)
        self.assertEqual(pe["image_base"], 0x140000000)
        self.assertEqual(pe["subsystem"], "console")

    def test_sections(self):
        _pe, sections, _i, _e, _l, _n = binary.parse_pe(self.data)
        self.assertEqual([s["name"] for s in sections],
                         [".text", ".rdata"])
        self.assertEqual(sections[0]["offset"], 0x200)
        self.assertEqual(sections[1]["offset"], 0x400)

    def test_imports_and_exports(self):
        _pe, _s, imports, exports, libs, _n = binary.parse_pe(self.data)
        self.assertEqual(libs, ["KERNEL32.dll"])
        self.assertEqual(imports, ["KERNEL32.dll!ExitProcess"])
        self.assertEqual(exports, ["TestFunc"])

    def test_truncated_is_partial_not_fatal(self):
        pe, _s, _i, _e, _l, notes = binary.parse_pe(self.data[:0xA0])
        self.assertTrue(notes)
        self.assertEqual(pe["machine"], "x86-64")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.elf_path = os.path.join(self.tmp, "tiny.elf")
        with open(self.elf_path, "wb") as f:
            f.write(build_tiny_elf())
        self.pe_path = os.path.join(self.tmp, "tiny.exe")
        with open(self.pe_path, "wb") as f:
            f.write(build_tiny_pe())

    def test_store_registers_with_meta_and_perms(self):
        sha, adir = binary.store_binary(self.elf_path, base=self.tmp)
        self.assertEqual(sha, hashlib.sha256(build_tiny_elf()).hexdigest())
        self.assertEqual(stat.S_IMODE(os.stat(adir).st_mode), 0o700)
        meta_path = os.path.join(adir, "meta.json")
        self.assertEqual(stat.S_IMODE(os.stat(meta_path).st_mode), 0o600)
        meta = binary.load_meta(sha, base=self.tmp)
        self.assertEqual(meta["format"], "elf")
        self.assertEqual(meta["source_name"], "tiny.elf")
        self.assertEqual(meta["size"], len(build_tiny_elf()))

    def test_store_missing_file(self):
        with self.assertRaises(binary.BinaryError):
            binary.store_binary(os.path.join(self.tmp, "nope"), base=self.tmp)

    def test_size_guard(self):
        with mock.patch.object(binary, "MAX_BINARY_BYTES", 10):
            with self.assertRaises(binary.BinaryError) as ctx:
                binary.store_binary(self.elf_path, base=self.tmp)
            self.assertIn("over the", str(ctx.exception))

    def test_analyze_elf_end_to_end(self):
        analysis = binary.analyze(self.elf_path, base=self.tmp)
        self.assertEqual(analysis["format"], "elf")
        self.assertEqual(analysis["imports"], ["printf", "puts"])
        self.assertEqual(analysis["exports"], ["myfunc"])
        self.assertEqual(analysis["import_libraries"], ["libc.so.6"])
        texts = {s["text"]: s for s in analysis["strings"]}
        self.assertIn("HelloMIMIK", texts)
        self.assertEqual(texts["HelloMIMIK"]["offset"], 0x1000 + 11)
        self.assertIn("printf", texts)
        self.assertIsNone(analysis["ghidra"])
        # Saved + reloadable.
        again = binary.load_analysis(analysis["sha256"], base=self.tmp)
        self.assertEqual(again["imports"], ["printf", "puts"])

    def test_analyze_pe_end_to_end(self):
        analysis = binary.analyze(self.pe_path, base=self.tmp)
        self.assertEqual(analysis["format"], "pe")
        self.assertEqual(analysis["imports"],
                         ["KERNEL32.dll!ExitProcess"])
        self.assertEqual(analysis["exports"], ["TestFunc"])
        texts = {s["text"]: s for s in analysis["strings"]}
        self.assertIn("HelloPE!", texts)
        self.assertIn("KERNEL32.dll", texts)

    def test_analyze_unknown_format_still_gets_strings(self):
        path = os.path.join(self.tmp, "notes.txt")
        with open(path, "wb") as f:
            f.write(b"some readable words in a plain file")
        analysis = binary.analyze(path, base=self.tmp)
        self.assertEqual(analysis["format"], "unknown")
        self.assertEqual(analysis["sections"], [])
        self.assertTrue(any("readable" in s["text"]
                            for s in analysis["strings"]))
        self.assertTrue(analysis["parse_notes"])

    def test_list_and_resolve(self):
        sha_e, _ = binary.store_binary(self.elf_path, base=self.tmp)
        sha_p, _ = binary.store_binary(self.pe_path, base=self.tmp)
        listed = binary.list_binaries(base=self.tmp)
        self.assertEqual(len(listed), 2)
        self.assertEqual(binary.resolve_binary(sha_e, base=self.tmp), sha_e)
        self.assertEqual(binary.resolve_binary(sha_p[:12], base=self.tmp),
                         sha_p)
        self.assertEqual(binary.resolve_binary("tiny.exe", base=self.tmp),
                         sha_p)
        self.assertIsNone(binary.resolve_binary("nosuch", base=self.tmp))
        self.assertIsNone(binary.resolve_binary("abc", base=self.tmp))

    def test_function_files(self):
        sha, adir = binary.store_binary(self.elf_path, base=self.tmp)
        funcs = os.path.join(adir, "functions")
        os.makedirs(funcs)
        with open(os.path.join(funcs, "401000_main.c"), "w") as f:
            f.write("int main(void) { return 42; }\n")
        c_path, asm_path = binary.function_files(
            sha, "0x401000", "main", base=self.tmp)
        self.assertTrue(c_path.endswith("401000_main.c"))
        self.assertIsNone(asm_path)
        # Names are sanitized the same way MimikExport.py does it.
        c2, _a2 = binary.function_files(
            sha, "0x401000", "weird name!", base=self.tmp)
        self.assertIsNone(c2)
        self.assertIsNone(binary.function_files(
            sha, "0x999999", "main", base=self.tmp)[0])

    def test_delete(self):
        sha, _ = binary.store_binary(self.elf_path, base=self.tmp)
        self.assertTrue(binary.delete_binary(sha, base=self.tmp))
        self.assertIsNone(binary.load_meta(sha, base=self.tmp))
        self.assertFalse(binary.delete_binary(sha, base=self.tmp))
        # The source file is untouched.
        self.assertTrue(os.path.isfile(self.elf_path))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.elf_path = os.path.join(self.tmp, "tiny.elf")
        with open(self.elf_path, "wb") as f:
            f.write(build_tiny_elf())
        self.sha, _ = binary.store_binary(self.elf_path, base=self.tmp)

    def _save_capture(self, host, launched):
        flows = [{
            "id": "1",
            "request": {"host": host, "method": "GET", "path": "/v1/x",
                        "scheme": "https", "port": 443, "headers": []},
            "response": {"status_code": 200},
        }]
        store.save_flows(flows, base=self.tmp,
                         meta={"launched_file": launched})

    def test_manual_link_roundtrip(self):
        binary.link_binary("app.example.com", self.sha, base=self.tmp)
        self.assertEqual(binary.get_links(base=self.tmp),
                         {"app.example.com": self.sha})
        self.assertEqual(binary.binary_for_host("app.example.com",
                                                base=self.tmp), self.sha)
        self.assertTrue(binary.unlink_binary("app.example.com",
                                             base=self.tmp))
        self.assertEqual(binary.get_links(base=self.tmp), {})
        self.assertFalse(binary.unlink_binary("app.example.com",
                                              base=self.tmp))

    def test_auto_match_by_launch_path(self):
        self._save_capture("app.example.com", self.elf_path)
        self.assertEqual(binary.binary_for_host("app.example.com",
                                                base=self.tmp), self.sha)

    def test_auto_match_by_unique_basename(self):
        self._save_capture("app.example.com",
                           "/somewhere/else/tiny.elf")
        self.assertEqual(binary.binary_for_host("app.example.com",
                                                base=self.tmp), self.sha)

    def test_no_match_without_launch_meta(self):
        self._save_capture("app.example.com", "")
        self.assertIsNone(binary.binary_for_host("app.example.com",
                                                 base=self.tmp))

    def test_manual_link_wins_over_auto(self):
        other = os.path.join(self.tmp, "other.elf")
        with open(other, "wb") as f:
            f.write(build_tiny_elf())
        sha2, _ = binary.store_binary(other, base=self.tmp)
        self._save_capture("app.example.com", self.elf_path)
        binary.link_binary("app.example.com", sha2, base=self.tmp)
        self.assertEqual(binary.binary_for_host("app.example.com",
                                                base=self.tmp), sha2)


if __name__ == "__main__":
    unittest.main()
