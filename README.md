# TokenPaw

> 로컬 AI 에이전트의 토큰이 어디에서 새는지 펫이 알려주는 관제 도구입니다.

TokenPaw (AI Agent Token Observatory)는 Claude Code, OpenAI Codex 등 코딩 에이전트의 로컬 로그를 분석해 **모델 → 프로젝트 → 세션**의 흐름으로 provider 기록을 추적합니다. 프롬프트 탐색은 로그에서 연결이 확인되는 경우에만 제한적으로 제공합니다.

원문과 소스 코드는 기본적으로 외부로 전송하지 않으며, provider가 기록한 실제 사용량만 사용자 화면에 표시합니다. provider가 원인별 token 귀속을 제공하지 않는 경우 임의의 token 추정값이나 원인 단정을 만들지 않습니다.

로컬에 저장된 AI 코딩 에이전트 로그를 수집해 세션·턴 단위 사용량을 정규화하는 프로젝트입니다.

## Phase 1 status

- Claude Code JSONL adapter
- OpenAI Codex rollout JSONL adapter
- 실제 로그 구조 기반 usage 파싱
- SQLite 저장소와 idempotent event ingestion
- 파일별 byte offset 기반 incremental parsing
- 개인정보 보호 기본값: prompt/tool/file 전문 미저장
- CLI: `scan`, `status`, `sessions`, `alerts`, `dashboard`
- sanitized parser/collector/storage tests
- FastAPI REST API
- 로컬 데이터에 연결된 responsive dashboard
- Phase 3 heuristic alert engine with idempotent alerts
- Session diagnostic insights: token spikes, tool-output contribution, repeated context, avoidable-token estimate, and heuristic efficiency score

## 조사 결과

### Claude Code

`%USERPROFILE%/.claude/projects/**` 아래 프로젝트별 `.jsonl` 파일을 확인했습니다. 주요 top-level event type은 `user`, `assistant`, `attachment`, `file-history-snapshot`, `file-history-delta`, `custom-title`, `last-prompt`입니다.

assistant event의 `message.usage`에는 다음 필드가 있습니다.

`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `server_tool_use`, `iterations`, `speed` 등.

`message.content`에는 `text`, `thinking`, `tool_use`, `tool_result` 블록이 혼재합니다. `tool_use`에는 `name`, `input`, `id`가 있고, `tool_result`에는 `tool_use_id`, `content`, `is_error`가 있습니다.

### OpenAI Codex

`%USERPROFILE%/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`을 확인했습니다. envelope type은 `session_meta`, `turn_context`, `response_item`, `event_msg`, `world_state` 등입니다.

- session metadata: `session_meta.payload`의 `session_id`, `cwd`, `cli_version`, `model_provider`, `context_window`, `git`
- turn metadata: `turn_context.payload`의 `turn_id`, `cwd`, `model`, `summary`
- usage: `event_msg.payload.type=token_count`의 `info.last_token_usage` 및 `info.total_token_usage`
- usage fields: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`
- tools: `response_item.payload.type=function_call` / `function_call_output`

## Token semantics

provider raw usage는 `raw_usage_json`에 보존합니다.

- Claude: Anthropic usage의 `input_tokens`는 cache read와 분리된 fresh input으로 취급하고, `cache_creation_input_tokens`도 fresh에 포함합니다. `cache_read_input_tokens`는 cached input입니다.
- Codex: `fresh_input_tokens = max(input_tokens - cached_input_tokens, 0)`으로 계산하고 `cache_write_input_tokens`는 raw usage에 보존합니다.
- 실제 provider usage가 없는 이벤트의 token 값은 추정하지 않고 null로 둡니다. tool/file/MCP 이벤트의 크기도 token으로 변환하지 않습니다.

## Run

```powershell
python -m agent_token_monitor scan
python -m agent_token_monitor status
python -m agent_token_monitor sessions
python -m agent_token_monitor dashboard
```

