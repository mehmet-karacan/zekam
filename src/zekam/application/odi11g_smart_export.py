"""ODI 11g Smart Export import, sanitization and exact lineage extraction.

The raw export remains local-only.  Only allow-listed design metadata and
secret-scanned expressions become retrieval chunks; topology credentials,
physical schemas, agents, contexts, audit users and variable values are never
copied into the sanitized representation.
"""

from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID
from xml.parsers import expat

from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.knowledge import Locator, UnitKind
from zekam.domain.retrieval import Chunk, ChunkProfile, estimate_tokens

SMART_IMPORT_PLAN_SCHEMA = "zekam-odi11g-smart-import-plan/v2"
SMART_BINDING_SCHEMA = "zekam-project-local-odi11g-smart-binding/v2"
SMART_SANITIZED_SCHEMA = "zekam-odi11g-sanitized-lineage/v1"
SMART_IMPORT_OPERATION = "project.odi-smart-import"
_MAX_EXPORT_BYTES = 128 * 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400

_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "SnpProject": frozenset({"IProject", "ProjectCode", "ProjectName"}),
    "SnpFolder": frozenset({"IFolder", "ParIFolder", "IProject", "FolderName"}),
    "SnpModel": frozenset({"IMod", "CodMod", "ModName", "LschemaName", "TechIntName"}),
    "SnpSubModel": frozenset({"ISmod", "ISmodParent", "IMod", "CodSmod", "SmodName"}),
    "SnpTable": frozenset({"ITable", "IMod", "ISubModel", "TableName", "TableAlias", "TableType"}),
    "SnpCol": frozenset({"ICol", "ITable", "ColName", "SourceDt", "Longc", "Scalec", "Pos"}),
    "SnpPop": frozenset(
        {"IPop", "IFolder", "IMod", "ITable", "PopName", "TableName", "LschemaName"}
    ),
    "SnpDataSet": frozenset({"IDataSet", "IPop", "DsName", "DsOperator", "DsOrder"}),
    "SnpSourceTab": frozenset(
        {"ISourceTab", "IDataSet", "IPop", "ITable", "LschemaName", "TableName", "SrcTabAlias"}
    ),
    "SnpPopCol": frozenset(
        {"IPop", "IPopCol", "ICol", "ISourceTab", "ColName", "ITxtMap", "SourceDt"}
    ),
    "SnpPopClause": frozenset(
        {
            "IPop",
            "IDataSet",
            "IPopClause",
            "ITable1",
            "ITable2",
            "ITxtSql",
            "ClauseType",
            "JoinType",
        }
    ),
    "SnpPackage": frozenset({"IPackage", "IFolder", "PackName"}),
    "SnpStep": frozenset(
        {"IStep", "IPackage", "IPop", "ITrt", "IVar", "StepName", "StepType", "TableName", "ModCod"}
    ),
    "SnpTrt": frozenset(
        {"ITrt", "IFolder", "IProject", "TrtName", "TrtType", "KmTechno", "ITxtTrtTxt"}
    ),
    "SnpLineTrt": frozenset(
        {"ITrt", "OrdTrt", "SqlName", "ColITxt", "DefITxt", "ColLschemaName", "DefLschemaName"}
    ),
    "SnpSequence": frozenset(
        {"SeqId", "IProject", "SeqName", "DbSeqName", "LschemaName", "SeqType"}
    ),
    "SnpScen": frozenset({"ScenNo", "IPackage", "IPop", "ITrt", "ScenName", "ScenVersion"}),
    "SnpScenTask": frozenset(
        {"ScenNo", "ScenTaskNo", "Nno", "OrdTrt", "TaskName1", "TaskName2", "TaskName3", "TaskType"}
    ),
    "SnpScenStep": frozenset(
        {
            "ScenNo",
            "Nno",
            "StepName",
            "StepType",
            "TableName",
            "ModCode",
            "OkNextStep",
            "KoNextStep",
        }
    ),
    "SnpVarScen": frozenset(
        {"ScenNo", "ITxtVar", "VarName", "VarDatatype", "VarParam", "VarParamOrder", "IndStore"}
    ),
    "SnpPopMapping": frozenset(
        {"IDataSet", "IPopCol", "ISourceTab", "ISrcSet", "ITxtMap", "ExeDb"}
    ),
    "SnpReferencedObject": frozenset({"IObject", "ObjectPKasString", "ObjectAKasString"}),
    "SnpKey": frozenset(
        {
            "IKey",
            "ITable",
            "KeyName",
            "ConsType",
            "CheckFlow",
            "CheckStat",
            "IndActive",
            "IndChange",
            "IndDb",
        }
    ),
    "SnpKeyCol": frozenset({"IKey", "ICol", "Pos"}),
    "SnpJoin": frozenset(
        {
            "IJoin",
            "IScTablePk",
            "ITableFk",
            "ITablePk",
            "ITxtComplex",
            "PkCatalog",
            "PkSchema",
            "PkTableAlias",
            "PkTableName",
            "FkName",
            "FkType",
            "DelRule",
            "UpdRule",
            "CheckFlow",
            "CheckStat",
            "IndActive",
            "IndChange",
        }
    ),
    "SnpJoinCol": frozenset({"IJoin", "IColFk", "IColPk", "Pos", "PkColName"}),
    "SnpTxtHeader": frozenset({"ITxt", "Txt", "Enc"}),
}
_FORBIDDEN_CLASSES = frozenset(
    {
        "SnpConnect",
        "SnpContext",
        "SnpAgent",
        "SnpPschema",
        "SnpPschemaCont",
        "SnpUser",
        "SnpProfile",
        "SnpSession",
        "SnpSess",
        "SnpWorkRep",
        "SnpMasterRep",
    }
)


