/* Biblioteca: every video once, with its texts for TikTok and YouTube, and the
   picker a queue adds videos from. */

import {
    $,
    accountLabel,
    accounts,
    api,
    button,
    closeModal,
    el,
    everyQueue,
    fillSelect,
    fmtBytes,
    fmtDuration,
    link,
    openModal,
    setAccount,
    setQueue,
    state,
    statusLabel,
    toast,
} from "./shared.js";
import {fixedHashtags, normalizeTag, openComposer} from "./composer.js";
import {dateTimePicker, longLabel, nowIn} from "./datetimepicker.js";
import {
    appendScope,
    autoColor,
    isEmptyScope,
    LABEL_PALETTE,
    labelChip,
    labelColor,
    labelColors,
    labelFilterRows,
    labelMatch,
    paint,
    rememberColors,
    scopeFolders,
    scopePicker,
} from "./labels.js";

const PLATFORMS = [["tiktok", "TikTok"], ["youtube", "YouTube"]];
const PLATFORM_LABEL = Object.fromEntries(PLATFORMS);
const VIDEO_RE = /\.(mp4|mov|webm)$/i;
const TEXT_RE = /\.txt$/i;
/* A video's cover: <video>.jpg, or <video>_portada.jpg as ContentBuilder names it. */
const COVER_RE = /\.(jpe?g|png)$/i;
/* The series in a reel's name, which is its folder (services/library.py: _SERIES_RE). */
const SERIES_RE = /^reel_\d+_([^_]+)_/i;
/* Where a video whose name says nothing goes (services/library.py: DEFAULT_FOLDER). */
const DEFAULT_FOLDER = "varios";
const WRITING = ["pending", "generating"];
/* lib.folder when the whole library is listed instead of one folder. */
const ALL = "*";
/* The queue list's status filter that shows a video in this status. */
const QUEUE_FILTER_OF = {
    QUEUED: "pending", SCHEDULED: "pending", UPLOADING: "pending", PROCESSING: "pending",
    DELEGATED: "delegated", PUBLISHED: "published", FAILED: "failed",
};
const SOURCE_LABEL = {
    "whisper": "Whisper", "whisper+ollama": "Whisper + Ollama",
    "ficha": "ficha .txt", "ficha+ollama": "ficha .txt + Ollama",
    "fallback": "sin voz", "manual": "texto propio",
};

/* The labels ticked in the sidebar, and whether a folder needs all of them or
   any; the ones crossed out, and «Sin etiquetas» (labels.js: emptyScope). */
const SCOPE_KEY = "tts.libScope";
const NO_LABELS = {labels: [], match: "all", exclude: [], unlabeled: ""};

function storedScope() {
    try {
        const value = JSON.parse(localStorage.getItem(SCOPE_KEY) || "null");
        const strings = (list) => (Array.isArray(list) ? list.filter((l) => typeof l === "string") : []);
        if (value && Array.isArray(value.labels)) {
            return {
                labels: strings(value.labels),
                match: value.match === "any" ? "any" : "all",
                exclude: strings(value.exclude),
                unlabeled: ["include", "exclude"].includes(value.unlabeled) ? value.unlabeled : "",
            };
        }
        // The single label of the first version of this filter.
        const old = localStorage.getItem("tts.libLabel");
        return {...NO_LABELS, labels: old ? [old] : []};
    } catch {
        return {...NO_LABELS};
    }
}

/* The accounts whose queues the library shows (none ticked = every account). */
const ACCOUNTS_KEY = "tts.libAccounts";

function storedAccounts() {
    try {
        const value = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "[]");
        return Array.isArray(value) ? value.filter(Number.isInteger) : [];
    } catch {
        return [];
    }
}

function saveAccounts() {
    try {
        localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(lib.accounts));
    } catch {
        /* a private window: the filter just is not remembered */
    }
}

/** The queues the library offers: never a hidden one, and only those of the
 *  accounts ticked above the list. */
function libQueues() {
    return everyQueue().filter((q) => !q.schedule.hidden
        && (!lib.accounts.length || lib.accounts.includes(q.account.id)));
}

/** One «📋 En …» entry of a video, under the same rule. */
/** A use shown under the video: hidden queues and other accounts only hide history. */
function shownUse(entry) {
    return entry.needs_file
        || (!entry.queue_hidden && (!lib.accounts.length || lib.accounts.includes(entry.account_id)));
}

function saveScope() {
    try {
        localStorage.setItem(SCOPE_KEY, JSON.stringify(labelScope()));
        localStorage.removeItem("tts.libLabel");
    } catch {
        /* a private window: the filter just is not remembered */
    }
}

/** What the label chips ask for, without the folders ticked. */
function labelScope() {
    return {folders: [], labels: lib.labels, match: lib.match, exclude: lib.exclude, unlabeled: lib.unlabeled};
}

/** The folders ticked (none = all) narrowed by the labels ticked (labels.js). */
function libScope() {
    return {...labelScope(), folders: lib.folders};
}

function labelFiltered() {
    return !isEmptyScope(labelScope());
}

/** «Todas las de «serie» + «terminada»», «Todas las sin etiquetas», «Todas las de «serie», sin «vieja»». */
function labelsTitle() {
    return `Todas las ${lib.labels.length ? "de " : ""}${labelsText()}`;
}

/** «serie + terminada», «serie o sin etiquetas», «sin «vieja» ni «rota»», «con alguna etiqueta». */
function labelsText() {
    const wanted = lib.labels.map((l) => `«${l}»`).join(lib.match === "any" ? " o " : " + ");
    const bare = lib.unlabeled === "include" ? [wanted, "sin etiquetas"].filter(Boolean).join(" o ") : wanted;
    return [
        bare,
        lib.exclude.length && `sin ${lib.exclude.map((l) => `«${l}»`).join(" ni ")}`,
        lib.unlabeled === "exclude" && "con alguna etiqueta",
    ].filter(Boolean).join(", ");
}

/** Lower case and without accents, so «Hábitos» is found typing «habit». */
function fold(text) {
    return text.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
}

function labelsOf(folder) {
    return lib.overview?.folders.find((f) => f.folder === folder)?.labels || [];
}

/** The folders the labels let through (all of them without labels). */
function labelledFolders() {
    const folders = lib.overview?.folders || [];
    const keep = scopeFolders(labelScope(), folders);
    return keep ? folders.filter((f) => keep.has(f.folder)) : folders;
}

/** The sidebar's folders: the ones the labels and the folder search let through,
 and the ticked ones (to untick them). */
function visibleFolders() {
    const keep = new Set(labelledFolders().map((f) => f.folder));
    const needle = fold(lib.folderQ);
    return (lib.overview?.folders || []).filter((f) => lib.folders.includes(f.folder)
        || (keep.has(f.folder) && (!needle || fold(f.folder).includes(needle))));
}

/** What «Todas las carpetas» lists now: every folder, the ticked ones, narrowed by the labels. */
function scopedFolders() {
    const folders = lib.overview?.folders || [];
    const keep = scopeFolders(libScope(), folders);
    return keep ? folders.filter((f) => keep.has(f.folder)) : folders;
}

const lib = {
    overview: null,
    items: null,
    folder: null,
    /* The search above the list: inside the folder open, or the folders and labels picked. */
    q: "",
    /* The sidebar's search: which folders it lists, nothing else. */
    folderQ: "",
    /* "" = every video; a queue id, "any" (in some queue) or "none" (in none). */
    queue: "",
    /* Accounts ticked in «Cuentas»: only their queues are offered (none = all). */
    accounts: storedAccounts(),
    /* "" = any; "written", "writing" (pending or generating) or "failed". */
    text: "",
    /* Folders ticked in the sidebar for «Todas las carpetas» (none = all of them),
       narrowed by the labels ticked: carrying all of them, or any (match). */
    folders: [],
    ...storedScope(),
    /* Only the videos whose texts the review found something to fix in. */
    issues: false,
    selected: new Set(),
    loadedAt: 0,
    loading: null,
    uploading: false,
    /* What was last drawn, so a poll that brings nothing new redraws nothing. */
    drawn: "",
};

/* ------------------------------------------------------------------ */
/* Data                                                                */

/* ------------------------------------------------------------------ */

async function load() {
    if (lib.loading) return lib.loading;
    lib.loading = (async () => {
        const overview = await api("/api/library");
        lib.overview = overview;
        rememberColors(overview);
        // A label or a folder that is gone stops narrowing anything.
        const known = (list) => list.filter((l) => overview.labels.some((x) => x.label === l));
        const labels = known(lib.labels);
        const exclude = known(lib.exclude);
        if (labels.length !== lib.labels.length || exclude.length !== lib.exclude.length) {
            lib.labels = labels;
            lib.exclude = exclude;
            saveScope();
        }
        lib.folders = lib.folders.filter((f) => overview.folders.some((x) => x.folder === f));
        const folders = labelledFolders().map((f) => f.folder);
        // The labels hide the open folder when it does not carry them: show every one that does.
        if (labelFiltered() && lib.folder && lib.folder !== ALL && !folders.includes(lib.folder)) lib.folder = ALL;
        if (lib.folder !== ALL && !folders.includes(lib.folder)) lib.folder = folders[0] || null;
        const params = new URLSearchParams({limit: "2000"});
        if (lib.folder === ALL) appendScope(params, libScope());
        else if (lib.folder) params.set("folder", lib.folder);
        if (lib.q) params.set("q", lib.q);
        if (lib.queue) params.set("queue", lib.queue);
        if (["any", "none"].includes(lib.queue)) lib.accounts.forEach((id) => params.append("accounts", id));
        if (lib.text) params.set("text", lib.text);
        if (lib.issues) params.set("issues", "true");
        lib.items = lib.folder
            ? await api(`/api/library/items?${params}`)
            : {total: 0, items: []};
        const present = new Set(lib.items.items.map((i) => i.id));
        lib.selected.forEach((id) => {
            if (!present.has(id)) lib.selected.delete(id);
        });
        lib.loadedAt = Date.now();
    })();
    try {
        await lib.loading;
    } finally {
        lib.loading = null;
    }
    const signature = JSON.stringify([lib.overview, lib.items, lib.folder, lib.q, lib.queue, lib.text, lib.folders, labelScope(), lib.issues, lib.accounts,
        everyQueue().map((q) => [q.schedule.id, q.schedule.name, q.schedule.hidden]), state.data?.captions?.eta_seconds]);
    if (signature === lib.drawn) return;
    lib.drawn = signature;
    draw();
}

function reload() {
    return load().catch((err) => toast(err.message, true));
}

/** Called on every refresh of the page: reloads only when something may have changed. */
export function renderLibraryPage() {
    const age = Date.now() - lib.loadedAt;
    const writing = (state.data?.library?.writing || 0) > 0;
    if (!lib.overview || (writing && age > 5000) || age > 60000) reload();
    else if (!lib.drawn) draw();
}

/** The page was left and entered again: draw what is known right away. */
export function forgetLibraryDrawing() {
    lib.drawn = "";
}

/* ------------------------------------------------------------------ */
/* Drawing                                                             */

/* ------------------------------------------------------------------ */

function textBadge(item) {
    if (item.preflight_errors.length) return el("span", "badge FAILED", "no válido");
    switch (item.caption_status) {
        case "pending":
            return el("span", "badge SCHEDULED", "✍️ en cola");
        case "generating":
            return el("span", "badge UPLOADING", "✍️ escribiendo");
        case "failed":
            return el("span", "badge FAILED", "texto falló");
        default:
            return el("span", "badge PUBLISHED", item.caption_source === "manual" ? "texto propio" : "listo");
    }
}

/** «⚠ 3»: what the editor's review would flag in the texts (errors and warnings, not tips). */
function reviewBadge(item) {
    const r = item.review;
    if (!r || !(r.errors + r.warnings)) return null;
    const badge = el("button", `badge review-badge ${r.errors ? "FAILED" : "UPLOADING"}`,
        `${r.errors ? "⛔" : "⚠"} ${r.errors + r.warnings}`);
    badge.type = "button";
    badge.title = r.issues.map((i) => `${i.platform === "youtube" ? "YouTube" : "TikTok"}: ${i.message}`)
        .join("\n") + "\n\nClic para abrir sus textos.";
    badge.addEventListener("click", () => openTextsEditor(item));
    return badge;
}

