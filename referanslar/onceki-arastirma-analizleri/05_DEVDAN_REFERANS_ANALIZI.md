# DevDan İçerikleri — Referans Analizi

## Kaynak sınırı

Bu analiz, kullanıcı tarafından sağlanan arşivdeki 182 video transkriptinden çıkarılmıştır. İçerikler ürün ve yöntem gösterimleri içerir; doğrudan teknik otorite veya bağımsız benchmark değildir. Hedef mimariye yalnız farklı videolarda tekrar eden, test edilebilir mühendislik desenleri alınmalıdır.

## Ana çıkarım

DevDan arşivinin hedef sistem için en değerli bölümü; **merkezî task graph, açık owner, builder–validator ayrımı, sandbox, tam observability, özel skill kütüphanesi, deterministik kod + agent karışımı ve kalite eşiğini geçen en ucuz model seçimi** yaklaşımıdır.

## Alınacak ilkeler

### 1. Merkezi task system

- Her görevin tek owner’ı, durumu, bağımlılıkları, beklenen çıktısı ve doğrulayıcısı olmalıdır.
- Ana ajan büyük işi bağımsız ve çatışmasız alt işlere böler; child agentlar yalnız atanmış kapsamı alır.
- Task dependencies, modellerin birbirine serbest konuşmasından daha güvenilir iletişim omurgasıdır.
- Tamamlanan subtask sonucu ana ajan tarafından sentezlenir; child session kanonik state olmaz.

**Mimari karşılığı:** Work Graph + DAG + owner/lease + result fan-in.

### 2. Builder ve validator ayrımı

- Builder kendi deterministic kontrollerini çalıştırsa bile yüksek riskli sonuç bağımsız validator/verifier tarafından yeniden doğrulanmalıdır.
- Verifier farklı execution identity ve tercihen farklı model family kullanmalıdır.
- Aynı yazma kapsamını iki builder’a vermek yerine ikinci ajan eleştirmen veya verifier olmalıdır.

**Mimari karşılığı:** minimum iki tamamlayıcı ajan kuralının güvenli uygulaması.

### 3. Sandboxes

- Çoklu agent ölçeklenmesi için her yürütmenin izole filesystem/process/network alanı olmalıdır.
- Source main tree, production cloud, host credential ve kurumsal network varsayılan erişim alanı değildir.
- Mutation yalnız approved worktree/path setinde; network default-deny ve explicit allowlist ile yapılmalıdır.
- Sandbox yalnız güvenlik değil, paralel çalışma ve temiz recovery aracıdır.

**Mimari karşılığı:** `ExecutionEnvironment` adapter’ı, detached worktree, container/VM seçenekleri.

### 4. Bash ve tool güvenliği

- Shell çok güçlü ortak kaçış noktasıdır; prompt allowlist’i tek başına güvenlik sağlamaz.
- Bir tool yasaklanırken başka tool üzerinden aynı etkinin dolaylı yapılması da engellenmelidir.
- Tool capability, path, network, process, resource ve secret etkileri sistem katmanında enforce edilmelidir.
- Deterministik işlemler için doğrudan typed tool/API tercih edilmelidir.

**Mimari karşılığı:** capability manifest + policy enforcement + sandbox; arbitrary shell en son seçenek.

### 5. Tam observability

- Agent başlangıç/bitiş, model çağrısı, tool call, task transition, lease, retry, token, latency, cost, error ve verifier sonucu event olarak izlenmelidir.
- System prompt, context, tool ve skill şişmesi ölçülmeli; yalnız toplam token değil “kullanışlı token” ve verified output başına maliyet izlenmelidir.
- Ana ajan ve kullanıcı aynı state’i dashboard/CLI/Markdown projection’dan görebilmelidir.

**Mimari karşılığı:** OpenTelemetry uyumlu event modeli + domain audit/receipt kayıtları.

### 6. Özel skill kütüphanesi ve meta-skill

- Skill’leri projelere kopyalamak yerine merkezi catalog’da Git/local package referansı ve pinned version/digest ile yönetme.
- Tek registry manifesti; sync, update, compatibility ve provenance.
- Proje çağrısında yalnız capability ve göreve uyan skill’lerin seçilmesi.
- Özel içerik, secret veya yetkisiz tool kapsamının skill metadata’sına sızmaması.

**Mimari karşılığı:** package-manager benzeri Skill Registry + lifecycle.

### 7. Deterministik kod ile agent işini ayırma

