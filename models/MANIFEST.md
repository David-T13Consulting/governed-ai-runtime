# Model manifest

The GGUF model files the runtime uses. The files themselves are not in git (too large);
they live in `~/Library/Application Support/dbrain/models/`. This list is enough to fetch
them again and check that a copy is the same file. Added for M1 (Q57, 6 Oct 2026).

Hashes were computed from the files on disk on 6 Oct 2026 (`shasum -a 256`). Each one
matches the pin recorded before use: C1–C3 in the T9 pre-download manifest
(`artifacts/t9-2026-10-06/manifest-pre-download.json`, git-ignored), B0 in
`experiments/t9_compare.py` (parked, not yet committed). Sources and licenses for C1–C3 come from that pre-download
manifest; B0's source and revision from the vault note `local-benchmark-2026-10-01.md`.

| ID | Model | Source (Hugging Face repo @ revision) | File | Bytes | SHA-256 | License |
|---|---|---|---|---|---|---|
| B0 | Qwen3-4B, Q4_K_M (MVP baseline) | [`Qwen/Qwen3-4B-GGUF`](https://huggingface.co/Qwen/Qwen3-4B-GGUF/tree/bc640142c66e1fdd12af0bd68f40445458f3869b) @ `bc640142c66e1fdd12af0bd68f40445458f3869b` | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` | apache-2.0 (model card at the pinned revision, checked 6 Oct) |
| C1 | Qwen3.5-4B, Q4_K_M (T9 candidate; Unsloth conversion, not an official Qwen release) | [`unsloth/Qwen3.5-4B-GGUF`](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/tree/e87f176479d0855a907a41277aca2f8ee7a09523) @ `e87f176479d0855a907a41277aca2f8ee7a09523` | `Qwen3.5-4B-Q4_K_M.gguf` | 2,740,937,888 | `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4` | apache-2.0 |
| C2 | Gemma 4 E4B-it, QAT q4_0 (T9 candidate) | [`google/gemma-4-E4B-it-qat-q4_0-gguf`](https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/tree/4b4a2c1d584be7264f87aac328a1bc739ce81b6c) @ `4b4a2c1d584be7264f87aac328a1bc739ce81b6c` | `gemma-4-E4B_q4_0-it.gguf` | 5,154,941,280 | `676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee` | apache-2.0 |
| C3 | Ministral 3 8B Instruct 2512, Q4_K_M (T9 candidate) | [`mistralai/Ministral-3-8B-Instruct-2512-GGUF`](https://huggingface.co/mistralai/Ministral-3-8B-Instruct-2512-GGUF/tree/0102285ad796bd99af90f58de616092e5630e970) @ `0102285ad796bd99af90f58de616092e5630e970` | `Ministral-3-8B-Instruct-2512-Q4_K_M.gguf` | 5,198,911,904 | `33e7a72cf5e6e2cfc2f2847075acc013d68bba023e35310cef86b5cf8fdca761` | apache-2.0 |

Local path for every file: `~/Library/Application Support/dbrain/models/<File>`.

To check a copy: `shasum -a 256 "<path>"` must print the SHA-256 above. Use a file only if
its size and hash match.
