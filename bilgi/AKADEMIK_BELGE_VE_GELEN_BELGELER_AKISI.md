# Akademik Belge ve Gelen Belgeler Akışı

## Gelen belge dizini

`ZEKAM_HOME/gelen-belgeler/` watcher source dosyasını doğrudan yürütmez. Stable-size/debounce
sonrası:

1. checksum ve file safety,
2. project/intent routing manifesti,
3. idempotent ingestion job,
4. parser/knowledge index,
5. gerekiyorsa Research Work Item,
6. kullanıcı policy'sine göre scheduled analysis

oluşturur.

Belge yanında opsiyonel manifest:

```yaml
project: gpu-fusion
operation: research-and-plan
question: "Bu makaledeki yontemleri mevcut mimariyle karsilastir."
apply: false
priority: normal
```

Manifest yoksa konu/proje uydurulmaz; inbox raporunda choice-required olur.

## Akademik karşılaştırma DAG'ı

- source analyst: makalenin iddia/yöntem/varsayım/sınırlamalarını çıkarır
- project analyst: capability/architecture/source evidence çıkarır
- counter-evidence researcher: uyumsuzluk/risk/güncellik
- synthesizer: applicable/not-applicable/experiment-needed
- citation verifier: exact evidence
- plan reviewer: yalnız verified applicable maddeleri plan candidate'a dönüştürür

İlk bağımsız analizler paralel olabilir.

## Çıktı

```text
makale özeti
claim-evidence tablosu
proje mevcut durum kanıtı
uygun yetenekler
uygunsuz/kapsam dışı
risk ve bağımlılıklar
ölçülebilir deneyler
uygulama adayları
bilinmeyenler/çelişkiler
kaynaklar
```

Research sonucu source mutation yapmaz. Uygulama ayrı Decision/Plan/Authorization/Delivery
akışıdır.

## Güncellik

Paper version/date, supporting sources ve project source revision kaydedilir. Güncel teknik
konu web research gerektiriyorsa exact source snapshot/observed date kullanılır. Eski kaynak
yeni gerçeği sessizce geçemez.

## Sabah raporu

Gelen belgelerde:
- ingest success/failure,
- project match,
- research progress,
- read sources/models/agents,
- verified findings,
- contradictions,
- proposed experiments/plans,
- pending approvals

gösterilir.