`status`, `sessions`, and `alerts` use concise operator-friendly output by default. `status` includes today's total tokens, cache hit rate, Top Project, Top Session, and active alerts. Add `--json` for automation; use `--limit N` to bound session/alert rows. CLI JSON output is forced to UTF-8 on supported terminals so local Korean/Unicode metadata does not break automation on Windows.

An optional local YAML/JSON config can be supplied with `--config path\to\config.yaml` or `TOKEN_MONITOR_CONFIG`. It controls database/pricing paths, collector paths and enablement, polling interval, alert thresholds, and privacy storage flags. CLI arguments and `CLAUDE_LOG_PATH`/`CODEX_LOG_PATH` environment overrides take precedence over configured paths.

기본 DB는 `./data/token-monitor.db`이며, 기본 collector 경로는 현재 사용자 프로필의 `.claude/projects`와 `.codex/sessions`입니다.

`dashboard` 명령은 `http://127.0.0.1:8000`에서 대시보드를 시작합니다. API만 실행하려면 다음을 사용합니다.

```powershell
uvicorn agent_token_monitor.api:app --host 127.0.0.1 --port 8000
```

대시보드에서 사용할 수 있는 Phase 2 API는 다음과 같습니다.

`GET /api/overview`, `/api/agents`, `/api/projects`, `/api/sessions`, `/api/sessions/{id}`, `/api/sessions/{id}/turns`, `/api/sessions/{id}/timeline`, `/api/sessions/{id}/insights`, `/api/turns/{id}`, `/api/turns/{id}/impact`, `/api/alerts`, `/api/search?q=...`, `/api/usage/timeline`, `/api/usage/models`, `/api/usage/projects`. Overview, agent, project, session, timeline, model, and usage-project endpoints accept `start`/`end` filters; overview/timeline use `agent`, while agent/project/model/usage-project endpoints also accept provider/agent filters. Alerts accept `resolved`, `alert_type`, `severity`, `agent`, `start`, and `end`. Period aggregation uses turn timestamps and normalizes timezone representations before comparing them. The full dashboard's `Today / Last 7 days` and agent controls now reuse these filters for KPI, timeline, sessions, and alerts. Session timeline rows combine provider turns with normalized ContextItem/ToolCall metadata; side-event token amounts are returned as unavailable when the provider does not record them separately.

Provider billing records are not present in the local transcripts, so cost is reported as unavailable. A local pricing file may remain for offline experiments, but its result is not shown as user-facing usage or billing.

## Desktop companion

The first desktop shell is under [`desktop`](desktop). It is a Tauri 2 + React + TypeScript companion window: the idle state is a small transparent pet, clicking it opens a local search palette, and the tray provides access to scan and the full dashboard. The Python collector remains the local FastAPI sidecar, so no transcript or source content leaves the machine.

```powershell
cd desktop
npm install
npm run dev
```

개발 중에는 설치 파일을 반복해서 만들 필요가 없습니다. Windows에서는 `cd desktop; npm run dev:desktop`을 실행하면 개발용 API(`8766`)와 Tauri 펫을 함께 시작하며, React/CSS 변경은 hot reload로 즉시 반영됩니다. macOS에서는 `bash ../scripts/dev_desktop.sh`를 사용합니다. 개발용 DB는 `data/token-monitor-dev.db`로 분리되므로 이미 설치된 앱을 실행한 채로도 UI를 확인할 수 있습니다.

The browser shell expects a local API on port `8765`:

```powershell
$env:TOKEN_MONITOR_PORT = "8765"
agent-token-monitor dashboard --host 127.0.0.1 --port 8765
```

For a packaged build, install the Rust/Tauri prerequisites first, build the PyInstaller sidecar with `scripts/build_sidecar.ps1` on Windows or `bash scripts/build_sidecar.sh` on macOS, then run `npm run tauri:build` from `desktop`. The Tauri bundle is configured for Windows NSIS and macOS DMG. Release builds use an OS-specific application-data directory for SQLite rather than the installation directory.

