# Kế Hoạch & Phân Tích Lỗi Timeout 500/504 Trong Muse2API

**Mã việc:** `TIMEOUT-500-PLAN`  
**Thực hiện:** `@Phuong_Claude_Bot`  
**Gửi tới:** `@Phuong_Codex_Bot`  
**Ngày:** 2026-10-22  

---

## 1. Dữ kiện thực tế vs Giả thuyết

### Dữ kiện xác thực (từ `data/requests.db` và mã nguồn):
- **Endpoint bị lỗi:** Chủ yếu `/v1/chat/completions` (các request 504: id 28, 29, 30, 31, 36, 68, 93) và `/v1/images/*`.
- **Độ trễ khi lỗi:** 
  - Đơn lẻ: 47s - 52s (gần ngưỡng `first_token_timeout` cũ = 45s).
  - Có retry failover: ~93s (2 lần x 45s trước khi dừng).
- **Độ trễ khi thành công:** Chat mất 59s - 109s; Image try-on/edit mất 82s - 112s.
- **Tầng gây lỗi:**
  1. `driver.py:500`: `_wait_for_submission` dùng `min(10.0, self.settings.first_token_timeout)`. Dù cấu hình timeout 300s thì hardcap 10s vẫn ném `UpstreamTimeout("muse.ai did not accept the submitted message")`.
  2. `driver.py:523-524`: `first_token_timeout` ngắt sau khi prompt đã gửi.
  3. `api/routes/chat.py:147`: Streaming mặc định (`stream_heartbeat_interval = 0`) gọi `await anext(stream)` trước khi gửi HTTP 200 headers. Reverse proxy (Cloudflare/Nginx/browser fetch) ngắt kết nối khi socket im lặng >60s/100s, sinh lỗi 504/502/500 tới client.

### Giả thuyết cần kiểm chứng:
- Anh Phương thấy "500": do Reverse Proxy (Cloudflare/Nginx) map HTTP 504 hoặc drop stream thành 500/502, hoặc do client fetch trong Playground hiển thị generic error `res.statusText`.

---

## 2. Rà soát Timeout, Exception & Retry trùng

- **Timeout hiện tại:**
  - `.env`: `CHAT_TIMEOUT=300`, `FIRST_TOKEN_TIMEOUT=300`, `PAGE_READY_TIMEOUT=180`.
  - Nhưng trong code `driver.py:500`, hàm `_wait_for_submission` bị kẹp cứng `min(10.0, ...)`.
- **Nguy cơ Retry trùng lặp:**
  - `errors.py:64`: `UpstreamError.retryable: bool = True`.
  - `errors.py:80`: `class UpstreamTimeout(UpstreamError)` thừa kế `retryable = True`.
  - `gateway.py:51-72`: Khi dính `UpstreamTimeout` trước khi có token đầu tiên (`emitted == False`), Gateway failover sang account khác và submit lại cùng một prompt. Vì prompt đầu tiên thực chất đã gửi lên web Muse, upstream sẽ sinh trùng lặp tác vụ.

---

## 3. Đề xuất phương án sửa

1. **Nguyên nhân gốc (Timeout):**
   - Bỏ hardcap 10s tại `driver.py:500`, nâng deadline chờ submit lên cấu hình linh hoạt (tối thiểu 30s - 45s).
   - Nâng `stream_heartbeat_interval` mặc định từ `0.0` lên `5.0` giây (`config.py` và `.env`), gửi `: keepalive\n\n` ngay khi mở stream để chống proxy ngắt kết nối.
2. **Mã lỗi trả về:**
   - Trả đúng chuẩn OpenAI: `504 Gateway Timeout` kèm JSON `{error: {message: ..., type: "upstream_error", code: "upstream_timeout"}}`.
3. **Chống Retry trùng:**
   - Đặt `UpstreamTimeout.retryable = False` trong `errors.py:80`.
   - Trong `ChatRequest`/`Gateway`: đánh dấu cờ `submitted = True` ngay sau khi `_send` hoàn tất. Nếu đã submit thì tuyệt đối không retry sang account khác.

---

## 4. File cần sửa, Rủi ro & Kế hoạch kiểm thử

### File cần sửa:
- `src/muse2api/drivers/browser/driver.py`: Bỏ hardcap 10s trong `_wait_for_submission`, tối ưu bộ chọn DOM xác nhận gửi tin.
- `src/muse2api/errors.py`: Đặt `UpstreamTimeout.retryable = False`.
- `src/muse2api/services/gateway.py`: Kiểm tra cờ `submitted` để chặn failover sau khi prompt đã bắn lên upstream.
- `src/muse2api/config.py` & `.env.example`: Đặt `stream_heartbeat_interval = 5.0`.

### Rủi ro:
- Nếu upstream thực sự chết/đơ vĩnh viễn, tăng timeout sẽ làm client phải chờ lâu hơn trước khi nhận lỗi. (Khắc phục bằng SSE heartbeat và client cancellation signal).

### Kế hoạch kiểm thử:
- Unit test mock `chat_stream` trả token sau 15s để kiểm tra `_wait_for_submission` không bị timeout sớm.
- Unit test `Gateway` ném `UpstreamTimeout` để xác nhận không có cuộc gọi sang account thay thế.
- Test SSE heartbeat phát ra `: keepalive` trước token đầu tiên.
