/* Scheduler — UI shell, router and the day-to-day pages.
   Admin pages (Cuentas, Base de datos, Ajustes) live in admin.js. */

import {
    $,
    $$,
    accountColor,
    accountLabel,
    accountLimits,
    accounts,
    accountsOfPlatform,
    api,
    button,
    closeModal,
    currentAccount,
    currentQueue,
    drawPlatformChips,
    el,
    everyQueue,
    fillSelect,
    fmtBytes,
    fmtDate,
    fmtDuration,
    link,
    minutesToTime,
    openModal,
    PLATFORM_NAMES,
    platformOf,
    queues,
    requestCloseModal,
    setAccount,
    setQueue,
    state,
    statusLabel,
    timeToMinutes,
    toast,
    windowLabel,
} from "./shared.js";
import {
    forgetAccountsDrawing,
    renderAccountsPage,
    renderDbPage,
    renderSettingsPage,
    wireAdmin,
    wireConnectForm,
} from "./admin.js";
import {forgetLibraryDrawing, openLibraryPicker, renderLibraryPage, wireLibrary} from "./library.js";
import {isEmptyScope, labelColor, paint, scopeFolders, scopeParams, scopePicker, scopeSuffix,} from "./labels.js";
import {fixedHashtags, openComposer} from "./composer.js";
import {renderCalendarPage, wireCalendar} from "./calendar.js";
import {dateTimePicker, nowIn, shortLabel} from "./datetimepicker.js";

const MUSIC_USAGE_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en";
const BRANDED_CONTENT_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en";
const ACCOUNT_PAGES = new Set(["cola", "programacion"]);
const PAGES = ["resumen", "biblioteca", "cola", "programacion", "calendario", "analiticas",
    "comentarios", "actividad", "cuentas", "db", "ajustes"];
const PLATFORM_LABEL = {tiktok: "TikTok", youtube: "YouTube"};
const WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];

/* ------------------------------------------------------------------ */
/* Router                                                              */

/* ------------------------------------------------------------------ */

function parseHash() {
    const raw = (location.hash || "#/resumen").replace(/^#\/?/, "");
    const [page, ...rest] = raw.split("/");
    return {page: PAGES.includes(page) ? page : "resumen", arg: rest.join("/") || null};
}

/* Cola and Programación carry the account and the queue in the address
   (#/cola/<cuenta>/<cola>), so a reload or a shared link opens that same queue.
   The hash is read once when it arrives (freshArg) and rewritten from the
   state after that: clicking another queue adds a history entry, a rename only
   corrects the current one. */
let freshArg = false;
let syncedIds = null;

function slugify(text) {
    return String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "")
        .toLowerCase().replace(/[^a-z0-9._]+/g, "-").replace(/^[-.]+|[-.]+$/g, "");
}

function accountSlug(account) {
    const base = (entry) => slugify(entry.account.username || accountLabel(entry.account))
        || `cuenta-${entry.account.id}`;
    const all = accounts();
    const mine = base({account});
    const twins = all.filter((e) => base(e) === mine);
    if (twins.length < 2) return mine;
    const withPlatform = `${mine}-${platformOf(account)}`;
    return twins.filter((e) => platformOf(e.account) === platformOf(account)).length < 2
        ? withPlatform : `${mine}-${account.id}`;
}

function queueSlug(entry, schedule) {
    const base = (s) => slugify(s.name) || `cola-${s.id}`;
    const mine = base(schedule);
    return queues(entry).filter((q) => base(q.schedule) === mine).length < 2
        ? mine : `${mine}-${schedule.id}`;
}

function queuePath() {
    const entry = currentAccount();
    const queue = currentQueue();
    if (!entry || !queue) return null;
    return `${accountSlug(entry.account)}/${queueSlug(entry, queue.schedule)}`;
}

/** Applies a freshly arrived #/cola/<cuenta>/<cola> to the state, then writes
 *  the state's queue back into the address. Unknown slugs fall back to the
 *  queue already selected. */
function followQueueUrl(page) {
    const fresh = freshArg;
    freshArg = false;
    if (fresh && ACCOUNT_PAGES.has(page) && state.pageArg) {
        const [accPart, queuePart] = state.pageArg.split("/");
        const entry = accounts().find((e) =>
            accountSlug(e.account) === accPart || String(e.account.id) === accPart);
        if (entry) {
            if (entry.account.id !== state.accountId) setAccount(entry.account.id);
            const queue = queues(entry).find((q) =>
                queueSlug(entry, q.schedule) === queuePart || String(q.schedule.id) === queuePart);
            if (queue) setQueue(queue.schedule.id);
        }
    }
    const path = queuePath();
    const entry = currentAccount();
    const queue = currentQueue();
    const ids = queue ? `${entry.account.id}/${queue.schedule.id}` : null;
    ["cola", "programacion"].forEach((name) => {
        $(`#mainnav a[data-page="${name}"]`).href = path ? `#/${name}/${path}` : `#/${name}`;
    });
    if (!ACCOUNT_PAGES.has(page) || !path) return;
    if (path !== state.pageArg) {
        const push = !fresh && state.pageArg && ids !== syncedIds;
        history[push ? "pushState" : "replaceState"](null, "", `#/${page}/${path}`);
        state.pageArg = path;
    }
    syncedIds = ids;
}

function route() {
    const {page, arg} = parseHash();
    const changed = page !== state.page || arg !== state.pageArg;
    state.page = page;
    state.pageArg = arg;
    freshArg = true;
    if (changed) state.formDirty = false;
    if (changed && page === "biblioteca") forgetLibraryDrawing();
    // Leaving Cuentas drops what was typed there and not saved.
    if (changed) forgetAccountsDrawing();
    renderAll();
    if (changed) window.scrollTo({top: 0});
}

/* ------------------------------------------------------------------ */
/* Shell                                                               */

/* ------------------------------------------------------------------ */

function renderShell() {
    const {health} = state.data;
    const all = accounts();
    const hasAccounts = all.length > 0;
    const page = hasAccounts ? state.page : "onboarding";
    if (hasAccounts) followQueueUrl(page);

    $$(".page").forEach((section) => {
        section.hidden = section.dataset.page !== page;
    });
    $$("#mainnav a").forEach((a) => a.classList.toggle("is-active", a.dataset.page === page));
    // A submenu is lit while one of its pages is open; the logo is Resumen's entry.
    $$(".navmenu").forEach((menu) => menu.classList.toggle("is-active",
        menu.dataset.pages.split(" ").includes(page)));
    $("#brand").classList.toggle("is-active", page === "resumen");
    $("#mainnav").classList.toggle("is-disabled", !hasAccounts);
    $("#pause-btn").hidden = !hasAccounts;
    $("#pause-btn").textContent = health.paused ? "▶ Reanudar" : "⏸ Pausar";

    const dot = $("#health-dot");
    dot.className = "dot " + (health.paused ? "paused" : health.scheduler_running ? "ok" : "err");
    dot.title = health.paused
        ? "Programación pausada"
        : health.scheduler_running
            ? `Planificador activo · último ciclo ${fmtDate(health.scheduler_last_tick)}`
            : "Planificador detenido";

    const problems = [];
    if (!health.ffmpeg)
        problems.push("ffmpeg/ffprobe no encontrado: los vídeos se subirán sin validación previa.");
    if (health.scheduler_error) problems.push(`Error del planificador: ${health.scheduler_error}`);
    if (health.paused) problems.push("La programación está pausada.");
    const inactive = all.filter((a) => !a.account.is_active).length;
    if (inactive) problems.push(`${inactive} cuenta(s) desactivada(s): no publican.`);
    if (health.disk && health.disk.low)
        problems.push(`Queda poco disco: ${fmtBytes(health.disk.free)} libres de ${fmtBytes(health.disk.total)}. `
            + "Las subidas a la biblioteca y la base de datos lo necesitan.");
    const banner = $("#banner");
    banner.hidden = problems.length === 0;
    banner.textContent = problems.join("  ·  ");

    /* Account switcher: only where the page is about one account. */
    const select = $("#account-switch");
    select.hidden = !(hasAccounts && ACCOUNT_PAGES.has(page)) || all.length < 2;
    if (!select.hidden) {
        fillSelect(select, all.map((entry) => [entry.account.id, accountLabel(entry.account)
        + (entry.account.is_active ? "" : " · desactivada")]), state.accountId);
    }

    renderQueueBar(page);

    const pending = all.reduce((sum, a) => sum + shownStat(a, "queued") + shownStat(a, "in_flight"), 0);
    $("#pill-queue").textContent = pending;
    $("#pill-queue").hidden = pending === 0;

    const shelving = state.data.library?.writing || 0;
    $("#pill-library").textContent = `✍️ ${shelving}`;
    $("#pill-library").hidden = shelving === 0;
    $("#pill-library").title = `${shelving} vídeo(s) de la biblioteca escribiendo sus textos`;

    // Shown on «Seguimiento» too: folded away, the count must not go unseen.
    const unanswered = state.data.comments_pending || 0;
    ["#pill-comments", "#pill-seguimiento"].forEach((id) => {
        $(id).textContent = unanswered;
        $(id).hidden = unanswered === 0;
    });
    $("#pill-seguimiento").title = `${unanswered} comentario(s) sin responder`;
}

/* The queue picker, shown on the two pages that are about one queue. */
function renderQueueBar(page) {
    const bar = $("#queue-bar");
    const slot = page === "cola" ? $("#cola-queue-slot")
        : page === "programacion" ? $("#prog-queue-slot") : null;
    bar.hidden = slot === null;
    if (slot === null) return;
    if (bar.parentElement !== slot) slot.append(bar);

    const active = currentQueue();
    // The hidden queues go last, pushed to the right edge: still at hand, out
    // of the way, and never the one opened by default (see currentQueue).
    const all = [...queues().filter((q) => !q.schedule.hidden), ...queues().filter((q) => q.schedule.hidden)];
    const firstHidden = all.find((q) => q.schedule.hidden);
    $("#queue-tabs").replaceChildren(
        ...all.map((entry) => {
            const {schedule, stats} = entry;
            const isActive = active && schedule.id === active.schedule.id;
            const tab = el("button", `queue-tab${isActive ? " is-active" : ""}${schedule.hidden ? " is-hidden" : ""}`
                + (entry === firstHidden ? " is-first-hidden" : ""));
            tab.type = "button";
            tab.append(
                el("span", `queue-dot ${schedule.enabled ? "on" : "off"}`,
                    schedule.enabled ? "●" : "○"),
                el("span", "queue-tab-name", schedule.name),
                ...(schedule.start_date ? [el("span", "queue-tab-start", "⏳")] : []),
                ...(schedule.hidden ? [el("span", "queue-tab-start", "🙈")] : []),
                el("span", "queue-tab-count", stats.queued),
            );
            tab.title = (schedule.hidden ? "Oculta: no sale en la Biblioteca ni se abre por defecto · " : "")
                + (schedule.start_date ? `Empieza el ${startLabel(schedule)} · ` : "")
                + (schedule.enabled
                    ? `Programación activa · próxima ${schedule.next_slot_local || "sin hueco"}`
                    : "Programación parada");
            tab.addEventListener("click", () => {
                setQueue(schedule.id);
                renderAll();
            });
            return tab;
        })
    );
    $("#queue-tabs").classList.toggle("is-single", all.length < 2);
}

function renderOnboarding() {
    const {health} = state.data;
    const reqs = [
        [health.scheduler_running, "Planificador en marcha"],
        [health.ffmpeg, "ffmpeg instalado (validación previa de los vídeos)"],
        [health.passcode_enabled, "Código de acceso configurado (UI_PASSCODE en .env) · recomendable"],
    ];
    $("#onboarding-requirements").replaceChildren(
        ...reqs.map(([ok, label]) => {
            const row = el("div", `req ${ok ? "ok" : "bad"}`);
            row.append(el("span", "mark", ok ? "✓" : "✕"), el("span", null, label));
            return row;
        })
    );
    const form = $("#connect-form");
    $("#onboarding-form-slot").append(form);
    form.hidden = false;
}

/* ------------------------------------------------------------------ */
/* Resumen                                                             */

/* ------------------------------------------------------------------ */

/** An account's number without its hidden queues: those do not count in the
 queue counters (header pill, Resumen), even when they keep publishing. */
function shownStat(entry, key) {
    const away = entry.queues.filter((q) => q.schedule.hidden)
        .reduce((n, q) => n + (q.stats[key] || 0), 0);
    return (entry.stats[key] || 0) - away;
}

/** The numbers of the accounts on screen, added up, in one line. */
function renderOverviewTotals(entries) {
    const box = $("#overview-totals");
    const sum = (key) => entries.reduce((n, e) => n + (e.stats[key] || 0), 0);
    const queued = entries.reduce((n, e) => n + shownStat(e, "queued"), 0);
    const parts = [
        [`${entries.length} cuenta(s)`],
        [`${queued} en cola`],
        ...(sum("delegated") ? [[`${sum("delegated")} en Zernio`]] : []),
        [`${sum("today")} hoy`],
        [`${sum("published")} publicados`],
        ...(sum("failed") ? [[`${sum("failed")} fallidos`, "bad"]] : []),
    ];
    box.replaceChildren(...parts.map(([text, cls]) => el("span", `overview-total${cls ? ` ${cls}` : ""}`, text)));
}

