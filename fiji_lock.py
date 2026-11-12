#!/usr/bin/env python3
"""
fiji-lock -- pin a Fiji installation to exact file versions and rebuild it later.

ImageJ update sites never delete anything: every file ever uploaded stays
available as  <site-url>/<filename>-<timestamp>  and is listed (with its
checksum) in the site's db.xml.gz. A Fiji installation's own db.xml.gz plus
the files on disk are therefore enough to write a lockfile that can rebuild
the exact same installation years later.

Commands
  lock     Fiji dir -> fiji.lock.json  (+ optional zip of files not on any update site)
           also records Maven coordinates and the bundled JDK (Azul Zulu, per platform)
  restore  fiji.lock.json -> new Fiji dir  (Maven first, then update sites; JDK from
           Azul; every file verified against the lockfile)
  verify   check a Fiji dir against a lockfile (CI / container smoke test)
  diff     compare two lockfiles

Checksums are computed exactly like the ImageJ updater does
(net.imagej.updater.util.UpdaterUtil), so they match the update-site records.

Standard library only; Python >= 3.9.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = "0.3.0"
LOCK_FORMAT = 1

DEFAULT_EXCLUDES = [
    "java/*",  # bundled JDK: not managed by update sites (recorded separately)
    "update/*",  # pending updates staged by the updater
    "db.xml.gz",
    ".checksums",
    "*.old",
    "*/.DS_Store",
    ".DS_Store",
    "*/__pycache__/*",
]

sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))  # SkipHashedLines port

# ---------------------------------------------------------------------------
# Platforms (port of net.imagej.updater.util.Platforms)
# ---------------------------------------------------------------------------


def current_platform() -> str:
    system = sys.platform
    machine = _platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system.startswith("linux"):
        return "linux-arm64" if arm else "linux64"
    if system == "darwin":
        return "macos-arm64" if arm else "macos64"
    if system in ("win32", "cygwin"):
        return "win-arm64" if arm else "win64"
    return "unknown"


def platform_matches(platform: str, candidates: list[str]) -> bool:
    if not candidates or platform == "all":
        return True  # file targets all platforms, or caller wants every platform
    for c in candidates:
        if c == platform:
            return True
        if c == "linuxx" and platform.startswith("linux"):
            return True
        if c == "macosx" and platform.startswith("macos"):
            return True
        if c == "winx" and platform.startswith("win"):
            return True
        # legacy platform names
        if c in ("linux32",) and platform == "linux32":
            return True
    return False


# ---------------------------------------------------------------------------
# Versioned file names (port of org.scijava.util.FileUtils.stripFilenameVersion)
# ---------------------------------------------------------------------------

_VERSION = r"\d+(\.\d+|\d{7})+[a-z]?\d?(-[A-Za-z0-9.]+?|\.GA)*?"
_SUFFIX = r"\.jar(-[a-z]*)?"
_CLASSIFIERS = (
    "(swing|swt|shaded|sources|javadoc|natives?-?\\w*|"
    "(natives-)?(android|linux|macosx|macos|solaris|windows)-"
    "(aarch64|amd64|arm64|armv6hf|armv6|arm|i386|i486|i586|i686|universal|"
    "x86[_-]32|x86[_-]64|x86))"
)
_VERSION_PATTERN = re.compile(
    "(.+?)(-" + _VERSION + ")?((-(" + _CLASSIFIERS + "))?(" + _SUFFIX + "))",
    re.ASCII,
)


def strip_version(filename: str) -> str:
    m = _VERSION_PATTERN.fullmatch(filename)
    if not m:
        return filename
    return m.group(1) + m.group(5)


# ---------------------------------------------------------------------------
# Updater checksums (port of net.imagej.updater.util.UpdaterUtil)
# ---------------------------------------------------------------------------


def _skip_hashed_lines(data: bytes) -> bytes:
    """Byte-exact port of net.imagej.updater.util.SkipHashedLines (quirks included)."""
    out = bytearray()
    n = len(data)
    pos = 0
    at_line_start = True

    def read() -> int:
        nonlocal pos, at_line_start
        if pos < n:
            ch = data[pos]
            pos += 1
        else:
            ch = -1
        if at_line_start:
            if ch == 0x23:  # '#'
                while True:
                    ch = read()
                    if ch == 0x0A or ch == -1:
                        break
            else:
                at_line_start = False
        elif ch == 0x0A:
            at_line_start = True
        return ch

    while True:
        ch = read()
        if ch < 0:
            break
        out.append(ch)
    return bytes(out)


_MANIFEST_SKIP = {
    "ARCHIVER-VERSION",
    "BUILT-BY",
    "BUILD-JDK",
    "CLASS-PATH",
    "CREATED-BY",
    "IMPLEMENTATION-BUILD",
}


def _filter_manifest(data: bytes, keep_only_main_class: bool) -> bytes:
    """Port of net.imagej.updater.util.FilterManifest."""
    if keep_only_main_class:
        # The Java code tests line.toUpperCase().startsWith("Main-Class:"),
        # which can never be true -- so the manifest contributes nothing.
        return b""
    text = data.decode("utf-8", errors="replace")
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "":
        lines.pop()  # BufferedReader.readLine() yields no trailing empty line
    out = []
    for line in lines:
        colon = line.find(":")
        if colon > 0 and line[:colon].upper() in _MANIFEST_SKIP:
            continue
        out.append(line + "\n")
    return "".join(out).encode("utf-8")


def _zip_name(info: zipfile.ZipInfo) -> str:
    # Java's JarFile decodes entry names as UTF-8; Python uses cp437 unless flagged.
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def jar_digest(
    path: Path, props: bool = True, manifest: bool = True, only_main_class: bool = True
) -> str:
    h = hashlib.sha1()
    with zipfile.ZipFile(path) as zf:
        infos = [(_zip_name(i), i) for i in zf.infolist()]
        # Jars may contain duplicate entry names. Java's JarFile.getInputStream()
        # looks entries up *by name* and always returns the LAST one in the
        # central directory -- so every duplicate contributes that same content.
        last = {name: info for name, info in infos}
        # Java String.compareTo == UTF-16 code-unit order (sort is stable)
        infos.sort(key=lambda t: t[0].encode("utf-16-be"))
        for name, info in infos:
            h.update(name.encode("ascii", errors="replace"))
            src = last[name]
            data = zf.read(src) if not src.is_dir() else b""
            if props and name.endswith(".properties"):
                data = _skip_hashed_lines(data)
            if manifest and name == "META-INF/MANIFEST.MF":
                data = _filter_manifest(data, only_main_class)
            h.update(data)
    return h.hexdigest()


def updater_digest(rel: str, path: Path) -> str:
    """Checksum as computed by the ImageJ updater for file `rel` (relative path)."""
    if rel == "plugins/Fiji_Updater.jar":
        return jar_digest(path, False, False, False)
    if rel.endswith(".jar"):
        return jar_digest(path)
    h = hashlib.sha1()
    h.update(rel.encode("ascii", errors="replace"))
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def obsolete_digests(rel: str, path: Path) -> list[str]:
    if not rel.endswith(".jar"):
        return []
    return [
        jar_digest(path, True, True, False),
        jar_digest(path, True, False, False),
        jar_digest(path, False, False, False),
    ]


def digest_matches(rel: str, path: Path, checksum: str) -> tuple[bool, str]:
    """Does the file match a recorded updater checksum? Like the updater itself,
    also accept the obsolete checksum variants: records uploaded by old updater
    versions (e.g. 2012-era Fiji jars) still carry checksums in those formats.
    Returns (match, current-format digest)."""
    try:
        digest = updater_digest(rel, path)
    except zipfile.BadZipFile:
        return False, "not a valid jar"
    if digest == checksum:
        return True, digest
    try:
        return checksum in obsolete_digests(rel, path), digest
    except zipfile.BadZipFile:
        return False, digest


def maven_coordinate(rel: str, path: Path) -> str | None:
    """groupId:artifactId:version[:classifier] from the jar's own pom.properties,
    if it unambiguously describes this file. Only a hint: restore verifies the
    updater checksum of whatever Maven returns and falls back to the update site."""
    base = Path(rel).name
    if not base.endswith(".jar"):
        return None
    stem = base[:-4]
    try:
        with zipfile.ZipFile(path) as zf:
            props = [
                n
                for n in zf.namelist()
                if n.startswith("META-INF/maven/") and n.endswith("/pom.properties")
            ]
            candidates = []
            for n in props:
                kv = {}
                for line in zf.read(n).decode("utf-8", "replace").splitlines():
                    if "=" in line and not line.lstrip().startswith("#"):
                        k, v = line.split("=", 1)
                        kv[k.strip()] = v.strip()
                g, a, v = kv.get("groupId"), kv.get("artifactId"), kv.get("version")
                if g and a and v:
                    candidates.append((g, a, v))
    except (zipfile.BadZipFile, OSError):
        return None
    for g, a, v in candidates:
        if v.endswith("-SNAPSHOT"):
            continue  # snapshot builds are not stable on Maven
        if stem == f"{a}-{v}":
            return f"{g}:{a}:{v}"
        if stem.startswith(f"{a}-{v}-"):
            return f"{g}:{a}:{v}:{stem[len(a) + len(v) + 2 :]}"
        if stem == a and len(candidates) == 1:
            return f"{g}:{a}:{v}"  # unversioned file name (some plugins/)
    return None


def maven_url(repo: str, coord: str) -> str:
    parts = coord.split(":")
    g, a, v = parts[:3]
    c = f"-{parts[3]}" if len(parts) > 3 else ""
    if not repo.endswith("/"):
        repo += "/"
    return f"{repo}{g.replace('.', '/')}/{a}/{v}/{a}-{v}{c}.jar"


DEFAULT_MAVEN_REPOS = ["https://maven.scijava.org/content/groups/public/"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def updater_timestamp(mtime: float) -> str:
    """UpdaterUtil.timestamp(): local-time yyyyMMddHHmmss."""
    return time.strftime("%Y%m%d%H%M%S", time.localtime(mtime))


# ---------------------------------------------------------------------------
# db.xml.gz
# ---------------------------------------------------------------------------


class Version:
    __slots__ = ("checksum", "filename", "filesize", "obsolete", "timestamp")

    def __init__(self, checksum, timestamp, filename, obsolete=None, filesize=None):
        self.checksum = checksum
        self.timestamp = timestamp
        self.filename = filename
        self.obsolete = obsolete
        self.filesize = filesize


class Record:
    def __init__(self, site, filename, executable):
        self.site = site
        self.filename = filename
        self.executable = executable
        self.platforms: list[str] = []
        self.current: Version | None = None
        self.previous: list[Version] = []

    def versions(self):
        if self.current:
            yield self.current
        yield from self.previous

    def find(self, checksums) -> Version | None:
        for v in self.versions():
            if v.checksum in checksums:
                return v
        return None

    def as_of(self, cutoff: str) -> Version | None:
        """Version that was current on the site at `cutoff` (None if absent/removed)."""
        best = None
        for v in self.versions():
            if v.timestamp <= cutoff and (best is None or v.timestamp > best.timestamp):
                best = v
        if best is None:
            return None
        if best is not self.current and best.obsolete and best.obsolete <= cutoff:
            return None
        return best


def read_db(root: Path):
    db = root / "db.xml.gz"
    if not db.exists():
        sys.exit(
            f"error: {db} not found -- is this a Fiji installation that has run the updater?"
        )
    with gzip.open(db, "rb") as f:
        tree = ET.parse(f)
    sites: dict[str, dict] = {}
    records: dict[str, list[Record]] = {}
    for el in tree.getroot():
        if el.tag in ("update-site", "disabled-update-site"):
            sites[el.get("name")] = {
                "name": el.get("name"),
                "url": el.get("url"),
                "timestamp": el.get("timestamp"),
                "enabled": el.tag == "update-site",
                **({"official": True} if el.get("official") == "true" else {}),
            }
        elif el.tag == "plugin":
            rec = Record(
                el.get("update-site"),
                el.get("filename"),
                el.get("executable") == "true",
            )
            for child in el:
                if child.tag == "platform" and child.text:
                    rec.platforms.append(child.text.strip())
                elif child.tag == "version":
                    rec.current = Version(
                        child.get("checksum"),
                        child.get("timestamp"),
                        rec.filename,
                        filesize=child.get("filesize"),
                    )
                elif child.tag == "previous-version":
                    rec.previous.append(
                        Version(
                            child.get("checksum"),
                            child.get("timestamp"),
                            child.get("filename") or rec.filename,
                            obsolete=child.get("timestamp-obsolete"),
                        )
                    )
            records.setdefault(strip_version(rec.filename), []).append(rec)
    return sites, records


def read_checksum_cache(root: Path) -> dict[str, tuple[str, str]]:
    """The updater's .checksums cache: '<sha1> <timestamp> <path>' per line."""
    cache = {}
    p = root / ".checksums"
    if p.exists():
        for line in p.read_text(errors="replace").splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3:
                cache[parts[2]] = (parts[0], parts[1])
    return cache


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def walk(root: Path, excludes: list[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
                continue
            if p.is_symlink() or not p.is_file():
                continue
            yield rel, p


def site_file_url(site_url: str, filename: str, timestamp: str) -> str:
    if not site_url.endswith("/"):
        site_url += "/"
    return site_url + filename.replace(" ", "%20") + "-" + timestamp


_RELEASE_KEYS = (
    "JAVA_VERSION",
    "JAVA_RUNTIME_VERSION",
    "IMPLEMENTOR",
    "IMPLEMENTOR_VERSION",
    "OS_NAME",
    "OS_ARCH",
)


def detect_jdks(root: Path) -> dict[str, dict]:
    """Every bundled JDK, keyed by its java/<platform>/ folder:
    {platform: {"dir": "java/<platform>/<name>", "java_version": ..., ...}}.
    Fiji installs one per platform; portable installs can carry several."""
    java_dir = root / "java"
    found: dict[str, dict] = {}
    if not java_dir.is_dir():
        return found
    for plat_dir in sorted(p for p in java_dir.iterdir() if p.is_dir()):
        for jdk in sorted(p for p in plat_dir.iterdir() if p.is_dir()):
            # the release file is at the top, or deeper on macOS
            # (zulu-21.jdk/Contents/Home/release)
            release = next(iter(sorted(jdk.rglob("release"))), None)
            if release is None:
                continue
            info = {"dir": jdk.relative_to(root).as_posix()}
            for line in release.read_text(errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k in _RELEASE_KEYS:
                        info[k.lower()] = v.strip().strip('"')
            if plat_dir.name in found:
                log(
                    f"java: warning: several JDKs in {plat_dir.name}/; "
                    f"only {found[plat_dir.name]['dir']} is locked"
                )
                continue
            found[plat_dir.name] = info
    return found


def java_installed(j: dict) -> dict[str, dict]:
    """Locked JDK fingerprints per platform (also reads 0.2.0 lockfiles)."""
    if j.get("installed"):
        return j["installed"]
    if j.get("manifest"):
        return {j.get("platform", "?"): {"dir": j["dir"], "manifest": j["manifest"]}}
    return {}


# ---------------------------------------------------------------------------
# Java (Azul Zulu, as bundled by Fiji)
# ---------------------------------------------------------------------------

AZUL_API = os.environ.get(
    "FIJI_LOCK_AZUL_API", "https://api.azul.com/metadata/v1/zulu/packages/"
)
# Fiji platform -> (Azul file-name os, Azul API os, arch, archive type)
AZUL_PLATFORMS = {
    "linux64": ("linux", "linux", "x64", "tar.gz"),
    "linux-arm64": ("linux", "linux", "aarch64", "tar.gz"),
    "macos64": ("macosx", "macos", "x64", "tar.gz"),
    "macos-arm64": ("macosx", "macos", "aarch64", "tar.gz"),
    "win64": ("win", "windows", "x64", "zip"),
    "win-arm64": ("win", "windows", "aarch64", "zip"),
}
_ZULU_NAME = re.compile(
    r"zulu(?P<zv>[\d.]+)-ca-(?P<crac>crac-)?(?P<pkg>jdk|jre)(?P<jv>[\d.]+)(?P<fx>-fx)?"
    r"-(?P<os>win|linux|macosx)_(?P<arch>x64|aarch64)$"
)


def tree_manifest(top: Path) -> dict[str, str]:
    """sha256 of every file under `top` (symlinks recorded by target)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames.sort()
        for name in sorted(
            filenames + [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
        ):
            p = Path(dirpath) / name
            rel = p.relative_to(top).as_posix()
            if p.is_symlink():
                out[rel] = "symlink:" + os.readlink(p)
            elif p.is_file():
                out[rel] = sha256_file(p)
    return out


def _get_json(url: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": f"fiji-lock/{TOOL_VERSION}"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def azul_package(name: str, platform: str, m) -> dict:
    """Download URL + sha256 for one Zulu archive, from Azul's metadata API."""
    _fos, api_os, arch, ext = AZUL_PLATFORMS[platform]
    filename = f"{name}.{ext}"
    pkg = {"archive": filename, "url": f"https://cdn.azul.com/zulu/bin/{filename}"}
    query = urllib.parse.urlencode(
        {
            "java_version": m["jv"],
            "os": api_os,
            "arch": arch,
            "archive_type": ext,
            "java_package_type": m["pkg"],
            "javafx_bundled": "true" if m["fx"] else "false",
            "availability_types": "CA",
            "page_size": 1000,
        }
    )
    try:
        hits = [
            p for p in _get_json(f"{AZUL_API}?{query}") if p.get("name") == filename
        ]
        if not hits:
            pkg["error"] = "not published by Azul for this platform"
            return pkg
        detail = _get_json(f"{AZUL_API}{hits[0]['package_uuid']}")
        pkg.update(
            url=detail.get("download_url") or pkg["url"],
            sha256=detail.get("sha256_hash"),
            size=detail.get("size"),
        )
    except Exception as e:  # noqa: BLE001
        pkg["error"] = f"Azul metadata lookup failed: {getattr(e, 'reason', e)}"
    return pkg


def lock_java(root: Path, source_platform: str, lookup: bool) -> dict | None:
    jdks = detect_jdks(root)
    if not jdks:
        return None
    for plat, info in jdks.items():
        log(f"java: fingerprinting {info['dir']} ...")
        info["manifest"] = tree_manifest(root / info["dir"])
    # summary fields come from the install's own platform (else the first JDK)
    main = jdks.get(source_platform) or next(iter(jdks.values()))
    j = {k: v for k, v in main.items() if k != "manifest"}
    j["installed"] = jdks
    m = _ZULU_NAME.match(Path(main["dir"]).name)
    if not m or m["crac"]:
        log(
            f"java: {Path(main['dir']).name} is not a standard Azul Zulu build; "
            "fingerprinted for `verify`, but cannot be re-downloaded"
        )
        return j
    packages = {}
    for plat, (fos, _, arch, ext) in AZUL_PLATFORMS.items():
        name = f"zulu{m['zv']}-ca-{m['pkg']}{m['jv']}{m['fx'] or ''}-{fos}_{arch}"
        packages[plat] = (
            azul_package(name, plat, m)
            if lookup
            else {
                "archive": f"{name}.{ext}",
                "url": f"https://cdn.azul.com/zulu/bin/{name}.{ext}",
            }
        )
    j["packages"] = packages
    ok = [p for p, v in packages.items() if v.get("sha256")]
    if lookup:
        log(
            f"java: Azul Zulu {m['zv']} (JDK {m['jv']}) pinned with sha256 for "
            f"{', '.join(ok) or 'no platform'}"
        )
        for p, v in packages.items():
            if v.get("error"):
                log(f"java:   {p}: {v['error']}")
    return j


def _safe_extract(archive: Path, dest: Path, name: str):
    dest = dest.resolve()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                out = (dest / info.filename).resolve()
                if dest != out and dest not in out.parents:
                    raise RuntimeError(
                        f"unsafe path in {archive.name}: {info.filename}"
                    )
                zf.extract(info, dest)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    out.chmod(mode)
    else:
        import tarfile

        with tarfile.open(archive) as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:  # Python < 3.12 without the backport
                for member in tf.getmembers():
                    out = (dest / member.name).resolve()
                    if dest != out and dest not in out.parents:
                        raise RuntimeError(
                            f"unsafe path in {archive.name}: {member.name}"
                        )
                    if member.issym() or member.islnk():
                        link = (out.parent / member.linkname).resolve()
                        if dest not in link.parents:
                            raise RuntimeError(
                                f"unsafe link in {archive.name}: {member.name}"
                            )
                tf.extractall(dest)


def compare_manifest(expected: dict, actual: dict) -> tuple[list, list, list]:
    missing = sorted(set(expected) - set(actual))
    changed = sorted(k for k in expected if k in actual and actual[k] != expected[k])
    extra = sorted(set(actual) - set(expected))
    return missing, changed, extra


_SAFE_ARCHIVE = re.compile(r"^[A-Za-z0-9._-]+\.(zip|tar\.gz)$")


def restore_java(
    lock: dict, target: Path, platforms: list[str], dl, cache: Path | None, resume: bool
):
    j = lock.get("java") or {}
    pkgs = j.get("packages") or {}
    installed = java_installed(j)
    for plat in platforms:
        pkg = pkgs.get(plat)
        if not pkg or pkg.get("error") == "not published by Azul for this platform":
            log(f"java: no Azul package recorded for {plat}; skipped")
            continue
        if plat not in AZUL_PLATFORMS or not _SAFE_ARCHIVE.match(pkg["archive"]):
            raise RuntimeError(
                f"refusing suspicious JDK entry {plat}: {pkg['archive']!r}"
            )
        dest_dir = target / "java" / plat
        jdk_dir = dest_dir / re.sub(r"\.(zip|tar\.gz)$", "", pkg["archive"])
        locked = installed.get(plat)

        if resume and jdk_dir.is_dir():
            # keep it if it matches the locked fingerprint (or there is none)
            if locked:
                missing, changed, _ = compare_manifest(
                    locked["manifest"], tree_manifest(jdk_dir)
                )
                keep = not missing and not changed
            else:
                keep = True
            if keep:
                log(f"java: {plat} JDK already present")
                continue
        if jdk_dir.exists():
            shutil.rmtree(jdk_dir)
        archive = (
            (cache / pkg["archive"])
            if cache
            else (target / "java" / f".{pkg['archive']}.part")
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not (
            archive.exists()
            and pkg.get("sha256")
            and sha256_file(archive) == pkg["sha256"]
        ):
            log(f"java: downloading {pkg['archive']} for {plat}")
            dl.get(pkg["url"], archive)
        if pkg.get("sha256"):
            got = sha256_file(archive)
            if got != pkg["sha256"]:
                raise RuntimeError(
                    f"{pkg['archive']}: sha256 mismatch ({got} != {pkg['sha256']})"
                )
        elif not locked:
            log(f"java: warning: no sha256 recorded for {pkg['archive']}; not verified")
        dest_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract(archive, dest_dir, pkg["archive"])
        if not cache:
            archive.unlink()
        if locked:
            missing, changed, extra = compare_manifest(
                locked["manifest"], tree_manifest(jdk_dir)
            )
            if missing or changed:
                raise RuntimeError(
                    f"{jdk_dir.relative_to(target)} differs from the locked JDK "
                    f"({len(missing)} missing, {len(changed)} changed, "
                    f"e.g. {(missing + changed)[:3]})"
                )
            log(
                f"java: {plat} JDK identical to the locked one "
                f"({len(locked['manifest'])} files)"
                + (f", plus {len(extra)} extra" if extra else "")
            )
        else:
            log(f"java: {plat} JDK installed ({pkg['archive']}, sha256 verified)")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


KNOWN_PLATFORMS = (
    "linux64",
    "linux-arm64",
    "macos64",
    "macos-arm64",
    "win64",
    "win-arm64",
)


def detect_source_platform(root: Path, installed: list[dict]) -> str:
    """Platform of the installation itself -- not of the machine running `lock`
    (a Windows Fiji can be locked from Linux, e.g. on a shared drive)."""
    java_dir = root / "java"
    if java_dir.is_dir():
        found = [
            d.name
            for d in java_dir.iterdir()
            if d.is_dir() and d.name in KNOWN_PLATFORMS
        ]
        if len(found) == 1:
            return found[0]
    # otherwise: the concrete platform of the installed platform-specific files
    from collections import Counter

    votes = Counter(
        p for f in installed for p in f.get("platforms", []) if p in KNOWN_PLATFORMS
    )
    if votes:
        return votes.most_common(1)[0][0]
    return current_platform()


def detect_dir_platform(root: Path, lock: dict) -> str:
    """Platform a restored directory was built for: its single java/<platform>/
    folder, else the platform whose platform-specific locked files are present,
    else the lockfile's source platform."""
    java_dir = root / "java"
    if java_dir.is_dir():
        found = [
            d.name
            for d in java_dir.iterdir()
            if d.is_dir() and d.name in KNOWN_PLATFORMS
        ]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return "all"
    from collections import Counter

    votes = Counter(
        p
        for f in lock["files"]
        for p in f.get("platforms", [])
        if p in KNOWN_PLATFORMS and (root / f["path"]).exists()
    )
    if len(votes) > 1:
        return "all"
    if votes:
        return next(iter(votes))
    return lock.get("source", {}).get("platform") or current_platform()


def cmd_lock(args):
    root = Path(args.fiji).resolve()
    sites, records = read_db(root)
    cache = {} if args.no_cache else read_checksum_cache(root)
    excludes = DEFAULT_EXCLUDES + (args.exclude or [])
    files, local_only, modified = [], [], []
    installed_keys: set[tuple[str, str]] = set()  # (site, unversioned)

    for rel, path in walk(root, excludes):
        key = strip_version(rel)
        recs = records.get(key)
        if not recs:
            local_only.append(
                {"path": rel, "sha256": sha256_file(path), "size": path.stat().st_size}
            )
            continue
        try:
            cached = cache.get(rel)
            if cached and cached[1] == updater_timestamp(path.stat().st_mtime):
                digest = cached[0]
            else:
                digest = updater_digest(rel, path)
            match = rec = None
            for r in recs:
                match = r.find({digest})
                if match:
                    rec = r
                    break
            if not match:
                old = set(obsolete_digests(rel, path))
                for r in recs:
                    match = r.find(old)
                    if match:
                        rec = r
                        break
        except zipfile.BadZipFile:
            match = None
            log(f"warning: {rel} is not a valid jar")
        if match:
            installed_keys.add((rec.site, key))
            coord = None if args.no_maven else maven_coordinate(rel, path)
            files.append(
                {
                    "path": rel,
                    "site": rec.site,
                    "filename": match.filename,
                    "timestamp": match.timestamp,
                    "checksum": match.checksum,
                    **({"maven": coord} if coord else {}),
                    **({"executable": True} if rec.executable else {}),
                    **({"platforms": rec.platforms} if rec.platforms else {}),
                }
            )
        else:
            modified.append(
                {
                    "path": rel,
                    "site": recs[0].site,
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )

    # Per-site cutoff: newest pinned timestamp coming from that site.
    cutoff: dict[str, str] = {}
    for f in files:
        if f["timestamp"] > cutoff.get(f["site"], ""):
            cutoff[f["site"]] = f["timestamp"]

    # Platform-specific files for *other* platforms (launchers, native libs):
    # resolve to whatever was current on the site at the cutoff, so the lockfile
    # can rebuild e.g. a Linux container from a lock made on a Mac.
    source_platform = args.platform or detect_source_platform(root, files)
    if source_platform != current_platform():
        log(
            f"installation platform: {source_platform} (this machine: {current_platform()})"
        )
    other = 0
    if not args.this_platform_only:
        for key, recs in records.items():
            for rec in recs:
                if not rec.platforms or rec.site not in cutoff:
                    continue
                if (rec.site, key) in installed_keys:
                    continue
                if platform_matches(source_platform, rec.platforms):
                    continue  # the install's own platform, not installed: user chose not to
                v = rec.as_of(cutoff[rec.site])
                if not v:
                    continue
                other += 1
                files.append(
                    {
                        "path": v.filename,
                        "site": rec.site,
                        "filename": v.filename,
                        "timestamp": v.timestamp,
                        "checksum": v.checksum,
                        **({"executable": True} if rec.executable else {}),
                        "platforms": rec.platforms,
                        "resolved": "as-of",
                    }
                )

    used_sites = sorted({f["site"] for f in files})
    lock = {
        "format": LOCK_FORMAT,
        "generator": f"fiji-lock {TOOL_VERSION}",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"platform": source_platform, "dir": root.name},
        "java": lock_java(root, source_platform, not args.no_java_lookup),
        "update_sites": [
            {**sites.get(s, {"name": s, "url": None}), "cutoff": cutoff.get(s)}
            for s in used_sites
        ]
        + [
            {**v, "cutoff": None}
            for k, v in sorted(sites.items())
            if k not in used_sites and v.get("enabled")
        ],
        "files": sorted(files, key=lambda f: f["path"]),
        "local_only": local_only,
        "modified": modified,
    }

    out = Path(args.output)
    out.write_text(json.dumps(lock, indent=1) + "\n")
    n_inst = len(files) - other
    n_mvn = sum(1 for f in files if "maven" in f)
    log(
        f"wrote {out}: {n_inst} pinned files from {len(used_sites)} update site(s) "
        f"({n_mvn} with Maven coordinates), {other} for other platforms, "
        f"{len(local_only)} local-only, {len(modified)} modified"
    )

    if args.local_bundle:
        with zipfile.ZipFile(args.local_bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            for e in local_only + modified:
                zf.write(root / e["path"], e["path"])
        log(
            f"wrote {args.local_bundle}: {len(local_only) + len(modified)} files "
            "not reproducible from update sites"
        )
    elif local_only or modified:
        log(
            "note: some files are not on any update site; list them with `jq .local_only,.modified` "
            "and consider --local-bundle to archive them alongside the lockfile"
        )
        for e in (modified + local_only)[:15]:
            log(f"  {'M' if e in modified else '?'} {e['path']}")
        extra = len(local_only) + len(modified) - 15
        if extra > 0:
            log(f"  ... and {extra} more")


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

_DB_DTD = """<!DOCTYPE pluginRecords [
<!ELEMENT pluginRecords ((update-site | disabled-update-site)*, plugin*)>
<!ELEMENT update-site EMPTY>
<!ELEMENT disabled-update-site EMPTY>
<!ELEMENT plugin (platform*, category*, version?, previous-version*)>
<!ELEMENT version (description?, dependency*, link*, author*)>
<!ELEMENT previous-version EMPTY>
<!ELEMENT description (#PCDATA)>
<!ELEMENT dependency EMPTY>
<!ELEMENT link (#PCDATA)>
<!ELEMENT author (#PCDATA)>
<!ELEMENT platform (#PCDATA)>
<!ELEMENT category (#PCDATA)>
<!ATTLIST update-site name CDATA #REQUIRED>
<!ATTLIST update-site url CDATA #REQUIRED>
<!ATTLIST update-site keep-url CDATA #IMPLIED>
<!ATTLIST update-site official CDATA #IMPLIED>
<!ATTLIST update-site ssh-host CDATA #IMPLIED>
<!ATTLIST update-site upload-directory CDATA #IMPLIED>
<!ATTLIST update-site description CDATA #IMPLIED>
<!ATTLIST update-site maintainer CDATA #IMPLIED>
<!ATTLIST update-site timestamp CDATA #REQUIRED>
<!ATTLIST disabled-update-site name CDATA #REQUIRED>
<!ATTLIST disabled-update-site url CDATA #REQUIRED>
<!ATTLIST disabled-update-site keep-url CDATA #IMPLIED>
<!ATTLIST disabled-update-site official CDATA #IMPLIED>
<!ATTLIST disabled-update-site ssh-host CDATA #IMPLIED>
<!ATTLIST disabled-update-site upload-directory CDATA #IMPLIED>
<!ATTLIST disabled-update-site description CDATA #IMPLIED>
<!ATTLIST disabled-update-site maintainer CDATA #IMPLIED>
<!ATTLIST disabled-update-site timestamp CDATA #REQUIRED>
<!ATTLIST plugin update-site CDATA #IMPLIED>
<!ATTLIST plugin filename CDATA #REQUIRED>
<!ATTLIST plugin executable CDATA #IMPLIED>
<!ATTLIST dependency filename CDATA #REQUIRED>
<!ATTLIST dependency timestamp CDATA #IMPLIED>
<!ATTLIST dependency overrides CDATA #IMPLIED>
<!ATTLIST version timestamp CDATA #REQUIRED>
<!ATTLIST version checksum CDATA #REQUIRED>
<!ATTLIST version filesize CDATA #REQUIRED>
<!ATTLIST previous-version filename CDATA #IMPLIED>
<!ATTLIST previous-version timestamp CDATA #REQUIRED>
<!ATTLIST previous-version timestamp-obsolete CDATA #IMPLIED>
<!ATTLIST previous-version checksum CDATA #REQUIRED>]>
"""


def _xml_attr(v: str) -> str:
    return (
        str(v)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_db(target: Path, lock: dict, restored: list[dict]):
    """Minimal db.xml.gz so the updater knows the sites and sees files as installed."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        _DB_DTD.rstrip("\n"),
        "<pluginRecords>",
    ]
    for s in lock["update_sites"]:
        if not s.get("url"):
            continue
        tag = "update-site" if s.get("enabled", True) else "disabled-update-site"
        attrs = f'name="{_xml_attr(s["name"])}" url="{_xml_attr(s["url"])}"'
        if s.get("official"):
            attrs += ' official="true"'
        attrs += (
            f' timestamp="{_xml_attr(s.get("timestamp") or s.get("cutoff") or "0")}"'
        )
        lines.append(f" <{tag} {attrs}/>")
    # Record every pinned file (other platforms too), so the restored copy can be
    # re-locked or inspected without losing information.
    sizes = {f["path"]: f.get("_size", 0) for f in restored}
    seen = set()
    for f in lock["files"]:
        if f["path"] in seen:
            continue
        seen.add(f["path"])
        f = {**f, "_size": sizes.get(f["path"], 0)}
        ex = ' executable="true"' if f.get("executable") else ""
        lines.append(
            f' <plugin update-site="{_xml_attr(f["site"])}" '
            f'filename="{_xml_attr(f["path"])}"{ex}>'
        )
        for p in f.get("platforms", []):
            lines.append(f"  <platform>{_xml_attr(p)}</platform>")
        lines.append(
            f'  <version checksum="{f["checksum"]}" timestamp="{f["timestamp"]}" '
            f'filesize="{f.get("_size", 0)}"/>'
        )
        lines.append(" </plugin>")
    lines.append("</pluginRecords>")
    with gzip.open(target / "db.xml.gz", "wb") as g:
        g.write(("\n".join(lines) + "\n").encode("utf-8"))


def _rewrite(url: str, rules: list[tuple[str, str]]) -> str:
    for old, new in rules:
        if url.startswith(old):
            return new + url[len(old) :]
    return url


class FetchError(RuntimeError):
    def __init__(self, url, reason, kind=None):
        super().__init__(f"{url}: {reason}")
        self.url, self.reason, self.kind = url, reason, kind or reason


class Downloader:
    """HTTP GET with a polite, shared back-off.

    Servers such as sites.imagej.net answer bursts of requests with 403/429.
    When that happens every worker pauses for that host (15 s, 30 s, 60 s, ...,
    at most 2 min) instead of hammering it with immediate retries. A URL that is
    still refused while other files download fine is reported as forbidden."""

    THROTTLE = frozenset({403, 429, 503})

    def __init__(self, attempts: int):
        import threading

        self.attempts = attempts
        self.backoff = float(os.environ.get("FIJI_LOCK_BACKOFF", "15"))
        self._lock = threading.Lock()
        self._pause_until: dict[str, float] = {}
        self._net_failures: dict[str, int] = {}
        self._ok: dict[str, int] = {}  # successful downloads per host
        self._dead: set[str] = set()

    def _wait(self, host):
        while True:
            with self._lock:
                delay = self._pause_until.get(host, 0) - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 5))

    def _pause(self, host, seconds):
        with self._lock:
            until = time.monotonic() + seconds
            if until > self._pause_until.get(host, 0):
                self._pause_until[host] = until
                log(f"  {host} is throttling (HTTP 403/429); pausing {seconds:g} s")

    def _network_failure(self, host):
        """After 3 files in a row fail with network errors, stop trying the host
        (e.g. an unreachable Maven repository should not slow down every jar)."""
        with self._lock:
            self._net_failures[host] = self._net_failures.get(host, 0) + 1
            if self._net_failures[host] >= 3 and host not in self._dead:
                self._dead.add(host)
                log(f"  {host} is unreachable; skipping it for the rest of this run")

    def get(self, url: str, dest: Path, quick: bool = False):
        """quick=True (optional sources such as Maven): any HTTP error fails at
        once, so the caller can fall back to the next source."""
        host = urllib.parse.urlsplit(url).netloc
        if host in self._dead:
            raise FetchError(url, "host unreachable (skipped)", "network error")
        req = urllib.request.Request(
            url, headers={"User-Agent": f"fiji-lock/{TOOL_VERSION}"}
        )
        net_errors = 0
        ok_at_first_refusal = None
        for attempt in range(self.attempts):
            if host in self._dead:
                break
            self._wait(host)
            try:
                with (
                    urllib.request.urlopen(req, timeout=120) as r,
                    open(dest, "wb") as out,
                ):
                    shutil.copyfileobj(r, out, 1 << 20)
                with self._lock:
                    self._net_failures[host] = 0
                    self._ok[host] = self._ok.get(host, 0) + 1
                return
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise FetchError(url, "HTTP 404 Not Found") from e
                last = FetchError(url, f"HTTP {e.code} {e.reason}")
                if quick:
                    raise last from e
                if e.code in self.THROTTLE:
                    # Throttling is host-wide. If the host has meanwhile served
                    # several other files but still refuses this one, the refusal
                    # is specific to this URL: stop waiting for it.
                    ok_now = self._ok.get(host, 0)
                    if ok_at_first_refusal is None:
                        ok_at_first_refusal = ok_now
                    elif ok_now - ok_at_first_refusal >= 3:
                        raise FetchError(
                            url,
                            f"HTTP {e.code} {e.reason} (refused while other files download fine)",
                            f"HTTP {e.code} {e.reason}",
                        ) from e
                    self._pause(host, min(self.backoff * 2**attempt, 120))
                else:
                    time.sleep(2**attempt)
            except Exception as e:  # noqa: BLE001 -- network errors, timeouts
                last = FetchError(
                    url, f"network error: {getattr(e, 'reason', e)}", "network error"
                )
                net_errors += 1
                if net_errors >= 3:
                    break
                time.sleep(2**attempt)
        if net_errors:
            self._network_failure(host)
        raise last


def cmd_restore(args):
    lock = json.loads(Path(args.lockfile).read_text())
    if lock.get("format") != LOCK_FORMAT:
        sys.exit(f"error: unsupported lockfile format {lock.get('format')}")
    target = Path(args.target).resolve()
    if target.exists() and any(target.iterdir()) and not (args.force or args.resume):
        sys.exit(
            f"error: {target} is not empty (use --resume to complete an interrupted "
            "restore, or --force to restore into it anyway)"
        )
    target.mkdir(parents=True, exist_ok=True)

    plat = args.platform or current_platform()
    src_plat = lock.get("source", {}).get("platform")
    if plat != "all" and src_plat and src_plat != plat:
        log(
            f"note: lockfile was made from a {src_plat} installation; restoring for {plat}. "
            f"Use --platform {src_plat} for an exact copy, or --platform all for every launcher."
        )
    sites = {s["name"]: s for s in lock["update_sites"]}
    rules = [tuple(r.split("=", 1)) for r in (args.rewrite_url or [])]
    cache = Path(args.cache).expanduser() if args.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    maven_repos = [] if args.no_maven else (args.maven_repo or DEFAULT_MAVEN_REPOS)
    dl = Downloader(args.attempts)

    wanted = [
        f for f in lock["files"] if platform_matches(plat, f.get("platforms", []))
    ]
    seen = set()
    todo = []
    for f in wanted:
        if f["path"] in seen:
            continue
        seen.add(f["path"])
        todo.append(f)
    n_mvn = sum(1 for f in todo if "maven" in f) if maven_repos else 0
    log(
        f"restoring {len(todo)} files for platform {plat} into {target}"
        + (f" ({n_mvn} tried from Maven first)" if n_mvn else "")
    )

    import threading
    from collections import Counter

    stats, stats_lock = Counter(), threading.Lock()

    def finish(f, dest, how):
        if f.get("executable"):
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        f["_size"] = dest.stat().st_size
        with stats_lock:
            stats[how] += 1
        return f

    def fetch(f):
        dest = (target / f["path"]).resolve()
        if target not in dest.parents:
            raise FetchError(f["path"], "refusing path outside the target directory")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if args.resume and dest.exists():
            try:
                if digest_matches(f["path"], dest, f["checksum"])[0]:
                    return finish(f, dest, "already present")
            except zipfile.BadZipFile:
                pass
        cached = (
            cache / f"{f['checksum']}-{Path(f['filename']).name}" if cache else None
        )
        if cached and cached.exists():
            shutil.copyfile(cached, dest)
            return finish(f, dest, "cache")

        sources = []
        if "maven" in f:
            sources += [
                ("Maven", _rewrite(maven_url(r, f["maven"]), rules))
                for r in maven_repos
            ]
        site = sites.get(f["site"])
        if site and site.get("url"):
            sources.append(
                (
                    "update site",
                    _rewrite(
                        site_file_url(site["url"], f["filename"], f["timestamp"]), rules
                    ),
                )
            )
        if not sources:
            raise FetchError(f["path"], f"unknown update site {f['site']!r}")

        problems = []
        tmp = dest.with_name(dest.name + ".part")
        for label, url in sources:
            try:
                dl.get(url, tmp, quick=(label == "Maven"))
            except FetchError as e:
                problems.append((label, e.reason, e.kind, url))
                continue
            ok, digest = digest_matches(f["path"], tmp, f["checksum"])
            if ok:
                tmp.replace(dest)
                if cached:
                    shutil.copyfile(dest, cached)
                return finish(f, dest, label)
            problems.append(
                (label, f"checksum mismatch ({digest})", "checksum mismatch", url)
            )
        tmp.unlink(missing_ok=True)
        # report the update-site problem: that is the authoritative source
        label, reason, kind, url = problems[-1]
        raise FetchError(
            url,
            f"{f['path']}: {reason}"
            + (f" [Maven: {problems[0][1]}]" if len(problems) > 1 else ""),
            kind,
        )

    errors, done = [], []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(fetch, f) for f in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                done.append(fut.result())
            except FetchError as e:
                errors.append(e)
            if i % 100 == 0 or i == len(futs):
                log(f"  {i}/{len(futs)}")
    log("sources: " + ", ".join(f"{n} {k}" for k, n in stats.most_common()))
    if errors:
        kinds = Counter(e.kind for e in errors)
        for e in errors[:25]:
            log(f"error: {e}")
        if len(errors) > 25:
            log(f"... and {len(errors) - 25} more")
        log(
            "failure summary: " + ", ".join(f"{n}x {k}" for k, n in kinds.most_common())
        )
        log(
            "re-run the same command with --resume to fetch only what is missing"
            + (
                " (and -j 2 if the server keeps throttling)"
                if any(
                    e.kind.startswith(("HTTP 403", "HTTP 429", "HTTP 503"))
                    for e in errors
                )
                else ""
            )
        )
        sys.exit(f"{len(errors)} file(s) failed; installation is incomplete")

    write_db(target, lock, sorted(done, key=lambda f: f["path"]))

    extras = lock.get("local_only", []) + lock.get("modified", [])
    if args.local_bundle:
        by_path = {e["path"]: e for e in extras}
        with zipfile.ZipFile(args.local_bundle) as zf:
            for name in zf.namelist():
                if name not in by_path:
                    log(f"warning: {name} in bundle but not in lockfile; skipped")
                    continue
                dest = (target / name).resolve()
                if target not in dest.parents:
                    sys.exit(f"error: unsafe path in bundle: {name}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                mode = (zf.getinfo(name).external_attr >> 16) & 0o777
                if mode:
                    dest.chmod(mode)  # e.g. local launcher scripts stay executable
                if sha256_file(dest) != by_path[name]["sha256"]:
                    sys.exit(f"error: {name}: sha256 mismatch with lockfile")
        log(f"added {len(zf.namelist())} local files from {args.local_bundle}")
    elif extras:
        log(
            f"note: lockfile lists {len(extras)} file(s) not on any update site; "
            "pass --local-bundle to add them"
        )

    j = lock.get("java")
    if j and not args.no_java and j.get("packages"):
        java_plats = list(j["packages"]) if plat == "all" else [plat]
        try:
            restore_java(lock, target, java_plats, dl, cache, args.resume)
        except Exception as e:  # noqa: BLE001 -- download, checksum or extraction
            sys.exit(f"error: java: {e}")
    elif j:
        log(
            f"java: not restored -- the source used {j.get('implementor', '?')} "
            f"{j.get('java_version', '?')}; provide it via JAVA_HOME / --java-home "
            "or the container base image"
        )
    log("done")


# ---------------------------------------------------------------------------
# verify / diff
# ---------------------------------------------------------------------------


def cmd_verify(args):
    lock = json.loads(Path(args.lockfile).read_text())
    root = Path(args.fiji).resolve()
    plat = args.platform or detect_dir_platform(root, lock)
    bad = 0
    expected = {}
    for f in lock["files"]:
        if platform_matches(plat, f.get("platforms", [])):
            expected.setdefault(f["path"], f)
    for e in lock.get("local_only", []) + lock.get("modified", []):
        expected.setdefault(e["path"], e)

    for path, f in sorted(expected.items()):
        p = root / path
        if not p.exists():
            if "sha256" in f and not args.strict:
                continue  # local extras are optional unless --strict
            print(f"missing   {path}")
            bad += 1
            continue
        ok = (
            (sha256_file(p) == f["sha256"])
            if "sha256" in f
            else digest_matches(path, p, f["checksum"])[0]
        )
        if not ok:
            print(f"changed   {path}")
            bad += 1
    for jplat, inst in java_installed(lock.get("java") or {}).items():
        if not platform_matches(plat, [jplat]):
            continue
        jdk = root / inst["dir"]
        if not jdk.is_dir():
            print(
                f"missing   {inst['dir']}/ (locked JDK; restore without --no-java to add it)"
            )
            if args.strict:
                bad += 1
            continue
        missing, changed, _ = compare_manifest(inst["manifest"], tree_manifest(jdk))
        for k in missing:
            print(f"missing   {inst['dir']}/{k}")
        for k in changed:
            print(f"changed   {inst['dir']}/{k}")
        bad += len(missing) + len(changed)
    for rel, _ in walk(root, DEFAULT_EXCLUDES + (args.exclude or [])):
        if rel not in expected:
            print(f"extra     {rel}")
            if args.strict:
                bad += 1
    if bad:
        print(f"FAIL: {bad} problem(s) (platform {plat})")
        print(
            "hint: `restore --resume` with the same lockfile fetches missing/changed files"
        )
        sys.exit(1)
    print(
        f"OK: {root} matches {args.lockfile} ({len(expected)} files, platform {plat})"
    )


def cmd_diff(args):
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())

    def index(lock):
        d = {}
        for f in lock["files"]:
            if f.get("resolved") == "as-of":
                continue
            d[strip_version(f["path"])] = f
        return d

    ia, ib = index(a), index(b)
    for k in sorted(set(ia) | set(ib)):
        fa, fb = ia.get(k), ib.get(k)
        if fa and not fb:
            print(f"- {fa['path']}  [{fa['site']}]")
        elif fb and not fa:
            print(f"+ {fb['path']}  [{fb['site']}]")
        elif fa["checksum"] != fb["checksum"]:
            name = (
                fa["path"]
                if fa["path"] == fb["path"]
                else f"{fa['path']} -> {fb['path']}"
            )
            print(f"~ {name}  {fa['timestamp']} -> {fb['timestamp']}  [{fb['site']}]")
    la = {
        e["path"]: e["sha256"] for e in a.get("local_only", []) + a.get("modified", [])
    }
    lb = {
        e["path"]: e["sha256"] for e in b.get("local_only", []) + b.get("modified", [])
    }
    for k in sorted(set(la) | set(lb)):
        if k not in lb:
            print(f"- {k}  [local]")
        elif k not in la:
            print(f"+ {k}  [local]")
        elif la[k] != lb[k]:
            print(f"~ {k}  [local, content changed]")
    ja = java_installed(a.get("java") or {})
    jb = java_installed(b.get("java") or {})
    for p in sorted(set(ja) | set(jb)):
        da, db_ = ja.get(p), jb.get(p)
        if (
            not da
            or not db_
            or da["dir"] != db_["dir"]
            or da["manifest"] != db_["manifest"]
        ):
            print(f"~ java/{p}  {da and da['dir']} -> {db_ and db_['dir']}")


# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="fiji-lock", description=__doc__.split("\n\n")[0].strip()
    )
    ap.add_argument("--version", action="version", version=f"fiji-lock {TOOL_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lock", help="write a lockfile for a Fiji installation")
    p.add_argument("fiji", help="Fiji directory (Fiji.app / Fiji)")
    p.add_argument("-o", "--output", default="fiji.lock.json")
    p.add_argument(
        "--local-bundle",
        dest="local_bundle",
        metavar="ZIP",
        help="also write ZIP with the files that are not on any update site "
        "(manually added jars, edited files, launcher config); "
        "pass the same ZIP to `restore --local-bundle`",
    )
    p.add_argument(  # old name of --local-bundle, kept working but not advertised
        "--bundle-local", dest="local_bundle", metavar="ZIP", help=argparse.SUPPRESS
    )
    p.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="extra path globs to ignore (repeatable)",
    )
    p.add_argument(
        "--this-platform-only",
        action="store_true",
        help="do not resolve launchers/native libs for other platforms",
    )
    p.add_argument(
        "--no-java-lookup",
        action="store_true",
        help="do not query Azul for the JDK download checksums (offline lock)",
    )
    p.add_argument(
        "--platform",
        help="platform of the installation, if auto-detection (java/<platform>/ dir, "
        "installed launchers) gets it wrong",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore the updater's .checksums cache and checksum every file",
    )
    p.add_argument(
        "--no-maven",
        action="store_true",
        help="do not record Maven coordinates (read from each jar's pom.properties)",
    )
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("restore", help="rebuild a Fiji installation from a lockfile")
    p.add_argument("lockfile")
    p.add_argument("target", help="directory to create")
    p.add_argument(
        "--platform",
        help="target platform: linux64, linux-arm64, macos64, macos-arm64, win64, win-arm64, "
        "or 'all' for a portable copy with every launcher (default: this machine)",
    )
    p.add_argument(
        "--local-bundle", metavar="ZIP", help="zip written by `lock --local-bundle`"
    )
    p.add_argument("--cache", metavar="DIR", help="download cache (keyed by checksum)")
    p.add_argument(
        "--rewrite-url",
        action="append",
        metavar="OLD=NEW",
        help="URL prefix rewrite, e.g. a mirror (repeatable)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="complete an interrupted restore: keep files that already verify, fetch the rest",
    )
    p.add_argument(
        "--maven-repo",
        action="append",
        metavar="URL",
        help=f"Maven repository to try before the update site (repeatable; default {DEFAULT_MAVEN_REPOS[0]})",
    )
    p.add_argument(
        "--no-maven", action="store_true", help="fetch everything from the update sites"
    )
    p.add_argument(
        "--no-java",
        action="store_true",
        help="do not download the locked JDK into java/<platform>/ "
        "(e.g. when a container base image provides Java)",
    )
    p.add_argument(
        "-j", "--jobs", type=int, default=4, help="parallel downloads (default 4)"
    )
    p.add_argument(
        "--attempts",
        type=int,
        default=6,
        help="attempts per URL; throttled hosts back off 15 s, 30 s, 60 s, ... (default 6)",
    )
    p.add_argument(
        "--force", action="store_true", help="restore into a non-empty directory"
    )
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("verify", help="check a Fiji installation against a lockfile")
    p.add_argument("lockfile")
    p.add_argument("fiji")
    p.add_argument(
        "--platform",
        help="platform the directory was restored for "
        "(default: detected from its java/<platform>/ folder or platform-specific files)",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="fail on extra files and on missing local-only files",
    )
    p.add_argument("--exclude", action="append", metavar="GLOB")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("diff", help="compare two lockfiles")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_diff)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
