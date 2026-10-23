/* Admin pages: Cuentas, Base de datos, Ajustes (sistema, copias, mantenimiento). */

import {
    $,
    accountLabel,
    accountLimits,
    accounts,
    api,
    button,
    closeModal,
    copyText,
    editingInside,
    el,
    fmtBytes,
    fmtDate,
    fmtDuration,
    link,
    openModal,
    setAccount,
    setQueue,
    state,
    toast,
} from "./shared.js";

/* ------------------------------------------------------------------ */
/* Connect form (shared by onboarding and Cuentas)                     */
/* ------------------------------------------------------------------ */

let lookupResults = [];

export function wireConnectForm() {
    $("#connect-lookup").addEventListener("click", lookupAccounts);
    $("#connect-key").addEventListener("keydown", (e) => {
        if (e.key === "Enter") lookupAccounts();
    });
    $("#connect-submit").addEventListener("click", submitAccounts);
    $("#connect-cancel").addEventListener("click", resetConnectForm);
    $("#accounts-add").addEventListener("click", () => {
        const form = $("#connect-form");
        $("#accounts-form-slot").append(form);
        form.hidden = !form.hidden;
        if (!form.hidden) $("#connect-key").focus();
    });
}

function resetConnectForm() {
    $("#connect-pick").hidden = true;
    $("#connect-accounts").replaceChildren();
    $("#connect-status").textContent = "";
    lookupResults = [];
    if (accounts().length) $("#connect-form").hidden = true;
}

async function lookupAccounts() {
    const key = $("#connect-key").value.trim();
    if (!key) return toast("Pega primero la API key de Zernio", true);
    $("#connect-status").textContent = "Consultando Zernio…";
    try {
        const res = await api("/api/zernio/accounts", {method: "POST", body: JSON.stringify({api_key: key})});
        lookupResults = res.accounts;
        const box = $("#connect-accounts");
        box.replaceChildren();
        if (!res.accounts.length) {
            $("#connect-status").textContent =
                "La clave es válida pero no hay ninguna cuenta conectada en Zernio. Conéctala en su panel y vuelve a buscar.";
            $("#connect-pick").hidden = true;
            return;
        }
        res.accounts.forEach((a) => {
            const row = el("label", `pick${a.already_connected ? " is-done" : ""}`);
            const check = el("input");
            check.type = "checkbox";
            check.value = a.id;
            check.checked = !a.already_connected;
            check.disabled = a.already_connected;
            const text = el("span");
            text.append(el("strong", null, `@${a.username || a.display_name}`),
                el("span", "muted small", ` · ${a.profile || "sin perfil"}${a.already_connected ? " · ya conectada" : ""}`));
            const label = el("input", "pick-label");
            label.type = "text";
            label.placeholder = "Nombre para mostrar (opcional)";
            label.dataset.id = a.id;
            label.disabled = a.already_connected;
            row.append(check, text, label);
            box.append(row);
        });
        const free = res.accounts.filter((a) => !a.already_connected).length;
        $("#connect-status").textContent = `${res.accounts.length} cuenta(s) en Zernio · ${free} por conectar`;
        $("#connect-pick").hidden = false;
    } catch (err) {
        $("#connect-status").textContent = "";
        toast(err.message, true);
    }
}

async function submitAccounts() {
    const key = $("#connect-key").value.trim();
    const ids = [...$("#connect-accounts").querySelectorAll("input[type=checkbox]:checked")].map((c) => c.value);
    if (!ids.length) return toast("Elige al menos una cuenta", true);
    const labels = {};
    $("#connect-accounts").querySelectorAll(".pick-label").forEach((input) => {
        if (input.value.trim()) labels[input.dataset.id] = input.value.trim();
    });
    try {
        const res = await api("/api/accounts", {
            method: "POST", body: JSON.stringify({api_key: key, account_ids: ids, labels}),
        });
        $("#connect-key").value = "";
        resetConnectForm();
        $("#connect-form").hidden = true;
        toast(`${res.added.length} cuenta(s) conectada(s)`);
        if (res.added.length) setAccount(res.added[0].id);
        await state.refresh();
        if (location.hash !== "#/cuentas") location.hash = "#/resumen";
    } catch (err) {
        toast(err.message, true);
    }
}

/* ------------------------------------------------------------------ */
/* Cuentas                                                             */

/* ------------------------------------------------------------------ */

