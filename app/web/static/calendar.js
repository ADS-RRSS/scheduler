/* Calendario: the week of every account at a glance.

   One column per day and, inside it, one lane per account, with every post at
   its minute: what went out, what Zernio holds, the slot a local queue has
   pinned, and — as a simulation — the hour the rules would give to what is
   still waiting (/api/calendar, services/calendar.py). A click on a post opens
   its queue with the video in view; a click on a day, that day's list. */

import {
    $,
    accountColor,
    accountLabel,
    accounts,
    api,
    button,
    closeModal,
    drawPlatformChips,
    el,
    openModal,
    PLATFORM_NAMES,
    setAccount,
    setQueue,
    state,
} from "./shared.js";

const HOUR_PX = 36;
// The simulation is not cheap and the week does not change by the second.
const REFRESH_MS = 60000;
const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
export const KINDS = {
    published: {icon: "✓", label: "publicado"},
    delegated: {icon: "🅩", label: "en Zernio (publica solo)"},
    handover: {icon: "⇡", label: "subiéndose a Zernio"},
    scheduled: {icon: "⏱", label: "programado aquí"},
    uploading: {icon: "⇡", label: "subiéndose ahora"},
    projected: {icon: "◌", label: "previsto por las reglas"},
    failed: {icon: "✕", label: "fallido"},
};
// The queue list's status filter that shows a video of each kind.
const QUEUE_FILTER = {
    published: "published", delegated: "delegated", handover: "delegated", scheduled: "pending",
    uploading: "pending", projected: "pending", failed: "failed",
};

const cal = {
    /* The Monday on screen (YYYY-MM-DD); null = this week, as the server counts it. */
    start: null,
    projected: true,
    data: null,
    key: null,
    loadedAt: 0,
    loading: null,
    error: "",
};

/* ------------------------------------------------------------------ */
/* Data                                                                */

/* ------------------------------------------------------------------ */

function shiftDate(iso, days) {
    const [y, m, d] = iso.split("-").map(Number);
    const moved = new Date(Date.UTC(y, m - 1, d + days));
    return moved.toISOString().slice(0, 10);
}

function query() {
    const params = new URLSearchParams();
    if (cal.start) params.set("start", cal.start);
    if (state.platform) params.set("platform", state.platform);
    if (!cal.projected) params.set("projected", "false");
    return params.toString();
}

async function load() {
    if (cal.loading) return cal.loading;
    const key = query();
    cal.loading = (async () => {
        $("#cal-status").textContent = "Calculando…";
        try {
            cal.data = await api(`/api/calendar${key ? `?${key}` : ""}`);
            cal.start = cal.data.start;
            cal.error = "";
        } catch (err) {
            // An app started before the calendar existed answers «Not Found».
            cal.error = /not found/i.test(err.message)
                ? "El calendario necesita la versión nueva del servidor: reinicia la aplicación."
                : err.message;
        }
        cal.key = query();
        cal.loadedAt = Date.now();
    })();
    try {
        await cal.loading;
    } finally {
        cal.loading = null;
    }
    draw();
}

/** Called on every refresh of the app: reloads only when needed. */
export function renderCalendarPage() {
    drawPlatformChips($("#cal-platforms"), () => load());
    if (cal.key !== query() || Date.now() - cal.loadedAt > REFRESH_MS) load();
    else if (!$("#cal-grid").childElementCount) draw();
}

/* ------------------------------------------------------------------ */
/* Drawing                                                             */

/* ------------------------------------------------------------------ */

function rangeLabel(days) {
    const first = days[0].date.split("-").map(Number);
    const last = days[days.length - 1].date.split("-").map(Number);
    const left = first[1] === last[1] ? `${first[2]}` : `${first[2]} ${MONTHS[first[1] - 1]}`;
    return `${left} – ${last[2]} ${MONTHS[last[1] - 1]} ${last[0]}`;
}

function labelOf(accountId) {
    const entry = accounts().find((e) => e.account.id === accountId);
    return entry ? accountLabel(entry.account)
        : cal.data?.accounts.find((a) => a.id === accountId)?.label || `Cuenta ${accountId}`;
}

/** Open the queue a post belongs to, with the post in view. */
function openMark(mark) {
    if (!mark.schedule_id) {
        if (mark.url) window.open(mark.url, "_blank", "noopener");
        return;
    }
    closeModal();
    setAccount(mark.account_id);
    setQueue(mark.schedule_id);
    state.queueFilter = QUEUE_FILTER[mark.kind] || "all";
    state.queueScope = {folders: [], labels: [], match: "all"};
    state.queueFocus = {id: mark.video_id, until: Date.now() + 6000, scrolled: false};
    location.hash = "#/cola";
}

function describe(mark) {
    return `${mark.time} · ${labelOf(mark.account_id)} · ${mark.queue || "sin cola"}\n`
        + `${mark.name}\n${KINDS[mark.kind]?.icon || ""} ${KINDS[mark.kind]?.label || mark.kind}`;
}

function drawLegend(data) {
    const legend = $("#cal-legend");
    legend.replaceChildren();
    data.accounts.forEach((a) => {
        const item = el("span", "cal-legend-item");
        const swatch = el("span", "cal-swatch");
        swatch.style.setProperty("--c", accountColor(a.id));
        item.append(swatch, el("span", `lib-plat ${a.platform}`, PLATFORM_NAMES[a.platform] || a.platform),
            document.createTextNode(` ${a.label}${a.is_active ? "" : " (desactivada)"}`));
        legend.append(item);
    });
    legend.append(el("span", "cal-legend-sep"));
    Object.entries(KINDS).filter(([kind]) => kind !== "uploading").forEach(([kind, info]) => {
        if (kind === "projected" && !cal.projected) return;
        const item = el("span", "cal-legend-item muted");
        const swatch = el("span", `cal-swatch ${kind}`);
        swatch.style.setProperty("--c", "var(--muted)");
        item.append(swatch, document.createTextNode(info.label));
        legend.append(item);
    });
}

