# Z — Nihai Araştırma ve Uygulama Paketini Okuma Sırası

## Bu paketin sonucu

Araştırma yeni bir proje kararı üretmiştir. Uygulama, eski repository’lerden birinin içinde değil, yeni `z-control-plane` repository’sinde başlatılmalıdır.

## Okuma sırası

1. `00_NIHAI_ARASTIRMA_RAPORU.md`  
   Mimari karar, modül haritası, kanonik/projection ayrımı, agent/model/RAG/security tasarımı ve aşamalar.

2. `01_Z_CONTROL_PLANE_UYGULAMA_GOREVI.md`  
   Herhangi bir model veya geliştiricinin uygulamaya başlayabileceği kanonik görev sözleşmesi.

3. `03_Z_PROJECT_MANIFEST.yaml`  
   Aynı kararların makinece okunur manifesti. Policy/config bootstrap’ında doğrudan referans alınır.

4. `04_BASLANGIC_ADR_KARARLARI.md`  
   İlk repository PR’ında ayrı ADR dosyalarına bölünecek kararlar.

5. `02_ILK_DIK_EKSEN_VE_BACKLOG.md`  
   PR-001’den PR-010’a kadar ilk çalışan dikey dilim ve acceptance/recovery testleri.

## Uygulamaya başlama komutu

Bu paket bir modele verildiğinde kullanılacak başlangıç talimatı:

```text
Bu paketin tamamını oku. 01_Z_CONTROL_PLANE_UYGULAMA_GOREVI.md dosyasını
kanonik uygulama görevi, 03_Z_PROJECT_MANIFEST.yaml dosyasını makinece okunur
policy başlangıcı kabul et. Yeni ve boş z-control-plane repository’sinde yalnız
PR-001 kapsamını uygula. Eski repository’lerden source file kopyalama. Gerçek
model/provider çağrısı yapma. Her değişikliği test ve completion receipt ile
kanıtla. PR-001 acceptance kapısı geçmeden PR-002’ye ilerleme.
```

## Subagent kuralındaki güncelleme

Ana araştırma promptundaki “önemsiz olmayan her işte minimum iki bağımsız yürütme kimliği” kuralı, son kullanıcı kararıyla aşağıdaki biçime değiştirilmiştir:

```text
- Agentic iş: minimum 1 subagent.
- Ana coordinator subagent sayılmaz.
- Deterministik exact işlem: 0 subagent olabilir.
- Sabit global maximum yoktur.
- Concurrency; DAG bağımsızlığı, lock, kapasite, kota, bütçe ve riskten run başına hesaplanır.
- Aynı write scope her durumda yalnız tek builder’a verilir.
- Yüksek/kritik riskte ayrıca bağımsız verifier gerekir.
```

Bu kural hem uygulama görevinde hem YAML manifestte kanoniktir.

## Referansların rolü

`referanslar/` altındaki dosyalar uygulama kodu değildir. Bunlardan:

- kaynak kod kopyalanmaz;
- “tamamlandı” iddiası test edilmeden kabul edilmez;
- yalnız sözleşme, negative test, acceptance fixture ve gerekçeli mimari karar alınır.

Context Vault aktif görevi Knowledge Plane’in hedef yönüdür. Z Control Plane’in Faz 7’sinde temiz bounded context olarak uygulanır; Faz 0–6’nın yerine geçmez.

## İlk başarı tanımı

Paketin ilk gerçek başarı ölçütü bir dashboard veya tüm modellerin entegrasyonu değildir. Aşağıdaki komutun kanonik state, minimum bir subagent, evidence ve cross-model continuity ile çalışmasıdır:

```bash
zctl ask "gpu projesindeki 123 numaralı defectin kök nedenini araştır"
```

## Dürüstlük notu

Bu araştırmanın yapıldığı çalışma ortamında çağrılabilir Codex, Claude veya OpenCode CLI bulunmadığı için gerçek haricî subagent koşturulmamıştır. Mimari bulgular ayrı bir karşı-kanıt ve uygulanabilirlik turuyla kontrol edilmiştir. Ürün runtime’ı agentic çalışmada minimum bir gerçek subagent’ı enforcement düzeyinde zorunlu kılacaktır.
