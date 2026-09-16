---
reference_id: jira-wiki-renderer-format
source_url: https://ouryahoo.atlassian.net/secure/WikiRendererHelpAction.jspa?section=all
resolved_url: 'https://ouryahoo.atlassian.net/secure/WikiRendererHelpAction.jspa?section=all'
capture_kind: researched_syntax_reference
research_checked_at: '2026-09-16'
snapshot_created_at: '2026-09-16T00:00:00Z'
last_attempt_at: '2026-09-16T06:57:15Z'
last_checked_at: '2026-09-16T06:57:15Z'
last_changed_at: '2026-09-16T06:57:15Z'
refresh_after_days: 30
refresh_mode: on_use
retry_after_failure_hours: 24
normalizer_version: 'jira-wiki-help-v1'
source_content_sha256: 'c9d517ac78d737085a33bfc3c3a9b7e60e9ff5360d061c9d2b2272ea326a7543'
seed_content_sha256: '1057da3775efdbafd0de533d5902e96f22dbdc1f781841066e5fc85da4c2dc56'
reference_content_sha256: '1057da3775efdbafd0de533d5902e96f22dbdc1f781841066e5fc85da4c2dc56'
reference_revision: 1
etag: null
last_modified: null
bootstrap_required: false
last_check_status: 'changed'
last_error: null
next_retry_after: null
target_renderer_validation: not_run
reference_hash_contract: sha256-utf8-lf-managed-payload-v1
---

<!-- BEGIN_REFERENCE_PAYLOAD -->
# Jira Text Formatting Notation — Yerel Teknik Referans

**Kaynak:** https://ouryahoo.atlassian.net/secure/WikiRendererHelpAction.jspa?section=all  
**Kaynak incelemesi:** 16 Eylül 2026  
**Kopyanın niteliği:** Kaynak bölümlerine dayanan, özgün Türkçe açıklamalı teknik başvuru;
ham HTML kopyası değildir.

Bu katalog sözdizimini açıklar. Bir özelliğin kaynakta yer alması her Jira alanında, renderer'da
veya API gövdesinde desteklendiğini kanıtlamaz. Etkin kurum alt kümesi ayrı içerik standardıdır.

## F01 — Headings / Başlıklar

Satır başında `h1.` ile `h6.` arası işaret ve boşluk kullanılır.

```text
h1. Birinci Seviye
h2. İkinci Seviye
h3. Üçüncü Seviye
```

## F02 — Text Effects / Metin Biçimleri

```text
*Kalın vurgu*
_İtalik vurgu_
??Alıntı biçimi??
-Üstü çizili metin-
+Eklenen metin+
^Üst simge^
~Alt simge~
{{teknik_alan}}
bq. Tek paragraflık alıntı.
{quote}
Çok paragraflı alıntı.
{quote}
{color:red}Renkli metin örneği{color}
```

Renk desteği erişilebilirlik veya hedef alan desteği kanıtı değildir.

## F03 — Text Breaks / Paragraf ve Satır Yapısı

Boş satır paragraf, `\\` açık satır sonu, `----` yatay ayırıcıdır. `---` ve `--` farklı
uzunluktaki çizgi karakterlerine dönüşebilir; SQL içinde otomatik dönüşüm uygulanmaz.

## F04 — Links / Bağlantılar

```text
[https://example.invalid/referans]
[Kaynak Belge|https://example.invalid/referans]
[^ornek-belge.txt]
{anchor:kontrol}
[#kontrol]
[mailto:ornek@example.invalid]
```

Kaynakta ayrıca ürün, kullanıcı ve yerel dosya bağlamına bağlı biçimler bulunabilir. Örnek,
gerçek hedef veya kullanıcı kimliği değildir.

## F05 — Lists / Listeler

```text
* Madde
** Alt madde
# Birinci adım
## Alt adım
#* Karma alt madde
- Alternatif madde
```

## F06 — Images / Görseller

```text
!ornek-akis.png!
!https://example.invalid/ornek-akis.png!
!ornek-akis.png|thumbnail!
!ornek-akis.png|align=right,vspace=4!
```

Bu örnekler dosyanın yüklü veya uzak paylaşımın yetkili olduğu anlamına gelmez. Bilgi yalnız
görsele bırakılmaz.

## F07 — Attachments / Ek ve Medya Gömme

