/* The text composer: title, description, hashtags and pinned comment of one
   video, for one platform or for both, with

   - the platform's limits counted as you type (the first line before «más»,
     2200 characters on TikTok, 100 in a title, 150 in a TikTok comment);
   - the review the generator runs on its own drafts (/api/text/check), live;
   - a preview of what goes out, fixed hashtags of Ajustes included, and
     which hashtags TikTok will count (the first five);
   - hashtags as chips, cleaned the way the server cleans them, with the
     ones worth adding: proven on the platform, used by the series, on topic;
   - «Arreglar» for the fixes that need no model, and «Proponer con IA» for a
     fresh draft that is only used if you say so.

   Nothing is saved until «Guardar». No framework, no build step. */

import {api, button, closeModal, el, openModal, toast} from "./shared.js";

const LABEL = {tiktok: "TikTok", youtube: "YouTube"};
const LIMITS = {
    tiktok: {title: 100, caption: 2200, comment: 150, fold: 100, count: 5},
    youtube: {title: 100, caption: 5000, comment: null, fold: 100, count: 5, tagChars: 500},
};
/* TikTok counts the first five hashtags of a post (hashtags.TIKTOK_MAX_TAGS). */
const COUNTED = 5;
const MAX_TAGS = 30;
const GENERIC = new Set(["parati", "fyp", "fypシ", "foryou", "foryoupage", "viral", "viralvideo",
    "tiktok", "trending", "reels", "xyzbca"]);
