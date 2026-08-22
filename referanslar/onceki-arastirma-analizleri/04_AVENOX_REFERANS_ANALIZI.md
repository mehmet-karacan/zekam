# Avenox İçerikleri — Referans Analizi

## Kaynak sınırı

Bu analiz, kullanıcı tarafından sağlanan arşivdeki 38 video transkriptinden çıkarılmıştır. İçerikler ürün demosu, kişisel deneyim ve yorum niteliğindedir; teknik standart veya bağımsız benchmark olarak kabul edilmemelidir. Yeni mimariye yalnız tekrar eden ve mühendislik karşılığı kurulabilen ilkeler alınmalıdır.

## Ana çıkarım

Avenox içeriklerinin ortak mesajı, model markasından çok **harness, context, ortak hafıza, görev düzeni, permission boundary ve ölçümlü workflow** tasarımının kalıcı değer taşıdığıdır. Bu yaklaşım kullanıcının “mimari kalıcı, model ve CLI geçici” hedefiyle doğrudan uyumludur.

## Alınacak ilkeler

### 1. Model değil sistem davranışı ölçülmeli

- Aynı model farklı system prompt, tool seti, context seçimi, memory ve loop altında farklı sonuç verir.
- Model benchmark’ı yalnız tek soru/cevap değil; aynı harness revision, fixture, tool policy, token bütçesi ve verifier ile tekrarlanmalıdır.
- Model raporunda yalnız kalite değil context verimliliği, hata türü, latency, token, retry ve human correction bulunmalıdır.

**Mimari karşılığı:** `ModelExecutionProfile + ProjectBenchmark + RuntimeObservation`.

### 2. Intent, katı rol promptundan daha önemlidir

- Modele “ne yazacağını” aşırı ayrıntılı dikte etmek yerine neden, hedef sonuç, başarı sinyali, kısıt ve yasaklar verilmelidir.
- Proje capability profili, iş kapsamı ve kabul ölçütü dinamik context olmalıdır.
- “Sen testçisin” gibi sabit persona promptları yetkinlik kanıtının yerini almamalıdır.

**Mimari karşılığı:** revision’lı `IntentContract`, plan effect’leri ve capability-aware context compiler.

### 3. Ortak hafıza tek bir metin yığını değildir

- Birden çok agentın ortak geçmişe erişmesi değerli; fakat hepsinin aynı dev context’i sürekli alması verimsizdir.
- Task desk, bilgi kayıtları, permission gate, artifact’lar ve çalışma notları ayrı tutulmalıdır.
- Agent yalnız görevi için gerekli hafıza ve skill parçalarını almalıdır.

**Mimari karşılığı:** Work Graph + scoped memory + retrieval projection + context manifest.

### 4. Task desk ve permission gate

- Çoklu ajan düzeninde kimin hangi işi yaptığı, işin durumu ve hangi etkilere izinli olduğu görünür olmalıdır.
- Aynı işin iki ajana verilmesi yerine bağımsız alt işler veya builder–verifier eşleşmesi kullanılmalıdır.
- İzin, model promptunun içinde değil sistemin dışındaki policy/adapter katmanında uygulanmalıdır.

**Mimari karşılığı:** owner, lease, lock, fencing, exact scope ve result envelope.

### 5. Secret Hub

- Secret değerlerinin modele gösterilmesi yerine ajan logical credential reference kullanmalıdır.
- Hub, yetkili tool çağrısında gerçek değeri dar scope ve kısa süreyle çözer.
- Prompt, log, memory, vector ve artifact metadata secret değeri içermez.

**Mimari karşılığı:** `SecretRef + SecretBroker + OutboundPolicy + redaction`.

### 6. Loop ile tekrar aynı şey değildir

- Loop; state, önceki sonuç, dış ölçüm, durma koşulu ve bir sonraki karar taşır.
- Salt “tekrar dene” token tüketir ve aynı hatayı büyütür.
- Bağımlı işler graph; bağımsız işler kontrollü paralel; aynı writable iş tek owner ile yürütülmelidir.

**Mimari karşılığı:** checkpoint’li measured loop, DAG, stall/cost/iteration limitleri.

### 7. Dinamik skill seçimi ve skill gardener

- Binlerce skill’i her çağrıda context’e koymak yerine yalnız ilgili olanlar seçilmelidir.
- Tekrarlanan iyi workflow’lar skill adayı olabilir.
- Skill oluşturma/iyileştirme otomatik üretimle bitmemeli; evaluation, verifier, onay, version ve rollback gerekir.

**Mimari karşılığı:** indexed skill catalog + capability match + controlled lifecycle.

### 8. Persistent ve zamanlanmış workflow

- Agentın tek sohbet oturumuna bağlı kalmaması; görev bırakma, zamanlanmış araştırma ve sabah raporu üretme hedefi değerlidir.
- Her background job’ın source snapshot’ı, başladığı plan, agent/model, sonuç, hata ve sonraki adımı kaydedilmelidir.

**Mimari karşılığı:** scheduler/inbox + durable jobs + continuity + daily report projection.

## Yeniden yorumlanması gerekenler

- “Tek hafıza” ifadesi tek veritabanı/tablo veya herkese açık ham context anlamına gelmemelidir; scope, authority, freshness ve sensitivity zorunludur.
- “Kendi kendini geliştiren skill” doğrudan aktif kod/policy değiştirmemelidir; yalnız aday ve evidence üretmelidir.
- “Çok ajan” başarının kendisi değildir. Aynı görevi tekrarlayan ajanlar yerine ayrık görev, eleştiri ve doğrulama kullanılmalıdır.
- Model karşılaştırmaları içerik üreticisinin kendi koşullarına aittir; hedef sistem kendi proje fixture ve harness’iyle benchmark yapmalıdır.
- Her zaman açık persistent agent, sınırsız tool/network/secret yetkisi taşımamalıdır.

## Al / Yeniden Tasarla / Alma

| Karar | İçerik |
|---|---|
| **Al** | Harness/context önceliği, intent engineering, ortak fakat scoped hafıza, task desk, permission/secret hub, checkpoint’li loop, dynamic skill retrieval, scheduled workflows |
| **Yeniden tasarla** | Skill gardener’ı controlled lifecycle’a, shared memory’yi katmanlı belleğe, agent ordusunu owner/lock/verifier düzenine dönüştürme |
| **Alma** | Video içi model sıralamalarını benchmark sayma, sınırsız self-modification, bütün hafızayı her modele verme, çok ajanı tek başına kalite ölçütü yapma |

## Hedef sisteme somut katkısı

```text
Intent > Persona
Harness > Model markası
Scoped Context > Dev context dump
Measured Loop > Kör tekrar
Task Ownership > Agent kalabalığı
SecretRef > Secret in prompt
Evaluated Skill > Otomatik self-modification
Durable Job > Tek sohbet oturumu
```

## Temsilî incelenen transkriptler

- “NASA Aynı Yapay Zekaya Mars Rotası Çizdiriyor — Ya Sen?”
- “Yapay Zekaya ‘Ne Yap’ Deme, ‘Neden’ De — Intent Engineering”
- “20 AI Agent’ı Tek Hafızada Çalıştırdım”
- “AI Loop Kurduğunu Sanıyorsun? Aslında Sadece Tekrar Ediyor”
- “Prompt Öldü, Context Kazandı”
- “Context’i Yanlış Kullanıyorsunuz”
- “Hermes Agent’ı Böyle Çalıştırıyorum: Workflow’larımı Paylaşıyorum”
- “Seni Hatırlayan Yapay Zekâ: Kendi İkinci Beynini Kur”