@dataclass(frozen=True, slots=True)
class OdiSmartImportPlan:
    body: dict[str, Any]
    source: Path
    destination: Path

    @property
    def plan_digest(self) -> str:
        return str(self.body["plan_digest"])


@dataclass(frozen=True, slots=True)
class OdiSanitizedPlan:
    manifest: bytes
    chunks: tuple[Chunk, ...]
    source_digest: str
    lineage_edges: tuple[dict[str, str], ...]
    unresolved_relations: tuple[dict[str, str], ...]
    object_counts: dict[str, int]
    excluded_object_counts: dict[str, int]
    skipped_secret_expressions: int

    @property
    def plan_digest(self) -> str:
        return digest_of_bytes(self.manifest)


def _regular_file(path: Path) -> tuple[Path, os.stat_result]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    _existing_ancestor_is_safe(absolute.parent)
    info = absolute.lstat()
    attrs = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE or not stat.S_ISREG(info.st_mode):
        raise PolicyViolation("ODI Smart Export regular, non-reparse file olmali")
    if not 0 < info.st_size <= _MAX_EXPORT_BYTES:
        raise ValidationFailed("ODI Smart Export boyutu 1..128 MiB araliginda olmali")
    return absolute, info


def _sha256_file(path: Path) -> str:
    import hashlib

    path, before = _regular_file(path)
    value = hashlib.sha256()
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise PolicyViolation("ODI Smart hash open identity drift")
        total = 0
        while block := os.read(descriptor, min(1024 * 1024, _MAX_EXPORT_BYTES + 1 - total)):
            total += len(block)
            if total > _MAX_EXPORT_BYTES:
                raise PolicyViolation("ODI Smart hash byte budget exceeded")
            value.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    fields = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if (
        fields(before) != fields(after)
        or fields(before) != fields(current)
        or total != before.st_size
    ):
        raise PolicyViolation("ODI Smart hash readback identity drift")
    return f"sha256:{value.hexdigest()}"


def _verify_smart_source_unchanged(path: Path, expected_digest: str) -> None:
    source, before = _regular_file(path)
    observed_digest = _sha256_file(source)
    after = source.lstat()
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ) or observed_digest != expected_digest:
        raise PolicyViolation("ODI Smart Export okuma sirasinda degisti")


def _reject_xml_declarations(path: Path) -> None:
    """Encoding-aware, bounded XML preflight; declarations are never expanded."""

    guard = expat.ParserCreate()

    def reject_declaration(*_args: object) -> None:
        raise PolicyViolation("ODI Smart Export DTD/entity tasiyamaz")

    def reject_external(*_args: object) -> int:
        raise PolicyViolation("ODI Smart Export external entity tasiyamaz")

    guard.StartDoctypeDeclHandler = reject_declaration
    guard.EntityDeclHandler = reject_declaration
    guard.ExternalEntityRefHandler = reject_external
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                total += len(block)
                if total > _MAX_EXPORT_BYTES:
                    raise ValidationFailed("ODI Smart Export boyutu 1..128 MiB araliginda olmali")
                guard.Parse(block, False)
            guard.Parse(b"", True)
    except expat.ExpatError:
        raise ValidationFailed("ODI Smart Export XML parse edilemedi") from None


def _validate_smart_xml(path: Path) -> tuple[str, int]:
    source, before = _regular_file(path)
    _reject_xml_declarations(source)
    root_name = ""
    smart_marker = False
    object_count = 0
    try:
        for event, elem in ET.iterparse(source, events=("start", "end")):
            tag = elem.tag.rsplit("}", 1)[-1]
            if event == "start" and not root_name:
                root_name = tag
            if event == "start" and tag == "SmartExportList":
                smart_marker = True
            if event == "end" and tag == "Object":
                object_count += 1
                elem.clear()
    except ET.ParseError as exc:
        raise ValidationFailed("ODI Smart Export XML parse edilemedi") from exc
    after = source.lstat()
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise PolicyViolation("ODI Smart Export okuma sirasinda degisti")
    if root_name != "SunopsisExport" or not smart_marker:
        raise ValidationFailed("Dosya ODI 11g Smart Export degil")
    return _sha256_file(source), object_count


def _existing_ancestor_is_safe(path: Path) -> None:
    current = path
    while not current.exists():
        if current.parent == current:
            raise ValidationFailed("ODI library root mevcut bounded ancestor ister")
        current = current.parent
    for item in (*reversed(current.parents), current):
        info = item.lstat()
        attrs = int(getattr(info, "st_file_attributes", 0))
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE:
            raise PolicyViolation("ODI library root/ancestor link veya reparse olamaz")
    if not current.is_dir():
        raise PolicyViolation("ODI library ancestor directory olmali")


