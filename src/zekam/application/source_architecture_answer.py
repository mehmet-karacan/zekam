"""GLM pure code-to-architecture summary helper (zekam-source-architecture-summary/v1).

Retrieval'den ayristirilmis, kod indeksine bagli mimari ozet kurucu. Yalniz
verilen chunk view, identity ve candidate metadata uzerinde calisir; dosya
sistemi okumaz, calisma zamani modul import etmez, ek bagimlilik istemez.
Sadece indekslenmis kod/manifest bildirimlerinden (class, annotation, import,
route, dependency) kanitli claim uretir; class/path ismi, yorum, docstring ve
string literal icerigi rol kaniti sayilmaz; yalniz satir basi anchored
import/from belirtecleri sozdizimsel korunur. Chunk birlikteligi tek basina
bagimlilik kenari uretmez. Kullaniciya donecek metin modul bazli sorumluluk
aciklamasi ve kanit yol+satir icerir; serbest metin kabul edilmez.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET

SCHEMA = "zekam-source-architecture-summary/v1"
_RAG_RESULT_SCHEMA = "zekam-embedded-rag-result/v1"
_CLAIM_BUDGET = 12
_PER_MODULE_CLAIM_LINES = 3

_CODE_SUFFIXES = {
    ".py", ".java", ".kt", ".kts", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".cs", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".scala", ".swift",
    ".vue", ".svelte",
}
_MANIFEST_NAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "package.json", "requirements.txt", "pyproject.toml",
    "setup.py", "setup.cfg", "go.mod", "cargo.toml", "makefile", "dockerfile",
    "jenkinsfile",
}
_DOC_SUFFIXES = {".md", ".rst", ".adoc", ".txt", ".pdf", ".docx", ".html"}
_EXCLUDED_DIRS = {
    "docs", "doc", "guidance", "guides", "tasks", "task", "skills", "skill",
    "checkpoints", "checkpoint", "logs", "log", "plans", "plan", "notes",
    "tests", "test", "__tests__", "spec", "specs", "e2e", "fixtures",
    "examples", "example", "samples", "node_modules", "vendor", "target",
    "build", "dist", "out", "generated", "gen", "__pycache__", "venv",
    ".venv", ".tox", ".eggs", "coverage", ".idea", ".vscode",
    ".git", ".opencode", ".agents", ".claude", ".codex", ".zekam", ".zekam-reference",
}
_TEST_NAME_PREFIXES = ("test_",)
_TEST_NAME_SUFFIXES = (
    "_test.py", "_test.go", "_test.js", "_test.ts", ".test.js", ".test.ts",
    ".spec.js", ".spec.ts", "tests.java", "test.java",
)

_TR_MAP = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
})
_STRONG_QUERY = (
    "architecture", "mimari", "modul sorumluluk", "modullerin", "moduller arasi",
    "modul yapisi", "katman yapisi", "bilesen yapisi", "kod yapis", "code structure",
    "codebase structure", "proje yapis", "sistem yapisi", "hangi modul",
    "hangi bilesen", "hangi katman", "which module", "which component",
    "which layer", "module responsib", "modules responsib", "module boundar",
)
_WEAK_QUERY = ("modul", "module", "bilesen", "component", "katman", "layer", "paket", "package")
_STRUCT_QUERY = (
    "sorumluluk", "responsib", "yapi", "structure", "boundar", "roller", "roles",
    "organized", "organiz", "overview", "dagilim", "hangisi", "what does", "gorev",
)
_ROUTE_SIGNALS = (
    "@app.route", "@app.get", "@app.post", "@app.put", "@app.delete", "@app.patch",
    "@router.get", "@router.post", "@router.put", "@router.delete", "@router.patch",
    "@requestmapping", "@getmapping", "@postmapping", "@putmapping", "@deletemapping",
    "@patchmapping", "@path(", "@get", "@post", "@restcontroller", "@httpget",
    "@httppost",
)
_SERVICE_SIGNALS = ("@service", "@injectable")
_DATA_SIGNALS = (
    "@repository", "@entity", "from sqlalchemy", "import sqlalchemy",
    "javax.persistence", "jakarta.persistence", "session.query", "__tablename__",
)
_ENTRY_SIGNALS = (
    "if __name__", "public static void main", "springapplication.run(",
    "uvicorn.run(", "func main()", "def main(", "require.main === module",
)
_FRONTEND_RENDER_SIGNALS = (
    "reactdom.render(", "createroot(", "vue.createapp(",
)
# useEffect tek basina HTTP/API istegi kaniti degil; yalniz gercek cagri
# belirtecleri (fetch(, axios., xmlhttprequest) sayilir.
_FRONTEND_API_SIGNALS = ("fetch(", "axios.", "xmlhttprequest")
_COMPONENT_ONLY_SUFFIXES = (".vue", ".svelte")
_FRONTEND_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")
_TYPE_RES = (
    re.compile(r"^\s*(?:public|protected|private|final|abstract)?\s*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+struct\b", re.MULTILINE),
)
_POM_MODULE_RE = re.compile(r"<module>([^<]+)</module>")
_GRADLE_PROJECT_RE = re.compile(r"include\s+[\"']([^\"']+)[\"']")
_GRADLE_DEP_RE = re.compile(r"(?:implementation|api|compile|runtimeOnly)\s+[\"']([^\"']+)[\"']")
_JSX_CUE_RE = re.compile(r"</[A-Za-z]|<[A-Za-z][^<>\n]*/>")
_JS_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*(?:import|export)[ \t][^\n;]*?\bfrom[ \t]*[\"']([^\"'\n]+)[\"']", re.MULTILINE,
)
_JS_BARE_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+[\"']([^\"'\n]+)[\"']", re.MULTILINE)
_FRONTEND_LIB_SPECIFIERS = frozenset({"react", "react-dom", "vue", "svelte"})

_ROLE_TEXTS = {
    "http_api": "HTTP isteklerini karsilar (route/controller bildirimi)",
    "service_logic": "uygulama servis bileseni (service/injectable bildirimi)",
    "data_access": "veri erisimi ve kalicilik (repository/persistence bildirimi)",
    "frontend_rendering": "kullanici arayuzu bileseni (component/template import)",
    "frontend_api_requests": "API istekleri (fetch/axios cagrisi)",
    "application_entrypoint": "uygulama giris noktasi (main/boot)",
    "library_dependency": "dis bagimlilik bildirimi",
    "structural": "yapisal tip bildirimi (rol cikarimi kanita dayali degil)",
}
_ROLE_PRIORITY = (
    "http_api", "service_logic", "data_access", "frontend_rendering",
    "frontend_api_requests", "application_entrypoint", "library_dependency",
    "structural",
)
_ROLE_NAMES = frozenset(_ROLE_TEXTS)


def _normalize(text: str) -> str:
    return (text or "").lower().translate(_TR_MAP)


def is_implementation_source(path: str) -> bool:
    """Desteklenen gercek kod/manifest kaynagi mi; guvenli relativ path sart."""
    if not isinstance(path, str) or not path or path.strip() != path:
        return False
    parts = path.replace("\\", "/").split("/")
    if parts[0] == "" or parts[0].endswith(":") or ".." in parts:
        return False
    parts = [part for part in parts if part != "."]
    if not parts or any(part.lower() in _EXCLUDED_DIRS for part in parts[:-1]):
        return False
    name = parts[-1]
    lowered = name.lower()
    if lowered in _MANIFEST_NAMES:
        return True
    if any(lowered.startswith(prefix) for prefix in _TEST_NAME_PREFIXES):
        return False
    if any(lowered.endswith(sfx) for sfx in _TEST_NAME_SUFFIXES):
        return False
    # Maven *IT.java entegrasyon testi konvansiyonu buyuk/kucuk harf duyarlidir;
    # Audit.java / Unit.java gibi gercek kod dosyalari ayiklanmaz.
    if name.endswith("IT.java"):
        return False
    stem, dot, suffix = lowered.rpartition(".")
    if not dot:
        return False
    if f".{suffix}" in _DOC_SUFFIXES:
        return False
    return f".{suffix}" in _CODE_SUFFIXES


def source_module(path: str) -> str:
    """Ilk path bileseni; kok dosyalari icin '.'."""
    if not isinstance(path, str) or not path.strip():
        return "."
    parts = [part for part in path.strip().replace("\\", "/").split("/") if part and part != "."]
    return "." if len(parts) <= 1 else parts[0]


def is_architecture_query(query: str) -> bool:
    """Turkce (vurgulu/ASCII) ve Ingilizce mimari/modul-sorumluluk niyeti."""
    if not isinstance(query, str) or not query.strip():
        return False
    normalized = _normalize(query)
    if any(pattern in normalized for pattern in _STRONG_QUERY):
        return True
    return any(token in normalized for token in _WEAK_QUERY) and any(
        token in normalized for token in _STRUCT_QUERY
    )


def _field(obj: object, name: str) -> object:
    if obj is None:
        return None
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _locator_as_dict(locator: object) -> dict | None:
    """Gercek Locator dataclass'ini (as_dict) veya dict'i kayipsiz cevirir."""
    if locator is None:
        return None
    if isinstance(locator, dict):
        return dict(locator)
    as_dict = getattr(locator, "as_dict", None)
    if callable(as_dict):
        converted = as_dict()
        return dict(converted) if isinstance(converted, dict) else None
    return None


def _valid_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    hexadecimal = value[7:]
    return len(hexadecimal) == 64 and all(char in "0123456789abcdef" for char in hexadecimal)


def _text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_noncode(
    text: str,
    *,
    drop_strings: bool,
    hash_comments: bool,
    slash_comments: bool,
) -> str:
    """String-durum bilincli temizlik.

    Yorumlari atar; ``drop_strings`` ise string literal icerigini maskeler.
    JavaScript/TypeScript backtick template literal icerigi (escape'li
    backtick dahil) de kod kaniti degildir; ``drop_strings`` ile maskelenir,
    manifest modunda (drop_strings=False) kayipsiz kalir.
    URL iceren string'ler ('https://...') yorum sanilmaz, bildirim/import/call
    sozdizimi korunur. Manifest icerigi (drop_strings=False) kayipsiz kalir.
    ``hash_comments``/``slash_comments`` kaynak turune gore aciktir; C-family
    onislemci satirlari (#include) ve Python floor division (//) korunur.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_block_comment = False
    while i < n:
        char = text[i]
        if in_block_comment:
            if char == "*" and i + 1 < n and text[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            if char == "\n":
                out.append("\n")
            i += 1
            continue
        if char in "\"'":
            quote = char
            triple = text[i:i + 3] == quote * 3
            start = i
            i += 3 if triple else 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if triple:
                    if text[i:i + 3] == quote * 3:
                        i += 3
                        break
                elif text[i] == quote:
                    i += 1
                    break
                i += 1
            if drop_strings:
                out.append(" ")
            else:
                out.append(text[start:i])
            continue
        if char == "`":
            # Template literal icerigi (escape'li backtick dahil) kanit degil.
            start = i
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "`":
                    i += 1
                    break
                i += 1
            if drop_strings:
                out.append(" ")
            else:
                out.append(text[start:i])
            continue
        if char == "<" and text[i:i + 4] == "<!--":
            end = text.find("-->", i + 4)
            i = n if end < 0 else end + 3
            continue
        if char == "/" and slash_comments and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "/" and slash_comments and i + 1 < n and text[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if char == "#" and hash_comments:
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _comment_style(source_path: str) -> tuple[bool, bool]:
    """(hash_comments, slash_comments) kaynak turune gore."""
    lowered = source_path.lower()
    if lowered.endswith((".py", ".rb", ".gemspec", ".rake", ".sh", ".toml", ".cfg", ".ini")):
        return True, False
    if lowered.endswith((".java", ".js", ".ts", ".jsx", ".tsx", ".kt", ".kts", ".cs", ".go", ".c", ".h", ".cpp", ".hpp", ".rs", ".php", ".swift", ".scala", ".vue", ".svelte")):
        return False, True
    return False, False


def _strip_comments(text: str, source_path: str) -> str:
    """Yorumlari string-bilincli atar; manifest/dependency icerigi korunur."""
    hash_comments, slash_comments = _comment_style(source_path)
    return _strip_noncode(text, drop_strings=False, hash_comments=hash_comments, slash_comments=slash_comments)


def _code_only(text: str, source_path: str) -> str:
    """Sinyal taramasi icin yorum + string literal icerigi maskelenmis kod."""
    hash_comments, slash_comments = _comment_style(source_path)
    return _strip_noncode(text, drop_strings=True, hash_comments=hash_comments, slash_comments=slash_comments)


def _manifest_modules(text: str, source_path: str) -> list[str]:
    lowered = source_path.lower()
    if lowered.endswith("pom.xml"):
        return _POM_MODULE_RE.findall(text)
    if lowered.endswith((".gradle", ".gradle.kts")):
        return _GRADLE_PROJECT_RE.findall(text)
    return []


def _local_tag(element) -> str:
    """Namespace-aware etiket adi; {uri}tag -> tag."""
    tag = element.tag
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _direct_pom_dependency_artifact_ids(root) -> list[str]:
    """SADECE project/dependencies/dependency/artifactId (dogrudan cocuk).

    Parent/plugin/dependencyManagement artifactId'leri ve plugin icice
    dependencies bloklari runtime/module bagimliligi sayilmaz.
    """
    names: list[str] = []
    for dependencies in list(root):
        if _local_tag(dependencies) != "dependencies":
            continue
        for dependency in list(dependencies):
            if _local_tag(dependency) != "dependency":
                continue
            for child in list(dependency):
                if _local_tag(child) == "artifactId" and isinstance(child.text, str):
                    name = child.text.strip()
                    if name and name not in names:
                        names.append(name)
    return names


def _manifest_dependencies(text: str, source_path: str) -> list[str]:
    """Sadece gercek dependency bildirimleri; project/parent/plugin/managed hariç."""
    lowered = source_path.lower()
    if lowered.endswith("pom.xml"):
        # Truncated/parse edilemeyen manifest tahmin uretmez. Stdlib XML parse:
        # yalniz dogrudan project/dependencies/dependency/artifactId (namespace
        # aware) gercek runtime/module bagimliligidir; plugin icice
        # dependencies bloklari ve dependencyManagement (managed) bildirimleri
        # bagimlilik sayilmaz.
        if "</project>" not in text:
            return []
        try:
            root = ET.fromstring(text)
        except (ET.ParseError, ValueError, TypeError):
            return []
        if _local_tag(root) != "project":
            return []
        return _direct_pom_dependency_artifact_ids(root)
    if lowered.endswith((".gradle", ".gradle.kts")):
        return _GRADLE_DEP_RE.findall(text)
    if lowered.endswith("package.json"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return []
        dependencies = data.get("dependencies") if isinstance(data, dict) else None
        return list(dependencies) if isinstance(dependencies, dict) else []
    if lowered.endswith("requirements.txt"):
        return [
            line.split("==")[0].split(">=")[0].split("<=")[0].strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))
        ]
    return []


def _first_type_symbol(text: str) -> str:
    for regex in _TYPE_RES:
        match = regex.search(text)
        if match:
            return match.group(1)
    return ""


def _render_cue(lowered: str, text: str, source_path: str) -> bool:
    """Gercek render ipucu: dogrudan render API cagrisi veya JSX isaretleme.

    Import belirteci tek basina render kaniti degildir; gercek bilesen
    ipucu (JSX/createRoot/render) olmadan rendering iddia edilmez. JSX
    ipucu yalniz frontend kaynak turlerinde aranir.
    """
    if any(_has_signal(lowered, signal) for signal in _FRONTEND_RENDER_SIGNALS):
        return True
    return source_path.lower().endswith(_FRONTEND_SUFFIXES) and bool(_JSX_CUE_RE.search(text))


def _js_frontend_imports(code_text: str) -> list[str]:
    """Anchored import/from belirtecleri (yorumlardan arindirilmis metinde).

    Yalniz satir basi ``import``/``export ... from`` sozdizimi taninir;
    keyfi string literal icerigi specifier sayilmaz.
    """
    specifiers: list[str] = []
    for regex in (_JS_FROM_IMPORT_RE, _JS_BARE_IMPORT_RE):
        for match in regex.finditer(code_text):
            name = match.group(1).strip().lower()
            if name and name not in specifiers:
                specifiers.append(name)
    return specifiers


def _has_signal(text: str, signal: str) -> bool:
    """Sinir-duyurlu signal uyumu.

    Son karakteri harf/rakam/alt cizgi olan signal'lerde substring uyumu
    yanlis rol eslestirme urettigi icin (@service -> @ServiceProvider,
    @router.get -> @router.get_user) token boundary uygulanir; digerlerinde
    duz membership yeterlidir.
    """
    if signal[-1].isalnum() or signal[-1] == "_":
        return re.search(re.escape(signal) + r"(?!\w)", text) is not None
    return signal in text


def _classify_role(text: str, source_path: str) -> str:
    """Gercek bildirim sinyallerinden rol; isim benzerligi kanit sayilmaz."""
    lowered = _normalize(text)
    if source_path.lower().endswith(_COMPONENT_ONLY_SUFFIXES) and (
        "<template" in lowered or "<script" in lowered
    ):
        return "frontend_rendering"
    if any(_has_signal(lowered, signal) for signal in _ROUTE_SIGNALS):
        return "http_api"
    if any(_has_signal(lowered, signal) for signal in _DATA_SIGNALS):
        return "data_access"
    if _render_cue(lowered, text, source_path):
        return "frontend_rendering"
    if source_path.lower().endswith(_FRONTEND_SUFFIXES) and any(
        _has_signal(lowered, signal) for signal in _FRONTEND_API_SIGNALS
    ):
        return "frontend_api_requests"
    if any(_has_signal(lowered, signal) for signal in _ENTRY_SIGNALS):
        return "application_entrypoint"
    if any(_has_signal(lowered, signal) for signal in _SERVICE_SIGNALS):
        return "service_logic"
    return "structural"


def _declaration_signals(text: str) -> list[str]:
    lowered = _normalize(text)
    signals: list[str] = []
    for group in (_ROUTE_SIGNALS, _SERVICE_SIGNALS, _DATA_SIGNALS, _ENTRY_SIGNALS, _FRONTEND_RENDER_SIGNALS, _FRONTEND_API_SIGNALS):
        for signal in group:
            if _has_signal(lowered, signal) and signal not in signals:
                signals.append(signal)
    return signals[:4]


def _make_claim(
    text: str,
    role: str,
    chunk_id: str,
    locator: dict,
    source_ref: str,
    kind: str,
    identity: dict,
    module_name: str,
) -> dict:
    """Kanit claim'i; gercek locator dict, digest'ler ve modul tasir."""
    line_start = locator.get("line_start")
    line_end = locator.get("line_end")
    where = f" (L{line_start}-L{line_end})" if isinstance(line_start, int) and isinstance(line_end, int) else ""
    return {
        "text": f"{source_ref}{where}: {text}",
        "role": role,
        "evidence_chunk_ids": [chunk_id],
        "source_ref": source_ref,
        "locator": dict(locator),
        "source_digest": identity.get("source_digest"),
        "content_digest": identity.get("content_digest"),
        "module": module_name,
        "signal_kind": kind,
    }


def _render_text(claims: list[dict], module_names: list[str], coverage_notes: list[str]) -> str:
    """Modul bazli sorumluluk aciklamasi; yalnizca dogrulanmis claim'lerden."""
    by_module: dict[str, list[dict]] = {}
    for claim in claims:
        by_module.setdefault(claim["module"], []).append(claim)
    lines = ["Kod mimarisi ozeti (yalnizca kod indeksi bildirimlerinden kanitli):"]
    for name in module_names:
        module_claims = by_module.get(name, [])
        if not module_claims:
            continue
        roles = [claim["role"] for claim in module_claims]
        primary = next(role for role in _ROLE_PRIORITY if role in roles)
        parts = [
            f"{name}: {len(module_claims)} kanitli bildirim",
            _ROLE_TEXTS[primary],
        ]
        extra_roles = sorted({role for role in roles if role != primary})
        if extra_roles:
            parts.append("diger: " + ", ".join(_ROLE_TEXTS[role] for role in extra_roles[:2]))
        lines.append("- " + "; ".join(parts))
        for claim in module_claims[:_PER_MODULE_CLAIM_LINES]:
            lines.append("  * " + claim["text"])
        if len(module_claims) > _PER_MODULE_CLAIM_LINES:
            lines.append(f"  * ... {len(module_claims) - _PER_MODULE_CLAIM_LINES} kanit daha")
    for note in coverage_notes[:4]:
        lines.append(f"- sinir: {note}")
    lines.append(
        "Sinirlar: rol etiketleri yalnizca indekslenmis bildirim sinyallerine dayanir; "
        "isim benzerligi, yorum ve string icerigi kanit sayilmaz, eksik moduller "
        "sinir notlarindadir."
    )
    return "\n".join(lines)


def build_architecture_summary(
    *,
    query: str,
    project_id: str,
    generation_digest: str,
    source_revision: str,
    views: dict,
    identities: dict,
    candidate_metadata: dict,
) -> dict:
    """Kod indeksinden kanitli, sinirli mimari ozet; uydurma iddia uretmez.

    views: chunk_id -> ChunkView (Locator dataclass veya dict locator).
    identities: chunk_id -> identity dict (gercek source_identities sozlesmesi).
    """
    del query
    views = views if isinstance(views, dict) else {}
    identities = identities if isinstance(identities, dict) else {}
    metadata = candidate_metadata if isinstance(candidate_metadata, dict) else {}
    chunk_ids = tuple(metadata.get("chunk_ids") or ())
    truncated = bool(metadata.get("truncated", False))
    observed = metadata.get("observed_module_count")
    sampled = metadata.get("sampled_module_count")
    observed = observed if isinstance(observed, int) and observed >= 0 else 0
    sampled = sampled if isinstance(sampled, int) and sampled >= 0 else 0

    valid_binding = (
        isinstance(project_id, str) and bool(project_id.strip())
        and _valid_digest(generation_digest)
        and isinstance(source_revision, str) and bool(source_revision.strip())
    )
    module_claims: dict[str, list[dict]] = {}
    module_seen: list[str] = []
    declared_names: list[str] = []
    coverage_notes: list[str] = []

    for chunk_id in chunk_ids:
        view = views.get(chunk_id)
        if view is None or _field(view, "chunk_id") != chunk_id:
            continue
        # Gercek SQLiteKnowledgeIndex.source_identities chunk_id anahtarlidir;
        # document_id anahtari uydurma eslesme urettigi icin kabul edilmez.
        identity = identities.get(chunk_id)
        if not isinstance(identity, dict):
            continue
        source_ref = identity.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.strip():
            continue
        if not is_implementation_source(source_ref):
            continue
        locator = _locator_as_dict(_field(view, "locator"))
        if locator is None or locator.get("relative_path") != source_ref:
            continue
        if identity.get("source_revision") != source_revision:
            continue
        if not _valid_digest(identity.get("source_digest")):
            continue
        view_text = _field(view, "text")
        if not isinstance(view_text, str) or not view_text.strip():
            continue
        view_digest = _field(view, "content_digest")
        if not _valid_digest(view_digest) or _text_digest(view_text) != view_digest:
            continue
        # Bos/malformed identity content digest kabul edilmez; gercek API
        # sozlesmesinde identity content digest chunk body ile aynidir.
        if not _valid_digest(identity.get("content_digest")) or identity.get("content_digest") != view_digest:
            continue
        if not valid_binding:
            continue
        code_text = _strip_comments(view_text, source_ref)
        signal_text = _code_only(view_text, source_ref)
        module_name = source_module(source_ref)
        chunk_claims: list[dict] = []
        declared = _manifest_modules(code_text, source_ref)
        dependencies = _manifest_dependencies(code_text, source_ref)
        # Import specifier'i yalniz anchored import/from sozdiziminden okunur;
        # keyfi string literal icerigi specifier sayilmaz.
        frontend_imports = (
            _js_frontend_imports(code_text)
            if source_ref.lower().endswith(_FRONTEND_SUFFIXES) else []
        )
        if declared:
            chunk_claims.append(_make_claim(
                "manifest modul bildirimi: " + ", ".join(declared[:5]), "structural",
                chunk_id, locator, source_ref, "module_declaration", identity, module_name,
            ))
        if dependencies:
            chunk_claims.append(_make_claim(
                "bagimlilik bildirimi: " + ", ".join(dependencies[:5]), "library_dependency",
                chunk_id, locator, source_ref, "dependency_declaration", identity, module_name,
            ))
        symbol = _first_type_symbol(signal_text)
        signals = _declaration_signals(signal_text)
        role = _classify_role(signal_text, source_ref)
        # Import belirteci ve JSX ipucu yalniz gercek rendering kanitiyla
        # (rol frontend_rendering) sinyal listesine girer; import tek basina
        # rendering iddiasi uretmez.
        if role == "frontend_rendering":
            if source_ref.lower().endswith(_FRONTEND_SUFFIXES) and _JSX_CUE_RE.search(signal_text):
                signals.append("jsx")
            for spec in frontend_imports:
                if spec in _FRONTEND_LIB_SPECIFIERS:
                    label = spec + " import"
                    if label not in signals:
                        signals.append(label)
            signals = signals[:4]
        if symbol:
            chunk_claims.append(_make_claim(
                f"{symbol} tip bildirimi", role, chunk_id, locator, source_ref,
                "type_declaration", identity, module_name,
            ))
        if signals and (not symbol or role != "structural"):
            chunk_claims.append(_make_claim(
                "bildirim sinyalleri: " + ", ".join(signals), role,
                chunk_id, locator, source_ref, "annotation_declaration", identity, module_name,
            ))
        # .svelte/.vue uzanti tek basina rol kaniti degil; component/template
        # markup kanitiyla kullanici arayuzu bileseni claim'i uretilir.
        if (
            not symbol
            and source_ref.lower().endswith(_COMPONENT_ONLY_SUFFIXES)
            and role == "frontend_rendering"
        ):
            chunk_claims.append(_make_claim(
                "bilesen/template bildirimi", role,
                chunk_id, locator, source_ref, "component_declaration", identity, module_name,
            ))
        if source_ref.lower().endswith("pom.xml") and "<project" in code_text and "</project>" not in code_text:
            coverage_notes.append("pom.xml kismi/truncated: tam parse edilemedi, bagimlilik kapsami kismi")
        if not chunk_claims:
            # Bildirimsiz kod parcasi tek basina rol kaniti degil; kullanilmaz.
            continue
        for claim in chunk_claims:
            if module_name not in module_claims:
                module_claims[module_name] = []
                module_seen.append(module_name)
            module_claims[module_name].append(claim)
        for name in declared:
            if name not in declared_names:
                declared_names.append(name)

    # Adil secim: her modul kovasinda sorumluluk rolu kanitlari yapisal/
    # kutuphane envanterinin onune gecer; ayni kaynak+rol+signal tekrarina
    # tek claim kalir. Moduller round-robin temsil edilir; ilk gelen modul
    # butceyi tek basina doldurmaz. Beklenen-ama-eksik modul karari TUM
    # adaylar tarandiktan SONRA verilir; sonradan gorulen modul kalici
    # 'missing' olmaz.
    role_order = {role: index for index, role in enumerate(_ROLE_PRIORITY)}
    for name in module_seen:
        bucket = module_claims[name]
        bucket.sort(key=lambda claim: role_order.get(claim["role"], len(role_order)))
        deduped: list[dict] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for claim in bucket:
            key = (claim["source_ref"], claim["role"], claim["signal_kind"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(claim)
        module_claims[name] = deduped
    total_candidate = sum(len(module_claims[name]) for name in module_seen)
    retained: list[dict] = []
    cursors = {name: 0 for name in module_seen}
    while len(retained) < _CLAIM_BUDGET:
        added = False
        for name in module_seen:
            if len(retained) >= _CLAIM_BUDGET:
                break
            bucket = module_claims[name]
            if cursors[name] < len(bucket):
                retained.append(bucket[cursors[name]])
                cursors[name] += 1
                added = True
        if not added:
            break
    if truncated or observed > sampled:
        coverage_notes.append(
            f"ornekleme sinirli: {observed} modul gozlemlendi, {sampled} modul orneklandi"
            + ("; aday listesi kisaltildi" if truncated else "")
        )
    if not retained:
        return {
            "schema": SCHEMA,
            "status": "insufficient",
            "text": (
                "Kod indeksinde bu soruya kanit olacak gecerli kod/manifest bildirimi bulunamadi. "
                "Sadece dokuman/plan kaynaklari veya dogrulanamayan chunk'lar vardi; modul "
                "sorumluluklari kanitsiz uretilmez, uydurma mimari aciklamasi yapilmaz."
            ),
            "modules": [],
            "claims": [],
            "used_chunk_ids": [],
            "coverage": {"partial": True, "notes": coverage_notes or ["gecerli kod kaniti yok"]},
            "project_id": project_id,
            "generation_digest": generation_digest,
            "source_revision": source_revision,
            "summary_digest": "",
        }

    retained_modules = {claim["module"] for claim in retained}
    omitted = [name for name in module_seen if name not in retained_modules]
    if omitted:
        coverage_notes.append("kanit butcesi nedeniyle aciklanmayan moduller: " + ", ".join(omitted[:5]))
    missing_expected = [name for name in declared_names if name not in retained_modules]
    if missing_expected:
        coverage_notes.append(
            "kok manifestoda beklenen ama indekslenmis kodu olmayan moduller: "
            + ", ".join(missing_expected[:5])
        )
    module_names = [name for name in module_seen if name in retained_modules]
    modules_out = []
    for name in module_names:
        module_retained = [claim for claim in retained if claim["module"] == name]
        modules_out.append({
            "module": name,
            "source_refs": sorted({claim["source_ref"] for claim in module_retained}),
            "roles": sorted({claim["role"] for claim in module_retained}),
        })
    evidence_ids = {cid for claim in retained for cid in claim["evidence_chunk_ids"]}
    final_used = [cid for cid in dict.fromkeys(
        cid for claim in retained for cid in claim["evidence_chunk_ids"]
    ) if cid in evidence_ids]
    coverage_notes = coverage_notes[:6]
    result = {
        "schema": SCHEMA,
        "status": "supported",
        "text": _render_text(retained, module_names, coverage_notes),
        "modules": modules_out,
        "claims": retained,
        "used_chunk_ids": final_used,
        "coverage": {
            "partial": bool(
                truncated or observed > sampled or missing_expected or omitted
                or total_candidate > len(retained)
            ),
            "notes": coverage_notes,
        },
        "project_id": project_id,
        "generation_digest": generation_digest,
        "source_revision": source_revision,
        "summary_digest": "",
    }
    result["summary_digest"] = _summary_digest(result)
    return result


def _summary_digest(result: dict) -> str:
    payload = json.dumps(
        {key: result.get(key) for key in (
            "schema", "status", "text", "claims", "coverage", "project_id",
            "generation_digest", "source_revision",
        )},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_claim(claim: object) -> bool:
    if not isinstance(claim, dict):
        return False
    evidence = claim.get("evidence_chunk_ids")
    locator = claim.get("locator")
    return (
        isinstance(claim.get("text"), str) and bool(claim["text"].strip())
        and claim.get("role") in _ROLE_NAMES
        and isinstance(claim.get("module"), str) and bool(claim["module"])
        and isinstance(claim.get("source_ref"), str) and is_implementation_source(claim["source_ref"])
        and isinstance(locator, dict) and locator.get("relative_path") == claim["source_ref"]
        and isinstance(evidence, list) and bool(evidence)
        and all(isinstance(cid, str) and bool(cid) for cid in evidence)
        and _valid_digest(claim.get("source_digest"))
        and _valid_digest(claim.get("content_digest"))
        and isinstance(claim.get("signal_kind"), str) and bool(claim["signal_kind"])
    )


def architecture_explanation(result: dict) -> str | None:
    """Native RAG sonucu (architecture_summary + citations) icin guvenli kabul.

    Serbest metin veya baglantisiz tekil ozet kabul edilmez. Ozet binding'i
    (project_id/generation_digest/source_revision) dis RAG sonucuyla, freshness
    'current'/snapshot_only=False olmak zorundadir; her kanit chunk'inin dis
    canonical citation'i ayni source_ref/source_digest/content_digest/locator/
    project_scope/source_revision tasimali, metin claim'lerden yeniden
    kurulup summary_digest ile dogrulanmalidir.
    """
    if not isinstance(result, dict) or result.get("schema") != _RAG_RESULT_SCHEMA:
        return None
    if result.get("index_freshness") != "current" or result.get("snapshot_only"):
        return None
    summary = result.get("architecture_summary")
    if not isinstance(summary, dict) or summary.get("schema") != SCHEMA or summary.get("status") != "supported":
        return None
    project_id = result.get("project_id")
    generation_digest = result.get("generation_digest")
    source_revision = result.get("source_revision")
    if (
        summary.get("project_id") != project_id
        or summary.get("generation_digest") != generation_digest
        or summary.get("source_revision") != source_revision
        or not isinstance(project_id, str) or not project_id.strip()
        or not _valid_digest(generation_digest)
        or not isinstance(source_revision, str) or not source_revision.strip()
    ):
        return None
    claims = summary.get("claims")
    used = summary.get("used_chunk_ids")
    modules = summary.get("modules")
    coverage = summary.get("coverage")
    if (
        not isinstance(claims, list) or not claims or len(claims) > _CLAIM_BUDGET
        or not isinstance(used, list) or not isinstance(modules, list)
        or not isinstance(coverage, dict)
    ):
        return None
    citations: dict[str, dict] = {}
    raw_citations = result.get("citations")
    if isinstance(raw_citations, list):
        for citation in raw_citations:
            if isinstance(citation, dict) and isinstance(citation.get("chunk_id"), str):
                citations[citation["chunk_id"]] = citation
    module_names: list[str] = []
    for entry in modules:
        if isinstance(entry, dict) and isinstance(entry.get("module"), str) and entry["module"]:
            module_names.append(entry["module"])
        else:
            return None
    evidence_ids: set[str] = set()
    for claim in claims:
        if not _valid_claim(claim) or claim["module"] not in module_names:
            return None
        for chunk_id in claim["evidence_chunk_ids"]:
            evidence_ids.add(chunk_id)
            citation = citations.get(chunk_id)
            if citation is None:
                return None
            if (
                citation.get("source_ref") != claim["source_ref"]
                or citation.get("source_digest") != claim["source_digest"]
                or citation.get("content_digest") != claim["content_digest"]
                or citation.get("source_revision") != source_revision
                or citation.get("project_scope") != project_id
                or citation.get("locator") != claim["locator"]
            ):
                return None
    if not evidence_ids or evidence_ids != set(used):
        return None
    notes = coverage.get("notes")
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        return None
    expected_text = _render_text(claims, module_names, notes)
    if summary.get("text") != expected_text or summary.get("summary_digest") != _summary_digest(summary):
        return None
    return expected_text


__all__ = [
    "SCHEMA",
    "architecture_explanation",
    "build_architecture_summary",
    "is_architecture_query",
    "is_implementation_source",
    "source_module",
]
