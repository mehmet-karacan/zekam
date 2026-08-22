"""Yetkili ve redaksiyonlu model saglayici tasimasi.

Bu modul endpoint veya credential degeri saklamaz. Her gercek istek:

1. logical endpoint referansini process belleginde cozer,
2. Outbound Gate kaydini hazirlar ve exact authorization ile eslestirir,
3. Secret Broker icinde credential'i gecici olarak cozer,
4. authorization'i ag cagrisi oncesinde tek kullanimlik tuketir,
5. yalniz response digest'i ile terminal outbound kaydi uretir.

HTTP gerceklemesi Python stdlib kullanir. Testler enjekte edilen bellek ici
transport ile tamamen cevrimdisi calisir.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from zekam.application.environment import environment_value
from zekam.application.governance import EffectRequest, GovernanceService, ProviderGate
from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.secret_broker import SecretBroker
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.security import (
    Authorization,
    AuthorizationState,
    DataClassification,
    OutboundRequest,
    OutboundState,
    SecretRef,
    SecretValue,
)
from zekam.domain.work import EffectKind

DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def validated_provider_endpoint(value: str) -> str:
    """Yalniz HTTPS veya loopback HTTP endpoint kabul eder."""
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationFailed("Provider endpoint userinfo, query veya fragment tasiyamaz")
    if not parsed.hostname or not parsed.path.startswith("/"):
        raise ValidationFailed("Provider endpoint absolute URL olmali")
    if parsed.scheme == "https":
        return value
    if parsed.scheme != "http":
        raise ValidationFailed("Provider endpoint HTTPS olmali")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        loopback = address.is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if not loopback:
        raise ValidationFailed("HTTP yalniz loopback provider endpoint icin kabul edilir")
    return value


def reviewed_endpoint_digest(value: str, *, path_hint: str) -> str:
    """Endpoint'i ham URL'yi kalicilastirmadan exact reviewed kimlige baglar.

    Scheme, normalize host, effective port ve path birebir kapsanir. Query,
    fragment ve userinfo zaten ``validated_provider_endpoint`` tarafindan
    reddedilir. Boylece locator ayni logical ref altinda baska bir HTTPS hedefe
    cevrilirse invoke fail-closed olur.
    """

    target = validated_provider_endpoint(value)
    parsed = urlsplit(target)
    if parsed.path != path_hint:
        raise ValidationFailed("Provider endpoint path reviewed path_hint ile eslesmiyor")
    try:
        port = parsed.port
    except ValueError:
        raise ValidationFailed("Provider endpoint port gecersiz") from None
    effective_port = port if port is not None else (443 if parsed.scheme == "https" else 80)
    return digest(
        {
            "scheme": parsed.scheme.casefold(),
            "host": (parsed.hostname or "").casefold(),
            "port": effective_port,
            "path_hint": path_hint,
        }
    )


class EndpointResolver(Protocol):
    """Logical endpoint referansini ham degeri kaydetmeden cozer."""

    def resolve(self, endpoint_ref: str, operation: str) -> str: ...


@dataclass(frozen=True, slots=True)
class EnvironmentEndpointResolver:
    """Endpoint URL'lerini adlari verilen ortam degiskenlerinden okur."""

    locators: Mapping[tuple[str, str], str]
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False)

    def resolve(self, endpoint_ref: str, operation: str) -> str:
        locator = self.locators.get((endpoint_ref, operation))
        if locator is None:
            raise ProbeUnavailable("Provider endpoint locator tanimli degil")
        value = environment_value(self.environ, locator)
        if not value:
            raise ProbeUnavailable("Provider endpoint degeri bulunamadi")
        return validated_provider_endpoint(value)


class JsonProviderTransport(Protocol):
    """JSON provider cagrisi; testlerde bellek ici adapter enjekte edilir."""

    def post_json(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        credential: SecretValue,
    ) -> Mapping[str, Any]: ...