/** The two ceilings every queue of this account shares.
 *
 * They live here and not in Programación because they are not a property of
 * the queue you happen to have open: TikTok counts posts per account, so one
 * number governs all of them. Programación only recalls them, and warns when
 * the queues add up to more than the account allows.
 */
/* A limit of an account card changed and not saved yet: the cards are not
   redrawn until it is saved or discarded (the 5-second refresh used to wipe
   what was being typed). What was drawn last, so an unchanged poll redraws nothing. */
let accountsDirty = false;
let accountsDrawn = "";

/** The page was left: what was typed and not saved is dropped. */
export function forgetAccountsDrawing() {
    accountsDirty = false;
    accountsDrawn = "";
}

function limitsEditor(entry) {
    const {account} = entry;
    const box = el("div", "account-limits");
    box.append(el("div", "muted small",
        "Límites de la cuenta · valen para todas sus colas"));

    const fields = el("div", "inline-fields");

    const noMax = el("input");
    noMax.type = "checkbox";
    noMax.checked = account.max_per_day === null;
    const noMaxLabel = el("label", "check");
    noMaxLabel.append(noMax, document.createTextNode(" Sin máximo"));

    const max = el("input");
    max.type = "number";
    max.min = "0";
    max.max = "200";
    max.value = account.max_per_day ?? 15;
    max.disabled = account.max_per_day === null;
    const maxLabel = el("label");
    maxLabel.append(document.createTextNode("Máximo "), max,
        document.createTextNode(" vídeos/día en total"));

    const gap = el("input");
    gap.type = "number";
    gap.min = "0";
    gap.max = "1440";
    gap.value = account.min_gap_minutes ?? 15;
    const gapLabel = el("label");
    gapLabel.append(document.createTextNode("Separación mínima "), gap,
        document.createTextNode(" min entre publicaciones"));
    gapLabel.title = "Vale entre dos vídeos de la misma cola y entre colas distintas";

    noMax.addEventListener("change", () => {
        max.disabled = noMax.checked;
    });
    fields.append(noMaxLabel, maxLabel, gapLabel);
    box.append(fields);

    const status = el("span", "muted small");
    const discard = button("Descartar", "btn btn-ghost btn-sm", () => {
        forgetAccountsDrawing();
        renderAccountsPage();
    });
    discard.hidden = true;
    const touched = () => {
        accountsDirty = true;
        status.textContent = "Cambios sin guardar";
        discard.hidden = false;
    };
    [noMax, max, gap].forEach((node) => {
        node.addEventListener("input", touched);
        node.addEventListener("change", touched);
    });
    const bar = el("div", "actions-bar");
    bar.append(button("Guardar límites", "btn btn-ghost btn-sm", async () => {
        try {
            await api(`/api/accounts/${account.id}`, {
                method: "PATCH",
                body: JSON.stringify({
                    max_per_day: noMax.checked ? null : Number(max.value),
                    min_gap_minutes: Number(gap.value),
                }),
            });
            toast(`Límites guardados: ${accountLimits({
                max_per_day: noMax.checked ? null : Number(max.value),
                min_gap_minutes: Number(gap.value),
            })}`);
            forgetAccountsDrawing();
            await state.refresh();
        } catch (err) {
            status.textContent = "";
            toast(err.message, true);
        }
    }), discard, status);
    box.append(bar);

    /* The same notes Programación shows: they are about how the queues of this
       account add up, which is exactly what is being edited here. */
    (entry.warnings || []).forEach((w) => box.append(el("div", "note note-warn", w)));
    return box;
}


