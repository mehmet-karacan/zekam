# Paketi Yerleştirme ve Nihai Proje Entegrasyonu

## A. Uygulama paketini başlatma

1. Yeni, boş bir dizin/repository oluştur:

```bash
mkdir zekam
cd zekam
git init
```

2. Bu ZIP'in **üst klasör içeriğini** repository köküne çıkar. Sonuçta
`zekam/00_BASLA.md` doğrudan bulunmalıdır; iki kat `ZEKAM_NIHAI_UYGULAMA_PAKETI/` nesting
bırakma.

3. Ham yerel referansların Git-ignore edildiğini doğrula:

```bash
git check-ignore yerel-referanslar/README.md
```

4. Paketi doğrula:

```bash
python scripts/paket_dogrula.py
```

5. Herhangi bir modeli/CLI'ı repository kökünde aç ve:

```text
00_BASLA.md dosyasini uygula ve Global Definition of Done tamamlanana kadar devam et.
```

talimatını ver.

6. İlk uygulama task'ı `ZEKAM-P00-T01`'dir. Model iş grafiğini kanıtla ilerletir.

## B. Uygulama tamamlandıktan sonra hedef kullanım

Aşağıdaki komutlar nihai üründe çalışmalıdır:

```bash
zekam doctor
zekam init
zekam project add /kaynak/proje --name "GPU Fusion" --alias gpu
zekam project scan gpu
zekam ask "gpu projesindeki 123 numarali defectin kok nedenini arastir"
zekam work show 123 --project gpu
zekam project resume gpu
zekam report today
```

Bu paket komutların implementasyonunu tarif eder; paket tek başına henüz executable sağlamaz.

## C. Proje entegrasyonunun güvenli akışı

1. Source root exact Git root ve read-only binding olarak kaydedilir.
2. Project ID, slug ve alias oluşturulur.
3. Source HEAD/tree/fingerprint kaydedilir.
4. Discovery secret/symlink/generated/binary filtreleriyle çalışır.
5. Capability Profile framework/version/module/DB/build/test kanıtlarını çıkarır.
6. Work/talep/defect import planı dry-run üretir.
7. Knowledge index planı source'u kopyalamadan hazırlanır.
8. Model benchmark suite proje profile'ından türetilir.
9. Entegrasyon test/verification geçince current olur.
10. Source değişikliği incremental scan ve staleness üretir.

## D. Mutation

Kullanıcı projede geliştirme istediğinde:
- source main tree read-only,
- Zekam worktree oluşturur,
- exact path/resource planı,
- single builder,
- test/verifier,
- patch/receipt,
- policy'ye göre local commit,
- push explicit.

## E. Ürün kimliği

Kurulum yalnız `mimari/ZEKAM_KIMLIK_SOZLESMESI.md` içindeki package, CLI, environment,
home, schema ve DB adlarını kullanır. Başka ürün namespace'iyle birleştirme yapılmaz.