/** Hide a queue from the library and the queue tabs, or show it again. */
async function toggleHidden(schedule) {
    await api(`/api/schedules/${schedule.id}`, {
        method: "PATCH", body: JSON.stringify({hidden: !schedule.hidden}),
    });
    toast(schedule.hidden
        ? `«${schedule.name}» vuelve a verse`
        : `«${schedule.name}» oculta${schedule.enabled ? " (sigue activa: publica igual)" : ""}`);
    await state.refresh();
}

function hideButton(schedule) {
    const b = button(schedule.hidden ? "Mostrar" : "Ocultar", "btn btn-ghost btn-sm",
        () => toggleHidden(schedule));
    b.title = schedule.hidden
        ? "Volver a ofrecerla en la Biblioteca y con las demás pestañas de cola"
        : "Que no salga en la Biblioteca y quede al final de las pestañas, sin abrirse por defecto. Si está activa, sigue publicando.";
    return b;
}

function renderOverview() {
    drawPlatformChips($("#overview-platforms"), renderOverview);
    const every = accounts();
    const all = accountsOfPlatform(every);
    renderOverviewTotals(all);
    const cards = $("#overview-cards");
    cards.replaceChildren(
        ...all.map((entry) => {
            const {account, stats, videos} = entry;
            const running = entry.queues.filter((q) => q.schedule.enabled).length;
            const card = el("div", `card account-card${account.is_active ? "" : " is-inactive"}`);
            // The same colour as its lane in the calendar.
            card.style.setProperty("--c", accountColor(account.id));
            const head = el("div", "account-card-head");
            const name = el("div", "account-card-name");
            const platform = platformOf(account);
            name.append(el("span", `lib-plat ${platform}`, PLATFORM_NAMES[platform]),
                el("strong", null, accountLabel(account)));
            head.append(name);
            const badges = el("div", "badges");
            badges.append(el("span", `badge ${running ? "PUBLISHED" : ""}`,
                running
                    ? `${running} de ${entry.queues.length} cola(s) activa(s)`
                    : `${entry.queues.length} cola(s) · ninguna activa`));
            if (!account.is_active) badges.append(el("span", "badge FAILED", "desactivada"));
            head.append(badges);
            card.append(head);
            if (account.username) card.append(el("div", "muted small", `@${account.username}${account.profile_name ? ` · ${account.profile_name}` : ""}`));

            const nums = el("div", "mini-stats");
            [["En cola", shownStat(entry, "queued")],
                ...(stats.delegated ? [["En Zernio", stats.delegated]] : []),
                ["Hoy", `${stats.today}${account.max_per_day != null ? ` / ${account.max_per_day}` : ""}`],
                ["Publicados", stats.published], ["Fallidos", stats.failed]].forEach(([label, value]) => {
                const s = el("div", "mini-stat");
                s.append(el("div", "value", value), el("div", "label", label));
                nums.append(s);
            });
            card.append(nums);

            const inFlight = videos.find((v) => ["UPLOADING", "PROCESSING"].includes(v.status));
            if (inFlight) card.append(el("div", "small next-line", `Subiendo ahora: ${inFlight.original_name}`));

            /* One line per queue: the whole point is seeing them side by side. */
            const list = el("div", "queue-rows");
            entry.queues.forEach((q) => {
                const {schedule: s, stats: qs} = q;
                const row = el("div", `queue-row${s.hidden ? " is-hidden" : ""}`);
                const head = el("div", "queue-row-head");
                head.append(el("span", `queue-dot ${s.enabled ? "on" : "off"}`, s.enabled ? "●" : "○"),
                    el("strong", null, s.name), ...(s.hidden ? [el("span", "muted small", "🙈 oculta")] : []));
                row.append(head);
                row.append(el("span", "muted small",
                    `${qs.queued} en cola · ${windowsSummary(s)} · `
                    + (s.start_date ? `empieza ${startLabel(s)} · ` : "")
                    + (s.enabled ? (s.next_slot_local ? `próxima ${s.next_slot_local}` : "sin hueco válido")
                        : "parada")));
                const go = el("div", "actions-bar");
                go.append(
                    button("Cola", "btn btn-ghost btn-sm", () => {
                        setAccount(account.id);
                        setQueue(s.id);
                        location.hash = "#/cola";
                    }),
                    button("Programación", "btn btn-ghost btn-sm", () => {
                        setAccount(account.id);
                        setQueue(s.id);
                        location.hash = "#/programacion";
                    }),
                    button(s.enabled ? "Parar" : "Activar", "btn btn-ghost btn-sm", async () => {
                        await api(`/api/schedules/${s.id}`, {
                            method: "PATCH", body: JSON.stringify({enabled: !s.enabled}),
                        });
                        toast(s.enabled ? `«${s.name}» parada` : `«${s.name}» activa`);
                        await state.refresh();
                    }),
                    hideButton(s),
                );
                row.append(go);
                list.append(row);
            });
            card.append(list);

            const failed = videos.find((v) => v.status === "FAILED" && v.last_error);
            if (failed) card.append(el("div", "item-error", `Último fallo: ${failed.last_error}`));
            (entry.warnings || []).forEach((w) => card.append(el("div", "note note-warn", w)));

            const actions = el("div", "actions-bar");
            actions.append(
                button("+ Nueva cola", "btn btn-ghost btn-sm", async () => {
                    const created = await api(`/api/accounts/${account.id}/schedules`, {
                        method: "POST", body: JSON.stringify({name: ""}),
                    });
                    setAccount(account.id);
                    setQueue(created.schedule.id);
                    toast(`Cola «${created.schedule.name}» creada`);
                    await state.refresh();
                    location.hash = "#/programacion";
                }),
            );
            card.append(actions);
            return card;
        })
    );

    /* Upcoming posts across every queue of every account, nearest first: the
       slot each queue will release next, plus everything already parked in
       Zernio — which is the only place those show up once handed over. */
    const active = all.filter((a) => a.account.is_active);
    const where = (a, name) => (every.length > 1 ? `${accountLabel(a.account)} · ${name}` : name);
    const upcoming = [
        ...active.flatMap((a) => a.queues
            .filter((q) => q.schedule.enabled && q.schedule.next_slot_at)
            .map((q) => ({
                when: q.schedule.next_slot_at,
                local: q.schedule.next_slot_local,
                label: where(a, q.schedule.name),
                head: queueHead(a.videos, q.schedule.id),
            }))),
        ...active.flatMap((a) => a.videos
            .filter((v) => v.status === "DELEGATED" && v.scheduled_at)
            .map((v) => ({
                when: v.scheduled_at,
                local: v.scheduled_local,
                label: where(a, v.queue_name || "Zernio"),
                head: v,
                zernio: true,
            }))),
    ].sort((x, y) => x.when.localeCompare(y.when)).slice(0, 12);
    const list = $("#overview-upcoming");
    list.replaceChildren(
        ...(upcoming.length ? upcoming.map((u) => {
            const row = el("div", "row");
            row.append(el("span", "idx", u.local),
                el("span", null, `${u.label}${u.head ? ` · ${u.head.original_name}` : " · cola vacía"}`));
            if (u.zernio) row.append(el("span", "badge DELEGATED", "Zernio"));
            return row;
        }) : [el("div", "empty", state.platform
            ? `Ninguna cola de ${PLATFORM_NAMES[state.platform]} tiene programación activa con vídeos dentro.`
            : "Ninguna cola tiene programación activa con vídeos dentro.")])
    );

    // With a platform picked, only its accounts' activity (general entries go with «Todas»).
    const shown = new Set(all.map((e) => e.account.id));
    const events = (state.data.events || [])
        .filter((e) => !state.platform || (e.account_id != null && shown.has(e.account_id)))
        .slice(0, 8);
    $("#overview-events").replaceChildren(
        ...(events.length ? events.map(eventRow) : [el("div", "empty", "Todavía no hay actividad.")])
    );
}

/* The order a queue releases its videos in, as the server applies it
   (queue.RELEASE_ORDER): «Ahora» first, then the turns the last shuffle
   dealt, then arrival for whatever came in after it. /api/state lists the
   videos newest first, so anything about turns sorts with this. */
function releaseOrder(a, b) {
    const turn = (v) => v.queue_rank ?? Infinity;
    // Two unshuffled videos give Infinity - Infinity = NaN, which falls through to the id.
    return a.priority - b.priority || turn(a) - turn(b) || a.id - b.id;
}

function arrivalOrder(a, b) {
    return a.priority - b.priority || a.id - b.id;
}

/* The videos of one queue still waiting for their turn, in release order. */
function waitingVideos(videos, scheduleId) {
    return videos
        .filter((v) => v.schedule_id === scheduleId && ["SCHEDULED", "QUEUED"].includes(v.status))
        .sort(releaseOrder);
}

/* The video that actually goes out next. */
function queueHead(videos, scheduleId) {
    return waitingVideos(videos, scheduleId)[0];
}

/* Has «Mezclar» left this queue in an order other than the one its videos arrived in? */
function isShuffled(waiting) {
    const arrival = [...waiting].sort(arrivalOrder);
    return waiting.some((v, i) => v.id !== arrival[i].id);
}

function windowsSummary(schedule) {
    const windows = schedule.windows || [];
    if (!windows.length) return "a cualquier hora";
    return windows.map(windowLabel).join(", ");
}

function eventRow(e) {
    const row = el("div", `event ${e.level}`);
    const who = e.account_id ? accounts().find((a) => a.account.id === e.account_id) : null;
    row.append(el("time", null, e.ts_local), el("span", "msg", (who ? `[${accountLabel(who.account)}] ` : "") + e.message));
    return row;
}

/* ------------------------------------------------------------------ */
/* Programación                                                        */

/* ------------------------------------------------------------------ */

function renderStats(entry, queue) {
    const {stats, schedule} = queue;
    const cards = [
        ["En cola", stats.queued],
        ["Publicados", stats.published],
        ["Hoy en esta cola", `${stats.today}${schedule.max_per_day != null ? ` / ${schedule.max_per_day}` : ""}`],
        ["Hoy en la cuenta",
            `${entry.stats.today}${entry.account.max_per_day != null ? ` / ${entry.account.max_per_day}` : ""}`],
    ];
    const row = $("#stat-row");
    row.replaceChildren(
        ...cards.map(([label, value]) => {
            const card = el("div", "stat");
            card.append(el("div", "value", String(value)), el("div", "label", label));
            return card;
        })
    );
    const next = el("div", "stat highlight");
    next.append(
        el("div", "value", schedule.next_slot_local || (schedule.enabled ? "sin hueco" : "—")),
        el("div", "label", "Próxima publicación")
    );
    row.append(next);
}

function renderQueueHeader(entry, queue) {
    const s = queue.schedule;
    const index = entry.queues.findIndex((q) => q.schedule.id === s.id);
    $("#queue-title").textContent = s.name;
    $("#queue-name-inline").textContent = `«${s.name}»`;
    $("#queue-sub").textContent =
        `Cola ${index + 1} de ${entry.queues.length} en ${accountLabel(entry.account)} · `
        + `${windowsSummary(s)} · ${queue.stats.queued} vídeo(s) esperando`
        + (s.start_date ? ` · empieza el ${startLabel(s)}` : "");
    $("#queue-up").disabled = index === 0;
    $("#queue-down").disabled = index === entry.queues.length - 1;
    $("#queue-hide").textContent = s.hidden ? "🙈 Mostrar" : "🙈 Ocultar";
    $("#queue-hide").title = s.hidden
        ? "Volver a ofrecerla en la Biblioteca y con las demás pestañas de cola"
        : "Que no salga en la Biblioteca y quede al final de las pestañas, sin abrirse por defecto. Si está activa, sigue publicando.";
    $("#queue-delete").disabled = entry.queues.length < 2;
    $("#queue-delete").title = entry.queues.length < 2
        ? "Una cuenta necesita al menos una cola" : "";

    // The account's limits are edited in Cuentas, where they belong: they are
    // one setting for every queue. Here they are only recalled, because they
    // cap whatever this queue's own form says.
    const account = entry.account;
    $("#acc-limits-summary").textContent = accountLimits(account);
    $("#acc-limits-link").href = `#/cuentas`;
    $("#acc-warnings").replaceChildren(
        ...(entry.warnings || []).map((w) => el("div", "note note-warn", w))
    );
}

function renderScheduleForm(queue) {
    const s = queue.schedule;
    if (!s) return;
    $("#sch-enabled").checked = s.enabled;
    $("#sch-int-min").value = s.interval_min_minutes;
    $("#sch-int-max").value = s.interval_max_minutes;
    $("#sch-nomax").checked = s.max_per_day === null;
    $("#sch-max").value = s.max_per_day ?? 6;
    $("#sch-max").disabled = s.max_per_day === null;
    $("#sch-tz").value = s.timezone;
    $("#sch-nocomment").checked = s.disable_comment;
    $("#sch-noduet").checked = s.disable_duet;
    $("#sch-nostitch").checked = s.disable_stitch;
    $("#sch-aigc").checked = s.is_aigc;
    $("#sch-disclose").checked = Boolean(s.brand_content_toggle || s.brand_organic_toggle);
    $("#sch-brand-organic").checked = Boolean(s.brand_organic_toggle);
    $("#sch-brand-content").checked = Boolean(s.brand_content_toggle);
    state.windows = (s.windows || []).map((w) => ({...w}));
    state.days = [...(s.days_of_week || [])];
    $("#sch-start").value = s.start_date || "";
    $("#sch-start-time").value = s.start_time || "";

    renderStartPresets();
    renderIntervalPresets();
    renderWindowPresets();
    renderWindows();
    renderDays();
    renderDisclosure();
    renderWarnings(queue.warnings);
}

