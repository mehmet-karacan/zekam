# Sandbox teslim ve istemci adaptorleri

## Neden sandbox

Entegre kaynak **main tree read-only**'dir ve bu bir tercih degil, kapatilamaz bir
kisittir: `SandboxPolicy(main_tree_read_only=False)` `PolicyViolation` uretir. Her
builder kendi detached worktree'sinde calisir; main tree'nin HEAD ve tree parmak
izi islem oncesi ve sonrasi karsilastirilir.

## Yazma sinirlari

`PathAllowlist` bos olamaz — "her yere yazabilir" bir sandbox degildir.

- Girdi bir dizinse altindaki yollar izinlidir; disindaki her yol reddedilir.
- Onek eslemesiyle kacilamaz: `docs` izinliyken `docs-gizli/` izinli **degildir**.
- Absolute path, `..` traversal ve ters bolu ayirici reddedilir.
- Symlink kacisi worktree kokunde cozulerek yakalanir: cozulmus hedef kokun
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
prepare (detached worktree)
  -> builder yazar (yalniz allowlist icine)
  -> build_artifact (git diff -> PatchArtifact)
  -> run_tests (typed, sandbox icinde)
  -> deliver: drift kontrolu -> git apply --check -> test kaniti
  -> DeliveryDecision -> receipt uygunlugu
  -> discard (worktree kaldirilir, main tree yeniden dogrulanir)
```

Kurallar:

- **Drift**: plan revision'i degistiyse veya yama plan disinda bir yola dokunduysa
  sonuc `drifted` olur; teslim durur.
- **apply-check**: yama hedefe uygulanmadan once `git apply --check` ile dogrulanir.
  Girdi bayt olarak verilir; text modunda Windows satir sonu cevrimi yamayi bozar.
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