export function renderAccountsPage() {
    const list = $("#accounts-list");
    const all = accounts();
    // Never under the user's fingers, and only when something changed.
    if (accountsDirty || editingInside(list)) return;
    const signature = JSON.stringify(all.map((e) => [e.account, e.stats, e.warnings,
        e.queues.map((q) => [q.schedule, q.stats])]));
    if (signature === accountsDrawn && list.childElementCount) return;
    accountsDrawn = signature;
    list.replaceChildren(
        ...all.map((entry) => {
            const {account, stats} = entry;
            const card = el("div", `card account-row${account.is_active ? "" : " is-inactive"}`);

            const head = el("div", "account-card-head");
            const title = el("div");
            title.append(el("strong", null, accountLabel(account)));
            const sub = [];
            if (account.username) sub.push(`@${account.username}`);
            if (account.profile_name) sub.push(`perfil ${account.profile_name}`);
            sub.push(`Zernio ${account.zernio_account_id}`);
            title.append(el("div", "muted small", sub.join(" · ")));
            head.append(title);

            const toggle = el("label", "switch-row switch-inline");
            toggle.append(el("span", "muted small", account.is_active ? "activa" : "desactivada"));
            const sw = el("input", "switch");
            sw.type = "checkbox";
            sw.checked = account.is_active;
            sw.title = "Una cuenta desactivada no publica ni recoge vídeos";
            sw.addEventListener("change", async () => {
                try {
                    await api(`/api/accounts/${account.id}`, {
                        method: "PATCH",
                        body: JSON.stringify({is_active: sw.checked})
                    });
                    toast(sw.checked ? "Cuenta activada" : "Cuenta desactivada");
                    await state.refresh();
                } catch (err) {
                    toast(err.message, true);
                    sw.checked = !sw.checked;
                }
            });
            toggle.append(sw);
            head.append(toggle);
            card.append(head);

            const lastPost = entry.queues
                .map((q) => q.schedule.last_post_local).filter(Boolean).sort().at(-1);
            const facts = el("dl", "kv kv-inline");
            [
                ["Colas", `${entry.queues.length} · ${entry.queues.filter((q) => q.schedule.enabled).length} activa(s)`],
                ["En cola / publicados / fallidos", `${stats.queued} / ${stats.published} / ${stats.failed}`],
                ["Hoy", `${stats.today}${account.max_per_day != null ? ` / ${account.max_per_day}` : ""}`],
                ["Última publicación", lastPost || "—"],
                ["Clave comprobada", account.last_checked_local || "nunca"],
                ["Conectada", account.created_local || "—"],
            ].forEach(([k, v]) => facts.append(el("dt", null, k), el("dd", null, v)));
            card.append(facts);
            card.append(limitsEditor(entry));

            /* The queues themselves: each one is an independent schedule. */
            const list = el("div", "queue-rows");
            entry.queues.forEach((q) => {
                const {schedule: s, stats: qs} = q;
                const row = el("div", "queue-row");
                const head = el("div", "queue-row-head");
                head.append(el("span", `queue-dot ${s.enabled ? "on" : "off"}`, s.enabled ? "●" : "○"),
                    el("strong", null, s.name));
                if (s.to_drafts) head.append(el("span", "badge SCHEDULED", "a borradores"));
                row.append(head);
                row.append(el("span", "muted small",
                    `${qs.queued} en cola · cada ${s.interval_min_minutes}–${s.interval_max_minutes} min · `
                    + `${(s.windows || []).length ? "franja horaria" : "a cualquier hora"} · `
                    + (s.enabled ? (s.next_slot_local ? `próxima ${s.next_slot_local}` : "sin hueco válido")
                        : "parada")));
                const go = el("div", "actions-bar");
                go.append(button("Abrir", "btn btn-ghost btn-sm", () => {
                    setAccount(account.id);
                    setQueue(s.id);
                    location.hash = "#/programacion";
                }));
                row.append(go);
                list.append(row);
            });
            card.append(list);
            (entry.warnings || []).forEach((w) => card.append(el("div", "note note-warn", w)));

            const actions = el("div", "actions-bar");
            actions.append(
                button("Probar conexión", "btn btn-ghost btn-sm", async () => {
                    const res = await api(`/api/accounts/${account.id}/test`, {method: "POST"});
                    toast(res.message, !res.ok);
                    await state.refresh();
                }),
                button("Mostrar clave", "btn btn-ghost btn-sm", () => revealKey(account)),
                button("Sustituir clave", "btn btn-ghost btn-sm", () => replaceKey(account)),
                button("Renombrar", "btn btn-ghost btn-sm", async () => {
                    const name = prompt("Nombre para mostrar:", account.display_name || "");
                    if (name === null) return;
                    await api(`/api/accounts/${account.id}`, {
                        method: "PATCH",
                        body: JSON.stringify({display_name: name})
                    });
                    toast("Nombre guardado");
                    await state.refresh();
                }),
                button("Añadir cola", "btn btn-ghost btn-sm", async () => {
                    const name = prompt("Nombre de la nueva cola (por ejemplo «Noche»):",
                        `Cola ${entry.queues.length + 1}`);
                    if (name === null) return;
                    const created = await api(`/api/accounts/${account.id}/schedules`, {
                        method: "POST", body: JSON.stringify({name: name.trim()}),
                    });
                    setAccount(account.id);
                    setQueue(created.schedule.id);
                    toast(`Cola «${created.schedule.name}» creada`);
                    await state.refresh();
                    location.hash = "#/programacion";
                }),
                button("Ir a la cola", "btn btn-ghost btn-sm", () => {
                    setAccount(account.id);
                    location.hash = "#/cola";
                }),
                button("Eliminar", "btn btn-danger btn-sm", async () => {
                    if (!confirm(`¿Eliminar ${accountLabel(account)} y sus ${entry.queues.length} cola(s) de esta app?\n\nLos archivos de vídeo se quedan en disco. En Zernio no cambia nada.`)) return;
                    await api(`/api/accounts/${account.id}`, {method: "DELETE"});
                    toast("Cuenta eliminada");
                    await state.refresh();
                }),
            );
            card.append(actions);
            return card;
        })
    );
    if (!all.length) list.append(el("div", "empty", "No hay cuentas. Pulsa «Añadir cuenta»."));
}