class MultipartProviderTransport(Protocol):
    """Multipart provider cagrisi; Whisper audio upload icin kullanilir."""

    def post_multipart(
        self,
        endpoint: str,
        payload: MultipartBody,
        credential: SecretValue,
    ) -> Mapping[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Authorization header'inin baska hosta yonlenmesini engeller."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


def _read_json_response(
    opener: Any,
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
) -> Mapping[str, Any]:
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ProbeUnavailable(f"Provider HTTP status {exc.code}") from None
    except (OSError, urllib.error.URLError, TimeoutError):
        raise ProbeUnavailable("Provider transport kullanilamiyor") from None
    if len(raw) > max_response_bytes:
        raise ProbeUnavailable("Provider response boyut sinirini asti")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProbeUnavailable("Provider response gecerli JSON degil") from None
    if not isinstance(document, dict):
        raise ProbeUnavailable("Provider response JSON object olmali")
    return document


@dataclass(frozen=True, slots=True)
class UrllibJsonProviderTransport:
    """Boyut sinirli, redirect reddeden stdlib JSON transport."""

    timeout_seconds: float = 30.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValidationFailed("Provider timeout pozitif olmali")
        if self.max_response_bytes < 1:
            raise ValidationFailed("Provider response limiti pozitif olmali")

    def post_json(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        credential: SecretValue,
    ) -> Mapping[str, Any]:
        target = validated_provider_endpoint(endpoint)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            target,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential.reveal()}",
                "Content-Type": "application/json",
            },
        )
        return _read_json_response(
            urllib.request.build_opener(_NoRedirect()),
            request,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )


@dataclass(frozen=True, slots=True)
class MultipartBody:
    """Body icerigini repr/log yolundan saklayan deterministik multipart payload."""

    content_type: str
    body: bytes = field(repr=False)

    @property
    def body_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.body).hexdigest()


def build_multipart_body(
    *,
    fields: Mapping[str, str],
    file_field: str,
    filename: str,
    file_content_type: str,
    content: bytes,
) -> MultipartBody:
    """CRLF/header injection reddeden, tekrar uretilebilir multipart body."""
    if not content:
        raise ValidationFailed("Multipart dosya icerigi bos olamaz")
    for value in (file_field, filename, file_content_type, *fields, *fields.values()):
        if not value or "\r" in value or "\n" in value or '"' in value:
            raise ValidationFailed("Multipart metadata gecersiz")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValidationFailed("Multipart filename yalniz basename olmali")
    boundary_seed = hashlib.sha256()
    boundary_seed.update(content)
    for key, value in sorted(fields.items()):
        boundary_seed.update(key.encode("utf-8"))
        boundary_seed.update(value.encode("utf-8"))
    boundary = "zekam-" + boundary_seed.hexdigest()[:32]
    chunks: list[bytes] = []
    for key, value in sorted(fields.items()):
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {file_content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return MultipartBody(f"multipart/form-data; boundary={boundary}", b"".join(chunks))


@dataclass(frozen=True, slots=True)
class UrllibMultipartProviderTransport:
    """Whisper upload icin boyut sinirli ve redirect reddeden stdlib transport."""

    timeout_seconds: float = 60.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_response_bytes < 1:
            raise ValidationFailed("Multipart provider transport limitleri pozitif olmali")

    def post_multipart(
        self,
        endpoint: str,
        payload: MultipartBody,
        credential: SecretValue,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            validated_provider_endpoint(endpoint),
            data=payload.body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential.reveal()}",
                "Content-Type": payload.content_type,
            },
        )
        return _read_json_response(
            urllib.request.build_opener(_NoRedirect()),
            request,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """Icerigi yalniz process belleginde kalan exact provider istegi."""

    provider_ref: str
    endpoint_ref: str
    operation: str
    request_identity: str
    payload: Mapping[str, Any] = field(repr=False)
    data_categories: tuple[DataClassification, ...] = (DataClassification.INTERNAL,)
    retention_assumption: str = "unknown"
    region: str = "unknown"
    endpoint_path_hint: str | None = None
    endpoint_binding_digest: str | None = None
    authorization_plan_digest: str | None = None
    authorization_resource: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("provider_ref", self.provider_ref),
            ("endpoint_ref", self.endpoint_ref),
            ("operation", self.operation),
            ("request_identity", self.request_identity),
        ):
            if not value.strip():
                raise ValidationFailed(f"Provider call {label} bos olamaz")

    @property
    def payload_digest(self) -> str:
        return digest(dict(self.payload))

    def effect_request(self, target: str) -> EffectRequest:
        action = "provider-call"
        if self.authorization_plan_digest is not None:
            action = "provider-contract-call-" + digest(
                {
                    "request_identity": self.request_identity,
                    "payload_digest": self.payload_digest,
                    "plan_digest": self.authorization_plan_digest,
                }
            ).removeprefix("sha256:")
        resources = (
            (target, self.authorization_resource)
            if self.authorization_plan_digest is not None
            and self.authorization_resource is not None
            else (target,)
        )
        return EffectRequest(
            action=action,
            effects=(EffectKind.PROVIDER_CALL,),
            resources=resources,
            data_classifications=self.data_categories,
            provider_refs=(self.provider_ref,),
            reversible=False,
            touches_external_system=True,
            required_capabilities=("provider.call",),
        )


