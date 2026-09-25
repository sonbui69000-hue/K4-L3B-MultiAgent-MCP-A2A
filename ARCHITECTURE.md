# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý một case được thực hiện trong `solve_case()` bằng state-machine async
thuần Python. Public contract trong `contracts/schemas/` là nguồn chân lý: output
chỉ chứa field được khai báo trong `l3b-output-v2.schema.json`, trace chỉ chứa
field được khai báo trong `trace-event-v1.schema.json`, và MCP response phải là
envelope hợp lệ theo `mcp-evidence-response-v1.schema.json`.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Case description, candidate identifiers | Resolve order/customer identity; rank and reject candidates; establish related orders | Tool discovery; customer/order lookup only | `entity_resolution`, `customer_context`; handoff to coordinator |
| Coordinator | Case + entity result | Own case state, assign specialists, enforce case scope and query budget, assemble final output | No broad investigation; may call only a specifically assigned fallback lookup | Specialist tasks; handoff to conflict resolver and verifier |
| Order/product | Resolved order/item IDs | Confirm order, item, seller and product facts | Order/item/product/seller tools | Affected entities and order claims |
| Shipment | Resolved order/item IDs | Reconstruct shipment timeline and classify seller vs logistics delay | Shipment/order tools | `shipment_analysis` and shipment evidence refs |
| Payment/refund | Resolved order/payment refs | Reconcile captures, installments, refunds and refundable amount | Payment/refund/order tools | `payment_analysis`, financial lines and evidence refs |
| Policy | Normalized claim and relevant facts | Apply policy source to eligibility and recommended action | Policy tool only | `policy_decided` trace event; policy evidence refs |
| Conflict resolver | Specialist results and source metadata | Detect conflicting fields, apply source precedence, preserve unresolved conflicts | No new broad search; targeted authoritative lookup only when necessary | `data_conflicts`, selected source and decision code |
| Verifier | Candidate final output + all refs | Validate schema, entity scope, arithmetic, claim linkage, action consistency and trace linkage | No new calls by default; one targeted recheck only for a deterministic validation failure | `verification_completed`; approved final output |

Least privilege is logical rather than a server-side MCP ACL: every actor receives
an allow-list of domains/tools from the coordinator, and a tool call must include
the current `case_id`. Tool discovery happens once per run; an actor may not guess
tool names or query another case.

## 3. Entity resolution và A2A protocol

Candidate được xếp hạng theo các tín hiệu quan sát được trong case và MCP:
exact identifier match trước, sau đó là customer/order/item/product consistency,
shipment/payment consistency và cuối cùng là textual hints. Candidate bị reject
nếu mâu thuẫn với một fact authoritative hoặc không đạt ngưỡng confidence. Chỉ
đánh dấu `resolved` khi có một kết quả duy nhất đủ mạnh; nếu có nhiều candidate
gần nhau thì giữ `ambiguous`, không tự đoán.

Handoff nội bộ dùng envelope in-memory sau đây; envelope này không được đưa vào
output JSON vì không phải public contract:

```text
{
  case_id, correlation_id, sender, receiver, task_type,
  input_refs, status, deadline, attempt
}
```

`case_id` là correlation key bắt buộc cho mọi message và MCP call. `input_refs`
chỉ tham chiếu dữ liệu/evidence thuộc cùng case; `attempt` bắt đầu từ 1 và tăng
cho retry. Handoff chỉ xảy ra khi actor trước đã tạo kết quả có thể kiểm tra.
Mỗi task có deadline ngắn hơn timeout toàn case; coordinator ghi nhận trạng thái
thất bại và chuyển sang fallback hợp lệ thay vì chờ vô hạn. Không cho phép
handoff quay lại actor đã hoàn tất cùng một task, và mọi task có `task_id` nội
bộ để ngăn vòng lặp/duplicate execution.

Trace chỉ ghi observable lifecycle (`task_assigned`, `handoff`, kết quả tool được
tiêu thụ, verification), không ghi prompt, chain-of-thought hay nội dung suy luận
riêng.

## 4. Evidence và conflict lifecycle

