const manifest = { name: "Hangul IME Bridge" };
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error("[Hangul IME Bridge] Decky Loader API is not available");
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
}
const callable = api.callable;
const getState = callable("get_state");
const setGameInput = callable("set_game_input");
const setGameEnabled = callable("set_game_enabled");
const setToggleKey = callable("set_toggle_key");
const setWakeWait = callable("set_wake_wait_ms");
const setGameSetting = callable("set_game_setting");
const removeGame = callable("remove_game");
const setKeepKeyboard = callable("set_keep_keyboard_on_enter");
const setOpacity = callable("set_keyboard_opacity");
const setPaused = callable("set_paused");
const setBackspaceHold = callable("set_backspace_hold_ms");
const setSendMethod = callable("set_send_method");
const setPasteGap = callable("set_paste_gap_ms");
const setHangulMode = callable("set_hangul_mode");
const getKeyboardMode = callable("get_keyboard_mode");
const setKeyboardHangul = callable("set_keyboard_hangul");
const setAccount = callable("set_account");
const claimGames = callable("claim_games");
const getBatchText = callable("get_batch_text");
const setBatchRowMissing = callable("set_batch_row_missing");

const XIM_OPTION = "XMODIFIERS=@im=hangulbridge";
const KEYBOARD_OPACITY_MIN = 30;
const KEYBOARD_OPACITY_MAX = 100;
const KEYBOARD_OPACITY_STEP = 5;
const BACKSPACE_HOLD_STEP_MS = 5;
const BACKSPACE_HOLD_MIN_MS = 10;
const BACKSPACE_HOLD_MAX_MS = 100;
const BACKSPACE_HOLD_DEFAULT_MS = 50;
const PASTE_GAP_STEP_MS = 5;
const PASTE_GAP_MIN_MS = 20;
const PASTE_GAP_MAX_MS = 70;
const PASTE_GAP_DEFAULT_MS = 35;
const WAKE_WAIT_STEP_MS = 50;
const WAKE_WAIT_MIN_MS = 0;
const WAKE_WAIT_MAX_MS = 1000;
const WAKE_WAIT_DEFAULT_MS = 500;
const SHORTCUT_APP_TYPE = 1073741824;
const STATUS_ON_COLOR = "#4caf50";
const STATUS_OFF_COLOR = "#f44336";
const STATUS_IDLE_COLOR = "#8b929a";
const STATUS_WARN_COLOR = "#ffc107";
const ADVANCED_OPEN_KEY = "hangul-ime-advanced-open";
const LOCALE_OPTION = "HOST_LC_ALL=ko_KR.UTF-8";
const XIM_OPTION_SET = /(^|\s)XMODIFIERS=@im=hangulbridge(?=\s|$)/;
const ACCOUNT_RETRY_MS = 3000;
const NOT_LOGGED_IN_STEAM_ID = "76561197960265728";  // Steam reports this until login ends
const isLoggedInSteamId = (id) => /^\d+$/.test(id ?? "") && BigInt(id) > BigInt(NOT_LOGGED_IN_STEAM_ID);
const FIRST_SHORTCUT_APP_ID = 0x80000000;
const LOCALE_OPTION_SET = /(^|\s)HOST_LC_ALL=ko_KR\.UTF-8(?=\s|$)/;
const ANY_LOCALE_OPTION = /(^|\s)HOST_LC_ALL=\S*(?=\s|$)/g;
// Leaves 16px between a section's dividing line and the next section's title.
const SECTION_PULLED_UP = { marginTop: "-8.5px" };
// The section right under the 고급 button, which has no dividing line above it.
const SECTION_UNDER_BUTTON = { marginTop: "-16px" };
// Notes of more than one line sit closer together than ordinary text.
const NOTE_LINE_HEIGHT = "15px";
const h = SP_REACT.createElement;

function readAdvancedOpen() {
    try {
        return window.localStorage.getItem(ADVANCED_OPEN_KEY) === "1";
    }
    catch {
        return false;
    }
}

function saveAdvancedOpen(open) {
    try {
        window.localStorage.setItem(ADVANCED_OPEN_KEY, open ? "1" : "0");
    }
    catch {
        /* it just opens closed next time */
    }
}

function addOption(options) {
    if (options.includes("XMODIFIERS=")) return options;
    if (options.includes("%command%")) return `${XIM_OPTION} ${options}`;
    return options ? `${XIM_OPTION} %command% ${options}` : `${XIM_OPTION} %command%`;
}

function removeOption(options) {
    const cleaned = options.replace(/(^|\s)XMODIFIERS=@im=hangulbridge(?=\s|$)/, "").trim();
    return cleaned === "%command%" ? "" : cleaned;
}

function readLaunchOptions(appId, isShortcut) {
    return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
            registration?.unregister();
            reject(new Error("실행 옵션을 읽지 못했어요"));
        }, 5000);
        const registration = SteamClient.Apps.RegisterForAppDetails(appId, (details) => {
            clearTimeout(timeout);
            setTimeout(() => registration?.unregister(), 0);
            resolve((isShortcut ? details.strShortcutLaunchOptions : details.strLaunchOptions) || "");
        });
    });
}

async function applyLaunchOptions(app, enabled) {
    const isShortcut = app.app_type === SHORTCUT_APP_TYPE;
    const current = await readLaunchOptions(app.appid, isShortcut);
    const next = enabled ? addOption(current) : removeOption(current);
    if (next === current) return;
    if (isShortcut) SteamClient.Apps.SetShortcutLaunchOptions(app.appid, next);
    else SteamClient.Apps.SetAppLaunchOptions(app.appid, next);
}

let accountReporting = false;
let accountTimer = null;

// Tell the backend whose games to use. Changing account restarts Steam and this
// plugin with it, so this is needed only until it has worked once. The older shared list goes by launch options,
// which each account keeps for itself: a game with the option is that account's.
async function reportAccount() {
    const account = window.App?.m_CurrentUser?.strSteamID;
    if (!isLoggedInSteamId(account) || accountReporting) return;
    accountReporting = true;
    try {
        const unclaimed = await setAccount(account);
        const options = await Promise.all(unclaimed.map(
            (app) => readLaunchOptions(Number(app), Number(app) >= FIRST_SHORTCUT_APP_ID)));
        const claimed = unclaimed.filter((_, index) => XIM_OPTION_SET.test(options[index]));
        if (claimed.length) await claimGames(claimed);
        clearInterval(accountTimer);
    }
    catch {
        accountReporting = false;  // the timer tries again
    }
}

function withLocaleOption(options, enabled) {
    // A language set by hand is replaced, as two would clash.
    const others = options.replace(ANY_LOCALE_OPTION, "").trim();
    if (!enabled) return others === "%command%" ? "" : others;
    if (others.includes("%command%")) return `${LOCALE_OPTION} ${others}`;
    return others ? `${LOCALE_OPTION} %command% ${others}` : `${LOCALE_OPTION} %command%`;
}

async function applyLocaleOption(app, enabled) {
    const isShortcut = app.app_type === SHORTCUT_APP_TYPE;
    const current = await readLaunchOptions(app.appid, isShortcut);
    const next = withLocaleOption(current, enabled);
    if (next === current) return;
    if (isShortcut) SteamClient.Apps.SetShortcutLaunchOptions(app.appid, next);
    else SteamClient.Apps.SetAppLaunchOptions(app.appid, next);
}