@dataclass(frozen=True, slots=True)
class MultipartProviderCall:
    """Audio bytes'i yalniz process belleginde tasiyan exact provider istegi."""

    provider_ref: str
    endpoint_ref: str
    operation: str
    request_identity: str
    payload: MultipartBody = field(repr=False)
    data_categories: tuple[DataClassification, ...] = (DataClassification.INTERNAL,)
    retention_assumption: str = "unknown"
    region: str = "unknown"
    endpoint_path_hint: str | None = None
    endpoint_binding_digest: str | None = None
    authorization_plan_digest: str | None = None
    authorization_resource: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.provider_ref,
            self.endpoint_ref,
            self.operation,
            self.request_identity,
        ):
            if not value.strip():
                raise ValidationFailed("Multipart provider call kimligi bos olamaz")

    @property
    def payload_digest(self) -> str:
        return digest(
            {
                "body_digest": self.payload.body_digest,
                "content_type": self.payload.content_type,
            }
        )

    def effect_request(self, target: str) -> EffectRequest:
        action = "provider-call"
        if self.authorization_plan_digest is not None:
            action = "provider-contract-call-" + digest(
                {
                    "request_identity": self.request_identity,
                    "payload_digest": self.payload_digest,
                    "plan_digest": self.authorization_plan_digest,
                }
            ).removeprefix("sha256:")
        resources = (
            (target, self.authorization_resource)
            if self.authorization_plan_digest is not None
            and self.authorization_resource is not None
            else (target,)
        )
        return EffectRequest(
            action=action,
            effects=(EffectKind.PROVIDER_CALL,),
            resources=resources,
            data_classifications=self.data_categories,
            provider_refs=(self.provider_ref,),
            reversible=False,
            touches_external_system=True,
            required_capabilities=("provider.call",),
        )


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    """Ham yanit ve yalniz kanonik kayda girecek digest."""

    response: Mapping[str, Any] = field(repr=False)
    response_digest: str
    outbound_request_id: UUID
    authorization_id: UUID


