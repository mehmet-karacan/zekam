from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from zekam.application.oracle_metadata_index import OracleDdlObject, OracleMetadataSnapshot
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.application.secret_detection import scan_text


def _fail(message: str) -> None:
    raise ValidationFailed("invalid oracle metadata snapshot")


def _check_path_component(part: str) -> None:
    if not part:
        _fail("empty path component")
    if part in (".", ".."):
        _fail("traversal component")
    if len(part) > 255:
        _fail("path component too long")
    if any(ord(c) < 32 for c in part):
        _fail("control character in path")


def _is_symlink_or_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x400)


def _validate_relative_directory(relative_directory: str) -> str:
    if not isinstance(relative_directory, str):
        _fail("relative directory must be string")
    rel = relative_directory
    if not rel or rel != rel.strip():
        _fail("relative directory invalid")
    if "\x00" in rel:
        _fail("relative directory contains NUL")
    if any(ord(c) < 32 for c in rel):
        _fail("control character in path")
    if rel.startswith("/") or rel.startswith("\\"):
        _fail("absolute directory")
    wpath = PureWindowsPath(rel)
    if wpath.anchor:
        _fail("drive or UNC path detected")
    if "/" in rel and "\\" in rel:
        _fail("mixed separators")
    sep = "/" if "/" in rel else "\\"
    parts = rel.split(sep)
    if any(p == "" for p in parts):
        _fail("duplicate slash ambiguity")
    for part in parts:
        _check_path_component(part)
    return rel


def _safe_identifier(value: Any, max_len: int = 128) -> str:
    if not isinstance(value, str):
        _fail("identifier must be string")
    s = value
    if not s or len(s) > max_len:
        _fail("identifier length invalid")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", s):
        _fail("identifier characters invalid")
    return s


def _safe_token(value: Any, max_len: int = 64) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if not isinstance(value, str):
        _fail("token must be string or bool")
    s = value
    if s == "N/A":
        return s
    if not s or len(s) > max_len:
        _fail("token length invalid")
    if any(ord(c) < 32 for c in s):
        _fail("control character in token")
    if not re.fullmatch(r"[A-Z0-9_ -]+", s):
        _fail("token characters invalid")
    return s


