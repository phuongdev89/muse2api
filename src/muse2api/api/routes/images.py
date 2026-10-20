from __future__ import annotations

import base64
import mimetypes
import time
from dataclasses import replace

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...drivers.base import ImageRequest, InputImage, MediaResult
from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ...services.tasks import Task
from ..deps import get_services, public_base, require_api_key
from ..schemas import ImageGenerationRequest

router = APIRouter(tags=["images"], dependencies=[Depends(require_api_key)])

# Reference images attached to one muse.ai message.
MAX_REFERENCE_IMAGES = 4

# Asking muse.ai for a "transparent background" makes it reply with text only, so
# ask for an easy-to-cut plain background and remove it locally instead.
_CUTOUT_HINT = "Isolated subject on a plain, uncluttered studio background."
_OUTFIT_EXTRACT_PROMPT = (
    "Extract only the clothing garments worn by the person in the reference image. "
    "Remove the person completely, including face, hair, skin, hands, legs, and body; "
    "remove all accessories, jewelry, bags, hats, belts, socks, and all shoes/footwear. "
    "Show only the clothing garments, preserving their original design, colors, fabric, "
    "pattern, and details, arranged neatly as a clean product flat-lay on a pure white "
    "background. No person, mannequin, accessories, footwear, text, or scene."
)
_OUTFIT_TRY_ON_PROMPT = (
    "Dress the person in the first reference image with the clothing shown in the second "
    "reference image. The first image is the person; the second image is the outfit. "
    "Preserve the person's identity, face, hair, body shape, pose, and the original scene "
    "as closely as possible. Transfer only the garments, matching their colors, cut, "
    "fabric, pattern, and details. Fit the clothes naturally to the person's body. "
    "Do not copy a person, mannequin, background, or accessories from the outfit reference."
)
_REMOVE_OBJECT_PROMPT = (
    "Remove the object or objects marked in the second reference image, which is a black "
    "and white selection mask. White areas indicate what to remove; black areas indicate "
    "what to preserve. The first reference image is the original photo. If there is no mask, "
    "remove only the object described in the additional instruction. Reconstruct the "
    "background naturally and preserve all unselected content."
)
_ADD_ACCESSORY_PROMPT = (
    "The first reference image is the original photo and the second reference image shows "
    "the accessory to add. Add only that accessory naturally to the person or scene in the "
    "original photo. Preserve identity, pose, clothing, lighting, and all unrelated details. "
    "Do not replace the original photo or copy the second image's background."
)
_REPLACE_BACKGROUND_PROMPT = (
    "The first reference image is the original photo to edit. The second reference image "
    "shows the desired background scene. Replace only the background of the first image "
    "with the scene from the second image. Preserve the foreground person or objects, "
    "their identity, shape, pose, clothing, and details. Adapt perspective, lighting, and "
    "shadows so the composite looks natural. Do not copy foreground subjects from the "
    "background reference."
)


def _image_item(r: MediaResult, fmt: str, svc: Services, base: str) -> dict:
    item: dict = {"revised_prompt": r.revised_prompt}
    if fmt == "b64_json":
        item["b64_json"] = base64.b64encode(r.data).decode()
    else:
        name = svc.media.save(r.data, r.mime, prefix="img")
        item["url"] = f"{base}/v1/media/{name}"
    return item


def _task_view(task: Task) -> dict:
    return {
        "id": task.id,
        "object": "image.task",
        "status": task.status.value,
        "progress": task.progress,
        "created_at": int(task.created_at),
        "model": task.request.get("model"),
        "result": task.result,
        "error": task.error,
    }


async def _generate(body: ImageGenerationRequest, refs: list[InputImage],
                    request: Request, svc: Services) -> dict:
    request.state.model = body.model or resolve_model(body.model, "image").id
    if len(refs) > MAX_REFERENCE_IMAGES:
        raise InvalidRequest(f"at most {MAX_REFERENCE_IMAGES} reference images are supported")
    account_id = request.headers.get("x-account-id") or request.query_params.get("account_id")
    if body.async_:
        return _submit(body, refs, request, svc, account_id=account_id)
    return await _run(body, refs, public_base(request), svc, account_id=account_id)


def _submit(body: ImageGenerationRequest, refs: list[InputImage], request: Request,
            svc: Services, account_id: str | None = None) -> dict:
    """Run the generation as a background task; results are stored as media URLs."""
    spec = resolve_model(body.model, "image")
    body = body.model_copy(update={"response_format": "url"})  # keep base64 out of tasks.json
    base = public_base(request)

    async def runner(progress) -> dict:
        return await _run(body, refs, base, svc, account_id=account_id)

    meta = {"model": spec.id, "prompt": body.prompt, "size": body.size, "n": body.n,
            "references": len(refs)}
    task = svc.tasks.submit("image", meta, runner)
    request.state.task_id = task.id
    return _task_view(task)


async def _run(body: ImageGenerationRequest, refs: list[InputImage], base: str,
               svc: Services, account_id: str | None = None) -> dict:
    spec = resolve_model(body.model, "image")
    transparent = body.background == "transparent"
    prompt = f"{body.prompt.rstrip('. ')}. {_CUTOUT_HINT}" if transparent else body.prompt
    req = ImageRequest(prompt=prompt, model=spec.id, size=body.size, n=body.n,
                       reference_images=refs, timeout=svc.settings.image_timeout)
    results = (await svc.gateway.generate_image(req, account_id=account_id)
               if account_id else await svc.gateway.generate_image(req))
    if transparent:
        # After the gateway call, so the account is not held during matting.
        results = [replace(r, data=await svc.matting.remove(r.data), mime="image/png")
                   for r in results]
    return {"created": int(time.time()),
            "data": [_image_item(r, body.response_format, svc, base) for r in results]}