@dataclass(frozen=True, slots=True)
class AuthorizedProviderClient:
    """Outbound Gate + Secret Broker + exact auth zincirli JSON client."""

    governance: GovernanceService
    endpoints: EndpointResolver
    broker: SecretBroker
    transport: JsonProviderTransport
    multipart_transport: MultipartProviderTransport | None = None

    @staticmethod
    def _require_exact_contract_binding(
        call: ProviderCall | MultipartProviderCall,
        *,
        endpoint: str,
        authorization: Authorization,
    ) -> None:
        fields = (
            call.endpoint_path_hint,
            call.endpoint_binding_digest,
            call.authorization_plan_digest,
            call.authorization_resource,
        )
        if all(item is None for item in fields):
            return
        if any(item is None for item in fields):
            raise PolicyViolation("Provider contract exact binding eksik")
        assert call.endpoint_path_hint is not None
        assert call.endpoint_binding_digest is not None
        assert call.authorization_plan_digest is not None
        if authorization.state is not AuthorizationState.ISSUED:
            raise PolicyViolation("Provider contract authorization issued olmali")
        if authorization.plan_digest != call.authorization_plan_digest:
            raise PolicyViolation("Provider contract authorization plan digest mismatch")
        current_endpoint_digest = reviewed_endpoint_digest(
            endpoint, path_hint=call.endpoint_path_hint
        )
        if current_endpoint_digest != call.endpoint_binding_digest:
            raise PolicyViolation("Provider endpoint reviewed binding drift")

    def invoke(
        self,
        call: ProviderCall,
        *,
        secret_ref: SecretRef,
        authorization: Authorization,
        consumed_by: str,
    ) -> ProviderCallResult:
        if secret_ref.provider != call.provider_ref:
            raise PolicyViolation("SecretRef provider ile outbound provider eslesmiyor")
        endpoint = self.endpoints.resolve(call.endpoint_ref, call.operation)
        self._require_exact_contract_binding(call, endpoint=endpoint, authorization=authorization)
        request = OutboundRequest.prepare(
            realm_id=self.governance.realm.id,
            provider_ref=call.provider_ref,
            endpoint_ref=call.endpoint_ref,
            operation=call.operation,
            payload_digest=call.payload_digest,
            request_identity=call.request_identity,
            data_categories=call.data_categories,
            retention_assumption=call.retention_assumption,
            region=call.region,
        )
        gate = ProviderGate(self.governance)
        prepared = gate.prepare(request)
        if prepared.state is OutboundState.DENIED:
            raise PolicyViolation(f"Outbound istek reddedildi: {prepared.denial_reason}")
        approved = gate.apply(prepared, authorization=authorization)
        effect = call.effect_request(approved.target)
        if authorization.effect_digest != effect.effect_digest:
            raise PolicyViolation("Provider contract authorization effect digest mismatch")
        try:
            with self.broker.resolve(
                secret_ref,
                operation=call.operation,
                authorization=authorization,
            ) as credential:
                self.governance.require_authorized(
                    effect,
                    authorization=authorization,
                    consumed_by=consumed_by,
                )
                response = self.transport.post_json(endpoint, call.payload, credential)
        except Exception as exc:
            denied = approved.with_state(
                OutboundState.DENIED,
                authorization_id=authorization.id,
                denial_reason=f"execution-failed:{type(exc).__name__}",
            )
            self.governance.outbound.record(denied)
            self.governance.audit.record(
                action="outbound.execute",
                subject_type="outbound",
                subject_id=str(request.id),
                decision="deny",
                reason=denied.denial_reason or "execution-failed",
                evidence=denied.as_dict(),
                actor_id=self.governance.actor_id,
                authorization_id=authorization.id,
            )
            raise
        response_digest = digest(dict(response))
        executed = approved.with_state(
            OutboundState.EXECUTED,
            authorization_id=authorization.id,
        )
        self.governance.outbound.record(executed)
        self.governance.audit.record(
            action="outbound.execute",
            subject_type="outbound",
            subject_id=str(request.id),
            decision="allow",
            reason="provider-response-digested",
            evidence=executed.as_dict() | {"response_digest": response_digest},
            actor_id=self.governance.actor_id,
            authorization_id=authorization.id,
        )
        return ProviderCallResult(
            response=response,
            response_digest=response_digest,
            outbound_request_id=request.id,
            authorization_id=authorization.id,
        )

    def invoke_multipart(
        self,
        call: MultipartProviderCall,
        *,
        secret_ref: SecretRef,
        authorization: Authorization,
        consumed_by: str,
    ) -> ProviderCallResult:
        """Multipart cagrida JSON yolu ile ayni gate/consume/receipt sirasi."""
        if self.multipart_transport is None:
            raise ProbeUnavailable("Multipart provider transport tanimli degil")
        if secret_ref.provider != call.provider_ref:
            raise PolicyViolation("SecretRef provider ile outbound provider eslesmiyor")
        endpoint = self.endpoints.resolve(call.endpoint_ref, call.operation)
        self._require_exact_contract_binding(call, endpoint=endpoint, authorization=authorization)
        request = OutboundRequest.prepare(
            realm_id=self.governance.realm.id,
            provider_ref=call.provider_ref,
            endpoint_ref=call.endpoint_ref,
            operation=call.operation,
            payload_digest=call.payload_digest,
            request_identity=call.request_identity,
            data_categories=call.data_categories,
            retention_assumption=call.retention_assumption,
            region=call.region,
        )
        gate = ProviderGate(self.governance)
        prepared = gate.prepare(request)
        if prepared.state is OutboundState.DENIED:
            raise PolicyViolation(f"Outbound istek reddedildi: {prepared.denial_reason}")
        approved = gate.apply(prepared, authorization=authorization)
        effect = call.effect_request(approved.target)
        if authorization.effect_digest != effect.effect_digest:
            raise PolicyViolation("Provider contract authorization effect digest mismatch")
        try:
            with self.broker.resolve(
                secret_ref,
                operation=call.operation,
                authorization=authorization,
            ) as credential:
                self.governance.require_authorized(
                    effect,
                    authorization=authorization,
                    consumed_by=consumed_by,
                )
                response = self.multipart_transport.post_multipart(
                    endpoint, call.payload, credential
                )
        except Exception as exc:
            denied = approved.with_state(
                OutboundState.DENIED,
                authorization_id=authorization.id,
                denial_reason=f"execution-failed:{type(exc).__name__}",
            )
            self.governance.outbound.record(denied)
            self.governance.audit.record(
                action="outbound.execute",
                subject_type="outbound",
                subject_id=str(request.id),
                decision="deny",
                reason=denied.denial_reason or "execution-failed",
                evidence=denied.as_dict(),
                actor_id=self.governance.actor_id,
                authorization_id=authorization.id,
            )
            raise
        response_digest = digest(dict(response))
        executed = approved.with_state(
            OutboundState.EXECUTED,
            authorization_id=authorization.id,
        )
        self.governance.outbound.record(executed)
        self.governance.audit.record(
            action="outbound.execute",
            subject_type="outbound",
            subject_id=str(request.id),
            decision="allow",
            reason="provider-response-digested",
            evidence=executed.as_dict() | {"response_digest": response_digest},
            actor_id=self.governance.actor_id,
            authorization_id=authorization.id,
        )
        return ProviderCallResult(
            response=response,
            response_digest=response_digest,
            outbound_request_id=request.id,
            authorization_id=authorization.id,
        )