def _bounded_int(value: Any, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        _fail("integer expected, got bool")
    if not isinstance(value, int):
        _fail("integer expected")
    if minimum is not None and value < minimum:
        _fail("integer below minimum")
    if maximum is not None and value > maximum:
        _fail("integer above maximum")
    return value


def _optional_bounded_int(value: Any, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value is None:
        return None
    effective_max = maximum if maximum is not None else 1_000_000_000
    return _bounded_int(value, minimum, effective_max)


def _nonnegative_int(value: Any) -> int:
    return _bounded_int(value, minimum=0)


def _positive_int(value: Any) -> int:
    return _bounded_int(value, minimum=1)


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        _fail("boolean expected")
    return value


def _raw_hex64(value: Any) -> str:
    if not isinstance(value, str):
        _fail("hex string expected")
    if len(value) != 64 or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail("hex format invalid")
    return value


def _source_ref_table(schema: str, name: str) -> str:
    return f"oracle://{schema}/TABLE/{name}"


def _source_ref_table_column(schema: str, table: str, column: str) -> str:
    return f"oracle://{schema}/TABLE/{table}/COLUMN/{column}"


def _source_ref_constraint(schema: str, table: str, constraint: str) -> str:
    return f"oracle://{schema}/TABLE/{table}/CONSTRAINT/{constraint}"


def _source_ref_index(schema: str, table: str, index: str) -> str:
    return f"oracle://{schema}/TABLE/{table}/INDEX/{index}"


def _read_capped(path: Path, cap: int) -> bytes:
    with open(path, "rb") as fh:
        chunk = fh.read(cap + 1)
    if len(chunk) > cap:
        _fail("file exceeds size limit")
    return chunk


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _load_json_impl(raw: bytes, cap: int) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("invalid encoding")
    if len(text.encode("utf-8")) > cap:
        _fail("file exceeds size limit")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError):
        _fail("invalid JSON")


def _load_json(raw: bytes, cap: int) -> Any:
    try:
        return _load_json_impl(raw, cap)
    except (OSError, TypeError, ValueError, RecursionError):
        _fail("invalid JSON")


def _format_version(value: Any) -> int:
    if isinstance(value, bool):
        _fail("formatVersion must be integer")
    if not isinstance(value, int):
        _fail("formatVersion must be integer")
    if value != 2:
        _fail("formatVersion must be 2")
    return value


def _application_version(value: Any) -> str:
    if not isinstance(value, str):
        _fail("applicationVersion must be string")
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        _fail("applicationVersion format invalid")
    return value


def _validate_manifest(data: Any) -> tuple[str, str, str, dict[str, int]]:
    if not isinstance(data, dict):
        _fail("manifest must be object")
    allowed = {"formatVersion", "applicationVersion", "schema", "database", "metadataSha256", "counts", "scope"}
    extra = set(data.keys()) - allowed
    if extra:
        _fail("unknown manifest keys")
    missing = {"formatVersion", "applicationVersion", "schema", "database", "metadataSha256", "counts"} - set(data.keys())
    if missing:
        _fail("missing manifest keys")

    _format_version(data["formatVersion"])
    _application_version(data["applicationVersion"])
    schema = _safe_identifier(data["schema"], 128)
    database = _safe_identifier(data["database"], 128)
    metadata_sha256 = _raw_hex64(data["metadataSha256"])

    counts = data["counts"]
    if not isinstance(counts, dict):
        _fail("counts must be object")
    count_keys = {"tables", "columns", "constraints", "indexes"}
    if set(counts.keys()) != count_keys:
        _fail("counts keys mismatch")
    tables_count = _nonnegative_int(counts["tables"])
    columns_count = _nonnegative_int(counts["columns"])
    constraints_count = _nonnegative_int(counts["constraints"])
    indexes_count = _nonnegative_int(counts["indexes"])
    if tables_count > 20000:
        _fail("too many tables")
    if columns_count > 500000:
        _fail("too many columns")
    if constraints_count > 500000:
        _fail("too many constraints")
    if indexes_count > 500000:
        _fail("too many indexes")
    return schema, database, metadata_sha256, {
        "tables": tables_count,
        "columns": columns_count,
        "constraints": constraints_count,
        "indexes": indexes_count,
    }


def _validate_column(col: Any, schema: str, table: str, seen_names: set[str], seen_positions: set[int]) -> None:
    if not isinstance(col, dict):
        _fail("column must be object")
    required = {"name", "position", "type", "nullable"}
    optional = {"byteLength", "charLength", "charSemantics", "precision", "scale", "sourceRef"}
    allowed = required | optional
    extra = set(col.keys()) - allowed
    if extra:
        _fail("unknown column keys")
    missing = required - set(col.keys())
    if missing:
        _fail("missing column keys")

    name = _safe_identifier(col["name"], 128)
    if name in seen_names:
        _fail("duplicate column name")
    seen_names.add(name)

    position = _positive_int(col["position"])
    if position in seen_positions:
        _fail("duplicate column position")
    seen_positions.add(position)

    ctype = col["type"]
    if not isinstance(ctype, str) or not ctype or len(ctype) > 128:
        _fail("column type invalid")
    if not re.fullmatch(r"[A-Za-z0-9 ()_]+", ctype):
        _fail("column type characters invalid")

    _bool(col["nullable"])

    if "byteLength" in col:
        _optional_bounded_int(col["byteLength"], minimum=0)
    if "charLength" in col:
        _optional_bounded_int(col["charLength"], minimum=0)
    if "precision" in col:
        _optional_bounded_int(col["precision"], minimum=0)
    if "scale" in col:
        _optional_bounded_int(col["scale"], minimum=-84, maximum=127)
    if "charSemantics" in col:
        cs = col["charSemantics"]
        if cs is not None and cs not in ("B", "C"):
            _fail("charSemantics invalid")
    if "sourceRef" in col:
        sr = col["sourceRef"]
        if sr is not None and sr != _source_ref_table_column(schema, table, name):
            _fail("sourceRef mismatch")


def _validate_constraint(con: Any, schema: str, table: str, table_columns: set[str], seen_names: set[str]) -> None:
    if not isinstance(con, dict):
        _fail("constraint must be object")
    required = {"name", "kind", "columns"}
    optional = {"references", "referenceColumns", "sourceRef", "deferrable", "deferred", "deleteRule", "status", "validated"}
    allowed = required | optional
    extra = set(con.keys()) - allowed
    if extra:
        _fail("unknown constraint keys")
    missing = required - set(con.keys())
    if missing:
        _fail("missing constraint keys")

    name = _safe_identifier(con["name"], 128)
    if name in seen_names:
        _fail("duplicate constraint name")
    seen_names.add(name)

    kind = con["kind"]
    if kind not in ("P", "U", "R", "C"):
        _fail("constraint kind invalid")

    cols = con["columns"]
    if not isinstance(cols, list):
        _fail("constraint columns must be list")
    for c in cols:
        cn = _safe_identifier(c, 128)
        if cn not in table_columns:
            _fail("constraint column not in table")

    if kind in ("P", "U", "R") and not cols:
        _fail("constraint columns required")

    if kind == "R":
        if "references" not in con or con["references"] is None:
            _fail("foreign key references required")
        _safe_identifier(con["references"], 128)
        if "referenceColumns" not in con or con["referenceColumns"] is None:
            _fail("foreign key referenceColumns required")
        rcols = con["referenceColumns"]
        if not isinstance(rcols, list):
            _fail("referenceColumns must be list")
        if len(rcols) != len(cols):
            _fail("referenceColumns length mismatch")
        for rc in rcols:
            _safe_identifier(rc, 128)
    elif "references" in con:
        ref = con["references"]
        if ref is not None:
            _safe_identifier(ref, 128)
    if "referenceColumns" in con and kind != "R":
        rcols = con["referenceColumns"]
        if not isinstance(rcols, list):
            _fail("referenceColumns must be list")
        for rc in rcols:
            _safe_identifier(rc, 128)
    if "sourceRef" in con:
        sr = con["sourceRef"]
        if sr is not None and sr != _source_ref_constraint(schema, table, name):
            _fail("constraint sourceRef mismatch")
    for flag_key in ("deferrable", "deferred", "validated"):
        if flag_key in con and con[flag_key] is not None:
            _safe_token(con[flag_key], 64)
    for token_key in ("deleteRule", "status"):
        if token_key in con and con[token_key] is not None:
            _safe_token(con[token_key], 64)


def _validate_index(idx: Any, schema: str, table: str, seen_names: set[str]) -> None:
    if not isinstance(idx, dict):
        _fail("index must be object")
    required = {"name", "kind", "columns"}
    optional = {"unique", "expressionOmitted", "status", "visibility", "sourceRef"}
    allowed = required | optional
    extra = set(idx.keys()) - allowed
    if extra:
        _fail("unknown index keys")
    missing = required - set(idx.keys())
    if missing:
        _fail("missing index keys")

    name = _safe_identifier(idx["name"], 128)
    if name in seen_names:
        _fail("duplicate index name")
    seen_names.add(name)

    kind = idx["kind"]
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        _fail("index kind invalid")
    if any(ord(c) < 32 for c in kind):
        _fail("control character in index kind")
    if not re.fullmatch(r"[A-Z0-9 -]+", kind):
        _fail("index kind characters invalid")

    cols = idx["columns"]
    if not isinstance(cols, list):
        _fail("index columns must be list")
    for c in cols:
        _safe_identifier(c, 128)

    if "unique" in idx:
        _bool(idx["unique"])
    if "expressionOmitted" in idx:
        _bool(idx["expressionOmitted"])
    for token_key in ("status", "visibility"):
        if token_key in idx and idx[token_key] is not None:
            _safe_token(idx[token_key], 64)
    if "sourceRef" in idx:
        sr = idx["sourceRef"]
        if sr is not None and sr != _source_ref_index(schema, table, name):
            _fail("index sourceRef mismatch")


def _validate_table(tbl: Any, schema: str, seen_tables: set[str]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(tbl, dict):
        _fail("table must be object")
    required = {"name", "columns", "constraints", "indexes"}
    optional = {"sha256", "sourceRef"}
    allowed = required | optional
    extra = set(tbl.keys()) - allowed
    if extra:
        _fail("unknown table keys")
    missing = required - set(tbl.keys())
    if missing:
        _fail("missing table keys")

    name = _safe_identifier(tbl["name"], 128)
    if name in seen_tables:
        _fail("duplicate table name")
    seen_tables.add(name)

    if "sha256" in tbl and tbl["sha256"] is not None:
        _raw_hex64(tbl["sha256"])
    if "sourceRef" in tbl:
        sr = tbl["sourceRef"]
        if sr is not None and sr != _source_ref_table(schema, name):
            _fail("table sourceRef mismatch")

    cols = tbl["columns"]
    if not isinstance(cols, list):
        _fail("columns must be list")
    seen_col_names: set[str] = set()
    seen_col_pos: set[int] = set()
    for col in cols:
        _validate_column(col, schema, name, seen_col_names, seen_col_pos)

    cons = tbl["constraints"]
    if not isinstance(cons, list):
        _fail("constraints must be list")
    seen_con_names: set[str] = set()
    table_cols = {c["name"] for c in cols}
    for con in cons:
        _validate_constraint(con, schema, name, table_cols, seen_con_names)

    idxs = tbl["indexes"]
    if not isinstance(idxs, list):
        _fail("indexes must be list")
    seen_idx_names: set[str] = set()
    for idx in idxs:
        _validate_index(idx, schema, name, seen_idx_names)

    return name, cols, cons, idxs


def _render_table_prose(schema: str, table_name: str, cols: list[dict[str, Any]], cons: list[dict[str, Any]], idxs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(f"TABLE {schema}.{table_name}")
    for col in cols:
        parts = [f"COLUMN {col['name']} type={col['type']} nullable={'true' if col['nullable'] else 'false'}"]
        if col.get("byteLength") is not None:
            parts.append(f"byteLength={col['byteLength']}")
        if col.get("charLength") is not None:
            parts.append(f"charLength={col['charLength']}")
        if col.get("precision") is not None:
            parts.append(f"precision={col['precision']}")
        if col.get("scale") is not None:
            parts.append(f"scale={col['scale']}")
        if col.get("charSemantics") is not None:
            parts.append(f"charSemantics={col['charSemantics']}")
        lines.append(" ".join(parts))
    for con in cons:
        col_list = ",".join(con["columns"]) if con["columns"] else ""
        line = f"CONSTRAINT {con['name']} kind={con['kind']} columns=[{col_list}]"
        if con.get("references"):
            line += f" REFERENCES {con['references']}"
        if con.get("referenceColumns"):
            line += f" referenceColumns=[{','.join(con['referenceColumns'])}]"
        if con.get("deferrable") is not None:
            line += f" deferrable={con['deferrable']}"
        if con.get("deferred") is not None:
            line += f" deferred={con['deferred']}"
        if con.get("deleteRule") is not None:
            line += f" deleteRule={con['deleteRule']}"
        if con.get("status") is not None:
            line += f" status={con['status']}"
        if con.get("validated") is not None:
            line += f" validated={con['validated']}"
        lines.append(line)
    for idx in idxs:
        col_list = ",".join(idx["columns"]) if idx["columns"] else ""
        line = f"INDEX {idx['name']} kind={idx['kind']} columns=[{col_list}]"
        if idx.get("unique") is not None:
            line += f" unique={'true' if idx['unique'] else 'false'}"
        if idx.get("expressionOmitted") is not None:
            line += f" expressionOmitted={'true' if idx['expressionOmitted'] else 'false'}"
        if idx.get("status") is not None:
            line += f" status={idx['status']}"
        if idx.get("visibility") is not None:
            line += f" visibility={idx['visibility']}"
        lines.append(line)
    return "\n".join(lines)


def _load_oracle_metadata_snapshot_impl(root: Path, relative_directory: str) -> OracleMetadataSnapshot:
    rel = _validate_relative_directory(relative_directory)
    
    if not root.is_dir() or _is_symlink_or_reparse(root):
        _fail("root must be a real directory")

    current = root
    rel_components = rel.split("/") if "/" in rel else rel.split("\\")
    for comp in rel_components:
        current = current / comp
        if _is_symlink_or_reparse(current):
            _fail("symlink or reparse point detected")

    base = current
    if not base.is_dir() or _is_symlink_or_reparse(base):
        _fail("base directory invalid")

    resolved_root = root.resolve()
    resolved_base = base.resolve()
    try:
        resolved_base.relative_to(resolved_root)
    except ValueError:
        _fail("path escapes root")

    manifest_path = resolved_base / "manifest.json"
    metadata_path = resolved_base / "metadata.json"

    if not manifest_path.is_file() or _is_symlink_or_reparse(manifest_path):
        _fail("manifest.json missing or invalid")
    if not metadata_path.is_file() or _is_symlink_or_reparse(metadata_path):
        _fail("metadata.json missing or invalid")

    raw_manifest = _read_capped(manifest_path, 1 * 1024 * 1024)
    raw_metadata = _read_capped(metadata_path, 32 * 1024 * 1024)

    manifest_data = _load_json(raw_manifest, 1 * 1024 * 1024)
    metadata_data = _load_json(raw_metadata, 32 * 1024 * 1024)

    schema, database, expected_sha256, expected_counts = _validate_manifest(manifest_data)

    actual_sha256 = hashlib.sha256(raw_metadata).hexdigest()
    if actual_sha256 != expected_sha256:
        _fail("metadata hash mismatch")

    if not isinstance(metadata_data, dict):
        _fail("metadata must be object")
    meta_allowed = {"formatVersion", "applicationVersion", "schema", "database", "tables"}
    extra = set(metadata_data.keys()) - meta_allowed
    if extra:
        _fail("unknown metadata keys")
    missing = {"formatVersion", "applicationVersion", "schema", "database", "tables"} - set(metadata_data.keys())
    if missing:
        _fail("missing metadata keys")

    _format_version(metadata_data["formatVersion"])
    if metadata_data["applicationVersion"] != manifest_data["applicationVersion"]:
        _fail("applicationVersion mismatch")
    if metadata_data["schema"] != schema:
        _fail("schema mismatch")
    if metadata_data["database"] != database:
        _fail("database mismatch")

    tables = metadata_data["tables"]
    if not isinstance(tables, list):
        _fail("tables must be list")
    if len(tables) > 20000:
        _fail("too many tables")

    total_columns = 0
    total_constraints = 0
    total_indexes = 0
    seen_tables: set[str] = set()
    table_names: set[str] = set()
    table_columns_map: dict[str, set[str]] = {}
    valid_table_entries: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = []

    for tbl in tables:
        tname, tcols, tcons, tidxs = _validate_table(tbl, schema, seen_tables)
        table_names.add(tname)
        table_cols_set = {c["name"] for c in tcols}
        table_columns_map[tname] = table_cols_set
        total_columns += len(tcols)
        total_constraints += len(tcons)
        total_indexes += len(tidxs)
        if max(total_columns, total_constraints, total_indexes) > 500000:
            _fail("too many objects")
        valid_table_entries.append((tname, tcols, tcons, tidxs))

    if total_columns != expected_counts["columns"]:
        _fail("column count mismatch")
    if total_constraints != expected_counts["constraints"]:
        _fail("constraint count mismatch")
    if total_indexes != expected_counts["indexes"]:
        _fail("index count mismatch")
    if len(seen_tables) != expected_counts["tables"]:
        _fail("table count mismatch")

    for tname, tcols, tcons, tidxs in valid_table_entries:
        for con in tcons:
            if con["kind"] == "R":
                target_table = con.get("references")
                if target_table in table_names:
                    target_cols = table_columns_map[target_table]
                    for rc in con.get("referenceColumns", []):
                        if rc not in target_cols:
                            _fail("foreign key reference column not in target table")

    objects: list[OracleDdlObject] = []
    for tname, tcols, tcons, tidxs in valid_table_entries:
        text = _render_table_prose(schema, tname, tcols, tcons, tidxs)
        header = "Oracle metadata snapshot; structure only, not original DDL"
        full_object_text = f"{header}\n{text}"
        findings = list(scan_text(full_object_text, relative_path=f"oracle/{tname}.sql", skip_placeholder_lines=False))
        if findings:
            raise PolicyViolation("secret detected in rendered metadata")
        ddl_digest = digest_of_bytes(full_object_text.encode('utf-8'))
        objects.append(OracleDdlObject(
            owner=schema,
            object_name=tname,
            object_type="TABLE",
            status="SNAPSHOT",
            last_ddl_at="",
            ddl_digest=ddl_digest,
            _ddl=full_object_text,
        ))

    conn_digest = digest({
        "offline_metadata_source": rel,
        "manifest_digest": digest_of_bytes(raw_manifest),
        "metadata_digest": digest_of_bytes(raw_metadata),
    })
    db_digest = digest({"database": database, "schema": schema})

    return OracleMetadataSnapshot(
        schema_name=schema,
        connection_identity_digest=conn_digest,
        database_identity_digest=db_digest,
        objects=tuple(objects),
        excluded_secret_objects=0,
        unresolved_relations=(),
    )


def load_oracle_metadata_snapshot(root: Path, relative_directory: str) -> OracleMetadataSnapshot:
    if not isinstance(root, Path):
        raise ValidationFailed("invalid oracle metadata snapshot") from None
    try:
        return _load_oracle_metadata_snapshot_impl(root, relative_directory)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise ValidationFailed("invalid oracle metadata snapshot") from None


def oracle_snapshot_freshness(state: dict, root: Path | None) -> list[str]:
    if not isinstance(state, dict):
        return ["oracle-offline-snapshot-unavailable"]
    source_rel = state.get("oracle_snapshot_source")
    revision = state.get("oracle_snapshot_revision_digest")
    if source_rel is None and revision is None:
        return []
    if not isinstance(source_rel, str) or not isinstance(revision, str):
        return ["oracle-offline-snapshot-unavailable"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", revision):
        return ["oracle-offline-snapshot-unavailable"]
    if root is None:
        return ["oracle-offline-snapshot-unavailable"]
    try:
        snap = load_oracle_metadata_snapshot(root, source_rel)
    except (ValidationFailed, PolicyViolation, OSError):
        return ["oracle-offline-snapshot-unavailable"]
    if snap.revision_digest != revision:
        return ["oracle-offline-snapshot-stale"]
    return []