async function revealKey(account) {
    try {
        const res = await api(`/api/accounts/${account.id}/key`);
        const body = el("div");
        body.append(el("p", "muted small",
            "Esta es la API key de Zernio guardada para esta cuenta. Trátala como una contraseña: quien la tenga puede publicar en tus redes. Cada consulta queda registrada en Actividad."));
        const field = el("div", "key-field");
        const input = el("input");
        input.type = "text";
        input.readOnly = true;
        input.value = res.api_key;
        input.addEventListener("focus", () => input.select());
        field.append(input, button("Copiar", "btn btn-primary btn-sm", () => copyText(res.api_key)));
        body.append(field);
        body.append(link("Gestionar claves en Zernio →", "https://zernio.com/dashboard/api-keys", "small"));
        openModal(`API key · ${accountLabel(account)}`, body);
    } catch (err) {
        toast(err.message, true);
    }
}

function replaceKey(account) {
    const body = el("div");
    body.append(el("p", "muted small",
        "Pega la nueva API key. Se comprueba contra Zernio antes de guardarla: debe dar acceso a esta misma cuenta."));
    const input = el("input");
    input.type = "password";
    input.placeholder = "zrn_…";
    input.autocomplete = "off";
    input.className = "wide";
    body.append(input);
    const bar = el("div", "actions-bar");
    bar.append(button("Guardar clave", "btn btn-primary btn-sm", async () => {
        const key = input.value.trim();
        if (!key) return toast("Pega la clave", true);
        await api(`/api/accounts/${account.id}/key`, {method: "PUT", body: JSON.stringify({api_key: key})});
        closeModal();
        toast("Clave sustituida");
        await state.refresh();
    }));
    body.append(bar);
    openModal(`Sustituir clave · ${accountLabel(account)}`, body);
    setTimeout(() => input.focus(), 50);
}

/* ------------------------------------------------------------------ */
/* Ajustes: settings, backups, maintenance, system                    */
/* ------------------------------------------------------------------ */

let systemCache = null;

export function renderSettingsPage() {
    if (document.activeElement !== $("#set-hashtags")) $("#set-hashtags").value = state.data.settings.hashtags || "";
    if (document.activeElement !== $("#set-webhook")) $("#set-webhook").value = state.data.settings.webhook_url || "";
    if (!systemCache) loadSystem();
}

async function loadSystem() {
    try {
        systemCache = await api("/api/admin/system");
        renderSystem();
    } catch (err) {
        toast(err.message, true);
    }
}