/* «Empezar a publicar». Days travel as YYYY-MM-DD and hours as HH:MM ("" = 00:00),
   local to the queue's time zone. */
function isoDay(day) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

function dayFromToday(offset) {
    const day = new Date();
    day.setHours(12, 0, 0, 0);
    day.setDate(day.getDate() + offset);
    return day;
}

/** «sáb 17/10» or «sáb 17/10 · 18:30» for a queue's start. */
function startLabel(s) {
    return shortLabel(s.start_date, s.start_time || "");
}

function startPresets() {
    const tomorrow = isoDay(dayFromToday(1));
    const monday = isoDay(dayFromToday(((8 - new Date().getDay()) % 7) || 7));
    return [
        {label: "Ahora", day: ""},
        {label: "Mañana", day: tomorrow},
        ...(monday !== tomorrow ? [{label: "Lunes que viene", day: monday}] : []),
    ];
}

let startPicker = null;

function setStart(day, time) {
    $("#sch-start").value = day;
    $("#sch-start-time").value = day ? time : "";
    markDirty();
    renderStartPresets();
}

function renderStartPresets() {
    const day = $("#sch-start").value;
    const time = $("#sch-start-time").value;
    const presets = startPresets();
    if (!startPicker) {
        startPicker = dateTimePicker({
            placeholder: "Otro día y hora…",
            now: () => nowIn($("#sch-tz").value),
            // The hours the queue opens at, to start with the first one in a click.
            suggestions: () => state.windows.map((w) => ({label: "abre franja", time: minutesToTime(w.start)})),
            onChange: (next) => setStart(next.day, next.time),
        });
        $("#start-pick").replaceChildren(startPicker.node);
    }
    const preset = !time && presets.find((p) => p.day === day);
    $("#start-presets").replaceChildren(
        ...presets.map((p) => {
            const chip = el("button", `chip${preset === p ? " is-active" : ""}`, p.label);
            chip.type = "button";
            if (p.day) chip.append(el("small", null, shortLabel(p.day, "")));
            chip.addEventListener("click", () => setStart(p.day, ""));
            return chip;
        })
    );
    startPicker.set({day, time}, !preset);
    const when = day && (time ? `del ${shortLabel(day, "")} a las ${time}` : `del ${shortLabel(day, "")}`);
    let hint = day
        ? `No publica nada antes ${when}; desde entonces publica sola con las reglas de abajo.`
        : "Publica en cuanto toque. Elige otro día (y hora) para que no reserve ninguna antes.";
    if (day && !$("#sch-enabled").checked) hint += " Actívala arriba o no empezará.";
    $("#start-hint").textContent = hint;
}

function renderIntervalPresets() {
    const min = Number($("#sch-int-min").value);
    const max = Number($("#sch-int-max").value);
    $("#interval-presets").replaceChildren(
        ...state.presets.intervals.map((preset) => {
            const active = preset.min === min && preset.max === max;
            const chip = el("button", `chip${active ? " is-active" : ""}`);
            chip.type = "button";
            chip.append(document.createTextNode(`${preset.label} · ${preset.min}–${preset.max} min`),
                el("small", null, preset.hint));
            chip.addEventListener("click", () => {
                $("#sch-int-min").value = preset.min;
                $("#sch-int-max").value = preset.max;
                markDirty();
                renderIntervalPresets();
            });
            return chip;
        })
    );
}

function sameWindows(a, b) {
    if (a.length !== b.length) return false;
    const key = (w) => `${w.start}-${w.end}`;
    return a.map(key).sort().join() === b.map(key).sort().join();
}

function renderWindowPresets() {
    $("#window-presets").replaceChildren(
        ...state.presets.windows.map((preset) => {
            const active = sameWindows(preset.windows, state.windows);
            const chip = el("button", `chip${active ? " is-active" : ""}`, preset.label);
            chip.type = "button";
            chip.addEventListener("click", () => {
                state.windows = preset.windows.map((w) => ({...w}));
                markDirty();
                renderWindows();
                renderWindowPresets();
            });
            return chip;
        })
    );
}

function renderWindows() {
    const box = $("#window-list");
    box.replaceChildren();
    if (!state.windows.length) {
        box.append(el("span", "muted small", "Indiferente: se puede publicar a cualquier hora."));
        return;
    }
    state.windows.forEach((w, i) => {
        const tag = el("span", "window-tag", windowLabel(w));
        const remove = el("button", null, "×");
        remove.type = "button";
        remove.title = "Quitar franja";
        remove.addEventListener("click", () => {
            state.windows.splice(i, 1);
            markDirty();
            renderWindows();
            renderWindowPresets();
        });
        tag.append(remove);
        box.append(tag);
    });
}

function renderDays() {
    $("#day-chips").replaceChildren(
        ...state.presets.days.map((label, index) => {
            const active = state.days.includes(index);
            const chip = el("button", `chip day${active ? " is-active" : ""}`, label);
            chip.type = "button";
            chip.addEventListener("click", () => {
                if (active) {
                    if (state.days.length === 1) return toast("Debe quedar al menos un día", true);
                    state.days = state.days.filter((d) => d !== index);
                } else {
                    state.days.push(index);
                }
                markDirty();
                renderDays();
            });
            return chip;
        })
    );
}

function renderDisclosure() {
    const on = $("#sch-disclose").checked;
    const organic = on && $("#sch-brand-organic").checked;
    const branded = on && $("#sch-brand-content").checked;
    $("#disclose-options").hidden = !on;

    const hint = $("#disclose-hint");
    let blocking = false;
    if (on && !organic && !branded) {
        hint.hidden = false;
        hint.textContent = "Indica si el contenido te promociona a ti, a un tercero o a ambos para poder guardar.";
        blocking = true;
    } else {
        hint.hidden = true;
    }

    const consent = $("#consent-text");
    consent.replaceChildren();

    // The queue belongs to the account on screen, and the platform to the account.
    const platform = currentAccount()?.account?.platform || "tiktok";

    if (platform === "youtube") {
        consent.append("Al publicar aceptas las condiciones de servicio de YouTube.");
    } else {
        if (branded) {
            consent.append("Al publicar aceptas la ", link("Branded Content Policy", BRANDED_CONTENT_URL),
                " y la ", link("Music Usage Confirmation", MUSIC_USAGE_URL), " de TikTok.");
        } else {
            consent.append("Al publicar aceptas la ", link("Music Usage Confirmation", MUSIC_USAGE_URL), " de TikTok.");
        }
    }
    $("#sch-save").disabled = blocking;
}

function renderWarnings(warnings) {
    $("#sch-warnings").replaceChildren(...(warnings || []).map((w) => el("div", "note note-warn", w)));
}

/* ------------------------------------------------------------------ */
/* Cola                                                                */
/* ------------------------------------------------------------------ */

const QUEUE_FILTERS = [
    ["pending", "Pendientes", (v) => ["QUEUED", "SCHEDULED", "UPLOADING", "PROCESSING"].includes(v.status)],
    ["delegated", "En Zernio", (v) => v.status === "DELEGATED"],
    ["published", "Publicados", (v) => v.status === "PUBLISHED"],
    ["failed", "Fallidos", (v) => v.status === "FAILED"],
    ["all", "Todos", () => true],
];

const inFlight = (v) => (["UPLOADING", "PROCESSING"].includes(v.status) ? 1 : 0);

/* The filters that list videos in the order they will go out: what is uploading
   now, then the queue in turn order; Zernio's, by the hour it publishes them.
   The rest keep the newest-first order of /api/state. */
const QUEUE_SORTS = {
    pending: (a, b) => inFlight(b) - inFlight(a) || releaseOrder(a, b),
    delegated: (a, b) => (a.scheduled_at || "").localeCompare(b.scheduled_at || "") || a.id - b.id,
};

/** Does this video carry TikTok's AI label? A video with no opinion of its own
 *  follows its queue, which is what the publisher does too. */
function aigcOn(video, queue) {
    return video.is_aigc === null || video.is_aigc === undefined
        ? !!(queue && queue.schedule.is_aigc)
        : !!video.is_aigc;
}

const isWaiting = (v) => ["QUEUED", "SCHEDULED"].includes(v.status);
/* Ticked in the list: waiting ones leave the queue, Zernio's come back. */
const isTickable = (v) => isWaiting(v) || v.status === "DELEGATED";

const folderLabels = () => state.data?.library?.folder_labels || {};

/** The library folders the list is narrowed to (several, or by label), or null for all. */
function queueFolders(videos) {
    if (isEmptyScope(state.queueScope)) return null;
    const table = folderLabels();
    const folders = [...new Set(videos.map((v) => v.library_folder).filter(Boolean))]
        .map((folder) => ({folder, labels: table[folder] || []}));
    return scopeFolders(state.queueScope, folders);
}

/** The queue's videos, narrowed to the folders and labels picked above the list. */
function inQueueFolder(videos) {
    const keep = queueFolders(videos);
    return keep ? videos.filter((v) => keep.has(v.library_folder)) : videos;
}

/** What «Enviar» and «Recuperar» act on, as the API takes it. */
function queueScope() {
    return scopeParams(state.queueScope);
}

let queuePicker = null;

function renderQueueFolders(videos) {
    const counts = new Map();
    videos.forEach((v) => {
        if (v.library_folder) counts.set(v.library_folder, (counts.get(v.library_folder) || 0) + 1);
    });
    const table = folderLabels();
    // A folder or a label that left the queue stops narrowing it.
    const scope = state.queueScope;
    const present = new Set([...counts.keys()].flatMap((f) => table[f] || []));
    const folders = scope.folders.filter((f) => counts.has(f));
    const labels = scope.labels.filter((l) => present.has(l));
    if (folders.length !== scope.folders.length || labels.length !== scope.labels.length)
        state.queueScope = {...scope, folders, labels};
    const box = $("#queue-scope");
    box.hidden = counts.size < 2 && !present.size && isEmptyScope(state.queueScope);
    if (!queuePicker) {
        queuePicker = scopePicker({
            ariaLabel: "Carpetas y etiquetas de la cola",
            // A list that shrinks under the open panel would scroll the page and move it.
            onOpen: (open) => {
                const list = $("#queue-list");
                list.style.minHeight = open ? `${list.offsetHeight}px` : "";
            },
            onChange: (next) => {
                state.queueScope = next;
                state.queueSelected.clear();
                const entry = currentAccount();
                const queue = currentQueue();
                if (entry && queue) renderQueue(entry, queue);
            },
        });
        box.replaceChildren(queuePicker.node);
    }
    queuePicker.update([...counts.keys()].sort().map((folder) => ({
        folder, labels: table[folder] || [], count: counts.get(folder),
    })), state.queueScope);
}

/** «📁 habitos ●●»: the folder of a queued video, with its labels' colours. */
function folderMark(folder, first) {
    const node = el("span", "queue-folder-mark", `${first ? "" : " · "}📁 ${folder}`);
    (folderLabels()[folder] || []).forEach((label) => {
        const dot = paint(el("span", "lib-folder-dot"), labelColor(label));
        dot.title = label;
        node.append(dot);
    });
    return node;
}

/* ------------------------------------------------------------------ */
/* Moving videos by hand inside the queue                              */

/* While a turn is being typed or a row dragged, the polling must not redraw
   the list from under the hand: it waits, and draws once it is let go. */
let queueHold = false;
let queueStale = false;

function holdQueue() {
    queueHold = true;
}

function releaseQueue() {
    queueHold = false;
    if (!queueStale) return;
    queueStale = false;
    const entry = currentAccount();
    const queue = currentQueue();
    if (entry && queue && state.page === "cola") renderQueue(entry, queue);
}

/** Waiting, and free to change turn: «Ahora» and retries keep the front. */
const isMovable = (v) => isWaiting(v) && !v.on_its_way;

/** POST the move; ``where`` is {position}, {before} or {after}. */
async function moveInQueue(ids, where, {follow = true} = {}) {
    const queue = currentQueue();
    if (!queue || !ids.length) return;
    try {
        const res = await api(`/api/schedules/${queue.schedule.id}/reorder`, {
            method: "POST", body: JSON.stringify({ids, ...where}),
        });
        toast(res.message);
        // A video sent to turn 140 is out of sight: the list follows it there.
        if (follow) state.queueFocus = {id: ids[0], until: Date.now() + 4000};
    } catch (err) {
        toast(err.message, true);
    } finally {
        queueHold = false;
        queueStale = false;
        await refresh();
    }
}

/** The turn number becomes a box: type another one, Enter (or click away). */
function editTurn(turn, video, current, total) {
    holdQueue();
    const input = el("input", "item-turn-input");
    input.type = "number";
    input.min = "1";
    input.max = String(total);
    input.value = String(current);
    input.title = `Turno al que pasa (1–${total}) · Enter para moverlo, Esc para dejarlo`;
    let done = false;
    const finish = (save) => {
        if (done) return;
        done = true;
        const wanted = Math.round(Number(input.value));
        if (save && Number.isFinite(wanted) && wanted >= 1 && wanted !== current) {
            moveInQueue([video.id], {position: Math.min(wanted, total)});
        } else {
            input.replaceWith(turn);
            releaseQueue();
        }
    };
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    turn.replaceWith(input);
    input.focus();
    input.select();
}