def build_smart_import_plan(
    *, project_id: str, project_slug: str, source: Path, library_root: Path, library_name: str
) -> OdiSmartImportPlan:
    from zekam.domain.identifiers import validate_slug

    project_slug = validate_slug(project_slug)
    if str(UUID(project_id)) != project_id:
        raise ValidationFailed("ODI Smart project canonical UUID olmali")
    source, info = _regular_file(source)
    source_digest, object_count = _validate_smart_xml(source)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", library_name):
        raise ValidationFailed("ODI library name slug olmali")
    root = Path(os.path.abspath(os.fspath(library_root)))
    if root == Path(root.anchor):
        raise PolicyViolation("ODI library root bounded olmali")
    _existing_ancestor_is_safe(root)
    hex_digest = source_digest.removeprefix("sha256:")
    destination = root / library_name / "exports" / hex_digest / "design" / "SmartExport.xml"
    stable = {
        "schema": SMART_IMPORT_PLAN_SCHEMA,
        "operation": SMART_IMPORT_OPERATION,
        "project_id": project_id,
        "project_slug": project_slug,
        "library_name": library_name,
        "library_root_digest": digest_of_bytes(str(root).encode("utf-8")),
        "source_digest": source_digest,
        "source_identity_digest": digest_of_bytes(str(source).encode("utf-8")),
        "source_snapshot_contract": {
            "revision_ref": source_digest,
            "tree_digest": digest(
                {
                    "source_identity_digest": digest_of_bytes(str(source).encode("utf-8")),
                    "source_digest": source_digest,
                }
            ),
            "content_digest": digest(
                {
                    "format": "odi11g-smart-export",
                    "source_digest": source_digest,
                    "source_identity_digest": digest_of_bytes(str(source).encode("utf-8")),
                    "source_size_bytes": info.st_size,
                }
            ),
        },
        "source_size_bytes": info.st_size,
        "object_count": object_count,
        "destination_ref": f"{library_name}/exports/{hex_digest}/design/SmartExport.xml",
        "binding_ref": f"projeler/{project_slug}/baglantilar/odi11g-smart.json",
        "raw_local_only": True,
        "provider_calls_performed": 0,
        "grants_authority": False,
    }
    return OdiSmartImportPlan(
        stable | {"plan_digest": digest(stable), "dry_run": True}, source, destination
    )


def import_smart_export(
    *, home: Path, plan: OdiSmartImportPlan, expected_plan_digest: str, work_binding=None
) -> dict[str, Any]:
    from zekam.application.odi11g_smart_runtime import import_canonical_smart_export

    return import_canonical_smart_export(
        home=home, plan=plan, expected_plan_digest=expected_plan_digest, work_binding=work_binding
    )


def load_smart_binding(
    home: Path, project_slug: str, *, verify_source: bool = True
) -> dict[str, Any] | None:
    from zekam.application.odi11g_smart_runtime import load_canonical_smart_binding

    return load_canonical_smart_binding(home, project_slug, verify_source=verify_source)


def smart_binding_source_current(binding: dict[str, Any]) -> bool:
    """Best-effort freshness check; binding identity validation remains separate."""

    try:
        source = Path(str(binding["source_file"]))
        return _sha256_file(_regular_file(source)[0]) == binding.get("source_digest")
    except (KeyError, OSError, ZekamError):
        return False


def _short_class(value: str) -> str:
    return value.rsplit(".", 1)[-1]


def _objects(path: Path) -> tuple[dict[str, list[dict[str, str]]], Counter[str]]:
    allowed: dict[str, list[dict[str, str]]] = defaultdict(list)
    excluded: Counter[str] = Counter()
    for _event, elem in ET.iterparse(path, events=("end",)):
        if elem.tag.rsplit("}", 1)[-1] != "Object":
            continue
        class_name = _short_class(str(elem.attrib.get("class", "")))
        if class_name in _ALLOWED_FIELDS:
            fields: dict[str, str] = {}
            for child in elem:
                if child.tag.rsplit("}", 1)[-1] != "Field":
                    continue
                name = str(child.attrib.get("name", ""))
                if name in _ALLOWED_FIELDS[class_name]:
                    fields[name] = (child.text or "").strip()
            allowed[class_name].append(fields)
        else:
            excluded[class_name or "unknown"] += 1
        elem.clear()
    return allowed, excluded


def _nonnull(value: str | None) -> str | None:
    return value if value and value.casefold() != "null" else None


