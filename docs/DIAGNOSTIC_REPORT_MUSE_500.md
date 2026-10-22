# Báo cáo Chẩn đoán Lỗi 500/504 khi Request Muse2API

**Mã công việc:** `muse-500-20261022`  
**Người thực hiện:** `@Phuong_Claude_Bot`  
**Đối tượng báo cáo:** `@Phuong_Codex_Bot`  
**Ngày:** 2026-10-22  

---

## 1. Môi trường & Runtime
- **Thư mục mã nguồn hoạt động:** `D:\Python\muse2api-v2` (thư mục `D:\Python\muse2api` trống).
- **Môi trường chạy:** Docker container (`muse2api:latest` / Python 3.12/3.13) chạy trên Docker bridge gateway `172.20.0.1`, port `18610`.
- **Driver sử dụng:** `BrowserDriver` (headless Chromium điều khiển qua CDP port `19210`). Driver `http` chưa hoàn thiện (ném 501).
- **Commit mới nhất:** `fd69b7a` (2026-10-20 23:01:56) bổ sung `_wait_for_submission`.

---

## 2. Bằng chứng thực tế từ `data/requests.db`
Cơ sở dữ liệu lưu 116 requests. Phân tích các requests thành công vs lỗi:

1. **Thời gian xử lý thực tế khi thành công:**
   - Request chat (id 95): latency **109,446 ms (~109.5s)** -> HTTP 200.
   - Request chat (id 27): latency **59,361 ms (~59.4s)** -> HTTP 200.
   - Image try-on (id 116): latency **112,053 ms (~112s)** -> HTTP 200.
   - Image extract (id 115): latency **86,856 ms (~86.8s)** -> HTTP 200.
   - Image edit (id 105): latency **82,322 ms (~82.3s)** -> HTTP 200.

2. **Các requests lỗi 504 / 500:**
   - Request chat id 93: latency **47,909 ms (~48s)** -> HTTP 504: `no first token from assistant`.
   - Request chat id 68: latency **51,912 ms (~52s)** -> HTTP 504: `no first token from assistant`.
   - Request chat id 36: latency **47,418 ms (~47s)** -> HTTP 504: `no first token from assistant`.
   - Request chat id 28-31: latency **93,000 ms (~93s)** -> HTTP 504: `no first token from assistant`.

---

## 3. Phân tích nguyên nhân gốc rễ (Root Cause Analysis)

### Điểm 1: Khâu xác nhận gửi `_wait_for_submission` (Commit fd69b7a)
- Trong `driver.py`:
  ```python
  deadline = time.monotonic() + min(10.0, self.settings.first_token_timeout)
  ```
  Hàm giới hạn tối đa chỉ **10 giây**.
- `_send` đã gõ prompt và kích hoạt `CLICK_SEND` (hoặc Enter). Prompt **đã gửi tới Muse**.
- Nếu web client Muse mất >10 giây để hiển thị nút stop hoặc tăng `userCount`, `_wait_for_submission` ném `UpstreamTimeout("muse.ai did not accept the submitted message")`.
- Hậu quả: API trả về 504/500, trong khi Muse web đã nhận và đang sinh kết quả bình thường.

### Điểm 2: Giới hạn `first_token_timeout` quá ngắn (mặc định 45s)
- Khi Muse tải nặng hoặc suy nghĩ phức tạp, thời gian để xuất hiện token đầu tiên thực tế là 50s - 110s.
- Driver ngắt tại 45s và ném `UpstreamTimeout("no first token from assistant")`.

### Điểm 3: Thiếu Keepalive SSE trước token đầu tiên (`stream_heartbeat_interval = 0`)
- Trong `api/routes/chat.py`:
  Mặc định `stream_heartbeat_interval: float = 0.0`.
  Khi gọi streaming, code thực hiện `first = await anext(stream)` **trước khi gửi HTTP 200 headers**.
- Toàn bộ kết nối HTTP bị treo im lặng 45s - 110s. Các Reverse Proxy trung gian (Nginx mặc định 60s, Cloudflare 100s, browser fetch) tự ngắt kết nối và trả về 504 Gateway Timeout hoặc 502/500.

### Điểm 4: Endpoints hình ảnh/video chạy hoàn toàn đồng bộ (Blocking HTTP POST)
- Các endpoint `/v1/images/*` từ Playground và REST chạy đồng bộ, giữ socket mở suốt 80s - 120s. Rất dễ dính timeout từ hạ tầng proxy.

### Điểm 5: Nguy cơ Retry trùng lặp (Duplicate Submission)
- `UpstreamTimeout` kế thừa từ `UpstreamError` có cờ `retryable: bool = True`.
- Khi xảy ra timeout nhưng prompt đã được gửi lên Muse, `Gateway` nếu thấy pool còn account khác sẽ tự động retry sang account mới, dẫn đến gửi trùng lặp cùng một câu lệnh sang nhiều account.

---

## 4. Kế hoạch đề xuất khắc phục (Proposal)

1. **Ngăn chặn Retry trùng:**
   - Trong `errors.py`, đặt `UpstreamTimeout.retryable = False` (hoặc đánh dấu cờ `submitted = True` trong request khi đã qua `_send` để `Gateway` không retry nếu đã submit).
2. **Nâng thời gian chờ submission:**
   - Nâng deadline của `_wait_for_submission` từ `min(10.0, ...)` lên tối thiểu 30.0s - 45.0s, kết hợp kiểm tra trạng thái DOM linh hoạt hơn.
3. **Cấu hình lại Timeout mặc định:**
   - Nâng `first_token_timeout` mặc định lên 120s.
   - Nâng `page_ready_timeout` lên 60s.
4. **Bật SSE Keepalive mặc định:**
   - Đặt `stream_heartbeat_interval` mặc định là `5.0` giây trong `config.py` để gửi `: keepalive\n\n` ngay lập tức, giữ sống kết nối qua Nginx/Cloudflare/Browser.
5. **Bộ kiểm thử (Test Plan):**
   - Viết unit test giả lập upstream phản hồi chậm sau 60s để kiểm tra keepalive SSE.
   - Viết test đảm bảo `Gateway` không retry sang account khác khi gặp `UpstreamTimeout`.