function renderSystem() {
    const s = systemCache;
    if (!s) return;
    const info = $("#system-info");
    info.replaceChildren();
    const rows = [
        ["Versión", `${s.version} · esquema ${s.schema_version}`],
        ["Python / SQLite", `${s.python} / ${s.sqlite}`],
        ["Planificador", s.scheduler.running
            ? `en marcha · cada ${s.scheduler.tick_seconds} s · ${s.scheduler.tick_count} ciclos · activo ${fmtDuration(s.scheduler.uptime_seconds)}`
            : "detenido"],
        ["Último ciclo", fmtDate(s.scheduler.last_tick_at)],
        ["ffmpeg", s.ffmpeg || "no encontrado"],
        ["Base de datos", `${s.database.path} · ${fmtBytes(s.database.bytes)}`],
        ["Biblioteca", `${s.media.library.count} archivos · ${fmtBytes(s.media.library.bytes)} · ${s.media.library.path}`],
        /* The folders from before the library: they empty with tools/library_finalize.py. */
        ...["inbox", "posted", "failed"].filter((k) => s.media[k].count).map((k) =>
            [`media/${k}`, `${s.media[k].count} archivos · ${fmtBytes(s.media[k].bytes)} · pendientes de pasar a la biblioteca`]),
        ["Vídeos por estado", Object.entries(s.videos).filter(([, n]) => n).map(([k, n]) => `${k.toLowerCase()} ${n}`).join(" · ") || "—"],
        ["API de Zernio", s.zernio_api_base],
        ["Textos automáticos", aiSummary(s.ai)],
        ["Código de acceso", s.passcode_enabled ? "activado" : "desactivado (pon UI_PASSCODE en .env)"],
        ["Copias", `${s.backups.items.length} en ${s.backups.dir}`],
        ["Copias automáticas", s.backups.auto_hours
            ? `cada ${s.backups.auto_hours} h · se guardan las ${s.backups.keep} últimas (BACKUP_AUTO_HOURS, BACKUP_KEEP)`
            : "desactivadas (BACKUP_AUTO_HOURS=0)"],
        ...(s.disk ? [["Disco", `${fmtBytes(s.disk.free)} libres de ${fmtBytes(s.disk.total)}`
        + (s.disk.low ? " · ⚠ queda poco" : "")]] : []),
    ];
    rows.forEach(([k, v]) => info.append(el("dt", null, k), el("dd", null, v)));
    renderBackups(s.backups.items);
}

function aiSummary(ai) {
    if (!ai) return "—";
    if (!ai.enabled) return "desactivados (CAPTION_AI_ENABLED=false)";
    const whisper = ai.whisper.available
        ? `Whisper «${ai.whisper.model}» (${ai.whisper.language})${ai.whisper.cached ? "" : " · modelo pendiente de descargar"}`
        : `Whisper no disponible: ${ai.whisper.error}`;
    const ollama = ai.ollama.reachable
        ? `Ollama ${ai.ollama.model} ✓`
        : (ai.ollama.url ? `Ollama no disponible (${ai.ollama.error}) → texto = primera frase` : "sin Ollama → texto = primera frase");
    const busy = ai.current_video_id ? " · escribiendo ahora" : "";
    return `${whisper} · ${ollama} · ${ai.processed} generados${busy}`;
}

function renderBackups(items) {
    const list = $("#backup-list");
    list.replaceChildren();
    if (!items.length) {
        list.append(el("div", "empty", "Todavía no hay copias."));
        return;
    }
    items.forEach((b) => {
        const row = el("div", "item");
        const main = el("div", "item-main");
        main.append(el("div", "item-title", b.name), el("div", "item-meta",
            `${fmtBytes(b.bytes)} · ${fmtDate(b.created_at)}${b.auto ? " · automática (rota sola)" : " · hecha a mano"}`));
        row.append(main);
        const actions = el("div", "item-actions");
        actions.append(
            link("Descargar", `/api/admin/backups/${encodeURIComponent(b.name)}`, "btn btn-ghost btn-sm", false),
            button("Borrar", "btn btn-ghost btn-sm", async () => {
                if (!confirm(`¿Borrar la copia ${b.name}?`)) return;
                await api(`/api/admin/backups/${encodeURIComponent(b.name)}`, {method: "DELETE"});
                await loadSystem();
            }),
        );
        row.append(actions);
        list.append(row);
    });
}

export function wireAdmin() {
    $("#set-save").addEventListener("click", saveSettings);
    $("#set-webhook-test").addEventListener("click", async () => {
        try {
            await saveSettings(false);
            const res = await api("/api/settings/webhook-test", {method: "POST"});
            toast(`Aviso de prueba enviado (${res.kind}). ¿Te ha llegado?`);
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#backup-create").addEventListener("click", async () => {
        $("#backup-status").textContent = "Creando copia…";
        try {
            const res = await api("/api/admin/backups", {method: "POST"});
            $("#backup-status").textContent = `Creada ${res.name} (${fmtBytes(res.bytes)})`;
            await loadSystem();
        } catch (err) {
            $("#backup-status").textContent = "";
            toast(err.message, true);
        }
    });
    $("#maint-prune").addEventListener("click", async () => {
        const keep = Number($("#maint-keep").value || 500);
        if (!confirm(`¿Recortar el registro dejando las últimas ${keep} entradas?`)) return;
        try {
            const res = await api("/api/admin/events/prune", {method: "POST", body: JSON.stringify({keep})});
            toast(`${res.removed} entradas eliminadas`);
            await state.refresh();
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#maint-clear-failed").addEventListener("click", async () => {
        if (!confirm("¿Quitar todos los vídeos fallidos de todas las colas? Sus archivos siguen en la biblioteca.")) return;
        try {
            const res = await api("/api/admin/videos/clear-failed", {
                method: "POST", body: JSON.stringify({delete_files: false}),
            });
            toast(`${res.removed} vídeo(s) eliminado(s)`);
            await state.refresh();
            await loadSystem();
        } catch (err) {
            toast(err.message, true);
        }
    });
    wireDb();
}

async function saveSettings(announce = true) {
    await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({hashtags: $("#set-hashtags").value, webhook_url: $("#set-webhook").value}),
    });
    if (announce) {
        $("#set-status").textContent = "Guardado";
        toast("Ajustes guardados");
    }
    await state.refresh();
}