def build_sanitized_odi_plan(
    *, project_id: UUID, project_slug: str, source: Path
) -> OdiSanitizedPlan:
    source_digest, _ = _validate_smart_xml(source)
    objects, excluded = _objects(source)
    _verify_smart_source_unchanged(source, source_digest)
    texts: dict[str, str] = {}
    skipped_secret = 0
    for item in objects.get("SnpTxtHeader", []):
        key, value = _nonnull(item.get("ITxt")), _nonnull(item.get("Txt"))
        if not key or not value or item.get("Enc", "0") not in {"0", "false", "False", "null", ""}:
            continue
        if scan_text(value, relative_path="odi11g/expression"):
            skipped_secret += 1
            continue
        texts[key] = value[:6000]
    models = {
        item.get("IMod"): item for item in objects.get("SnpModel", []) if _nonnull(item.get("IMod"))
    }
    tables = {
        item.get("ITable"): item
        for item in objects.get("SnpTable", [])
        if _nonnull(item.get("ITable"))
    }
    columns: dict[str, list[dict[str, str]]] = defaultdict(list)
    columns_by_id: dict[str, dict[str, str]] = {}
    for item in objects.get("SnpCol", []):
        if key := _nonnull(item.get("ITable")):
            columns[key].append(item)
        if column_id := _nonnull(item.get("ICol")):
            columns_by_id[column_id] = item
    datasets: dict[str, str] = {}
    for item in objects.get("SnpDataSet", []):
        if (dataset := _nonnull(item.get("IDataSet"))) and (pop := _nonnull(item.get("IPop"))):
            datasets[dataset] = pop
    sources: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_tabs: dict[str, dict[str, str]] = {}
    for item in objects.get("SnpSourceTab", []):
        if source_id := _nonnull(item.get("ISourceTab")):
            source_tabs[source_id] = item
        pop = _nonnull(item.get("IPop")) or datasets.get(str(item.get("IDataSet", "")))
        if pop:
            sources[pop].append(item)
    mappings: dict[str, list[dict[str, str]]] = defaultdict(list)
    pop_columns: dict[str, dict[str, str]] = {}
    for item in objects.get("SnpPopCol", []):
        if pop := _nonnull(item.get("IPop")):
            mappings[pop].append(item)
        if pop_col := _nonnull(item.get("IPopCol")):
            pop_columns[pop_col] = item
    pop_mappings: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpPopMapping", []):
        pop_col = pop_columns.get(str(item.get("IPopCol", "")))
        pop = _nonnull(pop_col.get("IPop")) if pop_col else None
        if pop is None:
            pop = datasets.get(str(item.get("ISrcSet", "")))
        if pop:
            pop_mappings[pop].append(item)
    clauses: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpPopClause", []):
        pop = _nonnull(item.get("IPop")) or datasets.get(str(item.get("IDataSet", "")))
        if pop:
            clauses[pop].append(item)

    scenario_tasks: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpScenTask", []):
        if scenario_id := _nonnull(item.get("ScenNo")):
            scenario_tasks[scenario_id].append(item)
    scenario_steps: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpScenStep", []):
        if scenario_id := _nonnull(item.get("ScenNo")):
            scenario_steps[scenario_id].append(item)
    scenario_vars: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpVarScen", []):
        if scenario_id := _nonnull(item.get("ScenNo")):
            scenario_vars[scenario_id].append(item)
    key_by_id = {
        key_id: item for item in objects.get("SnpKey", []) if (key_id := _nonnull(item.get("IKey")))
    }
    key_columns: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpKeyCol", []):
        if key_id := _nonnull(item.get("IKey")):
            key_columns[key_id].append(item)
    join_by_id = {
        join_id: item
        for item in objects.get("SnpJoin", [])
        if (join_id := _nonnull(item.get("IJoin")))
    }
    join_columns: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpJoinCol", []):
        if join_id := _nonnull(item.get("IJoin")):
            join_columns[join_id].append(item)

    profile = ChunkProfile(name="odi11g-smart-sanitized-v1", max_tokens=2048, overlap_tokens=0)
    chunks: list[Chunk] = []
    edges: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []

    def add(kind: str, identity: str, title: str, lines: list[str]) -> None:
        text = "\n".join([title, *lines]).strip()[:12000]
        if not text or scan_text(text, relative_path=f"odi11g/{kind}/{identity}"):
            return
        order = len(chunks)
        object_name = f"ODI:{kind}:{identity}"
        locator_key = digest_of_bytes(identity.encode("utf-8"))[-16:]
        chunks.append(
            Chunk(
                chunk_id=f"odi-{source_digest[-12:]}-{kind.casefold()}-{locator_key}",
                document_id=f"odi-{project_id}-{source_digest[-16:]}",
                text=text,
                locator=Locator(
                    object_name=object_name, relative_path=f"odi11g/{kind.casefold()}/{locator_key}"
                ),
                kind=UnitKind.DB_OBJECT,
                token_count=estimate_tokens(text),
                order=order,
                profile_digest=profile.profile_digest,
            )
        )

    def datastore_ref(table_id: str | None) -> str | None:
        if not table_id or table_id not in tables:
            return None
        table = tables[table_id]
        model = models.get(str(table.get("IMod", "")), {})
        schema = _nonnull(model.get("LschemaName")) or "unknown"
        name = _nonnull(table.get("TableName")) or f"ITable={table_id}"
        return f"datastore:{schema}.{name}"

    def column_ref(column_id: str | None) -> str | None:
        if not column_id or column_id not in columns_by_id:
            return None
        column = columns_by_id[column_id]
        table_ref = datastore_ref(_nonnull(column.get("ITable")))
        name = _nonnull(column.get("ColName")) or f"ICol={column_id}"
        return f"{table_ref}.{name}" if table_ref else None

    known_object_ids: set[str] = set()
    for records in objects.values():
        for record in records:
            for field in (
                "IProject",
                "IFolder",
                "IMod",
                "ISmod",
                "ITable",
                "ICol",
                "IPop",
                "IPopCol",
                "IDataSet",
                "ISourceTab",
                "IPackage",
                "IStep",
                "ITrt",
                "ScenNo",
                "IKey",
                "IJoin",
            ):
                if value := _nonnull(record.get(field)):
                    known_object_ids.add(value)

    for pop_record in objects.get("SnpPop", []):
        pop_id = _nonnull(pop_record.get("IPop"))
        if not pop_id:
            continue
        name = _nonnull(pop_record.get("PopName")) or f"IPop={pop_id}"
        target_table = tables.get(str(pop_record.get("ITable", "")), {})
        target_model = models.get(str(target_table.get("IMod", pop_record.get("IMod", ""))), {})
        target = (
            _nonnull(pop_record.get("TableName"))
            or _nonnull(target_table.get("TableName"))
            or "unknown"
        )
        target_schema = (
            _nonnull(pop_record.get("LschemaName"))
            or _nonnull(target_model.get("LschemaName"))
            or "unknown"
        )
        lines = [f"Interface: {name}", f"Target: {target_schema}.{target}"]
        target_id = _nonnull(pop_record.get("ITable"))
        target_resolved = target_id is not None and target_id in tables
        if not target_resolved:
            unresolved.append(
                {
                    "relation": "writes-to",
                    "source": f"interface:{name}",
                    "target": f"datastore:ITable={target_id or 'missing'}",
                    "reason": "target-datastore-unresolved",
                }
            )
        for src in sorted(
            sources.get(pop_id, []),
            key=lambda row: (row.get("LschemaName", ""), row.get("TableName", "")),
        ):
            src_table_id = _nonnull(src.get("ITable"))
            src_table = tables.get(str(src.get("ITable", "")), {})
            src_model = models.get(str(src_table.get("IMod", "")), {})
            src_name = (
                _nonnull(src.get("TableName")) or _nonnull(src_table.get("TableName")) or "unknown"
            )
            src_schema = (
                _nonnull(src.get("LschemaName"))
                or _nonnull(src_model.get("LschemaName"))
                or "unknown"
            )
            lines.append(
                f"Source: {src_schema}.{src_name} alias={_nonnull(src.get('SrcTabAlias')) or '-'}"
            )
            if src_table_id is not None and src_table_id in tables:
                edges.append(
                    {
                        "relation": "reads-from",
                        "source": f"interface:{name}",
                        "target": f"datastore:{src_schema}.{src_name}",
                    }
                )
            else:
                unresolved.append(
                    {
                        "relation": "reads-from",
                        "source": f"interface:{name}",
                        "target": f"datastore:ITable={src_table_id or 'missing'}",
                        "reason": "source-datastore-unresolved",
                    }
                )
        for mapping in mappings.get(pop_id, [])[:80]:
            column = _nonnull(mapping.get("ColName")) or "unknown-column"
            expression = texts.get(str(mapping.get("ITxtMap", "")))
            lines.append(f"Mapping: {column}" + (f" <- {expression}" if expression else ""))
        for mapping in pop_mappings.get(pop_id, [])[:200]:
            pop_col = pop_columns.get(str(mapping.get("IPopCol", "")), {})
            column = (
                _nonnull(pop_col.get("ColName")) or f"IPopCol={mapping.get('IPopCol', 'missing')}"
            )
            expression = texts.get(str(mapping.get("ITxtMap", "")))
            lines.append(f"MappingRef: {column}" + (f" <- {expression}" if expression else ""))
            source_tab = source_tabs.get(str(mapping.get("ISourceTab", "")))
            if source_tab is None:
                source_tab = next(iter(sources.get(pop_id, ())), None)
            source_table_id = _nonnull(source_tab.get("ITable")) if source_tab else None
            source_ref = datastore_ref(source_table_id)
            if source_ref:
                edges.append(
                    {
                        "relation": "maps-from",
                        "source": f"interface:{name}",
                        "target": source_ref,
                    }
                )
            else:
                unresolved.append(
                    {
                        "relation": "maps-from",
                        "source": f"interface:{name}",
                        "target": f"datastore:ITable={source_table_id or 'missing'}",
                        "reason": "mapping-source-unresolved",
                    }
                )
        for clause in clauses.get(pop_id, [])[:40]:
            expression = texts.get(str(clause.get("ITxtSql", "")))
            if expression:
                lines.append(f"{_nonnull(clause.get('ClauseType')) or 'Clause'}: {expression}")
        if target_resolved:
            edges.append(
                {
                    "relation": "writes-to",
                    "source": f"interface:{name}",
                    "target": f"datastore:{target_schema}.{target}",
                }
            )
        add("Interface", f"{name}#IPop={pop_id}", f"ODI 11g interface {name}", lines)

    for table_id, table in sorted(
        tables.items(), key=lambda row: (row[1].get("TableName", ""), str(row[0]))
    ):
        name = _nonnull(table.get("TableName")) or f"ITable={table_id}"
        model = models.get(str(table.get("IMod", "")), {})
        schema = _nonnull(model.get("LschemaName")) or "unknown"
        cols = sorted(columns.get(str(table_id), []), key=lambda row: int(row.get("Pos", "0") or 0))
        lines = [
            f"Datastore: {schema}.{name}",
            "Model: "
            + (_nonnull(model.get("ModName")) or _nonnull(model.get("CodMod")) or "unknown"),
        ]
        for col in cols[:200]:
            column_type = (
                f"{col.get('SourceDt', '')}({col.get('Longc', '')},{col.get('Scalec', '')})"
            )
            lines.append(f"Column: {col.get('ColName', 'unknown')} {column_type}")
        add(
            "Datastore",
            f"{schema}.{name}#ITable={table_id}",
            f"ODI 11g datastore {schema}.{name}",
            lines,
        )

    key_refs: dict[str, str] = {}
    for key_id, key in sorted(key_by_id.items()):
        key_name = _nonnull(key.get("KeyName")) or f"IKey={key_id}"
        key_ref = f"key:{key_name}#IKey={key_id}"
        key_refs[key_id] = key_ref
        lines = [
            f"Key: {key_name}",
            f"ConstraintType: {_nonnull(key.get('ConsType')) or 'unknown'}",
        ]
        add("Key", f"{key_name}#IKey={key_id}", f"ODI 11g key {key_name}", lines)
        table_ref = datastore_ref(_nonnull(key.get("ITable")))
        if table_ref:
            edges.append({"relation": "has-key", "source": table_ref, "target": key_ref})
        else:
            unresolved.append(
                {
                    "relation": "has-key",
                    "source": key_ref,
                    "target": f"datastore:ITable={key.get('ITable') or 'missing'}",
                    "reason": "key-table-unresolved",
                }
            )
    for key_id, records in sorted(key_columns.items()):
        key_ref = key_refs.get(key_id)
        if key_ref is None:
            continue
        for record in records:
            column = column_ref(_nonnull(record.get("ICol")))
            if column:
                edges.append(
                    {"relation": "key-contains-column", "source": key_ref, "target": column}
                )
            else:
                unresolved.append(
                    {
                        "relation": "key-contains-column",
                        "source": key_ref,
                        "target": f"column:ICol={record.get('ICol') or 'missing'}",
                        "reason": "key-column-unresolved",
                    }
                )

    join_refs: dict[str, str] = {}
    for join_id, join in sorted(join_by_id.items()):
        join_ref = f"join:IJoin={join_id}"
        join_refs[join_id] = join_ref
        lines = [
            f"Join: {join.get('PkTableName') or 'unknown'} <- {join.get('FkName') or 'unknown'}",
            f"DeleteRule: {_nonnull(join.get('DelRule')) or 'unknown'}",
            f"UpdateRule: {_nonnull(join.get('UpdRule')) or 'unknown'}",
        ]
        add("Join", f"IJoin={join_id}", f"ODI 11g join IJoin={join_id}", lines)
        pk_ref = datastore_ref(_nonnull(join.get("ITablePk")))
        fk_ref = datastore_ref(_nonnull(join.get("ITableFk")))
        if pk_ref and fk_ref:
            edges.append({"relation": "join-connects", "source": fk_ref, "target": pk_ref})
        else:
            unresolved.append(
                {
                    "relation": "join-connects",
                    "source": join_ref,
                    "target": f"datastore:ITable={join.get('ITablePk') or 'missing'}",
                    "reason": "join-table-unresolved",
                }
            )
    for join_id, records in sorted(join_columns.items()):
        join_ref = join_refs.get(join_id)
        if join_ref is None:
            continue
        for record in records:
            pk_col = column_ref(_nonnull(record.get("IColPk")))
            fk_col = column_ref(_nonnull(record.get("IColFk")))
            if pk_col and fk_col:
                edges.append({"relation": "join-connects", "source": fk_col, "target": pk_col})
            else:
                unresolved.append(
                    {
                        "relation": "join-connects",
                        "source": join_ref,
                        "target": f"column:ICol={record.get('IColPk') or 'missing'}",
                        "reason": "join-column-unresolved",
                    }
                )

    for index, reference in enumerate(objects.get("SnpReferencedObject", []), start=1):
        object_id = _nonnull(reference.get("IObject"))
        primary = _nonnull(reference.get("ObjectPKasString")) or "-"
        alternate = _nonnull(reference.get("ObjectAKasString")) or "-"
        reference_ref = f"reference:{index}:IObject={object_id or 'missing'}"
        add(
            "Reference",
            reference_ref,
            f"ODI 11g referenced object {object_id or 'missing'}",
            [f"PrimaryKey: {primary}", f"AlternateKey: {alternate}"],
        )
        if object_id and object_id in known_object_ids:
            edges.append(
                {
                    "relation": "references-object",
                    "source": reference_ref,
                    "target": f"object:IObject={object_id}",
                }
            )
        else:
            unresolved.append(
                {
                    "relation": "references-object",
                    "source": reference_ref,
                    "target": f"object:IObject={object_id or 'missing'}",
                    "reason": "referenced-object-unresolved",
                }
            )

    pop_names = {item.get("IPop"): item.get("PopName") for item in objects.get("SnpPop", [])}
    trt_names = {item.get("ITrt"): item.get("TrtName") for item in objects.get("SnpTrt", [])}
    package_steps: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpStep", []):
        if key := _nonnull(item.get("IPackage")):
            package_steps[key].append(item)
    for package in objects.get("SnpPackage", []):
        package_key = _nonnull(package.get("IPackage"))
        package_name = _nonnull(package.get("PackName"))
        if not package_key or not package_name:
            continue
        lines = [f"Package: {package_name}"]
        for step in package_steps.get(package_key, []):
            step_pop = _nonnull(step.get("IPop"))
            step_trt = _nonnull(step.get("ITrt"))
            target = (
                _nonnull(pop_names.get(step.get("IPop")))
                or _nonnull(trt_names.get(step.get("ITrt")))
                or _nonnull(step.get("TableName"))
                or ""
            )
            step_name = step.get("StepName") or step.get("StepType") or step.get("IStep")
            lines.append(f"Step: {step_name} -> {target}")
            if target:
                if (step_pop and step_pop in pop_names) or (step_trt and step_trt in trt_names):
                    edges.append(
                        {
                            "relation": "invokes",
                            "source": f"package:{package_name}",
                            "target": str(target),
                        }
                    )
                else:
                    unresolved.append(
                        {
                            "relation": "invokes",
                            "source": f"package:{package_name}",
                            "target": f"step:{step.get('IStep') or 'missing'}",
                            "reason": "package-step-target-unresolved",
                        }
                    )
        add(
            "Package",
            f"{package_name}#IPackage={package_key}",
            f"ODI 11g package {package_name}",
            lines,
        )

    scenario_packages = {
        _nonnull(item.get("IPackage")): item
        for item in objects.get("SnpPackage", [])
        if _nonnull(item.get("IPackage"))
    }
    scenario_pops = {
        _nonnull(item.get("IPop")): item
        for item in objects.get("SnpPop", [])
        if _nonnull(item.get("IPop"))
    }
    scenario_trts = {
        _nonnull(item.get("ITrt")): item
        for item in objects.get("SnpTrt", [])
        if _nonnull(item.get("ITrt"))
    }
    for scenario in objects.get("SnpScen", []):
        scenario_id = _nonnull(scenario.get("ScenNo")) or digest(scenario)[-12:]
        scenario_name = _nonnull(scenario.get("ScenName")) or f"ScenNo={scenario_id}"
        lines = [
            f"Scenario: {scenario_name}",
            f"Version: {_nonnull(scenario.get('ScenVersion')) or 'unknown'}",
        ]
        references = (
            ("package", _nonnull(scenario.get("IPackage")), scenario_packages),
            ("interface", _nonnull(scenario.get("IPop")), scenario_pops),
            ("procedure", _nonnull(scenario.get("ITrt")), scenario_trts),
        )
        scenario_ref = f"scenario:{scenario_name}#ScenNo={scenario_id}"
        for relation_kind, reference_id, registry in references:
            if reference_id is None:
                continue
            referenced = registry.get(reference_id)
            if referenced is None:
                unresolved.append(
                    {
                        "relation": "scenario-refers-to",
                        "source": f"scenario:{scenario_name}#ScenNo={scenario_id}",
                        "target": f"{relation_kind}:id={reference_id}",
                        "reason": "scenario-reference-unresolved",
                    }
                )
                continue
            target_name = (
                _nonnull(referenced.get("PackName"))
                or _nonnull(referenced.get("PopName"))
                or _nonnull(referenced.get("TrtName"))
                or f"id={reference_id}"
            )
            edges.append(
                {
                    "relation": "scenario-refers-to",
                    "source": scenario_ref,
                    "target": f"{relation_kind}:{target_name}",
                }
            )
            lines.append(f"{relation_kind.title()}: {target_name}")

        for index, task in enumerate(
            sorted(
                scenario_tasks.get(scenario_id, []),
                key=lambda row: (row.get("Nno", ""), row.get("ScenTaskNo", "")),
            ),
            start=1,
        ):
            task_key = _nonnull(task.get("ScenTaskNo")) or f"Nno={task.get('Nno') or index}"
            task_ref = f"scenario-task:{scenario_id}#{task_key}"
            task_name = (
                _nonnull(task.get("TaskName1"))
                or _nonnull(task.get("TaskName2"))
                or _nonnull(task.get("TaskName3"))
                or task_key
            )
            task_lines = [
                f"Scenario: {scenario_name}",
                f"Task: {task_name}",
                f"Type: {_nonnull(task.get('TaskType')) or 'unknown'}",
                f"Order: {_nonnull(task.get('Nno')) or 'unknown'}",
            ]
            add(
                "ScenarioTask",
                f"{scenario_id}#{task_key}",
                f"ODI 11g scenario task {task_name}",
                task_lines,
            )
            edges.append(
                {"relation": "scenario-contains-task", "source": scenario_ref, "target": task_ref}
            )
            lines.append(f"Task: {task_name}")

        step_numbers = {
            _nonnull(step.get("Nno"))
            for step in scenario_steps.get(scenario_id, [])
            if _nonnull(step.get("Nno"))
        }
        for index, step in enumerate(
            sorted(scenario_steps.get(scenario_id, []), key=lambda row: row.get("Nno", "")),
            start=1,
        ):
            step_number = _nonnull(step.get("Nno")) or str(index)
            step_ref = f"scenario-step:{scenario_id}#Nno={step_number}"
            step_name = _nonnull(step.get("StepName")) or f"Nno={step_number}"
            step_lines = [
                f"Scenario: {scenario_name}",
                f"Step: {step_name}",
                f"Type: {_nonnull(step.get('StepType')) or 'unknown'}",
            ]
            add(
                "ScenarioStep",
                f"{scenario_id}#Nno={step_number}",
                f"ODI 11g scenario step {step_name}",
                step_lines,
            )
            edges.append(
                {"relation": "scenario-contains-step", "source": scenario_ref, "target": step_ref}
            )
            lines.append(f"Step: {step_name}")
            for field, relation in (
                ("OkNextStep", "scenario-step-next-ok"),
                ("KoNextStep", "scenario-step-next-ko"),
            ):
                next_step = _nonnull(step.get(field))
                if next_step is None:
                    continue
                target = f"scenario-step:{scenario_id}#Nno={next_step}"
                if next_step in step_numbers:
                    edges.append({"relation": relation, "source": step_ref, "target": target})
                else:
                    unresolved.append(
                        {
                            "relation": relation,
                            "source": step_ref,
                            "target": target,
                            "reason": "scenario-step-next-unresolved",
                        }
                    )

        for index, variable in enumerate(scenario_vars.get(scenario_id, []), start=1):
            variable_name = _nonnull(variable.get("VarName")) or f"variable-{index}"
            variable_ref = f"scenario-variable:{scenario_id}#{variable_name}"
            variable_lines = [
                f"Scenario: {scenario_name}",
                f"Variable: {variable_name}",
                f"Datatype: {_nonnull(variable.get('VarDatatype')) or 'unknown'}",
            ]
            add(
                "ScenarioVariable",
                f"{scenario_id}#{variable_name}",
                f"ODI 11g scenario variable {variable_name}",
                variable_lines,
            )
            edges.append(
                {
                    "relation": "scenario-contains-variable",
                    "source": scenario_ref,
                    "target": variable_ref,
                }
            )
            lines.append(f"Variable: {variable_name}")
        add(
            "Scenario",
            f"{scenario_name}#ScenNo={scenario_id}",
            f"ODI 11g scenario {scenario_name}",
            lines,
        )

    trt_lines: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpLineTrt", []):
        if key := _nonnull(item.get("ITrt")):
            trt_lines[key].append(item)
    for trt in objects.get("SnpTrt", []):
        trt_key = _nonnull(trt.get("ITrt"))
        trt_name = _nonnull(trt.get("TrtName"))
        if not trt_key or not trt_name:
            continue
        lines = [
            f"Procedure/KM: {trt_name}",
            f"Type: {trt.get('TrtType', '')}",
            f"Technology: {trt.get('KmTechno', '')}",
        ]
        for line in sorted(
            trt_lines.get(trt_key, []), key=lambda row: int(row.get("OrdTrt", "0") or 0)
        ):
            sql = texts.get(str(line.get("ColITxt", ""))) or texts.get(str(line.get("DefITxt", "")))
            lines.append(f"Task: {line.get('SqlName', '')}" + (f"\n{sql}" if sql else ""))
        add(
            "Procedure",
            f"{trt_name}#ITrt={trt_key}",
            f"ODI 11g procedure {trt_name}",
            lines,
        )

    object_counts = {
        name: len(items) for name, items in sorted(objects.items()) if name != "SnpTxtHeader"
    }
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise PolicyViolation("ODI sanitizer duplicate chunk identity uretti")
    edge_tuple = tuple(
        sorted(edges, key=lambda row: (row["source"], row["relation"], row["target"]))
    )
    unresolved_tuple = tuple(
        sorted(
            unresolved,
            key=lambda row: (row["source"], row["relation"], row["target"], row["reason"]),
        )
    )
    manifest_doc = {
        "schema": SMART_SANITIZED_SCHEMA,
        "project_id": str(project_id),
        "project_slug": project_slug,
        "source_digest": source_digest,
        "source_format": "odi11g-smart-export",
        "sanitizer_profile": "odi11g-smart-sanitized-v1",
        "object_counts": object_counts,
        "excluded_object_counts": dict(sorted(excluded.items())),
        "chunk_count": len(chunks),
        "lineage_edge_count": len(edge_tuple),
        "lineage_edges": edge_tuple,
        "unresolved_relation_count": len(unresolved_tuple),
        "unresolved_relations": unresolved_tuple,
        "skipped_secret_expressions": skipped_secret,
        "raw_xml_embedded": False,
        "credentials_embedded": False,
        "physical_topology_embedded": False,
        "variable_values_embedded": False,
        "audit_users_embedded": False,
        "report_embedded": False,
        "data_classification": "confidential-corporate",
    }
    return OdiSanitizedPlan(
        canonical_json(manifest_doc).encode("utf-8"),
        tuple(chunks),
        source_digest,
        edge_tuple,
        unresolved_tuple,
        object_counts,
        dict(excluded),
        skipped_secret,
    )
