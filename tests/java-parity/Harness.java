import java.io.*; import java.security.*; import java.util.*; import java.util.jar.*;
import net.imagej.updater.util.*;
public class Harness {
  // verbatim from UpdaterUtil
  static String toHex(byte[] bytes){StringBuilder b=new StringBuilder();for(byte x:bytes)b.append(String.format("%02x",x));return b.toString();}
  static void updateDigest(InputStream input, MessageDigest digest) throws IOException {
    final byte[] buffer = new byte[65536];
    final DigestInputStream digestStream = new DigestInputStream(input, digest);
    while (digestStream.read(buffer) >= 0);
    digestStream.close();
  }
  static String getJarDigest(File file, boolean p, boolean m, boolean k) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-1");
    final JarFile jar = new JarFile(file);
    final List<JarEntry> list = Collections.list(jar.entries());
    Collections.sort(list, (a,b)->a.getName().compareTo(b.getName()));
    for (final JarEntry entry : list) {
      digest.update(entry.getName().getBytes("ASCII"));
      InputStream inputStream = jar.getInputStream(entry);
      if (p && entry.getName().endsWith(".properties")) inputStream = new SkipHashedLines(inputStream);
      if (m && entry.getName().equals("META-INF/MANIFEST.MF")) inputStream = new FilterManifest(inputStream, k);
      updateDigest(inputStream, digest);
    }
    jar.close();
    return toHex(digest.digest());
  }
  static String getDigest(String path, File file) throws Exception {
    if (path.endsWith(".jar")) return getJarDigest(file, true, true, true);
    MessageDigest d = MessageDigest.getInstance("SHA-1");
    d.update(path.getBytes("ASCII"));
    updateDigest(new FileInputStream(file), d);
    return toHex(d.digest());
  }
  public static void main(String[] a) throws Exception {
    BufferedReader r = new BufferedReader(new InputStreamReader(System.in));
    String line;
    while ((line = r.readLine()) != null) {
      String[] t = line.split("\t");  // rel \t abspath
      File f = new File(t[1]);
      StringBuilder sb = new StringBuilder(t[0]).append('\t').append(getDigest(t[0], f));
      if (t[0].endsWith(".jar")) for (boolean[] v : new boolean[][]{{true,true,false},{true,false,false},{false,false,false}})
        sb.append('\t').append(getJarDigest(f, v[0], v[1], v[2]));
      System.out.println(sb);
    }
  }
}
