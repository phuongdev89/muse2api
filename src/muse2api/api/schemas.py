"""OpenAI-compatible request schemas. Unknown fields are accepted and ignored so that
clients sending newer parameters keep working."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatMessage(_Lenient):
    role: str = Field(description="One of 'system', 'user', 'assistant'.")
    content: str | list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Plain text, or OpenAI vision content parts: "
            "[{\"type\": \"text\", \"text\": ...}, {\"type\": \"image_url\", "
            "\"image_url\": {\"url\": \"data:... | https://...\"}}]."
        ),
    )
    name: str | None = None


class ChatCompletionRequest(_Lenient):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    thread: str | None = Field(
        default=None,
        description=(
            "Continue an existing muse.ai conversation: pass the 'thread' URL returned "
            "by a previous completion. When set, history lives upstream and only the "
            "last user message is sent; omit it to start a new conversation."
        ),
    )
    # Accepted for compatibility; muse.ai does not expose sampling or tool controls.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None


class ImageGenerationRequest(_Lenient):
    prompt: str = Field(min_length=1)
    model: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = Field(
        default=None,
        description=(
            "Requested aspect ratio or output size. For reference-based edits, Muse is "
            "instructed to preserve the source image dimensions and aspect ratio; this "
            "field is a generation hint and does not resize the returned image."
        ),
    )
    response_format: Literal["url", "b64_json"] = "url"
    # Same values as OpenAI gpt-image-1. "transparent" cuts the subject out locally
    # and returns an RGBA PNG; "auto"/"opaque" leave the image as generated.
    background: Literal["auto", "opaque", "transparent"] | None = Field(
        default=None,
        description=(
            "'transparent' cuts the subject out locally and returns an RGBA PNG; "
            "'auto'/'opaque' keep the image as generated."
        ),
    )
    image: str | list[str] | None = Field(
        default=None, description="Reference image(s) as data URL, http(s) URL or base64 (max 4)")
    # A sync call can outlive a proxy's timeout (Cloudflare cuts at ~100 s with 524), so
    # "async": true returns a task to poll at GET /v1/images/generations/{task_id}.
    async_: bool = Field(
        default=False,
        alias="async",
        description=(
            "Run as a background task and return immediately (poll "
            "GET /v1/images/generations/{task_id}); useful when generation may "
            "outlive proxy timeouts (Cloudflare cuts at ~100 s with 524)."
        ),
    )
    user: str | None = None


class VideoCreateRequest(_Lenient):
    prompt: str = Field(min_length=1)
    model: str | None = None
    size: str | None = Field(default=None, description="Aspect ratio hint, e.g. '16:9' or '9:16'.")
    duration: int | None = Field(default=None, ge=1, le=60,
                                 description="Clip length in seconds (1-60).")
    seconds: int | None = Field(default=None, ge=1, le=60,
                                description="Alias of 'duration'; 'duration' wins when both are set.")
    image: str | None = Field(default=None, description="First frame as data URL or http(s) URL")
    input_reference: str | None = Field(default=None,
                                        description="Alias of 'image' (first frame reference).")


class AccountCreate(BaseModel):
    label: str = ""
    cookies: dict[str, str] | str | list[dict[str, Any]] = Field(
        description=(
            "Session cookies in any of three formats: a dict "
            '{"name": "value"}; a raw "Cookie" request header string "a=1; b=2"; '
            "or a cookie-export list (e.g. EditThisCookie) of "
            '[{"name": ..., "value": ..., "expirationDate": ...}, ...]. '
            "Export lists also carry expiry timestamps."
        ),
    )
    cookie_expires: dict[str, int] = Field(
        default_factory=dict,
        description="Optional cookie name -> unix expiry seconds override.",
    )
    enabled: bool = True


class AccountsImportRequest(BaseModel):
    """Bulk cookie import: every entry is validated like AccountCreate; entries that
    fail are reported individually without aborting the batch."""
    accounts: list[AccountCreate] = Field(min_length=1)


class AccountUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    cookies: dict[str, str] | None = Field(
        default=None,
        description=(
            "Replace the account's cookies (dict format only). Fresh cookies "
            "re-activate an account that was marked invalid."
        ),
    )
    cookie_expires: dict[str, int] | None = None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)
    key: str | None = Field(
        default=None, max_length=128,
        description="Custom key value; generated randomly when omitted.",
    )


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    revoked: bool | None = None
    key: str | None = Field(
        default=None, min_length=1, max_length=128,
        description="Replace the key value (clients must switch to the new value).",
    )


class ThreadRenameRequest(_Lenient):
    title: str = Field(
        min_length=1, max_length=200,
        description="New display title for the muse.ai conversation.",
    )