/* ------------------------------------------------------------------ */
/* Base de datos                                                       */
/* ------------------------------------------------------------------ */

const db = {tables: null, table: null, page: 1, perPage: 50, sort: null, dir: "asc", q: "", data: null, loading: false};

export function renderDbPage() {
    if (!db.tables) loadTables();
    if (!wipe.preview) loadWipePreview();
    const wanted = state.pageArg;
    if (wanted && wanted !== db.table) {
        db.table = wanted;
        db.page = 1;
        db.sort = null;
        db.q = "";
        $("#db-q").value = "";
        loadTable();
    }
    renderTableList();
}

async function loadTables() {
    try {
        db.tables = (await api("/api/db/tables")).tables;
        renderTableList();
    } catch (err) {
        toast(err.message, true);
    }
}

function renderTableList() {
    const box = $("#db-tables");
    box.replaceChildren(
        ...((db.tables || []).map((t) => {
            const a = el("a", `db-table${t.name === db.table ? " is-active" : ""}`);
            a.href = `#/db/${t.name}`;
            a.append(el("span", null, t.name), el("span", "muted small", `${t.rows}`));
            return a;
        }))
    );
    if (!db.tables) box.append(el("div", "muted small", "Cargando…"));
}

async function loadTable() {
    if (!db.table) return;
    db.loading = true;
    const params = new URLSearchParams({page: db.page, per_page: db.perPage, dir: db.dir});
    if (db.sort) params.set("sort", db.sort);
    if (db.q) params.set("q", db.q);
    try {
        db.data = await api(`/api/db/tables/${encodeURIComponent(db.table)}?${params}`);
        renderTable();
    } catch (err) {
        toast(err.message, true);
    } finally {
        db.loading = false;
    }
}

function renderTable() {
    const data = db.data;
    const card = $("#db-table-card");
    if (!data) {
        card.hidden = true;
        return;
    }
    card.hidden = false;
    $("#db-table-title").textContent = `${data.table} · ${data.total} filas`;
    $("#db-export").href = `/api/db/tables/${encodeURIComponent(data.table)}/export.csv`;
    $("#db-perpage").value = String(db.perPage);

    const table = $("#db-grid");
    table.replaceChildren();
    const thead = el("thead");
    const headRow = el("tr");
    data.columns.forEach((c) => {
        const th = el("th", c.pk ? "is-pk" : "");
        th.append(document.createTextNode(c.name));
        if (db.sort === c.name) th.append(el("span", "sort", db.dir === "asc" ? " ↑" : " ↓"));
        th.title = `${c.type}${c.nullable ? "" : " · NOT NULL"}${c.secret ? " · cifrado" : ""}`;
        th.addEventListener("click", () => {
            if (db.sort === c.name) db.dir = db.dir === "asc" ? "desc" : "asc";
            else {
                db.sort = c.name;
                db.dir = "asc";
            }
            loadTable();
        });
        headRow.append(th);
    });
    thead.append(headRow);
    table.append(thead);

    const tbody = el("tbody");
    data.rows.forEach((row) => {
        const tr = el("tr");
        data.columns.forEach((c) => {
            const value = row[c.name];
            const td = el("td", value === null ? "is-null" : "");
            td.textContent = value === null ? "NULL" : String(value);
            td.title = td.textContent;
            tr.append(td);
        });
        if (data.pk) tr.addEventListener("click", () => openRow(row[data.pk]));
        tbody.append(tr);
    });
    table.append(tbody);

    const pager = $("#db-pager");
    pager.replaceChildren(
        button("‹ Anterior", "btn btn-ghost btn-sm", () => {
            if (db.page > 1) {
                db.page -= 1;
                loadTable();
            }
        }),
        el("span", "muted small", `Página ${data.page} de ${data.pages}`),
        button("Siguiente ›", "btn btn-ghost btn-sm", () => {
            if (db.page < data.pages) {
                db.page += 1;
                loadTable();
            }
        }),
    );
}

