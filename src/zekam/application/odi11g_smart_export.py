"""ODI 11g Smart Export import, sanitization and exact lineage extraction.

The raw export remains local-only.  Only allow-listed design metadata and
secret-scanned expressions become retrieval chunks; topology credentials,
physical schemas, agents, contexts, audit users and variable values are never
copied into the sanitized representation.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator, UnitKind
from zekam.domain.retrieval import Chunk, ChunkProfile, estimate_tokens
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

SMART_IMPORT_PLAN_SCHEMA = "zekam-odi11g-smart-import-plan/v1"
SMART_BINDING_SCHEMA = "zekam-project-local-odi11g-smart-binding/v1"
SMART_SANITIZED_SCHEMA = "zekam-odi11g-sanitized-lineage/v1"
SMART_IMPORT_OPERATION = "project.odi-smart-import"
_MAX_EXPORT_BYTES = 128 * 1024 * 1024
_XML_DECLARATION_ATTACK = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
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
    object_counts: dict[str, int]
    excluded_object_counts: dict[str, int]
    skipped_secret_expressions: int

    @property
    def plan_digest(self) -> str:
        return digest_of_bytes(self.manifest)


def _regular_file(path: Path) -> tuple[Path, os.stat_result]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    info = absolute.lstat()
    attrs = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE or not stat.S_ISREG(info.st_mode):
        raise PolicyViolation("ODI Smart Export regular, non-reparse file olmali")
    if not 0 < info.st_size <= _MAX_EXPORT_BYTES:
        raise ValidationFailed("ODI Smart Export boyutu 1..128 MiB araliginda olmali")
    return absolute, info


def _sha256_file(path: Path) -> str:
    import hashlib

    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return f"sha256:{value.hexdigest()}"


def _reject_xml_declarations(path: Path) -> None:
    """Scan the complete bounded export, including declaration tokens split by a block."""

    overlap = b""
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            window = overlap + block
            if _XML_DECLARATION_ATTACK.search(window):
                raise PolicyViolation("ODI Smart Export DTD/entity tasiyamaz")
            overlap = window[-64:]


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
    *, home: Path, plan: OdiSmartImportPlan, expected_plan_digest: str
) -> dict[str, Any]:
    if expected_plan_digest != plan.plan_digest:
        raise PolicyViolation("ODI Smart import exact plan digest ister")
    current_digest, _ = _validate_smart_xml(plan.source)
    if current_digest != plan.body["source_digest"]:
        raise PolicyViolation("ODI Smart Export plan sonrasi degisti")
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    job, created = runtime.enqueue(
        idempotency_key=f"odi-smart-import:{plan.plan_digest}",
        payload=dict(plan.body) | {"dry_run": False},
        max_attempts=1,
    )
    if not created:
        snapshot = runtime.job_snapshot(job.id)
        if snapshot is None:
            raise PolicyViolation("ODI Smart import replay job bulunamadi")
        if snapshot["state"] == "recovery-required":
            if (
                not plan.destination.is_file()
                or _sha256_file(plan.destination) != plan.body["source_digest"]
            ):
                raise PolicyViolation("ODI Smart import recovery destination kaniti bulunamadi")
            binding = {
                "schema": SMART_BINDING_SCHEMA,
                "project_id": plan.body["project_id"],
                "project_slug": plan.body["project_slug"],
                "source_file": str(plan.destination),
                "source_digest": plan.body["source_digest"],
                "destination_ref": plan.body["destination_ref"],
                "plan_digest": plan.plan_digest,
                "raw_local_only": True,
                "sanitizer_required": True,
                "report_embedded": False,
            }
            KnowledgeFileStore(home).write_private_binding(
                str(plan.body["binding_ref"]), (canonical_json(binding) + "\n").encode("utf-8")
            )
            evidence = digest(
                {
                    "operation": SMART_IMPORT_OPERATION,
                    "plan_digest": plan.plan_digest,
                    "source_digest": plan.body["source_digest"],
                    "binding_digest": digest(binding),
                    "recovery": True,
                }
            )
            resolutions: list[str] = []
            for case in runtime.recovery_cases(open_only=True):
                if case.job_id == job.id:
                    resolutions.append(
                        runtime.resolve_recovery(
                            case.id, outcome="completed", evidence_digest=evidence
                        ).id
                    )
            if not resolutions:
                raise PolicyViolation("ODI Smart import recovery case bulunamadi")
            recovered = runtime.reconcile_recovery(job.id)
            return {
                "schema": "zekam-odi11g-smart-import-result/v1",
                "job_id": job.id,
                "state": recovered.state,
                "plan_digest": plan.plan_digest,
                "destination_ref": plan.body["destination_ref"],
                "source_digest": plan.body["source_digest"],
                "binding_ref": plan.body["binding_ref"],
                "replayed": True,
                "recovered": True,
                "recovery_resolution_ids": resolutions,
                "grants_authority": False,
            }
        return {
            "schema": "zekam-odi11g-smart-import-result/v1",
            "job_id": job.id,
            "state": snapshot["state"],
            "plan_digest": plan.plan_digest,
            "destination_ref": plan.body["destination_ref"],
            "replayed": True,
            "effects": snapshot["effects"],
            "grants_authority": False,
        }
    work = runtime.claim_next(
        owner_id=f"odi-smart-{os.getpid()}",
        owner_pid=os.getpid(),
        owner_token=os.urandom(32).hex(),
        lease_seconds=600,
        resources=(f"project:{plan.body['project_id']}:odi-smart",),
        supported_operations=(SMART_IMPORT_OPERATION,),
        job_id=job.id,
    )
    if work is None:
        raise PolicyViolation("ODI Smart import job claim edilemedi")
    claim, claim_created = runtime.claim_effect(
        work,
        operation=SMART_IMPORT_OPERATION,
        effect_digest=digest(
            {"plan_digest": plan.plan_digest, "destination_ref": plan.body["destination_ref"]}
        ),
        idempotency_key=f"odi-smart:{plan.plan_digest}:effect",
    )
    if not claim_created:
        runtime.finish(work, state="recovery-required")
        raise PolicyViolation("ODI Smart import effect replay; silent redispatch yasak")
    try:
        plan.destination.parent.mkdir(parents=True, exist_ok=True)
        _existing_ancestor_is_safe(plan.destination.parent)
        if plan.destination.exists():
            if _sha256_file(plan.destination) != plan.body["source_digest"]:
                raise PolicyViolation("Content-addressed ODI destination digest drift")
        else:
            temporary = plan.destination.with_name(f".{plan.destination.name}.{os.getpid()}.tmp")
            with plan.source.open("rb") as source_stream, temporary.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            if _sha256_file(temporary) != plan.body["source_digest"]:
                temporary.unlink(missing_ok=True)
                raise PolicyViolation("ODI Smart import copy digest drift")
            os.replace(temporary, plan.destination)
        binding = {
            "schema": SMART_BINDING_SCHEMA,
            "project_id": plan.body["project_id"],
            "project_slug": plan.body["project_slug"],
            "source_file": str(plan.destination),
            "source_digest": plan.body["source_digest"],
            "destination_ref": plan.body["destination_ref"],
            "plan_digest": plan.plan_digest,
            "raw_local_only": True,
            "sanitizer_required": True,
            "report_embedded": False,
        }
        KnowledgeFileStore(home).write_private_binding(
            str(plan.body["binding_ref"]), (canonical_json(binding) + "\n").encode("utf-8")
        )
        evidence = digest(
            {
                "operation": SMART_IMPORT_OPERATION,
                "plan_digest": plan.plan_digest,
                "source_digest": plan.body["source_digest"],
                "binding_digest": digest(binding),
            }
        )
        receipt = runtime.record_receipt(claim, status="completed", evidence_digest=evidence)
        runtime.finish(work, state="completed", evidence_digest=evidence)
    except Exception as exc:
        failure = digest(
            {
                "operation": SMART_IMPORT_OPERATION,
                "status": "unknown",
                "error_type": type(exc).__name__,
            }
        )
        try:
            runtime.record_receipt(claim, status="unknown", evidence_digest=failure)
            runtime.finish(work, state="recovery-required")
        except Exception:
            pass
        raise
    return {
        "schema": "zekam-odi11g-smart-import-result/v1",
        "job_id": job.id,
        "state": "completed",
        "plan_digest": plan.plan_digest,
        "destination_ref": plan.body["destination_ref"],
        "source_digest": plan.body["source_digest"],
        "binding_ref": plan.body["binding_ref"],
        "receipt_id": receipt.id,
        "replayed": False,
        "grants_authority": False,
    }


def load_smart_binding(home: Path, project_slug: str) -> dict[str, Any] | None:
    path = home / "projeler" / project_slug / "baglantilar" / "odi11g-smart.json"
    if not path.exists():
        return None
    binding = KnowledgeFileStore(home).read_private_binding(
        f"projeler/{project_slug}/baglantilar/odi11g-smart.json"
    )
    if binding.get("schema") != SMART_BINDING_SCHEMA or binding.get("project_slug") != project_slug:
        raise PolicyViolation("ODI Smart binding schema/project drift")
    source = Path(str(binding["source_file"]))
    if _sha256_file(_regular_file(source)[0]) != binding.get("source_digest"):
        raise PolicyViolation("ODI Smart binding source digest drift")
    return binding


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
    for item in objects.get("SnpCol", []):
        if key := _nonnull(item.get("ITable")):
            columns[key].append(item)
    datasets: dict[str, str] = {}
    for item in objects.get("SnpDataSet", []):
        if (dataset := _nonnull(item.get("IDataSet"))) and (pop := _nonnull(item.get("IPop"))):
            datasets[dataset] = pop
    sources: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpSourceTab", []):
        pop = _nonnull(item.get("IPop")) or datasets.get(str(item.get("IDataSet", "")))
        if pop:
            sources[pop].append(item)
    mappings: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpPopCol", []):
        if pop := _nonnull(item.get("IPop")):
            mappings[pop].append(item)
    clauses: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in objects.get("SnpPopClause", []):
        pop = _nonnull(item.get("IPop")) or datasets.get(str(item.get("IDataSet", "")))
        if pop:
            clauses[pop].append(item)

    profile = ChunkProfile(name="odi11g-smart-sanitized-v1", max_tokens=2048, overlap_tokens=0)
    chunks: list[Chunk] = []
    edges: list[dict[str, str]] = []

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
        for src in sorted(
            sources.get(pop_id, []),
            key=lambda row: (row.get("LschemaName", ""), row.get("TableName", "")),
        ):
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
            edges.append(
                {
                    "relation": "reads-from",
                    "source": f"interface:{name}",
                    "target": f"datastore:{src_schema}.{src_name}",
                }
            )
        for mapping in mappings.get(pop_id, [])[:80]:
            column = _nonnull(mapping.get("ColName")) or "unknown-column"
            expression = texts.get(str(mapping.get("ITxtMap", "")))
            lines.append(f"Mapping: {column}" + (f" <- {expression}" if expression else ""))
        for clause in clauses.get(pop_id, [])[:40]:
            expression = texts.get(str(clause.get("ITxtSql", "")))
            if expression:
                lines.append(f"{_nonnull(clause.get('ClauseType')) or 'Clause'}: {expression}")
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
            target = (
                _nonnull(pop_names.get(step.get("IPop")))
                or _nonnull(trt_names.get(step.get("ITrt")))
                or _nonnull(step.get("TableName"))
                or ""
            )
            step_name = step.get("StepName") or step.get("StepType") or step.get("IStep")
            lines.append(f"Step: {step_name} -> {target}")
            if target:
                edges.append(
                    {
                        "relation": "invokes",
                        "source": f"package:{package_name}",
                        "target": str(target),
                    }
                )
        add(
            "Package",
            f"{package_name}#IPackage={package_key}",
            f"ODI 11g package {package_name}",
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
    manifest_doc = {
        "schema": SMART_SANITIZED_SCHEMA,
        "project_id": str(project_id),
        "project_slug": project_slug,
        "source_digest": source_digest,
        "sanitizer_profile": "odi11g-smart-sanitized-v1",
        "object_counts": object_counts,
        "excluded_object_counts": dict(sorted(excluded.items())),
        "chunk_count": len(chunks),
        "lineage_edge_count": len(edge_tuple),
        "lineage_edges": edge_tuple,
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
        object_counts,
        dict(excluded),
        skipped_secret,
    )
