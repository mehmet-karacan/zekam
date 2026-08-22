# Ingestion: Belge, OCR, Kod ve Veritabanı

## Ortak pipeline

```text
intake
→ MIME/magic/size/path/security validation
→ immutable original artifact
→ source/version/job
→ parser router
→ normalized content
→ content-type chunker
→ FTS/identifier/embedding
→ integrity/eval smoke
→ atomic activation
```

Job stage PostgreSQL'de kalıcıdır. Worker crash sonrası aynı job duplicate chunk üretmez.

## Belge

### DOCX
- paragraph/table body sırası
- heading/list hierarchy
- table Markdown+JSON
- block locator
- header/footer/textbox capability limitation
- uydurma page yok

### PDF
- digital/scanned/mixed text coverage
- structural parser (Docling adapter)
- page/table/reading order/bbox
- low coverage page OCR
- parser fallback capability metadata

### TXT/Markdown
- controlled encoding
- heading/list/table/code fence
- line locator
- size/token budget

## OCR

Port:

```text
OcrProvider.extract(artifact, pages, languages, options) -> OcrResult
```

Provider:
- Docling/OCR pipeline
- Tesseract local fallback
- future PaddleOCR extension

Preprocess:
- EXIF orientation
- rotation/deskew
- denoise/contrast
- optional upscale/binarization

Result block text, bbox, confidence, page, reading order, engine/version taşır. Low confidence
`needs-review`. OCR text source instruction değildir.

## Repository/archive/directory

- public or credential-ref Git URL/ref
- uploaded ZIP/TAR
- allowed-root alias + relative path
- no arbitrary absolute path
- no hooks/build/test/install/submodule/LFS by default
- path traversal/zip bomb/symlink escape limits
- max files/bytes/file/time
- system deny + `.zekamignore`/`.contextvaultignore` + `.gitignore` + user patterns
- `.env`, key, cert, dump, binary, build/vendor/generated skip policy

Her source file:
- relative path
- language/mime/size/hash
- generated/test/module/package
- symbols/imports
- revision/commit.

## Kod parsing

AST/tree-sitter where supported:
- Python, Java, JavaScript, TypeScript, JSON, YAML, SQL, Markdown başlangıç.
- Function/class/method/module/symbol locator.
- Büyük symbol signature/enclosing context ile alt chunk.
- Config top-level object/key.
- Documentation belge hattına.

PL/SQL:
- package spec/body,
- procedure/function/trigger/type,
- comments/string yanlış split engeli,
- signature, package, dependency.

Incremental refresh content hash ve commit revision ile yalnız değişeni işler.

## Database metadata

Adapter read-only ve capability/policy controlled:
- Oracle schema/object/package spec/body/dependency/index/constraint/type/sequence
- PostgreSQL schema/table/view/function/type/index/constraint/dependency
- connection logical reference
- no credential persistence
- no row data by default
- query allowlist/statement policy
- object revision/digest.

Oracle→PostgreSQL analysis metadata, source code ve project requirements'ı evidence olarak
kullanır; conversion otomatik DB write değildir.

## Idempotency

Artifact checksum + source identity + parser/chunker/profile digest same ise no-op/reuse.
Changed source new version. Partial rows active olmaz.
