/* Shared helpers and state for the UI modules. No framework, no build step. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const state = {
    data: null,
    presets: null,
    accountId: null,
    /* The queue ("cola") being edited inside the current account. */
    queueId: null,
    page: "resumen",
    pageArg: null,
    /* While the user is typing we must not stomp the form with polled data. */
    formDirty: false,
    windows: [],
    days: [0, 1, 2, 3, 4, 5, 6],
    queueFilter: "pending",
    /* Library folders the queue list is narrowed to, and the videos ticked
       there to take back from Zernio. */
    /* {folders, labels, match} (labels.js): the folders ticked (none = all),
       narrowed by the labels ticked. */
    queueScope: {folders: [], labels: [], match: "all"},
    queueSelected: new Set(),
    /* {id, until, scrolled}: the video to bring into view in the queue list. */
    queueFocus: null,
    /* "" (every platform), "tiktok" or "youtube": Resumen and Calendario share it. */
    platform: "",
    /* Callbacks registered by app.js so admin.js can trigger a data reload. */
    refresh: async () => {
    },
    route: () => {
    },
};

/* ------------------------------------------------------------------ */
/* API                                                                 */

/* ------------------------------------------------------------------ */

export async function api(path, options = {}) {
    const res = await fetch(path, {
        headers: options.body instanceof FormData
            ? {}
            : {"Content-Type": "application/json"},
        ...options,
    });
    const text = await res.text();
    let payload = null;
    try {
        payload = text ? JSON.parse(text) : null;
    } catch {
        payload = {detail: text};
    }
    if (!res.ok) {
        const detail = payload?.detail;
        throw new Error(
            typeof detail === "string" ? detail
                : Array.isArray(detail) ? detail.map((d) => d.msg || d).join(" · ")
                    : `Error ${res.status}`
        );
    }
    return payload;
}

export function toast(message, isError = false) {
    const box = $("#toast");
    box.textContent = message;
    box.classList.toggle("err", isError);
    box.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
        box.hidden = true;
    }, isError ? 6000 : 3000);
}

/* ------------------------------------------------------------------ */
/* DOM                                                                 */

/* ------------------------------------------------------------------ */

/** Is the user typing in a field inside ``node``? The 5-second refresh must not
 *  redraw it then: a redraw replaces the fields and takes the focus and the text. */
export function editingInside(node) {
    const active = document.activeElement;
    return !!(node && active && node.contains(active)
        && active.matches("input:not([type=checkbox]):not([type=radio]), textarea, select"));
}

/** Fill a <select> without closing it under the user: its options are rebuilt
 *  only when they change, and not at all while it has the focus (open). */
export function fillSelect(select, options, value) {
    if (document.activeElement === select) return;
    const signature = JSON.stringify(options);
    if (select.dataset.options !== signature) {
        select.replaceChildren(...options.map(([optionValue, text]) => {
            const option = document.createElement("option");
            option.value = String(optionValue);
            option.textContent = text;
            return option;
        }));
        select.dataset.options = signature;
    }
    select.value = String(value);
}

export function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

export function link(text, href, className, newTab = true) {
    const a = el("a", className, text);
    a.href = href;
    if (newTab) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
    }
    return a;
}

export function button(label, className, onClick) {
    const b = el("button", className, label);
    b.type = "button";
    b.addEventListener("click", async () => {
        b.disabled = true;
        try {
            await onClick(b);
        } catch (err) {
            toast(err.message, true);
        } finally {
            b.disabled = false;
        }
    });
    return b;
}

/* Asked before the user closes the dialog (Cerrar, a click outside, Esc):
   returning false keeps it open. Closing it from code never asks. */
let closeGuard = null;

export function openModal(title, bodyNode, {wide = false, className = "", beforeClose = null} = {}) {
    $("#modal-title").textContent = title;
    const body = $("#modal-body");
    body.replaceChildren(bodyNode);
    const card = $(".modal-card");
    card.className = "modal-card";
    card.classList.toggle("is-wide", wide);
    if (className) card.classList.add(className);
    closeGuard = beforeClose;
    $("#modal").hidden = false;
}

export function closeModal() {
    closeGuard = null;
    $("#modal").hidden = true;
    $("#modal-body").replaceChildren();
}

/** The user wants the dialog gone: the dialog may still say no (unsaved changes). */
export function requestCloseModal() {
    if ($("#modal").hidden) return;
    if (closeGuard && closeGuard() === false) return;
    closeModal();
}

export async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        toast("Copiado");
    } catch {
        toast("No se pudo copiar automáticamente: selecciónalo y usa Cmd+C", true);
    }
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

export const pad = (n) => String(n).padStart(2, "0");
export const minutesToTime = (m) => `${pad(Math.floor(m / 60) % 24)}:${pad(m % 60)}`;
export const timeToMinutes = (t) => {
    const [h, m] = t.split(":").map(Number);
    return h * 60 + m;
};
export const windowLabel = (w) =>
    `${minutesToTime(w.start)} – ${w.end === 1440 ? "24:00" : minutesToTime(w.end)}`;