/* Drag and drop by the ⠿ handle. Dragging a ticked row takes every ticked
   one along, as a block, in the order they had. */
let queueDrag = null;

function clearDropMarks(list) {
    list.querySelectorAll(".drop-before, .drop-after, .is-dragging")
        .forEach((n) => n.classList.remove("drop-before", "drop-after", "is-dragging"));
}

function dropSpot(list, e) {
    const row = e.target.closest?.(".item[data-movable]");
    if (!row || !list.contains(row) || queueDrag.ids.includes(Number(row.dataset.videoId))) return null;
    const box = row.getBoundingClientRect();
    return {row, after: e.clientY > box.top + box.height / 2};
}

function wireQueueDrag() {
    const list = $("#queue-list");
    list.addEventListener("mousedown", (e) => {
        const handle = e.target.closest(".item-drag[data-video-id]");
        if (handle) handle.closest(".item").draggable = true;
    });
    list.addEventListener("mouseup", () => {
        list.querySelectorAll(".item[draggable=true]").forEach((n) => (n.draggable = false));
    });
    list.addEventListener("dragstart", (e) => {
        const row = e.target.closest?.(".item[data-movable]");
        if (!row || !row.draggable) return;
        const id = Number(row.dataset.videoId);
        const rows = [...list.querySelectorAll(".item[data-movable]")];
        const ticked = rows.filter((n) => state.queueSelected.has(Number(n.dataset.videoId)));
        const moving = state.queueSelected.has(id) && ticked.length > 1 ? ticked : [row];
        queueDrag = {ids: moving.map((n) => Number(n.dataset.videoId)), rows: moving, dropped: false};
        holdQueue();
        moving.forEach((n) => n.classList.add("is-dragging"));
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", queueDrag.ids.join(","));
        if (moving.length > 1) {
            const ghost = el("div", "drag-ghost", `${moving.length} vídeos`);
            document.body.append(ghost);
            e.dataTransfer.setDragImage(ghost, 10, 10);
            setTimeout(() => ghost.remove(), 0);
        }
    });
    list.addEventListener("dragover", (e) => {
        if (!queueDrag) return;
        // Near the top or bottom edge the page scrolls, so a long queue is reachable.
        const edge = 90;
        if (e.clientY < edge) window.scrollBy(0, -Math.ceil((edge - e.clientY) / 4));
        else if (e.clientY > innerHeight - edge) window.scrollBy(0, Math.ceil((e.clientY - innerHeight + edge) / 4));
        const spot = dropSpot(list, e);
        list.querySelectorAll(".drop-before, .drop-after")
            .forEach((n) => n.classList.remove("drop-before", "drop-after"));
        if (!spot) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        spot.row.classList.add(spot.after ? "drop-after" : "drop-before");
    });
    list.addEventListener("drop", (e) => {
        if (!queueDrag) return;
        const spot = dropSpot(list, e);
        if (!spot) return;
        e.preventDefault();
        queueDrag.dropped = true;
        const {ids, rows} = queueDrag;
        clearDropMarks(list);
        // Where it was let go, at once; the server's order arrives right after.
        if (spot.after) spot.row.after(...rows);
        else spot.row.before(...rows);
        const target = Number(spot.row.dataset.videoId);
        moveInQueue(ids, spot.after ? {after: target} : {before: target}, {follow: false});
    });
    list.addEventListener("dragend", (e) => {
        if (e.target.closest) {
            const row = e.target.closest(".item");
            if (row) row.draggable = false;
        }
        clearDropMarks(list);
        const dropped = queueDrag?.dropped;
        queueDrag = null;
        if (!dropped) releaseQueue();
    });
}

function renderQueue(entry, queue) {
    if (queueHold) {
        queueStale = true;
        return;
    }
    const list = $("#queue-list");
    const queueVideos = (entry.videos || []).filter((v) => v.schedule_id === queue.schedule.id);
    renderQueueFolders(queueVideos);
    const videos = inQueueFolder(queueVideos);
    const platform = PLATFORM_LABEL[entry.account.platform] || "TikTok";
    $("#queue-target-note").textContent =
        `Elige vídeos de la biblioteca: entran en «${queue.schedule.name}» con sus textos de `
        + `${platform} ya escritos · ${windowsSummary(queue.schedule)}.`;
    renderHandover(entry, queue);

    // The history (published, failed) is cut at the latest ones in /api/state;
    // the queue's own counters are the database's.
    const cut = (entry.history && entry.history.total > entry.history.shown);
    const exact = {published: queue.stats.published, failed: queue.stats.failed};
    $("#queue-filters").replaceChildren(
        ...QUEUE_FILTERS.map(([id, label, fn]) => {
            const count = cut && isEmptyScope(state.queueScope) && id in exact ? exact[id] : videos.filter(fn).length;
            const chip = el("button", `chip${state.queueFilter === id ? " is-active" : ""}`, `${label} · ${count}`);
            chip.type = "button";
            chip.addEventListener("click", () => {
                state.queueFilter = id;
                renderQueue(entry, queue);
            });
            return chip;
        })
    );
    const filter = QUEUE_FILTERS.find(([id]) => id === state.queueFilter)?.[2] || (() => true);
    const shown = videos.filter(filter);
    const sort = QUEUE_SORTS[state.queueFilter];
    if (sort) shown.sort(sort);
    const writing = videos.filter((v) => ["pending", "generating"].includes(v.caption_status)
        && ["QUEUED", "SCHEDULED"].includes(v.status)).length;
    const eta = state.data.captions?.eta_seconds;
    $("#queue-summary").textContent = `${shown.length} de ${videos.length}`
        + (writing ? ` · ✍️ ${writing} texto${writing === 1 ? "" : "s"} en camino` : "")
        + (writing && eta ? ` (la cola de textos acaba en ~${fmtDuration(eta)})` : "");

    // Turns are the whole queue's, whatever folder is shown.
    const waiting = waitingVideos(queueVideos, queue.schedule.id);
    const turns = new Map(waiting.map((v, i) => [v.id, i + 1]));
    const shuffleBtn = $("#queue-shuffle");
    shuffleBtn.disabled = waiting.length < 2;
    shuffleBtn.title = waiting.length < 2
        ? "Hacen falta al menos dos vídeos esperando para mezclarlos"
        : `Ordenar al azar los ${waiting.length} vídeos que esperan. Si luego mandas la cola `
        + "a Zernio, van en este orden.";
    $("#queue-unshuffle").hidden = !isShuffled(waiting);

    // What can be ticked: the waiting ones, to take them out of the queue,
    // and the ones in Zernio, to take them back, in one go.
    const tickable = shown.filter(isTickable);
    const tickableIds = new Set(tickable.map((v) => v.id));
    [...state.queueSelected].forEach((id) => {
        if (!tickableIds.has(id)) state.queueSelected.delete(id);
    });
    const picked = tickable.filter((v) => state.queueSelected.has(v.id));
    $("#queue-select-wrap").hidden = tickable.length === 0;
    const all = $("#queue-select-all");
    all.checked = tickable.length > 0 && picked.length === tickable.length;
    all.indeterminate = picked.length > 0 && picked.length < tickable.length;
    const ordering = state.queueFilter === "pending";
    const toMove = ordering ? picked.filter(isMovable).length : 0;
    $("#queue-move-group").hidden = toMove === 0;
    $("#queue-move-label").textContent = `Mover ${toMove}:`;
    const toRemove = picked.filter(isWaiting).length;
    const toRecover = picked.length - toRemove;
    const remove = $("#queue-remove-selected");
    remove.hidden = toRemove === 0;
    remove.textContent = `Quitar ${toRemove} de la cola`;
    const recover = $("#queue-recover-selected");
    recover.hidden = toRecover === 0;
    recover.textContent = `Recuperar ${toRecover} de Zernio`;

    if (!shown.length) {
        list.replaceChildren(el("div", "empty", videos.length
            ? "Nada que mostrar con este filtro."
            : `«${queue.schedule.name}» está vacía. Añade vídeos desde la biblioteca.`));
        return;
    }
    const historyNote = cut && ["published", "failed", "all"].includes(state.queueFilter)
        ? el("div", "note note-info",
            `Se listan los ${entry.history.shown} publicados o fallidos más recientes de la cuenta `
            + `(hay ${entry.history.total}). Todo lo que está por salir se lista siempre entero.`)
        : null;

    const others = everyQueue().filter((q) => q.schedule.id !== queue.schedule.id && !q.schedule.hidden);
    // A column for the turn as soon as one row has it, so the rows stay aligned.
    const numbered = shown.some((v) => turns.has(v.id));
    list.replaceChildren(
        ...shown.map((v) => {
            const item = el("div", `item${state.queueSelected.has(v.id) ? " is-selected" : ""}`);
            item.dataset.videoId = v.id;
            if (tickable.length) {
                if (isTickable(v)) {
                    const box = el("input", "item-check");
                    box.type = "checkbox";
                    box.checked = state.queueSelected.has(v.id);
                    box.title = isWaiting(v)
                        ? "Seleccionar para quitarlo de la cola"
                        : "Seleccionar para recuperarlo de Zernio";
                    box.addEventListener("change", () => {
                        if (box.checked) state.queueSelected.add(v.id);
                        else state.queueSelected.delete(v.id);
                        renderQueue(entry, queue);
                    });
                    item.append(box);
                } else {
                    item.append(el("span", "item-check"));
                }
            }
            if (numbered) {
                const n = turns.get(v.id);
                if (ordering && n && isMovable(v)) {
                    item.dataset.movable = "1";
                    const handle = el("span", "item-drag", "⠿");
                    handle.dataset.videoId = v.id;
                    handle.title = state.queueSelected.has(v.id) && toMove > 1
                        ? `Arrastra para mover los ${toMove} seleccionados juntos`
                        : "Arrastra para cambiarle el turno";
                    const turn = el("button", "item-turn is-editable", n);
                    turn.type = "button";
                    turn.title = `Turno ${n} de ${waiting.length} · pulsa para escribir otro`;
                    turn.addEventListener("click", () => editTurn(turn, v, n, waiting.length));
                    item.append(handle, turn);
                } else {
                    if (ordering) item.append(el("span", "item-drag"));
                    const turn = el("span", "item-turn", n ?? "");
                    if (n) turn.title = v.on_its_way && isWaiting(v)
                        ? `Turno ${n} de ${waiting.length} · ya está saliendo («Ahora» o un reintento): va delante y no se mueve`
                        : `Turno ${n} de ${waiting.length}`;
                    item.append(turn);
                }
            }
            item.append(el("span", `badge ${v.status}`, statusLabel(v.status)));

            const main = el("div", "item-main");
            main.append(el("div", "item-title", v.original_name));
            const bits = [];
            if (v.created_local && v.status !== "PUBLISHED") bits.push(`añadido ${v.created_local}`);
            if (v.scheduled_local && ["QUEUED", "SCHEDULED"].includes(v.status)) bits.push(`⏱ ${v.scheduled_local}`);
            if (v.status === "DELEGATED") {
                bits.push(v.handed_over
                    ? `🅩 Zernio publica el ${v.scheduled_local}`
                    : "🅩 subiendo a Zernio…");
            }
            if (aigcOn(v, queue) && v.status !== "PUBLISHED") bits.push("🤖 IA");
            if (v.published_local && v.status === "PUBLISHED") bits.push(`✓ ${v.published_local}`);
            if (v.extra) bits.push("⚡ fuera de turno");
            if (v.duration_sec) bits.push(`${v.duration_sec}s`);
            if (v.resolution) bits.push(v.resolution);
            if (v.size_mb) bits.push(`${v.size_mb} MB`);
            if (v.has_audio === false) bits.push("sin audio");
            if (v.attempts > 1) bits.push(`${v.attempts} intentos`);
            const meta = el("div", "item-meta", bits.join(" · "));
            if (v.library_folder) meta.append(folderMark(v.library_folder, !bits.length));
            main.append(meta);
            const pending = ["QUEUED", "SCHEDULED"].includes(v.status);
            // Its text can still change what goes out: waiting here, or
            // delegated but not yet uploaded to Zernio.
            const writable = pending || (v.status === "DELEGATED" && !v.handed_over);
            if (v.caption && writable) {
                const cap = el("div", "item-caption", v.caption);
                cap.title = v.caption;
                main.append(cap);
            }
            if (writable && v.auto_hashtags && v.auto_hashtags.length)
                main.append(el("div", "item-tags", v.auto_hashtags.join(" ")));
            if (writable && v.caption_status && v.caption_status !== "none")
                main.append(captionStatusLine(v));
            if (v.last_error) main.append(el("div", "item-error", v.last_error));
            if (v.metrics) main.append(el("div", "item-metrics", metricsLine(v)));
            if (v.post_url) main.append(link("Ver publicación", v.post_url, "small"));
            item.append(main);

            const actions = el("div", "item-actions");
            if (v.status !== "PUBLISHED") actions.append(link("Ver", `/api/videos/${v.id}/file`, "btn btn-ghost btn-sm"));
            if (["QUEUED", "SCHEDULED"].includes(v.status)) {
                const text = button("Texto", "btn btn-ghost btn-sm", () => openCaptionEditor(v, entry));
                text.title = "Título, descripción, hashtags y comentario fijado, con revisión y vista previa";
                actions.append(
                    text,
                    button("Ahora", "btn btn-ghost btn-sm", async () => {
                        const res = await api(`/api/videos/${v.id}/publish-now`, {method: "POST"});
                        toast(res.message);
                        state.refresh();
                    }),
                    aigcToggle(v, queue),
                );
                const regen = regenerateButton(v);
                if (regen) actions.append(regen);
                if (others.length) {
                    actions.append(button("Mover a…", "btn btn-ghost btn-sm", () => openMoveDialog(v, others)));
                }
            }
            if (v.status === "DELEGATED") {
                actions.append(button("Recuperar", "btn btn-ghost btn-sm", async () => {
                    if (!confirm(`¿Cancelar «${v.original_name}» en Zernio y devolverlo a la cola?`)) return;
                    const res = await api(`/api/videos/${v.id}/undelegate`, {method: "POST"});
                    toast(res.status === "PUBLISHED"
                        ? "Zernio ya lo había publicado: queda como publicado"
                        : "De vuelta en la cola");
                    state.refresh();
                }));
                // Not uploaded yet: its text can still be rewritten.
                const regen = !v.handed_over && regenerateButton(v);
                if (regen) actions.append(regen);
            }
            if (v.status === "FAILED") {
                actions.append(button("Reintentar", "btn btn-ghost btn-sm", async () => {
                    await api(`/api/videos/${v.id}/retry`, {method: "POST"});
                    toast("Vuelve a estar en la cola");
                    state.refresh();
                }));
            }
            // Also while uploading or processing: one that never finishes
            // blocks every other upload of the account. The server refuses an
            // upload that may really be running.
            actions.append(button("Quitar", "btn btn-ghost btn-sm", async () => {
                const question = {
                    DELEGATED: `«${v.original_name}» está programado en Zernio. ¿Cancelarlo allí y quitarlo de la lista?`,
                    PROCESSING: `«${v.original_name}» sigue «procesando» en Zernio y, mientras, esta cuenta no sube nada más. ¿Cancelarlo en Zernio (si aún lo tiene) y quitarlo de la cola?`,
                    UPLOADING: `«${v.original_name}» figura como «subiendo». ¿Quitarlo de la cola? Si la subida sigue en marcha, no te dejará.`,
                }[v.status] || `¿Quitar «${v.original_name}» de la cola?`;
                if (!confirm(question)) return;
                const res = await api(`/api/videos/${v.id}`, {method: "DELETE"});
                if (res && res.published) toast("Zernio ya lo había publicado: queda como publicado");
                state.refresh();
            }));
            item.append(actions);
            return item;
        }),
        ...(historyNote ? [historyNote] : []),
    );
    focusQueueRow(list);
}

