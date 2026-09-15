export type Language = "ko" | "en";

export type I18nKey =
  | "eyebrow" | "title" | "searchPlaceholder" | "searchShortcut" | "collectorConnected"
  | "collectorStarting" | "connectionWaiting" | "connectionStarting" | "retry"
  | "today" | "cache" | "alerts" | "characterAppearance" | "currentSelection"
  | "clickToChange" | "selected" | "openDetail" | "todayUsage" | "search"
  | "recentAlerts" | "settings" | "scanNow" | "movePet" | "close" | "back"
  | "allModels" | "modelList" | "projectList" | "sessionList" | "promptList"
  | "showTopModels" | "alertsDescription" | "noAlerts" | "openFullDashboard" | "navigationPath" | "home"
  | "settingsTitle" | "settingsDescription" | "general" | "language" | "languageKo"
  | "languageEn" | "pet" | "petSize" | "position" | "positionTopLeft" | "positionTopRight"
  | "positionBottomLeft" | "positionBottomRight" | "positionLast" | "alwaysOnTop"
  | "showStatusBadge" | "showBubbles" | "resetPosition" | "saved" | "collector"
  | "claudeCollector" | "codexCollector" | "privacyNote" | "settingsHint" | "menuTitle"
  | "statusNormal" | "statusWarning" | "statusCritical" | "dashboard";

const TEXT: Record<Language, Record<I18nKey, string>> = {
  ko: {
    eyebrow: "로컬 AI 관제", title: "TokenPaw", searchPlaceholder: "검색하거나 아래 계층을 탐색하세요", searchShortcut: "Ctrl K",
    collectorConnected: "로컬 수집기 연결됨", collectorStarting: "로컬 수집기 시작 중…", connectionWaiting: "아직 사용량 데이터를 불러오지 못했습니다",
    connectionStarting: "로컬 수집기를 시작하는 중입니다", retry: "다시 시도", today: "오늘", cache: "캐시", alerts: "알림",
    characterAppearance: "펫 외형", currentSelection: "현재 선택", clickToChange: "클릭해 변경", selected: "선택됨",
    openDetail: "상세 관제 열기", todayUsage: "오늘 사용량 보기", search: "검색하기", recentAlerts: "최근 알림",
    settings: "펫 설정", scanNow: "지금 로그 스캔", movePet: "이동 모드", close: "닫기", back: "뒤로",
    allModels: "전체 모델", modelList: "모델 목록", projectList: "프로젝트 목록", sessionList: "세션 목록", promptList: "프롬프트 목록",
    showTopModels: "상위 4개만", alertsDescription: "확인이 필요한 사용량 이상 징후", noAlerts: "현재 확인할 알림이 없습니다.",
    openFullDashboard: "브라우저에서 전체 대시보드 열기", navigationPath: "사용량 탐색 경로", home: "홈",
    settingsTitle: "펫 설정", settingsDescription: "화면에 표시되는 펫과 관제 화면의 동작을 설정합니다.", general: "일반", language: "언어",
    languageKo: "한국어", languageEn: "English", pet: "펫", petSize: "펫 크기", position: "기본 위치",
    positionTopLeft: "왼쪽 위", positionTopRight: "오른쪽 위", positionBottomLeft: "왼쪽 아래", positionBottomRight: "오른쪽 아래", positionLast: "마지막 위치",
    alwaysOnTop: "항상 위에 표시", showStatusBadge: "상태 배지 표시", showBubbles: "상태 말풍선 표시", resetPosition: "위치 초기화", saved: "자동 저장됨",
    collector: "수집기", claudeCollector: "Claude Code 로그", codexCollector: "Codex 로그", privacyNote: "프롬프트는 민감정보를 가린 뒤 로컬에 저장합니다. 코드와 도구 출력 원문은 저장하지 않습니다.",
    settingsHint: "설정은 이 컴퓨터에만 저장됩니다.", menuTitle: "빠른 메뉴", statusNormal: "정상", statusWarning: "주의", statusCritical: "긴급", dashboard: "전체 대시보드",
  },
  en: {
    eyebrow: "LOCAL AI OBSERVABILITY", title: "TokenPaw", searchPlaceholder: "Search or explore the hierarchy below", searchShortcut: "Ctrl K",
    collectorConnected: "Local collector connected", collectorStarting: "Starting local collector…", connectionWaiting: "Usage data is not available yet",
    connectionStarting: "Starting the local collector", retry: "Retry", today: "Today", cache: "Cache", alerts: "Alerts",
    characterAppearance: "Pet appearance", currentSelection: "Selected", clickToChange: "Click to change", selected: "Selected",
    openDetail: "Open detailed view", todayUsage: "View today’s usage", search: "Search", recentAlerts: "Recent alerts",
    settings: "Pet settings", scanNow: "Scan logs now", movePet: "Move pet", close: "Close", back: "Back",
    allModels: "All models", modelList: "Model list", projectList: "Project list", sessionList: "Session list", promptList: "Prompt list",
    showTopModels: "Show top 4", alertsDescription: "Usage anomalies that need attention", noAlerts: "There are no alerts to review.",
    openFullDashboard: "Open full dashboard in browser", navigationPath: "Usage navigation path", home: "Home",
    settingsTitle: "Pet settings", settingsDescription: "Choose how the pet and local observability panel behave.", general: "General", language: "Language",
    languageKo: "한국어", languageEn: "English", pet: "Pet", petSize: "Pet size", position: "Default position",
    positionTopLeft: "Top left", positionTopRight: "Top right", positionBottomLeft: "Bottom left", positionBottomRight: "Bottom right", positionLast: "Last position",
    alwaysOnTop: "Always on top", showStatusBadge: "Show status badge", showBubbles: "Show status bubble", resetPosition: "Reset position", saved: "Saved automatically",
    collector: "Collectors", claudeCollector: "Claude Code logs", codexCollector: "Codex logs", privacyNote: "Prompts are stored locally after masking sensitive data. Source and tool output contents are not stored.",
    settingsHint: "Settings are stored on this computer only.", menuTitle: "Quick menu", statusNormal: "Normal", statusWarning: "Warning", statusCritical: "Critical", dashboard: "Full dashboard",
  },
};

export function t(language: Language, key: I18nKey): string {
  return TEXT[language][key] ?? TEXT.ko[key];
}