def openai_chat_text(response: Mapping[str, Any]) -> str:
    """OpenAI-compatible chat response'tan metni fail-closed cikarir."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValidationFailed("Chat response choices[0] ister")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValidationFailed("Chat response message ister")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        joined = "".join(parts).strip()
        if joined:
            return joined
    raise ValidationFailed("Chat response metin icerigi tasimiyor")


def openai_embeddings(response: Mapping[str, Any]) -> tuple[tuple[float, ...], ...]:
    """OpenAI-compatible embedding response'u sirali vektorlere cevirir."""
    rows = response.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValidationFailed("Embedding response data ister")
    indexed: list[tuple[int, tuple[float, ...]]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
            raise ValidationFailed("Embedding response vector sekli gecersiz")
        try:
            vector = tuple(float(value) for value in row["embedding"])
            index = int(row.get("index", position))
        except (TypeError, ValueError):
            raise ValidationFailed("Embedding response sayisal olmali") from None
        if not vector:
            raise ValidationFailed("Embedding response bos vector tasiyamaz")
        indexed.append((index, vector))
    ordered = sorted(indexed, key=lambda item: item[0])
    if [item[0] for item in ordered] != list(range(len(ordered))):
        raise ValidationFailed("Embedding response indexleri tam sirali olmali")
    return tuple(item[1] for item in ordered)


def openai_transcript(response: Mapping[str, Any]) -> str:
    """OpenAI-compatible transcription JSON response'unu dogrular."""
    text = response.get("text", response.get("transcript"))
    if not isinstance(text, str) or not text.strip():
        raise ValidationFailed("Transcription response text ister")
    return text


def openai_rerank_scores(response: Mapping[str, Any]) -> tuple[float, ...]:
    """Yaygin OpenAI/vLLM rerank response sekillerini normalize eder."""
    rows = response.get("results", response.get("data"))
    if not isinstance(rows, list) or not rows:
        raise ValidationFailed("Rerank response results ister")
    scores: list[tuple[int, float]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValidationFailed("Rerank response satiri object olmali")
        raw_score = row.get("relevance_score", row.get("score"))
        if not isinstance(raw_score, (int, float, str)):
            raise ValidationFailed("Rerank response sayisal skor ister")
        try:
            scores.append((int(row.get("index", position)), float(raw_score)))
        except (TypeError, ValueError):
            raise ValidationFailed("Rerank response sayisal skor ister") from None
    return tuple(score for _, score in sorted(scores, key=lambda item: item[0]))


def openai_chat_payload(
    model: str,
    prompt: str,
    *,
    system: str = "Yaniti yalniz istenen formatta ver.",
    max_output_tokens: int | None = None,
    output_token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
) -> dict[str, Any]:
    """Chat/code endpoint'i icin deterministik OpenAI-compatible payload."""
    if not model.strip() or not prompt.strip() or not system.strip():
        raise ValidationFailed("Chat payload model/system/prompt ister")
    if max_output_tokens is not None and max_output_tokens < 1:
        raise ValidationFailed("Chat payload output token limiti pozitif olmali")
    if output_token_field not in {"max_tokens", "max_completion_tokens"}:
        raise ValidationFailed("Chat payload output token alan sozlesmesi gecersiz")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    if max_output_tokens is not None:
        payload[output_token_field] = max_output_tokens
    return payload


def openai_embedding_payload(model: str, inputs: tuple[str, ...]) -> dict[str, Any]:
    if not model.strip() or not inputs or any(not item.strip() for item in inputs):
        raise ValidationFailed("Embedding payload model ve bos olmayan input ister")
    return {"model": model, "input": list(inputs), "encoding_format": "float"}


def openai_rerank_payload(
    model: str,
    query: str,
    documents: tuple[str, ...],
) -> dict[str, Any]:
    if (
        not model.strip()
        or not query.strip()
        or not documents
        or any(not item.strip() for item in documents)
    ):
        raise ValidationFailed("Rerank payload model/query/documents ister")
    return {"model": model, "query": query, "documents": list(documents)}


def openai_guardrail_payload(model: str, samples: tuple[str, ...]) -> dict[str, Any]:
    """Guardrail modelinden exact sirada JSON labels ister."""
    if not samples or any(not item.strip() for item in samples):
        raise ValidationFailed("Guardrail payload bos olmayan samples ister")
    prompt = json.dumps(
        {"samples": list(samples), "output_schema": {"labels": ["safe|unsafe"]}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return openai_chat_payload(
        model,
        prompt,
        system=(
            "Her sample icin ayni sirada safe veya unsafe etiketi ver. "
            'Yalniz {"labels":[...]} JSON object dondur.'
        ),
    )


def openai_vision_payload(
    model: str,
    prompt: str,
    image: bytes,
    *,
    media_type: str,
) -> dict[str, Any]:
    """VL grounding icin inline image_url payload; yalniz process belleginde kalir."""
    if media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValidationFailed("VL image media type desteklenmiyor")
    if not image or not prompt.strip() or not model.strip():
        raise ValidationFailed("VL payload model/prompt/image ister")
    encoded = base64.b64encode(image).decode("ascii")
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    'Yalniz gorselde acikca bulunan nesneleri {"objects":[...]} '
                    "JSON object olarak ver."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    },
                ],
            },
        ],
        "temperature": 0,
    }


