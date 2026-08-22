"""Provider adapter'in cevrimdisi endpoint ve response sozlesmeleri."""

from __future__ import annotations

import urllib.request
from email.message import Message

import pytest

from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.provider_adapter import (
    EnvironmentEndpointResolver,
    MultipartProviderCall,
    UrllibJsonProviderTransport,
    UrllibMultipartProviderTransport,
    build_multipart_body,
    openai_chat_payload,
    openai_chat_text,
    openai_embedding_payload,
    openai_embeddings,
    openai_guardrail_labels,
    openai_guardrail_payload,
    openai_rerank_payload,
    openai_rerank_scores,
    openai_transcript,
    openai_transcription_body,
    openai_vision_objects,
    openai_vision_payload,
    reviewed_endpoint_digest,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.security import SecretValue

pytestmark = pytest.mark.unit


def test_endpoint_resolver_accepts_https_without_exposing_value() -> None:
    resolver = EnvironmentEndpointResolver(
        {("model-endpoint:1", "embeddings"): "MODEL_EMBEDDING_URL"},
        {"MODEL_EMBEDDING_URL": "https://models.example.test/v1/embeddings"},
    )
    assert resolver.resolve("model-endpoint:1", "embeddings").endswith("/v1/embeddings")
    assert "models.example.test" not in repr(resolver)


@pytest.mark.parametrize(
    "url",
    (
        "http://models.example.test/v1/embeddings",
        "ftp://localhost/v1/embeddings",
        "https://user:password@models.example.test/v1/embeddings",
        "https://models.example.test/v1/embeddings?token=x",
    ),
)
def test_endpoint_resolver_rejects_unsafe_urls(url: str) -> None:
    resolver = EnvironmentEndpointResolver({("endpoint", "op"): "URL"}, {"URL": url})
    with pytest.raises(ValidationFailed):
        resolver.resolve("endpoint", "op")


def test_endpoint_resolver_allows_loopback_http() -> None:
    resolver = EnvironmentEndpointResolver(
        {("endpoint", "op"): "URL"}, {"URL": "http://127.0.0.1:8000/v1/test"}
    )
    assert resolver.resolve("endpoint", "op").startswith("http://127.0.0.1")


def test_reviewed_endpoint_digest_binds_scheme_host_port_and_exact_path() -> None:
    first = reviewed_endpoint_digest(
        "https://models.example.test/v1/embeddings", path_hint="/v1/embeddings"
    )
    assert first.startswith("sha256:")
    assert first != reviewed_endpoint_digest(
        "https://models.example.test:8443/v1/embeddings",
        path_hint="/v1/embeddings",
    )
    with pytest.raises(ValidationFailed, match="path_hint"):
        reviewed_endpoint_digest("https://models.example.test/v1/other", path_hint="/v1/embeddings")


def test_missing_endpoint_value_is_sanitized() -> None:
    resolver = EnvironmentEndpointResolver({("endpoint", "op"): "PRIVATE_URL"}, {})
    with pytest.raises(ProbeUnavailable, match="degeri bulunamadi") as caught:
        resolver.resolve("endpoint", "op")
    assert "PRIVATE_URL" not in str(caught.value)


def test_urllib_transport_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpener:
        def open(self, request: object, timeout: float) -> object:
            captured["request"] = request
            captured["timeout"] = timeout
            raise urllib.error.HTTPError("redacted", 302, "redirect", Message(), None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    credential = SecretValue("super-secret-token")
    with pytest.raises(ProbeUnavailable, match="HTTP status 302") as caught:
        UrllibJsonProviderTransport().post_json(
            "https://models.example.test/v1/chat/completions",
            {"model": "test"},
            credential,
        )
    assert "super-secret-token" not in str(caught.value)
    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert request.get_header("Authorization") == "Bearer super-secret-token"


def test_openai_chat_parser_supports_text_parts() -> None:
    response = {"choices": [{"message": {"content": [{"type": "text", "text": "merhaba"}]}}]}
    assert openai_chat_text(response) == "merhaba"


def test_openai_embedding_parser_restores_index_order() -> None:
    response = {
        "data": [
            {"index": 1, "embedding": [3, 4]},
            {"index": 0, "embedding": [1, 2]},
        ]
    }
    assert openai_embeddings(response) == ((1.0, 2.0), (3.0, 4.0))


def test_transcription_and_rerank_parsers() -> None:
    assert openai_transcript({"text": "ornek ses"}) == "ornek ses"
    assert openai_rerank_scores(
        {"results": [{"index": 1, "relevance_score": 0.2}, {"index": 0, "score": 0.9}]}
    ) == (0.9, 0.2)


@pytest.mark.parametrize(
    ("parser", "response"),
    (
        (openai_chat_text, {}),
        (openai_embeddings, {"data": []}),
        (openai_transcript, {"text": ""}),
        (openai_rerank_scores, {"results": []}),
    ),
)
def test_response_parsers_fail_closed(parser: object, response: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        parser(response)  # type: ignore[operator]


def test_modality_request_builders_have_exact_shapes() -> None:
    chat = openai_chat_payload("chat-model", "merhaba")
    assert chat["temperature"] == 0
    assert chat["messages"][1] == {"role": "user", "content": "merhaba"}
    assert openai_embedding_payload("embed-model", ("a", "b"))["input"] == ["a", "b"]
    assert openai_rerank_payload("rerank-model", "q", ("a", "b"))["documents"] == [
        "a",
        "b",
    ]
    guard = openai_guardrail_payload("guard-model", ("guvenli", "zararli"))
    assert guard["temperature"] == 0
    assert "labels" in guard["messages"][0]["content"]


def test_vision_payload_uses_inline_image_without_repr_guard() -> None:
    payload = openai_vision_payload(
        "vl-model", "nesneleri ver", b"\x89PNG\r\n", media_type="image/png"
    )
    url = payload["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    with pytest.raises(ValidationFailed):
        openai_vision_payload("vl", "x", b"x", media_type="image/svg+xml")


def test_guardrail_and_vision_parsers_require_exact_json() -> None:
    guard = {"choices": [{"message": {"content": '{"labels":["safe","unsafe"]}'}}]}
    assert openai_guardrail_labels(guard, expected_count=2) == (False, True)
    vision = {"choices": [{"message": {"content": '{"objects":["kedi","masa"]}'}}]}
    assert openai_vision_objects(vision) == ("kedi", "masa")
    with pytest.raises(ValidationFailed):
        openai_guardrail_labels({"choices": [{"message": {"content": "safe"}}]}, expected_count=1)


def test_whisper_multipart_is_deterministic_and_content_is_hidden_from_repr() -> None:
    first = openai_transcription_body(
        "whisper-model",
        b"RIFF-private-audio-bytes",
        filename="fixture.wav",
        media_type="audio/wav",
        language="tr",
    )
    second = openai_transcription_body(
        "whisper-model",
        b"RIFF-private-audio-bytes",
        filename="fixture.wav",
        media_type="audio/wav",
        language="tr",
    )
    assert first == second
    assert first.content_type.startswith("multipart/form-data; boundary=zekam-")
    assert b'filename="fixture.wav"' in first.body
    assert "private-audio-bytes" not in repr(first)
    call = MultipartProviderCall("model:1", "endpoint:1", "audio", "request-1", first)
    assert "private-audio-bytes" not in repr(call)


@pytest.mark.parametrize(
    ("filename", "media_type"),
    (("../fixture.wav", "audio/wav"), ("fixture.wav", "application/octet-stream")),
)
def test_whisper_multipart_rejects_unsafe_metadata(filename: str, media_type: str) -> None:
    with pytest.raises(ValidationFailed):
        openai_transcription_body("whisper", b"audio", filename=filename, media_type=media_type)


def test_generic_multipart_rejects_header_injection() -> None:
    with pytest.raises(ValidationFailed):
        build_multipart_body(
            fields={"model\r\nInjected": "x"},
            file_field="file",
            filename="audio.wav",
            file_content_type="audio/wav",
            content=b"audio",
        )


def test_multipart_transport_reuses_redirect_deny_and_sanitizes_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOpener:
        def open(self, request: object, timeout: float) -> object:
            del request, timeout
            raise urllib.error.HTTPError("redacted", 307, "redirect", Message(), None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    body = openai_transcription_body(
        "whisper", b"audio", filename="audio.wav", media_type="audio/wav"
    )
    with pytest.raises(ProbeUnavailable, match="HTTP status 307") as caught:
        UrllibMultipartProviderTransport().post_multipart(
            "https://models.example.test/v1/audio/transcriptions",
            body,
            SecretValue("never-render-this-secret"),
        )
    assert "never-render-this-secret" not in str(caught.value)
