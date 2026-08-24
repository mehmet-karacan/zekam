# Sandbox teslim ve istemci adaptorleri

## Neden bound-source policy

Entegre kaynak project registry'de bagli **gercek source root**'tur. Builder exact path
allowlist, authorization, claim ve tek-writer logical lock ile dogrudan bu kokte yazar.
Kopya, mirror, audit-work klasoru, detached worktree veya gecici proje klonu uretilmez.
HEAD ve tree parmak izi islem oncesi ve sonrasi karsilastirilir.

## Yazma sinirlari

`PathAllowlist` bos olamaz — "her yere yazabilir" bir sandbox degildir.

- Girdi bir dizinse altindaki yollar izinlidir; disindaki her yol reddedilir.
- Onek eslemesiyle kacilamaz: `docs` izinliyken `docs-gizli/` izinli **degildir**.
- Absolute path, `..` traversal ve ters bolu ayirici reddedilir.
- Symlink kacisi bagli source kokunde cozulerek yakalanir: cozulmus hedef kokun
  disina cikamaz.

## Network

`NetworkPolicy` varsayilan olarak **default-deny**'dir. Izin exact host **ve**
exact operasyon listesi ister; yalniz host vermek `PolicyViolation` uretir.

## Typed process runner

Shell yoktur: komut daima argv listesidir ve `shell=False` ile calisir.

| Kisit | Davranis |
|---|---|
| Calistirilabilir alan | shell metakarakteri veya bosluklu komut satiri reddedilir |
| Arguman | satir sonu reddedilir; `;` ve `$` serbesttir (kabuga ulasmaz) |
| Timeout | zorunlu, 1..3600 saniye |
| Ortam | allowlist; cagiran surecin ortami devralinmaz |
| Cikti | bayt siniri uygulanir, asilirsa `truncated = true` |
| Kayit | ham cikti degil yalniz `stdout_digest` / `stderr_digest` |

Arguman icindeki metakarakter neden serbest? `shell=False` oldugundan bir
argumanin icindeki `;` kabuga hic ulasmaz ve mesru olabilir — ornegin
`python -c "import os; ..."`. Gercek risk calistirilabilir alanina gizlenmis bir
komut satiridir; kontrol oraya uygulanir.

## Teslim akisi

```text
prepare (bound real source root + exact path allowlist)
  -> builder dogrudan gercek source dosyasina yazar
  -> changed-path kaniti ve tree fingerprint uretilir
  -> run_tests (typed, bagli source rootunda)
  -> deliver: drift kontrolu -> test kaniti
  -> DeliveryDecision -> receipt uygunlugu
  -> source tree yeniden dogrulanir
```

Kurallar:

- **Drift**: plan revision'i degistiyse veya yama plan disinda bir yola dokunduysa
  sonuc `drifted` olur; teslim durur.
- **No-copy**: patch'i baska bir proje kopyasinda hazirlayip hedefe tasimak yasaktir.
- **Bagimsiz test**: builder'in "gecti" demesi yeterli degildir; Zekam testleri
  kendisi calistirir.
- **Verifier**: `verifier_ref` ile `builder_ref` ayni olamaz.
- **Receipt**: yalniz `applied` teslim `receipt_eligible` olur. Bu bile mutation
  izni degildir; effect claim ve exact authorization zinciri ayrica calisir.

## Istemci adaptorleri

Core hicbir istemciye baglanmaz; Codex, Claude Code, OpenCode ve kurum ici
modeller birer adapter'dir.

- Her istemci **exact calistirilabilir dosya** beyan eder.
- Yetenekler acikca beyan edilir; beyan edilmeyen yetenek cikarim yoluyla
  varsayilmaz (`assert_supports` reddeder). Bilinmeyen yetenek adi da reddedilir.
- Komut satiri talimat **metni** degil `instruction_digest` tasir; secret gecmez.
- Sonuc strict JSON envelope'dur. Ayristirilamayan cikti, bilinmeyen `outcome` ve
  liste tipli payload sessizce kabul edilmez; `failed` olarak gorunur kalir.
- Timeout iptaldir; istemci `cancellation` beyan etmiyorsa timeout gorunur hata
  uretir.
- `grants_authority` her zaman `false`'tur.

Kayitli olmayan istemci turetilmez: `ClientRegistry.get` bilinmeyen kimlik icin
`PolicyViolation` uretir.

## Execution environment snapshot

Bir calisma ortami yalniz host path veya tek bir serbest digest ile tanimlanmaz.
`ExecutionEnvironmentSnapshot` asagidaki boyutlari birlikte ve append-only saklar:

- environment-native `cwd_locator` ve canonical sirali workspace root'lari;
- shell turu, binary ve startup profile digest'leri;
- permission profile ile filesystem/network policy digest'leri;
- tool runtime, capability ve effective config digest'leri;
- executor protocol, platform, source revision ve exact expiry.

Host absolute path kanonik snapshot'a giremez; adapter sinirina kadar
`workspace:<logical-root>` locator'i kullanilir. Snapshot authority uretmez.

Iki farkli okuma semantigi vardir:

1. `initialize`, ayni execution identity icin yapiskan snapshot'i bir kez alir;
   clone/recovery boyunca cache'deki exact nesneyi dondurur.
2. `force_probe`, cache'i atlar ve executor'dan current snapshot alir. Workspace,
   shell, permission, filesystem, network, tool runtime, capability, config ve source
   drift'leri ayri reason code'lardir.

DB, probe reason code listesini sticky/current snapshot'lardan yeniden hesaplar;
sahte veya eksik listeyi reddeder. Assignment once exact environment snapshot'a
baglanir. Her attempt icin `TurnExecutionSnapshot`; assignment/run/attempt, route,
reasoning, context, tool set, hook set ve config'i ayni environment'a baglar.
`ExecutionEnvelope` bu turn snapshot olmadan yazilamaz. Gateway enforce modunda model
manifest'i environment, permission, tool, hook ve config ile exact eslesmezse provider
effect baslamadan reddedilir. Expired environment veya son bes dakikada drift'siz force
probe kaniti bulunmamasi da fail-closed'dur.

## Commit ve push kapisi

`kalite/COMMIT_POLITIKASI.md` kod olarak uygulanir:

```bash
zekam git commit-check --dosya .git/COMMIT_EDITMSG --json
zekam git push-check origin main <head> --kullanici-istedi --yetki-digest <d> \
  --test-gecti --verifier-gecti --json
```

- Baslik `<tur>: <kisa emir cumlesi>`; yalniz izinli on turler.
- **ASCII disi karakter reddedilir**; icerik Turkce anlam tasir.
- Zorunlu govde bolumleri: Neden, Degisiklik, Kanit, Risk, Geri donus.
- "update", "wip", "fix stuff", yalniz issue kimligi reddedilir.
- Secret ve kisisel absolute path reddedilir.
- `Merge` / `Revert` mesajlarina controlled exception verilir.

Push kapisi sirasi: **acik kullanici talebi -> exact authorization -> test ->
verifier**. Varsayilan karar reddir ve force push hicbir kosulda otomatik izinli
degildir. Politika ihlali `6` cikis kodu dondurur.
