import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { LogicalSize, PhysicalPosition } from "@tauri-apps/api/dpi";
import { listen } from "@tauri-apps/api/event";
import { currentMonitor, getCurrentWindow, Window } from "@tauri-apps/api/window";
import { emitTo } from "@tauri-apps/api/event";
import gptSprite from "./assets/characters/reference/gpt-reference.png";
import geminiSprite from "./assets/characters/reference/gemini-reference.png";
import claudeSprite from "./assets/characters/reference/claude-reference.png";
import grokSprite from "./assets/characters/reference/grok-reference.png";
import { Language, t } from "./i18n";

type SearchResult = { result_id: string; session_id?: string | null; turn_id?: string | null; timestamp?: string | null; title?: string | null; match_kind?: string | null; agent_name?: string | null; project_name?: string | null; is_estimated?: number };
type Overview = { today?: { input_tokens?: number; total_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number; reasoning_tokens?: number; estimated_cost?: number | null; cache_hit_rate?: number | null; actual_turns?: number; estimated_turns?: number; usage_quality?: string; by_agent?: AgentRow[] }; active_sessions?: unknown[]; top_sessions?: SessionRow[]; top_projects?: ProjectRow[]; alerts?: { total?: number; critical?: number; warning?: number } };
type AgentRow = { id?: string; name: string; input_tokens?: number; total_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number; reasoning_tokens?: number; actual_turns?: number; estimated_turns?: number; usage_quality?: string; cache_hit_rate?: number | null; estimated_cost?: number | null };
type ModelRow = { provider?: string; agent_name?: string; model: string; turns?: number; input_tokens?: number; effective_input_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number; reasoning_tokens?: number; total_tokens?: number; actual_turns?: number; estimated_turns?: number; usage_quality?: string; cache_hit_rate?: number | null; estimated_cost?: number | null };
type ProjectRow = { id: string; project_name: string; project_path?: string; agent_name?: string; input_tokens?: number; total_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number; reasoning_tokens?: number; actual_turns?: number; estimated_turns?: number; usage_quality?: string; session_count?: number; estimated_cost?: number | null };
type PromptCoverage = { status?: "NO_DATA" | "UNAVAILABLE" | "PARTIAL" | "COMPLETE"; actual_usage_turns?: number; linked_prompt_turns?: number; unlinked_usage_turns?: number; prompt_content_stored_turns?: number; actual_tokens?: number; linked_actual_tokens?: number; unlinked_actual_tokens?: number; linked_token_coverage?: number | null; token_attribution_available?: boolean; note?: string };
type SessionRow = { id: string; external_session_id: string; session_name?: string | null; project_id?: string | null; agent_name?: string; provider?: string; project_name?: string; model?: string | null; total_input_tokens?: number; total_cached_input_tokens?: number; total_fresh_input_tokens?: number; total_output_tokens?: number; total_reasoning_tokens?: number; context_utilization?: number | null; actual_turns?: number; estimated_turns?: number; usage_quality?: string; cache_hit_rate?: number | null; estimated_cost?: number | null; status?: string; health_status?: string; prompt_coverage?: PromptCoverage };
type TurnRow = { id: string; timestamp?: string | null; user_prompt?: string | null; input_tokens?: number | null; cached_input_tokens?: number | null; fresh_input_tokens?: number | null; output_tokens?: number | null; reasoning_tokens?: number | null; estimated_cost?: number | null; input_before_tokens?: number | null; input_after_tokens?: number | null; input_delta_tokens?: number | null; fresh_context_added_tokens?: number | null; is_estimated?: number; prompt_linked?: boolean; prompt_content_stored?: boolean; prompt_linkage?: "linked" | "unlinked" };
type TurnDetail = TurnRow & { model?: string | null; likely_cause?: { kind?: string; source?: string; file_path?: string; tool_name?: string; target?: string; estimated_tokens?: number; confidence?: string } | null; context_items?: Array<{ type?: string; file_path?: string; tool_name?: string; token_count?: number; is_estimated?: number }>; tool_calls?: Array<{ tool_name?: string; estimated_tokens?: number; target?: string }>; tool_summary?: { tools_used?: number; files_read?: number; mcp_calls?: number; subagents?: number; estimated_tool_tokens?: number; is_estimated?: boolean } };
type SessionTimelineItem = { event_id: string; event_type?: string; context_type?: string; timestamp?: string | null; turn_id?: string; title?: string; effective_input_tokens?: number; cached_input_tokens?: number | null; fresh_input_tokens?: number | null; output_tokens?: number | null; reasoning_tokens?: number | null; token_count?: number | null; target?: string | null; is_estimated?: boolean; token_count_available?: boolean };
type SessionInsights = { effective_input_tokens?: number; cached_input_tokens?: number; fresh_input_tokens?: number; output_tokens?: number; estimated_cost?: number | null; cache_hit_rate?: number | null; tool_calls?: number; estimated_tool_tokens?: number; potentially_avoidable_tokens?: number; duplicate_file_paths?: number; alerts?: number; efficiency_score?: number; efficiency_score_is_heuristic?: boolean; spikes?: Array<{ turn_id: string; timestamp?: string | null; delta_tokens?: number }>; top_tools?: Array<{ tool_name?: string; target?: string; estimated_tokens?: number }>; tool_activity?: Array<{ tool_name?: string; calls?: number; targets?: string[] }>; context_attribution?: Array<{ type?: string; items?: number; token_count?: number; estimated_items?: number }>; repeated_contexts?: Array<{ type?: string; file_path?: string; tool_name?: string; injected_count?: number; unique_tokens?: number; potentially_avoidable_tokens?: number }>; prompt_coverage?: PromptCoverage };
type TurnImpact = { persistent_context_turns?: number; persistent_context_impact_tokens?: number; potentially_avoidable_tokens?: number; is_estimated?: boolean; confidence?: string | null; attribution_method?: string; attribution_note?: string; items?: Array<{ type?: string; file_path?: string; tool_name?: string; injected_count?: number; subsequent_turns?: number; cumulative_tokens?: number; confidence?: string; attribution_method?: string }> };
type TimelineAgent = { effective_input_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number };
type TimelineModel = TimelineAgent & { provider?: string; agent_name?: string; model?: string };
type TimelineRow = { bucket: string; effective_input_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number; agents?: Record<string, TimelineAgent>; models?: Record<string, TimelineModel> };
type AlertRow = { id: string; type?: string; severity?: string; title?: string; description?: string; timestamp?: string | null; agent_name?: string; project_name?: string; external_session_id?: string; session_id?: string; turn_id?: string | null; current_value?: number | null; baseline?: number | null; metric?: string | null; likely_cause?: { tool_name?: string; file_path?: string; estimated_tokens?: number; confidence?: string } | null };
type CharacterId = "gpt" | "gemini" | "claude" | "grok";
type PanelMode = "home" | "search" | "alerts" | "settings";
type PetPosition = "top-left" | "top-right" | "bottom-left" | "bottom-right" | "last";
type DesktopSettings = {
  version: 1;
  language: Language;
  characterId: CharacterId;
  scale: number;
  position: PetPosition;
  alwaysOnTop: boolean;
  showStatusBadge: boolean;
  showBubbles: boolean;
};
type Character = { id: CharacterId; name: string; provider: string; description: string; descriptionEn: string; sprite: string };

const CHARACTERS: Character[] = [
  { id: "gpt", name: "짚쨩", provider: "GPT / ChatGPT", description: "차분한 분석형", descriptionEn: "Calm analyst", sprite: gptSprite },
  { id: "gemini", name: "제미니", provider: "Gemini", description: "밝은 탐색형", descriptionEn: "Bright explorer", sprite: geminiSprite },
  { id: "claude", name: "클로드", provider: "Claude", description: "신중한 기록형", descriptionEn: "Careful archivist", sprite: claudeSprite },
  { id: "grok", name: "그록", provider: "Grok", description: "빠른 경고형", descriptionEn: "Fast sentinel", sprite: grokSprite },
];
const CHARACTER_STORAGE_KEY = "selected-character";
const SETTINGS_STORAGE_KEY = "desktop-settings";
const PANEL_OFFSET_STORAGE_KEY = "panel-offset-from-pet";
const DISPLAY_ALIAS_STORAGE_KEY = "display-aliases";
const DEFAULT_SETTINGS: DesktopSettings = { version: 1, language: "ko", characterId: "gpt", scale: 1, position: "last", alwaysOnTop: true, showStatusBadge: true, showBubbles: false };