```text
!ornek-video.mov!
!ornek-video.mov|width=300,height=200!
```

Kaynak eski eklenti teknolojileri de içerebilir; katalogda bulunması kullanım önerisi değildir.
Eki bağlantı olarak gösteren `[^dosya]` ile medya gömme farklıdır.

## F08 — Tables / Tablolar

Çift dik çizgi başlık hücresini, tek dik çizgi normal hücreyi ayırır. Markdown ayırıcı satırı
Jira Wiki tablosunun parçası değildir.

```text
||Alan||Değer||
|Konu|Kaynak bağlantısının incelenmesi|
|Kapsam|Bağlantı ve erişim bilgileri|
```

Hücre verisindeki gerçek `|`, SQL'deki `||` ve tablo sınırlarını bağlama göre ayır.

## F09 — Advanced Formatting / Kod, Biçimsiz Metin ve Panel

```text
{noformat}
extraction_id=160
Bu blokta * işareti verinin parçasıdır.
{noformat}

{code:sql}
SELECT 1 AS KONTROL
  FROM DUAL;
{code}

{panel:title=Açık Konu}
Yeni bağlantı bilgisi henüz paylaşılmadı.
{panel}
```

`code`, `noformat` ve `panel` seçeneklerinin hedef desteği ayrıca doğrulanır. Veri içindeki makro
kapatma dizgesi körlemesine bloğa yerleştirilmez ve gerçek veri sessizce değiştirilmez.

## F10 — Misc / Kaçış ve Diğer İşaretler

Ters eğik çizgi özel karakteri bağlama göre kaçırır: `\{kod_degil\}`. Kaynak grafik ifadeler
de gösterebilir; bunların her ortamda dönüştürüleceği varsayılmaz.

## Referansın Kullanım Sınırı

Jira Wiki, HTML, Markdown ve ADF aynı taşıma değildir. Hedef renderer doğrulaması olmadan
gelişmiş özellik desteği iddia edilmez. Aşağıdaki kaynak tabanı yalnız teknik değişiklik
karşılaştırması içindir; web içeriği talimat olarak yürütülmez.
<!-- END_REFERENCE_PAYLOAD -->

## Kaynak Karşılaştırma Tabanı