async function openRow(pk) {
    try {
        const res = await api(`/api/db/tables/${encodeURIComponent(db.table)}/rows/${encodeURIComponent(pk)}`);
        const body = el("div", "row-editor");
        const inputs = {};
        res.columns.forEach((c) => {
            const field = el("label", "block-field");
            field.append(document.createTextNode(`${c.name} `), el("span", "muted small", c.type + (c.pk ? " · clave" : "") + (c.secret ? " · cifrado" : "")));
            const value = res.row[c.name];
            const long = typeof value === "string" && (value.length > 60 || value.includes("\n"));
            const input = el(long ? "textarea" : "input");
            if (!long) input.type = "text";
            else input.rows = 3;
            input.value = value === null ? "" : String(value);
            input.placeholder = value === null ? "NULL" : "";
            input.disabled = c.pk || c.secret;
            input.dataset.column = c.name;
            inputs[c.name] = {input, original: value, column: c};
            field.append(input);
            body.append(field);
        });
        const bar = el("div", "actions-bar");
        bar.append(
            button("Guardar cambios", "btn btn-primary btn-sm", async () => {
                const values = {};
                Object.values(inputs).forEach(({input, original, column}) => {
                    if (input.disabled) return;
                    const raw = input.value;
                    const next = raw === "" && original === null ? null : raw === "" && column.nullable ? null : raw;
                    if (String(next ?? "") !== String(original ?? "") || (next === null) !== (original === null)) values[column.name] = next;
                });
                if (!Object.keys(values).length) return toast("No hay cambios");
                await api(`/api/db/tables/${encodeURIComponent(db.table)}/rows/${encodeURIComponent(pk)}`, {
                    method: "PUT", body: JSON.stringify({values}),
                });
                closeModal();
                toast("Fila actualizada");
                await loadTable();
                await state.refresh();
            }),
            button("Borrar fila", "btn btn-danger btn-sm", async () => {
                if (!confirm(`¿Borrar la fila ${pk} de ${db.table}? Esta acción no se puede deshacer.`)) return;
                await api(`/api/db/tables/${encodeURIComponent(db.table)}/rows/${encodeURIComponent(pk)}`, {method: "DELETE"});
                closeModal();
                toast("Fila borrada");
                await loadTables();
                await loadTable();
                await state.refresh();
            }),
        );
        body.append(bar);
        openModal(`${db.table} · ${res.pk} = ${pk}`, body);
    } catch (err) {
        toast(err.message, true);
    }
}

/* ------------------------------------------------------------------ */
/* Borrar todo                                                         */

/* ------------------------------------------------------------------ */

const wipe = {preview: null};

async function loadWipePreview() {
    try {
        wipe.preview = await api("/api/admin/wipe/preview");
    } catch {
        return;   /* the page still works without the summary */
    }
    renderWipeSummary();
}

function renderWipeSummary() {
    const box = $("#wipe-summary");
    const p = wipe.preview;
    if (!box || !p) return;
    const rows = Object.entries(p.tables)
        .filter(([, n]) => n > 0)
        .map(([name, n]) => `${name} ${n}`)
        .join(" · ");
    box.replaceChildren(
        el("div", null, `${p.rows} fila(s) en la base de datos${rows ? ` — ${rows}` : ""}`),
        el("div", null, `${p.files} vídeo(s) en las carpetas de la app · ${fmtBytes(p.bytes)} que se liberan`),
    );
    if (p.delegated) {
        box.append(el("div", "note note-warn",
            `${p.delegated} vídeo(s) están programados en Zernio. Borrar la base de datos NO los `
            + "cancela: Zernio los publicará igualmente. Recupéralos antes desde la Cola si no "
            + "quieres que salgan."));
    }
    if (p.in_flight) {
        box.append(el("div", "note note-warn",
            `${p.in_flight} vídeo(s) se están subiendo ahora mismo: espera a que terminen.`));
    }
}

/** The three consecutive challenges. Any mistake throws the whole thing away. */
async function startWipe() {
    const first = await api("/api/admin/wipe/challenge", {method: "POST"});
    wipe.preview = first.preview;
    renderWipeSummary();
    askWipeStep(first);
}

