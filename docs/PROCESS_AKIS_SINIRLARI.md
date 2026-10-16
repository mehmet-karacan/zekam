# Process Akis Sinirlari

Genel typed process runner artik stdout/stderr'i islem bittikten sonra kirpmaz.
Her stream mevcut max_output_bytes sinirinda concurrently okunur; buffer en
fazla limit+1 bayttir. Overflow gorulunce owned process group / Windows Job
Object durdurulur. Canonical ProcessResult yalniz bounded cikti digest'lerini
tasir; truncated sonuc exit=0 olsa da succeeded olamaz.

Timeout mevcut 124 sonucunu korur. Iptal event'i onceden set ise child acilmaz;
aktif iptal ve Ctrl+C child/pipes temizliginden sonra yeniden yukseltilir.
Normal parent exit'i de pipe'lari acik tutan owned descendants'i birakamaz.
POSIX'te yalniz runner'in yeni session ile olusturdugu group'a sinyal gider;
inherited/unowned group oldurulmez. Cleanup sonrasinda group ownership tuketilir.
Stderr handle'i stdout/stdin ile birlikte kapanir. Bu sinir tam bir OS sandbox
veya host-native tool permission enforcement yerine gecmez.

Client adapter strict UTF-8, duplicate-key/nonfinite JSON reddi ve gozlenen exit
status bagini uygular. Truncated, bozulan veya actual nonzero process'in success
JSON'u basari sayilmaz. Gecerli failure envelope gercek exit status'uyla korunur.
JSON'daki exit_code gercek status'u gecersiz kilamaz. Ham stderr/transcript hata
metnine tasinmaz. Env allowlist ve shell-free argv mevcut sozlesmesini korur.

Root cause kaynak uzerinden saptandi: subprocess.run(capture_output=True)
butun ciktiyi bellege aldiktan sonra sinir uyguluyordu; adapter da gozlenen exit
yerine model/wrapper JSON status'una guvenebiliyordu. Systematic-debugging
workflow'unda kaynak/data-flow ve ortak bounded stream primitive'i karsilastirildi.
Kullanici talimatiyla reproduction ve regresyon kosulari final test asamasindadir.

Regresyon kaynaklari: `tests/integration/test_process_stream_limits.py`,
`tests/e2e/test_client_adapters.py`, `tests/fixtures/fake_client.py`. Gercek yerel
Python child'larla stdout/stderr flood, interruption, pre-cancel, terminal parent
descendant pipe ve forged exit/JSON senaryolari hazir, CALISTIRILMADI.

WP-09 explicit OpenCode batch composition native run/event/CAS adapter'ine
baglandi; `docs/OPENCODE_NATIVE_BATCH.md`. Fixture basarisi gercek OpenCode
modeli veya unit-test engineering kabulu sayilmaz. Native Maven measurement
yolu bu model adapter'ini kullanmaz.

## Digest-bound stdin

`ProcessSpec.stdin_digest` istege bagli girdinin hash'ini process spec'e baglar.
Eski stdin'siz spec digest'leri degismez. `runner.run(stdin=...)` yalniz bounded
bytes kabul eder; boyut/digest hatasini child baslamadan reddeder. Prompt ham metni
argv, ProcessSpec veya ProcessResult kaydina eklenmez.

Stdin ayri thread'de raw fd uzerinden yazilir; stdout/stderr ayni anda drain
edilir. Caller pipe yazisinda bloklanmaz. Timeout/cancel/Ctrl+C mevcut owned
process-tree cleanup'a gider. Normal cikista tam teslim edilmeyen girdi sonuc
olarak kabul edilmez. Stdin ust siniri 262144 byte'tir.

Bu parca OpenCode batch icin Windows argv sinirini asmadan CAS context tasima
zeminidir. Native run/event parsing ve config tool-denial baglantisi batch
adapter'inda kurulur. Yerel Python child regresyon kaynaklari eklendi;
CALISTIRILMADI.
