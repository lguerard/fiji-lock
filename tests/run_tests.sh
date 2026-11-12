#!/usr/bin/env bash
# End-to-end test against fake update sites + a fake Maven repository on localhost.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FL="python3 $HERE/../fiji_lock.py"
WORK="$(mktemp -d)"
PORT="${PORT:-8765}"
MVN="http://127.0.0.1:$PORT/maven/"
export FIJI_LOCK_BACKOFF=0.2   # seconds; keeps the throttling test fast
export FIJI_LOCK_AZUL_API="http://127.0.0.1:$PORT/azul/packages/"
trap 'kill $SERVER 2>/dev/null || true' EXIT

python3 "$HERE/build_fixture.py" "$PORT" "$WORK" >/dev/null
THROTTLE="/ImageJ/macros/" python3 "$HERE/serve.py" "$PORT" "$WORK/sites" &
SERVER=$!
sleep 1
cd "$WORK"

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; exit 1; }

$FL lock Fiji.app -o lock.json --local-bundle local.zip --no-cache 2>/dev/null
q() { python3 -c "import json,sys; l=json.load(open('lock.json')); print($1)"; }
file() { echo "next(f for f in l['files'] if f['path']=='$1')"; }

[ "$(q "$(file jars/foo-1.0.jar)['timestamp']")" = 20230101000000 ] \
  && pass "outdated jar pinned to its previous version" || fail "previous version"
[ "$(q "$(file lib/macos64/libz.dylib)['timestamp']")" = 20220101000000 ] \
  && pass "other-platform file resolved as of lock date (not newer)" || fail "as-of"
[ "$(q "any(f['path']=='lib/win64/old.dll' for f in l['files'])")" = False ] \
  && pass "file removed from site before lock date is skipped" || fail "obsolete"
[ "$(q "[e['path'] for e in l['local_only']]+[e['path'] for e in l['modified']]")" = "['run-fiji.sh', 'plugins/Hand_Made.jar', 'macros/edited.ijm']" ] \
  && pass "local-only and modified files detected" || fail "local files"
[ "$(q "$(file jars/dupe-1.0.jar)['timestamp']")" = 20230101000000 ] \
  && pass "jar with duplicate entries matches its update-site checksum" || fail "duplicate entries"
[ "$(q "$(file jars/legacy-1.0.jar)['timestamp']")" = 20120404210913 ] \
  && pass "2012-era record with an obsolete-format checksum is matched" || fail "legacy lock"
[ "$(q "$(file jars/bar-2.0.jar)['maven']")" = org.example:bar:2.0 ] \
  && pass "Maven coordinates read from pom.properties" || fail "maven coord"

# --- restore (Maven first, throttled macros/ path) ---
$FL restore lock.json linux --platform linux64 --local-bundle local.zip --maven-repo "$MVN" 2>restore.log \
  || { cat restore.log; fail "restore"; }
grep -q "throttling" restore.log && pass "403 triggers a shared back-off, then succeeds" || fail "throttle"
grep -q "sources: .*1 Maven" restore.log && pass "re-packaged jar accepted from Maven (same updater checksum)" || { cat restore.log; fail "maven source"; }
cmp -s Fiji.app/jars/foo-1.0.jar linux/jars/foo-1.0.jar \
  && pass "Maven jar with different code rejected, update-site copy used" || fail "maven fallback"
$FL verify lock.json linux --platform linux64 --strict >/dev/null && pass "verify --strict" || fail "verify"
[ -x linux/fiji-linux-x64 ] && pass "launcher is executable" || fail "exec bit"
[ -e linux/jars/legacy-1.0.jar ] && pass "... and restored + verified (restore accepts obsolete-format checksums too)" || fail "legacy restore"

$FL restore lock.json exact --platform linux64 --local-bundle local.zip --no-maven 2>/dev/null
diff -r -x db.xml.gz -x java Fiji.app exact >/dev/null && pass "--no-maven restore is byte-identical" || fail "restore differs"

$FL restore lock.json mac --platform macos64 --maven-repo "$MVN" 2>/dev/null
[ "$(cat mac/lib/macos64/libz.dylib)" = dylib-old ] && [ ! -e mac/fiji-linux-x64 ] \
  && pass "cross-platform restore (macos64)" || fail "mac restore"

# --- a Windows installation locked from this (Linux) machine ---
cp -r Fiji.app Win.app && rm Win.app/fiji-linux-x64 && rm -r Win.app/java/linux64
python3 -c "import zipfile; zipfile.ZipFile('sites/azul-cdn/zulu21.42.19-ca-jdk21.0.7-win_x64.zip').extractall('Win.app/java/win64')"
cp sites/ImageJ/fiji-windows-x64.exe-20230201000000 Win.app/fiji-windows-x64.exe
$FL lock Win.app -o win.json --no-cache 2>win.log
wq() { python3 -c "import json; l=json.load(open('win.json')); print($1)"; }
[ "$(wq "l['source']['platform']")" = win64 ] && pass "install platform detected from java/win64, not from this machine" || fail "source platform"
[ "$(wq "next(f.get('resolved') for f in l['files'] if f['path']=='fiji-linux-x64')")" = as-of ] \
  && pass "Linux launcher pinned although only the Windows one is installed" || fail "linux launcher missing"