function registeredGames(state) {
    return Object.keys(state?.games ?? {}).sort();
}

function statusText(state, isEnabled, app) {
    if (!app) return "게임을 실행하면 여기서 켤 수 있어요.";
    if (!isEnabled) return "꺼져 있어요.";
    if (state?.paused) return "일시 중지 중이에요.";
    if (state?.active && state?.xim_running) return "한글 입력 동작 중이에요.";
    if (!state?.xim_running) return "입력 서버를 준비하는 중이에요.";
    return "이 게임으로 돌아오면 한글 조합이 시작돼요.";
}

function gameName(appId) {
    let overview = null;
    try {
        overview = window.appStore?.GetAppOverviewByAppID?.(Number(appId));
    }
    catch {
        /* fall back to the id below */
    }
    return overview?.display_name || `앱 ID ${appId}`;
}

function processStatus(process, state) {
    if (!state?.games?.[process.app_id]) return "토글 꺼짐";
    return process.has_xim_option ? "적용됨" : "재시작 필요";
}

// Every row's value sits centred in one column on the right.
const VALUE_COLUMN = {
    childrenLayout: "inline",
    childrenContainerWidth: "min",
    inlineWrap: "keep-inline",
};

function columnValue(value) {
    return h("div", {
        style: {
            width: "80px",
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            whiteSpace: "nowrap",
        },
    }, value);
}

// A toggle's name with its grey note lines under it, all in the toggle's own row.
function labelWithNote(title, ...lines) {
    return h("div", { style: { whiteSpace: "nowrap" } },
        h("div", null, title),
        h("div", { style: { fontSize: "12px", lineHeight: NOTE_LINE_HEIGHT, color: "#8b929a", marginTop: "2px" } },
            ...lines.map((line) => h("div", { key: line }, line))));
}

// Grey note lines closing a section, with the section's dividing line under them.
function ClosingNote(...lines) {
    return h(DFL.PanelSectionRow, null,
        h(DFL.Field, {
            label: h("div", { style: { whiteSpace: "nowrap", fontSize: "12px", lineHeight: NOTE_LINE_HEIGHT, color: "#8b929a" } },
                ...lines.map((line) => h("div", { key: line }, line))),
        }));
}

function InfoRow({ label, value }) {
    return h(DFL.PanelSectionRow, null,
        h(DFL.Field, { label, focusable: true, ...VALUE_COLUMN }, columnValue(value)));
}

function StatusLight({ label, on, color }) {
    return h("span", { style: { display: "inline-flex", alignItems: "center", gap: "6px" } },
        h("span", {
            style: {
                width: "10px",
                height: "10px",
                borderRadius: "50%",
                background: color ?? (on ? STATUS_ON_COLOR : STATUS_OFF_COLOR),
            },
        }),
        label);
}

function LocaleOptionSection() {
    const app = DFL.Router.MainRunningApp;
    const appId = app?.appid;
    // Read from the game's launch options each time, so that it also shows an edit
    // made by hand. null until read, or when there is no game to read.
    const [enabled, setEnabled] = SP_REACT.useState(null);
    const [changed, setChanged] = SP_REACT.useState(false);
    SP_REACT.useEffect(() => {
        let active = true;
        setEnabled(null);
        setChanged(false);
        if (app) {
            readLaunchOptions(app.appid, app.app_type === SHORTCUT_APP_TYPE)
                .then((options) => { if (active) setEnabled(LOCALE_OPTION_SET.test(options)); })
                .catch(() => {});
        }
        return () => { active = false; };
    }, [appId]);
    const onChange = async (next) => {
        try {
            await applyLocaleOption(app, next);
            setEnabled(next);
            setChanged(true);
        }
        catch {
            /* the launch options stay as they were */
        }
    };
    return h(DFL.PanelSection, { title: "실행 옵션" },
        h(ToggleRow, {
            label: labelWithNote("한국어 환경으로 실행", LOCALE_OPTION, "글자가 깨지면 켜주세요.", "현재 게임에만 적용돼요."),
            value: enabled === true,
            disabled: enabled === null,
            onChange,
            separator: !changed,
        }),
        changed && ClosingNote("게임을 다시 켜야 적용돼요."));
}

function GameRow({ appId, running, onRemove }) {
    return h(DFL.PanelSectionRow, null,
        h(DFL.Field, { label: gameName(appId), focusable: running, ...VALUE_COLUMN },
            columnValue(running
                ? "실행 중"
                : h(DFL.DialogButton, {
                    onClick: () => onRemove(appId),
                    style: { minWidth: "64px", width: "auto", height: "32px", padding: "0 12px" },
                }, "제거"))));
}

function GameSection({ state, runningAppId, onRemove }) {
    const enabledApps = registeredGames(state);
    const runningApps = new Set((state?.processes ?? []).map((process) => process.app_id));
    if (runningAppId) runningApps.add(runningAppId);
    return h("div", { style: SECTION_PULLED_UP },
        h(DFL.PanelSection, { title: `등록된 게임 (${enabledApps.length})` },
            enabledApps.length === 0
                ? ClosingNote("등록된 게임이 없어요.")
                : enabledApps.map((appId) => h(GameRow, {
                    key: appId,
                    appId,
                    running: runningApps.has(appId),
                    onRemove,
                }))));
}

function sendMethod(game) {
    if (game?.paste) return "paste";
    return game?.direct ? "direct" : "ime";
}

function noteRow(...lines) {
    return h(DFL.PanelSectionRow, null,
        h("div", { style: { whiteSpace: "nowrap", fontSize: "12px", lineHeight: NOTE_LINE_HEIGHT, color: "#8b929a" } },
            ...lines.map((line) => h("div", { key: line }, line))));
}