function where(entry) {
    const who = accounts().find((a) => a.account.id === entry.account_id);
    const name = who ? accountLabel(who.account) : PLATFORM_LABEL[entry.platform] || entry.platform;
    return `${name} · ${entry.queue_name || "sin cola"}${entry.queue_hidden ? " (oculta)" : ""}`;
}

/** Open the queue a video is in, filtered so that the video is on screen. */
function showInQueue(item, entry) {
    if (!entry.schedule_id) return;
    setAccount(entry.account_id);
    setQueue(entry.schedule_id);
    state.queueFilter = QUEUE_FILTER_OF[entry.status] || "all";
    state.queueScope = {folders: [item.folder], labels: [], match: "all"};
    state.queueFocus = {id: entry.video_id, until: Date.now() + 6000, scrolled: false};
    location.hash = "#/cola";
}

/** Where the video is queued, right under its name: one chip per queue. */
function usageLine(item) {
    const box = el("div", "lib-usage");
    const shown = item.queued.filter(shownUse);
    if (!shown.length) {
        box.append(el("span", "lib-chip none", item.queued.length && lib.accounts.length
            ? "📋 No está en ninguna cola de estas cuentas"
            : "📋 No está en ninguna cola"));
        return box;
    }
    box.append(el("span", "lib-usage-label", "📋 En"));
    shown.forEach((entry) => {
        const chip = el("button", `lib-chip ${entry.status}`,
            `${where(entry)} · ${statusLabel(entry.status)}`);
        chip.type = "button";
        chip.disabled = !entry.schedule_id;
        chip.title = (entry.schedule_id ? "Abrir la cola con este vídeo a la vista. " : "")
            + (entry.follows_library
                ? "Publica los textos de la biblioteca."
                : "Esta cola tiene sus propios textos.");
        chip.addEventListener("click", () => showInQueue(item, entry));
        box.append(chip);
    });
    return box;
}

function textLines(item) {
    const box = el("div", "lib-texts");
    PLATFORMS.forEach(([key, label]) => {
        const t = item.texts[key];
        const line = el("div", "lib-text");
        line.append(el("span", `lib-plat ${key}`, label));
        const first = key === "youtube"
            ? t.title || (t.description || "").split("\n")[0]
            : (t.description || "").split("\n")[0];
        line.append(el("span", "lib-text-body",
            [first || "— sin texto —", (t.tags || []).join(" ")].filter(Boolean).join("  ·  ")));
        line.title = [t.title && `Título: ${t.title}`, t.description,
            (t.tags || []).join(" "), t.first_comment && `Comentario fijado: ${t.first_comment}`]
            .filter(Boolean).join("\n\n");
        box.append(line);
    });
    return box;
}

function itemRow(item) {
    const row = el("div", `item lib-item${lib.selected.has(item.id) ? " is-selected" : ""}`);
    const box = el("input");
    box.type = "checkbox";
    box.checked = lib.selected.has(item.id);
    box.addEventListener("change", () => {
        if (box.checked) lib.selected.add(item.id);
        else lib.selected.delete(item.id);
        row.classList.toggle("is-selected", box.checked);
        drawToolbar();
    });
    row.append(box, textBadge(item));

    const main = el("div", "item-main");
    const titleRow = el("div", "item-title-row");
    titleRow.append(el("div", "item-title", item.original_name));
    const flagged = reviewBadge(item);
    if (flagged) titleRow.append(flagged);
    main.append(titleRow);
    main.append(usageLine(item));
    const bits = [];
    if (lib.folder === ALL) bits.push(`📁 ${item.folder}`);
    if (item.duration_sec) bits.push(`${item.duration_sec}s`);
    if (item.resolution) bits.push(item.resolution);
    if (item.size_mb) bits.push(`${item.size_mb} MB`);
    if (item.has_audio === false) bits.push("sin audio");
    if (item.has_cover) bits.push("🖼 con portada");
    if (item.caption_source && SOURCE_LABEL[item.caption_source]) bits.push(SOURCE_LABEL[item.caption_source]);
    const meta = el("div", "item-meta", bits.join(" · "));
    if (lib.folder === ALL) labelsOf(item.folder).forEach((l) => meta.append(" ", labelChip(l)));
    main.append(meta);
    main.append(textLines(item));
    if (item.caption_status === "generating") {
        const line = el("div", "item-ai generating");
        line.append(el("span", "pulse"), document.createTextNode(" Escuchando el vídeo y escribiendo sus textos…"));
        main.append(line);
    }
    if (item.caption_error) main.append(el("div", `item-ai ${item.caption_status === "failed" ? "failed" : ""}`,
        `${item.caption_status === "failed" ? "⚠️ " : ""}${item.caption_error}`));
    item.preflight_errors.forEach((e) => main.append(el("div", "item-error", e)));
    row.append(main);

    const actions = el("div", "item-actions");
    actions.append(
        link("Ver", `/api/library/${item.id}/file`, "btn btn-ghost btn-sm"),
        ...(item.has_cover ? [link("Portada", `/api/library/${item.id}/cover`, "btn btn-ghost btn-sm")] : []),
        button("Textos", "btn btn-ghost btn-sm", () => openTextsEditor(item)),
    );
    const ai = state.data?.health?.ai;
    if (ai && ai.enabled && !WRITING.includes(item.caption_status) && !item.preflight_errors.length) {
        actions.append(button("Reescribir", "btn btn-ghost btn-sm", async () => {
            await api(`/api/library/${item.id}/rewrite`, {method: "POST"});
            toast("Escribiendo sus textos… aparecerán aquí");
            await reload();
            await state.refresh();
        }));
    }
    if (!item.preflight_errors.length) {
        actions.append(button("Añadir…", "btn btn-ghost btn-sm", () => openAddToQueue([item])));
    }
    actions.append(button("Mover…", "btn btn-ghost btn-sm", () => openMoveItems([item])));
    row.append(actions);
    return row;
}

/** The labels as a small table: each one ✓ (the folders must carry it) or ✕ (must not). */
function drawLabelFilter() {
    const box = $("#lib-label-filter");
    const labels = lib.overview?.labels || [];
    const folders = lib.overview?.folders || [];
    box.hidden = !labels.length;
    if (!labels.length) return box.replaceChildren();
    const pick = (next) => {
        Object.assign(lib, next);
        saveScope();
        lib.selected.clear();
        reload();
    };
    const result = el("div", "lib-lf-result small");
    result.hidden = !labelFiltered();
    if (labelFiltered()) {
        const kept = labelledFolders();
        result.textContent = `→ ${kept.length} de ${folders.length} carpetas · ${kept.reduce((n, f) => n + f.count, 0)} vídeos`;
    }
    box.replaceChildren(...labelFilterRows({
        labels: labels.map(({label, folders: count}) => ({label, count})),
        bare: folders.filter((f) => !f.labels.length).length,
        scope: labelScope(),
        onChange: pick,
        onClear: () => pick({labels: [], exclude: [], unlabeled: ""}),
    }), labelMatch(labelScope(), (match) => pick({match})), result);
}

function drawFolders() {
    drawLabelFilter();
    const box = $("#lib-folders");
    const folders = visibleFolders();
    const allView = lib.folder === ALL;
    if (!lib.overview?.folders.length) {
        box.replaceChildren(el("div", "empty", "Todavía no hay vídeos."));
    } else {
        const everything = el("button", `lib-folder${allView && !lib.folders.length ? " is-active" : ""}`);
        everything.type = "button";
        const total = labelledFolders().reduce((n, f) => n + f.count, 0);
        everything.append(el("span", "lib-folder-name", labelFiltered() ? labelsTitle() : "Todas las carpetas"),
            el("span", "lib-folder-count", String(total)));
        everything.title = "Todas juntas. Marca las casillas para juntar sólo algunas.";
        everything.addEventListener("click", () => {
            lib.folder = ALL;
            lib.folders = [];
            lib.selected.clear();
            reload();
        });
        const kept = new Set(labelledFolders().map((f) => f.folder));
        const missing = lib.folderQ && !folders.length
            ? [el("div", "lib-folders-empty muted small", `Ninguna carpeta${labelFiltered() ? " de las etiquetas elegidas" : ""} se llama «${lib.folderQ}».`)]
            : [];
        box.replaceChildren(everything, ...missing, ...folders.map((f) => {
            const ticked = lib.folders.includes(f.folder);
            const line = el("div", `lib-folder-line${ticked ? " is-ticked" : ""}`);
            const tick = el("input");
            tick.type = "checkbox";
            tick.checked = ticked;
            tick.title = ticked ? "Quitar de la selección" : "Juntar con otras carpetas";
            tick.setAttribute("aria-label", `Juntar «${f.folder}» con otras carpetas`);
            tick.addEventListener("change", () => {
                lib.folders = tick.checked ? [...lib.folders, f.folder] : lib.folders.filter((x) => x !== f.folder);
                lib.folder = ALL;
                lib.selected.clear();
                reload();
            });
            const active = f.folder === lib.folder || (allView && ticked && kept.has(f.folder));
            const row = el("button", `lib-folder${active ? " is-active" : ""}`);
            row.type = "button";
            if (ticked && !kept.has(f.folder)) {
                line.classList.add("is-excluded");
                line.title = "Marcada, pero no tiene las etiquetas elegidas";
            }
            line.append(tick, row);
            const name = el("span", "lib-folder-name", f.folder);
            if (f.labels.length) {
                const dots = el("span", "lib-folder-dots");
                f.labels.forEach((l) => {
                    const dot = paint(el("span", "lib-folder-dot"), labelColor(l));
                    dot.title = l;
                    dots.append(dot);
                });
                name.prepend(dots);
            }
            row.append(name);
            const marks = el("span", "lib-folder-count");
            marks.append(document.createTextNode(String(f.count)));
            if (f.writing) marks.append(el("span", "lib-folder-mark", ` · ✍️ ${f.writing}`));
            if (f.failed || f.invalid) marks.append(el("span", "lib-folder-mark bad", ` · ⚠ ${f.failed + f.invalid}`));
            row.append(marks);
            row.title = `${f.count} vídeo(s) · ${fmtBytes(f.bytes)} · ${f.in_queues} en alguna cola`
                + (f.labels.length ? `\n🏷️ ${f.labels.join(", ")}` : "");
            row.addEventListener("click", () => {
                lib.folder = f.folder;
                lib.folders = [];
                lib.selected.clear();
                reload();
            });
            return line;
        }));
    }
    const o = lib.overview;
    const eta = state.data?.captions?.eta_seconds;
    $("#lib-totals").textContent = o
        ? `${o.total} vídeo(s) · ${fmtBytes(o.bytes)}` + (o.writing ? ` · ✍️ ${o.writing} escribiéndose` : "")
        + (o.writing && eta ? ` · acaban en ~${fmtDuration(eta)}` : "")
        : "";
    $("#lib-totals").title = o && o.writing
        ? "Uno a la vez, al ritmo de los últimos textos. «Reescribir» en un vídeo suelto pasa delante."
        : "";
}

function drawToolbar() {
    const shown = lib.items?.items || [];
    const count = lib.selected.size;
    const all = $("#lib-select-all");
    all.checked = shown.length > 0 && shown.every((i) => lib.selected.has(i.id));
    all.indeterminate = count > 0 && !all.checked;
    $("#lib-add").disabled = count === 0;
    $("#lib-rewrite").disabled = count === 0 || !state.data?.health?.ai?.enabled;
    $("#lib-sheet").disabled = count === 0;
    $("#lib-delete").disabled = count === 0;
    $("#lib-move").disabled = count === 0;
    $("#lib-tags").disabled = count === 0 && !(lib.folder && lib.folder !== ALL);
    $("#lib-issues").checked = lib.issues;
    $("#lib-text-filter").value = lib.text;
    $("#lib-add").textContent = count ? `Añadir ${count} a una cola…` : "Añadir a una cola…";
    const writing = shown.filter((i) => WRITING.includes(i.caption_status)).length;
    $("#lib-summary").textContent = lib.items
        ? `${shown.length}${lib.items.total > shown.length ? ` de ${lib.items.total}` : ""} vídeo(s)`
        + (count ? ` · ${count} seleccionado(s)` : "")
        + (writing ? ` · ✍️ ${writing} escribiéndose` : "")
        : "";
}