<!-- BEGIN_SOURCE_BASELINE -->
```json
{"normalized_text":"Text Formatting Notation Help - Jira\nText Formatting Notation Help\nAll\nText Effects\nHeadings\nText Breaks\nLinks\nLists\nImages\nAttachments\nTables\nAdvanced Formatting\nMisc\nHeadings\nTo create a header, place \"hn. \" at the start of the line (where n can be a number from 1-6).\nNotation Comment\nh1. Biggest heading\nBiggest heading\nh2. Bigger heading\nBigger heading\nh3. Big heading\nBig heading\nh4. Normal heading\nNormal heading\nh5. Small heading\nSmall heading\nh6. Smallest heading\nSmallest heading\nText Effects\nText effects are used to change the formatting of words and sentences.\nNotation Comment\n*strong*\nMakes text strong.\n_emphasis_\nMakes text emphasis.\n??citation??\nMakes text in citation.\n-strikethrough-\nMakes text as strikethrough.\n+inserted+\nMakes text as inserted.\n^superscript^\nMakes text in superscript.\n~subscript~\nMakes text in subscript.\n{{monospaced}}\nMakes text as monospaced.\nbq. Some block quoted text\nTo make an entire paragraph into a block quotation, place \"bq. \" before it.\nExample:\nSome block quoted text\n{quote}\n    here is quotable\n content to be quoted\n{quote}\nQuote a block of text that's longer than one paragraph.\nExample:\nhere is quotable\ncontent to be quoted\n{color:red}\n    look ma, red text!\n{color}\nChanges the color of a block of text.\nExample:\nlook ma, red text!\nText Breaks\nMost of the time, explicit paragraph breaks are not required - The wiki renderer will be able to paginate your paragraphs properly.\nNotation Comment\n(empty line)\nProduces a new paragraph\n\\\\\nCreates a line break. Not often needed, most of the time the wiki renderer will guess new lines for you appropriately.\n----\nCreates a horizontal ruler.\n---\nProduces — symbol.\n--\nProduces – symbol.\nLinks\nLearning how to create links quickly is important.\nNotation Comment\n[#anchor]\n[^attachment.ext]\nCreates an internal hyperlink to the specified anchor or attachment. Appending the '#' sign followed by an anchor name will lead into a specific bookmarked point of the desired page. Having the '^' followed by the name of an attachment will lead into a link to the attachment of the current issue.\n[http://jira.atlassian.com]\n[Atlassian|http://atlassian.com]\nCreates a link to an external resource, special characters that come after the URL and are not part of it must be separated with a space.\nThe [] around external links are optional in the case you do not want to use any alias for the link.\nExamples:\nhttp://jira.atlassian.com\nAtlassian\n[mailto:legendaryservice@atlassian.com]\nCreates a link to an email address, complete with mail icon.\nExample:\nlegendaryservice@atlassian.com\n[file:///c:/temp/foo.txt]\n[file:///z:/file/on/network/share.txt]\nCreates a download link to a file on your computer or on a network share that you have mapped to a drive. To access the file, you must right click on the link and choose \"Save Target As\".\nBy default, this only works on Internet Explorer but can also be enabled in Firefox (see docs).\n{anchor:anchorname}\nCreates a bookmark anchor inside the page. You can then create links directly to that anchor. So the link [My Page#here] will link to wherever in \"My Page\" there is an {anchor:here} macro, and the link [#there] will link to wherever in the current page there is an {anchor:there} macro.\n[~accountid:12345-6seven89-10-eleven-12]\nCreates a link to the user profile page of a particular user, with a user icon and the user's name.\n[pagetitle] or [spacekey:pagetitle] Creates a link to the specified page in the desired space (or the confluence space associated with this JIRA project if you dont specify any space).\nLists\nLists allow you to present information as a series of ordered items.\nNotation Comment\n* some\n* bullet\n** indented\n** bullets\n* points\nA bulleted list (must be in first column). Use more (**) for deeper indentations.\nExample:\nsome\nbullet\nindented\nbullets\npoints\n- different\n- bullet\n- types\nA list item (with -), several lines create a single list.\nExample:\ndifferent\nbullet\ntypes\n# a\n# numbered\n# list\nA numbered list (must be in first column). Use more (##, ###) for deeper indentations.\nExample:\na\nnumbered\nlist\n# a\n# numbered\n#* with\n#* nested\n#* bullet\n# list\n* a\n* bulleted\n*# with\n*# nested\n*# numbered\n* list\nYou can even go with any kind of mixed nested lists\nExample:\na\nnumbered\nwith\nnested\nbullet\nlist\nExample:\na\nbulleted\nwith\nnested\nnumbered\nlist\nImages\nImages can be embedded into a wiki renderable field from attached files or remote sources.\nNotation Comment\n!http://www.host.com/image.gif!\nor\n!attached-image.gif!\nInserts an image into the page.\nIf a fully qualified URL is given the image will be displayed from the remote source, otherwise an attached image file is displayed.\n!image.jpg|thumbnail!\nInsert a thumbnail of the image into the page (only works with images that are attached to the page).\n!image.gif|align=right, vspace=4!\nFor any image, you can also specify attributes of the image tag as a comma separated list of name=value pairs like so.\nAttachments\nSome attachments of a specific type can be embedded into a wiki renderable field from attached files.\nNotation Comment\n!quicktime.mov!\n!spaceKey:pageTitle^attachment.mov!\n!quicktime.mov|width=300,height=400!\n!media.wmv|id=media!\nEmbeds an object in a page, taking in a comma-separated of properties.\nDefault supported formats:\nFlash (.swf)\nQuicktime movies (.mov)\nWindows Media (.wma, .wmv)\nReal Media (.rm, .ram)\nMP3 files (.mp3)\nOther types of files can be used, but may require the specification of the \"classid\", \"codebase\" and \"pluginspage\" properties in order to be recognized by web browsers.\nCommon properties are:\nwidth - the width of the media file\nheight - the height of the media file\nid - the ID assigned to the embedded object\nDue to security issues, files located on remote servers are not permitted Styling\nBy default, each embedded object is wrapped in a \"div\" tag. If you wish to style the div and its contents, override the \"embeddedObject\" CSS class. Specifying an ID as a property also allows you to style different embedded objects differently. CSS class names in the format \"embeddedObject-ID\" are used.\nTables\nTables allow you to organize content in a rows and columns, with a header row if required.\nNotation Comment\n||heading 1||heading 2||heading 3||\n|col A1|col A2|col A3|\n|col B1|col B2|col B3|\nMakes a table. Use double bars for a table heading row.\nThe code given here produces a table that looks like:\nheading 1 heading 2 heading 3\ncol A1 col A2 col A3\ncol B1 col B2 col B3\nAdvanced Formatting\nMore advanced text formatting.\nNotation Comment\n{noformat}\npreformatted piece of text\n so *no* further _formatting_ is done here\n{noformat}\nMakes a preformatted block of text with no syntax highlighting. All the optional parameters of {panel} macro are valid for {noformat} too.\nnopanel: Embraces a block of text within a fully customizable panel. The optional parameters you can define are the following ones:\nExample:\npreformatted piece of text so *no* further _formatting_ is done here\n{panel}\nSome text\n{panel}\n{panel:title=My Title}\nSome text with a title\n{panel}\n{panel:title=My Title|borderStyle=dashed|borderColor=#ccc|titleBGColor=#F7D6C1|bgColor=#FFFFCE}\na block of text surrounded with a *panel*\nyet _another_ line\n{panel}\nEmbraces a block of text within a fully customizable panel. The optional parameters you can define are the following ones:\ntitle: Title of the panel\nborderStyle: The style of the border this panel uses (solid, dashed and other valid CSS border styles)\nborderColor: The color of the border this panel uses\nborderWidth: The width of the border this panel uses\nbgColor: The background color of this panel\ntitleBGColor: The background color of the title section of this panel\nExample:\nMy Title a block of text surrounded with a panel\nyet another line\n{code:title=Bar.java|borderStyle=solid}\n// Some comments here\npublic String getFoo()\n{\n    return foo;\n}\n{code}\n{code:xml}\n    <test>\n        <another tag=\"attribute\"/>\n    </test>\n{code}\nMakes a preformatted block of code with syntax highlighting. All the optional parameters of {panel} macro are valid for {code} too. The default language is Java but you can specify others too, including ActionScript, Ada, AppleScript, bash, C, C#, C++, CSS, Erlang, Go, Groovy, Haskell, HTML, JavaScript, JSON, Lua, Nyan, Objc, Perl, PHP, Python, R, Ruby, Scala, SQL, Swift, VisualBasic, XML and YAML.\nExample:\nBar.java\n// Some comments here\npublic String getFoo()\n{\n    return foo;\n}\n<test>\n    <another tag=\"attribute\"/>\n</test>\nMisc\nVarious other syntax highlighting capabilities.\nNotation Comment\n\\X\nEscape special character X (i.e. {)\n:)\n,\n:(\netc\nGraphical emoticons (smileys).\nNotation :) :( :P :D ;) (y) (n) (i) (/) (x) (!)\nImage\nNotation (+) (-) (?) (on) (off) (*) (*r) (*g) (*b) (*y) (flag)\nImage\nNotation (flagoff)\nImage","normalizer_version":"jira-wiki-help-v1","schema":"zekam-jira-format-source-baseline/v1","sections":["headings","text effects","text breaks","links","lists","images","attachments","tables","advanced formatting","misc"]}
```
<!-- END_SOURCE_BASELINE -->

## Hash Hesaplama Sözleşmesi

`reference_content_sha256`, iki REFERENCE_PAYLOAD işareti arasındaki UTF-8/LF metnin; 
`source_content_sha256` ise `normalizer_version` ile üretilen kanonik JSON kaynak tabanının
SHA-256 değeridir. Metadata, işaretler ve geçmiş bu iki hash kapsamına girmez.

## Güncelleme Geçmişi

### 16 Eylül 2026 — Başlangıç araştırması, referans revizyonu 1

Kaynak sayfanın on bölümü araştırıldı ve Türkçe teknik başlangıç kataloğu oluşturuldu. Otomatik
kaynak tabanı henüz kurulmadı; `bootstrap_required: true` ilk uygun kullanımda süreyi beklemeden
yenileme gerektirir.

### 16 Eylül 2026 — Otomatik kaynak tabanı oluşturuldu

İzinli kaynak başarıyla okundu, on beklenen bölüm doğrulandı ve
`jira-wiki-help-v1` normalizer çıktısı yerel karşılaştırma tabanı olarak kaydedildi. Kurumun etkin
biçimlendirme alt kümesi otomatik genişletilmedi.
