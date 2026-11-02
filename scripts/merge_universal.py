"""Merge an Apple-chip Penny.app and an Intel Penny.app into one universal Penny.app.

    python3 scripts/merge_universal.py <arm64 app> <x86_64 app> <output app>

Both apps must come from the same Python version and the same code (see
build_mac_app.sh), so they hold the same files. Files that are byte-for-byte
the same are kept once; compiled code (Mach-O) is fused with Apple's lipo,
so each file carries both chips' code. Zip archives (PyInstaller's
base_library.zip) count as the same when every file inside matches, since
only the packaging differs. Code signatures are left out: the merged app is
signed afresh. Anything else that differs stops the merge rather than guessing.
"""
import filecmp
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

MACHO = (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def is_macho(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(4) in MACHO


def tree(root: Path) -> dict[str, Path]:
    # Signatures are redone for the merged app (codesign in build_mac_app.sh).
    return {str(p.relative_to(root)): p for p in root.rglob("*") if "_CodeSignature" not in p.parts}


def same_zip_contents(a: Path, b: Path) -> bool:
    if not (zipfile.is_zipfile(a) and zipfile.is_zipfile(b)):
        return False
    with zipfile.ZipFile(a) as za, zipfile.ZipFile(b) as zb:
        names = za.namelist()
        return sorted(names) == sorted(zb.namelist()) and all(za.read(n) == zb.read(n) for n in names)


def merge(arm: Path, intel: Path, out: Path):
    a, b = tree(arm), tree(intel)
    only = sorted(set(a) ^ set(b))
    if only:
        raise SystemExit("The two apps don't hold the same files:\n  " + "\n  ".join(only[:20]))
    if out.exists():
        shutil.rmtree(out)
    fused = same = 0
    problems = []
    for rel in sorted(a, key=lambda r: r.count(os.sep)):
        src, other, dest = a[rel], b[rel], out / rel
        if src.is_symlink():
            if os.readlink(src) != os.readlink(other):
                problems.append(f"{rel}: links differ")
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(src), dest)
        elif src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif filecmp.cmp(src, other, shallow=False) or same_zip_contents(src, other):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            same += 1
        elif is_macho(src) and is_macho(other):
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["lipo", "-create", str(src), str(other), "-output", str(dest)], check=True)
            shutil.copymode(src, dest)
            fused += 1
        else:
            problems.append(f"{rel}: differs between the two apps and isn't compiled code")
    if problems:
        shutil.rmtree(out, ignore_errors=True)
        raise SystemExit("Not merging:\n  " + "\n  ".join(problems[:20]))
    print(f"Universal app: {fused} compiled files fused, {same} identical files kept once")


if __name__ == "__main__":
    merge(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
