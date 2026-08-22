# Zekam Devam Protokolü

## Amaç

Model, sağlayıcı, CLI, context window veya makine değişse bile aynı Work Item'dan güvenli
şekilde devam etmeyi sağlar. Sohbet transcript'i kanonik değildir.

Kullanım limiti tükendiği için işi **başka bir modele** devrederken kullanılacak
hazır metin: [MODEL_DEVIR_METNI.md](MODEL_DEVIR_METNI.md). O metin de yetki
devretmez; bu protokolün yerine geçmez.

## Kanonik kaynaklar

Ürün uygulandıktan sonra öncelik sırası:

1. PostgreSQL Work Graph ve revision kayıtları
2. Task Plan, Run, Step, Claim, Receipt ve Verification kayıtları
3. Source binding ve source revision
4. Checkpoint, Work Journal ve Continuity Packet
5. İnsan okunur projection'lar (`AKTIF_GOREV.md`, raporlar)
6. Retrieval ve memory sonuçları
7. Sohbet geçmişi

Alt seviye kaynak üst seviyeyi değiştiremez.

## Oturum kimliği

Her çalışma:

```text
realm_id
project_id
work_item_id
plan_revision_id
run_id
execution_identity
client_identity
model_inventory_id
source_revision
```

alanlarıyla bağlanır. Bir kimlik eksikse mutation çalışmaz.

## Checkpoint

Checkpoint en az şunları taşır:

- tamamlanan ve bekleyen step kimlikleri,
- step result digest'leri,
- aktif logical resource'lar,
- source revision,
- test/eval durumu,
- kalan token/maliyet/zaman bütçesi,
- son güvenli geri dönüş noktası,
- bir sonraki safe action,
- yetki devretmediğini belirten sabit alan.

Checkpoint transcript, API key, lease owner token veya ham model çıktısı taşımaz.

## Continuity Packet

Yeni modelin ilk bounded context'idir:

```text
hedef ve non-goals
Work ve Intent revision
current plan/step
tamamlanan işler ve kanıt referansları
bekleyen işler
kararlar ve review trigger'lari
riskler ve blocker'lar
project capability profile
source revision
prohibited actions
ilk okunacak logical references
next safe action
```

Packet authority vermez. Yeni oturum gerekli authorization ve lease'i yeniden edinir.

## Stale durum

Aşağıdakilerden biri değişirse checkpoint/plan stale olur:

- hedef Work revision,
- source HEAD/tree veya binding revision,
- ilgili dependency fingerprint,
- policy veya capability digest,
- exact effect kapsamı,
- model benchmark/health gereksinimi,
- migration state.

Stale iş sessizce devam etmez; yeni plan revision üretilir.

## Recovery

Claim var ve terminal receipt yoksa:

```text
state = recovery-required
silent retry = false
```

Recovery önce effect'in dış dünyada gerçekleşip gerçekleşmediğini adapter kanıtıyla uzlaştırır.
Kanıt yoksa aynı plan tekrar yürütülmez; yeni, açıkça gözden geçirilmiş recovery planı gerekir.

## Compaction

Context bütçesi dolduğunda:

1. Kanonik kayıtlara yazılmamış sonuçları önce kalıcılaştır.
2. Continuity Packet yenile.
3. Low-priority tarihçeyi packet'tan çıkar; authority kayıtlarını çıkarma.
4. Omitted count ve retrieval referansı bırak.
5. Yeni model packet digest'ini ve kaynak revision'ları doğrulasın.

## Çapraz istemci uyumu

`AGENTS.md`, `CLAUDE.md`, `.opencode/` ve `.ai/repository-context.json` aynı kurala yönlenir.
Hiçbiri ayrı ürün politikası tanımlamaz.