/** «Cuentas: Todas · Chica TikTok · Chica YouTube…»: which accounts' queues the
 *  library deals with. Several can be ticked (a reel goes to the TikTok and
 *  the YouTube of the same channel); none ticked is «Todas». */
function drawAccountChips() {
    const box = $("#lib-accounts");
    const all = accounts();
    // An account that was removed stops narrowing anything.
    const known = lib.accounts.filter((id) => all.some((e) => e.account.id === id));
    if (all.length && known.length !== lib.accounts.length) {
        lib.accounts = known;
        saveAccounts();
    }
    box.hidden = all.length < 2;
    if (box.hidden) return;
    const visible = (entry) => (entry.queues || []).filter((q) => !q.schedule.hidden).length;
    const chip = (active, children, onClick, title) => {
        const b = el("button", `chip${active ? " is-active" : ""}`);
        b.type = "button";
        b.setAttribute("aria-pressed", String(active));
        b.append(...children);
        if (title) b.title = title;
        b.addEventListener("click", onClick);
        return b;
    };
    box.replaceChildren(
        el("span", "lib-accounts-label", "Cuentas"),
        chip(!lib.accounts.length, [document.createTextNode("Todas")], () => pickAccounts([]),
            "Las colas de todas las cuentas"),
        ...all.map((entry) => {
            const {account} = entry;
            const on = lib.accounts.includes(account.id);
            const platform = account.platform || "tiktok";
            return chip(on, [
                    el("span", `lib-plat ${platform}`, PLATFORM_LABEL[platform] || platform),
                    document.createTextNode(` ${accountLabel(account)}`),
                    el("small", "chip-count", ` ${visible(entry)}`),
                ], () => pickAccounts(on
                    ? lib.accounts.filter((id) => id !== account.id)
                    : [...lib.accounts, account.id]),
                `${on ? "Quitar" : "Añadir"} las colas de ${accountLabel(account)} · ${visible(entry)} cola(s) visibles`);
        }),
    );
}

function pickAccounts(ids) {
    // Every account ticked is the same as none: «Todas».
    lib.accounts = ids.length >= accounts().length ? [] : ids;
    saveAccounts();
    lib.selected.clear();
    reload();
}

function drawQueueFilter() {
    const select = $("#lib-queue");
    const all = libQueues();
    if (lib.queue && !["any", "none"].includes(lib.queue)
        && !all.some((q) => String(q.schedule.id) === lib.queue)) lib.queue = "";
    const several = new Set(all.map((q) => q.account.id)).size > 1;
    const scoped = lib.accounts.length ? " (de estas cuentas)" : "";
    fillSelect(select, [
        ["", "Cualquier cola"],
        ["any", `En alguna cola${scoped}`],
        ["none", `En ninguna cola${scoped}`],
        ...all.map((q) => [String(q.schedule.id),
            `📋 ${several ? `${accountLabel(q.account)} · ` : ""}${q.schedule.name}`]),
    ], lib.queue);
}

/** «Todas las carpetas», «habitos, roma · «serie»», «Todas las de «serie» + «terminada»». */
function allTitle() {
    if (!lib.folders.length) return labelFiltered() ? labelsTitle() : "Todas las carpetas";
    const names = lib.folders.length > 3 ? `${lib.folders.length} carpetas` : lib.folders.join(", ");
    return labelFiltered() ? `${names} · ${labelsText()}` : names;
}

function draw() {
    if (state.page !== "biblioteca") return;
    drawFolders();
    $("#lib-folder-title").textContent = lib.folder === ALL ? allTitle() : (lib.folder || "Biblioteca");
    const oneFolder = !!lib.folder && lib.folder !== ALL;
    $("#lib-rename").hidden = !oneFolder;
    $("#lib-labels").hidden = !oneFolder;
    $("#lib-folder-labels").replaceChildren(...(oneFolder ? labelsOf(lib.folder).map((l) => labelChip(l)) : []));
    drawAccountChips();
    drawQueueFilter();
    const list = $("#lib-list");
    const items = lib.items?.items || [];
    if (!lib.overview) {
        list.replaceChildren(el("div", "empty", "Cargando…"));
    } else if (!items.length) {
        list.replaceChildren(el("div", "empty", lib.q
            ? `Ningún vídeo de aquí coincide con «${lib.q}».`
            : lib.issues ? "Ningún texto de aquí tiene nada que corregir. ✓"
                : lib.folder === ALL && !isEmptyScope(libScope()) && !scopedFolders().length
                    ? "Ninguna carpeta coincide con las carpetas y etiquetas elegidas."
                    : lib.text === "writing" ? "Aquí no se está escribiendo ningún texto. ✓"
                        : lib.text ? "Ningún vídeo de aquí coincide con el filtro de textos."
                            : lib.queue ? "Ningún vídeo de aquí coincide con el filtro de cola."
                                : "La biblioteca está vacía. Arrastra aquí arriba la carpeta con tus vídeos y sus .txt."));
    } else {
        list.replaceChildren(...items.map(itemRow));
    }
    drawToolbar();
    drawAiSwitch();
}

/** The switch «Reescribir los textos con IA», as the server has it. */
function drawAiSwitch() {
    const o = lib.overview;
    const sw = $("#lib-ai");
    if (!o || !sw) return;
    sw.checked = !!o.ai_rewrite && !!o.ai_enabled;
    sw.disabled = !o.ai_enabled;
    sw.closest(".switch-row").classList.toggle("is-disabled", !o.ai_enabled);
    sw.title = o.ai_enabled ? ""
        : "Los textos automáticos están desactivados (CAPTION_AI_ENABLED=false en .env): "
        + "los vídeos con ficha salen siempre con sus textos.";
}

/* ------------------------------------------------------------------ */
/* Editing the texts                                                   */

/* ------------------------------------------------------------------ */

function field(label, node, hint) {
    const wrap = el("label", "block-field");
    wrap.append(document.createTextNode(label), node);
    if (hint) wrap.append(el("span", "muted small", hint));
    return wrap;
}

/** Both platforms' texts of one library video, in the composer (composer.js). */
function openTextsEditor(item) {
    const following = item.queued.filter((q) => q.follows_library && q.writable).length;
    const notes = [];
    if (following) notes.push(`${following} vídeo(s) en cola usan estos textos y se actualizarán al guardar.`);
    if (WRITING.includes(item.caption_status)) {
        notes.push("Sus textos se están escribiendo: si guardas ahora, se quedan los tuyos y lo que "
            + "estaba escribiendo el modelo se descarta.");
    }
    openComposer({
        title: `Textos de «${item.original_name}»`,
        platforms: PLATFORMS.map(([key]) => key),
        texts: Object.fromEntries(PLATFORMS.map(([key]) => [key, item.texts[key]])),
        context: {library_id: item.id},
        notes,
        ai: state.data?.health?.ai,
        fixed: fixedHashtags(state.data?.settings),
        onSave: async (texts) => {
            await api(`/api/library/${item.id}`, {method: "PATCH", body: JSON.stringify(texts)});
            toast("Textos guardados");
            await reload();
            await state.refresh();
        },
    });
}

/* ------------------------------------------------------------------ */
/* Hashtags on many videos at once                                     */

/* ------------------------------------------------------------------ */

function tagList(text) {
    return text.split(/[\s,]+/).map(normalizeTag).filter(Boolean);
}

function openBulkTags(items) {
    const folder = lib.folder && lib.folder !== ALL ? lib.folder : null;
    const body = el("div", "bulk-tags");
    const scope = el("select", "wide");
    if (items.length) {
        const opt = el("option", null, `Los ${items.length} vídeo(s) seleccionados`);
        opt.value = "selection";
        scope.append(opt);
    }
    if (folder) {
        const info = lib.overview?.folders.find((f) => f.folder === folder);
        const opt = el("option", null, `Toda la carpeta «${folder}»${info ? ` (${info.count} vídeos)` : ""}`);
        opt.value = "folder";
        scope.append(opt);
    }
    body.append(field("A qué vídeos", scope));

    const add = el("input");
    add.type = "text";
    add.placeholder = "#psicologia #habitos";
    const remove = el("input");
    remove.type = "text";
    remove.placeholder = "#fyp #viral";
    body.append(field("Añadir", add, "Separados por espacios. Se normalizan como siempre (minúsculas, sin signos)."),
        field("Quitar", remove));

    const where = el("select");
    [["start", "Al principio: cuenta seguro (TikTok cuenta los cinco primeros)"],
        ["end", "Al final"]].forEach(([value, text]) => {
        const opt = el("option", null, text);
        opt.value = value;
        where.append(opt);
    });
    body.append(field("Dónde van los añadidos", where));

    const platforms = el("div", "checks");
    const boxes = PLATFORMS.map(([key, label]) => {
        const row = el("label", "check");
        const box = el("input");
        box.type = "checkbox";
        box.checked = true;
        box.value = key;
        row.append(box, document.createTextNode(` ${label}`));
        platforms.append(row);
        return box;
    });
    body.append(el("div", "block-field", "Plataformas"), platforms);
    const preview = el("div", "muted small");
    const refresh = () => {
        const a = tagList(add.value);
        const r = tagList(remove.value);
        preview.textContent = a.length || r.length
            ? [a.length && `Se añadirán ${a.join(" ")}`, r.length && `se quitarán ${r.join(" ")}`]
            .filter(Boolean).join(" y ") + "."
            : "Escribe qué hashtags añadir o quitar.";
    };
    add.addEventListener("input", refresh);
    remove.addEventListener("input", refresh);
    refresh();
    body.append(preview, el("div", "note note-info",
        "Los vídeos que se estén escribiendo no se tocan. Las colas que siguen a la biblioteca "
        + "reciben los hashtags nuevos si el vídeo aún no ha salido hacia Zernio."));

    const bar = el("div", "actions-bar");
    bar.append(
        button("Aplicar", "btn btn-primary", async () => {
            const payload = {
                add: tagList(add.value), remove: tagList(remove.value), position: where.value,
                platforms: boxes.filter((b) => b.checked).map((b) => b.value),
            };
            if (!payload.platforms.length) return toast("Elige al menos una plataforma", true);
            if (scope.value === "folder") payload.folder = folder;
            else payload.ids = items.map((i) => i.id);
            const res = await api("/api/library/tags", {method: "POST", body: JSON.stringify(payload)});
            closeModal();
            toast(res.message, res.writing.length > 0 && res.changed === 0);
            await reload();
            await state.refresh();
        }),
        button("Cancelar", "btn btn-ghost", closeModal),
    );
    body.append(bar);
    openModal("Hashtags en bloque", body);
    add.focus();
}

/* ------------------------------------------------------------------ */
/* From the library into a queue                                       */

/* ------------------------------------------------------------------ */

function queueName(q) {
    return accounts().length > 1 ? `${accountLabel(q.account)} · ${q.schedule.name}` : q.schedule.name;
}

function rewriteOption() {
    const ai = state.data?.health?.ai;
    const box = el("input");
    box.type = "checkbox";
    const row = el("label", "check");
    row.append(box, document.createTextNode(" Escribir textos nuevos para esta cola (Whisper + Ollama)"));
    row.append(el("span", "muted small block",
        "Sin marcar se usan los de la biblioteca tal cual y no se procesa nada."));
    row.hidden = !(ai && ai.enabled);
    return {row, box};
}

async function enqueue(queue, ids, rewrite) {
    const res = await api(`/api/schedules/${queue.schedule.id}/videos`, {
        method: "POST", body: JSON.stringify({library_ids: ids, rewrite}),
    });
    return res;
}