def openai_transcription_body(
    model: str,
    audio: bytes,
    *,
    filename: str,
    media_type: str,
    language: str | None = None,
) -> MultipartBody:
    """Whisper-compatible multipart transcription body uretir."""
    if media_type not in {
        "audio/flac",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
    }:
        raise ValidationFailed("Whisper audio media type desteklenmiyor")
    if not model.strip():
        raise ValidationFailed("Whisper model bos olamaz")
    fields = {"model": model, "response_format": "json"}
    if language is not None:
        if not re_full_language_tag(language):
            raise ValidationFailed("Whisper language etiketi gecersiz")
        fields["language"] = language
    return build_multipart_body(
        fields=fields,
        file_field="file",
        filename=filename,
        file_content_type=media_type,
        content=audio,
    )


def re_full_language_tag(value: str) -> bool:
    """Kisa BCP-47 alt kumesi; header/form injection'i de reddeder."""
    if not 2 <= len(value) <= 35:
        return False
    parts = value.split("-")
    return all(part.isascii() and part.isalnum() and 1 < len(part) <= 8 for part in parts)


def _chat_json_object(response: Mapping[str, Any]) -> Mapping[str, Any]:
    text = openai_chat_text(response)
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        raise ValidationFailed("Chat response exact JSON object olmali") from None
    if not isinstance(document, dict):
        raise ValidationFailed("Chat response exact JSON object olmali")
    return document


def openai_guardrail_labels(
    response: Mapping[str, Any], *, expected_count: int
) -> tuple[bool, ...]:
    """Guardrail JSON labels'i unsafe boolean dizisine cevirir."""
    labels = _chat_json_object(response).get("labels")
    if not isinstance(labels, list) or len(labels) != expected_count:
        raise ValidationFailed("Guardrail labels sayisi fixture ile eslesmeli")
    normalized: list[bool] = []
    for label in labels:
        if label is True or (isinstance(label, str) and label.casefold() == "unsafe"):
            normalized.append(True)
        elif label is False or (isinstance(label, str) and label.casefold() == "safe"):
            normalized.append(False)
        else:
            raise ValidationFailed("Guardrail label safe/unsafe olmali")
    return tuple(normalized)


def openai_vision_objects(response: Mapping[str, Any]) -> tuple[str, ...]:
    """VL JSON objects listesini normalize eder; serbest metin kabul etmez."""
    objects = _chat_json_object(response).get("objects")
    if not isinstance(objects, list) or any(not isinstance(item, str) for item in objects):
        raise ValidationFailed("VL response objects string listesi ister")
    normalized = tuple(item.strip() for item in objects if item.strip())
    if not normalized:
        raise ValidationFailed("VL response en az bir nesne ister")
    return normalized
