# fiji-lock

A lockfile for Fiji: record exactly which version of every file a Fiji
installation contains, then rebuild that same installation later, on any
platform, straight from the ImageJ update sites.

```bash
python3 fiji_lock.py lock    Fiji.app -o fiji.lock.json --local-bundle fiji.local.zip
python3 fiji_lock.py restore fiji.lock.json Fiji-restored --local-bundle fiji.local.zip
python3 fiji_lock.py verify  fiji.lock.json Fiji-restored
python3 fiji_lock.py diff    old.lock.json new.lock.json
```

It is a single file that uses only the Python standard library (Python 3.9+).

## Why this works

ImageJ update sites never delete files. Every upload stays available as
`<site-url>/<path>-<yyyyMMddHHmmss>`, and the site's `db.xml.gz` lists the
checksum of every version ever published. Fiji keeps a merged copy of those
databases in `Fiji.app/db.xml.gz`.

`lock` checksums every file in your installation and looks each checksum up in
that history. That gives the exact update site and timestamp for each file,
including files you never updated. `restore` downloads those exact versions
and checks each one against the recorded checksum.

### Maven first, update sites as fallback

Most jars in `jars/` and `plugins/` are Maven artifacts. `lock` reads each
jar's own `META-INF/maven/.../pom.properties` and records
`"maven": "groupId:artifactId:version"`. `restore` then tries
`https://maven.scijava.org/content/groups/public/` first; that repository
also proxies Maven Central. Fetching from Maven takes most of the load off
`sites.imagej.net`, which throttles large downloads.

Every download, whichever source it came from, must match the **updater**
checksum recorded in the lockfile. That checksum deliberately ignores the
manifest and the build-date comment in `pom.properties`. So a Maven copy of the
same build is accepted, even though those metadata bytes may differ. A Maven
copy with different code is rejected and the update-site file is used instead.
If you need byte-identical files, use `--no-maven`.

The checksum algorithm is a port of the ImageJ updater's own
(`UpdaterUtil.getJarDigest`, `SkipHashedLines`, `FilterManifest`), so the
values match the update-site records. On 406 real jars and edge cases it
produced results identical to the updater's Java code; see
`tests/java-parity/`. One of those edge cases is jars with duplicate entry
names: Java's `JarFile` returns the *last* duplicate for every occurrence, and
the port does the same.

## What goes in the lockfile

| key | content |
|---|---|
| `files` | Each pinned file: path, update site, filename on the site, timestamp, updater checksum, platforms, executable flag |
| `update_sites` | Name and URL of each site, plus a `cutoff` (the newest pinned timestamp from that site) |
| `local_only` | Files that are not on any update site, such as jars you copied in by hand or launcher config (sha256) |
| `modified` | Files that are on an update site but whose content matches no published version, such as an edited macro (sha256) |
| `java` | The bundled JDK: version, a sha256 for every file, and the matching Azul Zulu download (URL + sha256) for each platform |

**Files in `local_only` and `modified` can't be fetched from any update site.**
`--local-bundle` zips them so you can archive the zip alongside the lockfile.
`restore --local-bundle` puts them back and checks their sha256.

### Cross-platform restore

Launchers and native libraries are platform-specific. By default, `lock`
also pins those files for other platforms: for each one it takes the version
that was current on its update site at that site's cutoff date. That lets you
lock on a Mac laptop and rebuild for `linux64` on a cluster:

```bash
python3 fiji_lock.py restore fiji.lock.json /scratch/Fiji --platform linux64
```

Platform names follow the updater: `linux64`, `linux-arm64`, `macos64` (Intel),
`macos-arm64`, `win64`, `win-arm64`.

`lock` detects the installation's platform from the install itself (its
`java/<platform>/` folder, or the launchers it contains), not from the machine
running `lock`. So you can lock a Windows Fiji from Linux, for example on a
shared drive (override with `lock --platform`). `restore` builds for the
current machine by default. Use `--platform win64` to get an exact copy of
a Windows install, or `--platform all` for a portable copy that includes the
launchers for every OS.

The files installed on your own machine are pinned exactly. Files for other
platforms are a best reconstruction, marked `"resolved": "as-of"` in the
lockfile. Use `--this-platform-only` to skip them.

### Java

Fiji bundles an Azul Zulu JDK in `java/<platform>/<build>/`, for example
`java/win64/zulu21.42.19-ca-jdk21.0.7-win_x64/`. A portable install can contain
one JDK per platform, and each one is fingerprinted under its own platform. That folder name is also the
name of Azul's download archive. For Java, `lock` does three things:

1. It records a sha256 for every file of the installed JDK.
2. It asks Azul's metadata API (`api.azul.com`) for the same build on
   every platform and records each download URL with Azul's published sha256.
   Platforms Azul doesn't publish that build for are marked as such.
   `--no-java-lookup` skips this step if you are offline.
3. `restore` then downloads the archive for the target platform, checks its
   sha256 and unpacks it into `java/<platform>/`. For the platform the lockfile
   was made from, it also compares every file with the recorded fingerprint, so
   the restored JDK is proven identical to the one you analysed with.
   `--platform all` installs the JDK for every platform.

`--no-java` skips Java, for example when a container base image provides it.
JDKs that aren't standard Zulu builds (such as Fiji Stable's Java 8) are still
fingerprinted, so `verify` can check them, but they can't be downloaded again.