function showResult(res, title) {
    if (!res.problems.length) {
        closeModal();
        toast(res.message);
        return;
    }
    const body = el("div");
    body.append(el("p", null, res.message));
    const list = el("div", "warn-list");
    res.problems.forEach((p) => list.append(el("div", "note note-warn", p)));
    body.append(list);
    const bar = el("div", "actions-bar");
    bar.append(button("Cerrar", "btn btn-primary", closeModal));
    body.append(bar);
    openModal(title, body);
}

/* ------------------------------------------------------------------ */
/* Renaming a folder                                                   */

/* ------------------------------------------------------------------ */

/** What the server makes of a name (services/library.py: slug). */
function folderSlug(text) {
    return text.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
        .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 120).replace(/-+$/g, "");
}

function openRenameFolder(folder) {
    const info = lib.overview?.folders.find((f) => f.folder === folder);
    const body = el("div");
    const input = el("input");
    input.type = "text";
    input.value = folder;
    const preview = el("span", "muted small");
    const save = button("Renombrar", "btn btn-primary", async () => {
        const res = await api("/api/library/folders/rename", {
            method: "POST", body: JSON.stringify({old: folder, new: input.value}),
        });
        lib.folder = res.folder;
        lib.folders = lib.folders.map((f) => (f === folder ? res.folder : f));
        const scope = state.queueScope;
        if (scope.folders.includes(folder))
            state.queueScope = {...scope, folders: scope.folders.map((f) => (f === folder ? res.folder : f))};
        closeModal();
        toast(res.message);
        await reload();
        await state.refresh();
    });
    const update = () => {
        const name = folderSlug(input.value);
        const clash = name !== folder && lib.overview?.folders.some((f) => f.folder === name);
        preview.textContent = !name ? "Escribe alguna letra o número."
            : clash ? `Ya existe una carpeta «${name}».`
                : `Se llamará «${name}» · media/library/${name}/`;
        save.disabled = !name || name === folder || clash;
    };
    input.addEventListener("input", update);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !save.disabled) save.click();
    });
    body.append(field("Nuevo nombre", input, null), preview);
    body.append(el("div", "note note-info",
        `Se renombra la carpeta en disco y en la base de datos`
        + (info ? ` (${info.count} vídeo(s)${info.in_queues ? `, ${info.in_queues} en alguna cola` : ""})` : "")
        + ". Las colas siguen publicándolos igual, y los vídeos de esta serie que subas "
        + "después entrarán en la carpeta nueva."));
    const bar = el("div", "actions-bar");
    bar.append(save, button("Cancelar", "btn btn-ghost", closeModal));
    body.append(bar);
    update();
    openModal(`Renombrar «${folder}»`, body);
    input.focus();
    input.select();
}

/* ------------------------------------------------------------------ */
/* Moving videos to another folder                                     */

/* ------------------------------------------------------------------ */

/** One folder of the library, picked like in the sidebar: the labels table
 *  (✓ / ✕) and a search by name. With `create`, a name that is no folder yet
 *  offers to create it. `onChange(folder)` hears every pick (null = none);
 *  `onConfirm` is two clicks on a folder, or Enter in the search. */
function folderPicker({folders, create = false, footer = null, onChange, onConfirm}) {
    let scope = {folders: [], labels: [], match: "all", exclude: [], unlabeled: ""};
    let chosen = null;

    const body = el("div", "move-picker");
    const side = el("div", "lib-label-filter move-picker-labels");
    const main = el("div", "move-picker-main");
    const search = el("input");
    search.type = "search";
    search.placeholder = create ? "Buscar o escribir una carpeta nueva…" : "Buscar carpeta…";
    search.setAttribute("aria-label", "Buscar carpeta de destino");
    const list = el("div", "lib-folders move-picker-list");
    main.append(search, list);
    if (footer) main.append(footer);
    body.append(side, main);

    const named = () => {
        const needle = fold(search.value.trim());
        return folders.filter((f) => !needle || fold(f.folder).includes(needle));
    };
    const filtered = () => {
        const keep = scopeFolders(scope, folders);
        return named().filter((f) => !keep || keep.has(f.folder));
    };
    /** Folders with that name the labels hide: worth saying before creating another. */
    const hidden = () => (search.value.trim() ? named().length - filtered().length : 0);
    /** The slug typed, when it is a folder that does not exist yet. */
    const fresh = () => {
        const name = create ? folderSlug(search.value) : "";
        return name && !(lib.overview?.folders || []).some((f) => f.folder === name) ? name : "";
    };

    function drawLabels() {
        // Counted over the destinations: a folder left out of them does not count.
        const counts = new Map();
        folders.forEach((f) => f.labels.forEach((l) => counts.set(l, (counts.get(l) || 0) + 1)));
        const labels = (lib.overview?.labels || [])
            .map(({label}) => ({label, count: counts.get(label) || 0}))
            .filter(({label, count}) => count || scope.labels.includes(label) || scope.exclude.includes(label));
        side.hidden = !labels.length;
        if (!labels.length) return;
        const pick = (next) => {
            scope = {...scope, ...next};
            drawLabels();
            drawList();
        };
        const kept = scopeFolders(scope, folders);
        const result = el("div", "lib-lf-result small");
        result.hidden = !kept;
        if (kept) result.textContent = `→ ${kept.size} de ${folders.length} carpetas`;
        side.replaceChildren(...labelFilterRows({
            labels,
            bare: folders.filter((f) => !f.labels.length).length,
            scope,
            onChange: pick,
            onClear: () => pick({labels: [], exclude: [], unlabeled: ""}),
        }), labelMatch(scope, (match) => pick({match})), result);
    }

    function folderRow(f) {
        const row = el("button", `lib-folder${chosen === f.folder ? " is-active" : ""}`);
        row.type = "button";
        const name = el("span", "lib-folder-name", f.folder);
        if (f.labels.length) {
            const dots = el("span", "lib-folder-dots");
            f.labels.forEach((l) => {
                const dot = paint(el("span", "lib-folder-dot"), labelColor(l));
                dot.title = l;
                dots.append(dot);
            });
            name.prepend(dots);
        }
        row.append(name, el("span", "lib-folder-count", String(f.count)));
        row.title = f.labels.length ? `🏷️ ${f.labels.join(", ")}` : "";
        row.addEventListener("click", () => choose(f.folder));
        // Two clicks on it: that one, now.
        row.addEventListener("dblclick", () => {
            choose(f.folder);
            onConfirm?.();
        });
        return row;
    }

    function drawList() {
        const shown = filtered();
        const name = fresh();
        const rows = shown.map(folderRow);
        if (name) {
            const make = el("button", `lib-folder move-picker-new${chosen === name ? " is-active" : ""}`);
            make.type = "button";
            make.append(el("span", "lib-folder-name", `＋ Crear «${name}»`),
                el("span", "lib-folder-count", "nueva"));
            make.addEventListener("click", () => choose(name));
            rows.push(make);
        }
        const away = hidden();
        if (away) {
            const note = el("div", "lib-folders-empty muted small",
                `${away} carpeta(s) con ese nombre no tienen las etiquetas elegidas. `);
            const clear = el("button", "lib-lf-clear", "Quitar filtro");
            clear.type = "button";
            clear.addEventListener("click", () => {
                scope = {...scope, labels: [], exclude: [], unlabeled: ""};
                drawLabels();
                drawList();
            });
            note.append(clear);
            rows.push(note);
        } else if (!rows.length) {
            rows.push(el("div", "lib-folders-empty muted small", !search.value.trim()
                ? (folders.length ? "Ninguna carpeta tiene las etiquetas elegidas."
                    : create ? "No hay otras carpetas: escribe un nombre para crear una."
                        : "La biblioteca no tiene carpetas todavía.")
                : create ? "Ninguna carpeta se llama así. Escribe alguna letra o número para crear una."
                    : "Ninguna carpeta se llama así."));
        }
        list.replaceChildren(...rows);
        // The one picked fell out of the filter: nothing is picked.
        if (chosen && !shown.some((f) => f.folder === chosen) && chosen !== name) choose(null);
        else onChange(chosen);
    }

    function choose(folder) {
        chosen = folder;
        list.querySelectorAll(".lib-folder").forEach((row) => row.classList.remove("is-active"));
        drawList();
    }

    search.addEventListener("input", () => {
        // A name only one folder matches picks it. «Crear» is never picked on
        // its own: a typo would make a folder.
        const shown = filtered();
        chosen = shown.length === 1 && !fresh() ? shown[0].folder
            : shown.some((f) => f.folder === chosen) ? chosen : null;
        drawList();
    });
    search.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        // Enter on a new name that nothing else matches: that is asking for it.
        if (!chosen && fresh() && !filtered().length && !hidden()) choose(fresh());
        onConfirm?.();
    });

    drawLabels();
    drawList();
    return {body, side, search};
}

function openMoveItems(items) {
    const from = new Set(items.map((i) => i.folder));
    let chosen = null;
    const preview = el("div", "muted small move-picker-preview");
    const save = button(`Mover ${items.length}`, "btn btn-primary", async () => {
        if (!chosen) return;
        const res = await api("/api/library/move", {
            method: "POST", body: JSON.stringify({folder: chosen, ids: items.map((i) => i.id)}),
        });
        items.forEach((i) => lib.selected.delete(i.id));
        await reload();
        await state.refresh();
        showResult(res, `Mover a «${res.folder}»`);
    });
    const picker = folderPicker({
        // Moving out of one folder: that one is no destination.
        folders: (lib.overview?.folders || []).filter((f) => !(from.size === 1 && from.has(f.folder))),
        create: true,
        footer: preview,
        onChange: (folder) => {
            chosen = folder;
            const already = chosen ? items.filter((i) => i.folder === chosen).length : 0;
            const isNew = chosen && !(lib.overview?.folders || []).some((f) => f.folder === chosen);
            preview.textContent = !chosen ? "Elige la carpeta de destino."
                : `→ media/library/${chosen}/${isNew ? " (nueva)" : ""}`
                + (already ? ` · ${already} ya están en ella y se quedan igual` : "");
            save.disabled = !chosen || already === items.length;
        },
        onConfirm: () => {
            if (!save.disabled) save.click();
        },
    });

    const queued = items.filter((i) => i.queued.some((q) => !["PUBLISHED", "FAILED"].includes(q.status))).length;
    const note = el("div", "note note-info",
        "Se mueve cada vídeo con su .txt y su portada, en disco y en la base de datos."
        + (queued ? ` ${queued} están en alguna cola: siguen en ella y se publican igual desde la carpeta nueva.` : "")
        + " Los que subas después de la misma serie seguirán entrando en la carpeta de siempre.");
    const bar = el("div", "actions-bar");
    bar.append(save, button("Cancelar", "btn btn-ghost", closeModal));
    const wrap = el("div");
    wrap.append(picker.body, note, bar);
    openModal(items.length === 1 ? `Mover «${items[0].original_name}»` : `Mover ${items.length} vídeos`, wrap,
        {wide: !picker.side.hidden});
    picker.search.focus();
}

/* ------------------------------------------------------------------ */
/* Labels of a folder                                                  */

/* ------------------------------------------------------------------ */