const LEVEL_ICON = {error: "⛔", warn: "⚠️", tip: "💡"};
const LEVEL_NAMES = {error: ["error", "errores"], warn: ["aviso", "avisos"], tip: ["sugerencia", "sugerencias"]};
const SOURCE_ICON = {funcionan: "📈", serie: "📁", tema: "🏷️"};
const FIELD_LABEL = {
    title: "Título", description: "Descripción", tags: "Hashtags", first_comment: "Comentario fijado",
};
const INLINE_TAG_RE = /(?<![\p{L}\p{N}_#])#[^\s#]+/gu;
const REVIEW_DELAY = 350;

/* ------------------------------------------------------------------ */
/* Hashtags, the way services/hashtags.py sees them                    */

/* ------------------------------------------------------------------ */

/** Accent-free lower case: #hábitos and #habitos are the same tag. */
export function foldTag(tag) {
    return String(tag).toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

/** What the server keeps of a hashtag (hashtags.normalize), or null. */
export function normalizeTag(raw) {
    let body = String(raw || "").trim().replace(/^#+/, "").trim().toLowerCase();
    body = [...body].filter((c) => /[\p{L}\p{N}_]/u.test(c)).join("");
    if (body.length < 2 || body.length > 40 || /^\p{N}+$/u.test(body)) return null;
    if (/[^\p{Script=Latin}\p{N}_ºª]/u.test(body)) return null;
    return `#${body}`;
}

function splitTags(text) {
    return String(text || "").split(/[\s,]+/).filter(Boolean);
}

function inlineTags(text) {
    return [...String(text || "").matchAll(INLINE_TAG_RE)].map((m) => m[0]);
}

function isGeneric(tag) {
    return GENERIC.has(foldTag(tag).replace(/^#/, ""));
}

/** The first line of the description, without hashtags: what shows before «más». */
function firstLine(text) {
    const line = String(text || "").replace(INLINE_TAG_RE, "").split("\n")
        .map((l) => l.trim()).find(Boolean);
    return line ? line.replace(/\s{2,}/g, " ") : "";
}

/** In post order (the text's own first, then the list, then the fixed ones): the five that count. */
function countedTags(texts, fixed) {
    const seen = new Set();
    const order = [];
    [...inlineTags(texts.description), ...texts.tags, ...fixed].forEach((raw) => {
        const tag = normalizeTag(raw);
        if (!tag || seen.has(foldTag(tag))) return;
        seen.add(foldTag(tag));
        order.push(tag);
    });
    return new Set(order.slice(0, COUNTED).map(foldTag));
}

function withFixed(tags, fixed) {
    const known = new Set(tags.map(foldTag));
    return [...tags, ...fixed.filter((t) => !known.has(foldTag(t)))];
}

/* ------------------------------------------------------------------ */
/* Small pieces                                                        */

/* ------------------------------------------------------------------ */

function counter(node, value, max) {
    node.textContent = max ? `${value}/${max}` : String(value);
    node.classList.toggle("near", !!max && value > max * 0.9 && value <= max);
    node.classList.toggle("over", !!max && value > max);
}

function plural(n, [one, many]) {
    return `${n} ${n === 1 ? one : many}`;
}

/** A caption with its hashtags marked, built from text nodes (never innerHTML). */
function highlighted(text) {
    const box = el("div", "preview-text");
    let last = 0;
    for (const match of String(text || "").matchAll(INLINE_TAG_RE)) {
        if (match.index > last) box.append(document.createTextNode(text.slice(last, match.index)));
        box.append(el("span", "preview-tag", match[0]));
        last = match.index + match[0].length;
    }
    if (last < text.length) box.append(document.createTextNode(text.slice(last)));
    return box;
}

/** Hashtags typed as chips: Enter, space or comma add; Backspace removes; a click puts one first. */
function chipInput({tags, placeholder, onChange}) {
    const root = el("div", "chip-input");
    const input = el("input");
    input.type = "text";
    input.placeholder = placeholder;
    input.setAttribute("aria-label", "Añadir hashtag");
    const note = el("div", "chip-note muted small");
    let list = [...tags];
    let marks = {counted: null};

    const chip = (tag) => {
        const node = el("span", "tag-chip");
        const counted = marks.counted;
        if (counted && !counted.has(foldTag(tag))) node.classList.add("is-uncounted");
        if (isGeneric(tag)) node.classList.add("is-generic");
        const text = el("button", "tag-chip-text", tag);
        text.type = "button";
        text.title = counted && !counted.has(foldTag(tag))
            ? "TikTok no cuenta este: sólo los cinco primeros. Clic: ponerlo el primero."
            : "Clic: ponerlo el primero (TikTok cuenta los cinco primeros).";
        text.addEventListener("click", () => {
            list = [tag, ...list.filter((t) => t !== tag)];
            draw();
            onChange([...list]);
        });
        const remove = el("button", "tag-chip-x", "×");
        remove.type = "button";
        remove.title = `Quitar ${tag}`;
        remove.addEventListener("click", () => {
            list = list.filter((t) => t !== tag);
            draw();
            onChange([...list]);
            input.focus();
        });
        node.append(text, remove);
        return node;
    };
    // The input never leaves the DOM: moving it would take the focus away
    // in the middle of typing the next hashtag.
    root.append(input);
    const draw = () => {
        root.querySelectorAll(".tag-chip").forEach((node) => node.remove());
        list.forEach((tag) => root.insertBefore(chip(tag), input));
    };

    const add = (raw) => {
        const refused = [];
        let changed = false;
        splitTags(raw).forEach((part) => {
            const tag = normalizeTag(part);
            if (!tag) {
                refused.push(part);
                return;
            }
            if (list.some((t) => foldTag(t) === foldTag(tag))) return;
            if (list.length >= MAX_TAGS) {
                refused.push(part);
                return;
            }
            list.push(tag);
            changed = true;
        });
        note.textContent = refused.length
            ? `No válido: ${refused.join(" ")} (letras, números y «_», de 2 a 40, sin otros alfabetos).`
            : "";
        if (changed) {
            draw();
            onChange([...list]);
        }
        return changed;
    };
    const flush = () => {
        if (!input.value.trim()) return false;
        const value = input.value;
        input.value = "";
        return add(value);
    };

    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || (e.key === "Tab" && input.value.trim())) {
            // Enter never submits anything from here: it closes the hashtag.
            e.preventDefault();
            flush();
        } else if (e.key === "Backspace" && !input.value && list.length) {
            list = list.slice(0, -1);
            draw();
            onChange([...list]);
        }
    });
    // A space or a comma closes a hashtag, however it arrived: typed, pasted,
    // or inserted by a phone keyboard that sends no key events at all.
    input.addEventListener("input", () => {
        if (!/[\s,]/.test(input.value)) return;
        const parts = input.value.split(/[\s,]+/);
        const rest = /[\s,]$/.test(input.value) ? "" : parts.pop();
        input.value = rest;
        const done = parts.filter(Boolean).join(" ");
        if (done) add(done);
    });
    input.addEventListener("blur", flush);
    root.addEventListener("click", (e) => {
        if (e.target === root) input.focus();
    });
    draw();
    return {
        root, note, input, flush, add,
        get: () => [...list],
        set(tagsNow) {
            list = [...tagsNow];
            draw();
        },
        mark(counted) {
            marks = {counted};
            draw();
        },
    };
}

/* ------------------------------------------------------------------ */
/* The composer                                                        */

/* ------------------------------------------------------------------ */

/**
 * @param {object} options
 * @param {string} options.title          the dialog's title
 * @param {string[]} options.platforms    ["tiktok"], ["youtube"] or both
 * @param {object} options.texts          {platform: {title, description, tags, first_comment}}
 * @param {object} options.context        {library_id} or {video_id}: what the review compares with
 * @param {Function} options.onSave       async (texts) => …, before the dialog closes
 * @param {string[]} [options.notes]      shown above the buttons
 * @param {string} [options.handle]       the account's name, for the preview
 * @param {string} [options.focus]        "tags" to start in the hashtags
 * @param {boolean} [options.templates]   the description is a template ({a|b}, [fecha]…)
 * @param {object} [options.ai]           state.data.health.ai, for «Proponer con IA»
 * @param {string[]} [options.fixed]      the fixed hashtags of Ajustes
 */
export function openComposer(options) {
    const {platforms, context = {}, onSave, notes = [], handle = "", templates = false} = options;
    const fixed = (options.fixed || []).map(normalizeTag).filter(Boolean);
    const model = {
        active: platforms[0],
        texts: Object.fromEntries(platforms.map((p) => [p, {
            title: options.texts[p]?.title || "",
            description: options.texts[p]?.description || "",
            tags: (options.texts[p]?.tags || []).map(normalizeTag).filter(Boolean),
            first_comment: options.texts[p]?.first_comment || "",
        }])),
        reports: {},
        suggestions: {},
        timers: {},
        seq: {},
        unavailable: false,
        proposal: null,
    };
    const original = JSON.stringify(model.texts);
    const dirty = () => JSON.stringify(model.texts) !== original;

    const body = el("div", "composer");
    const head = el("div", "composer-head");
    const tabs = el("div", "composer-tabs");
    const tools = el("div", "composer-tools");
    head.append(tabs, tools);
    const proposalBox = el("div", "proposal");
    proposalBox.hidden = true;
    const grid = el("div", "composer-grid");
    const editor = el("div", "composer-editor");
    const side = el("div", "composer-side");
    grid.append(editor, side);
    body.append(head, proposalBox, grid);

    /* One section per platform; only the active one is shown. */
    const sections = {};
    platforms.forEach((platform) => {
        sections[platform] = buildSection(platform);
        editor.append(sections[platform].root);
    });

    notes.forEach((n) => body.append(el("div", "note note-info", n)));
    const status = el("span", "muted small composer-status");
    const save = button("Guardar", "btn btn-primary", async () => {
        platforms.forEach((p) => {
            if (sections[p].chips.flush()) model.texts[p].tags = sections[p].chips.get();
        });
        await onSave(JSON.parse(JSON.stringify(model.texts)));
        closeModal();
    });
    save.title = "Guardar (⌘ Enter)";
    const bar = el("div", "actions-bar composer-actions");
    bar.append(save, button("Cancelar", "btn btn-ghost", () => {
        if (!dirty() || confirm("Hay cambios sin guardar. ¿Cerrar y descartarlos?")) closeModal();
    }), status);
    body.append(bar);
    body.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            save.click();
        }
    });

    /* ---------------- tools ---------------- */
    const fixBtn = button("🪄 Arreglar lo automático", "btn btn-ghost btn-sm", autofix);
    fixBtn.title = "Signos «¿¡», contador «4/50», hashtags dentro del texto, repetidos, en inglés o #fyp";
    const ai = options.ai;
    const canPropose = !!(ai && ai.enabled && ai.ollama && ai.ollama.reachable);
    const proposeBtn = button("✨ Proponer con IA", "btn btn-ghost btn-sm", propose);
    proposeBtn.title = canPropose
        ? `Ollama (${ai.ollama.model}) escribe otra versión desde el guion o la transcripción. No se guarda nada hasta que la uses.`
        : "Necesita Ollama en marcha (Ajustes → Sistema).";
    proposeBtn.disabled = !canPropose;
    tools.append(fixBtn, proposeBtn);

    function drawTabs() {
        tabs.hidden = platforms.length < 2;
        tabs.replaceChildren(...platforms.map((p) => {
            const tab = el("button", `composer-tab${p === model.active ? " is-active" : ""}`);
            tab.type = "button";
            tab.append(el("span", `lib-plat ${p}`, LABEL[p]));
            const counts = model.reports[p]?.counts;
            if (counts && (counts.error || counts.warn)) {
                tab.append(el("span", `tab-count ${counts.error ? "error" : "warn"}`,
                    String(counts.error + counts.warn)));
            } else if (counts) {
                tab.append(el("span", "tab-count ok", "✓"));
            }
            tab.addEventListener("click", () => {
                model.active = p;
                Object.entries(sections).forEach(([k, s]) => {
                    s.root.hidden = k !== p;
                });
                drawTabs();
                drawSide();
                if (!model.suggestions[p]) loadSuggestions(p);
            });
            return tab;
        }));
    }

    /* ---------------- one platform ---------------- */
    function buildSection(platform) {
        const limits = LIMITS[platform];
        const t = model.texts[platform];
        const root = el("div", "composer-section");

        const field = (label, input, counterNode, hint) => {
            const wrap = el("label", "block-field composer-field");
            const top = el("span", "field-head");
            top.append(el("span", null, label));
            if (counterNode) top.append(counterNode);
            wrap.append(top, input);
            if (hint) wrap.append(hint);
            return wrap;
        };

        const title = el("input");
        title.type = "text";
        title.value = t.title;
        title.placeholder = platform === "youtube"
            ? "Obligatorio en YouTube: el tema concreto, de 30 a 80 caracteres"
            : "TikTok apenas lo usa; se envía igualmente";
        const titleCount = el("span", "counter");

        const description = el("textarea");
        description.rows = 6;
        description.value = t.description;
        description.placeholder = "Una idea por línea: gancho con las palabras que alguien buscaría, "
            + "un dato concreto del vídeo y una pregunta para comentar.";
        const descCount = el("span", "counter");
        const descHint = el("span", "muted small field-hint");
        if (templates) {
            descHint.textContent = "Admite variantes {a|b|c} y campos [fecha] [hora] [dia] [mes] [archivo] "
                + "[hashtags]. Los hashtags de abajo se añaden al final al publicar.";
        } else {
            descHint.textContent = "Los hashtags de abajo se añaden al final al publicar.";
        }
        const foldLine = el("div", "fold-line muted small");

        const tagsCount = el("span", "counter");
        const chips = chipInput({
            tags: t.tags,
            placeholder: t.tags.length ? "Otro…" : "#hashtag y Enter (o pega varios)",
            onChange: (tags) => {
                t.tags = tags;
                changed(platform);
                drawSuggestions(platform);
            },
        });
        const fixedLine = el("div", "muted small field-hint");
        if (fixed.length) fixedLine.textContent = `Se añaden siempre (Ajustes): ${fixed.join(" ")}`;
        const suggestBox = el("div", "suggest-row");

        const comment = el("textarea");
        comment.rows = 2;
        comment.value = t.first_comment;
        comment.placeholder = "Una pregunta para fijar bajo el vídeo (opcional)";
        const commentCount = el("span", "counter");

        const tagsField = el("div", "block-field composer-field");
        const tagsTop = el("span", "field-head");
        tagsTop.append(el("span", null, platform === "youtube" ? "Hashtags y etiquetas" : "Hashtags"), tagsCount);
        tagsField.append(tagsTop, chips.root, chips.note, fixedLine, suggestBox);

        const titleField = field("Título", title, titleCount);
        const descField = field("Descripción", description, descCount, descHint);
        descField.append(foldLine);
        root.append(titleField, descField, tagsField, field("Comentario fijado", comment, commentCount));

        if (platforms.length > 1 && platform === "youtube") {
            const copy = button("Copiar de TikTok", "btn btn-ghost btn-sm", () => {
                const from = model.texts.tiktok;
                t.description = from.description;
                t.tags = [...from.tags];
                t.first_comment = from.first_comment;
                if (!t.title.trim()) t.title = from.title;
                syncInputs(platform);
                changed(platform);
                toast("Copiados la descripción, los hashtags y el comentario de TikTok");
            });
            copy.title = "Descripción, hashtags y comentario fijado de TikTok (y su título si aquí no hay)";
            root.prepend(el("div", "composer-copy"));
            root.firstChild.append(copy);
        }

        const onInput = (key, node) => node.addEventListener("input", () => {
            t[key] = node.value;
            changed(platform);
        });
        onInput("title", title);
        onInput("description", description);
        onInput("first_comment", comment);

        const counters = () => {
            counter(titleCount, t.title.trim().length, limits.title);
            const tags = withFixed(t.tags, fixed);
            const total = t.description.trim().length + (tags.length ? tags.join(" ").length + 1 : 0);
            counter(descCount, total, limits.caption);
            descCount.title = "Con los hashtags que se añaden al publicar";
            const hook = firstLine(t.description);
            foldLine.replaceChildren();
            if (hook) {
                const size = el("span", "counter");
                counter(size, hook.length, limits.fold);
                foldLine.append(document.createTextNode("Primera línea (lo que se ve antes de «más»): "), size);
            } else {
                foldLine.textContent = "La primera línea es lo que se ve antes de «más».";
            }
            if (platform === "youtube") {
                // Like hashtags.plain_tags: the list, then the ones written in the text.
                const plain = [...tags, ...inlineTags(t.description)].map((x) => x.replace(/^#/, ""));
                const chars = [...new Map(plain.map((x) => [foldTag(x), x])).values()].join(",").length;
                counter(tagsCount, chars, limits.tagChars);
                tagsCount.title = "Caracteres de las etiquetas de YouTube (máximo 500)";
            } else {
                const counted = countedTags(t, fixed);
                tagsCount.textContent = `${Math.min(counted.size, COUNTED)}/${COUNTED} cuentan`;
                tagsCount.classList.toggle("over", withFixed(t.tags, fixed).length > COUNTED);
                tagsCount.title = "TikTok cuenta los cinco primeros hashtags del post, contando los fijos de Ajustes";
                chips.mark(counted);
            }
            counter(commentCount, t.first_comment.trim().length, limits.comment);
        };

        const syncInputs = () => {
            title.value = t.title;
            description.value = t.description;
            comment.value = t.first_comment;
            chips.set(t.tags);
            counters();
        };

        root.hidden = platform !== model.active;
        return {
            root, chips, counters, syncInputs, suggestBox,
            focus: {title, description, tags: chips.input, first_comment: comment},
        };
    }

    function changed(platform) {
        sections[platform].counters();
        status.textContent = dirty() ? "Cambios sin guardar" : "";
        scheduleReview(platform);
    }

    /* ---------------- review ---------------- */
    function payload(platform) {
        const t = model.texts[platform];
        return {
            platform, title: t.title, description: t.description, tags: t.tags,
            first_comment: t.first_comment, ...context
        };
    }

    function scheduleReview(platform) {
        clearTimeout(model.timers[platform]);
        model.timers[platform] = setTimeout(() => review(platform), REVIEW_DELAY);
    }

    async function review(platform) {
        if (model.unavailable) return;
        const seq = (model.seq[platform] || 0) + 1;
        model.seq[platform] = seq;
        try {
            const report = await api("/api/text/check", {method: "POST", body: JSON.stringify(payload(platform))});
            if (model.seq[platform] !== seq) return;
            model.reports[platform] = report;
        } catch (err) {
            // An app started before this version has no /api/text: the editor still works.
            if (/not found/i.test(err.message)) model.unavailable = true;
            model.reports[platform] = null;
        }
        drawTabs();
        if (platform === model.active) drawSide();
    }

    function drawSide() {
        const platform = model.active;
        const report = model.reports[platform];
        side.replaceChildren();
        if (model.unavailable) {
            side.append(el("div", "note note-warn",
                "La revisión y la vista previa necesitan la versión nueva del servidor: reinicia la aplicación."));
            fixBtn.disabled = true;
            return;
        }
        side.append(previewCard(platform, report), reviewCard(platform, report));
        fixBtn.disabled = !(report && report.fixable);
    }

    function previewCard(platform, report) {
        const card = el("div", `preview-card ${platform}`);
        card.append(el("div", "preview-label", `Así saldrá en ${LABEL[platform]}`));
        if (!report) {
            card.append(el("div", "muted small", "Revisando…"));
            return card;
        }
        const p = report.preview;
        if (platform === "youtube") {
            card.append(el("div", "preview-title", p.title || "— sin título —"));
        } else if (handle) {
            card.append(el("div", "preview-handle", handle));
        }
        const caption = p.caption || "";
        const long = caption.length > p.visible.length + 2 || caption.includes("\n");
        let text = highlighted(long ? p.visible : caption);
        card.append(text);
        if (long) {
            // What shows before «más», and the whole post on demand.
            const more = el("button", "preview-more", "… más");
            more.type = "button";
            let open = false;
            more.addEventListener("click", () => {
                open = !open;
                const next = highlighted(open ? caption : p.visible);
                text.replaceWith(next);
                text = next;
                more.textContent = open ? "menos" : "… más";
            });
            card.append(more);
        }
        if (platform === "tiktok" && p.counted_tags.length) {
            const line = el("div", "preview-counted muted small");
            line.append(document.createTextNode(`Cuentan: ${p.counted_tags.join(" ")}`));
            if (p.ignored_tags.length) line.append(el("span", "bad", ` · No cuentan: ${p.ignored_tags.join(" ")}`));
            card.append(line);
        }
        if (platform === "youtube" && (p.youtube_tags || []).length) {
            card.append(el("div", "preview-counted muted small",
                `Etiquetas: ${p.youtube_tags.join(", ")}`));
        }
        return card;
    }

    function reviewCard(platform, report) {
        const card = el("div", "review-card");
        const counts = report?.counts;
        const title = el("div", "review-head");
        if (!report) {
            title.textContent = "Revisión";
            card.append(title);
            return card;
        }
        const total = counts.error + counts.warn + counts.tip;
        if (!total) {
            title.append(el("span", "review-ok", "✓ Nada que corregir"));
        } else {
            ["error", "warn", "tip"].filter((l) => counts[l]).forEach((level) => {
                title.append(el("span", `review-count ${level}`,
                    `${LEVEL_ICON[level]} ${plural(counts[level], LEVEL_NAMES[level])}`));
            });
        }
        card.append(title);
        const list = el("ul", "review-list");
        report.issues.forEach((issue) => {
            const item = el("li", `review-item ${issue.level}`);
            const go = el("button", "review-go");
            go.type = "button";
            go.append(el("span", "review-icon", LEVEL_ICON[issue.level]),
                el("span", "review-field", FIELD_LABEL[issue.field] || issue.field),
                el("span", "review-msg", issue.message));
            if (issue.fixable) go.append(el("span", "review-fixable", "arreglable"));
            go.title = "Ir al campo";
            go.addEventListener("click", () => sections[platform].focus[issue.field]?.focus());
            item.append(go);
            list.append(item);
        });
        card.append(list);
        return card;
    }

    /* ---------------- fixes and proposals ---------------- */
    async function autofix() {
        const platform = model.active;
        const res = await api("/api/text/fix", {method: "POST", body: JSON.stringify(payload(platform))});
        const t = model.texts[platform];
        Object.assign(t, {
            title: res.texts.title, description: res.texts.description,
            tags: res.texts.tags, first_comment: res.texts.first_comment
        });
        sections[platform].syncInputs();
        model.reports[platform] = res.report;
        status.textContent = dirty() ? "Cambios sin guardar" : "";
        drawTabs();
        drawSide();
        drawSuggestions(platform);
        toast(res.changes.length ? res.changes.join(" · ") : "No había nada que arreglar sin el modelo");
    }

    async function propose() {
        const platform = model.active;
        proposalBox.hidden = false;
        proposalBox.replaceChildren(el("div", "proposal-wait",
            `✨ Escribiendo con ${ai.ollama.model}… Si la biblioteca está escribiendo textos, espera a que termine el que está en marcha (uno a la vez).`));
        try {
            const t = model.texts[platform];
            model.proposal = await api("/api/text/suggest", {
                method: "POST",
                body: JSON.stringify({platform, ...context, title: t.title, description: t.description}),
            });
        } catch (err) {
            proposalBox.replaceChildren(el("div", "note note-warn", err.message));
            const close = button("Cerrar", "btn btn-ghost btn-sm", () => {
                proposalBox.hidden = true;
            });
            proposalBox.append(close);
            return;
        }
        drawProposal(platform);
    }

    function drawProposal(platform) {
        const p = model.proposal;
        proposalBox.replaceChildren();
        const top = el("div", "proposal-head");
        const from = {
            "ficha": "desde la ficha .txt", "transcripción": "desde la transcripción",
            "tu texto": "mejorando tu texto"
        }[p.source] || p.source;
        top.append(el("strong", null, `✨ Propuesta para ${LABEL[platform]}`),
            el("span", "muted small", `${p.model} · ${from} · ${p.seconds} s`));
        proposalBox.append(top);
        if (p.title) proposalBox.append(el("div", "proposal-title", p.title));
        proposalBox.append(highlighted(p.description));
        if (p.tags.length) proposalBox.append(el("div", "proposal-tags", p.tags.join(" ")));
        if (p.first_comment) proposalBox.append(el("div", "muted small", `Comentario: ${p.first_comment}`));
        (p.changes || []).forEach((c) => proposalBox.append(el("div", "muted small", `· ${c}`)));
        const t = model.texts[platform];
        const use = (fields) => () => {
            if (fields.includes("title") && p.title) t.title = p.title;
            if (fields.includes("description")) t.description = p.description;
            if (fields.includes("tags")) t.tags = p.tags.map(normalizeTag).filter(Boolean);
            if (fields.includes("first_comment") && p.first_comment) t.first_comment = p.first_comment;
            sections[platform].syncInputs();
            changed(platform);
            drawSuggestions(platform);
            proposalBox.hidden = true;
            toast("Propuesta aplicada: revísala y pulsa Guardar");
        };
        const actions = el("div", "actions-bar");
        actions.append(
            button("Usar esta propuesta", "btn btn-primary btn-sm",
                use(["title", "description", "tags", "first_comment"])),
            button("Sólo el texto", "btn btn-ghost btn-sm", use(["title", "description"])),
            button("Sólo los hashtags", "btn btn-ghost btn-sm", use(["tags"])),
            button("Descartar", "btn btn-ghost btn-sm", () => {
                proposalBox.hidden = true;
            }),
        );
        proposalBox.append(actions);
    }

    /* ---------------- suggestions ---------------- */
    async function loadSuggestions(platform) {
        if (model.unavailable) return;
        const t = model.texts[platform];
        const params = new URLSearchParams({
            platform, current: t.tags.join(" "),
            text: `${t.title}\n${t.description}`.slice(0, 1500)
        });
        Object.entries(context).forEach(([k, v]) => v != null && params.set(k, String(v)));
        try {
            model.suggestions[platform] = await api(`/api/text/hashtags?${params}`);
        } catch {
            model.suggestions[platform] = {suggestions: []};
        }
        drawSuggestions(platform);
    }

    function drawSuggestions(platform) {
        const box = sections[platform].suggestBox;
        const data = model.suggestions[platform];
        const present = new Set(model.texts[platform].tags.map(foldTag));
        const offers = (data?.suggestions || []).filter((s) => !present.has(foldTag(s.tag)));
        box.replaceChildren();
        if (!offers.length) return;
        box.append(el("span", "muted small", "Sugerencias:"));
        offers.slice(0, 12).forEach((s) => {
            const chip = el("button", `suggest-chip ${s.source}`, `${SOURCE_ICON[s.source] || "+"} ${s.tag}`);
            chip.type = "button";
            chip.title = `${s.detail}. Clic para añadirlo.`;
            chip.addEventListener("click", () => {
                sections[platform].chips.add(s.tag);
            });
            box.append(chip);
        });
    }

    /* ---------------- open ---------------- */
    openModal(options.title, body, {
        wide: true,
        className: "is-composer",
        beforeClose: () => !dirty() || confirm("Hay cambios sin guardar. ¿Cerrar y descartarlos?"),
    });
    drawTabs();
    platforms.forEach((p) => {
        sections[p].counters();
        review(p);
    });
    drawSide();
    loadSuggestions(model.active);
    const start = options.focus === "tags" ? sections[model.active].focus.tags
        : sections[model.active].focus.description;
    setTimeout(() => start.focus(), 30);
}

/** The fixed hashtags of Ajustes (a string in /api/state) as a list. */
export function fixedHashtags(settings) {
    return splitTags(settings?.hashtags || "").map(normalizeTag).filter(Boolean);
}