function loadSettings(): DesktopSettings {
  try {
    const saved = JSON.parse(window.localStorage.getItem(SETTINGS_STORAGE_KEY) || "null") as Partial<DesktopSettings> | null;
    const legacyCharacter = window.localStorage.getItem(CHARACTER_STORAGE_KEY) as CharacterId | null;
    const legacyCharacterId = CHARACTERS.find((character) => character.id === legacyCharacter)?.id;
    const characterId: CharacterId = saved?.characterId && CHARACTERS.some((character) => character.id === saved.characterId) ? saved.characterId : (legacyCharacterId ?? DEFAULT_SETTINGS.characterId);
    const language = saved?.language === "en" ? "en" : "ko";
    const position = ["top-left", "top-right", "bottom-left", "bottom-right", "last"].includes(saved?.position || "") ? saved?.position as PetPosition : DEFAULT_SETTINGS.position;
    return { ...DEFAULT_SETTINGS, ...saved, language, characterId, position, scale: Math.min(1.5, Math.max(.7, Number(saved?.scale) || 1)) };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

type DisplayAliases = { sessions: Record<string, string>; prompts: Record<string, string> };
function loadDisplayAliases(): DisplayAliases {
  try {
    const saved = JSON.parse(window.localStorage.getItem(DISPLAY_ALIAS_STORAGE_KEY) || "null") as Partial<DisplayAliases> | null;
    return { sessions: saved?.sessions ?? {}, prompts: saved?.prompts ?? {} };
  } catch {
    return { sessions: {}, prompts: {} };
  }
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";
const compact = (value?: number | null) => value == null ? "—" : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
const cost = (_value?: number | null) => "확인 불가";
const percent = (value?: number | null) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const time = (value?: string | null) => value ? new Date(value).toLocaleString([], { hour: "2-digit", minute: "2-digit" }) : "Unknown time";
const alertGroupKey = (alert: AlertRow) => `${alert.type || "unknown"}|${alert.session_id || alert.external_session_id || "unknown"}|${alert.agent_name || ""}|${alert.project_name || ""}`;
const alertPriority = (alert: AlertRow) => alert.severity?.toLowerCase() === "critical" ? 2 : 1;
const friendlyAlert = (alert: AlertRow, language: Language) => {
  const current = alert.current_value;
  const currentText = current == null ? "" : alert.metric === "context_utilization" ? `${current.toFixed(1)}%` : compact(current);
  if (language === "en") {
    if (alert.type === "CONTEXT_PRESSURE") return {
      title: "Conversation context is getting full",
      description: currentText ? `This turn uses about ${currentText} of the model's context capacity.` : "This turn is using a large part of the model's context capacity.",
      action: "Consider starting a new session or reducing unnecessary file and tool output.",
    };
    if (alert.type === "CONTEXT_SPIKE") return { title: "Input tokens jumped", description: alert.description || "The input grew sharply compared with the previous turn.", action: "Open the session to see which file or tool added the context." };
    if (alert.type === "CACHE_DROP") return { title: "Cache efficiency dropped", description: alert.description || "A smaller share of the input was served from cache.", action: "Check whether the prompt or context changed substantially." };
    return { title: alert.title || alert.type || "Usage needs attention", description: alert.description || "An unusual token usage pattern was detected.", action: "Open the related session for details." };
  }
  if (alert.type === "CONTEXT_PRESSURE") return {
    title: "대화 맥락이 많이 쌓였습니다",
    description: currentText ? `현재 턴의 입력이 모델이 기억할 수 있는 한도의 약 ${currentText}입니다.` : "현재 턴의 입력이 모델이 기억할 수 있는 한도의 큰 부분을 차지합니다.",
    action: "권장: 새 세션을 시작하거나 불필요한 파일·도구 출력을 줄여보세요.",
  };
  if (alert.type === "CONTEXT_SPIKE") return { title: "입력 토큰이 갑자기 늘었습니다", description: alert.description || "직전 턴보다 입력 토큰이 크게 증가했습니다.", action: "세션을 열어 어떤 파일이나 도구가 컨텍스트를 추가했는지 확인하세요." };
  if (alert.type === "CACHE_DROP") return { title: "캐시 효율이 떨어졌습니다", description: alert.description || "이전보다 캐시에서 재사용된 입력의 비율이 낮아졌습니다.", action: "프롬프트나 컨텍스트가 크게 바뀐 시점을 확인하세요." };
  if (alert.type === "LARGE_TOOL_OUTPUT") return { title: "도구 결과가 너무 큽니다", description: alert.description || "도구가 많은 양의 결과를 반환했습니다.", action: "검색 결과를 제한하거나 필요한 부분만 읽도록 조정하세요." };
  if (alert.type === "LARGE_FILE_READ") return { title: "큰 파일이 컨텍스트에 들어왔습니다", description: alert.description || "한 파일을 읽는 데 많은 토큰이 사용되었습니다.", action: "파일 전체 대신 필요한 구간만 읽도록 조정하세요." };
  if (alert.type === "REPEATED_FILE_READ" || alert.type === "REPEATED_CONTEXT") return { title: "같은 컨텍스트가 반복되었습니다", description: alert.description || "동일한 파일 또는 내용이 여러 턴에 다시 들어왔습니다.", action: "반복 주입을 줄이면 이후 턴의 입력 토큰을 아낄 수 있습니다." };
  if (alert.type === "ABNORMAL_BURN") return { title: "비정상적인 토큰 소모가 감지되었습니다", description: alert.description || "최근 턴 평균보다 토큰 사용량이 크게 증가했습니다.", action: "해당 턴을 열어 실제 input 변화와 함께 기록된 활동을 확인하세요." };
  return { title: alert.title || alert.type || "확인이 필요한 사용량", description: alert.description || "평소와 다른 토큰 사용 패턴이 감지되었습니다.", action: "관련 세션을 열어 실제 usage 변화를 확인하세요." };
};
const usageQuality = (row: { usage_quality?: string; actual_turns?: number; estimated_turns?: number }) => row.usage_quality ?? ((row.actual_turns ?? 0) > 0 && (row.estimated_turns ?? 0) > 0 ? "Mixed" : (row.estimated_turns ?? 0) > 0 ? "Estimated" : "Actual");
const usageQualityClass = (value: string) => value.toLowerCase();
const inputTotal = (row: { input_tokens?: number; cached_tokens?: number; fresh_tokens?: number }) => (row.cached_tokens ?? 0) + (row.fresh_tokens ?? 0) || (row.input_tokens ?? 0);
const usageTotal = (row: { input_tokens?: number; cached_tokens?: number; fresh_tokens?: number; output_tokens?: number }) => inputTotal(row) + (row.output_tokens ?? 0);
const modelKey = (model?: string | null, provider?: string | null) => `${provider ?? "unknown"}::${model ?? "Unknown"}`;
const rangeParams = (range: "today" | "7d" | "all") => {
  const params = new URLSearchParams();
  if (range !== "all") {
    const start = new Date();
    if (range === "today") start.setHours(0, 0, 0, 0);
    else start.setDate(start.getDate() - 7);
    params.set("start", start.toISOString());
  }
  return params;
};

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

function Pet({ onClick, onContextMenu, severity, character, scale = 1, showStatusBadge = true, showBubbles = false }: { onClick: () => void; onContextMenu?: () => void; severity: "ok" | "warning" | "critical"; character: Character; scale?: number; showStatusBadge?: boolean; showBubbles?: boolean }) {
  const dragTimer = useRef<number | undefined>(undefined);
  const dragging = useRef(false);
  const didDrag = useRef(false);
  const startPoint = useRef<{ x: number; y: number } | null>(null);
  const beginDrag = () => { if (dragging.current) return; dragging.current = true; didDrag.current = true; getCurrentWindow().startDragging().catch(() => { dragging.current = false; didDrag.current = false; }); };
  const rememberPosition = async () => { try { const position = await getCurrentWindow().outerPosition(); window.localStorage.setItem("pet-position", JSON.stringify({ x: position.x, y: position.y })); } catch { /* Browser preview has no native window API. */ } };
  const startDragAfterHold = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    startPoint.current = { x: event.screenX, y: event.screenY };
    dragTimer.current = window.setTimeout(beginDrag, 220);
  };
  const movePet = (event: React.MouseEvent<HTMLButtonElement>) => { if (event.buttons !== 1 || dragging.current || !startPoint.current) return; const dx = event.screenX - startPoint.current.x; const dy = event.screenY - startPoint.current.y; if (Math.hypot(dx, dy) >= 6) { if (dragTimer.current !== undefined) window.clearTimeout(dragTimer.current); dragTimer.current = undefined; beginDrag(); } };
  const stopDrag = () => { if (dragTimer.current !== undefined) window.clearTimeout(dragTimer.current); dragTimer.current = undefined; startPoint.current = null; if (dragging.current) void rememberPosition(); dragging.current = false; };
  const clickPet = () => { const wasDrag = didDrag.current; stopDrag(); didDrag.current = false; if (wasDrag) return; onClick(); };
  const openMenu = (event: React.MouseEvent<HTMLButtonElement>) => { event.preventDefault(); onContextMenu?.(); };
  const statusLabel = severity === "ok" ? "OK" : severity === "warning" ? "!" : "!!";
  return <button className={`pet-button ${severity} character-${character.id}`} style={{ ["--pet-scale" as string]: scale } as React.CSSProperties} onMouseDown={startDragAfterHold} onMouseMove={movePet} onMouseUp={stopDrag} onMouseLeave={() => { if (!dragging.current) stopDrag(); }} onClick={clickPet} onContextMenu={openMenu} aria-label={`${character.name} · 토큰 관제 검색 열기`} title={`${character.name} · 클릭해서 열기 · 길게 눌러 이동`}><span className="pet-aura" />{showBubbles && <span className="pet-bubble">{severity === "ok" ? "OK" : severity === "warning" ? "!" : "!!"}</span>}<img className="pet-sprite" src={character.sprite} alt="" draggable="false" />{showStatusBadge && <span className="pet-status">{statusLabel}</span>}</button>;
}

function MetricLine({ input, cached, fresh, output, reasoning, cache }: { input?: number; cached?: number; fresh?: number; output?: number; reasoning?: number; cache?: number | null }) {
  return <span className="metric-lines"><span>입력 {compact(input)} · 출력 {compact(output)} · 추론 {compact(reasoning)}</span><span>캐시 {compact(cached)} · 신규 {compact(fresh)}{cache == null ? "" : ` · 적중 ${percent(cache)}`}</span></span>;
}

type ChartPoint = { label: string; values: Record<string, number> };
type ChartSeries = { key: string; label: string; color: string };

function LineChart({ points, series, onSeriesSelect }: { points: ChartPoint[]; series: ChartSeries[]; onSeriesSelect?: (key: string) => void }) {
  const width = 760;
  const height = 220;
  const left = 48;
  const right = 14;
  const top = 14;
  const bottom = 30;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const maxValue = Math.max(...points.flatMap((point) => series.map((item) => point.values[item.key] ?? 0)), 1);
  const x = (index: number) => points.length <= 1 ? left + plotWidth / 2 : left + (index / (points.length - 1)) * plotWidth;
  const y = (value: number) => top + plotHeight - (value / maxValue) * plotHeight;
  const yTicks = [0, .25, .5, .75, 1];
  const xTickIndexes = Array.from(new Set([0, Math.floor((points.length - 1) / 3), Math.floor((points.length - 1) / 2), Math.floor(((points.length - 1) * 2) / 3), points.length - 1].filter((index) => index >= 0)));
  return <div className="line-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="모델별 시간 흐름 토큰 사용량 선 그래프">
    <title>모델·프로바이더별 사용 추이</title>
    <desc>시간 구간별 실효 입력과 출력 토큰을 모델별 선으로 비교합니다.</desc>
    {yTicks.map((tick) => <g key={tick}><line className="line-chart-grid" x1={left} x2={width - right} y1={y(tick * maxValue)} y2={y(tick * maxValue)} /><text className="line-chart-axis-label" x={left - 7} y={y(tick * maxValue) + 3} textAnchor="end">{compact(tick * maxValue)}</text></g>)}
    <line className="line-chart-axis" x1={left} x2={left} y1={top} y2={top + plotHeight} /><line className="line-chart-axis" x1={left} x2={width - right} y1={top + plotHeight} y2={top + plotHeight} />
    {xTickIndexes.map((index) => <text className="line-chart-axis-label" key={index} x={x(index)} y={height - 8} textAnchor="middle">{points[index]?.label}</text>)}
    {series.map((item) => <g key={item.key}><polyline className="line-chart-line" stroke={item.color} points={points.map((point, index) => `${x(index)},${y(point.values[item.key] ?? 0)}`).join(" ")} />{points.map((point, index) => <circle className="line-chart-point" key={`${item.key}-${index}`} cx={x(index)} cy={y(point.values[item.key] ?? 0)} r="4" fill={item.color} data-tooltip={`${item.label} · ${point.label} · ${compact(point.values[item.key] ?? 0)} 토큰`} onClick={() => onSeriesSelect?.(item.key)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSeriesSelect?.(item.key); }} tabIndex={0} role="button"><title>{`${item.label} · ${point.label} · ${compact(point.values[item.key] ?? 0)} 토큰`}</title></circle>)}</g>)}
  </svg><div className="dashboard-legend" aria-label="모델 범례">{series.map((item) => <button type="button" key={item.key} onClick={() => onSeriesSelect?.(item.key)}><i style={{ background: item.color }} />{item.label}</button>)}</div></div>;
}

function SessionShareChart({ sessions, onSelectSession, sessionDisplayName }: { sessions: SessionRow[]; onSelectSession: (id: string) => void; sessionDisplayName: (session: SessionRow) => string }) {
  const ranked = sessions.map((session) => ({ session, total: (session.total_cached_input_tokens ?? 0) + (session.total_fresh_input_tokens ?? 0) + (session.total_output_tokens ?? 0) || (session.total_input_tokens ?? 0) })).filter((item) => item.total > 0).sort((a, b) => b.total - a.total);
  if (!ranked.length) return <div className="session-share-empty">선택한 프로젝트의 실제 세션 사용량이 없습니다.</div>;
  const visible = ranked.slice(0, 6);
  const otherTotal = ranked.slice(6).reduce((sum, item) => sum + item.total, 0);
  if (otherTotal > 0) visible.push({ session: { id: "__other__", external_session_id: "__other__", project_name: "기타" }, total: otherTotal });
  const total = visible.reduce((sum, item) => sum + item.total, 0);
  const colors = ["#43d6df", "#a17bff", "#f3af5d", "#54d39e", "#ff7180", "#e6c35a", "#71809a"];
  let cursor = 0;
  const stops = visible.map((item, index) => { const start = cursor; cursor += item.total / total * 100; return `${colors[index % colors.length]} ${start}% ${cursor}%`; });
  return <div className="session-share-chart"><div className="session-donut" style={{ background: `conic-gradient(${stops.join(", ")})` }} role="img" aria-label="프로젝트 내 세션별 실제 토큰 점유율"><div><strong>{compact(total)}</strong><small>실제 토큰</small></div></div><div className="session-share-legend">{visible.map((item, index) => { const label = item.session.id === "__other__" ? "기타 세션" : sessionDisplayName(item.session); return item.session.id === "__other__" ? <div className="session-share-row" key={item.session.id}><i style={{ background: colors[index % colors.length] }} /><span><strong>{label}</strong><small>{compact(item.total)} · {percent(item.total / total)}</small></span></div> : <button type="button" className="session-share-row" key={item.session.id} onClick={() => onSelectSession(item.session.id)}><i style={{ background: colors[index % colors.length] }} /><span><strong>{label}</strong><small>{compact(item.total)} · {percent(item.total / total)}</small></span><b>›</b></button>; })}</div></div>;
}

function ProjectShareChart({ projects, onSelectProject }: { projects: ProjectRow[]; onSelectProject: (id: string) => void }) {
  const ranked = projects.map((project) => ({ project, total: usageTotal(project) })).filter((item) => item.total > 0).sort((a, b) => b.total - a.total);
  if (ranked.length < 2) return null;
  const visible = ranked.slice(0, 6);
  const otherTotal = ranked.slice(6).reduce((sum, item) => sum + item.total, 0);
  if (otherTotal > 0) visible.push({ project: { id: "__other__", project_name: "기타 프로젝트" }, total: otherTotal });
  const total = visible.reduce((sum, item) => sum + item.total, 0);
  const colors = ["#43d6df", "#a17bff", "#f3af5d", "#54d39e", "#ff7180", "#e6c35a", "#71809a"];
  let cursor = 0;
  const stops = visible.map((item, index) => { const start = cursor; cursor += item.total / total * 100; return `${colors[index % colors.length]} ${start}% ${cursor}%`; });
  return <div className="project-share-panel"><div className="group-title"><span>프로젝트별 토큰 분포</span><small>선택한 모델 · 실제 토큰 점유율</small></div><div className="session-share-chart"><div className="session-donut" style={{ background: `conic-gradient(${stops.join(", ")})` }} role="img" aria-label="모델 내 프로젝트별 실제 토큰 점유율"><div><strong>{compact(total)}</strong><small>실제 토큰</small></div></div><div className="session-share-legend">{visible.map((item, index) => item.project.id === "__other__" ? <div className="session-share-row" key={item.project.id}><i style={{ background: colors[index % colors.length] }} /><span><strong>기타 프로젝트</strong><small>{compact(item.total)} · {percent(item.total / total)}</small></span></div> : <button type="button" className="session-share-row" key={item.project.id} onClick={() => onSelectProject(item.project.id)}><i style={{ background: colors[index % colors.length] }} /><span><strong>{item.project.project_name || "프로젝트 없음"}</strong><small>{compact(item.total)} · {percent(item.total / total)}</small></span><b>›</b></button>)}</div></div></div>;
}

function CompositionBar({ cached, fresh, output }: { cached: number; fresh: number; output: number }) {
  const total = cached + fresh + output;
  const width = (value: number) => `${total ? (value / total) * 100 : 0}%`;
  return <div className="composition-visual" aria-label="오늘 토큰 구성"><div className="composition-bar" role="img" aria-label={`캐시 ${percent(total ? cached / total : 0)}, 신규 ${percent(total ? fresh / total : 0)}, 출력 ${percent(total ? output / total : 0)}`}><i className="cached" style={{ width: width(cached) }} /><i className="fresh" style={{ width: width(fresh) }} /><i className="output" style={{ width: width(output) }} /></div><div className="composition-legend"><span><i className="cached" />캐시 {compact(cached)} <b>{percent(total ? cached / total : 0)}</b></span><span><i className="fresh" />신규 {compact(fresh)} <b>{percent(total ? fresh / total : 0)}</b></span><span><i className="output" />출력 {compact(output)} <b>{percent(total ? output / total : 0)}</b></span></div></div>;
}

function PromptCoverageNotice({ coverage }: { coverage?: PromptCoverage }) {
  if (!coverage || coverage.status === "NO_DATA") return null;
  const status = coverage.status === "COMPLETE" ? "complete" : coverage.status === "PARTIAL" ? "partial" : "unavailable";
  const title = coverage.status === "COMPLETE" ? "모든 실제 usage가 프롬프트에 연결됨" : coverage.status === "PARTIAL" ? "프롬프트 연결이 일부만 확인됨" : "프롬프트 연결을 확인할 수 없음";
  const coverageText = coverage.linked_token_coverage == null ? "—" : percent(coverage.linked_token_coverage);
  return <div className={`prompt-coverage-notice ${status}`}>
    <div><strong>{title}</strong><small>세션의 공식 사용량은 전체 실제 usage 기준입니다. 프롬프트 수치는 연결된 턴만 묶었습니다.</small></div>
    <b>{coverageText}<small>usage 연결률</small></b>
    <span>연결 {coverage.linked_prompt_turns ?? 0}턴 · 미연결 {coverage.unlinked_usage_turns ?? 0}턴 · 미연결 {compact(coverage.unlinked_actual_tokens)} 토큰</span>
  </div>;
}

type DashboardTab = "overview" | "hierarchy" | "prompts" | "causes";
type DashboardProps = {
  overview: Overview | null;
  models: ModelRow[];
  projects: ProjectRow[];
  sessions: SessionRow[];
  turns: TurnRow[];
  timeline: TimelineRow[];
  groupedAlerts: AlertRow[];
  selectedModel: string | null;
  selectedProject: string | null;
  selectedSession: string | null;
  selected: SearchResult | null;
  detail: TurnDetail | null;
  impact: TurnImpact | null;
  sessionInsights: SessionInsights | null;
  hierarchyRange: "today" | "7d" | "all";
  timelineRange: "today" | "7d" | "all";
  setTimelineRange: (range: "today" | "7d" | "all") => void;
  setBucketMinutes: (minutes: number) => void;
  bucketMinutes: number;
  onSelectModel: (model: ModelRow) => void;
  onSelectProject: (id: string) => void;
  onSelectSession: (id: string) => void;
  onClearModel: () => void;
  onClearProject: () => void;
  onClearSession: () => void;
  onOpenTurn: (turn: TurnRow, index: number) => void;
  onOpenAlert: (alert: AlertRow) => void;
  onClearDetail: () => void;
  sessionDisplayName: (session: SessionRow) => string;
  promptDisplayName: (turn: TurnRow, index: number) => string;
};

type CauseRow = { label: string; detail: string; tokens: number; estimated: boolean };

function DashboardView({ overview, models, projects, sessions, turns, timeline, groupedAlerts, selectedModel, selectedProject, selectedSession, selected, detail, impact, sessionInsights, hierarchyRange, timelineRange, setTimelineRange, setBucketMinutes, bucketMinutes, onSelectModel, onSelectProject, onSelectSession, onClearModel, onClearProject, onClearSession, onOpenTurn, onOpenAlert, onClearDetail, sessionDisplayName, promptDisplayName }: DashboardProps) {
  const [tab, setTab] = useState<DashboardTab>("overview");
  const today = overview?.today ?? {};
  const modelKeys = Array.from(new Set(timeline.flatMap((row) => Object.keys(row.models ?? {}))));
  const colors = ["#43d6df", "#a17bff", "#f3af5d", "#54d39e", "#ff7180", "#e6c35a"];
  const chartSeries = modelKeys.map((key, index) => { const row = timeline.flatMap((item) => Object.entries(item.models ?? {})).find(([candidate]) => candidate === key)?.[1]; return { key, color: colors[index % colors.length], label: `${row?.agent_name || row?.provider || "제공자 없음"} · ${row?.model || "모델 정보 없음"}` }; });
  const chartPoints = timeline.slice(-24).map((row) => ({ label: new Date(row.bucket).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), values: Object.fromEntries(modelKeys.map((key) => [key, (row.models?.[key]?.effective_input_tokens ?? 0) + (row.models?.[key]?.output_tokens ?? 0)])) }));
  const cacheSeries: ChartSeries[] = [{ key: "cached", label: "캐시 입력", color: colors[0] }, { key: "fresh", label: "신규 입력", color: colors[2] }];
  const cachePoints = timeline.slice(-24).map((row) => ({ label: new Date(row.bucket).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), values: { cached: row.cached_tokens ?? 0, fresh: row.fresh_tokens ?? 0 } }));
  const activeModel = models.find((model) => modelKey(model.model, model.provider) === selectedModel);
  const activeProject = projects.find((project) => project.id === selectedProject);
  const visibleSessions = sessions.filter((session) => (!selectedModel || modelKey(session.model, session.provider) === selectedModel) && (!selectedProject || session.project_id === selectedProject));
  const topModel = models[0];
  const topProject = overview?.top_projects?.[0] ?? projects[0];
  const topSession = overview?.top_sessions?.[0] ?? sessions[0];
  const selectedSessionRow = sessions.find((session) => session.id === selectedSession);
  const detailCauseRows: CauseRow[] = [];
  const detailCauseMax = Math.max(...detailCauseRows.map((item) => item.tokens), 1);
  const metric = (row: ModelRow | ProjectRow | SessionRow) => {
    if ("total_tokens" in row && row.total_tokens != null) return row.total_tokens;
    if ("total_cached_input_tokens" in row) return (row.total_cached_input_tokens ?? 0) + (row.total_fresh_input_tokens ?? 0) + (row.total_output_tokens ?? 0);
    return usageTotal(row as ModelRow | ProjectRow);
  };

  const breadcrumb = <div className="dashboard-breadcrumb"><button onClick={() => { onClearModel(); onClearProject(); onClearSession(); }}>전체</button>{activeModel && <><span>›</span><button onClick={() => { onClearProject(); onClearSession(); }}>{activeModel.agent_name || activeModel.provider || "제공자"} · {activeModel.model}</button></>}{activeProject && <><span>›</span><button onClick={() => { onClearSession(); }}>{activeProject.project_name}</button></>}{selectedSessionRow && <><span>›</span><span>{sessionDisplayName(selectedSessionRow)}</span></>}</div>;

  const hierarchy = <section className="dashboard-panel dashboard-hierarchy-panel"><div className="dashboard-panel-head"><div><strong>사용량 계층 탐색</strong><small>모델 → 프로젝트 → 세션까지는 전체 실제 usage, 프롬프트는 연결된 턴만 확인합니다.</small></div><span className="dashboard-period">{hierarchyRange === "today" ? "오늘" : hierarchyRange === "7d" ? "최근 7일" : "전체"}</span></div>{breadcrumb}{!selectedModel && !selectedProject && !selectedSession && <div className="dashboard-list">{models.map((model) => <button type="button" className="dashboard-list-row" key={modelKey(model.model, model.provider)} onClick={() => onSelectModel(model)}><span className="dashboard-list-icon model">◈</span><span><strong>{model.model}</strong><small>{model.agent_name || model.provider || "제공자 정보 없음"} · {model.turns ?? 0}턴 · 캐시 {percent(model.cache_hit_rate)}</small></span><b>{compact(metric(model))}</b><i>›</i></button>)}</div>}{selectedModel && !selectedProject && !selectedSession && <div className="dashboard-list"><div className="dashboard-subtitle">{activeModel?.model || "선택한 모델"}을 사용한 프로젝트</div>{projects.map((project) => <button type="button" className="dashboard-list-row" key={project.id} onClick={() => onSelectProject(project.id)}><span className="dashboard-list-icon project">⌂</span><span><strong>{project.project_name || "프로젝트 없음"}</strong><small>{project.session_count ?? 0}개 세션 · {project.agent_name || activeModel?.agent_name || "제공자 정보 없음"}</small></span><b>{compact(metric(project))}</b><i>›</i></button>)}</div>}{selectedProject && !selectedSession && <div className="dashboard-list"><div className="dashboard-subtitle">{activeProject?.project_name || "선택한 프로젝트"}의 세션</div>{visibleSessions.map((session) => <button type="button" className="dashboard-list-row" key={session.id} onClick={() => onSelectSession(session.id)}><span className={`dashboard-list-icon session ${session.status === "ACTIVE" ? "active" : ""}`}>◉</span><span><strong>{sessionDisplayName(session)}</strong><small>{session.model || "모델 정보 없음"} · {session.status || "상태 없음"} · 캐시 {percent(session.cache_hit_rate)}</small></span><b>{compact(metric(session))}</b><i>›</i></button>)}</div>}{selectedSession && <><PromptCoverageNotice coverage={selectedSessionRow?.prompt_coverage} /><div className="dashboard-list"><div className="dashboard-subtitle">{selectedSessionRow ? sessionDisplayName(selectedSessionRow) : "선택한 세션"}의 연결된 프롬프트·턴</div>{turns.map((turn, index) => { const input = (turn.cached_input_tokens ?? 0) + (turn.fresh_input_tokens ?? 0) || (turn.input_tokens ?? 0); const linked = turn.prompt_linked; return <button type="button" className={`dashboard-list-row prompt ${linked ? "linked" : "unlinked"}`} key={turn.id} onClick={() => { setTab("prompts"); onOpenTurn(turn, index); }}><span className="dashboard-list-icon prompt">{linked ? index + 1 : "·"}</span><span><strong>{linked ? promptDisplayName(turn, index) : "미연결 provider usage"}</strong><small>{time(turn.timestamp)} · {linked ? (turn.prompt_content_stored ? "프롬프트 연결됨 · 원문 저장됨" : "프롬프트 연결됨 · 원문 미저장") : "이 usage에 연결된 프롬프트 정보 없음"} · 입력 {compact(input)} · 출력 {compact(turn.output_tokens)}</small></span><b>{compact(input + (turn.output_tokens ?? 0))}</b><i>›</i></button>; })}</div></>}{!models.length && <div className="dashboard-empty">선택한 기간에 모델 사용량이 없습니다.</div>}</section>;

  const promptDetail = selected && detail ? <section className="dashboard-panel dashboard-prompt-detail"><div className="dashboard-panel-head"><div><strong>프롬프트 사용량</strong><small>{selectedSessionRow ? sessionDisplayName(selectedSessionRow) : selected.project_name || "세션 정보"} · {time(detail.timestamp)}</small></div><button type="button" onClick={onClearDetail}>목록으로</button></div><div className="dashboard-prompt-title"><strong>{selected.title || "프롬프트"}</strong><span className="actual">provider 기록</span></div><details className="prompt-source"><summary>프롬프트 원문 보기</summary><pre>{detail.user_prompt || "원문이 저장되지 않았습니다. 설정에서 프롬프트 저장을 켜면 확인할 수 있습니다."}</pre></details><div className="dashboard-token-strip"><div><small>실효 입력</small><strong>{compact((detail.cached_input_tokens ?? 0) + (detail.fresh_input_tokens ?? 0) || detail.input_tokens)}</strong></div><div><small>캐시 입력</small><strong>{compact(detail.cached_input_tokens)}</strong></div><div><small>신규 입력</small><strong>{compact(detail.fresh_input_tokens)}</strong></div><div><small>출력</small><strong>{compact(detail.output_tokens)}</strong></div><div><small>증가량</small><strong>{compact(detail.input_delta_tokens)}</strong></div><div><small>비용</small><strong>{cost(detail.estimated_cost)}</strong></div></div><div className="dashboard-observed-note"><strong>이 턴에서 확인된 활동</strong><span>provider 로그에는 활동별 token 귀속 정보가 없습니다. 아래에는 같은 턴에 기록된 이벤트만 표시합니다.</span></div><div className="dashboard-explanation">이 턴에 연결된 활동별 token 수는 provider 로그에서 확인할 수 없습니다.</div>{!!detail.tool_summary && <div className="dashboard-chip-row"><span>도구 {detail.tool_summary.tools_used ?? 0}개</span><span>파일 {detail.tool_summary.files_read ?? 0}개</span><span>MCP {detail.tool_summary.mcp_calls ?? 0}회</span><span>서브에이전트 {detail.tool_summary.subagents ?? 0}개</span></div>}<div className="dashboard-explanation">입력 {compact(detail.input_before_tokens)} → {compact(detail.input_after_tokens ?? detail.input_tokens)}. 이 화면은 실제 provider usage와 같은 턴에 기록된 활동을 함께 보여줍니다. 활동별 token 원인은 provider 로그에서 제공되지 않습니다.</div></section> : null;

  const cacheValue = today.cached_tokens ?? 0;
  const freshValue = today.fresh_tokens ?? 0;
  const outputValue = today.output_tokens ?? 0;
  const chart = timeline.length ? <LineChart points={chartPoints} series={chartSeries} onSeriesSelect={(key) => { const model = models.find((item) => modelKey(item.model, item.provider) === key); if (model) onSelectModel(model); }} /> : <div className="dashboard-empty">선택한 기간의 사용량 추이가 없습니다.</div>;
  return <section className="dashboard-view"><div className="dashboard-view-head"><div><small>토큰 사용량 진단</small><h2>어디에서 토큰이 늘었는지 찾아보세요</h2><p>모델 → 프로젝트 → 세션 → 프롬프트 순서로 분석합니다.</p></div><div className="dashboard-head-stat"><strong>{compact(today.total_tokens)}</strong><span>오늘 실효 토큰</span><em>캐시 적중 {percent(today.cache_hit_rate)}</em></div></div><nav className="dashboard-tabs" aria-label="상세 분석 메뉴"><button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>흐름</button><button className={tab === "hierarchy" ? "active" : ""} onClick={() => setTab("hierarchy")}>계층 탐색</button><button className={tab === "prompts" ? "active" : ""} onClick={() => setTab("prompts")}>프롬프트</button><button className={tab === "causes" ? "active" : ""} onClick={() => setTab("causes")}>확인된 활동</button></nav>{selected && detail ? promptDetail : tab === "overview" ? <><div className="dashboard-kpis"><div><small>가장 많이 쓴 모델</small><strong>{topModel?.model || "—"}</strong><span>{topModel ? `${topModel.agent_name || topModel.provider || "제공자 없음"} · ${compact(metric(topModel))}` : "모델 데이터 없음"}</span></div><div><small>가장 많이 쓴 프로젝트</small><strong>{topProject?.project_name || "—"}</strong><span>{topProject ? compact(metric(topProject)) : "프로젝트 데이터 없음"}</span></div><div><small>가장 많이 쓴 세션</small><strong>{topSession ? sessionDisplayName(topSession) : "—"}</strong><span>{topSession ? compact(metric(topSession)) : "세션 데이터 없음"}</span></div><div><small>확인이 필요한 세션</small><strong>{groupedAlerts.length}개</strong><span>반복 기록은 세션·유형별로 묶음</span></div></div><div className="dashboard-columns"><section className="dashboard-panel dashboard-chart-panel"><div className="dashboard-panel-head"><div><strong>모델·프로바이더별 사용 추이</strong><small>선 위 점에 마우스를 올리면 구간별 토큰을 보고, 범례를 누르면 해당 모델을 탐색합니다.</small></div><div className="dashboard-controls"><select value={timelineRange} onChange={(event) => setTimelineRange(event.target.value as "today" | "7d" | "all")}><option value="today">오늘</option><option value="7d">최근 7일</option><option value="all">전체</option></select><select value={bucketMinutes} onChange={(event) => setBucketMinutes(Number(event.target.value))}><option value="5">5분</option><option value="15">15분</option><option value="60">1시간</option><option value="1440">1일</option></select></div></div>{chart}</section><section className="dashboard-panel dashboard-composition-panel"><div className="dashboard-panel-head"><div><strong>오늘 입력 구성</strong><small>캐시와 새 컨텍스트가 어떻게 섞였는지 보여줍니다.</small></div><span className="dashboard-period">{percent(today.cache_hit_rate)} 캐시 적중</span></div><CompositionBar cached={cacheValue} fresh={freshValue} output={outputValue} /></section><section className="dashboard-panel dashboard-alert-panel"><div className="dashboard-panel-head"><div><strong>먼저 확인할 세션</strong><small>토큰 증가가 기록된 세션을 바로 열어볼 수 있습니다.</small></div><b>{groupedAlerts.length}</b></div>{groupedAlerts.slice(0, 5).map((alert) => <button type="button" className="dashboard-alert-row" key={alertGroupKey(alert)} onClick={() => onOpenAlert(alert)}><span className={alert.severity?.toLowerCase() === "critical" ? "critical" : "warning"}>!</span><div><strong>{friendlyAlert(alert, "ko").title}</strong><small>{alert.project_name || "프로젝트 없음"} · {alert.external_session_id?.slice(0, 8) || "세션 없음"}</small></div><b>›</b></button>)}{!groupedAlerts.length && <div className="dashboard-empty">현재 우선 확인할 이상 사용이 없습니다.</div>}</section></div></> : tab === "hierarchy" ? hierarchy : tab === "prompts" ? <>{selectedSession ? hierarchy : <section className="dashboard-panel dashboard-empty-panel"><strong>프롬프트를 분석할 세션을 선택하세요.</strong><span>계층 탐색에서 프로젝트와 세션을 선택하면 이곳에서 프롬프트별 사용량을 비교할 수 있습니다.</span><button type="button" onClick={() => setTab("hierarchy")}>계층 탐색으로 이동</button></section>}</> : <>{selectedSession && sessionInsights ? <section className="dashboard-cause-layout"><section className="dashboard-panel"><div className="dashboard-panel-head"><div><strong>{selectedSessionRow ? sessionDisplayName(selectedSessionRow) : "선택한 세션"}의 확인된 활동</strong><small>provider usage와 같은 시점에 기록된 활동</small></div><button type="button" onClick={() => setTab("prompts")}>프롬프트 보기</button></div><div className="dashboard-cause-metrics"><div><small>실효 입력</small><strong>{compact(sessionInsights.effective_input_tokens)}</strong><span>provider 기록</span></div><div><small>실제 출력</small><strong>{compact(sessionInsights.output_tokens)}</strong><span>provider 기록</span></div><div><small>캐시 적중</small><strong>{percent(sessionInsights.cache_hit_rate)}</strong><span>실제 usage로 계산</span></div><div><small>원인별 token</small><strong>확인 불가</strong><span>provider 미제공</span></div></div>{!!sessionInsights.tool_activity?.length && <div className="dashboard-cause-list"><strong>확인된 도구 호출</strong>{sessionInsights.tool_activity.slice(0, 6).map((tool, index) => <div key={`${tool.tool_name}-${index}`}><span>{tool.tool_name || "도구"}<small>{tool.targets?.join(", ") || "대상 정보 없음"}</small></span><b>{tool.calls ?? 0}회</b></div>)}</div>}{!!sessionInsights.repeated_contexts?.length && <div className="dashboard-cause-list repeated"><strong>반복 확인된 컨텍스트</strong>{sessionInsights.repeated_contexts.slice(0, 6).map((item, index) => <div key={`${item.file_path || item.tool_name || index}`}><span>{item.file_path || item.tool_name || item.type || "컨텍스트"}</span><b>{item.injected_count}회 · token 영향 확인 불가</b></div>)}</div>}</section></section> : <section className="dashboard-panel dashboard-empty-panel"><strong>사용량을 확인할 세션을 선택하세요.</strong><span>세션을 선택하면 provider가 기록한 실제 usage와 같은 시점의 활동 이벤트를 확인할 수 있습니다.</span><button type="button" onClick={() => setTab("hierarchy")}>세션 선택하기</button></section>}</>}</section>;
}