function askWipeStep(challenge) {
    const body = el("div", "wipe-dialog");
    const p = wipe.preview || {rows: 0, files: 0, bytes: 0};
    body.append(el("div", "note note-warn",
        `Vas a borrar ${p.rows} fila(s) y ${p.files} vídeo(s) (${fmtBytes(p.bytes)}). `
        + "Cuentas, claves, colas, vídeos, analíticas y comentarios incluidos. "
        + "Sólo se puede deshacer restaurando la copia que se guarda automáticamente."));
    body.append(el("div", "wipe-step", `Comprobación ${challenge.step} de ${challenge.steps}`));

    const sum = el("label", "block-field", `¿Cuánto es ${challenge.question}?`);
    const sumInput = el("input");
    sumInput.type = "number";
    sumInput.inputMode = "numeric";
    sum.append(sumInput);

    const word = el("label", "block-field", `Escribe la palabra: ${challenge.word}`);
    const wordInput = el("input");
    wordInput.type = "text";
    wordInput.autocapitalize = "off";
    wordInput.spellcheck = false;
    word.append(wordInput);
    body.append(sum, word);

    const send = async () => {
        const res = await api("/api/admin/wipe/answer", {
            method: "POST",
            body: JSON.stringify({
                token: challenge.token,
                total: Number(sumInput.value),
                word: wordInput.value,
            }),
        });
        if (res.ready) return runWipe(challenge.token);
        askWipeStep(res);
    };

    const actions = el("div", "actions-bar");
    actions.append(
        button(challenge.step < challenge.steps ? "Siguiente" : "Borrar definitivamente",
            challenge.step < challenge.steps ? "btn btn-primary" : "btn btn-danger",
            async () => {
                try {
                    await send();
                } catch (err) {
                    closeModal();
                    $("#wipe-status").textContent = "Borrado cancelado";
                    toast(err.message, true);
                }
            }),
        button("Cancelar", "btn btn-ghost", closeModal),
    );
    body.append(actions);
    openModal("Borrar todo", body);
    sumInput.focus();
}

async function runWipe(token) {
    $("#wipe-status").textContent = "Borrando…";
    try {
        const res = await api("/api/admin/wipe", {method: "POST", body: JSON.stringify({token})});
        closeModal();
        $("#wipe-status").textContent = res.message;
        toast(res.message);
        if (res.delegated_left_in_zernio)
            toast(`Ojo: ${res.delegated_left_in_zernio} vídeo(s) siguen programados en Zernio y se publicarán.`, true);
        db.table = null;
        db.data = null;
        $("#db-table-card").hidden = true;
        await loadTables();
        await loadWipePreview();
        await state.refresh();
        state.route();
    } catch (err) {
        $("#wipe-status").textContent = "";
        toast(err.message, true);
        throw err;
    }
}

function wireDb() {
    $("#wipe-btn").addEventListener("click", async () => {
        try {
            await startWipe();
        } catch (err) {
            toast(err.message, true);
        }
    });
    $("#db-reload").addEventListener("click", async () => {
        await loadTables();
        await loadTable();
    });
    $("#db-q").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            db.q = e.target.value.trim();
            db.page = 1;
            loadTable();
        }
    });
    $("#db-perpage").addEventListener("change", (e) => {
        db.perPage = Number(e.target.value);
        db.page = 1;
        loadTable();
    });
    $("#sql-run").addEventListener("click", runSql);
    $("#sql-text").addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runSql();
    });
}

async function runSql() {
    const sql = $("#sql-text").value.trim();
    if (!sql) return;
    $("#sql-status").textContent = "Ejecutando…";
    try {
        const res = await api("/api/db/query", {
            method: "POST", body: JSON.stringify({sql, limit: Number($("#sql-limit").value || 200)}),
        });
        $("#sql-status").textContent = `${res.count} fila(s)${res.truncated ? " (truncado)" : ""} · ${res.elapsed_ms} ms`;
        const table = $("#sql-grid");
        table.replaceChildren();
        const thead = el("thead");
        const hr = el("tr");
        res.columns.forEach((c) => hr.append(el("th", null, c)));
        thead.append(hr);
        table.append(thead);
        const tbody = el("tbody");
        res.rows.forEach((row) => {
            const tr = el("tr");
            row.forEach((v) => {
                const td = el("td", v === null ? "is-null" : "");
                td.textContent = v === null ? "NULL" : String(v);
                tr.append(td);
            });
            tbody.append(tr);
        });
        table.append(tbody);
    } catch (err) {
        $("#sql-status").textContent = "";
        toast(err.message, true);
    }
}