function openFolderLabels(folder) {
    let labels = [...labelsOf(folder)];
    /* label -> new colour, or null for automatic: saved with «Guardar». */
    const recolor = new Map();
    const colorOf = (label) => (recolor.has(label) ? recolor.get(label) || autoColor(label) : labelColor(label));
    const body = el("div");
    const box = el("div", "chip-input");
    const input = el("input");
    input.type = "text";
    input.placeholder = "serie, terminada, para revisar… (Enter para añadir)";
    input.setAttribute("aria-label", "Añadir etiqueta");
    input.maxLength = 32;
    box.append(input);
    box.addEventListener("click", () => input.focus());
    const suggest = el("div", "folder-label-suggest");
    const clean = (text) => text.replace(/\s+/g, " ").trim().replace(/^#+/, "").trim().toLowerCase();
    const draw = () => {
        box.querySelectorAll(".folder-label").forEach((n) => n.remove());
        labels.forEach((label) => {
            const chip = labelChip(label, "span", colorOf(label));
            const x = el("button", "folder-label-x", "×");
            x.type = "button";
            x.title = `Quitar «${label}»`;
            x.addEventListener("click", (e) => {
                e.stopPropagation();
                labels = labels.filter((l) => l !== label);
                draw();
                input.focus();
            });
            chip.append(x);
            box.insertBefore(chip, input);
        });
        const others = (lib.overview?.labels || []).map((l) => l.label).filter((l) => !labels.includes(l));
        suggest.replaceChildren(...(others.length ? [el("span", "muted small", "Ya usadas: ")] : []),
            ...others.map((label) => {
                const chip = labelChip(label, "button");
                chip.type = "button";
                chip.title = `Añadir «${label}»`;
                chip.addEventListener("click", () => add(label));
                return chip;
            }));
        colors.replaceChildren(...labels.map(colorRow));
        colorsField.hidden = !labels.length;
    };
    /* One row per label: its chip, the palette, any colour, and back to automatic. */
    const colorRow = (label) => {
        const current = colorOf(label);
        const row = el("div", "label-color-row");
        row.append(labelChip(label, "span", current));
        const swatches = el("div", "label-swatches");
        const pick = (color) => {
            recolor.set(label, color);
            draw();
        };
        LABEL_PALETTE.forEach((color) => {
            const swatch = paint(el("button", `label-swatch${color === current ? " is-active" : ""}`), color);
            swatch.type = "button";
            swatch.title = color;
            swatch.setAttribute("aria-label", `Color ${color} para «${label}»`);
            swatch.addEventListener("click", () => pick(color));
            swatches.append(swatch);
        });
        const custom = el("input", "label-swatch-custom");
        custom.type = "color";
        custom.value = current;
        custom.title = "Otro color";
        custom.setAttribute("aria-label", `Otro color para «${label}»`);
        custom.addEventListener("change", () => pick(custom.value));
        swatches.append(custom);
        const picked = recolor.has(label) ? recolor.get(label) : labelColors.get(label);
        if (picked) {
            const auto = el("button", "btn btn-ghost btn-sm", "Automático");
            auto.type = "button";
            auto.title = "El color que le toca por su nombre";
            auto.addEventListener("click", () => pick(null));
            swatches.append(auto);
        }
        row.append(swatches);
        return row;
    };
    const add = (raw) => {
        raw.split(",").map(clean).filter(Boolean).forEach((label) => {
            if (!labels.includes(label) && labels.length < 12) labels.push(label.slice(0, 32));
        });
        draw();
        input.focus();
    };
    input.addEventListener("keydown", (e) => {
        if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
            e.preventDefault();
            add(input.value);
            input.value = "";
        } else if (e.key === "Backspace" && !input.value && labels.length) {
            labels.pop();
            draw();
        } else if (e.key === "Enter" && !input.value.trim()) {
            e.preventDefault();
            save.click();
        }
    });
    const save = button("Guardar", "btn btn-primary", async () => {
        if (input.value.trim()) add(input.value);
        input.value = "";
        await api("/api/library/folders/labels", {
            method: "POST", body: JSON.stringify({folder, labels}),
        });
        for (const [label, color] of recolor) {
            if (!labels.includes(label) || color === (labelColors.get(label) || null)) continue;
            await api("/api/library/labels/color", {method: "POST", body: JSON.stringify({label, color})});
            if (color) labelColors.set(label, color);
            else labelColors.delete(label);
        }
        lib.drawn = "";
        closeModal();
        toast(labels.length ? `«${folder}»: ${labels.join(", ")}` : `«${folder}» ya no tiene etiquetas`);
        await reload();
    });
    const colors = el("div", "label-colors");
    const colorsField = field("Colores", colors, "El color es de la etiqueta: cambia en todas las "
        + "carpetas que la llevan.");
    body.append(field("Etiquetas", box, "Sirven para filtrar las carpetas en la barra de la izquierda "
        + "y para añadir varias carpetas a una cola de una vez."), suggest, colorsField);
    const bar = el("div", "actions-bar");
    bar.append(save, button("Cancelar", "btn btn-ghost", closeModal));
    body.append(bar);
    draw();
    openModal(`Etiquetas de «${folder}»`, body);
    input.focus();
}