$FL restore win.json win-copy --platform win64 --maven-repo "$MVN" 2>/dev/null
[ -e win-copy/fiji-windows-x64.exe ] && [ ! -e win-copy/fiji-linux-x64 ] && pass "restore --platform win64" || fail "win64 restore"
$FL restore win.json win-linux --maven-repo "$MVN" 2>wl.log
[ -x win-linux/fiji-linux-x64 ] && grep -q "made from a win64 installation" wl.log \
  && pass "restore on Linux gets the Linux launcher and a platform note" || fail "linux restore of windows lock"
$FL restore win.json portable --platform all --maven-repo "$MVN" 2>/dev/null
[ -e portable/fiji-windows-x64.exe ] && [ -x portable/fiji-linux-x64 ] && [ -e portable/lib/macos64/libz.dylib ] \
  && pass "restore --platform all includes every launcher" || fail "all platforms"

# --- Java ---
[ "$(q "sorted(p for p,v in l['java']['packages'].items() if v.get('sha256'))")" = "['linux64', 'macos-arm64', 'macos64', 'win64']" ] \
  && [ "$(q "l['java']['packages']['linux-arm64']['error']")" = "not published by Azul for this platform" ] \
  && pass "lock pins the Azul JDK with sha256 for every published platform" || fail "java packages"
[ -x linux/java/linux64/zulu21.42.19-ca-jdk21.0.7-linux_x64/bin/java ] && grep -q "JDK identical to the locked one" restore.log \
  && pass "restore installs the JDK and proves it identical to the locked one" || { cat restore.log; fail "java restore"; }
$FL restore win.json win-full --platform win64 --maven-repo "$MVN" 2>wf.log
grep -q "win64 JDK identical" wf.log && [ -e win-full/java/win64/zulu21.42.19-ca-jdk21.0.7-win_x64/bin/java.exe ] \
  && $FL verify win.json win-full >/dev/null \
  && pass "complete Windows Fiji (incl. its JDK) rebuilt from Linux and verified" || { cat wf.log; fail "win java"; }
[ -L portable/java/macos-arm64/zulu21.42.19-ca-jdk21.0.7-macosx_aarch64/bin ] && [ -e portable/java/linux64 ] && [ ! -e portable/java/linux-arm64 ] \
  && pass "--platform all installs every published JDK (macOS symlink kept)" || fail "all java"
cp -r Fiji.app Mod.app && echo patched >> Mod.app/java/linux64/zulu21.42.19-ca-jdk21.0.7-linux_x64/lib/modules
$FL lock Mod.app -o mod.json --no-cache 2>/dev/null
if $FL restore mod.json modcopy --platform linux64 --maven-repo "$MVN" 2>mod.log; then fail "modified JDK not detected"; \
  else grep -q "differs from the locked JDK" mod.log && pass "a modified bundled JDK is detected on restore" || { cat mod.log; fail "mod jdk msg"; }; fi
$FL restore lock.json nojava --platform linux64 --maven-repo "$MVN" --no-java 2>/dev/null
[ ! -e nojava/java ] && $FL verify lock.json nojava --platform linux64 >/dev/null \
  && pass "--no-java skips Java; verify still passes (JDK only required with --strict)" || fail "no-java"
echo x >> linux/java/linux64/zulu21.42.19-ca-jdk21.0.7-linux_x64/release
if $FL verify lock.json linux --platform linux64 >/dev/null; then fail "verify missed JDK change"; else pass "verify checks the JDK"; fi

# --- review fixes (0.3.0) ---
$FL verify win.json win-copy | grep -q "platform win64" && pass "verify detects the platform of the directory (no --platform)" || fail "verify platform detect"
[ -x linux/run-fiji.sh ] && pass "local-only launcher script stays executable (--local-bundle)" || fail "bundle mode"

# portable install with a JDK per platform: each JDK fingerprinted under its own platform
cp -r Win.app Port.app
python3 -c "import tarfile; tarfile.open('sites/azul-cdn/zulu21.42.19-ca-jdk21.0.7-linux_x64.tar.gz').extractall('Port.app/java/linux64', filter='data')"
$FL lock Port.app -o port.json --no-cache 2>/dev/null
[ "$(python3 -c "import json; print(sorted(json.load(open('port.json'))['java']['installed']))")" = "['linux64', 'win64']" ] \
  && $FL restore port.json port-win --platform win64 --maven-repo "$MVN" 2>pw.log && grep -q "win64 JDK identical" pw.log \
  && $FL restore port.json port-lin --platform linux64 --maven-repo "$MVN" 2>pl.log && grep -q "linux64 JDK identical" pl.log \
  && pass "portable install: every bundled JDK locked and verified under its own platform" || { cat pw.log pl.log; fail "multi-jdk"; }