/** Coming from the library's «En …» chip: bring that video into view and
 *  keep it marked for a few seconds, through the redraws of the polling. */
function focusQueueRow(list) {
    const focus = state.queueFocus;
    if (!focus) return;
    if (Date.now() > focus.until) {
        state.queueFocus = null;
        return;
    }
    const row = list.querySelector(`[data-video-id="${focus.id}"]`);
    if (!row) return;
    row.classList.add("is-focus");
    if (focus.scrolled) return;
    focus.scrolled = true;
    // After the route's own scroll to the top.
    setTimeout(() => row.scrollIntoView({block: "center", behavior: "smooth"}), 50);
}

/** Per-video switch for TikTok's AI-generated content label.
 *  The queue decides what a video is born with; this takes it back. */
function aigcToggle(video, queue) {
    const on = aigcOn(video, queue);
    const btn = button(on ? "🤖 IA" : "🤖 IA no", `btn btn-ghost btn-sm${on ? " is-on" : ""}`,
        async () => {
            await api(`/api/videos/${video.id}`, {
                method: "PATCH", body: JSON.stringify({is_aigc: !on}),
            });
            toast(on ? "Ya no se declara como generado por IA" : "Se declarará como generado por IA");
            state.refresh();
        });
    btn.title = on
        ? "Este vídeo se publicará declarado como contenido generado por IA. Haz clic para quitarle la etiqueta."
        : "Este vídeo se publicará sin la etiqueta de IA. Haz clic para ponérsela.";
    return btn;
}

/* ------------------------------------------------------------------ */
/* Enviar la cola a Zernio                                             */

/* ------------------------------------------------------------------ */

function renderHandover(entry, queue) {
    const videos = (entry.videos || []).filter((v) => v.schedule_id === queue.schedule.id);
    const delegated = videos.filter((v) => v.status === "DELEGATED");
    const waiting = delegated.filter((v) => !v.handed_over);
    const sent = delegated.length - waiting.length;
    // The buttons act on the folder (or label) picked above the list, if any.
    const scoped = inQueueFolder(videos);
    const pending = scoped.filter((v) => ["QUEUED", "SCHEDULED"].includes(v.status));
    const recoverable = scoped.filter((v) => v.status === "DELEGATED");
    const of = scopeSuffix(state.queueScope);

    $("#handover-title").textContent = isEmptyScope(state.queueScope)
        ? "Mandar la cola entera a Zernio"
        : `Mandar las carpetas${of} a Zernio`;
    const btn = $("#handover-btn");
    btn.disabled = pending.length === 0;
    btn.textContent = pending.length
        ? `Enviar ${pending.length} vídeo(s)${of} a Zernio…`
        : `No hay vídeos pendientes${of} que enviar`;

    const cancel = $("#handover-cancel");
    cancel.hidden = recoverable.length === 0;
    cancel.textContent = `Recuperar ${recoverable.length}${of} de Zernio`;

    const progress = $("#handover-progress");
    progress.hidden = waiting.length === 0;
    if (waiting.length)
        $("#handover-bar").style.width = `${Math.round((sent / delegated.length) * 100)}%`;

    const bits = [];
    if (waiting.length) bits.push(`Subiendo a Zernio: ${sent} de ${delegated.length}`);
    else if (delegated.length)
        bits.push(`${delegated.length} vídeo(s) esperando su hora en Zernio · ya puedes cerrar la aplicación`);
    if (waiting.length && state.data.health.paused)
        bits.push("la programación está pausada, así que las subidas que faltan esperan");
    $("#handover-status").textContent = bits.join(" · ");
}

async function openHandoverDialog(queue) {
    const scope = queueScope();
    const query = new URLSearchParams();
    Object.entries(scope).forEach(([key, value]) =>
        (Array.isArray(value) ? value : [value]).forEach((v) => query.append(key, v)));
    const plan = await api(`/api/schedules/${queue.schedule.id}/delegation`
        + (query.size ? `?${query}` : ""));
    const body = el("div", "handover-plan");
    const of = scopeSuffix(state.queueScope);
    const title = of
        ? `Enviar los vídeos${of} de «${queue.schedule.name}» a Zernio`
        : `Enviar «${queue.schedule.name}» a Zernio`;

    if (!plan.count) {
        body.append(el("div", "note note-warn",
            plan.pending
                ? "Las reglas de esta cola no dan ningún hueco válido: revisa el máximo diario, las franjas y los días de la semana."
                : `Esta cola no tiene vídeos pendientes${of}.`));
        openModal(title, body);
        return;
    }

    const summary = el("div", "plan-summary");
    summary.append(
        el("div", "plan-headline", `${plan.count} vídeo(s) · ${plan.days} día(s)`),
        el("div", "muted small", `Del ${plan.first_local} al ${plan.last_local}`),
        el("div", "muted small", plan.gap_avg
            ? `Separación entre vídeos: ${plan.gap_min}–${plan.gap_max} min (media ${plan.gap_avg} min)`
            : "Un único vídeo, sin separación que calcular"),
    );
    body.append(summary);
    if (plan.already_delegated) {
        body.append(el("div", "note note-info",
            `Esta cola ya tiene ${plan.already_delegated} vídeo(s) en Zernio; estos van detrás, `
            + "con la misma separación."));
    }
    if (isShuffled(waitingVideos(currentAccount()?.videos || [], queue.schedule.id))) {
        body.append(el("div", "note note-info",
            "🔀 Van en el orden que dejó «Mezclar», el mismo de la lista de la cola."));
    }
    (plan.warnings || []).forEach((w) => body.append(el("div", "note note-warn", w)));

    const list = el("div", "plan-list");
    plan.posts.forEach((post, i) => {
        const row = el("div", "plan-row");
        row.append(el("span", "plan-n", `${i + 1}`));
        const main = el("div", "plan-main");
        main.append(el("div", "plan-name", post.name));
        const marks = [];
        if (post.gap_minutes) marks.push(`+${post.gap_minutes} min`);
        if (post.is_aigc) marks.push("🤖 IA");
        if (post.caption_pending) marks.push("✍️ texto en camino");
        main.append(el("div", "muted small", marks.join(" · ")));
        row.append(main);
        row.append(el("span", "plan-when", post.local));
        list.append(row);
    });
    body.append(list);

    body.append(el("div", "note note-info",
        "Al enviarlos, esta aplicación deja de publicarlos: lo hace Zernio a la hora indicada, "
        + "aunque cierres el programa o apagues el ordenador. Los vídeos se suben de uno en uno, "
        + "así que deja la aplicación abierta hasta que la barra llegue al final. Mientras no se "
        + "publiquen puedes recuperarlos con «Recuperar de Zernio»."));

    const actions = el("div", "actions-bar");
    actions.append(
        button(`Enviar ${plan.count} vídeo(s) a Zernio`, "btn btn-primary", async () => {
            const res = await api(`/api/schedules/${queue.schedule.id}/delegate`, {
                method: "POST", body: JSON.stringify(scope),
            });
            closeModal();
            toast(res.message);
            await state.refresh();
        }),
        button("Cancelar", "btn btn-ghost", closeModal),
    );
    body.append(actions);
    openModal(title, body);
}

/** «Escribir solo» / «Regenerar»: the automatic text, (again), for one video. */
function regenerateButton(v) {
    const ai = state.data.health && state.data.health.ai;
    if (!ai || !ai.enabled || ["pending", "generating"].includes(v.caption_status)) return null;
    return button(v.caption_status === "none" ? "Escribir solo" : "Regenerar",
        "btn btn-ghost btn-sm", async () => {
            try {
                await api(`/api/videos/${v.id}/caption/regenerate`, {method: "POST"});
                toast("Escribiendo el texto… aparecerá en la cola");
                state.refresh();
            } catch (err) {
                toast(err.message, true);
            }
        });
}

/** The texts of one queued video, in the composer (composer.js). */
function openCaptionEditor(v, entry, focus = null) {
    const platform = entry?.account?.platform || "tiktok";
    const notes = [];
    if (v.caption_source === "biblioteca") {
        notes.push("Estos textos son los de la biblioteca. Si los cambias aquí, este vídeo se queda con "
            + "los tuyos y deja de recibir los cambios que hagas allí.");
    }
    openComposer({
        title: `Texto de «${v.original_name}»`,
        platforms: [platform],
        texts: {
            [platform]: {
                title: v.title || "", description: v.caption || "",
                tags: v.auto_hashtags || [], first_comment: v.first_comment || "",
            },
        },
        context: {video_id: v.id},
        handle: entry ? accountLabel(entry.account) : "",
        notes,
        focus,
        templates: true,
        ai: state.data.health && state.data.health.ai,
        fixed: fixedHashtags(state.data.settings),
        onSave: async (texts) => {
            const t = texts[platform];
            await api(`/api/videos/${v.id}`, {
                method: "PATCH", body: JSON.stringify({
                    title: t.title, caption: t.description, hashtags: t.tags,
                    first_comment: t.first_comment,
                }),
            });
            toast("Texto guardado");
            state.refresh();
        },
    });
}

/** «Mover a…»: every other queue, of this account and of the others. */
function openMoveDialog(v, others) {
    const name = (o) => (accounts().length > 1 ? `${accountLabel(o.account)} · ${o.schedule.name}` : o.schedule.name);
    const body = el("div");
    body.append(el("p", "muted small",
        "Se va con sus textos. En otra cuenta de la misma plataforma o de otra, toma los de la "
        + "biblioteca para esa plataforma si los seguía."));
    const list = el("div", "move-list");
    others.forEach((o) => {
        const row = button(name(o), "btn btn-ghost move-choice", async () => {
            await api(`/api/videos/${v.id}`, {
                method: "PATCH", body: JSON.stringify({schedule_id: o.schedule.id}),
            });
            closeModal();
            toast(`Movido a ${name(o)}`);
            state.refresh();
        });
        row.append(el("span", "muted small", ` · ${PLATFORM_LABEL[o.account.platform] || "TikTok"}`
            + ` · ${o.stats?.queued ?? 0} en cola`));
        list.append(row);
    });
    body.append(list);
    const bar = el("div", "actions-bar");
    bar.append(button("Cancelar", "btn btn-ghost", closeModal));
    body.append(bar);
    openModal(`Mover «${v.original_name}» a…`, body);
}