function openDay(day, marks) {
    const body = el("div", "cal-agenda");
    if (!marks.length) {
        body.append(el("div", "empty", "Nada este día."));
    } else {
        marks.forEach((mark) => {
            const row = el("button", `cal-agenda-row ${mark.kind}`);
            row.type = "button";
            const dot = el("span", "cal-swatch");
            dot.style.setProperty("--c", accountColor(mark.account_id));
            row.append(el("span", "cal-agenda-time", mark.time), dot,
                el("span", "cal-agenda-who", `${labelOf(mark.account_id)} · ${mark.queue || "sin cola"}`),
                el("span", "cal-agenda-name", mark.name),
                el("span", "cal-agenda-kind", `${KINDS[mark.kind]?.icon || ""} ${KINDS[mark.kind]?.label || mark.kind}`));
            row.title = mark.schedule_id ? "Abrir su cola con el vídeo a la vista" : "";
            row.addEventListener("click", () => openMark(mark));
            body.append(row);
        });
    }
    const bar = el("div", "actions-bar");
    bar.append(button("Cerrar", "btn btn-ghost", closeModal));
    body.append(bar);
    openModal(`${day.label} · ${marks.length} publicación(es)`, body, {wide: true});
}

function draw() {
    if (state.page !== "calendario") return;
    const grid = $("#cal-grid");
    const data = cal.data;
    $("#cal-projected").checked = cal.projected;
    if (cal.error) {
        $("#cal-status").textContent = "";
        $("#cal-notes").replaceChildren(el("div", "note note-warn", cal.error));
        if (!data) {
            grid.replaceChildren();
            return;
        }
    }
    if (!data) return;
    $("#cal-range").textContent = rangeLabel(data.days);
    const counted = data.marks.filter((m) => m.kind !== "failed").length;
    $("#cal-status").textContent = `${counted} publicación(es) · ${data.timezone}`;
    if (!cal.error) {
        $("#cal-notes").replaceChildren(...data.notes.map((n) => el("div", "note note-warn", n)));
    }
    drawLegend(data);

    const lanes = Math.max(data.accounts.length, 1);
    const laneOf = new Map(data.accounts.map((a, i) => [a.id, i]));
    grid.style.setProperty("--cal-days", String(data.days.length));
    grid.style.setProperty("--cal-hour", `${HOUR_PX}px`);
    const byDay = new Map(data.days.map((d) => [d.date, []]));
    data.marks.forEach((m) => byDay.get(m.day)?.push(m));

    const nodes = [el("div", "cal-corner")];
    data.days.forEach((day) => {
        const head = el("button", `cal-dayhead${day.today ? " is-today" : ""}`);
        head.type = "button";
        head.append(el("span", "cal-dayname", day.label));
        const totals = el("span", "cal-totals");
        const perAccount = data.totals[day.date] || {};
        data.accounts.forEach((a) => {
            const n = perAccount[String(a.id)] || 0;
            const total = el("span", `cal-total${a.max_per_day != null && n > a.max_per_day ? " over" : ""}`);
            total.style.setProperty("--c", accountColor(a.id));
            total.append(document.createTextNode(String(n)));
            if (a.max_per_day != null) total.append(el("small", null, `/${a.max_per_day}`));
            total.title = `${a.label}: ${n} ese día${a.max_per_day != null ? ` (máximo ${a.max_per_day})` : ""}`;
            totals.append(total);
        });
        head.append(totals);
        head.title = "Ver la lista de ese día";
        head.addEventListener("click", () => openDay(day, byDay.get(day.date) || []));
        nodes.push(head);
    });

    const hours = el("div", "cal-hours");
    for (let h = 1; h < 24; h += 1) {
        const label = el("span", null, `${String(h).padStart(2, "0")}:00`);
        label.style.top = `${h * HOUR_PX}px`;
        hours.append(label);
    }
    nodes.push(hours);

    data.days.forEach((day) => {
        const column = el("div", `cal-day${day.today ? " is-today" : ""}`);
        column.dataset.day = day.date;
        (byDay.get(day.date) || []).forEach((m) => {
            const mark = el("button", `cal-mark ${m.kind}`);
            mark.type = "button";
            const lane = laneOf.get(m.account_id) ?? 0;
            mark.style.top = `${(m.minute / 1440) * 100}%`;
            mark.style.left = `calc(${lane} * 100% / ${lanes} + 2px)`;
            mark.style.width = `calc(100% / ${lanes} - 4px)`;
            mark.style.setProperty("--c", accountColor(m.account_id));
            mark.title = describe(m);
            mark.setAttribute("aria-label", describe(m).replaceAll("\n", " · "));
            mark.addEventListener("click", () => openMark(m));
            column.append(mark);
        });
        if (day.today) {
            const now = el("div", "cal-now");
            now.style.top = `${(data.now_minute / 1440) * 100}%`;
            now.title = "Ahora";
            column.append(now);
        }
        nodes.push(column);
    });
    grid.replaceChildren(...nodes);
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */

/* ------------------------------------------------------------------ */

export function wireCalendar() {
    const go = (days) => {
        cal.start = days === null ? null : shiftDate(cal.start || cal.data?.start
            || new Date().toISOString().slice(0, 10), days);
        load();
    };
    $("#cal-prev").addEventListener("click", () => go(-7));
    $("#cal-next").addEventListener("click", () => go(7));
    $("#cal-today").addEventListener("click", () => go(null));
    $("#cal-projected").addEventListener("change", (e) => {
        cal.projected = e.target.checked;
        load();
    });
}