# --resume keeps a JDK that is already identical
$FL restore lock.json rjava --platform linux64 --maven-repo "$MVN" 2>/dev/null
$FL restore lock.json rjava --platform linux64 --maven-repo "$MVN" --resume 2>rj.log
grep -q "linux64 JDK already present" rj.log && pass "--resume keeps an identical JDK" || { cat rj.log; fail "resume java"; }

# a Maven 403 falls through to the update site at once; a file-specific 403 does not stall the run
kill $SERVER; sleep 0.3
FORBID="/maven/org/example/bar/" python3 "$HERE/serve.py" "$PORT" "$WORK/sites" & SERVER=$!; sleep 0.5
SECONDS=0
$FL restore lock.json mvn403 --platform linux64 --maven-repo "$MVN" --no-java 2>m403.log
grep -q "sources: .*update site" m403.log && ! grep -q "throttling" m403.log && [ $SECONDS -lt 10 ] \
  && pass "Maven 403 falls back to the update site immediately (${SECONDS}s)" || { cat m403.log; fail "maven 403"; }
kill $SERVER; sleep 0.3
FORBID="/ImageJ/jars/bar-2.0.jar" python3 "$HERE/serve.py" "$PORT" "$WORK/sites" & SERVER=$!; sleep 0.5
SECONDS=0
if $FL restore lock.json forbid --platform linux64 --no-maven --no-java -j 4 2>f403.log; then fail "forbidden file not reported"; fi
grep -q "refused while other files download fine" f403.log && [ $SECONDS -lt 30 ] \
  && pass "file-specific 403 reported without stalling (${SECONDS}s)" || { cat f403.log; fail "file 403"; }
kill $SERVER; sleep 0.3
THROTTLE="/ImageJ/macros/" python3 "$HERE/serve.py" "$PORT" "$WORK/sites" & SERVER=$!; sleep 0.5

# a malicious lockfile cannot write outside the target
python3 -c "
import json; l=json.load(open('lock.json')); f=dict(l['files'][0]); f['path']='../../escape.txt'; l['files'].append(f)
json.dump(l, open('evil.json','w'))"
$FL restore evil.json evil --platform linux64 --no-maven --no-java 2>evil.log || true
[ ! -e escape.txt ] && [ ! -e ../escape.txt ] && grep -q "outside the target" evil.log \
  && pass "lockfile paths outside the target are refused" || { cat evil.log; fail "path traversal"; }

# diff reports local-file and JDK changes
$FL diff lock.json mod.json > d.txt
grep -q "java/linux64" d.txt && pass "diff reports JDK changes" || { cat d.txt; fail "diff java"; }

# --- an unreachable Maven repository is given up on quickly ---
SECONDS=0
$FL restore lock.json deadmvn --platform linux64 --maven-repo http://127.0.0.1:9/ 2>dead.log
grep -q "unreachable; skipping" dead.log && [ $SECONDS -lt 30 ] && $FL verify lock.json deadmvn --platform linux64 >/dev/null \
  && pass "unreachable Maven repo skipped after 3 files, update sites used (${SECONDS}s)" || { cat dead.log; fail "dead host"; }

# --- failures, then --resume ---
echo tampered >> linux/macros/x.ijm
if $FL verify lock.json linux --platform linux64 >/dev/null; then fail "tamper not detected"; else pass "verify detects tampering"; fi

mv sites/ImageJ/jars/bar-2.0.jar-20230501000000 bar.bak
if $FL restore lock.json partial --platform linux64 --no-maven --attempts 1 2>/dev/null; then fail "missing file not reported"; else pass "missing file makes restore fail"; fi
mv bar.bak sites/ImageJ/jars/bar-2.0.jar-20230501000000
$FL restore lock.json partial --platform linux64 --no-maven --resume 2>resume.log
grep -q "already present" resume.log && grep -q "sources: .*1 update site" resume.log \
  && $FL verify lock.json partial --platform linux64 >/dev/null \
  && pass "--resume fetches only the missing file" || { cat resume.log; fail "resume"; }

python3 -c "import zipfile; zipfile.ZipFile('sites/ImageJ/jars/bar-2.0.jar-20230501000000','a').writestr('evil.class','x')"
if $FL restore lock.json bad --platform linux64 --no-maven 2>/dev/null; then fail "bad download accepted"; else pass "restore rejects a file whose checksum changed"; fi

echo "all tests passed ($WORK)"