/** The line under a queued video that says where its automatic text stands. */
function captionStatusLine(v) {
    const source = {
        "whisper": "Whisper", "whisper+ollama": "Whisper + Ollama",
        "ficha": "ficha .txt", "ficha+ollama": "ficha .txt + Ollama",
        "fallback": "sin voz: sólo hashtags", "manual": "editado a mano",
        "biblioteca": "de la biblioteca",
    }[v.caption_source] || v.caption_source || "";
    const fromLibrary = v.caption_source === "biblioteca";
    switch (v.caption_status) {
        case "pending":
            return el("div", "item-ai pending", fromLibrary
                ? "📚 La biblioteca está escribiendo sus textos; llegarán solos…"
                : "✍️ Texto automático en cola…");
        case "generating": {
            const line = el("div", "item-ai generating");
            line.append(el("span", "pulse"), document.createTextNode(" Escuchando el vídeo y escribiendo el texto…"));
            return line;
        }
        case "done": {
            const note = v.caption_error ? ` · ${v.caption_error}` : "";
            const line = el("div", "item-ai done", fromLibrary
                ? `📚 Textos de la biblioteca${note}`
                : `✍️ Texto generado · ${source}${note}`);
            const fromSheet = (v.caption_source || "").startsWith("ficha");
            const heard = v.transcript_excerpt || v.transcript;
            if (heard) line.title = `${fromSheet ? "Guion (de la ficha .txt)" : "Transcripción"}: ${heard}`
                + (v.transcript_excerpt && v.has_transcript ? "…" : "");
            return line;
        }
        case "failed":
            return el("div", "item-ai failed", `⚠️ Texto automático: ${v.caption_error || "falló"}. Se publica con el texto actual.`);
        default:
            return el("div", "item-ai", "");
    }
}

/* ------------------------------------------------------------------ */
/* Analíticas · Comentarios · Actividad                                */

/* ------------------------------------------------------------------ */

const nf = new Intl.NumberFormat("es-ES");

/** One-line summary of a published video's numbers, for the queue list. */
function metricsLine(v) {
    const m = v.metrics;
    const bits = [`${nf.format(m.views)} reproducciones`, `${nf.format(m.likes)} me gusta`,
        `${nf.format(m.comments)} comentarios`, `${nf.format(m.shares)} compartidos`];
    if (m.engagement_rate) bits.push(`${m.engagement_rate}% interacción`);
    if (m.completion_rate) bits.push(`${Math.round(m.completion_rate * 100)}% visto entero`);
    return `📈 ${bits.join(" · ")}`;
}

let analyticsCache = null;

async function loadAnalytics() {
    analyticsCache = await api("/api/analytics");
    renderAnalytics();
}

/** Horizontal bars, scaled to the best bucket, so the shape is readable. */
function renderBars(node, buckets, labelOf) {
    if (!buckets.length) {
        node.replaceChildren(el("div", "empty", "Todavía no hay vídeos con datos."));
        return;
    }
    const top = Math.max(...buckets.map((b) => b.views_avg), 1);
    node.replaceChildren(
        ...buckets.map((b) => {
            const row = el("div", "bar-row");
            row.append(el("span", "bar-label", labelOf(b.bucket)));
            const track = el("div", "bar-track");
            const fill = el("div", "bar-fill");
            fill.style.width = `${Math.max((b.views_avg / top) * 100, 2)}%`;
            track.append(fill);
            row.append(track);
            row.append(el("span", "bar-value",
                `${nf.format(b.views_avg)} · ${b.posts} vídeo${b.posts === 1 ? "" : "s"}`));
            return row;
        })
    );
}

function renderAnalytics() {
    const data = analyticsCache;
    if (!data) {
        $("#an-totals").replaceChildren(el("div", "empty", "Cargando…"));
        loadAnalytics().catch((err) => {
            $("#an-totals").replaceChildren(el("div", "empty", err.message));
        });
        return;
    }

    const t = data.totals;
    $("#an-totals").replaceChildren(
        ...[["Vídeos con datos", nf.format(data.posts)],
            ["Reproducciones", nf.format(t.views)],
            ["Media por vídeo", nf.format(t.views_avg)],
            ["Me gusta", nf.format(t.likes)],
            ["Comentarios", nf.format(t.comments)],
            ["Compartidos", nf.format(t.shares)],
            ["Interacción", `${t.engagement_rate}%`],
            ["Vistas de perfil", nf.format(t.profile_views)]].map(([label, value]) => {
            const card = el("div", "stat");
            card.append(el("div", "value", value), el("div", "label", label));
            return card;
        })
    );

    renderBars($("#an-hours"), data.by_hour, (h) => `${String(h).padStart(2, "0")}:00`);
    renderBars($("#an-weekdays"), data.by_weekday, (d) => WEEKDAYS[d] || d);

    const tagList = $("#an-hashtags");
    const tags = data.by_hashtag || [];
    if (!tags.length) {
        tagList.replaceChildren(el("div", "empty", "Aún no hay vídeos publicados con hashtags y datos."));
    } else {
        tagList.replaceChildren(...tags.map((t) => {
            const row = el("div", "bar-row");
            row.append(el("span", "bar-label tag", t.tag));
            const track = el("div", "bar-track");
            const fill = el("div", `bar-fill${t.trusted ? "" : " faint"}`);
            fill.style.width = `${Math.max((t.views_avg / Math.max(tags[0].views_avg, 1)) * 100, 2)}%`;
            track.append(fill);
            row.append(track);
            row.append(el("span", "bar-value",
                `${nf.format(t.views_avg)} · ${t.posts} vídeo${t.posts === 1 ? "" : "s"}${t.trusted ? "" : " · pocos datos"}`));
            return row;
        }));
    }

    const best = $("#an-best");
    if (data.best_hours && data.best_hours.length) {
        best.hidden = false;
        best.textContent = "Tus mejores horas hasta ahora: "
            + data.best_hours.map((h) => `${String(h).padStart(2, "0")}:00`).join(", ")
            + ". Puedes reflejarlo en las franjas horarias de Programación.";
    } else {
        best.hidden = true;
    }

    const list = $("#an-top");
    if (!data.top.length) {
        list.replaceChildren(el("div", "empty",
            "Sin datos todavía. Tarda un rato en contar un vídeo recién publicado."));
    } else {
        list.replaceChildren(
            ...data.top.map((row, i) => {
                const item = el("div", "item");
                item.append(el("span", "badge", `#${i + 1}`));
                const main = el("div", "item-main");
                main.append(el("div", "item-title", row.name));
                main.append(el("div", "item-meta",
                    `${nf.format(row.views)} reproducciones · ${nf.format(row.likes)} me gusta · `
                    + `${nf.format(row.comments)} comentarios · ${row.engagement_rate}% interacción`));
                main.append(el("div", "muted small", row.published_local || ""));
                item.append(main);
                if (row.post_url) item.append(link("Ver", row.post_url, "btn btn-ghost btn-sm"));
                return item;
            })
        );
    }

    $("#an-status").textContent = data.updated_at
        ? `Últimos datos: ${new Date(data.updated_at).toLocaleString("es-ES")}`
        : "Sin datos todavía";
}

function wireAnalytics() {
    $("#an-refresh").addEventListener("click", async () => {
        $("#an-status").textContent = "Consultando a Zernio…";
        try {
            const res = await api("/api/analytics/refresh", {method: "POST"});
            if (res.problems && res.problems.length) toast(res.problems.join(" · "), true);
            analyticsCache = null;
            await loadAnalytics();
            await refresh();
        } catch (err) {
            $("#an-status").textContent = "";
            toast(err.message, true);
        }
    });
}

let commentsCache = null;

async function loadComments() {
    const params = new URLSearchParams();
    if ($("#cm-pending").checked) params.set("pending", "true");
    if ($("#cm-account").value) params.set("account_id", $("#cm-account").value);
    // Yours («Tú») never need an answer: out of the way unless asked for.
    if (!$("#cm-mine").checked) params.set("mine", "false");
    commentsCache = await api(`/api/comments?${params}`);
    renderComments();
}

function renderComments() {
    const data = commentsCache;
    const list = $("#cm-list");
    // value "" for «todas», as in Actividad.
    const select = $("#cm-account");
    fillSelect(select, [["", "Todas las cuentas"],
        ...accounts().map((a) => [a.account.id, accountLabel(a.account)])], select.value);
    if (!data) {
        list.replaceChildren(el("div", "empty", "Cargando…"));
        loadComments().catch((err) => list.replaceChildren(el("div", "empty", err.message)));
        return;
    }

    $("#cm-status").textContent = data.pending
        ? `${data.pending} sin responder`
        : "Todo respondido";
    $("#cm-own").textContent = data.own ? `(${data.own})` : "";

    if (!data.comments.length) {
        list.replaceChildren(el("div", "empty", data.own && !$("#cm-mine").checked && !$("#cm-pending").checked
            ? "Por ahora sólo hay comentarios tuyos: marca «Mostrar los míos» para verlos."
            : "No hay comentarios. Pulsa «Buscar nuevos» o espera a que se lean solos."));
        return;
    }

    list.replaceChildren(
        ...data.comments.map((c) => {
            const item = el("div", `item comment${c.is_owner ? " mine" : ""}`
                + (c.hidden ? " hidden-comment" : ""));
            const main = el("div", "item-main");
            const head = el("div", "item-title");
            head.append(commentAccount(c));
            head.append(el("strong", null, c.is_owner ? "Tú" : `@${c.author}`));
            if (c.parent_external_id) head.append(el("span", "badge", "respuesta"));
            if (c.hidden) head.append(el("span", "badge FAILED", "oculto"));
            if (!c.is_owner && !c.answered && !c.hidden)
                head.append(el("span", "badge SCHEDULED", "sin responder"));
            if (!c.is_owner && c.answered) head.append(el("span", "badge PUBLISHED", "respondido"));
            main.append(head);
            main.append(el("div", "comment-text", c.message));
            const meta = [c.created_local, c.video_name];
            if (c.like_count) meta.push(`${c.like_count} me gusta`);
            main.append(el("div", "item-meta", meta.filter(Boolean).join(" · ")));
            item.append(main);

            const actions = el("div", "item-actions");
            if (!c.is_owner) {
                actions.append(button("Responder", "btn btn-ghost btn-sm", async () => {
                    const message = prompt(`Responder a @${c.author}:`, "");
                    if (message === null || !message.trim()) return;
                    try {
                        await api(`/api/comments/${c.id}/reply`,
                            {method: "POST", body: JSON.stringify({message})});
                        toast("Respuesta enviada");
                        commentsCache = null;
                        await loadComments();
                        await refresh();
                    } catch (err) {
                        toast(err.message, true);
                    }
                }));
                actions.append(button(c.hidden ? "Mostrar" : "Ocultar", "btn btn-ghost btn-sm",
                    async () => {
                        try {
                            await api(`/api/comments/${c.id}/hide`,
                                {method: "POST", body: JSON.stringify({hidden: !c.hidden})});
                            commentsCache = null;
                            await loadComments();
                            await refresh();
                        } catch (err) {
                            toast(err.message, true);
                        }
                    }));
                // Answered in the TikTok app, or nothing to answer: by hand.
                actions.append(button(c.answered ? "Marcar sin responder" : "✓ Respondido",
                    "btn btn-ghost btn-sm", async () => {
                        try {
                            await api(`/api/comments/${c.id}/answered`,
                                {method: "POST", body: JSON.stringify({answered: !c.answered})});
                            commentsCache = null;
                            await loadComments();
                            await refresh();
                        } catch (err) {
                            toast(err.message, true);
                        }
                    }));
            }
            if (c.post_url) actions.append(link("Vídeo", c.post_url, "btn btn-ghost btn-sm"));
            item.append(actions);
            return item;
        })
    );
}

/* Whose inbox a comment is in: the platform, and the account in the colour of
   its card and its calendar lane. */
function commentAccount(c) {
    const known = accounts().find((a) => a.account.id === c.account_id)?.account;
    const platform = platformOf(known || {platform: c.account_platform});
    const chip = el("span", "comment-account");
    chip.style.setProperty("--c", accountColor(c.account_id));
    chip.title = c.account_username ? `@${c.account_username}` : "";
    chip.append(el("span", `lib-plat ${platform}`, PLATFORM_NAMES[platform]),
        el("span", "comment-account-name", known ? accountLabel(known) : c.account_label));
    return chip;
}

function wireComments() {
    ["#cm-pending", "#cm-mine", "#cm-account"].forEach((id) => $(id).addEventListener("change", () => {
        commentsCache = null;
        loadComments().catch((err) => toast(err.message, true));
    }));
    $("#cm-refresh").addEventListener("click", async () => {
        $("#cm-status").textContent = "Buscando…";
        try {
            const res = await api("/api/comments/refresh", {method: "POST"});
            toast(res.new ? `${res.new} comentario(s) nuevo(s)` : "Sin comentarios nuevos");
            if (res.problems && res.problems.length) toast(res.problems.join(" · "), true);
            commentsCache = null;
            await loadComments();
            await refresh();
        } catch (err) {
            $("#cm-status").textContent = "";
            toast(err.message, true);
        }
    });
}

let eventsCache = null;