export function fmtBytes(n) {
    if (n == null) return "—";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtDuration(seconds) {
    if (seconds == null) return "—";
    const s = Math.floor(seconds);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d} d ${h} h`;
    if (h) return `${h} h ${m} min`;
    return `${m} min`;
}

export function statusLabel(status) {
    return {
        QUEUED: "en cola", SCHEDULED: "programado", UPLOADING: "subiendo",
        PROCESSING: "procesando", DELEGATED: "en Zernio", PUBLISHED: "publicado",
        FAILED: "fallido",
    }[status] || status;
}

/* ------------------------------------------------------------------ */
/* Accounts                                                            */

/* ------------------------------------------------------------------ */

export function accounts() {
    return state.data ? state.data.accounts : [];
}

export function currentAccount() {
    const all = accounts();
    return all.find((a) => a.account.id === state.accountId) || all[0] || null;
}

export function setAccount(id) {
    const changed = Number(id) !== state.accountId;
    state.accountId = Number(id);
    state.formDirty = false;
    /* The queues belong to the account: never carry one across. */
    if (changed) setQueue(null);
    try {
        localStorage.setItem("tts.accountId", String(state.accountId));
    } catch { /* ignore */
    }
}

export function accountLabel(account) {
    return account.label || account.display_name || (account.username ? `@${account.username}` : `Cuenta ${account.id}`);
}

/* The two ceilings every queue of an account shares, in one phrase. Edited in
   Cuentas; Programación only recalls them. */
export function accountLimits(account) {
    const cap = account.max_per_day != null
        ? `${account.max_per_day} vídeos/día` : "sin máximo diario";
    return `${cap} · ${account.min_gap_minutes} min entre publicaciones`;
}

/* ------------------------------------------------------------------ */
/* Queues (colas)                                                      */

/* ------------------------------------------------------------------ */

export function queues(entry = currentAccount()) {
    return entry ? entry.queues || [] : [];
}

export function currentQueue() {
    const all = queues();
    return all.find((q) => q.schedule.id === state.queueId)
        || all.find((q) => !q.schedule.hidden) || all[0] || null;
}

export function setQueue(id) {
    state.queueId = id === null ? null : Number(id);
    state.formDirty = false;
    try {
        if (state.queueId === null) localStorage.removeItem("tts.queueId");
        else localStorage.setItem("tts.queueId", String(state.queueId));
    } catch { /* ignore */
    }
}

/* Every queue of every account, for the "move to…" menus. */
export function everyQueue() {
    return accounts().flatMap((entry) =>
        (entry.queues || []).map((q) => ({account: entry.account, schedule: q.schedule, stats: q.stats})));
}


/* ------------------------------------------------------------------ */
/* Platforms                                                           */

/* ------------------------------------------------------------------ */

export const PLATFORM_NAMES = {tiktok: "TikTok", youtube: "YouTube"};

export function platformOf(account) {
    return account?.platform === "youtube" ? "youtube" : "tiktok";
}

export function setPlatform(value) {
    state.platform = PLATFORM_NAMES[value] ? value : "";
    try {
        localStorage.setItem("tts.platform", state.platform);
    } catch { /* ignore */
    }
}

/** The accounts of the platform picked in the filter (all of them with none). */
export function accountsOfPlatform(entries = accounts()) {
    return state.platform ? entries.filter((e) => platformOf(e.account) === state.platform) : entries;
}

/** «Todas · 4», «TikTok · 2», «YouTube · 2»; hidden when there is only one platform. */
export function drawPlatformChips(box, onChange) {
    const all = accounts();
    const counts = {tiktok: 0, youtube: 0};
    all.forEach((e) => {
        counts[platformOf(e.account)] += 1;
    });
    const present = Object.keys(counts).filter((k) => counts[k]);
    if (state.platform && !present.includes(state.platform)) setPlatform("");
    box.hidden = present.length < 2;
    if (box.hidden) {
        box.replaceChildren();
        return;
    }
    box.replaceChildren(...[["", "Todas", all.length], ...present.map((k) => [k, PLATFORM_NAMES[k], counts[k]])]
        .map(([value, label, n]) => {
            const chip = el("button", `chip${state.platform === value ? " is-active" : ""}`);
            chip.type = "button";
            chip.setAttribute("aria-pressed", String(state.platform === value));
            if (value) chip.append(el("span", `lib-plat ${value}`, label));
            else chip.append(document.createTextNode(label));
            chip.append(el("small", "chip-count", ` ${n}`));
            chip.addEventListener("click", () => {
                if (state.platform === value) return;
                setPlatform(value);
                onChange();
            });
            return chip;
        }));
}

/** Each account keeps its colour everywhere (its place in the list of accounts). */
export function accountColor(accountId) {
    const index = accounts().findIndex((e) => e.account.id === accountId);
    return `var(--acc-${(index < 0 ? 0 : index) % 8})`;
}