## Publishing an analysis

1. Set up Fiji, run the analysis, then lock it:
   `fiji_lock.py lock Fiji.app --local-bundle fiji.local.zip`
2. Commit `fiji.lock.json` (and the zip, if `lock` wrote one) next to your scripts.
   The lockfile is plain JSON, so changes show up clearly in diffs, and
   `fiji_lock.py diff` gives a summary of what changed.
3. Build a container from it with `apptainer build fiji.sif fiji.def` or
   `docker build .`. The build restores Fiji and then runs `verify`, so it fails
   if anything differs from the lockfile.
4. Archive the image and the lockfile on Zenodo for a DOI. The lockfile also
   works as a human-readable record of the software environment for your
   methods section.

Example run:

```bash
apptainer run fiji.sif --console --run analysis.groovy 'input="/data/img.tif"'
```

## Options worth knowing

- `restore --resume`: finishes an interrupted or partly failed restore. Files
  that already verify are kept, and only the rest are downloaded.
- `restore -j N` / `--attempts N`: sets parallel downloads (default 4) and attempts per URL
  (default 6). On HTTP 403/429/503 every worker pauses for that host, for
  15 s, 30 s, 60 s and so on (at most 2 minutes), instead of retrying straight
  away. A URL that keeps being refused while other files download fine is
  reported as forbidden instead of retried. An HTTP error from Maven falls back to
  the update site straight away.
- `restore --maven-repo URL` (repeatable) / `--no-maven`: chooses the Maven
  repositories, or turns Maven off. `lock --no-maven` skips recording coordinates.
- `restore --cache DIR`: keeps downloads, keyed by checksum, so rebuilding is fast.
- `restore --rewrite-url OLD=NEW`: fetches from a mirror instead, e.g. Institut Pasteur's
  `https://mirrors.pasteur.fr/fiji/sites/`, or your own copy of the update sites.
- `lock --exclude GLOB`: skips paths such as large sample data inside `Fiji.app`.
- `lock --no-cache`: by default the tool reuses checksums from the updater's
  `.checksums` file when a file's modification time hasn't changed. This flag
  recomputes them all.
- `verify --strict`: also fails on extra files, missing local-only files and a missing JDK.

## Limitations

- Java can only be re-downloaded if it is a standard Azul Zulu build, which is
  what Fiji Latest ships. For anything else, provide the JDK yourself (a
  container base image, `JAVA_HOME` or `--java-home`) and restore with `--no-java`.
- Restoring depends on each update site keeping its history. Sites hosted on
  `sites.imagej.net` and the official sites do. Self-hosted sites might not.
  For those, mirror the files or rely on `--local-bundle`.
- Plugins that download their own models or binaries at runtime (some deep-learning
  plugins, conda environments) are outside the scope of this tool. Pin those
  separately.
- The restored `db.xml.gz` is minimal: it lists the sites and the pinned files.
  If you open *Help › Update…* in the restored Fiji, it will offer the newest
  versions. Don't apply them if you want to stay pinned.

## Safety

`restore` treats a lockfile as untrusted input, because it may come from
someone else's paper:

- every file must match the checksum recorded in the lockfile;
- paths that would land outside the target directory are refused;
- JDK archives are checked against Azul's sha256 and unpacked safely, with no
  path traversal and no links pointing outside the target.

## Tests

```bash
tests/run_tests.sh   # end-to-end test against two fake update sites on localhost
```

The tests cover:

- pinning to a previous version;
- other-platform files resolved as of the lock date;
- files removed from a site before that date;
- local and modified files;
- a jar with duplicate entries;
- Maven source accept and reject;
- the locked JDK on every platform (including portable installs with several JDKs), and detection of a modified one;
- Maven 403 fallback, file-specific 403s, refused path traversal;
- a Windows install locked and rebuilt from Linux;
- HTTP 403 back-off;
- `--resume`;
- a tampered download.

`tests/java-parity/Harness.java` is the harness used to compare checksums
against the updater's Java code. Compile it together with `SkipHashedLines.java`
and `FilterManifest.java` from imagej-updater, then feed it `relpath<TAB>abspath`
lines.

## Releasing

Commits follow `type(scope): gitmoji description` (for example
`fix(restore): 🐛 …` or `feat(lock): ✨ …`), and [git-cliff](https://git-cliff.org)
builds the changelog from them using `cliff.toml`.

1. Preview the next version and its changelog:

   ```bash
   git cliff --bumped-version          # e.g. v0.4.0
   git cliff --unreleased --bump       # the notes that release will get
   ```

   The first release is `v0.3.0`, the version `fiji_lock.py` already reports
   (`initial_tag` in `cliff.toml`).

2. Set `TOOL_VERSION` in `fiji_lock.py` to that version and commit it as
   `chore(version): 🔖 v0.4.0`. Version commits are left out of the changelog.
3. Tag and push:

   ```bash
   git tag v0.4.0 && git push origin v0.4.0
   ```

The `Release` workflow checks that the tag matches `TOOL_VERSION`, runs the
tests, and publishes a GitHub release with the changelog and `fiji_lock.py`
attached. Every lockfile records the version that wrote it (`generator`), and
a paper can link to the matching release.