@router.post("/v1/images/generations")
async def generate_images(body: ImageGenerationRequest, request: Request,
                          svc: Services = Depends(get_services)) -> dict:
    refs = body.image if isinstance(body.image, list) else [body.image] if body.image else []
    loaded = [InputImage(*(await load_image_ref(ref))) for ref in refs]
    return await _generate(body, loaded, request, svc)


@router.post("/v1/images/edits")
async def edit_images(request: Request, svc: Services = Depends(get_services)) -> dict:
    """OpenAI-style multipart edit: ``image`` / ``image[]`` files plus a prompt.

    muse.ai has no inpainting, so the images act as references for a new image
    and ``mask`` is rejected rather than silently ignored.
    """
    form = await request.form()
    if form.get("mask") is not None:
        raise InvalidRequest("mask is not supported; muse.ai cannot inpaint a region")
    files = [f for key in ("image", "image[]") for f in form.getlist(key)
             if isinstance(f, UploadFile)]
    if not files:
        raise InvalidRequest("at least one image file is required (field 'image' or 'image[]')")
    fields = {k: v for k, v in form.items() if isinstance(v, str) and k not in ("image", "image[]")}
    try:
        body = ImageGenerationRequest(**fields)
    except ValidationError as exc:
        raise InvalidRequest(exc.errors()[0].get("msg", "invalid request")) from exc
    refs = []
    for f in files:
        mime = f.content_type or ""
        if not mime.startswith("image/"):
            mime = mimetypes.guess_type(f.filename or "")[0] or "image/png"
        refs.append(InputImage(data=await f.read(), mime=mime))
    return await _generate(body, refs, request, svc)


async def _read_image_upload(upload: UploadFile) -> InputImage:
    mime = upload.content_type or ""
    if not mime.startswith("image/"):
        mime = mimetypes.guess_type(upload.filename or "")[0] or "image/png"
    return InputImage(data=await upload.read(), mime=mime)


async def _image_action_request(request: Request, prompt: str, required: tuple[str, ...],
                                svc: Services, optional: tuple[str, ...] = (),
                                prompt_required: bool = False,
                                prompt_or_optional: tuple[str, ...] = ()) -> dict:
    form = await request.form()
    uploads: dict[str, UploadFile] = {}
    for field in required:
        value = form.get(field)
        if not isinstance(value, UploadFile):
            raise InvalidRequest(f"image file is required (field '{field}')")
        uploads[field] = value
    for field in optional:
        value = form.get(field)
        if value is not None:
            if not isinstance(value, UploadFile):
                raise InvalidRequest(f"'{field}' must be an image file")
            uploads[field] = value
    fields = {k: v for k, v in form.items()
              if isinstance(v, str) and k not in (*required, *optional)}
    extra_prompt = str(form.get("prompt") or "").strip()
    if prompt_required and not extra_prompt:
        raise InvalidRequest("prompt is required")
    if prompt_or_optional and not extra_prompt and not any(
        field in uploads for field in prompt_or_optional
    ):
        raise InvalidRequest("provide a prompt or a marked mask")
    fields["prompt"] = f"{prompt} Additional instruction: {extra_prompt}" if extra_prompt else prompt
    try:
        body = ImageGenerationRequest(**fields)
    except ValidationError as exc:
        raise InvalidRequest(exc.errors()[0].get("msg", "invalid request")) from exc
    refs = [await _read_image_upload(uploads[field])
            for field in (*required, *optional) if field in uploads]
    return await _generate(body, refs, request, svc)


@router.post("/v1/images/outfits/extract")
async def extract_outfit(request: Request, svc: Services = Depends(get_services)) -> dict:
    """Return only the clothing from ``image`` on a plain white background."""
    return await _image_action_request(request, _OUTFIT_EXTRACT_PROMPT, ("image",), svc)


@router.post("/v1/images/outfits/try-on")
async def try_on_outfit(request: Request, svc: Services = Depends(get_services)) -> dict:
    """Apply ``outfit`` to the person in ``character``."""
    return await _image_action_request(request, _OUTFIT_TRY_ON_PROMPT,
                                       ("character", "outfit"), svc)


@router.post("/v1/images/objects/remove")
async def remove_objects(request: Request, svc: Services = Depends(get_services)) -> dict:
    """Remove an object using an optional black/white mask and/or text instruction."""
    return await _image_action_request(request, _REMOVE_OBJECT_PROMPT,
                                       ("image",), svc, optional=("mask",),
                                       prompt_or_optional=("mask",))


@router.post("/v1/images/accessories/add")
async def add_accessory(request: Request, svc: Services = Depends(get_services)) -> dict:
    """Add the accessory in ``accessory`` to the original ``image``."""
    return await _image_action_request(request, _ADD_ACCESSORY_PROMPT,
                                       ("image", "accessory"), svc)


@router.post("/v1/images/background/replace")
async def replace_background(request: Request, svc: Services = Depends(get_services)) -> dict:
    """Replace the background in ``image`` using ``background_image`` as reference."""
    return await _image_action_request(request, _REPLACE_BACKGROUND_PROMPT,
                                       ("image", "background_image"), svc)


@router.get("/v1/images/generations/{task_id}")
async def get_image_task(task_id: str, request: Request,
                         svc: Services = Depends(get_services)) -> dict:
    task = svc.tasks.get(task_id)
    if task is None or task.kind != "image":
        raise NotFound(f"image task '{task_id}' not found")
    request.state.model = task.request.get("model")
    return _task_view(task)