/** Lowercase, no accents, words joined by "-": how a folder and a queue name are compared. */
function slug(text) {
    return (text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
        .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

/** Library → queue(s), for the videos selected in the library page (or one row's «Añadir…»).
 *  Several queues at once — the same reel to its TikTok and its YouTube queue — and, optionally,
 *  «fuera de turno», on top of each queue's rhythm, which keeps its hours: out right now, or
 *  handed to Zernio for a day and hour. Nothing is ticked at first, and a summary of the
 *  accounts and platforms asks for confirmation before anything is sent. */
/* The filters of «Añadir a una cola», remembered apart from the page's own. */
const ADD_FILTER_KEY = "tts.addQueueFilter";

function storedAddFilter() {
    try {
        const v = JSON.parse(localStorage.getItem(ADD_FILTER_KEY) || "null") || {};
        return {
            platform: ["tiktok", "youtube"].includes(v.platform) ? v.platform : "",
            accounts: Array.isArray(v.accounts) ? v.accounts.filter(Number.isInteger) : null,
            hideTaken: v.hideTaken !== false,
        };
    } catch {
        return {platform: "", accounts: null, hideTaken: true};
    }
}

function openAddToQueue(items) {
    // Every queue in sight; the modal's own filters narrow it below.
    const all = everyQueue().filter((q) => !q.schedule.hidden);
    const body = el("div");
    if (!all.length) {
        body.append(el("div", "note note-warn", everyQueue().length
            ? "Todas las colas están ocultas: muestra alguna desde Programación o el Resumen."
            : "No hay ninguna cola: conecta una cuenta primero."));
        openModal("Añadir a una cola", body);
        return;
    }
    body.append(el("p", "muted small",
        items.length === 1
            ? `«${items[0].original_name}» entra con los textos de la plataforma de cada cola que marques.`
            : `${items.length} vídeos entran con los textos de la plataforma de cada cola que marques.`));

    // A queue whose account already has every one of these videos has nothing to take.
    const taken = (q) => items.every((i) => i.queued.some((u) => u.account_id === q.account.id));
    const where = (q) => {
        const hit = items.length === 1 && items[0].queued.find((u) => u.account_id === q.account.id);
        return hit ? `ya está en «${hit.queue_name}»` : "ya están todos en esta cuenta";
    };
    const list = el("div", "add-queues");
    const boxes = all.map((q) => {
        const box = el("input");
        box.type = "checkbox";
        box.disabled = taken(q);
        const row = el("label", "check");
        const platform = q.account.platform || "tiktok";
        row.append(box, el("span", `lib-plat ${platform}`, PLATFORM_LABEL[platform] || platform),
            document.createTextNode(` ${queueName(q)}`));
        if (box.disabled) row.append(el("span", "muted small", ` · ${where(q)}`));
        list.append(row);
        box.addEventListener("change", sync);
        return {q, box, row};
    });

    /* Filters: platform, accounts (several; none = all), a search, and the
       queues that already have the video(s) out of the way. A ticked queue
       never hides: what gets added is always on screen. */
    const saved = storedAddFilter();
    const known = (ids) => ids.filter((id) => accounts().some((e) => e.account.id === id));
    const filter = {
        platform: saved.platform,
        accounts: known(saved.accounts ?? lib.accounts),
        q: "",
        hideTaken: saved.hideTaken,
    };
    const remember = () => {
        try {
            localStorage.setItem(ADD_FILTER_KEY, JSON.stringify(
                {platform: filter.platform, accounts: filter.accounts, hideTaken: filter.hideTaken}));
        } catch {
            /* a private window: not remembered */
        }
    };
    const owners = [...new Map(all.map((q) => [q.account.id, q.account])).values()];
    const platforms = [...new Set(owners.map((a) => a.platform || "tiktok"))];
    const matches = (q) => {
        const platform = q.account.platform || "tiktok";
        if (filter.platform && platform !== filter.platform) return false;
        if (filter.accounts.length && !filter.accounts.includes(q.account.id)) return false;
        if (filter.hideTaken && taken(q)) return false;
        const text = slug(filter.q);
        return !text || slug(`${accountLabel(q.account)} ${q.schedule.name}`).includes(text);
    };
    const chip = (label, active, onClick, extra = []) => {
        const b = el("button", `chip${active ? " is-active" : ""}`);
        b.type = "button";
        b.setAttribute("aria-pressed", String(active));
        b.append(...extra, document.createTextNode(label));
        b.addEventListener("click", onClick);
        return b;
    };
    const filters = el("div", "add-filters");
    const platformRow = el("div", "chips add-chips");
    const accountRow = el("div", "chips add-chips");
    const search = el("input");
    search.type = "search";
    search.placeholder = "Buscar cola o cuenta…";
    search.setAttribute("aria-label", "Buscar cola o cuenta");
    search.addEventListener("input", () => {
        filter.q = search.value;
        apply();
    });
    const takenBox = el("input");
    takenBox.type = "checkbox";
    takenBox.checked = filter.hideTaken;
    const takenRow = el("label", "check");
    const takenCount = boxes.filter((b) => b.box.disabled).length;
    takenRow.append(takenBox, document.createTextNode(` Ocultar las que ya lo tienen (${takenCount})`));
    takenRow.hidden = !takenCount;
    takenBox.addEventListener("change", () => {
        filter.hideTaken = takenBox.checked;
        remember();
        apply();
    });
    const count = el("span", "muted small");
    const tickShown = button("Marcar las visibles", "btn btn-ghost btn-sm", () => {
        boxes.forEach((b) => {
            if (!b.row.hidden && !b.box.disabled) b.box.checked = true;
        });
        sync();
    });
    const untick = button("Desmarcar todas", "btn btn-ghost btn-sm", () => {
        boxes.forEach((b) => {
            b.box.checked = false;
        });
        apply();
        sync();
    });
    const tools = el("div", "add-tools");
    tools.append(search, takenRow, count, tickShown, untick);
    filters.append(...(platforms.length > 1 ? [platformRow] : []), ...(owners.length > 1 ? [accountRow] : []), tools);

    function drawChips() {
        platformRow.replaceChildren(
            chip("Todas", !filter.platform, () => setPlatform("")),
            ...platforms.map((p) => chip("", filter.platform === p, () => setPlatform(p),
                [el("span", `lib-plat ${p}`, PLATFORM_LABEL[p] || p)])),
        );
        accountRow.replaceChildren(
            chip("Todas las cuentas", !filter.accounts.length, () => setAccounts([])),
            ...owners.filter((a) => !filter.platform || (a.platform || "tiktok") === filter.platform)
                .map((a) => {
                    const on = filter.accounts.includes(a.id);
                    return chip(` ${accountLabel(a)}`, on, () => setAccounts(on
                            ? filter.accounts.filter((id) => id !== a.id)
                            : [...filter.accounts, a.id]),
                        [el("span", `lib-plat ${a.platform || "tiktok"}`, PLATFORM_LABEL[a.platform || "tiktok"])]);
                }),
        );
    }

    function setPlatform(p) {
        filter.platform = p;
        // An account of the other platform would leave nothing to show.
        filter.accounts = filter.accounts.filter((id) =>
            !p || (owners.find((a) => a.id === id)?.platform || "tiktok") === p);
        remember();
        apply();
    }

    function setAccounts(ids) {
        filter.accounts = ids.length >= owners.length ? [] : ids;
        remember();
        apply();
    }

    function apply() {
        drawChips();
        let shown = 0;
        boxes.forEach((b) => {
            b.row.hidden = !b.box.checked && !matches(b.q);
            if (!b.row.hidden) shown += 1;
        });
        count.textContent = `${shown} de ${boxes.length} colas`;
        empty.hidden = shown > 0;
    }

    const empty = el("div", "muted small", "Ninguna cola coincide con los filtros.");
    // Not field(): its <label> would swallow the clicks meant for the boxes.
    const listField = el("div", "block-field");
    listField.append(document.createTextNode("Colas"), filters, list, empty);
    body.append(listField);

    /* When: in its turn, out right now, or handed to Zernio for a day and hour.
       Both of the last two are «fuera de turno»: on top of the queue's rhythm. */
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    let mode = "queue";
    let at = {day: "", time: ""};
    const picker = dateTimePicker({
        placeholder: "Elegir día y hora",
        lead: "Se publica el",
        now: () => nowIn(zone),
        onChange: (next) => {
            at = next;
            picker.set(at);
            sync();
        },
    });
    const pickRow = el("div", "add-when-pick");
    pickRow.append(picker.node, el("span", "muted small", `Hora de este ordenador (${zone}).`));
    const whenField = el("div", "block-field add-when");
    whenField.append(document.createTextNode("Cuándo"));
    [
        ["queue", "En su turno, con el ritmo de cada cola"],
        ["now", "⚡ Publicar ya, fuera de turno"],
        ["at", "📅 Programar día y hora en Zernio, fuera de turno"],
    ].forEach(([value, label]) => {
        const radio = el("input");
        radio.type = "radio";
        radio.name = "add-when";
        radio.value = value;
        radio.checked = value === mode;
        radio.addEventListener("change", () => {
            mode = value;
            sync();
            if (mode === "at" && !at.day) picker.open(true);
        });
        const row = el("label", "check");
        row.append(radio, document.createTextNode(` ${label}`));
        whenField.append(row);
        if (value === "at") whenField.append(pickRow);
    });
    const whenHint = el("div", "muted small");
    whenField.append(whenHint);
    body.append(whenField);
    const {row, box} = rewriteOption();
    body.append(row);

    /** The chosen instant as a Date (read in this computer's zone), or null. */
    const instant = () => at.day ? new Date(`${at.day}T${at.time || "00:00"}:00`) : null;
    const whenText = () => mode === "now" ? "⚡ ya mismo, fuera de turno"
        : mode === "at" ? `📅 el ${longLabel(at.day, at.time)}, programado en Zernio, fuera de turno`
            : "en su turno, con el ritmo de cada cola";

    const go = button("", "btn btn-primary", () => confirm());

    async function send(targets) {
        const messages = [], problems = [];
        const when = mode === "at" ? instant().toISOString() : null;
        for (const target of targets) {
            try {
                const res = await api(`/api/schedules/${target.schedule.id}/videos`, {
                    method: "POST",
                    body: JSON.stringify({
                        library_ids: items.map((i) => i.id), rewrite: box.checked,
                        publish_now: mode === "now", publish_at: when,
                    }),
                });
                messages.push(res.message);
                problems.push(...res.problems);
            } catch (err) {
                problems.push(`«${target.schedule.name}»: ${err.message}`);
            }
        }
        lib.selected.clear();
        await state.refresh();
        await reload();
        showResult({message: messages.join(" · ") || "No se añadió nada.", problems},
            mode === "now" ? "Publicar ya" : mode === "at" ? "Programar en Zernio" : "Añadir a una cola");
    }

    /** The second step: what goes where, by account and platform, before anything is sent. */
    function confirm() {
        const targets = boxes.filter((b) => b.box.checked).map((b) => b.q);
        if (mode === "at" && !(instant() > new Date())) {
            sync();
            return;
        }
        const sheet = el("div", "add-confirm");
        sheet.append(el("p", null, items.length === 1
            ? `«${items[0].original_name}» se publicará ${whenText()} en:`
            : `${items.length} vídeos se publicarán ${whenText()} en:`));

        const byAccount = new Map();
        targets.forEach((q) => {
            const list = byAccount.get(q.account.id) || {account: q.account, queues: []};
            list.queues.push(q);
            byAccount.set(q.account.id, list);
        });
        const table = el("div", "add-confirm-list");
        [...byAccount.values()].forEach(({account, queues: owned}) => {
            const platform = account.platform || "tiktok";
            const line = el("div", "add-confirm-row");
            line.append(el("span", `lib-plat ${platform}`, PLATFORM_LABEL[platform] || platform),
                el("strong", null, ` ${accountLabel(account)}`),
                el("span", "muted", ` · ${owned.map((q) => `«${q.schedule.name}»`).join(", ")}`));
            table.append(line);
        });
        sheet.append(table);

        const names = [...new Set([...byAccount.values()].map(({account}) => PLATFORM_LABEL[account.platform || "tiktok"]))];
        sheet.append(el("p", "muted small",
            `${byAccount.size} cuenta(s) · ${names.join(" y ")} · ${items.length * targets.length} publicación(es) en total.`
            + (box.checked ? " Con textos nuevos escritos para cada cola." : "")));
        if (items.length > 1 && items.length <= 12) {
            const videos = el("ul", "add-confirm-videos muted small");
            items.forEach((i) => videos.append(el("li", null, i.original_name)));
            sheet.append(videos);
        }

        const yes = button(mode === "now" ? "⚡ Sí, publicar ya" : mode === "at" ? "📅 Sí, programar" : "Sí, añadir",
            "btn btn-primary", async () => {
                yes.disabled = true;
                back.disabled = true;
                await send(targets);
            });
        const back = button("Volver", "btn btn-ghost", () => openModal(title, body));
        const bar = el("div", "actions-bar");
        bar.append(yes, back);
        sheet.append(bar);
        openModal("¿Confirmas?", sheet);
    }

    function sync() {
        const n = boxes.filter((b) => b.box.checked).length;
        const count = n === 1 ? "1 cola" : `${n} colas`;
        pickRow.hidden = mode !== "at";
        const gone = mode === "at" && at.day && !(instant() > new Date());
        whenHint.classList.toggle("is-bad", Boolean(gone));
        whenHint.textContent = {
            queue: "Entra al final de cada cola y sale a su hora.",
            now: "Sale en cuanto esté listo, sin esperar su hora. El resto de la cola no se mueve (ni su "
                + "ritmo, ni el cupo del día, ni la separación entre vídeos) y queda registrado en ella, "
                + "así que no podrá volver a entrar.",
            at: gone ? "Esa hora ya pasó: elige una más tarde."
                : "Se sube a Zernio enseguida y Zernio lo publica a esa hora, aunque cierres la app. "
                + "Como «Publicar ya», no mueve el ritmo de la cola ni ocupa su cupo, y se puede "
                + "recuperar de Zernio antes de su hora.",
        }[mode];
        go.textContent = n ? `Revisar: ${mode === "now" ? "⚡ publicar ya en" : mode === "at"
            ? "📅 programar en" : "añadir a"} ${count}…` : "Marca al menos una cola";
        go.disabled = !n || (mode === "at" && (!at.day || gone));
    }

    apply();
    sync();
    const bar = el("div", "actions-bar");
    bar.append(go, button("Cancelar", "btn btn-ghost", closeModal));
    body.append(bar);
    const title = items.length === 1 ? `Añadir «${items[0].original_name}»` : "Añadir a una cola";
    openModal(title, body);
}

/** Queue → library: the picker of the Cola page. */
export async function openLibraryPicker(entry, queue) {
    const overview = await api(`/api/library?account_id=${entry.account.id}`);
    const body = el("div", "lib-picker");
    const picked = new Set();
    const platform = entry.account.platform || "tiktok";

    if (!overview.total) {
        body.append(el("div", "note note-info", "La biblioteca está vacía. Sube primero tus vídeos en Biblioteca."));
        const bar = el("div", "actions-bar");
        bar.append(link("Ir a la biblioteca", "#/biblioteca", "btn btn-primary", false));
        body.append(bar);
        openModal(`Añadir a «${queue.schedule.name}»`, body);
        bar.querySelector("a").addEventListener("click", closeModal);
        return;
    }

    body.append(el("p", "muted small",
        `Entran con sus textos de ${PLATFORM_LABEL[platform] || platform}, ya escritos en la biblioteca. `
        + "Los que ya están en esta cuenta aparecen desactivados."));
    rememberColors(overview);
    const top = el("div", "toolbar");
    // Several folders (or all), narrowed by labels: «Todas» + serie + terminada.
    const choices = overview.folders.map((f) => ({
        folder: f.folder, labels: f.labels, count: f.available, total: f.count,
        note: f.available ? `${f.available} de ${f.count}` : `todos ya (${f.count})`,
    }));
    const sum = (fs, key) => fs.reduce((n, f) => n + f[key], 0);
    let scope = {folders: [], labels: [], match: "all", exclude: [], unlabeled: ""};
    const picker = scopePicker({
        negate: true,
        unit: "disponible(s)",
        allNote: (fs) => (sum(fs, "count") ? `${sum(fs, "count")} de ${sum(fs, "total")}`
            : `todos ya (${sum(fs, "total")})`),
        onChange: (next) => {
            scope = next;
            search.value = "";
            loadList().catch((err) => toast(err.message, true));
        },
    });
    // Open where there is something left to add.
    const firstFree = overview.folders.find((f) => f.available > 0);
    if (firstFree) scope = {...scope, folders: [firstFree.folder]};
    picker.update(choices, scope);
    const folder = picker.node;
    const several = () => scope.folders.length !== 1 || scope.labels.length > 0
        || scope.exclude.length > 0 || !!scope.unlabeled;
    const search = el("input");
    search.type = "search";
    search.placeholder = "Buscar en toda la biblioteca…";
    top.append(folder, search);
    body.append(top);

    const allRow = el("label", "check lib-pick-all");
    const allBox = el("input");
    allBox.type = "checkbox";
    allRow.append(allBox, el("span", null, " Todos los disponibles"));
    body.append(allRow);
    const list = el("div", "lib-pick-list");
    body.append(list);

    const {row: rewriteRow, box: rewriteBox} = rewriteOption();
    body.append(rewriteRow);
    const bar = el("div", "actions-bar");
    const submit = button("Añadir", "btn btn-primary", async () => {
        const res = await enqueue(queue, [...picked], rewriteBox.checked);
        await state.refresh();
        lib.loadedAt = 0;
        showResult(res, `Añadir a «${queue.schedule.name}»`);
    });
    bar.append(submit, button("Cancelar", "btn btn-ghost", closeModal));
    body.append(bar);

    let shown = [];
    const refreshButton = () => {
        submit.disabled = picked.size === 0;
        submit.textContent = picked.size ? `Añadir ${picked.size} a «${queue.schedule.name}»` : "Añadir";
        const free = shown.filter((i) => !i.in_account && !i.preflight_errors.length);
        allBox.checked = free.length > 0 && free.every((i) => picked.has(i.id));
        allBox.disabled = free.length === 0;
        allRow.lastChild.textContent = ` Todos los disponibles (${free.length})`;
    };
    const drawList = () => {
        list.replaceChildren(...(shown.length ? shown.map((item) => {
            const row = el("label", "lib-pick");
            const box = el("input");
            box.type = "checkbox";
            const blocked = item.in_account || item.preflight_errors.length > 0;
            box.disabled = blocked;
            box.checked = picked.has(item.id);
            box.addEventListener("change", () => {
                if (box.checked) picked.add(item.id);
                else picked.delete(item.id);
                refreshButton();
            });
            const main = el("span", "lib-pick-main");
            main.append(el("span", "lib-pick-name", item.original_name));
            const t = item.texts[platform] || item.texts.tiktok;
            const why = item.in_account
                ? `ya en ${item.queued.filter((q) => q.account_id === entry.account.id).map((q) => `«${q.queue_name}»`).join(", ")}`
                : item.preflight_errors.length ? "no pasa la validación"
                    : WRITING.includes(item.caption_status) ? "✍️ textos escribiéndose (llegarán solos)"
                        : (platform === "youtube" ? t.title : (t.description || "").split("\n")[0]) || "sin texto";
            main.append(el("span", "muted small",
                (search.value.trim() || several() ? `📁 ${item.folder} · ` : "") + why));
            row.append(box, main);
            if (blocked) row.classList.add("is-done");
            return row;
        }) : [el("div", "empty", "Nada que mostrar.")]));
        refreshButton();
    };
    const loadList = async () => {
        const params = new URLSearchParams({account_id: String(entry.account.id), limit: "2000"});
        if (search.value.trim()) params.set("q", search.value.trim());
        else appendScope(params, scope);
        list.replaceChildren(el("div", "empty", "Cargando…"));
        shown = (await api(`/api/library/items?${params}`)).items;
        drawList();
    };
    allBox.addEventListener("change", () => {
        shown.filter((i) => !i.in_account && !i.preflight_errors.length)
            .forEach((i) => (allBox.checked ? picked.add(i.id) : picked.delete(i.id)));
        drawList();
    });
    let timer = null;
    search.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => loadList().catch((err) => toast(err.message, true)), 250);
    });

    openModal(`Añadir a «${queue.schedule.name}» desde la biblioteca`, body, {wide: true});
    await loadList();
}