function App() {
  const isPanel = getCurrentWindow().label === "panel";
  const [expanded, setExpanded] = useState(isPanel);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selected, setSelected] = useState<SearchResult | null>(null);
  const [detail, setDetail] = useState<TurnDetail | null>(null);
  const [impact, setImpact] = useState<TurnImpact | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [models, setModels] = useState<ModelRow[]>([]);
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [turns, setTurns] = useState<TurnRow[]>([]);
  const [sessionInsights, setSessionInsights] = useState<SessionInsights | null>(null);
  const [sessionTimeline, setSessionTimeline] = useState<SessionTimelineItem[]>([]);
  const [timeline, setTimeline] = useState<TimelineRow[]>([]);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [bucketMinutes, setBucketMinutes] = useState(60);
  const [timelineRange, setTimelineRange] = useState<"today" | "7d" | "all">("today");
  const [hierarchyRange, setHierarchyRange] = useState<"today" | "7d" | "all">("today");
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [selectedProject, setSelectedProject] = useState<string | null>(null);
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loadingOverview, setLoadingOverview] = useState(true);
  const [searching, setSearching] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [displayAliases, setDisplayAliases] = useState<DisplayAliases>(() => loadDisplayAliases());
  const [settings, setSettings] = useState<DesktopSettings>(() => loadSettings());
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [contextMenuOpen, setContextMenuOpen] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [dashboardOpen, setDashboardOpen] = useState(false);
  const [showAllModels, setShowAllModels] = useState(false);
  const anchorPosition = useRef<{ x: number; y: number } | null>(null);
  const dashboardRestorePosition = useRef<{ x: number; y: number } | null>(null);

  const language = settings.language;
  const characterId = settings.characterId;
  const setCharacterId = (next: CharacterId) => setSettings((current) => ({ ...current, characterId: next }));
  const ui = (key: Parameters<typeof t>[1]) => t(language, key);
  const severity = useMemo(() => (overview?.alerts?.critical ?? 0) > 0 ? "critical" : (overview?.alerts?.warning ?? 0) > 0 ? "warning" : "ok", [overview]);
  const groupedAlerts = useMemo(() => {
    const groups = new Map<string, AlertRow>();
    for (const alert of alerts) {
      const key = alertGroupKey(alert);
      const existing = groups.get(key);
      if (!existing || alertPriority(alert) > alertPriority(existing) || (alertPriority(alert) === alertPriority(existing) && String(alert.timestamp || "") > String(existing.timestamp || ""))) groups.set(key, alert);
    }
    return Array.from(groups.values()).sort((a, b) => {
      const priority = alertPriority(b) - alertPriority(a);
      return priority || String(b.timestamp || "").localeCompare(String(a.timestamp || ""));
    });
  }, [alerts]);
  const today = overview?.today ?? {};
  const todayAgents = today.by_agent ?? [];
  const todayInput = overview ? inputTotal({ input_tokens: today.input_tokens, cached_tokens: today.cached_tokens, fresh_tokens: today.fresh_tokens }) : undefined;
  const todayTotal = overview ? (today.total_tokens ?? (todayInput ?? 0) + (today.output_tokens ?? 0)) : undefined;
  const topSession = overview?.top_sessions?.[0];
  const topSessionInput = topSession ? ((topSession.total_cached_input_tokens ?? 0) + (topSession.total_fresh_input_tokens ?? 0) || (topSession.total_input_tokens ?? 0)) : null;
  const topSessionTotal = topSessionInput == null ? null : topSessionInput + (topSession?.total_output_tokens ?? 0);
  const topProject = overview?.top_projects?.[0];
  const topProjectTotal = topProject?.total_tokens ?? usageTotal(topProject ?? {});
  const hierarchyQuality = useMemo(() => {
    const actual = agents.reduce((sum, row) => sum + (row.actual_turns ?? 0), 0);
    const estimated = agents.reduce((sum, row) => sum + (row.estimated_turns ?? 0), 0);
    return actual && estimated ? "Mixed" : estimated ? "Estimated" : "Actual";
  }, [agents]);
  const activeAgent = agents.find((agent) => agent.name === selectedAgent);
  const activeModel = models.find((model) => modelKey(model.model, model.provider) === selectedModel);
  const modelSessions = sessions.filter((session) => !selectedModel || modelKey(session.model, session.provider) === selectedModel);
  const modelProjectIds = new Set(modelSessions.map((session) => session.project_id).filter(Boolean));
  const visibleProjects = projects.filter((project) => (!activeAgent || project.agent_name === activeAgent.name) && (!selectedModel || modelProjectIds.has(project.id)));
  const activeProject = visibleProjects.find((project) => project.id === selectedProject);
  const visibleSessions = modelSessions.filter((session) => (!activeAgent || session.agent_name === activeAgent.name) && (!activeProject || session.project_id === activeProject.id));
  const timelineModels = Array.from(new Set(timeline.flatMap((row) => Object.keys(row.models ?? {}))));
  const timelineLabels = timelineModels.map((key) => { const model = timeline.flatMap((row) => Object.entries(row.models ?? {})).find(([candidate]) => candidate === key)?.[1]; return { key, label: `${model?.agent_name || model?.provider || "제공자 없음"} · ${model?.model || "Unknown"}` }; });
  const timelineChartSeries = timelineLabels.map(({ key, label }, index) => ({ key, label, color: ["#43d6df", "#a17bff", "#f3af5d", "#54d39e", "#ff7180", "#e6c35a"][index % 6] }));
  const timelineChartPoints = timeline.slice(-12).map((row) => ({ label: new Date(row.bucket).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), values: Object.fromEntries(timelineModels.map((key) => [key, (row.models?.[key]?.effective_input_tokens ?? 0) + (row.models?.[key]?.output_tokens ?? 0)])) }));
  const selectedCharacter = CHARACTERS.find((character) => character.id === characterId) ?? CHARACTERS[0];
  const sessionDisplayName = (session: SessionRow) => displayAliases.sessions[session.id] || session.session_name || (session.project_name ? `${session.project_name} · ${session.model || "세션"}` : `Session ${session.external_session_id.slice(0, 8)}`);
  const promptDisplayName = (turn: TurnRow, index: number) => !turn.prompt_linked ? "미연결 provider usage" : displayAliases.prompts[turn.id] || (turn.user_prompt?.trim().replace(/\s+/g, " ").slice(0, 72) || `프롬프트 #${index + 1} · ${time(turn.timestamp)}`);
  const renameSession = (session: SessionRow) => { const next = window.prompt(language === "ko" ? "세션 표시 이름을 입력하세요\n비우면 자동 이름으로 돌아갑니다." : "Enter a display name for this session\nLeave blank to use the automatic name.", sessionDisplayName(session)); if (next === null) return; setDisplayAliases((current) => { const sessions = { ...current.sessions }; if (next.trim()) sessions[session.id] = next.trim(); else delete sessions[session.id]; return { ...current, sessions }; }); };
  const renamePrompt = (turn: TurnRow, index: number) => { const next = window.prompt(language === "ko" ? "프롬프트 표시 이름을 입력하세요\n비우면 자동 이름으로 돌아갑니다." : "Enter a display name for this prompt\nLeave blank to use the automatic name.", promptDisplayName(turn, index)); if (next === null) return; setDisplayAliases((current) => { const prompts = { ...current.prompts }; if (next.trim()) prompts[turn.id] = next.trim(); else delete prompts[turn.id]; return { ...current, prompts }; }); };

  useEffect(() => { const loadOverview = (showLoading = false) => { if (showLoading) setLoadingOverview(true); get<Overview>("/api/overview").then((next) => { setOverview(next); setError(""); setLoadingOverview(false); }).catch((reason: Error) => { setError(reason.message); setLoadingOverview(false); }); }; loadOverview(true); const refresh = window.setInterval(() => loadOverview(), 10000); return () => window.clearInterval(refresh); }, []);
  useEffect(() => { if (!expanded) return; let cancelled = false; const loadHierarchy = () => { const baseParams = rangeParams(hierarchyRange); const suffix = baseParams.toString() ? `?${baseParams.toString()}` : ""; const projectParams = new URLSearchParams(baseParams); const sessionParams = new URLSearchParams(baseParams); if (activeModel?.model) { projectParams.set("model", activeModel.model); sessionParams.set("model", activeModel.model); if (activeModel.provider) { projectParams.set("provider", activeModel.provider); sessionParams.set("provider", activeModel.provider); } } const projectSuffix = projectParams.toString() ? `?${projectParams.toString()}` : ""; const sessionSuffix = sessionParams.toString() ? `?${sessionParams.toString()}` : ""; return Promise.all([get<AgentRow[]>(`/api/agents${suffix}`), get<ModelRow[]>(`/api/usage/models${suffix}`), get<ProjectRow[]>(`/api/projects${projectSuffix}`), get<SessionRow[]>(`/api/sessions${sessionSuffix}`), get<AlertRow[]>("/api/alerts?resolved=false")]).then(([a, m, p, s, activeAlerts]) => { if (!cancelled) { setAgents(a); setModels(m); setProjects(p); setSessions(s); setAlerts(activeAlerts); } }).catch((reason: Error) => { if (!cancelled) setError(reason.message); }); }; loadHierarchy(); const refresh = window.setInterval(loadHierarchy, 10000); return () => { cancelled = true; window.clearInterval(refresh); }; }, [expanded, hierarchyRange, selectedModel, activeModel?.model, activeModel?.provider]);
  useEffect(() => { if (!expanded) return; let cancelled = false; const loadTimeline = () => { const params = new URLSearchParams({ bucket_minutes: String(bucketMinutes) }); if (timelineRange !== "all") { const start = new Date(); if (timelineRange === "today") start.setHours(0, 0, 0, 0); else start.setDate(start.getDate() - 7); params.set("start", start.toISOString()); } get<TimelineRow[]>(`/api/usage/timeline?${params.toString()}`).then((rows) => { if (!cancelled) setTimeline(rows); }).catch((reason: Error) => { if (!cancelled) setError(reason.message); }); }; loadTimeline(); const refresh = window.setInterval(loadTimeline, 10000); return () => { cancelled = true; window.clearInterval(refresh); }; }, [expanded, bucketMinutes, timelineRange]);
  useEffect(() => { if (!selectedSession) { setTurns([]); setSessionInsights(null); setSessionTimeline([]); return; } const params = rangeParams(hierarchyRange).toString(); const suffix = params ? `?${params}` : ""; Promise.all([get<TurnRow[]>(`/api/sessions/${selectedSession}/turns${suffix}`), get<SessionInsights>(`/api/sessions/${selectedSession}/insights${suffix}`), get<SessionTimelineItem[]>(`/api/sessions/${selectedSession}/timeline${suffix}`)]).then(([turnRows, insights, timelineRows]) => { setTurns(turnRows); setSessionInsights(insights); setSessionTimeline(timelineRows); }).catch((reason: Error) => setError((reason as Error).message)); }, [selectedSession, hierarchyRange]);
  useEffect(() => { let unlisten: (() => void) | undefined; listen("request-scan", async () => { try { await fetch(`${API_BASE}/api/scan`, { method: "POST" }); setOverview(await get<Overview>("/api/overview")); } catch (reason) { setError((reason as Error).message); } }).then((dispose) => { unlisten = dispose; }).catch(() => undefined); return () => unlisten?.(); }, []);
  useEffect(() => { const onKeyDown = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); if (isPanel) { setExpanded(true); setSearchOpen(true); } else void Window.getByLabel("panel").then((panel) => panel?.show().then(() => panel.setFocus())); } if (event.key === "Escape") { event.preventDefault(); if (dashboardOpen) { void closeDashboard(); return; } setExpanded(false); setSelected(null); setSearchOpen(false); setContextMenuOpen(false); setAlertsOpen(false); setSettingsOpen(false); if (isPanel) void getCurrentWindow().hide(); } }; window.addEventListener("keydown", onKeyDown); return () => window.removeEventListener("keydown", onKeyDown); }, [isPanel, dashboardOpen]);
  useEffect(() => { if (!expanded || !query.trim()) { setResults([]); return; } const timer = window.setTimeout(() => { setSearching(true); get<SearchResult[]>(`/api/search?q=${encodeURIComponent(query.trim())}&limit=30`).then(setResults).catch((reason: Error) => setError(reason.message)).finally(() => setSearching(false)); }, 180); return () => window.clearTimeout(timer); }, [expanded, query]);
  useEffect(() => { window.localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(settings)); window.localStorage.setItem(CHARACTER_STORAGE_KEY, settings.characterId); if (isPanel) void emitTo("main", "settings-changed", settings); }, [settings, isPanel]);
  useEffect(() => { window.localStorage.setItem(DISPLAY_ALIAS_STORAGE_KEY, JSON.stringify(displayAliases)); }, [displayAliases]);
  useEffect(() => { if (isPanel) return; let unlisten: (() => void) | undefined; listen<DesktopSettings>("settings-changed", (event) => { if (event.payload?.version === 1) setSettings(event.payload); }).then((dispose) => { unlisten = dispose; }).catch(() => undefined); return () => unlisten?.(); }, [isPanel]);
  useEffect(() => { if (!isPanel) return; let unlisten: (() => void) | undefined; listen<{ mode?: PanelMode }>("panel-command", (event) => { const mode = event.payload?.mode ?? "home"; setExpanded(true); setContextMenuOpen(false); setAlertsOpen(mode === "alerts"); setSettingsOpen(mode === "settings"); setSearchOpen(mode === "search"); setSelected(null); setDetail(null); setImpact(null); setQuery(""); if (mode === "home") { setSelectedAgent(null); setSelectedModel(null); setSelectedProject(null); setSelectedSession(null); } }).then((dispose) => { unlisten = dispose; }).catch(() => undefined); return () => unlisten?.(); }, [isPanel]);
  useEffect(() => { if (!isPanel) return; let unlisten: (() => void) | undefined; getCurrentWindow().onMoved(({ payload }) => { void Window.getByLabel("main").then(async (main) => { if (!main) return; const pet = await main.outerPosition(); window.localStorage.setItem(PANEL_OFFSET_STORAGE_KEY, JSON.stringify({ x: payload.x - pet.x, y: payload.y - pet.y })); }); }).then((dispose) => { unlisten = dispose; }).catch(() => undefined); return () => unlisten?.(); }, [isPanel]);
  useEffect(() => { getCurrentWindow().setAlwaysOnTop(settings.alwaysOnTop).catch(() => undefined); }, [settings.alwaysOnTop]);
  useEffect(() => { if (!isPanel) return; void getCurrentWindow().setSize(new LogicalSize(420, 560)).catch(() => undefined); }, [isPanel]);

  useEffect(() => { if (isPanel || expanded || settings.position === "last") return; const placePet = async () => { try { const monitor = await currentMonitor(); const scale = monitor?.scaleFactor ?? 1; const workArea = monitor?.workArea; const width = Math.round(112 * scale); const height = Math.round(112 * scale); const minX = workArea?.position.x ?? 0; const minY = workArea?.position.y ?? 0; const maxX = Math.max(minX, (workArea?.position.x ?? 0) + (workArea?.size.width ?? width) - width); const maxY = Math.max(minY, (workArea?.position.y ?? 0) + (workArea?.size.height ?? height) - height); const x = settings.position.includes("right") ? maxX : minX + Math.round(16 * scale); const y = settings.position.includes("bottom") ? maxY : minY + Math.round(16 * scale); await getCurrentWindow().setPosition(new PhysicalPosition(x, y)); } catch { /* Browser preview has no native window API. */ } }; void placePet(); }, [isPanel, expanded, settings.position]);
  useEffect(() => { if (isPanel || expanded || settings.position !== "last") return; try { const saved = JSON.parse(window.localStorage.getItem("pet-position") || "null") as { x?: number; y?: number } | null; if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) void getCurrentWindow().setPosition(new PhysicalPosition(saved.x as number, saved.y as number)); } catch { /* Browser preview has no native window API. */ } }, [isPanel, expanded, settings.position]);
  const chooseModel = (model: ModelRow) => { setSelectedModel(modelKey(model.model, model.provider)); setSelectedAgent(null); setSelectedProject(null); setSelectedSession(null); setSelected(null); };
  const chooseAgent = (name: string) => { setSelectedAgent(name); setSelectedModel(null); setSelectedProject(null); setSelectedSession(null); setSelected(null); };
  const chooseProject = (id: string) => { setSelectedProject(id); setSelectedSession(null); setSelected(null); };
  const chooseSession = (id: string) => { setSelectedSession(id); setSelected(null); };
  const retryOverview = () => { setLoadingOverview(true); setError(""); get<Overview>("/api/overview").then((next) => { setOverview(next); setLoadingOverview(false); }).catch((reason: Error) => { setError(reason.message); setLoadingOverview(false); }); };
  const openResult = async (result: SearchResult) => { setSelected(result); setDetail(null); setImpact(null); if (!result.turn_id) return; try { const [turnDetail, turnImpact] = await Promise.all([get<TurnDetail>(`/api/turns/${result.turn_id}`), get<TurnImpact>(`/api/turns/${result.turn_id}/impact`)]); setDetail(turnDetail); setImpact(turnImpact); } catch (reason) { setError((reason as Error).message); } };
  const openTurn = async (turn: TurnRow, index = 0) => { setSelected({ result_id: turn.id, turn_id: turn.id, title: promptDisplayName(turn, index), timestamp: turn.timestamp, match_kind: "Prompt / turn", agent_name: activeAgent?.name, project_name: activeProject?.project_name }); setImpact(null); try { const [turnDetail, turnImpact] = await Promise.all([get<TurnDetail>(`/api/turns/${turn.id}`), get<TurnImpact>(`/api/turns/${turn.id}/impact`)]); setDetail(turnDetail); setImpact(turnImpact); } catch (reason) { setError((reason as Error).message); } };
  const openAlertSession = async (alert: AlertRow) => {
    if (!alert.session_id) return;
    setContextMenuOpen(false);
    setAlertsOpen(false);
    setSettingsOpen(false);
    setSearchOpen(false);
    setQuery("");
    setSelected(null);
    setDetail(null);
    setImpact(null);
    setSelectedAgent(null);
    setSelectedModel(null);
    setSelectedProject(null);
    setSelectedSession(alert.session_id);
    setExpanded(true);
    if (alert.turn_id) {
      const result: SearchResult = { result_id: alert.turn_id, session_id: alert.session_id, turn_id: alert.turn_id, timestamp: alert.timestamp, title: alert.title || friendlyAlert(alert, language).title, match_kind: alert.type || "Alert", agent_name: alert.agent_name, project_name: alert.project_name };
      setSelected(result);
      try {
        const [turnDetail, turnImpact] = await Promise.all([get<TurnDetail>(`/api/turns/${alert.turn_id}`), get<TurnImpact>(`/api/turns/${alert.turn_id}/impact`)]);
        setDetail(turnDetail);
        setImpact(turnImpact);
      } catch (reason) { setError((reason as Error).message); }
    }
  };
  const submit = (event: FormEvent) => { event.preventDefault(); if (query.trim()) setExpanded(true); };

  const openPanel = async (mode: PanelMode = "home") => { try { const panel = await Window.getByLabel("panel"); if (!panel) return; const current = await getCurrentWindow().outerPosition(); const monitor = await currentMonitor(); const scale = monitor?.scaleFactor ?? 1; const panelWidth = Math.round(420 * scale); const panelHeight = Math.round(560 * scale); const workArea = monitor?.workArea; const minX = workArea?.position.x ?? 0; const minY = workArea?.position.y ?? 0; const maxX = Math.max(minX, (workArea?.position.x ?? 0) + (workArea?.size.width ?? panelWidth) - panelWidth); const maxY = Math.max(minY, (workArea?.position.y ?? 0) + (workArea?.size.height ?? panelHeight) - panelHeight); let savedOffset: { x?: number; y?: number } | null = null; try { savedOffset = JSON.parse(window.localStorage.getItem(PANEL_OFFSET_STORAGE_KEY) || "null") as { x?: number; y?: number } | null; } catch { savedOffset = null; } const desiredX = current.x + (Number.isFinite(savedOffset?.x) ? savedOffset?.x as number : -Math.round(308 * scale)); const desiredY = current.y + (Number.isFinite(savedOffset?.y) ? savedOffset?.y as number : -Math.round(448 * scale)); await panel.setPosition(new PhysicalPosition(Math.min(maxX, Math.max(minX, desiredX)), Math.min(maxY, Math.max(minY, desiredY)))); await emitTo("panel", "panel-command", { mode }); await panel.show(); await panel.setFocus(); } catch (reason) { setError((reason as Error).message); } };
  const openContextMenu = () => { void invoke("show_pet_context_menu", { language, windowLabel: isPanel ? "panel" : "main" }).catch((reason) => setError((reason as Error).message)); };
  useEffect(() => { if (isPanel) return; let unlisten: (() => void) | undefined; listen<{ mode?: PanelMode }>("open-panel-request", (event) => { void openPanel(event.payload?.mode ?? "home"); }).then((dispose) => { unlisten = dispose; }).catch(() => undefined); return () => unlisten?.(); }, [isPanel]);
  const openExplorer = () => { setContextMenuOpen(false); setAlertsOpen(false); setSettingsOpen(false); setSearchOpen(false); setQuery(""); setSelected(null); setSelectedAgent(null); setSelectedModel(null); setSelectedProject(null); setSelectedSession(null); setExpanded(true); };
  const goHome = () => { openExplorer(); if (dashboardOpen) void closeDashboard(); };
  const openDashboard = async () => {
    if (!isPanel || dashboardOpen) return;
    setDashboardOpen(true);
    setExpanded(true);
    try {
      const window = getCurrentWindow();
      const current = await window.outerPosition();
      dashboardRestorePosition.current = { x: current.x, y: current.y };
      const monitor = await currentMonitor();
      const scale = monitor?.scaleFactor ?? 1;
      const width = Math.round(980 * scale);
      const height = Math.round(760 * scale);
      const workArea = monitor?.workArea;
      const minX = workArea?.position.x ?? 0;
      const minY = workArea?.position.y ?? 0;
      const maxX = Math.max(minX, (workArea?.position.x ?? 0) + (workArea?.size.width ?? width) - width);
      const maxY = Math.max(minY, (workArea?.position.y ?? 0) + (workArea?.size.height ?? height) - height);
      await window.setSize(new LogicalSize(980, 760));
      await window.setPosition(new PhysicalPosition(Math.min(maxX, Math.max(minX, minX + Math.round(((workArea?.size.width ?? width) - width) / 2))), Math.min(maxY, Math.max(minY, minY + Math.round(((workArea?.size.height ?? height) - height) / 2)))));
    } catch (reason) { setError((reason as Error).message); }
  };
  const closeDashboard = async () => {
    if (!isPanel || !dashboardOpen) return;
    setDashboardOpen(false);
    try {
      const window = getCurrentWindow();
      await window.setSize(new LogicalSize(420, 560));
      const restore = dashboardRestorePosition.current;
      if (restore) await window.setPosition(new PhysicalPosition(restore.x, restore.y));
    } catch (reason) { setError((reason as Error).message); }
    dashboardRestorePosition.current = null;
  };
  const closePanel = () => { if (dashboardOpen) { void closeDashboard(); return; } setSelected(null); setContextMenuOpen(false); setAlertsOpen(false); setSettingsOpen(false); setSearchOpen(false); setQuery(""); if (isPanel) void getCurrentWindow().hide(); };
  if (!isPanel) return <Pet onClick={() => void openPanel("home")} onContextMenu={openContextMenu} severity={severity} character={selectedCharacter} scale={settings.scale} showStatusBadge={settings.showStatusBadge} showBubbles={settings.showBubbles} />;
  const showSearch = searchOpen || Boolean(query.trim());
  const explorationLevel = selectedSession ? "사용량 상세" : activeProject ? "세션" : selectedModel || selectedAgent ? "프로젝트" : "모델";
  const explorationTitle = selectedSession ? "세션 실제 사용량" : activeProject ? "세션별 사용량" : selectedModel || selectedAgent ? "프로젝트별 사용량" : "모델별 사용량";
  const displayedModels = showAllModels ? models : models.slice(0, 4);
  const overviewMode = !showSearch && !selected && !selectedModel && !selectedAgent && !activeProject && !selectedSession;
  const overlayMode = contextMenuOpen || alertsOpen || settingsOpen;
  return <main className={`popover ${overviewMode ? "overview-mode" : "explore-mode"} ${dashboardOpen ? "dashboard-mode" : ""} ${overlayMode ? "overlay-mode" : ""}`}>
    <header className="popover-header panel-drag-handle" onMouseDown={(event) => { if (event.button === 0 && !(event.target as HTMLElement).closest("button")) void getCurrentWindow().startDragging(); }} title={language === "ko" ? "이 영역을 드래그해 사이드카 위치를 변경할 수 있습니다" : "Drag this area to move the panel"}><div><div className="eyebrow">{ui("eyebrow")}</div><h1>{ui("title")}</h1><small className="panel-position-hint">{language === "ko" ? "헤더를 드래그하면 위치가 저장됩니다" : "Drag the header to save a new position"}</small></div><div className="header-actions">{(!overviewMode || dashboardOpen) && <button type="button" className="home-button" onClick={goHome} aria-label={ui("home")}>⌂ <span>{ui("home")}</span></button>}<button type="button" className="header-search-button" onClick={() => setSearchOpen((open) => !open)} aria-label={ui("search")}>⌕ <span>{ui("search")}</span></button><button className="icon-button" onClick={closePanel} aria-label={ui("close")}>×</button></div></header>
    <div className="popover-scroll">
    {dashboardOpen ? <DashboardView overview={overview} models={models} projects={projects} sessions={sessions} turns={turns} timeline={timeline} groupedAlerts={groupedAlerts} selectedModel={selectedModel} selectedProject={selectedProject} selectedSession={selectedSession} selected={selected} detail={detail} impact={impact} sessionInsights={sessionInsights} hierarchyRange={hierarchyRange} timelineRange={timelineRange} setTimelineRange={setTimelineRange} setBucketMinutes={setBucketMinutes} bucketMinutes={bucketMinutes} onSelectModel={chooseModel} onSelectProject={chooseProject} onSelectSession={chooseSession} onClearModel={() => setSelectedModel(null)} onClearProject={() => setSelectedProject(null)} onClearSession={() => setSelectedSession(null)} onOpenTurn={openTurn} onOpenAlert={openAlertSession} onClearDetail={() => { setSelected(null); setDetail(null); setImpact(null); }} sessionDisplayName={sessionDisplayName} promptDisplayName={promptDisplayName} /> : <>
    {contextMenuOpen && !settingsOpen && !alertsOpen && <section className="quick-menu-overlay" aria-label={ui("menuTitle")}><div className="quick-menu-head"><div><strong>{ui("menuTitle")}</strong><small>{selectedCharacter.name} · {error ? ui("statusWarning") : ui("statusNormal")}</small></div><button type="button" onClick={() => setContextMenuOpen(false)}>{ui("close")}</button></div><button type="button" className="quick-menu-item" onClick={openExplorer}><span>◈</span><span><strong>{ui("openDetail")}</strong><small>{ui("title")}에서 모델·프로젝트·세션·프롬프트를 탐색합니다.</small></span><b>›</b></button><button type="button" className="quick-menu-item" onClick={() => { setContextMenuOpen(false); setSearchOpen(true); setQuery(""); }}><span>⌕</span><span><strong>{ui("search")}</strong><small>프롬프트, 파일, 도구, 세션 메타데이터 검색</small></span><b>›</b></button><button type="button" className="quick-menu-item" onClick={() => { setContextMenuOpen(false); setAlertsOpen(true); }}><span>!</span><span><strong>{ui("recentAlerts")}</strong><small>{groupedAlerts.length}개 세션·유형 · {severity === "critical" ? ui("statusCritical") : severity === "warning" ? ui("statusWarning") : ui("statusNormal")}</small></span><b>›</b></button><div className="quick-menu-divider" /><button type="button" className="quick-menu-item" onClick={() => { setSettingsOpen(true); setContextMenuOpen(false); setAlertsOpen(false); }}><span>⚙</span><span><strong>{ui("settings")}</strong><small>{ui("language")} · {ui("petSize")} · {ui("position")}</small></span><b>›</b></button><button type="button" className="quick-menu-item" onClick={() => { setContextMenuOpen(false); setExpanded(false); }}><span>✥</span><span><strong>{ui("movePet")}</strong><small>펫을 길게 눌러 원하는 위치로 이동합니다.</small></span><b>›</b></button><button type="button" className="quick-menu-item" onClick={async () => { setContextMenuOpen(false); try { await fetch(`${API_BASE}/api/scan`, { method: "POST" }); setOverview(await get<Overview>("/api/overview")); } catch (reason) { setError((reason as Error).message); } }}><span>↻</span><span><strong>{ui("scanNow")}</strong><small>Claude Code와 Codex의 변경된 로컬 로그를 확인합니다.</small></span><b>›</b></button><div className="quick-menu-note">{ui("settingsHint")}</div></section>}
    {alertsOpen && !contextMenuOpen && !settingsOpen && <section className="alerts-overlay quick-menu-overlay" aria-label={ui("recentAlerts")}><div className="quick-menu-head"><div><strong>{ui("recentAlerts")}</strong><small>{groupedAlerts.length}개 세션·유형 · 반복 기록 {alerts.length}건을 묶어 표시</small></div><button type="button" onClick={() => setAlertsOpen(false)}>{ui("close")}</button></div>{groupedAlerts.length ? groupedAlerts.map((alert) => { const copy = friendlyAlert(alert, language); return <button type="button" className={`alert-detail-row ${alert.severity?.toLowerCase() === "critical" ? "critical" : "warning"}`} key={alertGroupKey(alert)} onClick={() => openAlertSession(alert)} disabled={!alert.session_id} title={alert.session_id ? "이 세션의 상세 분석 열기" : "세션 정보를 찾을 수 없습니다"}><span className="alert-badge">!</span><span><strong>{copy.title}</strong><small>{alert.agent_name || "에이전트 없음"} · {alert.project_name || "프로젝트 없음"} · 세션 {alert.external_session_id?.slice(0, 8) || "없음"}</small><em>{copy.description}</em><em className="alert-action">{copy.action}</em></span><time>{time(alert.timestamp)}</time><b className="alert-chevron">›</b></button>; }) : <div className="alert-detail-empty">{ui("noAlerts")}</div>}</section>}
    {settingsOpen && <section className="settings-overlay" aria-label={ui("settingsTitle")}><div className="settings-head"><button type="button" onClick={() => setSettingsOpen(false)}>← {ui("back")}</button><span>{ui("saved")}</span></div><h2>{ui("settingsTitle")}</h2><p>{ui("settingsDescription")}</p><div className="settings-group"><strong>{ui("general")}</strong><label><span>{ui("language")}</span><select value={language} onChange={(event) => setSettings((current) => ({ ...current, language: event.target.value as Language }))}><option value="ko">{ui("languageKo")}</option><option value="en">{ui("languageEn")}</option></select></label></div><div className="settings-group"><strong>{ui("pet")}</strong><label><span>{ui("characterAppearance")}</span><select value={characterId} onChange={(event) => setCharacterId(event.target.value as CharacterId)}>{CHARACTERS.map((character) => <option key={character.id} value={character.id}>{character.name} · {character.provider}</option>)}</select></label><label><span>{ui("petSize")} <em>{Math.round(settings.scale * 100)}%</em></span><input type="range" min=".7" max="1.5" step=".05" value={settings.scale} onChange={(event) => setSettings((current) => ({ ...current, scale: Number(event.target.value) }))} /></label><label><span>{ui("position")}</span><select value={settings.position} onChange={(event) => setSettings((current) => ({ ...current, position: event.target.value as PetPosition }))}><option value="top-left">{ui("positionTopLeft")}</option><option value="top-right">{ui("positionTopRight")}</option><option value="bottom-left">{ui("positionBottomLeft")}</option><option value="bottom-right">{ui("positionBottomRight")}</option><option value="last">{ui("positionLast")}</option></select></label><label className="toggle-row"><span>{ui("alwaysOnTop")}</span><input type="checkbox" checked={settings.alwaysOnTop} onChange={(event) => setSettings((current) => ({ ...current, alwaysOnTop: event.target.checked }))} /></label><label className="toggle-row"><span>{ui("showStatusBadge")}</span><input type="checkbox" checked={settings.showStatusBadge} onChange={(event) => setSettings((current) => ({ ...current, showStatusBadge: event.target.checked }))} /></label><label className="toggle-row"><span>{ui("showBubbles")}</span><input type="checkbox" checked={settings.showBubbles} onChange={(event) => setSettings((current) => ({ ...current, showBubbles: event.target.checked }))} /></label><button type="button" className="secondary-setting-button" onClick={() => { setSettings((current) => ({ ...current, position: "last" })); window.localStorage.removeItem("pet-position"); }}>{ui("resetPosition")}</button></div><div className="settings-group"><strong>{ui("collector")}</strong><div className="collector-status"><span>●</span><span>{ui("claudeCollector")} · {ui("collectorConnected")}</span></div><div className="collector-status"><span>●</span><span>{ui("codexCollector")} · {ui("collectorConnected")}</span></div><p className="privacy-note">{ui("privacyNote")}</p></div></section>}
    {!overlayMode && <>
    {showSearch && <form className="search-form" onSubmit={submit}><span className="search-icon">⌕</span><input autoFocus={showSearch} value={query} onChange={(event) => { setQuery(event.target.value); setSelected(null); }} placeholder={ui("searchPlaceholder")} /><kbd>{ui("searchShortcut")}</kbd><button type="button" className="search-close" onClick={() => { setSearchOpen(false); setQuery(""); }}>×</button></form>}
    <div className="status-row"><span className={`health-dot ${error ? "warning" : severity}`} /><span>{error ? `연결 대기 중 · ${error}` : overview ? ui("collectorConnected") : ui("collectorStarting")}</span><span className="status-spacer" /><span>{ui("today")} {compact(todayTotal)}</span><span>{ui("cache")} {percent(overview ? today.cache_hit_rate : null)}</span><span>{ui("alerts")} {groupedAlerts.length}</span></div>
    {dashboardOpen && <section className="dashboard-intro"><div><small>상세 분석</small><strong>어디에서 토큰이 늘었는지 찾아보세요</strong><span>모델 → 프로젝트 → 세션 → 프롬프트</span></div><div><span>provider가 기록한 실제 usage만 표시합니다.</span><button type="button" onClick={() => void closeDashboard()}>사이드카로 돌아가기</button></div></section>}
    {overviewMode && !!groupedAlerts.length && <section className="alert-summary" aria-label={ui("alerts")}><div className="alert-summary-head"><div><strong>주의가 필요한 사용량</strong><small>토큰이 평소보다 많이 쓰이거나 대화 맥락이 차오른 항목입니다.</small></div><span>{groupedAlerts.length}개 세션·유형</span></div>{groupedAlerts.slice(0, 2).map((alert) => { const copy = friendlyAlert(alert, language); const contextPressure = alert.metric === "context_utilization" && alert.current_value != null; return <button type="button" className={`alert-row ${alert.severity?.toLowerCase() === "critical" ? "critical" : "warning"}`} key={alertGroupKey(alert)} onClick={() => openAlertSession(alert)} disabled={!alert.session_id} title={alert.session_id ? "이 세션의 상세 분석 열기" : "세션 정보를 찾을 수 없습니다"}><span className="alert-badge">!</span><span className="alert-copy"><strong>{copy.title}</strong><small>{alert.agent_name || "에이전트 없음"} · {alert.project_name || "프로젝트 없음"} · 세션 {alert.external_session_id?.slice(0, 8) || "없음"}</small><em>{copy.description}</em><em className="alert-action">{copy.action}</em>{contextPressure && <span className="context-meter" aria-label={`컨텍스트 사용량 ${alert.current_value}%`}><i style={{ width: `${Math.min(100, Math.max(0, alert.current_value as number))}%` }} /></span>}</span><time>{time(alert.timestamp)}</time><b className="alert-chevron">›</b></button>; })}</section>}
    {!showSearch && !selected && !!models.length && <section className="model-leaderboard" aria-label="모델 및 프로바이더별 사용량"><div className="model-leaderboard-head"><div><strong>모델·프로바이더별 사용량</strong><small>같은 모델명도 제공자별로 따로 집계됩니다 · 행을 누르면 프로젝트로 이동합니다</small></div>{models.length > 4 && <button type="button" className="text-action" onClick={() => setShowAllModels((visible) => !visible)}>{showAllModels ? ui("showTopModels") : `${ui("allModels")} (${models.length})`}</button>}</div>{displayedModels.map((model) => <button type="button" className="model-leaderboard-row" key={modelKey(model.model, model.provider)} onClick={() => chooseModel(model)} aria-label={`${model.model} · ${model.agent_name || model.provider || "제공자 없음"} · ${compact(model.total_tokens ?? usageTotal(model))} · 상세 보기`}><span className="model-leaderboard-name"><strong>{model.model}</strong><small>{model.agent_name || model.provider || "제공자 없음"}</small></span><span className="model-leaderboard-bar"><i style={{ width: `${Math.max(3, ((model.total_tokens ?? usageTotal(model)) / Math.max(models[0]?.total_tokens ?? 1, 1)) * 100)}%` }} /></span><span className="model-leaderboard-total">{compact(model.total_tokens ?? usageTotal(model))}</span><span className="chevron">›</span></button>)}</section>}
    {!overview && <section className={`connection-card ${error ? "connection-error" : ""}`}><div className="connection-icon">{error ? "!" : "…"}</div><div className="connection-copy"><strong>{error ? "아직 사용량 데이터를 불러오지 못했습니다" : "로컬 수집기를 시작하는 중입니다"}</strong><span>{error ? "앱 시작 직후에는 몇 초가 걸릴 수 있습니다. Claude Code와 Codex 로그는 외부로 전송되지 않습니다." : "Claude Code와 Codex의 로컬 로그를 확인하고 있습니다."}</span></div><button type="button" onClick={retryOverview} disabled={loadingOverview}>{loadingOverview ? "확인 중…" : "다시 시도"}</button></section>}
    <section className="usage-summary" aria-label="오늘 토큰 사용량"><div><small>오늘 사용량</small><strong>{compact(todayTotal)}</strong><em>{overview ? `${today.actual_turns ?? 0}개 provider 기록 턴` : loadingOverview ? "데이터 수집 중" : "연결을 확인하세요"}</em></div><div><small>입력 토큰</small><strong>{compact(todayInput)}</strong><em>캐시 + 신규 · 원본 {compact(today.input_tokens)}</em></div><div><small>캐시된 입력</small><strong>{compact(overview ? today.cached_tokens : null)}</strong><em>캐시 적중률 {percent(overview ? today.cache_hit_rate : null)}</em></div><div><small>새로 들어온 입력</small><strong>{compact(overview ? today.fresh_tokens : null)}</strong><em>새 컨텍스트</em></div><div><small>출력 토큰</small><strong>{compact(overview ? today.output_tokens : null)}</strong><em>에이전트 응답 생성량</em></div><div><small>추론 토큰</small><strong>{compact(overview ? today.reasoning_tokens : null)}</strong><em>제공자 기록이 있을 때 표시</em></div><div><small>활성 세션</small><strong>{overview ? overview.active_sessions?.length ?? 0 : "—"}</strong><em>현재 작업 중</em></div><div><small>가장 많이 쓴 프로젝트</small><strong>{topProject ? compact(topProjectTotal) : "—"}</strong><em>{topProject ? `${topProject.project_name || "프로젝트 없음"} · 세션 ${topProject.session_count ?? 0}개` : "프로젝트 데이터 없음"}</em></div><div><small>가장 많이 쓴 세션</small><strong>{topSession ? compact(topSessionTotal) : "—"}</strong><em>{topSession ? sessionDisplayName(topSession) : "세션 데이터 없음"}</em></div><div><small>알림</small><strong>{overview ? groupedAlerts.length : "—"}</strong><em>{overview ? `세션·유형 기준 · 원본 기록 ${overview.alerts?.total ?? 0}건` : "연결 상태 확인 필요"}</em></div><div><small>비용</small><strong>{cost(overview ? today.estimated_cost : null)}</strong><em>provider 과금 정보 없음</em></div></section>
    {!showSearch && !selected && !!todayAgents.length && <section className="today-agents" aria-label="오늘 에이전트별 토큰 사용량"><div className="today-agents-head"><strong>오늘 에이전트별 사용량</strong><span>provider 기록</span></div>{todayAgents.map((agent) => <div className="today-agent-row" key={agent.name}><span className="today-agent-name"><strong>{agent.name}</strong><small className={`usage-quality ${usageQualityClass(usageQuality(agent))}`}>{usageQuality(agent)}</small></span><span>입력 {compact(inputTotal(agent))}</span><span>캐시 {compact(agent.cached_tokens)} · 신규 {compact(agent.fresh_tokens)}</span><span>출력 {compact(agent.output_tokens)}</span><span>추론 {compact(agent.reasoning_tokens)}</span><span>{cost(agent.estimated_cost)}</span></div>)}</section>}
    <section className="results" aria-live="polite">
    {!showSearch && !selected && !overviewMode && <div className="exploration-context"><span className="exploration-kicker">현재 탐색 단계 · {explorationLevel}</span><strong>{explorationTitle}</strong><small>{selectedSession ? "provider가 기록한 세션 실제 usage와 연결 가능한 범위를 확인합니다." : activeProject ? "이 프로젝트의 세션별 실제 사용량과 점유율을 비교합니다." : "행을 클릭하면 다음 단계로 이동합니다. 각 단계의 토큰 합계는 현재 선택한 기간 기준입니다."}</small><small className="rename-hint">세션 이름은 더블클릭하거나 우클릭하면 화면용 이름을 지정할 수 있습니다.</small></div>}
    {!showSearch && !selected && activeProject && !selectedSession && <button type="button" className="model-context-back" onClick={() => { setSelectedProject(null); setSelectedSession(null); }} aria-label={ui("projectList")}>← {ui("projectList")}</button>}
    {!showSearch && !selected && !activeAgent && !selectedModel && !!models.length && <div className="hierarchy-group model-group"><div className="group-title"><span>전체 모델</span><small>제공자별로 구분 · 행을 클릭하면 프로젝트로 이동</small></div>{models.map((model) => <button type="button" className="hierarchy-row" key={modelKey(model.model, model.provider)} onClick={() => chooseModel(model)}><span className="row-icon model-icon">M</span><span className="row-main"><strong>{model.model}</strong><small className="provider-label">{model.agent_name || model.provider || "제공자 없음"}</small><MetricLine input={model.effective_input_tokens ?? model.input_tokens} cached={model.cached_tokens} fresh={model.fresh_tokens} output={model.output_tokens} reasoning={model.reasoning_tokens} cache={model.cache_hit_rate} /><small><span className={`usage-quality ${usageQualityClass(usageQuality(model))}`}>{usageQuality(model)}</span> · {model.turns ?? 0}턴 · {cost(model.estimated_cost)}</small></span><span className="row-total">{compact(model.total_tokens ?? usageTotal(model))}</span><span className="chevron">›</span></button>)}</div>}
    {!showSearch && !selected && selectedModel && !activeProject && <><ProjectShareChart projects={visibleProjects} onSelectProject={chooseProject} /><div className="hierarchy-group model-projects"><div className="group-title"><span>{activeModel?.model || "선택한 모델"} 프로젝트</span><small>{activeModel?.agent_name || activeModel?.provider || "제공자 없음"} · 프로젝트를 선택하면 세션으로 이동</small><button type="button" className="model-back" onClick={() => setSelectedModel(null)} aria-label={ui("modelList")}>← {ui("modelList")}</button></div>{visibleProjects.map((project) => <button className="hierarchy-row" key={project.id} onClick={() => chooseProject(project.id)}><span className="row-icon project-icon">⌂</span><span className="row-main"><strong>{project.project_name || "프로젝트 없음"}</strong><MetricLine input={inputTotal(project)} cached={project.cached_tokens} fresh={project.fresh_tokens} output={project.output_tokens} reasoning={project.reasoning_tokens} /><small>{project.session_count ?? 0}개 세션 · {cost(project.estimated_cost)}</small></span><span className="row-total">{compact(usageTotal(project))}</span><span className="chevron">›</span></button>)}</div></>}
        {!showSearch && !selected && <div className="hierarchy-view"><div className="hierarchy-heading"><div><strong>사용량 탐색</strong><span>모델 → 프로젝트 → 세션 → 프롬프트</span></div><select value={hierarchyRange} onChange={(event) => setHierarchyRange(event.target.value as "today" | "7d" | "all")} aria-label="사용량 기간"><option value="today">오늘</option><option value="7d">7일</option><option value="all">전체</option></select><span className={`usage-quality ${usageQualityClass(hierarchyQuality)}`}>{hierarchyQuality}</span></div><div className="breadcrumb"><button className={!selectedAgent && !selectedModel ? "current" : ""} onClick={() => { setSelectedAgent(null); setSelectedModel(null); setSelectedProject(null); setSelectedSession(null); }}>전체 모델</button>{selectedModel && <><span>›</span><button className={!activeProject ? "current" : ""} onClick={() => { setSelectedProject(null); setSelectedSession(null); }}>{activeModel?.agent_name || activeModel?.provider || "제공자 없음"} · {activeModel?.model || "모델"}</button></>}{activeAgent && <><span>›</span><button className={!selectedProject ? "current" : ""} onClick={() => { setSelectedProject(null); setSelectedSession(null); }}>{activeAgent.name}</button></>}{activeProject && <><span>›</span><button className={!selectedSession ? "current" : ""} onClick={() => setSelectedSession(null)}>{activeProject.project_name}</button></>}{selectedSession && <><span>›</span><span>{sessionDisplayName(sessions.find((session) => session.id === selectedSession) ?? { id: selectedSession, external_session_id: selectedSession })}</span></>}</div>
        {!activeAgent && !selectedModel && <div className="timeline-card"><div className="timeline-card-head"><div><strong>모델·프로바이더별 토큰 추이</strong><small>선 위 점에 마우스를 올리면 시간대별 사용량을 확인할 수 있습니다.</small></div><div className="timeline-controls"><select value={timelineRange} onChange={(event) => setTimelineRange(event.target.value as "today" | "7d" | "all")} aria-label="Timeline range"><option value="today">오늘</option><option value="7d">7일</option><option value="all">전체</option></select><select value={bucketMinutes} onChange={(event) => setBucketMinutes(Number(event.target.value))} aria-label="Timeline bucket"><option value="5">5분</option><option value="15">15분</option><option value="60">1시간</option><option value="1440">1일</option></select></div></div>{timeline.length ? <LineChart points={timelineChartPoints} series={timelineChartSeries} onSeriesSelect={(key) => { const model = models.find((item) => modelKey(item.model, item.provider) === key); if (model) chooseModel(model); }} /> : <div className="timeline-empty">선택한 기간의 사용량이 없습니다.</div>}</div>}
        {!activeAgent && !selectedModel && <div className="hierarchy-group agent-root-group"><div className="group-title"><span>에이전트별 사용량</span><small>{agents.length}개 · 선택한 기간</small></div>{agents.map((agent) => { const quality = usageQuality(agent); return <button type="button" className="hierarchy-row" key={agent.id} onClick={() => chooseAgent(agent.name)}><span className="row-icon agent-icon">●</span><span className="row-main"><strong>{agent.name}</strong><MetricLine input={inputTotal(agent)} cached={agent.cached_tokens} fresh={agent.fresh_tokens} output={agent.output_tokens} reasoning={agent.reasoning_tokens} cache={agent.cache_hit_rate} /><small><span className={`usage-quality ${usageQualityClass(quality)}`}>{quality}</span> · {cost(agent.estimated_cost)}</small></span><span className="row-total">{compact(usageTotal(agent))}</span><span className="chevron">›</span></button>; })}</div>}
        {activeAgent && !activeProject && <div className="hierarchy-group"><div className="group-title"><span>{activeAgent.name}의 프로젝트</span><small>{visibleProjects.length}개 · 선택한 기간</small></div>{visibleProjects.map((project) => { const quality = usageQuality(project); return <button type="button" className="hierarchy-row" key={project.id} onClick={() => chooseProject(project.id)}><span className="row-icon project-icon">⌂</span><span className="row-main"><strong>{project.project_name || "프로젝트 없음"}</strong><MetricLine input={inputTotal(project)} cached={project.cached_tokens} fresh={project.fresh_tokens} output={project.output_tokens} reasoning={project.reasoning_tokens} /><small><span className={`usage-quality ${usageQualityClass(quality)}`}>{quality}</span> · 세션 {project.session_count ?? 0}개 · {cost(project.estimated_cost)}</small></span><span className="row-total">{compact(usageTotal(project))}</span><span className="chevron">›</span></button>; })}</div>}
        {activeProject && !selectedSession && <><div className="session-share-panel"><div className="group-title"><span>{activeProject.project_name}의 세션 분포</span><small>실제 토큰 점유율 · 조각을 누르면 세션으로 이동</small></div><SessionShareChart sessions={visibleSessions} onSelectSession={chooseSession} sessionDisplayName={sessionDisplayName} /></div><div className="hierarchy-group"><div className="group-title"><span>{activeProject.project_name}의 세션</span><small>{visibleSessions.length}개 · 선택한 기간</small></div>{visibleSessions.map((session) => { const effectiveInput = (session.total_cached_input_tokens ?? 0) + (session.total_fresh_input_tokens ?? 0) || (session.total_input_tokens ?? 0); const quality = usageQuality(session); return <button type="button" className="hierarchy-row" key={session.id} title="더블클릭 또는 우클릭하여 세션 표시 이름 설정" onClick={() => chooseSession(session.id)} onDoubleClick={() => renameSession(session)} onContextMenu={(event) => { event.preventDefault(); renameSession(session); }}><span className={`row-icon status-icon ${session.status === "ACTIVE" ? "live" : ""}`}>◉</span><span className="row-main"><strong>{sessionDisplayName(session)}</strong><MetricLine input={effectiveInput} cached={session.total_cached_input_tokens} fresh={session.total_fresh_input_tokens} output={session.total_output_tokens} reasoning={session.total_reasoning_tokens} cache={session.cache_hit_rate} /><small><span className={`usage-quality ${usageQualityClass(quality)}`}>{quality}</span> · 모델 {session.model || "모델 정보 없음"} · {session.status || "상태 없음"} · 컨텍스트 {percent(session.context_utilization)} · {cost(session.estimated_cost)}</small></span><span className="row-total">{compact(effectiveInput + (session.total_output_tokens ?? 0))}</span><span className="chevron">›</span></button>; })}</div></>}
        {selectedSession && <button type="button" className="model-context-back" onClick={() => setSelectedSession(null)} aria-label={ui("sessionList")}>← {ui("sessionList")}</button>}
        {selectedSession && sessionInsights && <div className="session-insights"><div className="insight-head"><strong>세션 진단</strong><span className="actual">provider 기록 기준</span></div><div className="insight-grid"><div><small>실효 입력</small><strong>{compact(sessionInsights.effective_input_tokens)}</strong></div><div><small>캐시 적중</small><strong>{percent(sessionInsights.cache_hit_rate)}</strong></div><div><small>실제 출력</small><strong>{compact(sessionInsights.output_tokens)}</strong></div><div><small>비용</small><strong>확인 불가</strong></div></div><div className="turn-chart" aria-label="턴별 입력 토큰 추이">{turns.slice(-32).map((turn) => { const amount = (turn.cached_input_tokens ?? 0) + (turn.fresh_input_tokens ?? 0) || (turn.input_tokens ?? 0); const max = Math.max(...turns.map((item) => (item.cached_input_tokens ?? 0) + (item.fresh_input_tokens ?? 0) || (item.input_tokens ?? 0)), 1); const spike = sessionInsights.spikes?.some((item) => item.turn_id === turn.id); return <span key={turn.id} className={spike ? "spike" : ""} style={{ height: `${Math.max(5, amount / max * 100)}%` }} title={`${time(turn.timestamp)} · ${compact(amount)} input`} />; })}</div>{!!sessionInsights.context_attribution?.length && <div className="attribution-summary"><small>확인된 컨텍스트 활동 · token 귀속 없음</small>{sessionInsights.context_attribution.slice(0, 6).map((item) => <div key={item.type}><span>{item.type || "기타"}</span><span>{item.items ?? 0}개 이벤트 · token 수 확인 불가</span></div>)}</div>}<div className="insight-foot"><span>{sessionInsights.tool_calls ?? 0}회 도구 호출 · provider별 tool token 귀속 없음</span><span>{sessionInsights.alerts ?? 0}개 알림</span></div>{!!sessionInsights.repeated_contexts?.length && <div className="repeated-summary"><small>반복 확인된 컨텍스트</small>{sessionInsights.repeated_contexts.slice(0, 3).map((item, index) => <div key={`${item.file_path}-${index}`}><span>{item.file_path || item.tool_name || item.type || "컨텍스트"}</span><span>{item.injected_count}회 · 반복 사실만 확인</span></div>)}</div>}</div>}
        {selectedSession && !!sessionTimeline.length && <div className="session-timeline"><div className="group-title"><span>세션 타임라인</span><small>턴 → 컨텍스트 / 도구 · 이벤트 기록</small></div>{sessionTimeline.slice(-24).map((event) => <div className={`session-timeline-row ${event.event_type === "turn" ? "turn-event" : "side-event"}`} key={event.event_id}><span className="timeline-dot">{event.event_type === "turn" ? "↳" : event.event_type === "tool_call" ? "⌘" : "◈"}</span><span className="row-main"><strong>{event.title || "이벤트"}</strong><small>{time(event.timestamp)} · {event.event_type === "context_item" ? event.context_type || "컨텍스트" : event.event_type === "tool_call" ? event.target || "도구 호출" : "제공자 턴"}</small></span><span className="row-total">{event.event_type === "turn" ? compact((event.effective_input_tokens ?? 0) + (event.output_tokens ?? 0)) : compact(event.token_count)}<small>{event.token_count_available ? " provider 기록" : " token 수 없음"}</small></span></div>)}</div>}
        {selectedSession && <div className="hierarchy-group"><div className="group-title"><span>프롬프트별 사용량</span><small>{turns.length}개 · 전체 토큰</small></div>{turns.map((turn, index) => { const effectiveInput = (turn.cached_input_tokens ?? 0) + (turn.fresh_input_tokens ?? 0) || (turn.input_tokens ?? 0); return <button className="hierarchy-row" key={turn.id} title="더블클릭 또는 우클릭하여 프롬프트 표시 이름 설정" onClick={() => openTurn(turn, index)} onDoubleClick={() => renamePrompt(turn, index)} onContextMenu={(event) => { event.preventDefault(); renamePrompt(turn, index); }}><span className="row-icon turn-icon">{index + 1}</span><span className="row-main"><strong>{promptDisplayName(turn, index)}</strong><MetricLine input={effectiveInput} cached={turn.cached_input_tokens ?? undefined} fresh={turn.fresh_input_tokens ?? undefined} output={turn.output_tokens ?? undefined} /><small>{time(turn.timestamp)} · {"provider 기록"}</small></span><span className="row-total">{compact(effectiveInput + (turn.output_tokens ?? 0))}</span><span className="chevron">›</span></button>; })}</div>}
        {!activeAgent && !selectedModel && <div className="hierarchy-hint">모델을 선택하면 프로젝트 → 세션 순서로 좁혀볼 수 있습니다. Codex가 지정한 세션 제목이 있으면 자동으로 표시됩니다.</div>}
      </div>}
      {selected && <div className="detail-view"><button className="back-button" onClick={() => setSelected(null)}>← 탐색으로 돌아가기</button><div className="detail-title"><span className="result-icon">↳</span><span><strong>{selected.title || "프롬프트 / 이벤트"}</strong><small>{selected.agent_name || "에이전트 정보 없음"} · {selected.project_name || "프로젝트 정보 없음"}</small></span></div>{!detail && <div className="empty-state compact-empty">프롬프트 사용량을 불러오는 중…</div>}{detail && <><div className="detail-explanation">provider가 기록한 실제 턴 사용량과 이 턴에 연결된 활동을 보여줍니다. 활동별 token 귀속은 provider 로그에 없어 계산하지 않습니다.</div><div className="detail-meta"><span>{time(detail.timestamp)}</span><span>{detail.model || "모델 정보 없음"}</span><span className="actual">provider 기록</span></div><div className="token-grid"><div><small>실효 입력</small><strong>{compact((detail.cached_input_tokens ?? 0) + (detail.fresh_input_tokens ?? 0) || detail.input_tokens)}</strong></div><div><small>캐시 입력</small><strong>{compact(detail.cached_input_tokens)}</strong></div><div><small>신규 입력</small><strong>{compact(detail.fresh_input_tokens)}</strong></div><div><small>출력</small><strong>{compact(detail.output_tokens)}</strong></div><div><small>추론</small><strong>{compact(detail.reasoning_tokens)}</strong></div><div><small>비용</small><strong>{cost(detail.estimated_cost)}</strong></div></div><div className="detail-note">입력 {compact(detail.input_before_tokens)} → {compact(detail.input_after_tokens ?? detail.input_tokens)} · 증가 {compact(detail.input_delta_tokens)} · 새 컨텍스트 {compact(detail.fresh_context_added_tokens)} · 제공자 원본 입력 {compact(detail.input_tokens)}</div>{detail.tool_summary && <div className="analysis-summary"><span>도구 {detail.tool_summary.tools_used ?? 0}개</span><span>파일 {detail.tool_summary.files_read ?? 0}개</span><span>MCP {detail.tool_summary.mcp_calls ?? 0}회</span><span>서브에이전트 {detail.tool_summary.subagents ?? 0}개</span><span>tool별 token 귀속 없음</span></div>}{impact && <div className="impact-card"><div className="impact-head"><strong>이 프롬프트와 반복 활동</strong><span className="actual">확인된 이벤트</span></div><div className="impact-summary"><div><small>이후 반복 확인된 턴</small><strong>{impact.persistent_context_turns ?? 0}</strong></div><div><small>반복 token 영향</small><strong>확인 불가</strong></div><div><small>절감 가능량</small><strong>계산하지 않음</strong></div></div>{!!impact.items?.length && <div className="detail-list"><small>반복 확인된 content hash</small>{impact.items.slice(0, 5).map((item, index) => <div key={`${item.file_path || item.tool_name || item.type}-${index}`}><span>{item.file_path || item.tool_name || item.type || "컨텍스트"}</span><span>{item.injected_count}회 · token 영향 확인 불가</span></div>)}</div>}{!!impact.attribution_note && <div className="impact-note">{impact.attribution_note}</div>}{!impact.items?.length && <div className="impact-empty">이후 턴에서 반복된 content hash가 확인되지 않았습니다.</div>}</div>}{!!detail.tool_calls?.length && <div className="detail-list"><small>이 프롬프트에서 확인된 도구</small>{detail.tool_calls.map((tool, index) => <div key={`${tool.tool_name}-${index}`}><span>{tool.tool_name || "도구 정보 없음"}</span><span>{tool.target || "대상 정보 없음"} · token 영향 확인 불가</span></div>)}</div>}{!!detail.context_items?.length && <div className="detail-list"><small>이 턴에서 확인된 컨텍스트</small>{detail.context_items.slice(0, 8).map((item, index) => <div key={`${item.type}-${index}`}><span>{item.file_path || item.tool_name || item.type || "컨텍스트"}</span><span>token 영향 확인 불가</span></div>)}</div>}</>}</div>}
      {showSearch && !selected && searching && <div className="empty-state">검색 중…</div>}{showSearch && !selected && !searching && !results.length && <div className="empty-state"><strong>검색 결과가 없습니다</strong><span>기본 설정에서는 저장된 메타데이터만 검색합니다.</span></div>}{showSearch && !selected && !searching && results.map((result) => <button className="result" key={`${result.match_kind}-${result.result_id}`} onClick={() => openResult(result)}><span className="result-icon">{result.match_kind === "Tool call" ? "⌘" : result.match_kind === "Context item" ? "◈" : result.match_kind === "Alert" ? "!" : "↳"}</span><span className="result-main"><strong>{result.title || "Untitled"}</strong><span>{result.agent_name || "Unknown"} · {result.project_name || "Unknown project"}</span></span><span className="result-meta"><span>{result.match_kind}</span><span>{time(result.timestamp)}</span></span></button>)}
    </section>
    </>}
    </>}
    </div>
      <footer className="popover-footer"><span>{language === "ko" ? "모델 → 프로젝트 → 세션 → 프롬프트 순서로 자세히 확인할 수 있습니다" : "Explore usage from model to project, session, and prompt"}</span><span>Esc {ui("close")}</span>{dashboardOpen ? <button type="button" title="사이드카로 돌아가기" onClick={() => void closeDashboard()}>사이드카로 돌아가기</button> : <button type="button" title="상세 분석 창 열기" onClick={() => void openDashboard()}>상세 분석 열기 ↗</button>}<button type="button" className="popover-close-button" onClick={closePanel}>{ui("close")}</button></footer>
  </main>;
}

export default App;