The desktop popover starts as a small pet. Click to open the diagnostics panel; long-press and drag to move it. The panel is positioned above and to the left of the pet (clamped to the monitor work area) and drills down through `Agent → Project → Session → Prompt / Turn`. The first screen separately shows today’s per-agent usage, then the hierarchy shows provider-recorded input, cached, fresh, output, reasoning, and cache-hit values at each level for the selected `Today / 7 days / All time` period. It also includes an agent-stacked token-burn timeline with `Today / 7 days / All time` and `5m / 15m / 1h / 1d` controls; the hierarchy refreshes every 10 seconds while open. Session view includes a compact measured-turn input chart and observed activity counts. Turn detail shows `Input Before → Input After`, delta, fresh-context added, reasoning, and Tools/Files/MCP/Subagents counts. Effective input is `cached input + fresh input` when those provider fields exist; provider raw input remains visible as a separate value. Prompt usage is attached to the first measured provider call for that prompt. The desktop bundle stores masked user prompt text locally so prompt-level search works; tool output and file contents remain disabled. CLI/library users can keep prompt storage disabled unless `store_prompt_content` is enabled explicitly.

The full local dashboard also exposes synchronized filters for preset or custom date range, agent, provider, model, project, session, Git repository/branch, alert type, token range, and cost range. These filters are sent to the overview, timeline, session, and alert queries together so KPI values and drill-down rows describe the same slice. The session filter is applied by stable internal session id while displaying the agent, project, and human-readable session name when the provider exposes one. Codex names are read from the local `~/.codex/session_index.jsonl` (`thread_name`) without storing transcript content.

Turn persistent impact reports only the observed number of later turns containing the same content hash. Provider logs do not expose a complete context-to-token attribution graph, so repeated-event counts are shown without converting them into token impact or savings.

## Phase 3 alerts

`scan` 실행 후 다음 규칙을 평가합니다: `CONTEXT_SPIKE`, `CACHE_DROP`, `LARGE_TOOL_OUTPUT`, `LARGE_FILE_READ`, `REPEATED_FILE_READ`, `REPEATED_CONTEXT`, `SESSION_LOG_INGESTION`, `ABNORMAL_BURN`, `CONTEXT_PRESSURE`, `AGENT_FANOUT`.

alert는 `alert type + session + turn` hash를 id로 사용하므로 같은 scan을 반복해도 중복 생성되지 않습니다. 사용자에게 노출하는 alert는 provider usage로 직접 계산한 context spike, cache drop, abnormal burn, context pressure만 포함합니다. tool/file/context 크기 기반 alert는 생성하지 않습니다.

```powershell
python -m agent_token_monitor scan --db .\data\token-monitor.db --claude-path C:\Users\me\.claude\projects --codex-path C:\Users\me\.codex\sessions
```

## 정확도 한계

- provider 로그가 전체 context를 원인별 token으로 attribution하지 않으므로 파일·shell·MCP·sub-agent별 token 기여량은 표시하지 않습니다. 대신 실제 이벤트, 경로, 호출 횟수, content hash 반복 여부만 표시합니다.
- Claude의 tool result와 Codex function output은 보호 설정에 따라 크기와 hash를 저장할 수 있지만, 크기는 token 사용량으로 변환하지 않습니다.
- Codex token_count는 model call 단위이며 여러 call이 하나의 `turn_context.turn_id`에 속할 수 있어, 저장된 `external_turn_id`에 envelope ordinal을 추가해 call 단위로 보존합니다.

남은 고도화 항목은 provider가 노출하지 않는 prompt의 persistent-impact 정확도 향상과 실제 macOS 호스트에서의 native sidecar/DMG 설치 검증입니다. macOS 빌드 명령과 target-specific sidecar 생성 스크립트는 제공됩니다.