async function loadEvents() {
    const params = new URLSearchParams();
    if ($("#ev-level").value) params.set("level", $("#ev-level").value);
    if ($("#ev-account").value) params.set("account_id", $("#ev-account").value);
    if ($("#ev-q").value.trim()) params.set("q", $("#ev-q").value.trim());
    params.set("limit", "300");
    try {
        eventsCache = (await api(`/api/events?${params}`)).events;
        renderEvents();
    } catch (err) {
        toast(err.message, true);
    }
}

function renderEvents() {
    // value "" for «todas»: rebuilt without it, choosing it again sent
    // account_id=Todas las cuentas and the filter failed.
    const select = $("#ev-account");
    fillSelect(select, [["", "Todas las cuentas"],
        ...accounts().map((a) => [a.account.id, accountLabel(a.account)])], select.value);

    const events = eventsCache ?? state.data.events ?? [];
    $("#ev-count").textContent = `${events.length} entradas`;
    $("#event-list").replaceChildren(
        ...(events.length ? events.map(eventRow) : [el("div", "empty", "Nada que mostrar.")])
    );
}

/* ------------------------------------------------------------------ */
/* Orchestration                                                       */

/* ------------------------------------------------------------------ */

function renderAll() {
    if (!state.data || !state.presets) return;
    renderShell();
    const all = accounts();
    if (!all.length) {
        renderOnboarding();
        return;
    }
    // The connect form lives in the onboarding page until the first account
    // exists. After that it only shows when «Añadir cuenta» opened it in
    // Cuentas, and a refresh must leave it as the user left it: hiding it here
    // on every poll closed it under their fingers while they pasted the key.
    const form = $("#connect-form");
    if (!$("#accounts-form-slot").contains(form)) form.hidden = true;
    const entry = currentAccount();
    state.accountId = entry.account.id;
    const queue = currentQueue();
    if (queue) state.queueId = queue.schedule.id;

    switch (state.page) {
        case "resumen":
            renderOverview();
            break;
        case "biblioteca":
            renderLibraryPage();
            break;
        case "cola":
            if (queue) renderQueue(entry, queue);
            break;
        case "calendario":
            renderCalendarPage();
            break;
        case "programacion":
            if (!queue) break;
            renderStats(entry, queue);
            renderQueueHeader(entry, queue);
            if (!state.formDirty) renderScheduleForm(queue);
            break;
        case "analiticas":
            renderAnalytics();
            break;
        case "comentarios":
            renderComments();
            break;
        case "actividad":
            renderEvents();
            break;
        case "cuentas":
            renderAccountsPage();
            break;
        case "db":
            renderDbPage();
            break;
        case "ajustes":
            renderSettingsPage();
            break;
        default:
            break;
    }
}

/* The queue remembered from the last visit, until the first data arrives. */
let restoredQueue = false;

async function refresh() {
    try {
        state.data = await api("/api/state");
        // Not reopened if it is hidden now: a hidden queue opens only when
        // chosen (its tab, Resumen, or an address naming it, applied after).
        if (restoredQueue) {
            restoredQueue = false;
            if (currentQueue()?.schedule.hidden) setQueue(null);
        }
        renderAll();
    } catch (err) {
        console.error(err);
    }
}

/* /api/state carries every video of every account (megabytes once Zernio holds
   months of them), so it is not asked for blindly every 5 s: that pace only
   while someone is using the page or a video is on its way to Zernio, every
   30 s otherwise, and never while the tab is hidden (coming back asks at once). */
const POLL_FAST_MS = 5000;
const POLL_SLOW_MS = 30000;
const ACTIVE_MS = 60000;
const MOVING = ["UPLOADING", "PROCESSING"];
const poller = {timer: null, busy: false, lastAt: 0, activeAt: Date.now()};

function pollDelay() {
    const moving = (state.data?.accounts || []).some((a) => a.videos.some((v) => MOVING.includes(v.status)));
    return moving || Date.now() - poller.activeAt < ACTIVE_MS ? POLL_FAST_MS : POLL_SLOW_MS;
}

async function poll() {
    if (poller.busy) return;  // the one in flight schedules the next
    clearTimeout(poller.timer);
    poller.timer = null;
    if (document.hidden) return;
    poller.busy = true;
    poller.lastAt = Date.now();
    try {
        await refresh();
    } finally {
        poller.busy = false;
    }
    if (!document.hidden) poller.timer = setTimeout(poll, pollDelay());
}

function wirePolling() {
    const active = () => {
        const idle = Date.now() - poller.activeAt >= ACTIVE_MS;
        poller.activeAt = Date.now();
        // Back after a while: what changed meanwhile now, not at the next slow tick.
        if (idle && Date.now() - poller.lastAt >= POLL_FAST_MS) poll();
    };
    ["pointerdown", "pointermove", "keydown", "wheel"].forEach((type) =>
        document.addEventListener(type, active, {capture: true, passive: true}));
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            clearTimeout(poller.timer);
            poller.timer = null;
        } else {
            poller.activeAt = Date.now();
            poll();
        }
    });
    poller.timer = setTimeout(poll, pollDelay());
}

function markDirty() {
    state.formDirty = true;
    $("#sch-status").textContent = "Cambios sin guardar";
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */

/* ------------------------------------------------------------------ */

/* The UI width (1×, 1,5× or 2×) lives in html[data-width]; the inline script in
   the page head has already put the saved one on before the first paint. */
function wireWidthSwitch() {
    const buttons = $$("#width-switch button");
    const show = (value) => {
        if (value === "1") delete document.documentElement.dataset.width;
        else document.documentElement.dataset.width = value;
        buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.width === value)));
    };
    show(document.documentElement.dataset.width || "1");
    buttons.forEach((b) => b.addEventListener("click", () => {
        show(b.dataset.width);
        try {
            if (b.dataset.width === "1") localStorage.removeItem("tts.uiWidth");
            else localStorage.setItem("tts.uiWidth", b.dataset.width);
        } catch (e) { /* storage blocked: it applies until the reload */
        }
    }));
}

function wireShell() {
    window.addEventListener("hashchange", route);
    wireWidthSwitch();
    $("#account-switch").addEventListener("change", (e) => {
        setAccount(e.target.value);
        renderAll();
    });
    $("#pause-btn").addEventListener("click", async () => {
        try {
            await api("/api/system/pause", {
                method: "POST", body: JSON.stringify({paused: !state.data.health.paused}),
            });
            await refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
    wireNavMenus();
    $("#modal-close").addEventListener("click", requestCloseModal);
    $("#modal").addEventListener("click", (e) => {
        if (e.target.id === "modal") requestCloseModal();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !$("#modal").hidden) requestCloseModal();
    });
}

/** «Seguimiento ▾»: the header's submenus. The panel is placed with fixed
 *  coordinates because the nav scrolls sideways (overflow-x), which would
 *  clip anything hanging out of it. Click, Esc or leaving closes it. */
function wireNavMenus() {
    $$(".navmenu").forEach((menu) => {
        const toggle = menu.querySelector(".navmenu-toggle");
        const panel = menu.querySelector(".navmenu-panel");
        const close = () => {
            if (panel.hidden) return;
            panel.hidden = true;
            toggle.setAttribute("aria-expanded", "false");
        };
        const open = (byKeyboard) => {
            // The top bar's backdrop-filter makes it the frame of a fixed
            // element: coordinates are measured from it, not from the window.
            const box = toggle.getBoundingClientRect();
            const frame = $(".topbar").getBoundingClientRect();
            const left = Math.max(8, Math.min(box.left, window.innerWidth - 230));
            panel.style.top = `${Math.round(box.bottom - frame.top + 6)}px`;
            panel.style.left = `${Math.round(left - frame.left)}px`;
            panel.hidden = false;
            toggle.setAttribute("aria-expanded", "true");
            if (byKeyboard) panel.querySelector("a")?.focus();
        };
        toggle.addEventListener("click", (e) => {
            e.stopPropagation();
            if (panel.hidden) open(e.detail === 0);
            else close();
        });
        panel.addEventListener("click", (e) => {
            if (e.target.closest("a")) close();
        });
        panel.addEventListener("keydown", (e) => {
            const links = [...panel.querySelectorAll("a")];
            const at = links.indexOf(document.activeElement);
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const next = (at + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
                links[next].focus();
            }
        });
        document.addEventListener("click", (e) => {
            if (!menu.contains(e.target)) close();
        });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && !panel.hidden) {
                close();
                toggle.focus();
            }
        });
        window.addEventListener("resize", close);
        window.addEventListener("hashchange", close);
        $("#mainnav").addEventListener("scroll", close);
    });
}

/* ------------------------------------------------------------------ */
/* Queue management                                                    */

/* ------------------------------------------------------------------ */

async function createQueue(copyFrom = null) {
    const entry = currentAccount();
    if (!entry) return;
    const name = prompt(
        copyFrom
            ? "Nombre de la copia (por ejemplo «Noche»):"
            : "Nombre de la nueva cola (por ejemplo «Noche»):",
        copyFrom ? `${copyFrom.name} (copia)` : `Cola ${entry.queues.length + 1}`
    );
    if (name === null) return;
    const body = {name: name.trim()};
    if (copyFrom) body.copy_from = copyFrom.id;
    const created = await api(`/api/accounts/${entry.account.id}/schedules`, {
        method: "POST", body: JSON.stringify(body),
    });
    setQueue(created.schedule.id);
    toast(`Cola «${created.schedule.name}» creada`);
    await refresh();
    if (state.page !== "programacion") location.hash = "#/programacion";
}

/* Deleting a queue is the one irreversible thing in the page, so it asks in a
   dialog that says exactly what goes and what stays. */
function deleteQueue() {
    const entry = currentAccount();
    const queue = currentQueue();
    if (!entry || !queue || entry.queues.length < 2) return;

    const {schedule, stats} = queue;
    const others = entry.queues.filter((q) => q.schedule.id !== schedule.id);
    const total = stats.queued + stats.in_flight + stats.delegated + stats.published + stats.failed;
    const body = el("div", "queue-delete");

    body.append(el("p", null,
        `Se borrará la cola «${schedule.name}» y sus reglas: horario, intervalo, máximo `
        + "diario y opciones de publicación. Los límites de la cuenta y el registro de "
        + "Actividad no se tocan."));

    let mode = others.length ? "move" : "purge";
    const target = el("select");
    others.forEach((o) => {
        const opt = el("option", null, o.schedule.name);
        opt.value = o.schedule.id;
        target.append(opt);
    });
    // Videos parked in Zernio block a purge unless the user insists: the usual
    // reason is that they already emptied Zernio's queue by hand.
    const force = el("input");
    force.type = "checkbox";
    force.disabled = mode !== "purge";

    if (total) {
        const breakdown = [
            stats.queued && `${stats.queued} esperando`,
            stats.in_flight && `${stats.in_flight} subiéndose`,
            stats.delegated && `${stats.delegated} entregados a Zernio`,
            stats.published && `${stats.published} publicados`,
            stats.failed && `${stats.failed} fallidos`,
        ].filter(Boolean).join(" · ");
        body.append(el("p", "muted small", `Tiene ${total} vídeo(s): ${breakdown}.`));

        const choices = el("div", "checks choices");
        const forceRow = el("label", "check check-nested");
        forceRow.append(force, document.createTextNode(
            ` Borrarla igualmente, con sus ${stats.delegated} vídeo(s) entregados a Zernio`));
        forceRow.append(el("span", "muted small block",
            "Antes se intenta cancelarlos en Zernio; si ya los borraste allí, no pasa nada. "
            + "Los que Zernio no deje cancelar se publicarán aunque desaparezcan de aquí."));

        [["move", "Mover sus vídeos a otra cola"], ["purge", `Borrar la cola con sus ${total} vídeo(s)`]]
            .forEach(([value, label]) => {
                if (value === "move" && !others.length) return;
                const row = el("label", "check");
                const radio = el("input");
                radio.type = "radio";
                radio.name = "queue-delete-mode";
                radio.value = value;
                radio.checked = value === mode;
                radio.addEventListener("change", () => {
                    mode = value;
                    target.disabled = mode !== "move";
                    force.disabled = mode !== "purge";
                    if (mode !== "purge") force.checked = false;
                });
                row.append(radio, document.createTextNode(` ${label}`));
                if (value === "move") row.append(document.createTextNode(" "), target);
                choices.append(row);
                if (value === "purge" && stats.delegated) choices.append(forceRow);
            });
        body.append(choices);
        if (stats.delegated) {
            body.append(el("div", "note note-warn",
                `«${schedule.name}» tiene ${stats.delegated} vídeo(s) entregados a Zernio, que se `
                + "publicarán aunque los borres aquí. Recupéralos primero (Cola → Recuperar de "
                + "Zernio), muévelos a otra cola o marca «Borrarla igualmente»."));
        }
        body.append(el("div", "note note-warn",
            "Borrar la cola con sus vídeos no se puede deshacer: también desaparece el "
            + "historial de lo que publicó. Para conservarlo, muévelos a otra cola. Los "
            + "archivos siguen en la biblioteca."));
    }

    if (stats.in_flight) {
        body.append(el("div", "note note-warn",
            "Hay un vídeo subiéndose en esta cola. Espera a que termine para poder borrarla."));
    }

    const bar = el("div", "actions-bar");
    bar.append(
        button("Eliminar cola", "btn btn-danger btn-sm", async () => {
            const params = mode === "move" && others.length
                ? `?move_to=${target.value}`
                : "?purge=true" + (force.checked ? "&force=true" : "");
            const res = await api(`/api/schedules/${schedule.id}${params}`, {method: "DELETE"});
            closeModal();
            setQueue(others[0].schedule.id);
            // One toast replaces the other, so a warning rides on the summary.
            toast([
                `Cola «${schedule.name}» eliminada`,
                res.moved && `${res.moved} vídeo(s) movidos`,
                res.removed && `${res.removed} vídeo(s) borrados`,
                res.files_deleted && `${res.files_deleted} archivo(s) borrados del disco`,
                res.cancelled_in_zernio && `${res.cancelled_in_zernio} cancelado(s) en Zernio`,
                res.left_in_zernio && `${res.left_in_zernio} siguen en Zernio y se publicarán `
                + `(${res.zernio_problem}): bórralos desde Zernio`,
            ].filter(Boolean).join(" · "), Boolean(res.left_in_zernio));
            await refresh();
        }),
        button("Cancelar", "btn btn-ghost btn-sm", () => closeModal()),
    );
    body.append(bar);
    openModal(`Eliminar «${schedule.name}»`, body);
}