- Lint, format, schema validation, AST inspection, test, hash, diff, migration check ve policy gate deterministik kodla yapılmalıdır.
- Agent; belirsiz analiz, tasarım, sentez ve çözüm üretiminde kullanılmalıdır.
- Agent çıktısı deterministic validator’dan geçmeden source-of-truth olmamalıdır.

**Mimari karşılığı:** workflow node’ları `deterministic` veya `agentic` olarak sınıflandırılır.

### 8. Model stack ve tokenomics

- Tek “en iyi model” yerine state-of-the-art, workhorse ve lightweight katmanları.
- Önce eligibility/quality floor; sonra hız, quota, token ve maliyet optimizasyonu.
- Basit sınıflandırma/özet için büyük model; kritik mimari veya zor refactor için küçük model kullanılmamalı.
- Model değişimi state transferiyle yapılmalı; önceki transcript’e bağımlı kalmamalıdır.

**Mimari karşılığı:** Model Decision Service ve configurable fallback chain.

### 9. Fusion ve bounded debate

- Karmaşık kararda bağımsız çözüm önerileri almak ve ayrı synthesizer/verifier ile birleştirmek yararlı olabilir.
- Debate; exact soru, evidence seti, tur/süre/token limiti ve karar kuralı taşır.
- Aynı işin production mutation’ını birden fazla modele yaptırmak fusion değildir.

**Mimari karşılığı:** read-only `DeliberationRun`, proposer/opponent/synthesizer/verifier.

### 10. Yazılım fabrikası ve workflow

- Tek dev prompt yerine tekrar kullanılabilir plan, görev, araç, doğrulama ve teslim zinciri.
- Bir işin tamamlanması yalnız kod üretimi değil; test, evidence, artifact, review ve receipt içerir.
- Workflow şablonları proje capability profiline göre uyarlanmalıdır.

**Mimari karşılığı:** versioned workflow templates + project-specific TaskPlan.

## Yeniden yorumlanması gerekenler

- “One agent is not enough” her işte çok sayıda model kullanmak anlamına gelmemeli; en az iki execution identity tamamlayıcı görevlerde kullanılmalıdır.
- Tmux veya belirli bir CLI mimari temel değildir; sadece adapter/runtime seçeneğidir.
- Full observability ham source, secret veya özel prompt içeriğini loglamak anlamına gelmez; event metadata ve digest esas olmalıdır.
- Model stacking/fusion yalnız kalite farkı ölçülüyorsa uygulanmalıdır; maliyet ve quota bütçesi olmadan varsayılan olmamalıdır.
- Skill kütüphanesi sınırsız context injection yapmamalı; retrieval ve explicit manifest gerekir.

## Al / Yeniden Tasarla / Alma

| Karar | İçerik |
|---|---|
| **Al** | Merkezi task graph, tek owner, builder–validator, sandbox, typed tool güvenliği, tam event observability, private skill catalog, deterministic+agentic workflow, quality-floor model routing, bounded fusion |
| **Yeniden tasarla** | Tmux/CLI örneklerini adapter’a, shell kullanımını policy+sandbox’a, tokenomics’i verified-value metriğine, multi-agent UI’ı ortak runtime projection’a dönüştürme |
| **Alma** | Aynı writable işi çok modele verme, unrestricted Bash, production credential erişimi, model tartışmasını kanıt yerine koyma, ürün/CLI lock-in |

## Hedef sisteme somut katkısı

```text
One task → one owner
Non-trivial work → builder/researcher + independent verifier/critic
Parallelism → only disjoint resources
Tool use → typed capability + sandbox
Agent output → deterministic validation + receipt
Model route → quality floor, then quota/cost/latency
Skills → referenced, pinned, tested, dynamically selected
Observability → every state/effect event, no raw secrets
```

## Temsilî incelenen transkriptler

- “Claude Code Task System: Anti-Hype Agentic Coding”
- “Claude Code Multi-Agent Orchestration with Tmux and Agent Sandboxes”
- “Pi Coding Agent Observability”
- “I Can See Everything: Hooks for Multi Agent Observability”
- “The Library Meta-Skill: Private Skills, Agents and Prompts”
- “Engineers, Delete the Bash Tool: Agentic Security”
- “Your Software Factory Needs Agent Sandboxes to Scale”
- “One Prompt Every Agentic Codebase Should Have”
- “Model Stacking”
- “Fusion Chain”
- “My Super Simple Software Factory”