/* ------------------------------------------------------------------ */
/* Uploading folders                                                   */

/* ------------------------------------------------------------------ */

/** What was dropped as {file, path}: a folder gives its own files only, its
 subfolders are not walked, unless it has no video, .txt or cover of its own:
 then its subfolders (their own files) are what was meant. */
async function droppedFiles(dataTransfer) {
    const entries = [...(dataTransfer.items || [])]
        .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
        .filter(Boolean);
    if (!entries.length) return [...(dataTransfer.files || [])].map((file) => ({file, path: file.name}));
    const fileOf = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));
    const children = async (dir) => {
        const reader = dir.createReader();
        const all = [];
        for (; ;) {
            const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
            if (!batch.length) return all;
            all.push(...batch);
        }
    };
    const out = [];
    for (const entry of entries) {
        if (entry.isFile) {
            out.push({file: await fileOf(entry), path: entry.name});
            continue;
        }
        if (!entry.isDirectory) continue;
        const inside = await children(entry);
        const own = inside.filter((c) => c.isFile);
        for (const child of own) out.push({file: await fileOf(child), path: `${entry.name}/${child.name}`});
        if (own.some((c) => usable(c.name))) continue;
        for (const sub of inside.filter((c) => c.isDirectory && !c.name.startsWith("."))) {
            for (const child of (await children(sub)).filter((c) => c.isFile))
                out.push({file: await fileOf(child), path: `${entry.name}/${sub.name}/${child.name}`});
        }
    }
    return out;
}

/** A video, a .txt or a cover, and not hidden: what an upload takes. */
function usable(name) {
    return !name.startsWith(".") && (VIDEO_RE.test(name) || TEXT_RE.test(name) || COVER_RE.test(name));
}

function stemKey(path) {
    const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    const name = path.slice(path.lastIndexOf("/") + 1).toLowerCase();
    return `${dir}/${name.replace(/\.caption\.txt$/, "").replace(/\.[^.]+$/, "").replace(/_portada$/, "")}`;
}

/** One job per video (with its .txt and its cover), then the loose .txt and covers together.
 Only the files directly inside a chosen folder count (`folder/file`, or a
 loose `file`): subfolders, hidden entries and any other type are ignored. */
function plan(files) {
    const clean = files.filter(({file, path}) => {
        const parts = path.split("/");
        return parts.length <= 2 && !parts.some((part) => part.startsWith(".")) && usable(file.name);
    });
    const texts = new Map();
    const covers = new Map();
    clean.filter(({file}) => TEXT_RE.test(file.name)).forEach((f) => texts.set(stemKey(f.path), f));
    clean.filter(({file}) => COVER_RE.test(file.name)).forEach((f) => covers.set(stemKey(f.path), f));
    const jobs = clean.filter(({file}) => VIDEO_RE.test(file.name)).map((video) => {
        const key = stemKey(video.path);
        const extras = [texts.get(key), covers.get(key)].filter(Boolean);
        texts.delete(key);
        covers.delete(key);
        const top = video.path.includes("/") ? video.path.split("/").slice(-2)[0] : "";
        return {files: [video, ...extras], folder: top};
    });
    if (texts.size || covers.size) jobs.push({files: [...texts.values(), ...covers.values()], folder: ""});
    return {jobs, ignored: files.length - clean.length};
}

/** What was picked or dropped, by folder: name -> [{file, path}], loose files
 under "". A folder with nothing of its own lends its subfolders, one level
 down (their paths lose it): picking the folder that holds several is picking
 all of them. */
function groupFolders(files) {
    const own = new Set(files.filter(({file, path}) => path.split("/").length === 2 && usable(file.name)
        && !path.startsWith(".")).map(({path}) => path.split("/")[0]));
    const groups = new Map();
    for (const {file, path} of files) {
        const parts = path.split("/");
        const kept = parts.length > 2 && !own.has(parts[0]) ? parts.slice(1).join("/") : path;
        const name = kept.includes("/") ? kept.split("/")[0] : "";
        if (!groups.has(name)) groups.set(name, []);
        groups.get(name).push({file, path: kept});
    }
    return groups;
}

/** One folder (or loose files) goes straight up, as always; several ask where to first. */
function received(files) {
    const groups = groupFolders(files);
    const folders = [...groups.values()].filter((list) => plan(list).jobs.length);
    if (folders.length > 1) return openBatchUpload(files);
    return upload([...groups.values()].flat());
}

/** Where the server files a video when no folder was picked (services/library.py: destination). */
function destinationOf(name, hint) {
    const series = name.replace(/\.[^.]+$/, "").match(SERIES_RE);
    const folder = (series && folderSlug(series[1])) || folderSlug(hint) || DEFAULT_FOLDER;
    return lib.overview?.aliases?.[folder] || folder;
}

/** Several folders at once: the list (more can be added, or dropped on it) and
 *  where they go: each to its own folder as always, all into one that
 *  exists, or all into a new one. `pick` opens the folder dialog right away. */
function openBatchUpload(files = [], {pick = false} = {}) {
    const batch = new Map();
    let mode = "own";
    let existing = null;
    const known = (name) => (lib.overview?.folders || []).some((f) => f.folder === name);

    const input = el("input");
    input.type = "file";
    input.multiple = true;
    input.webkitdirectory = true;
    input.hidden = true;
    const list = el("div", "lib-batch-list");
    const nameInput = el("input");
    nameInput.type = "text";
    nameInput.placeholder = "Nombre de la carpeta nueva";
    const namePreview = el("div", "muted small move-picker-preview");
    const pickerPreview = el("div", "muted small move-picker-preview");
    const nameBox = el("div");
    nameBox.append(nameInput, namePreview);
    const note = el("div", "note note-info",
        "Los vídeos que ya estén en la biblioteca no se suben otra vez ni cambian de carpeta "
        + "(para eso, «Mover…» en la lista). Los que subas después de la misma serie seguirán "
        + "entrando en la carpeta de su serie.");

    const into = () => (mode === "existing" ? existing : mode === "new" ? folderSlug(nameInput.value) : "");
    const go = button("Subir", "btn btn-primary", async () => {
        if (lib.uploading) return toast("Ya hay una subida en marcha", true);
        const folder = into();
        const picked = [...batch.values()].flat();
        closeModal();
        await upload(picked, {into: folder});
    });
    // Assigned after it first draws, which is before the dialog has anything to draw.
    let picker = null;
    picker = folderPicker({
        folders: lib.overview?.folders || [],
        footer: pickerPreview,
        onChange: (folder) => {
            existing = folder;
            if (picker) draw();
        },
        onConfirm: () => {
            if (!go.disabled) go.click();
        },
    });

    function add(picked) {
        const groups = [...groupFolders(picked)].filter(([, list]) => plan(list).jobs.length);
        if (!groups.length) return toast("No hay vídeos, .txt ni portadas en lo que has elegido", true);
        // The same folder again: read again, nothing doubled.
        groups.forEach(([name, list]) => {
            const merged = new Map((batch.get(name) || []).map((f) => [f.path, f]));
            list.forEach((f) => merged.set(f.path, f));
            batch.set(name, [...merged.values()]);
        });
        draw();
    }

    function folderRow(name, files) {
        const sent = plan(files).jobs.flatMap((job) => job.files);
        const videos = sent.filter(({file}) => VIDEO_RE.test(file.name));
        const texts = sent.filter(({file}) => TEXT_RE.test(file.name)).length;
        const covers = sent.filter(({file}) => COVER_RE.test(file.name)).length;
        const row = el("div", "lib-batch-row");
        const main = el("div", "lib-batch-main");
        main.append(el("span", "lib-batch-name", name ? `📁 ${name}` : "Archivos sueltos"),
            el("span", "muted small", [
                `${videos.length} vídeo(s)`, texts && `${texts} .txt`, covers && `${covers} portada(s)`,
                fmtBytes(sent.reduce((n, {file}) => n + file.size, 0)),
            ].filter(Boolean).join(" · ")));
        row.append(main);
        if (mode === "own") {
            const where = new Map();
            videos.forEach(({file}) => {
                const folder = destinationOf(file.name, name);
                where.set(folder, (where.get(folder) || 0) + 1);
            });
            row.append(el("span", "lib-batch-dest small", !where.size ? "→ junto a su vídeo, esté donde esté"
                : `→ ${[...where].map(([folder, n]) => folder + (where.size > 1 ? ` (${n})` : "")
                    + (known(folder) ? "" : " · nueva")).join(", ")}`));
        }
        const drop = el("button", "lib-batch-x", "×");
        drop.type = "button";
        drop.title = "Quitar de la lista";
        drop.addEventListener("click", () => {
            batch.delete(name);
            draw();
        });
        row.append(drop);
        return row;
    }

    function draw() {
        list.replaceChildren(...(batch.size ? [...batch].map(([name, files]) => folderRow(name, files))
            : [el("div", "lib-batch-empty muted small",
                "Ninguna carpeta todavía: añádelas con «＋ Añadir carpeta…» o arrástralas aquí.")]));
        picker.body.hidden = mode !== "existing";
        nameBox.hidden = mode !== "new";
        note.hidden = mode === "own";
        const count = (existing && lib.overview?.folders.find((f) => f.folder === existing)?.count) || 0;
        pickerPreview.textContent = !existing ? "Elige la carpeta."
            : `→ media/library/${existing}/ · ya tiene ${count} vídeo(s)`;
        const fresh = folderSlug(nameInput.value);
        namePreview.textContent = !fresh ? "Escribe alguna letra o número."
            : known(fresh) ? `Ya existe «${fresh}»: irán a ella.` : `→ media/library/${fresh}/ (nueva)`;
        const videos = [...batch.values()].reduce((n, files) => n + plan(files).jobs
            .filter((job) => VIDEO_RE.test(job.files[0].file.name)).length, 0);
        go.textContent = videos ? `Subir ${videos} vídeo(s)` : "Subir";
        go.disabled = !batch.size || (mode !== "own" && !into());
    }

    const choices = el("div", "checks choices lib-batch-choices");
    [
        ["own", "Cada una a su carpeta, como hasta ahora",
            "La de la serie del nombre (reel_07_pensamientos-estoicos_… → pensamientos-estoicos) "
            + "o, si el nombre no la dice, la que se llama como la carpeta subida.", null],
        ["existing", "Todas a una carpeta que ya existe", null, picker.body],
        ["new", "Todas a una carpeta nueva", null, nameBox],
    ].forEach(([value, label, hint, extra]) => {
        const row = el("label", "check");
        const radio = el("input");
        radio.type = "radio";
        radio.name = "lib-batch-mode";
        radio.value = value;
        radio.checked = value === mode;
        radio.addEventListener("change", () => {
            mode = value;
            draw();
            if (value === "existing") picker.search.focus();
            if (value === "new") nameInput.focus();
        });
        row.append(radio, document.createTextNode(` ${label}`));
        if (hint) row.append(el("span", "muted small block", hint));
        choices.append(row);
        if (extra) {
            extra.classList.add("check-nested");
            choices.append(extra);
        }
    });
    nameInput.addEventListener("input", draw);
    nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !go.disabled) go.click();
    });
    input.addEventListener("change", () => {
        const picked = [...input.files].map((file) => ({file, path: file.webkitRelativePath || file.name}));
        input.value = "";
        if (picked.length) add(picked);
    });

    const adder = el("div", "actions-bar lib-batch-add");
    adder.append(button("＋ Añadir carpeta…", "btn btn-ghost btn-sm", () => input.click()),
        el("span", "muted small", "El navegador deja elegir una cada vez; si están todas dentro "
            + "de otra, elige esa: sin vídeos propios, se toman sus subcarpetas."));
    const bar = el("div", "actions-bar");
    bar.append(go, button("Cancelar", "btn btn-ghost", closeModal));
    const wrap = el("div", "lib-batch");
    wrap.append(input, list, adder, el("h3", "lib-batch-title", "¿A qué carpeta van?"), choices, note, bar);
    wrap.addEventListener("dragover", (e) => {
        e.preventDefault();
        list.classList.add("is-over");
    });
    wrap.addEventListener("dragleave", (e) => {
        if (!wrap.contains(e.relatedTarget)) list.classList.remove("is-over");
    });
    wrap.addEventListener("drop", async (e) => {
        e.preventDefault();
        list.classList.remove("is-over");
        try {
            add(await droppedFiles(e.dataTransfer));
        } catch (err) {
            toast(err.message, true);
        }
    });

    if (files.length) add(files);
    draw();
    openModal("Subir varias carpetas", wrap, {wide: true});
    if (pick) input.click();
}