`EvidenceGateway.call()` luôn gửi `case_id`, nhận MCP envelope và validate envelope
trước khi specialist được sử dụng dữ liệu. Chỉ lưu `evidence_ref` do gateway trả
về; không sửa, tự tạo hoặc suy ra ref từ hash. Khi specialist thực sự dùng một
response, trace emit `tool_result_consumed` với `tool_name` và ref tương ứng.

Evidence được giữ trong context của đúng case, deduplicate theo `evidence_ref`,
và cache theo `(case_id, tool_name, normalized arguments)` trong thời gian xử lý
case. Cache không dùng chung giữa các case hoặc các run. Output chỉ đưa các ref
liên quan vào `evidence_refs` và các claim cụ thể; ref không liên quan không được
đưa vào để tránh penalty precision.

Conflict resolver gom các giá trị khác nhau theo field, giữ danh sách source,
chọn source theo thứ tự: policy/authoritative service, transaction record,
shipment/order record, rồi mới đến lower-confidence context. Nếu không thể phân
giải, `selected_source` là `null`, `resolution_code` nêu trạng thái unresolved,
và output hạ confidence hoặc dùng trạng thái `needs_investigation`. Không biến
missing evidence thành dữ liệu phỏng đoán.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 2 retries tối đa, exponential backoff ngắn | Dùng evidence đã cache; nếu không có thì đánh dấu thiếu bằng chứng | `tool_result_consumed` với decision code `mcp_timeout` nếu có thể; verifier giữ `insufficient_evidence` |
| Entity not found/ambiguous | Không retry cùng query quá 1 lần | Giữ `not_found`/`ambiguous`, không bịa order ID | `handoff` hoặc `verification_completed` với `entity_unresolved` |
| Source conflict | 1 targeted authoritative lookup tối đa | Ghi conflict và `selected_source: null` nếu vẫn unresolved | `policy_decided`/`verification_completed` với `source_conflict` |
| Invalid specialist result | Không retry nếu lỗi schema do logic; tối đa 1 rebuild từ facts đã có | Bỏ kết quả lỗi, coordinator/verifier quyết định trạng thái thiếu evidence | `handoff` với `invalid_result` |

MCP retry chỉ áp dụng cho timeout hoặc lỗi tạm thời, phải idempotent và không đổi
`case_id`; không retry lỗi input, quyền hoặc schema. Coordinator áp dụng query
budget theo case, ưu tiên exact lookup và fan-out giới hạn; không quét toàn bộ
customer/order history khi chưa cần. Tool discovery là một lần cho mỗi run, và
mọi call kể cả call không được đưa vào output đều được tính vào efficiency.

## 6. Verification invariants

Trước `case_finalized`, verifier phải kiểm tra:

- output validate thành công với `l3b-output-v2.schema.json`, không có extra field;
- `case_id` khớp input và mọi entity/evidence đều thuộc đúng case;
- entity resolution phản ánh candidate đã reject, không dùng ID không có nguồn;
- mọi evidence ref đều tồn tại, được trace tiêu thụ và liên kết với claim/analysis;
- timeline shipment nhất quán với verdict và seller/logistics responsibility;
- captured, refunded và refundable totals không âm và không mâu thuẫn với payment/refund evidence;
- source precedence và các conflict được biểu diễn đầy đủ;
- resolution actions phù hợp với primary issue, status và responsibility, không trùng nhau;
- mọi confidence nằm trong `[0, 1]` và giảm khi evidence thiếu/conflict unresolved;
- trace có thứ tự `case_received` → lifecycle events → `verification_completed` → `case_finalized`.

## 7. Reproducibility

Workflow dùng Python 3.11+, dependency constraints trong `pyproject.toml`, async
concurrency giới hạn theo case và không dùng random choice để quyết định nghiệp
vụ. Event IDs và timestamps do `TraceWriter` sinh khi chạy; API key chỉ đọc từ
`.env`, không ghi vào output, trace, architecture hoặc submission.

Lệnh chuẩn:

```bash
python -m pip install -e ".[dev]"
pytest -q
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Mỗi run xóa output/trace cũ theo cơ chế CLI trước khi chạy lại, giúp artifact
không bị trộn giữa các run. Submission chỉ gồm manifest, trace và outputs; source,
input, `.env` và secret không được đóng gói.