// A setting in milliseconds, with - and + buttons on each side of its value.
function MillisecondsStepper({ title, value, min, max, step, inUse, onChange }) {
    const known = typeof value === "number";
    const button = (label, next) => {
        const target = Math.min(max, Math.max(min, next));
        return h(DFL.DialogButton, {
            disabled: !inUse || !known || target === value,
            onClick: () => onChange(target),
            style: { minWidth: "44px", width: "44px", height: "36px", padding: 0 },
        }, label);
    };
    return [
        h(DFL.PanelSectionRow, { key: "title" },
            h(DFL.Field, {
                label: h("span", { style: { whiteSpace: "nowrap" } }, title),
                bottomSeparator: "none",
            })),
        h(DFL.PanelSectionRow, { key: "stepper" },
            h(DFL.Focusable, {
                "flow-children": "horizontal",
                style: { display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" },
            },
                button("-", value - step),
                h("div", {
                    style: { width: "64px", textAlign: "center", fontSize: "16px", color: inUse ? undefined : "#8b929a" },
                }, known ? `${value} ms` : "..."),
                button("+", value + step))),
    ];
}

// A toggle with its name and note lines, in its own row.
function ToggleRow({ label, value, disabled, onChange, separator }) {
    return h(DFL.PanelSectionRow, null,
        h(DFL.Field, { label, bottomSeparator: separator ? "standard" : "none", ...VALUE_COLUMN },
            columnValue(h("div", { style: { position: "relative", left: "3px" } },
                h(DFL.Toggle, { value, disabled, onChange })))));
}

// How the focused game gets the composition. Typing it as text and pasting are
// toggles that exclude each other; with both off, the IME is used. The settings of
// each show only while it is on.
function SendMethodRows({ state, game, isEnabled, onMethodChange, onHoldChange, onEraseTextChange, onPasteGapChange, onPasteBatchChange, onImeWakeChange, onWakeWaitChange }) {
    const method = sendMethod(game);
    const direct = method === "direct";
    const paste = method === "paste";
    // Only Backspace keys are held: with Backspace sent as a character, the time
    // does nothing.
    const holdInUse = !game?.erase_text;
    const directSettings = direct ? [
        ...MillisecondsStepper({
            title: "백스페이스 누름 시간",
            value: state?.backspace_hold_ms,
            min: BACKSPACE_HOLD_MIN_MS,
            max: BACKSPACE_HOLD_MAX_MS,
            step: BACKSPACE_HOLD_STEP_MS,
            inUse: holdInUse,
            onChange: onHoldChange,
        }),
        noteRow("글자가 꼬이거나 지워지지 않으면 늘려주세요.", `(기본값 ${BACKSPACE_HOLD_DEFAULT_MS}ms)`),
        h(ToggleRow, {
            label: labelWithNote("지연 시간 줄이기", "백스페이스를 누르지 않고 문자로 보내요.", "글자가 쌓이거나 □가 보이면 꺼주세요."),
            value: Boolean(game?.erase_text),
            onChange: onEraseTextChange,
            separator: true,
        }),
    ] : [];
    const pasteSettings = paste ? [
        h(ToggleRow, {
            label: labelWithNote("모아서 입력", "입력한 한글을 입력줄에 모아", "Enter로 한 번에 보내요."),
            value: Boolean(game?.paste_batch),
            disabled: !isEnabled,
            onChange: onPasteBatchChange,
            separator: true,
        }),
        ...MillisecondsStepper({
            title: "붙여넣기 간격",
            value: state?.paste_gap_ms,
            min: PASTE_GAP_MIN_MS,
            max: PASTE_GAP_MAX_MS,
            step: PASTE_GAP_STEP_MS,
            inUse: true,
            onChange: onPasteGapChange,
        }),
        ClosingNote("글자가 빠지면 늘려주세요.", `(기본값 ${PASTE_GAP_DEFAULT_MS}ms, 30fps까지 안정)`),
    ] : [];
    // A paste presses keys of its own, so it needs no waking.
    const imeWake = Boolean(game?.ime_wake);
    const wakeSettings = [
        h(ToggleRow, {
            label: labelWithNote("Enter 후 IME 깨우기", "입력창을 다시 열면 한글이 안 들어가는", "게임에서 켜주세요."),
            value: imeWake,
            disabled: !isEnabled,
            onChange: onImeWakeChange,
            separator: !imeWake,
        }),
        ...(imeWake ? [
            ...MillisecondsStepper({
                title: "Enter 후 입력 대기",
                value: game?.wake_wait_ms,
                min: WAKE_WAIT_MIN_MS,
                max: WAKE_WAIT_MAX_MS,
                step: WAKE_WAIT_STEP_MS,
                inUse: true,
                onChange: onWakeWaitChange,
            }),
            ClosingNote("글자가 빠지면 늘려주세요.", `(기본값 ${WAKE_WAIT_DEFAULT_MS}ms)`),
        ] : []),
    ];
    return h(SP_REACT.Fragment, null,
        h(ToggleRow, {
            label: labelWithNote("조합 중 글자 바로 입력", "조합 창이 따로 뜨는 게임에서 켜주세요."),
            value: direct,
            disabled: !isEnabled,
            onChange: (on) => onMethodChange(on ? "direct" : "ime"),
        }),
        // Each group has a place of its own: spread side by side, the steppers' row
        // keys would clash, and React would leave rows behind.
        h(SP_REACT.Fragment, { key: "directSettings" }, ...directSettings),
        h(ToggleRow, {
            label: labelWithNote("붙여넣기로 입력", "IME로 한글이 안 되는 게임에서 켜주세요.", "클립보드가 바뀌고 조합 중 글자는 안 보여요."),
            value: paste,
            disabled: !isEnabled,
            onChange: (on) => onMethodChange(on ? "paste" : "ime"),
            separator: !paste,
        }),
        h(SP_REACT.Fragment, { key: "pasteSettings" }, ...pasteSettings),
        h(SP_REACT.Fragment, { key: "wakeSettings" }, ...(paste ? [] : wakeSettings)));
}

// The keys that switch between Hangul and English, in the order they show.
const TOGGLE_KEY_LABELS = [
    ["hangul", "한/영 키", "한/영 키"],
    ["right_alt", "오른쪽 Alt", "RAlt"],
    ["shift_space", "Shift + Space", "Shift+Space"],
];
const ALL_TOGGLE_KEYS = TOGGLE_KEY_LABELS.map(([name]) => name);

function chosenToggleKeys(state) {
    return state?.toggle_keys ?? ALL_TOGGLE_KEYS;
}

// The hint beside the 가/A button: the keys that switch, as chosen.
function toggleKeyHint(state) {
    const chosen = chosenToggleKeys(state);
    const names = TOGGLE_KEY_LABELS.filter(([name]) => chosen.includes(name)).map(([, , short]) => short);
    return names.length ? ["전환 키", names.join(", ")] : ["가/A 버튼으로 전환해요."];
}

function ToggleKeySection({ state, onToggleKeyChange }) {
    const chosen = chosenToggleKeys(state);
    return h("div", { style: SECTION_PULLED_UP }, h(DFL.PanelSection, { title: "한/영 전환 키" },
        ...TOGGLE_KEY_LABELS.map(([name, label]) => h(ToggleRow, {
            key: name,
            label: h("span", { style: { whiteSpace: "nowrap" } }, label),
            value: chosen.includes(name),
            onChange: (enabled) => onToggleKeyChange(name, enabled),
        })),
        ClosingNote("꺼 둔 키는 게임에 그대로 들어가요.", "모두 끄면 가/A 버튼이나 지구본 키로 바꿔요.",
            "Steam 키보드는 한국어·영어 자판을 함께 두면", "지구본 키가 한/영 전환 키가 돼요.")));
}

// How the Steam keyboard's Hangul last came in: grey before any, green the usual
// way, yellow the fallback, where Steam's own composing is read back.
function SteamKeyboardLight({ path }) {
    if (path === "readback") return h(StatusLight, { label: "Steam 키보드 (예비)", color: STATUS_WARN_COLOR });
    return h(StatusLight, { label: "Steam 키보드", color: path === "letters" ? STATUS_ON_COLOR : STATUS_IDLE_COLOR });
}

function AdvancedSections({ state, onPausedChange, onToggleKeyChange }) {
    const [open, setOpen] = SP_REACT.useState(readAdvancedOpen);
    const processes = state?.processes ?? [];
    const onToggle = () => {
        saveAdvancedOpen(!open);
        setOpen(!open);
    };
    const toggle = h("div", { key: "toggle", style: { marginTop: "-24px" } },
        h(DFL.PanelSection, null,
            h(DFL.PanelSectionRow, null,
                h(DFL.ButtonItem, { layout: "below", bottomSeparator: open ? "none" : "standard", onClick: onToggle },
                    `고급 ${open ? "▲" : "▼"}`))));
    if (!open) return toggle;
    return [
        toggle,
        h("div", { key: "status", style: SECTION_UNDER_BUTTON }, h(DFL.PanelSection, { title: "상태" },
            h(DFL.PanelSectionRow, null,
                h("div", { style: { display: "flex", gap: "20px", fontSize: "14px" } },
                    h(StatusLight, { label: "입력 서버", on: Boolean(state?.xim_running) }),
                    h(SteamKeyboardLight, { path: state?.steam_keyboard_path }))),
            h(DFL.PanelSectionRow, null,
                h(DFL.Field, {
                    label: labelWithNote("일시 중지", "입력 서버와 포커스 감지를 꺼요.", "다시 켜면 게임을 재시작해 주세요."),
                    ...VALUE_COLUMN,
                },
                    columnValue(h("div", { style: { position: "relative", left: "3px" } },
                        h(DFL.Toggle, {
                            value: Boolean(state?.paused),
                            onChange: onPausedChange,
                        }))))))),
        h(ToggleKeySection, { key: "toggleKeys", state, onToggleKeyChange }),
        h("div", { key: "localeOption", style: SECTION_PULLED_UP }, h(LocaleOptionSection)),
        h("div", { key: "processes", style: SECTION_PULLED_UP }, h(DFL.PanelSection, { title: `현재 프로세스 (${processes.length})` },
            processes.length === 0
                ? h(DFL.PanelSectionRow, null,
                    h(DFL.Field, { label: h("div", { style: { whiteSpace: "nowrap", fontSize: "12px", color: "#8b929a" } }, "실행 중인 게임 프로세스가 없어요.") }))
                : processes.map((process) => h(InfoRow, {
                    key: process.pid,
                    label: process.name,
                    value: processStatus(process, state),
                })))),
    ];
}

function App() {
    const [state, setState] = SP_REACT.useState(null);
    const [message, setMessage] = SP_REACT.useState("");
    const [restartNotice, setRestartNotice] = SP_REACT.useState("");
    const [opacityDraft, setOpacityDraft] = SP_REACT.useState(null);
    const app = DFL.Router.MainRunningApp;
    const appId = app?.appid ? String(app.appid) : null;
    const game = appId ? state?.games?.[appId] : null;
    const isEnabled = Boolean(game);

    SP_REACT.useEffect(() => {
        let active = true;
        const refresh = () => getState()
            .then((next) => { if (active) setState(next); })
            .catch((error) => { if (active) setMessage(String(error)); });
        refresh();
        const timer = setInterval(refresh, 2000);
        return () => { active = false; clearInterval(timer); };
    }, []);

    const onEnglishOnCloseChange = async (enabled) => {
        keyboardMode = { ...keyboardMode, english_on_close: enabled };
        try {
            setState(await setGameSetting(appId, "english_on_close", enabled));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onKeepKeyboardChange = async (enabled) => {
        keepKeyboardOnEnter = enabled;
        try {
            setState(await setKeepKeyboard(enabled));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const opacity = opacityDraft ?? state?.keyboard_opacity ?? KEYBOARD_OPACITY_MAX;

    const onOpacityChange = async (percent) => {
        setOpacityDraft(percent);
        keyboardOpacity = percent;
        applyKeyboardOpacity();
        try {
            setState(await setOpacity(percent));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onPausedChange = async (paused) => {
        try {
            setState(await setPaused(paused));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const hangulMode = Boolean(state?.hangul_mode);

    const onModeButton = async () => {
        setState({ ...state, hangul_mode: !hangulMode });
        try {
            setState(await setHangulMode(!hangulMode));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onRemove = async (targetId) => {
        setMessage("");
        try {
            const overview = window.appStore?.GetAppOverviewByAppID?.(Number(targetId));
            await applyLaunchOptions({ appid: Number(targetId), app_type: overview?.app_type }, false);
        }
        catch (error) {
            setMessage(`실행 옵션을 빼지 못했어요. 게임 속성에서 XMODIFIERS=@im=hangulbridge를 직접 지워 주세요: ${error?.message ?? error}`);
        }
        try {
            setState(await removeGame(targetId));
        }
        catch (error) {
            setMessage(`제거하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onBackspaceHoldChange = async (milliseconds) => {
        try {
            setState(await setBackspaceHold(milliseconds));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onPasteGapChange = async (milliseconds) => {
        try {
            setState(await setPasteGap(milliseconds));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onWakeWaitChange = async (milliseconds) => {
        try {
            setState(await setWakeWait(appId, milliseconds));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onToggleKeyChange = async (name, enabled) => {
        try {
            setState(await setToggleKey(name, enabled));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onMethodChange = async (method) => {
        setMessage("");
        try {
            setState(await setSendMethod(appId, method));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onOptionToggle = (option) => async (enabled) => {
        setMessage("");
        try {
            setState(await setGameInput(appId, option, enabled));
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    const onUseToggle = async (enabled) => {
        setMessage("");
        try {
            if (enabled !== isEnabled) await applyLaunchOptions(app, enabled);
            setState(await setGameEnabled(appId, enabled));
            if (enabled !== isEnabled) {
                setRestartNotice(enabled
                    ? "실행 옵션을 추가했어요. 게임을 다시 켜야 적용돼요."
                    : "실행 옵션을 뺐어요. 게임을 다시 켜야 적용돼요.");
            }
        }
        catch (error) {
            setMessage(`설정하지 못했어요: ${error?.message ?? error}`);
        }
    };

    return h(SP_REACT.Fragment, null,
        h(DFL.PanelSection, { title: app?.display_name || "한글 입력" },
            h(DFL.PanelSectionRow, null,
                h("div", { style: { whiteSpace: "nowrap", fontSize: "12px", color: "#8b929a" } },
                    statusText(state, isEnabled, app))),
            restartNotice && h(DFL.PanelSectionRow, null, h("div", { style: { fontSize: "12px" } }, restartNotice)),
            message && h(DFL.PanelSectionRow, null, h("div", { style: { fontSize: "12px" } }, message)),
            h(DFL.PanelSectionRow, null,
                h(DFL.Field, {
                    label: h("span", { style: { whiteSpace: "nowrap" } }, "이 게임에서 사용"),
                    bottomSeparator: isEnabled ? "none" : "standard",
                    ...VALUE_COLUMN,
                },
                    columnValue(h("div", { style: { position: "relative", left: "3px" } },
                        h(DFL.Toggle, {
                            value: isEnabled,
                            disabled: !app,
                            onChange: onUseToggle,
                        }))))),
            isEnabled && h(ToggleRow, {
                label: labelWithNote("키보드 닫으면 영문으로", "조작키가 한글로 바뀌지 않게 해요.", "다시 열면 이전 한/영 상태로 돌아가요."),
                value: game?.english_on_close ?? true,
                onChange: onEnglishOnCloseChange,
            }),
            isEnabled && h(DFL.PanelSectionRow, null,
                h("div", { style: { marginTop: "-8px" } },
                    h(DFL.Field, {
                        // As tall as the button beside it, so the text sits at the top.
                        label: h("div", { style: { height: "36px", whiteSpace: "nowrap", fontSize: "12px", lineHeight: NOTE_LINE_HEIGHT, color: "#8b929a" } },
                            ...toggleKeyHint(state).map((line) => h("div", { key: line }, line))),
                        ...VALUE_COLUMN,
                    },
                        columnValue(h(DFL.DialogButton, {
                            disabled: !game?.physical || !state?.xim_running,
                            onClick: onModeButton,
                            style: { minWidth: "56px", width: "56px", height: "36px", padding: 0, fontSize: "20px" },
                        }, hangulMode ? "가" : "A"))))),
            isEnabled && h(SendMethodRows, { state, game, isEnabled, ...{
                    onMethodChange,
                    onHoldChange: onBackspaceHoldChange,
                    onEraseTextChange: onOptionToggle("erase_text"),
                    onPasteGapChange,
                    onPasteBatchChange: onOptionToggle("paste_batch"),
                    onImeWakeChange: onOptionToggle("ime_wake"),
                    onWakeWaitChange,
                } })),
        h("div", { style: SECTION_PULLED_UP }, h(DFL.PanelSection, { title: "편의 기능" },
            h(DFL.PanelSectionRow, null,
                h(DFL.Field, {
                    label: h("span", { style: { whiteSpace: "nowrap" } }, "엔터 눌러도 키보드 유지"),
                    bottomSeparator: "none",
                    ...VALUE_COLUMN,
                },
                    columnValue(h("div", { style: { position: "relative", left: "3px" } },
                        h(DFL.Toggle, {
                            value: state?.keep_keyboard_on_enter ?? true,
                            onChange: onKeepKeyboardChange,
                        }))))),

            h(DFL.PanelSectionRow, null,
                h(DFL.SliderField, {
                    label: `키보드 불투명도 (${opacity}%)`,
                    value: opacity,
                    min: KEYBOARD_OPACITY_MIN,
                    max: KEYBOARD_OPACITY_MAX,
                    step: KEYBOARD_OPACITY_STEP,
                    onChange: onOpacityChange,
                })))),
        h(AdvancedSections, {
            state,
            onPausedChange,
            onToggleKeyChange,
        }),
        h(GameSection, { state, runningAppId: appId, onRemove }));
}

// Steam closes its keyboard on Enter through this flag on each window's keyboard
// manager, and sets the flag for single-line text. While the setting is on, the flag
// reads as false everywhere, so the keyboard stays open for the next line.
const keptOpen = new Set();
let keepKeyboardOnEnter = true;
let keyboardOpacity = KEYBOARD_OPACITY_MAX;
const OPACITY_STYLE_ID = "hangul-ime-keyboard-opacity";

function keepKeyboardOpen(manager) {
    if (!manager || keptOpen.has(manager)) return;
    let stored = Boolean(manager.m_bDismissOnEnter);
    Object.defineProperty(manager, "m_bDismissOnEnter", {
        configurable: true,
        get() {
            return keepKeyboardOnEnter ? false : stored;
        },
        set(value) {
            stored = value;
        },
    });
    keptOpen.add(manager);
}

function restoreKeyboards() {
    for (const manager of keptOpen) {
        keepKeyboardOnEnter = false;
        const stored = manager.m_bDismissOnEnter;
        delete manager.m_bDismissOnEnter;
        manager.m_bDismissOnEnter = stored;
    }
    keptOpen.clear();
}

// The keyboard's style classes are hashed and change with Steam updates, so they
// are looked up rather than written down. findModule stops at the first match, and
// several modules define the class, so the search never reports one.
let keyboardClasses = null;

function findKeyboardClasses() {
    if (keyboardClasses?.length) return keyboardClasses;
    const found = new Set();
    try {
        DFL.findModule((module) => {
            if (module && typeof module === "object") {
                if (typeof module.VirtualKeyboardContainer === "string") found.add(module.VirtualKeyboardContainer);
                if (typeof module.ModalVirtualKeyboardContainer === "string") found.add(module.ModalVirtualKeyboardContainer);
            }
            return false;
        });
    }
    catch {
        /* no classes found, so no style is applied */
    }
    keyboardClasses = [...found];
    return keyboardClasses;
}

function steamDocuments() {
    const documents = new Set([document]);
    window.SteamUIStore?.WindowStore?.SteamUIWindows?.forEach((steamWindow) => {
        const windowDocument = steamWindow.BrowserWindow?.document;
        if (windowDocument) documents.add(windowDocument);
    });
    return documents;
}

function applyKeyboardOpacity() {
    const classes = findKeyboardClasses();
    const css = keyboardOpacity >= KEYBOARD_OPACITY_MAX || classes.length === 0
        ? ""
        : `${classes.map((name) => `.${name}`).join(",")}{opacity:${keyboardOpacity / 100} !important;}`;
    for (const target of steamDocuments()) {
        let style = target.getElementById(OPACITY_STYLE_ID);
        if (!style) {
            if (!css) continue;
            style = target.createElement("style");
            style.id = OPACITY_STYLE_ID;
            target.head.appendChild(style);
        }
        if (style.textContent !== css) style.textContent = css;
    }
}

function removeKeyboardOpacity() {
    for (const target of steamDocuments()) {
        target.getElementById(OPACITY_STYLE_ID)?.remove();
    }
}

function patchKeyboards() {
    window.SteamUIStore?.WindowStore?.SteamUIWindows?.forEach((steamWindow) => {
        keepKeyboardOpen(steamWindow.VirtualKeyboardManager);
    });
    applyKeyboardOpacity();
    getState()
        .then((state) => {
            keepKeyboardOnEnter = state.keep_keyboard_on_enter;
            keyboardOpacity = state.keyboard_opacity;
        })
        .catch(() => {});
}

// The Steam keyboard, in a game that uses the bridge. Its Korean layout types each
// character through a key map of its own, which a game at a low frame rate reads too
// late, and which then has to be read back here; letters it types plainly. So:
// - the Korean layout shows as it is, but each jamo key types the letter at its
//   place, which is composed into Hangul like a physical keyboard's, and Hangul
//   mode follows the layout: on with the Korean layout, off with any other,
// - the globe key switches between the Korean layout and the others, as Hangul and
//   English; with no Korean layout, it turns Hangul mode on and off,
// - while Hangul shows, Caps Lock does nothing, and long presses bring up no
//   extended characters.
// All of it only while typing into the game: Steam's own text fields, as in the
// overlay, get Steam's keyboard as it is.
// Only what shows and which letter goes out change: if this stops working after a
// Steam update, the keyboard goes on typing as Steam's own.
const KEYBOARD_MODE_POLL_MS = 500;
const BATCH_TEXT_POLL_MS = 60;
// Steam shows the row only with some text in it.
const EMPTY_INPUT_ROW = " ";
const GLOBE_KEY = "SwitchKeys_Layout";
const KOREAN_LAYOUT = "korean";
const LAYOUT_SWITCH_TRIES = 6;
const LAYOUT_SWITCH_STEP_MS = 150;
// The letter at each key of the Korean layout, by the Linux key code it carries.
const KEYCODE_LETTERS = {
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i", 24: "o", 25: "p",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h", 36: "j", 37: "k", 38: "l",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m",
};
const CAPS_LOCK_KEY = "CapsLock";
const EXTENDED_CHARS_ATTRIBUTE = "data-extended-chars";

let keyboardMode = { active: false, hangul: false, english_on_close: true };
// Open for typing into the game, as last seen; and the Hangul mode to go back to
// when it opens again, after closing switched to English.
let keyboardOpenForGame = false;
let hangulBeforeClose = null;
let lastHangulMode = false;
const patchedKeyboardClasses = new Map();  // prototype -> its own methods
const keyboards = new Set();  // keyboard components found
const unextendedKeys = new Set();  // keys whose extended characters are hidden
const keyboardObservers = new Map();  // keyboard element -> MutationObserver
const keyboardStates = new WeakMap();  // keyboard -> { layout, switching, noKorean }

// Steam's toggle states: the low bits say on for one key or locked, bit 4 held.
const toggleOn = (state) => (state & 7) !== 0;
const afterOneKey = (state) => ((state & 3) === 1 ? 0 : state & 3) | (state & 4);

function reactInstanceWithMethod(element, method) {
    const fiberKey = Object.keys(element).find((key) => key.startsWith("__reactFiber$"));
    for (let fiber = fiberKey ? element[fiberKey] : null; fiber; fiber = fiber.return) {
        if (typeof fiber.stateNode?.[method] === "function") return fiber.stateNode;
    }
    return null;
}

function keyboardState(keyboard) {
    let state = keyboardStates.get(keyboard);
    if (!state) {
        state = { layout: undefined, switching: false, noKorean: false };
        keyboardStates.set(keyboard, state);
    }
    return state;
}

// Whether the keyboard types into the game. Typing into the game, Steam still sets
// a keyboard target, but one with no settings, and no text field has focus; a text
// field of Steam's own, as in the overlay's chat or browser, brings settings of its
// own, or has focus.
function typingIntoGame(keyboard) {
    const manager = keyboard.props?.VirtualKeyboardManager;
    if (!manager || manager.m_fnVROnTextEnteredOverride) return false;
    const props = manager.m_ActiveElementProps;
    if (props && Object.keys(props).length > 0) return false;
    for (const target of steamDocuments()) {
        const focused = target.hasFocus?.() ? target.activeElement : null;
        if (focused && (focused.isContentEditable || ["INPUT", "TEXTAREA"].includes(focused.tagName))) return false;
    }
    return true;
}

// Whether the keyboard is open. Switching its layout while closed would open it.
function keyboardShown(keyboard) {
    const showing = keyboard.props?.VirtualKeyboardManager?.IsShowingVirtualKeyboard;
    if (showing && "Value" in showing) return Boolean(showing.Value);
    const root = keyboard.m_keyboardDiv;
    return Boolean(root?.isConnected && root.getClientRects().length > 0);
}

function layoutName(keyboard) {
    return keyboard.state?.standardLayout?.name;
}

function showsKorean(keyboard) {
    return layoutName(keyboard) === KOREAN_LAYOUT;
}

// Hangul is showing: on the standard keys, not the numbers or emoji.
function hangulShowing(keyboard) {
    // While switching, as the mode being switched to.
    const hangul = hangulTarget ?? keyboardMode.hangul;
    return keyboardMode.active && hangul && keyboard.state?.layoutState === 0
        && typingIntoGame(keyboard);
}

function withoutCapsLock(update) {
    if (!update?.toggleStates?.CapsLock) return update;
    return { ...update, toggleStates: { ...update.toggleStates, CapsLock: 0 } };
}

function hasManyLayouts(keyboard, own) {
    return typeof own.BIsKeyEnabled === "function" ? own.BIsKeyEnabled.call(keyboard, GLOBE_KEY) : true;
}

function sendLetter(keyboard, letter, shift) {
    keyboard.props.VirtualKeyboardManager.HandleVirtualKeyDown(shift ? letter.toUpperCase() : letter, shift);
}

function typeLetter(keyboard, letter, key) {
    const toggles = keyboard.state?.toggleStates ?? {};
    const shift = Boolean(key.strShifted) || toggleOn(toggles.Shift);
    // Switching to Hangul, the daemon may not compose yet: the letter waits.
    if (hangulTarget === true && !keyboardMode.hangul) heldLetters.push({ keyboard, letter, shift });
    else sendLetter(keyboard, letter, shift);
    keyboard.setState((state) => ({
        ...state,
        toggleStates: {
            ...state.toggleStates,
            Shift: afterOneKey(state.toggleStates.Shift),
            Control: afterOneKey(state.toggleStates.Control),
            Alt: afterOneKey(state.toggleStates.Alt),
            AltGr: afterOneKey(state.toggleStates.AltGr),
        },
    }));
}

// From the globe key until the daemon has left Hangul mode, it would compose what is
// typed, so keys wait, whatever the keyboard already shows.
function goingToEnglish(keyboard) {
    if (!keyboardMode.active || !typingIntoGame(keyboard)) return false;
    return keyboardState(keyboard).toEnglish === true || (hangulTarget === false && keyboardMode.hangul);
}

function releaseHeldKeys() {
    const held = heldKeys;
    heldKeys = [];
    for (const { keyboard, own, key } of held) own.TypeKeyInternal.call(keyboard, key);
}

function patchKeyboardClass(keyboard) {
    const prototype = Object.getPrototypeOf(keyboard);
    if (patchedKeyboardClasses.has(prototype)) return;
    const own = {
        TypeKeyInternal: prototype.TypeKeyInternal,
        BIsKeyEnabled: prototype.BIsKeyEnabled,
        LongPressTimerExpired: prototype.LongPressTimerExpired,
        setState: prototype.setState,
        hadSetState: Object.prototype.hasOwnProperty.call(prototype, "setState"),
    };
    patchedKeyboardClasses.set(prototype, own);
    prototype.TypeKeyInternal = function (key) {
        if (keyboardMode.active && key?.strKey === GLOBE_KEY && typingIntoGame(this)) {
            pressGlobe(this, own);
            return undefined;
        }
        if (goingToEnglish(this)) {
            heldKeys.push({ keyboard: this, own, key });
            return undefined;
        }
        if (hangulShowing(this)) {
            if (key?.strKey === CAPS_LOCK_KEY) return undefined;
            const letter = showsKorean(this) ? KEYCODE_LETTERS[Number(key?.strKeycode)] : null;
            if (letter) {
                typeLetter(this, letter, key);
                return undefined;
            }
        }
        return own.TypeKeyInternal.call(this, key);
    };
    if (typeof own.BIsKeyEnabled === "function") {
        // With one layout the globe key is hidden; in this mode it has a use.
        prototype.BIsKeyEnabled = function (key) {
            if (key === GLOBE_KEY && keyboardMode.active && typingIntoGame(this)) return true;
            return own.BIsKeyEnabled.call(this, key);
        };
    }
    if (typeof own.LongPressTimerExpired === "function") {
        // Touch and mouse start this timer on every key, whatever the key's attribute
        // says. Left alone here, the key is typed when it is let go. Backspace
        // repeats through this timer, and keeps doing so.
        prototype.LongPressTimerExpired = function (source) {
            const slot = hangulShowing(this) ? this.LongPressSlot?.(source) : null;
            if (slot?.holdTarget && slot.holdTarget.getAttribute("data-key") !== "Backspace") {
                slot.nTimer = null;
                return undefined;
            }
            return own.LongPressTimerExpired.call(this, source);
        };
    }
    // Caps Lock comes on by touch, held, and by a stick click too: while Hangul
    // shows, every change keeps it off.
    prototype.setState = function (update, callback) {
        if (!hangulShowing(this)) return own.setState.call(this, update, callback);
        const next = typeof update === "function"
            ? (state, props) => withoutCapsLock(update(state, props))
            : withoutCapsLock(update);
        return own.setState.call(this, next, callback);
    };
}

function unpatchKeyboardClasses() {
    for (const [prototype, own] of patchedKeyboardClasses) {
        prototype.TypeKeyInternal = own.TypeKeyInternal;
        if (own.BIsKeyEnabled) prototype.BIsKeyEnabled = own.BIsKeyEnabled;
        if (own.LongPressTimerExpired) prototype.LongPressTimerExpired = own.LongPressTimerExpired;
        if (own.hadSetState) prototype.setState = own.setState;
        else delete prototype.setState;
    }
    patchedKeyboardClasses.clear();
}

// Press the globe key Steam's way until the layout is the one wanted, or give up.
function switchLayout(keyboard, toKorean) {
    const state = keyboardState(keyboard);
    const own = patchedKeyboardClasses.get(Object.getPrototypeOf(keyboard));
    if (state.switching || !own) return;
    state.switching = true;
    state.toEnglish = !toKorean;
    let tries = 0;
    const step = () => {
        if (showsKorean(keyboard) === toKorean) {
            state.switching = false;
            state.toEnglish = false;
            state.layout = layoutName(keyboard);
            switchHangulMode(toKorean);
            return;
        }
        if (!keyboard.m_keyboardDiv?.isConnected || !keyboardShown(keyboard) || !typingIntoGame(keyboard)) {
            state.switching = false;  // closed meanwhile: done again as it opens
            state.toEnglish = false;
            releaseHeldKeys();
            return;
        }
        if (tries >= LAYOUT_SWITCH_TRIES) {
            state.switching = false;
            state.toEnglish = false;
            releaseHeldKeys();
            if (toKorean) {
                // No Korean layout to go to: the letters are composed all the same.
                state.noKorean = true;
                switchHangulMode(true);
            }
            return;
        }
        tries += 1;
        own.TypeKeyInternal.call(keyboard, { strKey: GLOBE_KEY });
        setTimeout(step, LAYOUT_SWITCH_STEP_MS);
    };
    step();
}

function pressGlobe(keyboard, own) {
    const state = keyboardState(keyboard);
    if (hasManyLayouts(keyboard, own) && !state.noKorean) switchLayout(keyboard, !showsKorean(keyboard));
    else switchHangulMode();
}

function findKeyboards() {
    for (const target of steamDocuments()) {
        const key = target.querySelector?.("[data-key]");
        if (!key) continue;
        const keyboard = reactInstanceWithMethod(key, "TypeKeyInternal");
        if (!keyboard) continue;
        patchKeyboardClass(keyboard);
        if (!keyboards.has(keyboard)) {
            keyboards.add(keyboard);
            keyboard.forceUpdate?.();
        }
    }
    for (const keyboard of keyboards) {
        if (!keyboard.m_keyboardDiv?.isConnected) keyboards.delete(keyboard);
    }
}

// The look of Hangul: no extended characters, so a controller's press types the key
// at once. Touch and mouse are handled in LongPressTimerExpired.
function showHangul(keyboard) {
    const root = keyboard.m_keyboardDiv;
    if (!root) return;
    for (const key of root.querySelectorAll(`[${EXTENDED_CHARS_ATTRIBUTE}]`)) {
        key.removeAttribute(EXTENDED_CHARS_ATTRIBUTE);
        unextendedKeys.add(key);
    }
}

function hideHangul() {
    for (const key of unextendedKeys) {
        if (key.isConnected) key.setAttribute(EXTENDED_CHARS_ATTRIBUTE, "1");
    }
    unextendedKeys.clear();
}

function turnOffCapsLock(keyboard) {
    if (!keyboard.state?.toggleStates?.CapsLock) return;
    keyboard.setState((state) => ({ ...state, toggleStates: { ...state.toggleStates, CapsLock: 0 } }));
}

function watchKeyboard(keyboard) {
    const root = keyboard.m_keyboardDiv;
    if (!root || keyboardObservers.has(root)) return;
    const Observer = root.ownerDocument.defaultView.MutationObserver;
    const observer = new Observer(() => {
        if (hangulShowing(keyboard)) showHangul(keyboard);
    });
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    keyboardObservers.set(root, observer);
}

function unwatchKeyboards() {
    for (const observer of keyboardObservers.values()) observer.disconnect();
    keyboardObservers.clear();
}

// The layout follows Hangul mode: as the keyboard opens, it shows the layout of the
// mode it is in, whichever one Steam opened it on; and while it is open, a change of
// mode some other way, as by the 가/A button or a physical Hangul key, changes the
// layout too. A change of layout while open, other than by the globe key, which is
// already a change of mode, sets the mode.
function followLayout(keyboard) {
    const state = keyboardState(keyboard);
    if (state.switching) return;
    const name = layoutName(keyboard);
    const korean = name === KOREAN_LAYOUT;
    const own = patchedKeyboardClasses.get(Object.getPrototypeOf(keyboard));
    const many = Boolean(own) && hasManyLayouts(keyboard, own);
    if (state.layout !== name) {
        const opening = state.layout === undefined;
        state.layout = name;
        if (korean) state.noKorean = false;
        if (opening) {
            if (korean === keyboardMode.hangul) return;
            if (many && !(keyboardMode.hangul && state.noKorean)) switchLayout(keyboard, keyboardMode.hangul);
            // With the Korean layout alone, it can only type Hangul.
            else if (korean) switchHangulMode(true);
            return;
        }
        if (korean !== keyboardMode.hangul && !(state.noKorean && keyboardMode.hangul)) switchHangulMode(korean);
        return;
    }
    if (keyboardMode.hangul !== lastHangulMode && korean !== keyboardMode.hangul && !state.noKorean && many) {
        switchLayout(keyboard, keyboardMode.hangul);
    }
}

// Closing the keyboard after typing into the game switches to English, so the
// game's keys work as controls again; opening it again goes back to Hangul, unless
// the mode was changed meanwhile some other way.
function keyboardClosed() {
    if (!keyboardMode.english_on_close) return;
    hangulBeforeClose = keyboardMode.hangul;
    if (keyboardMode.hangul) switchHangulMode(false);
}

// Returns true if this takes care of the keyboard's layout as it opens.
function keyboardOpened(keyboard) {
    const restore = hangulBeforeClose;
    hangulBeforeClose = null;
    if (!restore || keyboardMode.hangul || !keyboardMode.english_on_close) return false;
    const state = keyboardState(keyboard);
    const own = patchedKeyboardClasses.get(Object.getPrototypeOf(keyboard));
    state.layout = layoutName(keyboard);
    if (own && hasManyLayouts(keyboard, own) && !state.noKorean && !showsKorean(keyboard)) switchLayout(keyboard, true);
    else switchHangulMode(true);
    return true;
}

function updateKeyboards() {
    let showing = false;
    let openForGame = null;
    for (const keyboard of keyboards) {
        watchKeyboard(keyboard);
        const state = keyboardState(keyboard);
        // Closed, the keyboard may stay in the page: opening it again is an opening
        // all the same. Typing into Steam's own fields, it is Steam's as it is.
        if (!keyboardMode.active || !keyboardShown(keyboard) || !typingIntoGame(keyboard)) {
            if (!state.switching) state.layout = undefined;
            continue;
        }
        openForGame = keyboard;
        if (!keyboardOpenForGame) {
            keyboardOpenForGame = true;
            if (keyboardOpened(keyboard)) continue;
        }
        followLayout(keyboard);
        if (hangulShowing(keyboard)) {
            showing = true;
            turnOffCapsLock(keyboard);
            showHangul(keyboard);
        }
    }
    if (!showing) hideHangul();
    if (keyboardOpenForGame && !openForGame) {
        keyboardOpenForGame = false;
        keyboardClosed();
    }
    lastHangulMode = keyboardMode.hangul;
}

function setKeyboardMode(next) {
    const activeChanged = next.active !== keyboardMode.active;
    keyboardMode = next;
    if (activeChanged) {
        // The globe key shows or hides with the mode.
        for (const keyboard of keyboards) keyboard.forceUpdate?.();
    }
    updateKeyboards();
}

// The mode being switched to, while the daemon takes it; and how many switches
// there have been, so that a poll sent before one is not taken after it.
let hangulTarget = null;
let modeSwitches = 0;
// Letters typed on the Korean layout while switching to Hangul: sent once it is on,
// so they are composed rather than typed as English.
let heldLetters = [];
let heldKeys = [];

async function switchHangulMode(on) {
    const target = on ?? !(hangulTarget ?? keyboardMode.hangul);
    if (target === (hangulTarget ?? keyboardMode.hangul)) {
        releaseHeldKeys();
        return;
    }
    hangulTarget = target;
    modeSwitches += 1;
    let next = null;
    try {
        next = await setKeyboardHangul(target);
    }
    catch {
        /* the mode stays as it was */
    }
    if (hangulTarget !== target) return;  // a newer switch took over
    hangulTarget = null;
    modeSwitches += 1;
    if (next) {
        lastHangulMode = next.hangul;  // asked for here, so no layout to follow
        setKeyboardMode(next);
    }
    const letters = heldLetters;
    heldLetters = [];
    for (const { keyboard, letter, shift } of letters) sendLetter(keyboard, letter, shift);
    releaseHeldKeys();
}

function isInputRowState(value) {
    return value && typeof value === "object" && "strText" in value && "nCursorPos" in value
        && "bVisible" in value && !("vecCandidates" in value);
}

// The row the Japanese keyboard shows what it composes in. It draws a state of its
// own, which is where the text goes.
function findInputRowHook(keyboard) {
    const stack = [keyboard._reactInternals?.child];
    while (stack.length) {
        const fiber = stack.pop();
        if (!fiber) continue;
        stack.push(fiber.sibling, fiber.child);
        let hook = fiber.memoizedState;
        if (!hook || typeof hook !== "object" || !("queue" in hook)) continue;
        for (; hook; hook = hook.next) {
            if (isInputRowState(hook.memoizedState) && hook.queue?.dispatch) return hook;
        }
    }
    return null;
}

function keyboardTypingIntoGame() {
    for (const keyboard of keyboards) {
        if (keyboardShown(keyboard) && typingIntoGame(keyboard)) return keyboard;
    }
    return null;
}

let batchRowMissing = false;
let batchTextAsked = false;

// Against what the row has now, as changing layout empties it.
function setInputRow(hook, text) {
    const shown = hook?.memoizedState;
    if (!shown || (shown.strText === text && shown.bVisible === (text !== ""))) return;
    hook.queue.dispatch({ strText: text, nCursorPos: text.length, bVisible: text !== "" });
}

// Steam keeps the row's text to a third of the width, cut off at the start so that
// the end shows. The cut stays, at the full width.
function widenInputRow(keyboard) {
    const text = keyboard.m_keyboardDiv?.querySelector(".Row_IME")?.firstElementChild;
    if (text && text.style.maxWidth !== "100%") text.style.maxWidth = "100%";
}

function reportInputRow(found) {
    if (found === !batchRowMissing) return;
    batchRowMissing = !found;
    setBatchRowMissing(batchRowMissing).catch(() => {});
}

// At the top of the screen the keyboard grows downwards as the row comes, and the
// keys move under the finger: there the row stays, empty while nothing is held.
function keyboardAtTop(keyboard) {
    if (keyboard.props?.VirtualKeyboardManager?.KeyboardLocation === "center-top") return true;
    const root = keyboard.m_keyboardDiv;
    const view = root?.ownerDocument?.defaultView;
    if (!root || !view) return false;
    const box = root.getBoundingClientRect();
    return box.height > 0 && box.top + box.height / 2 < view.innerHeight / 2;
}

function followBatchText() {
    const keyboard = keyboardMode.active && keyboardMode.batch ? keyboardTypingIntoGame() : null;
    if (!keyboard || batchTextAsked) return;
    const hook = findInputRowHook(keyboard);
    reportInputRow(Boolean(hook));
    widenInputRow(keyboard);
    const atTop = keyboardAtTop(keyboard);
    batchTextAsked = true;
    getBatchText()
        .then((text) => setInputRow(hook, text || (atTop ? EMPTY_INPUT_ROW : "")))
        .catch(() => {})
        .finally(() => { batchTextAsked = false; });
}

function followKeyboardMode() {
    try {
        findKeyboards();
    }
    catch {
        /* no keyboard to change this time */
    }
    if (hangulTarget !== null) return;
    const sentAt = modeSwitches;
    getKeyboardMode()
        .then((mode) => {
            if (hangulTarget === null && sentAt === modeSwitches) setKeyboardMode(mode);
        })
        .catch(() => {});
}

function restoreKeyboardLook() {
    unwatchKeyboards();
    hideHangul();
    unpatchKeyboardClasses();
    for (const keyboard of keyboards) keyboard.forceUpdate?.();
    keyboards.clear();
}

var index = DFL.definePlugin(() => {
    patchKeyboards();
    const timer = setInterval(patchKeyboards, 3000);
    const keyboardTimer = setInterval(followKeyboardMode, KEYBOARD_MODE_POLL_MS);
    const batchTextTimer = setInterval(followBatchText, BATCH_TEXT_POLL_MS);
    accountTimer = setInterval(reportAccount, ACCOUNT_RETRY_MS);
    reportAccount();
    return {
        name: manifest.name,
        titleView: h("div", { className: DFL.staticClasses.Title }, "한글 IME"),
        content: h(App, null),
        icon: h("span", { style: { fontWeight: "bold" } }, "한"),
        onDismount() {
            clearInterval(timer);
            clearInterval(keyboardTimer);
            clearInterval(batchTextTimer);
            clearInterval(accountTimer);
            restoreKeyboardLook();
            restoreKeyboards();
            removeKeyboardOpacity();
        },
    };
});

export { index as default };
