# muse2api API Reference

OpenAI-compatible gateway and proxy for muse.ai services (Chat, Images, Videos).

- **Interactive Swagger UI**: [http://localhost:18610/docs](http://localhost:18610/docs)
- **ReDoc Documentation**: [http://localhost:18610/redoc](http://localhost:18610/redoc)
- **OpenAPI Schema (JSON)**: [http://localhost:18610/openapi.json](http://localhost:18610/openapi.json)
- **Web Dashboard**: [http://localhost:18610/dashboard](http://localhost:18610/dashboard)
- **Playground**: [http://localhost:18610/playground](http://localhost:18610/playground)

---

## 1. Authentication

All `/v1/*` and `/admin/*` endpoints require authentication.

### Header format:
```http
Authorization: Bearer <API_KEY_OR_ADMIN_KEY>
```
Or:
```http
X-API-Key: <API_KEY_OR_ADMIN_KEY>
```

- **Admin Key (`MUSE2API_ADMIN_KEY`)**: Full access to all endpoints, including `/admin/*`.
- **Client API Keys (`/admin/keys`)**: Access to `/v1/*` endpoints.

### Account Routing (Optional)
Để chỉ định một tài khoản cụ thể trong pool (dùng khi test, debug):
```http
X-Account-ID: <ACCOUNT_ID>
```
Hoặc qua query string: `?account_id=<ACCOUNT_ID>`. Khi được chỉ định, hệ thống sẽ sử dụng trực tiếp tài khoản này mà không tự động failover sang tài khoản khác.

---

## 2. Models (`/v1/models`)

### List Available Models
```http
GET /v1/models
Authorization: Bearer <KEY>
```

**Response (200 OK):**
```json
{
  "object": "list",
  "data": [
    {"id": "muse-chat", "object": "model", "owned_by": "muse", "kind": "chat"},
    {"id": "muse-image", "object": "model", "owned_by": "muse", "kind": "image"},
    {"id": "muse-video", "object": "model", "owned_by": "muse", "kind": "video"}
  ]
}
```

---

## 3. Chat Completions (`/v1/chat/completions`)

OpenAI-compatible chat completion with streaming and vision support.

### Request Body:
```json
{
  "model": "muse-chat",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Xin chào, bạn có thể làm được gì?"}
  ],
  "stream": false
}
```

### Multimodal Vision Request:
```json
{
  "model": "muse-chat",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Mô tả bức ảnh này"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
      ]
    }
  ]
}
```

### Response (Non-stream, 200 OK):
```json
{
  "id": "chatcmpl-9b8417c4-069d-4e96-a192-f04bf37042a9",
  "object": "chat.completion",
  "created": 1792030000,
  "model": "muse-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Tôi có thể hỗ trợ bạn trò chuyện, tạo ảnh và tạo video!"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 18,
    "total_tokens": 30
  }
}
```

### Streaming SSE (`stream: true`):
Chunks formatted as:
```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{"content":"Xin"}}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{"content":" chào"}}]}

data: [DONE]
```

---

## 4. Image Generation (`/v1/images/generations`)

Tạo ảnh bằng văn bản hoặc chỉnh sửa ảnh (img2img).

### Request Body:
```json
{
  "model": "muse-image",
  "prompt": "Cyberpunk street with neon reflections, highly detailed 8k",
  "size": "1024x1024",
  "n": 1,
  "response_format": "url",
  "background": "auto"
}
```

**Parameters:**
- `model`: `"muse-image"` hoặc `"dall-e-3"` (alias).
- `prompt`: Văn bản mô tả ảnh cần tạo.
- `size`: `"1024x1024"` (1:1), `"1280x720"` (16:9), `"720x1280"` (9:16).
- Với ảnh tham chiếu/chỉnh sửa, Muse được yêu cầu giữ nguyên chiều rộng, chiều cao pixel và tỉ lệ khung hình của ảnh nguồn được nêu trong prompt. API trả nguyên byte ảnh Muse tạo ra, không resize sau khi nhận; việc tuân thủ kích thước nguồn phụ thuộc Muse.
- `background`: `"auto"` (mặc định), `"opaque"` hoặc `"transparent"`. Playground gửi `auto` khi không bật xóa phông.
- `image`: URL hoặc Base64 Data URL ảnh tham chiếu (tối đa 4 ảnh).
- `async`: `false` (mặc định - chờ kết quả) hoặc `true` (trả về Task ID để poll).

### Response (200 OK):
```json
{
  "created": 1792030000,
  "data": [
    {
      "url": "http://localhost:18610/v1/media/img_abc123.png",
      "revised_prompt": "Cyberpunk street with neon reflections, highly detailed 8k"
    }
  ]
}
```

---

## 5. Image Editing Tools

Các endpoint multipart này dùng `muse-image` mặc định và trả về cùng cấu trúc `data[]` như Image Generation. Có thể truyền thêm `model`, `size` và `response_format` (`url` hoặc `b64_json`). Prompt là hướng dẫn bổ sung tùy chọn; thay bối cảnh dùng ảnh thứ hai làm ảnh tham chiếu nền. Tất cả yêu cầu ảnh đều yêu cầu Muse trả đúng một ảnh 2K và không tạo collage. Với các thao tác chỉnh sửa, API cũng yêu cầu giữ nguyên kích thước pixel (chiều rộng và chiều cao) cùng tỉ lệ ảnh nguồn; đầu ra được trả trực tiếp, không resize phía gateway. Đây là chỉ dẫn cho Muse, kích thước thực tế vẫn tùy kết quả upstream.

Quy tắc kích thước theo thao tác:
- **Sửa ảnh**, **tách trang phục**, **xóa vật thể**: giữ chiều rộng, chiều cao và tỉ lệ ảnh gốc.
- **Thử trang phục**, **thêm phụ kiện**, **thay bối cảnh**: giữ chiều rộng, chiều cao và tỉ lệ của ảnh 1 (ảnh `character` hoặc `image`).

### Tách trang phục (`POST /v1/images/outfits/extract`)

Trường `image` là ảnh người mặc. API yêu cầu chỉ giữ lại quần áo trên nền trắng, loại bỏ người, phụ kiện và giày dép.

```bash
curl -X POST http://localhost:18610/v1/images/outfits/extract \
  -H "Authorization: Bearer $MUSE2API_API_KEY" \
  -F "image=@person.jpg" \
  -F "model=muse-image" \
  -F "size=1024x1024"
```

### Thử trang phục (`POST /v1/images/outfits/try-on`)

Gửi hai ảnh riêng biệt theo đúng thứ tự ngữ nghĩa: `character` là ảnh nhân vật, `outfit` là ảnh trang phục.

```bash
curl -X POST http://localhost:18610/v1/images/outfits/try-on \
  -H "Authorization: Bearer $MUSE2API_API_KEY" \
  -F "character=@character.jpg" \
  -F "outfit=@outfit.png" \
  -F "model=muse-image" \
  -F "size=1024x1024"
```

### Xóa vật thể (`POST /v1/images/objects/remove`)

Trường `image` là ảnh gốc. Có thể gửi `mask` là ảnh PNG đen/trắng (vùng trắng là vật thể cần xóa, vùng đen là vùng cần giữ) và/hoặc `prompt` mô tả vật thể. Muse dùng mask như ảnh tham chiếu để hướng dẫn tạo ảnh; endpoint không thực hiện inpainting chính xác từng pixel.

```bash
curl -X POST http://localhost:18610/v1/images/objects/remove \
  -H "Authorization: Bearer $MUSE2API_API_KEY" \
  -F "image=@photo.jpg" \
  -F "mask=@remove-mask.png" \
  -F "prompt=Remove the marked person and reconstruct the wall"
```

### Thêm phụ kiện (`POST /v1/images/accessories/add`)

Gửi `image` là ảnh gốc, `accessory` là ảnh tham chiếu phụ kiện; `prompt` bổ sung là tùy chọn.

```bash
curl -X POST http://localhost:18610/v1/images/accessories/add \
  -H "Authorization: Bearer $MUSE2API_API_KEY" \
  -F "image=@person.jpg" \
  -F "accessory=@glasses.png" \
  -F "prompt=Place the glasses on the person's face"
```

### Thay bối cảnh (`POST /v1/images/background/replace`)

Gửi đúng hai ảnh: `image` là ảnh 1 cần sửa; `background_image` là ảnh 2 làm bối cảnh tham chiếu. Có thể thêm `prompt` để hướng dẫn chi tiết, nhưng không bắt buộc.

```bash
curl -X POST http://localhost:18610/v1/images/background/replace \
  -H "Authorization: Bearer $MUSE2API_API_KEY" \
  -F "image=@person.jpg" \
  -F "background_image=@beach.jpg" \
  -F "prompt=Preserve the person and match the lighting"
```

### Sửa ảnh (`POST /v1/images/edits`)

Endpoint tương thích multipart kiểu OpenAI: gửi `image` một hoặc nhiều lần (hoặc dùng `image[]`) và bắt buộc có `prompt`. Muse dùng ảnh làm tham chiếu để tạo ảnh mới; `mask` không được hỗ trợ.

```bash
curl -X POST http://localhost:18610/v1/images/edits \
  -H "Authorization: Bearer $MUSE2API_API_KEY" \
  -F "image=@photo.jpg" \
  -F "prompt=Change the shirt to blue while preserving the person"
```

## 6. Video Generation (`/v1/videos`)

Tạo video text-to-video hoặc image-to-video. Vì tạo video mất thời gian, endpoint chạy dưới dạng async task.

### Tạo Task Video:
```http
POST /v1/videos
Content-Type: application/json
Authorization: Bearer <KEY>
```
```json
{
  "model": "muse-video",
  "prompt": "Drone shot flying over ocean waves at golden hour",
  "duration": 5,
  "image": "data:image/jpeg;base64,..."
}
```

**Parameters:**
- `model`: `"muse-video"`.
- `prompt`: Mô tả nội dung video.
- `duration`: `5` hoặc `10` (giây).
- `image` / `input_reference`: Ảnh đầu tiên làm khung mẫu (First-frame / Image-to-video).

### Response khởi tạo (200 OK):
```json
{
  "id": "task_e6a8276f",
  "object": "video",
  "status": "pending",
  "progress": 0.0,
  "created_at": 1792030000,
  "model": "muse-video",
  "result": null,
  "error": null
}
```

### Kiểm tra trạng thái Task:
```http
GET /v1/videos/{task_id}
Authorization: Bearer <KEY>
```

**Response khi hoàn thành (200 OK):**
```json
{
  "id": "task_e6a8276f",
  "object": "video",
  "status": "completed",
  "progress": 1.0,
  "created_at": 1792030000,
  "model": "muse-video",
  "result": {
    "url": "http://localhost:18610/v1/media/vid_xyz789.mp4",
    "mime": "video/mp4",
    "width": 1280,
    "height": 720
  },
  "error": null
}
```

---

## 6. Truy xuất File Media (`/v1/media/{name}`)

Truy xuất hình ảnh hoặc video đã được tạo:
```http
GET /v1/media/img_abc123.png
GET /v1/media/vid_xyz789.mp4
```

---

## 7. Quản trị hệ thống (`/admin/*`)

Yêu cầu `MUSE2API_ADMIN_KEY`.

| Method | Endpoint | Mô tả |
| :--- | :--- | :--- |
| `GET` | `/admin/status` | Tình trạng driver, pool accounts, Chromium status |
| `GET` | `/admin/accounts` | Danh sách tài khoản muse.ai trong pool |
| `POST` | `/admin/accounts` | Thêm tài khoản mới bằng cookies |
| `PATCH` | `/admin/accounts/{id}` | Cập nhật nhãn, trạng thái bật/tắt, hoặc cookies mới |
| `DELETE`| `/admin/accounts/{id}` | Xóa tài khoản khỏi pool |
| `GET` | `/admin/accounts/{id}/quota` | Kiểm tra quota tài khoản trực tiếp từ upstream |
| `GET` | `/admin/keys` | Danh sách các API Key đã cấp |
| `POST` | `/admin/keys` | Tạo mới API Key |
| `PATCH` | `/admin/keys/{id}` | Thu hồi hoặc đổi tên API Key |
| `DELETE`| `/admin/keys/{id}` | Xóa vĩnh viễn API Key |
| `GET` | `/admin/requests` | Lịch sử request (hỗ trợ phân trang và lọc) |
| `GET` | `/admin/stats` | Thống kê throughput, latency, lỗi theo khung giờ |