function wireQueueBar() {
    $("#queue-add").addEventListener("click", () => createQueue().catch((e) => toast(e.message, true)));
    $("#queue-duplicate").addEventListener("click", () => {
        const queue = currentQueue();
        if (queue) createQueue(queue.schedule).catch((e) => toast(e.message, true));
    });
    $("#queue-rename").addEventListener("click", async () => {
        const queue = currentQueue();
        if (!queue) return;
        const name = prompt("Nombre de la cola:", queue.schedule.name);
        if (name === null || !name.trim()) return;
        try {
            const res = await api(`/api/schedules/${queue.schedule.id}`, {
                method: "PATCH", body: JSON.stringify({name: name.trim()}),
            });
            toast(`Ahora se llama «${res.schedule.name}»`);
            await refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#queue-delete").addEventListener("click", deleteQueue);
    $("#queue-hide").addEventListener("click", () => {
        const queue = currentQueue();
        if (queue) toggleHidden(queue.schedule).catch((e) => toast(e.message, true));
    });
    [["#queue-up", -1], ["#queue-down", 1]].forEach(([sel, delta]) => {
        $(sel).addEventListener("click", async () => {
            const entry = currentAccount();
            const queue = currentQueue();
            if (!entry || !queue) return;
            const order = entry.queues.map((q) => q.schedule.id);
            const from = order.indexOf(queue.schedule.id);
            const to = from + delta;
            if (to < 0 || to >= order.length) return;
            order.splice(to, 0, ...order.splice(from, 1));
            try {
                await api(`/api/accounts/${entry.account.id}/schedules/order`, {
                    method: "PUT", body: JSON.stringify({order}),
                });
                await refresh();
            } catch (err) {
                toast(err.message, true);
            }
        });
    });

}

function wireScheduleForm() {
    const inputs = [
        "#sch-enabled", "#sch-int-min", "#sch-int-max", "#sch-max", "#sch-tz",
        "#sch-nocomment", "#sch-noduet", "#sch-nostitch", "#sch-aigc",
        "#sch-disclose", "#sch-brand-organic", "#sch-brand-content",
    ];
    inputs.forEach((sel) => {
        $(sel).addEventListener("change", () => {
            markDirty();
            if (sel === "#sch-int-min" || sel === "#sch-int-max") renderIntervalPresets();
            renderDisclosure();
            renderStartPresets();
        });
    });
    $("#sch-nomax").addEventListener("change", (e) => {
        $("#sch-max").disabled = e.target.checked;
        markDirty();
    });
    $("#win-add").addEventListener("click", () => {
        const start = timeToMinutes($("#win-start").value || "09:00");
        const endRaw = timeToMinutes($("#win-end").value || "22:00");
        const end = endRaw === 0 ? 1440 : endRaw;
        if (start === end) return toast("La franja no puede empezar y acabar a la misma hora", true);
        if (state.windows.some((w) => w.start === start && w.end === end)) return toast("Esa franja ya está añadida", true);
        state.windows.push({start, end});
        markDirty();
        renderWindows();
        renderWindowPresets();
    });
    $("#sch-save").addEventListener("click", saveSchedule);
    $("#sch-preview").addEventListener("click", loadPreview);
}

async function saveSchedule() {
    const queue = currentQueue();
    if (!queue) return;
    const disclose = $("#sch-disclose").checked;
    const body = {
        name: queue.schedule.name,
        enabled: $("#sch-enabled").checked,
        interval_min_minutes: Number($("#sch-int-min").value),
        interval_max_minutes: Number($("#sch-int-max").value),
        jitter_seconds: queue.schedule.jitter_seconds,
        max_per_day: $("#sch-nomax").checked ? null : Number($("#sch-max").value),
        windows: state.windows,
        days_of_week: state.days.slice().sort((a, b) => a - b),
        timezone: $("#sch-tz").value,
        start_date: $("#sch-start").value || null,
        start_time: ($("#sch-start").value && $("#sch-start-time").value) || null,
        disable_comment: $("#sch-nocomment").checked,
        disable_duet: $("#sch-noduet").checked,
        disable_stitch: $("#sch-nostitch").checked,
        is_aigc: $("#sch-aigc").checked,
        brand_content_toggle: disclose && $("#sch-brand-content").checked,
        brand_organic_toggle: disclose && $("#sch-brand-organic").checked,
    };
    if (body.interval_min_minutes > body.interval_max_minutes)
        return toast("El intervalo mínimo no puede ser mayor que el máximo", true);
    try {
        const res = await api(`/api/schedules/${queue.schedule.id}`, {method: "PUT", body: JSON.stringify(body)});
        state.formDirty = false;
        $("#sch-status").textContent = res.schedule.next_slot_local
            ? `Guardado · próxima publicación ${res.schedule.next_slot_local}`
            : "Guardado · no hay huecos válidos con estas reglas";
        toast(`«${res.schedule.name}» guardada`);
        await refresh();
        renderWarnings(res.warnings);
    } catch (err) {
        toast(err.message, true);
    }
}

async function loadPreview() {
    const queue = currentQueue();
    if (!queue) return;
    if (state.formDirty) toast("Guarda los cambios para simularlos", true);
    try {
        const res = await api(`/api/schedules/${queue.schedule.id}/preview?count=12`);
        const list = $("#preview-list");
        list.replaceChildren(
            ...(res.slots.length ? res.slots.map((slot, i) => {
                const row = el("div", "row");
                row.append(el("span", "idx", `#${i + 1}`), el("span", null, slot.local));
                return row;
            }) : [el("div", "empty", "Con estas reglas no hay ningún hueco válido.")])
        );
        renderWarnings(res.warnings);
        $("#preview-card").hidden = false;
        $("#preview-card").scrollIntoView({behavior: "smooth", block: "nearest"});
    } catch (err) {
        toast(err.message, true);
    }
}

function wireQueue() {
    $("#queue-from-library").addEventListener("click", async () => {
        const entry = currentAccount();
        const queue = currentQueue();
        if (!entry || !queue) return;
        try {
            await openLibraryPicker(entry, queue);
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#handover-btn").addEventListener("click", async () => {
        const queue = currentQueue();
        if (!queue) return;
        try {
            await openHandoverDialog(queue);
        } catch (err) {
            toast(err.message, true);
        }
    });

    /** Take back from Zernio: the whole queue, one folder, or the ticked videos. */
    async function recover(queue, scope, question) {
        if (!confirm(question)) return;
        try {
            const res = await api(`/api/schedules/${queue.schedule.id}/delegation/cancel`, {
                method: "POST", body: JSON.stringify(scope),
            });
            state.queueSelected.clear();
            toast(res.message, !!res.problem);
            await refresh();
        } catch (err) {
            toast(err.message, true);
        }
    }

    $("#handover-cancel").addEventListener("click", () => {
        const queue = currentQueue();
        if (!queue) return;
        const of = scopeSuffix(state.queueScope);
        recover(queue, queueScope(), of
            ? `¿Cancelar en Zernio los vídeos${of} programados en «${queue.schedule.name}» y devolverlos a la cola?`
            : `¿Cancelar en Zernio los vídeos programados de «${queue.schedule.name}» y devolverlos a la cola?`);
    });

    /** The ticked videos of the current queue that pass ``test``. */
    function selectedWhere(test) {
        const entry = currentAccount();
        const queue = currentQueue();
        if (!entry || !queue) return [];
        return (entry.videos || []).filter((v) => v.schedule_id === queue.schedule.id
            && state.queueSelected.has(v.id) && test(v));
    }

    $("#queue-recover-selected").addEventListener("click", () => {
        const queue = currentQueue();
        const ids = selectedWhere((v) => v.status === "DELEGATED").map((v) => v.id);
        if (!queue || !ids.length) return;
        recover(queue, {video_ids: ids},
            `¿Cancelar en Zernio los ${ids.length} vídeo(s) seleccionados y devolverlos a la cola?`);
    });
    $("#queue-remove-selected").addEventListener("click", async () => {
        const queue = currentQueue();
        const ids = selectedWhere(isWaiting).map((v) => v.id);
        if (!queue || !ids.length) return;
        if (!confirm(`¿Quitar ${ids.length} vídeo(s) de «${queue.schedule.name}»? `
            + "Siguen en la biblioteca y puedes volver a añadirlos.")) return;
        try {
            const res = await api(`/api/schedules/${queue.schedule.id}/videos/remove`, {
                method: "POST", body: JSON.stringify({video_ids: ids}),
            });
            ids.forEach((id) => state.queueSelected.delete(id));
            toast(res.message);
            await refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
    const moveSelected = (where) => {
        const ids = selectedWhere(isMovable)
            .sort(releaseOrder)
            .map((v) => v.id);
        if (ids.length) moveInQueue(ids, where);
    };
    $("#queue-move-top").addEventListener("click", () => moveSelected({position: 1}));
    $("#queue-move-bottom").addEventListener("click", () => moveSelected({position: 1_000_000}));
    $("#queue-move-to").addEventListener("click", () => {
        const queue = currentQueue();
        const entry = currentAccount();
        if (!queue || !entry) return;
        const total = waitingVideos(entry.videos || [], queue.schedule.id).length;
        const count = selectedWhere(isMovable).length;
        const answer = prompt(`¿A qué turno van ${count === 1 ? "el seleccionado" : `los ${count} seleccionados`}? (1–${total})\n`
            + (count > 1 ? "Van juntos, en el orden que tenían, empezando en ese turno." : ""));
        if (answer === null) return;
        const turn = Math.round(Number(answer));
        if (!Number.isFinite(turn) || turn < 1) {
            toast("Escribe un número de turno, 1 o más", true);
            return;
        }
        moveSelected({position: turn});
    });
    wireQueueDrag();
    $("#queue-select-all").addEventListener("change", (e) => {
        const entry = currentAccount();
        const queue = currentQueue();
        if (!entry || !queue) return;
        const filter = QUEUE_FILTERS.find(([id]) => id === state.queueFilter)?.[2] || (() => true);
        inQueueFolder((entry.videos || []).filter((v) => v.schedule_id === queue.schedule.id))
            .filter(filter)
            .filter(isTickable)
            .forEach((v) => (e.target.checked ? state.queueSelected.add(v.id) : state.queueSelected.delete(v.id)));
        renderQueue(entry, queue);
    });
    [["#queue-shuffle", "shuffle"], ["#queue-unshuffle", "unshuffle"]].forEach(([sel, action]) => {
        $(sel).addEventListener("click", async () => {
            const queue = currentQueue();
            if (!queue) return;
            try {
                const res = await api(`/api/schedules/${queue.schedule.id}/${action}`, {method: "POST"});
                // The new order is the thing to look at.
                state.queueFilter = "pending";
                toast(res.message);
                await refresh();
            } catch (err) {
                toast(err.message, true);
            }
        });
    });
}

function wireEvents() {
    ["#ev-level", "#ev-account"].forEach((sel) => $(sel).addEventListener("change", loadEvents));
    $("#ev-q").addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadEvents();
    });
    $("#ev-refresh").addEventListener("click", loadEvents);
}

/* ------------------------------------------------------------------ */
/* Boot                                                                */

/* ------------------------------------------------------------------ */

async function boot() {
    state.refresh = refresh;
    state.route = route;
    try {
        const stored = localStorage.getItem("tts.accountId");
        if (stored) state.accountId = Number(stored);
        const storedQueue = localStorage.getItem("tts.queueId");
        if (storedQueue) state.queueId = Number(storedQueue);
        restoredQueue = Boolean(storedQueue);
        state.platform = localStorage.getItem("tts.platform") || "";
    } catch { /* private mode etc. */
    }

    wireShell();
    wireQueueBar();
    wireScheduleForm();
    wireQueue();
    wireLibrary();
    wireCalendar();
    wireAnalytics();
    wireComments();
    wireEvents();
    wireConnectForm();
    wireAdmin();

    try {
        state.presets = await api("/api/presets");
    } catch {
        toast("No se pudo contactar con el servidor", true);
        return;
    }
    const {page, arg} = parseHash();
    state.page = page;
    state.pageArg = arg;
    freshArg = true;
    await refresh();
    poller.lastAt = Date.now();
    wirePolling();
}

boot();