function send(form, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/library/upload");
        xhr.upload.onprogress = (e) => onProgress(e.loaded);
        xhr.onload = () => {
            let payload;
            try {
                payload = JSON.parse(xhr.responseText);
            } catch {
                payload = {detail: xhr.responseText};
            }
            if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
            else reject(new Error(typeof payload?.detail === "string" ? payload.detail : `Error ${xhr.status}`));
        };
        xhr.onerror = () => reject(new Error("Se perdió la conexión con la aplicación"));
        xhr.send(form);
    });
}

/** Upload in the background of the page, one video per request. `into`: the
 folder everything goes to; empty, each video's own (its series). */
async function upload(files, {into = ""} = {}) {
    if (lib.uploading) return toast("Ya hay una subida en marcha", true);
    const {jobs, ignored} = plan(files);
    if (!jobs.length) return toast("No hay vídeos, .txt ni portadas en lo que has elegido", true);
    const total = jobs.reduce((sum, job) => sum + job.files.reduce((s, f) => s + f.file.size, 0), 0) || 1;
    const status = $("#lib-upload-status");
    const progress = $("#lib-progress");
    const bar = $("#lib-bar");
    const summary = {added: 0, duplicates: 0, problems: [], notes: []};
    const videos = jobs.filter((j) => j.files.some((f) => VIDEO_RE.test(f.file.name))).length;
    const aiRewrite = $("#lib-ai").checked && !$("#lib-ai").disabled;
    let sent = 0;
    lib.uploading = true;
    progress.hidden = false;
    try {
        for (const [i, job] of jobs.entries()) {
            const size = job.files.reduce((s, f) => s + f.file.size, 0);
            const name = job.files[0].file.name;
            const form = new FormData();
            job.files.forEach((f) => form.append("files", f.file, f.file.name));
            form.append("folder", job.folder);
            if (into) form.append("into", into);
            // The switch as it was when the upload started, for every request of it.
            form.append("ai", aiRewrite ? "1" : "0");
            const label = VIDEO_RE.test(name) ? `${Math.min(i + 1, videos)} de ${videos} · ${name}` : "los .txt y portadas sueltos";
            try {
                const res = await send(form, (loaded) => {
                    bar.style.width = `${Math.round(((sent + Math.min(loaded, size)) / total) * 100)}%`;
                    status.textContent = `Subiendo ${label}…`;
                });
                summary.added += res.added.length;
                summary.duplicates += res.duplicates.length;
                summary.problems.push(...res.problems);
                summary.notes.push(...res.notes);
            } catch (err) {
                summary.problems.push(`«${name}»: ${err.message}`);
            }
            sent += size;
            bar.style.width = `${Math.round((sent / total) * 100)}%`;
            if ((i + 1) % 10 === 0) reload();
        }
    } finally {
        lib.uploading = false;
        progress.hidden = true;
    }
    status.textContent = [
        `${summary.added} vídeo(s) nuevos en ${into ? `«${into}»` : "la biblioteca"}`,
        summary.duplicates && `${summary.duplicates} ya estaban`,
        ignored && `${ignored} archivo(s) ignorados`,
        summary.problems.length && `${summary.problems.length} aviso(s)`,
    ].filter(Boolean).join(" · ");
    if (summary.problems.length) {
        const body = el("div");
        body.append(el("p", null, status.textContent));
        const list = el("div", "warn-list");
        summary.problems.forEach((p) => list.append(el("div", "note note-warn", p)));
        summary.notes.forEach((n) => list.append(el("div", "note note-info", n)));
        body.append(list);
        openModal("Subida a la biblioteca", body);
    } else {
        toast(status.textContent);
    }
    await reload();
    await state.refresh();
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */

/* ------------------------------------------------------------------ */

function selectedItems() {
    return (lib.items?.items || []).filter((i) => lib.selected.has(i.id));
}

export function wireLibrary() {
    const zone = $("#lib-dropzone");
    const folderInput = $("#lib-folder-input");
    const fileInput = $("#lib-file-input");
    $("#lib-pick-folder").addEventListener("click", (e) => {
        e.stopPropagation();
        folderInput.click();
    });
    $("#lib-pick-files").addEventListener("click", (e) => {
        e.stopPropagation();
        fileInput.click();
    });
    $("#lib-pick-many").addEventListener("click", (e) => {
        e.stopPropagation();
        if (lib.uploading) return toast("Ya hay una subida en marcha", true);
        openBatchUpload([], {pick: true});
    });
    zone.addEventListener("click", () => folderInput.click());
    folderInput.addEventListener("change", () => {
        const files = [...folderInput.files].map((file) => ({file, path: file.webkitRelativePath || file.name}));
        folderInput.value = "";
        if (files.length) received(files);
    });
    fileInput.addEventListener("change", () => {
        const files = [...fileInput.files].map((file) => ({file, path: file.name}));
        fileInput.value = "";
        if (files.length) upload(files);
    });
    ["dragenter", "dragover"].forEach((evt) => zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.add("is-over");
    }));
    ["dragleave", "drop"].forEach((evt) => zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.remove("is-over");
    }));
    zone.addEventListener("drop", async (e) => {
        try {
            const files = await droppedFiles(e.dataTransfer);
            if (files.length) await received(files);
        } catch (err) {
            toast(err.message, true);
        }
    });
    window.addEventListener("beforeunload", (e) => {
        if (!lib.uploading) return;
        e.preventDefault();
        e.returnValue = "";
    });

    $("#lib-scan").addEventListener("click", async () => {
        try {
            const res = await api("/api/library/scan", {method: "POST"});
            toast(res.added ? `${res.added} vídeo(s) nuevos en la biblioteca` : "No hay archivos nuevos en media/library");
            await reload();
        } catch (err) {
            toast(err.message, true);
        }
    });
    let timer = null;
    $("#lib-q").addEventListener("input", (e) => {
        clearTimeout(timer);
        timer = setTimeout(() => {
            lib.q = e.target.value.trim();
            lib.selected.clear();
            reload();
        }, 250);
    });
    $("#lib-folder-q").addEventListener("input", (e) => {
        lib.folderQ = e.target.value.trim();
        drawFolders();
    });
    $("#lib-select-all").addEventListener("change", (e) => {
        (lib.items?.items || []).forEach((i) => (e.target.checked ? lib.selected.add(i.id) : lib.selected.delete(i.id)));
        draw();
    });
    $("#lib-queue").addEventListener("change", (e) => {
        lib.queue = e.target.value;
        lib.selected.clear();
        reload();
    });
    $("#lib-text-filter").addEventListener("change", (e) => {
        lib.text = e.target.value;
        lib.selected.clear();
        reload();
    });
    $("#lib-issues").addEventListener("change", (e) => {
        lib.issues = e.target.checked;
        lib.selected.clear();
        reload();
    });
    $("#lib-tags").addEventListener("click", () => {
        const items = selectedItems();
        if (!items.length && !(lib.folder && lib.folder !== ALL)) return;
        openBulkTags(items);
    });
    $("#lib-move").addEventListener("click", () => {
        const items = selectedItems();
        if (items.length) openMoveItems(items);
    });
    $("#lib-rename").addEventListener("click", () => {
        if (lib.folder && lib.folder !== ALL) openRenameFolder(lib.folder);
    });
    $("#lib-labels").addEventListener("click", () => {
        if (lib.folder && lib.folder !== ALL) openFolderLabels(lib.folder);
    });
    $("#lib-add").addEventListener("click", () => {
        const items = selectedItems().filter((i) => !i.preflight_errors.length);
        if (items.length) openAddToQueue(items);
        else toast("Ninguno de los seleccionados se puede añadir a una cola", true);
    });
    $("#lib-rewrite").addEventListener("click", async () => {
        const items = selectedItems();
        if (!items.length) return;
        if (!confirm(`¿Volver a escribir los textos de TikTok y YouTube de ${items.length} vídeo(s)? `
            + "Los que estén en cola siguiendo la biblioteca recibirán los nuevos.")) return;
        try {
            const res = await api("/api/library/bulk", {
                method: "POST", body: JSON.stringify({action: "rewrite", ids: items.map((i) => i.id)}),
            });
            toast(`${res.done} vídeo(s) escribiendo sus textos` + (res.problems.length ? ` · ${res.problems.length} no` : ""));
            await reload();
            await state.refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#lib-sheet").addEventListener("click", async () => {
        const items = selectedItems();
        if (!items.length) return;
        if (!confirm(`¿Poner a ${items.length} vídeo(s) los textos de TikTok y YouTube de su ficha .txt, tal cual? `
            + "Sustituyen a los que tengan, también a los editados a mano.")) return;
        try {
            const res = await api("/api/library/bulk", {
                method: "POST", body: JSON.stringify({action: "sheet", ids: items.map((i) => i.id)}),
            });
            toast(`${res.done} vídeo(s) con los textos de su ficha` + (res.problems.length ? ` · ${res.problems.length} no` : ""),
                res.problems.length > 0 && res.done === 0);
            if (res.problems.length) {
                const body = el("div", "warn-list");
                res.problems.forEach((p) => body.append(el("div", "note note-warn", p)));
                openModal("Textos de la ficha", body);
            }
            await reload();
            await state.refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#lib-ai").addEventListener("change", async (e) => {
        const enabled = e.target.checked;
        try {
            const res = await api("/api/library/ai-texts", {method: "POST", body: JSON.stringify({enabled})});
            if (lib.overview) lib.overview.ai_rewrite = res.ai_rewrite;
            toast(res.ai_rewrite
                ? "Lo que entre con ficha .txt lo reescribirá Ollama"
                : "Lo que entre con ficha .txt saldrá con los textos de la ficha, tal cual");
        } catch (err) {
            e.target.checked = !enabled;
            toast(err.message, true);
        }
    });
    $("#lib-delete").addEventListener("click", async () => {
        const items = selectedItems();
        if (!items.length) return;
        const busy = items.filter((i) => i.queued.some((q) => !["PUBLISHED", "FAILED"].includes(q.status)));
        if (!confirm(`¿Borrar ${items.length} vídeo(s) de la biblioteca y del disco? No se puede deshacer.`
            + (busy.length ? `\n\n${busy.length} siguen en alguna cola y se quedarán: quítalos antes de ahí.` : ""))) return;
        try {
            const res = await api("/api/library/bulk", {
                method: "POST",
                body: JSON.stringify({action: "delete", ids: items.map((i) => i.id), delete_files: true}),
            });
            lib.selected.clear();
            toast(`${res.done} vídeo(s) borrados` + (res.problems.length ? ` · ${res.problems.length} siguen en cola` : ""),
                res.problems.length > 0);
            if (res.problems.length) {
                const body = el("div", "warn-list");
                res.problems.forEach((p) => body.append(el("div", "note note-warn", p)));
                openModal("No se borraron", body);
            }
            await reload();
            await state.refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
}
