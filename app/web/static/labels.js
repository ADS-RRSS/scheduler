/* Folder labels: their colours, and the picker that combines folders and labels
   (the queue's «Añadir desde la biblioteca» and the Cola page's folder filter). */

import {el, state} from "./shared.js";

/* The --acc-0…7 of app.css (what a label gets from its name), then a few more to pick from. */
export const AUTO_COLORS = ["#8b5cf6", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899", "#14b8a6", "#f97316", "#84cc16"];
export const LABEL_PALETTE = [...AUTO_COLORS, "#ef4444", "#3b82f6", "#a16207", "#64748b"];
/* label -> #rrggbb picked by the user (GET /api/library: labels[].color). */
export const labelColors = new Map();

export function rememberColors(overview) {
    (overview?.labels || []).forEach((l) => {
        if (l.color) labelColors.set(l.label, l.color);
        else labelColors.delete(l.label);
    });
}

export function autoColor(label) {
    let hash = 0;
    for (const ch of label) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
    return AUTO_COLORS[hash % AUTO_COLORS.length];
}

/** Each label keeps one colour everywhere: the one picked for it, or one from its name. */
export function labelColor(label) {
    return labelColors.get(label) || state.data?.library?.label_colors?.[label] || autoColor(label);
}

/** Dark text on a light colour, white on a dark one (for a filled chip). */
function inkOn(hex) {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6 ? "#16161c" : "#fff";
}

export function paint(node, color) {
    node.style.setProperty("--label-color", color);
    node.style.setProperty("--label-ink", inkOn(color));
    return node;
}

export function labelChip(label, tag = "span", color = labelColor(label)) {
    return paint(el(tag, "folder-label", label), color);
}

export function labelDots(labels) {
    const dots = el("span", "lib-folder-dots");
    labels.forEach((label) => {
        const dot = paint(el("span", "lib-folder-dot"), labelColor(label));
        dot.title = label;
        dots.append(dot);
    });
    return dots;
}

/** One label's two switches: «con» (✓) and «sin» (✕); a second click turns it off. */
function labelSwitches(mode, titles, onPick) {
    const box = el("span", "lib-lf-switch");
    [["include", "✓"], ["exclude", "✕"]].forEach(([value, text]) => {
        const b = el("button", `lib-lf-btn is-${value}${mode === value ? " is-on" : ""}`, text);
        b.type = "button";
        b.title = mode === value ? "Quitar del filtro" : titles[value];
        b.setAttribute("aria-pressed", String(mode === value));
        b.addEventListener("click", () => onPick(mode === value ? "" : value));
        box.append(b);
    });
    return box;
}

/**
 * The labels as a small table: each one ✓ (the folders must carry it) or ✕
 * (must not), and a last row for the folders without any label (✓ adds them,
 * ✕ leaves them out). ``labels`` is [{label, count}], ``bare`` how many folders
 * carry none; ``onChange`` gets {labels, exclude} or {unlabeled}; ``onClear``,
 * when given, puts a «Quitar filtro» in the head.
 */
export function labelFilterRows({labels, bare, scope, onChange, onClear}) {
    const exclude = scope.exclude || [];
    const unlabeled = scope.unlabeled || "";
    const head = el("div", "lib-lf-head");
    const left = el("span");
    left.append(el("span", "lib-lf-title", "Etiquetas"));
    if (onClear) {
        const clear = el("button", "lib-lf-clear", "Quitar filtro");
        clear.type = "button";
        clear.hidden = !scope.labels.length && !exclude.length && !unlabeled;
        clear.addEventListener("click", onClear);
        left.append(clear);
    }
    const cols = el("span", "lib-lf-switch");
    cols.append(el("span", "lib-lf-col", "con"), el("span", "lib-lf-col", "sin"));
    head.append(left, cols);

    const rows = labels.map(({label, count}) => {
        const mode = scope.labels.includes(label) ? "include" : exclude.includes(label) ? "exclude" : "";
        const row = el("div", `lib-lf-row${mode ? ` is-${mode}` : ""}`);
        const name = labelChip(label);
        name.append(el("small", null, ` ${count}`));
        row.append(name, labelSwitches(mode, {
            include: `Sólo las carpetas con «${label}»`,
            exclude: `Fuera las carpetas con «${label}»`,
        }, (next) => onChange({
            labels: [...scope.labels.filter((l) => l !== label), ...(next === "include" ? [label] : [])],
            exclude: [...exclude.filter((l) => l !== label), ...(next === "exclude" ? [label] : [])],
        })));
        return row;
    });

    if (bare || unlabeled) {
        const row = el("div", `lib-lf-row is-bare${unlabeled ? ` is-${unlabeled}` : ""}`);
        const name = el("span", "lib-lf-bare", "sin etiquetas");
        name.append(el("small", null, ` ${bare}`));
        row.append(name, labelSwitches(unlabeled, {
            include: scope.labels.length ? "Sumar también las carpetas sin etiquetas" : "Sólo las carpetas sin etiquetas",
            exclude: "Fuera las carpetas sin etiquetas",
        }, (next) => onChange({unlabeled: next})));
        rows.push(row);
    }
    return [head, ...rows];
}

/** With two or more ✓: the folders carrying all of them, or any. */
export function labelMatch(scope, onPick) {
    const match = el("div", "scope-match lib-lf-match");
    match.hidden = scope.labels.length < 2;
    match.append(el("span", "muted small", "Con"),
        ...[["all", "todas"], ["any", "alguna"]].map(([value, text]) => {
            const b = el("button", `chip${scope.match === value ? " is-active" : ""}`, text);
            b.type = "button";
            b.title = value === "all" ? "Las carpetas que tienen todas las marcadas con ✓"
                : "Las carpetas que tienen al menos una de las marcadas con ✓";
            b.addEventListener("click", () => onPick(value));
            return b;
        }),
        el("span", "muted small", "las ✓"));
    return match;
}

/* ------------------------------------------------------------------ */
/* Folders + labels                                                    */

/* ------------------------------------------------------------------ */

/* The folders ticked (none = every folder), narrowed by the labels ticked:
   the folders carrying all of them (match "all") or at least one ("any").
   Optional, only the Biblioteca's sidebar sets them: «exclude», labels that
   leave out the folders carrying any of them, and «unlabeled», "include" (the
   folders without labels join the ones the labels keep) or "exclude". */
export function emptyScope() {
    return {folders: [], labels: [], match: "all"};
}

export function isEmptyScope(scope) {
    return !scope || (!scope.folders.length && !scope.labels.length && !scope.exclude?.length && !scope.unlabeled);
}

/** Does a folder with these labels get through the scope's labels? */
function labelsPass(scope, have) {
    if ((scope.exclude || []).some((l) => have.includes(l))) return false;
    if (scope.unlabeled === "exclude" && !have.length) return false;
    const bare = scope.unlabeled === "include";
    if (!scope.labels.length && !bare) return true;
    const test = scope.match === "any" ? "some" : "every";
    return (scope.labels.length > 0 && scope.labels[test]((l) => have.includes(l))) || (bare && !have.length);
}

/** The names of the folders a scope keeps, or null when it keeps them all. */
export function scopeFolders(scope, folders) {
    if (isEmptyScope(scope)) return null;
    const base = scope.folders.length ? folders.filter((f) => scope.folders.includes(f.folder)) : folders;
    return new Set(base.filter((f) => labelsPass(scope, f.labels || [])).map((f) => f.folder));
}

/** The scope as the API takes it (query string or JSON body); {} for everything. */
export function scopeParams(scope) {
    if (isEmptyScope(scope)) return {};
    const labelled = scope.labels.length || scope.exclude?.length || scope.unlabeled;
    if (!labelled && scope.folders.length === 1) return {folder: scope.folders[0]};
    return {
        ...(scope.folders.length ? {folders: scope.folders} : {}),
        ...(scope.labels.length ? {labels: scope.labels, match: scope.match} : {}),
        ...(scope.exclude?.length ? {exclude: scope.exclude} : {}),
        ...(scope.unlabeled ? {unlabeled: scope.unlabeled} : {}),
    };
}

/** Into URLSearchParams, a list as repeated keys (?folders=a&folders=b). */
export function appendScope(params, scope) {
    Object.entries(scopeParams(scope)).forEach(([key, value]) =>
        (Array.isArray(value) ? value : [value]).forEach((v) => params.append(key, v)));
    return params;
}

function joined(names, word) {
    const quoted = names.map((n) => `«${n}»`);
    return quoted.length === 1 ? quoted[0] : `${quoted.slice(0, -1).join(", ")} ${word} ${quoted.at(-1)}`;
}

/** « de «a» y «b»» / « con «serie» y «terminada»», after a count; "" for everything. */
export function scopeSuffix(scope) {
    if (isEmptyScope(scope)) return "";
    const of = scope.folders.length ? ` de ${joined(scope.folders, "y")}` : "";
    const tagged = scope.labels.length ? ` con ${joined(scope.labels, scope.match === "any" ? "o" : "y")}` : "";
    const without = scope.exclude?.length ? ` sin ${joined(scope.exclude, "ni")}` : "";
    const bare = {include: " (o sin etiquetas)", exclude: " con alguna etiqueta"}[scope.unlabeled] || "";
    return of + tagged + without + bare;
}

/** A short name for the picker's button. */
function scopeSummary(scope) {
    const where = !scope.folders.length ? "Todas las carpetas"
        : scope.folders.length === 1 ? `📁 ${scope.folders[0]}` : `📁 ${scope.folders.length} carpetas`;
    const tags = [
        scope.labels.join(scope.match === "any" ? " o " : " + "),
        scope.exclude?.length && `sin ${scope.exclude.join(", ")}`,
        scope.unlabeled === "include" && (scope.labels.length ? "o sin etiquetas" : "sin etiquetas"),
        scope.unlabeled === "exclude" && "con alguna",
    ].filter(Boolean).join(" · ");
    return tags ? `${where} · 🏷️ ${tags}` : where;
}

/**
 * A button that opens the folders (several, or all) and the labels that narrow them.
 *
 * ``update(folders, scope)`` gives it the folders to offer — {folder, labels,
 * count, note?} — and the current choice; ``onChange(scope)`` gets every change.
 * ``unit`` names what ``count`` counts, for the result line; ``onOpen(open)``
 * lets the page hold its layout still while the panel is open; ``allNote``
 * writes the «Todas las carpetas» row's count, as each folder's ``note`` does.
 */
export function scopePicker({
                                onChange, onOpen = () => {
    }, unit = "vídeo(s)", ariaLabel = "Carpetas y etiquetas",
                                allNote = (folders) => String(folders.reduce((n, f) => n + (f.count || 0), 0)),
                                negate = false,
                            }) {
    const root = el("div", "scope-picker");
    const toggle = el("button", "scope-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-haspopup", "true");
    toggle.setAttribute("aria-label", ariaLabel);
    const panel = el("div", "scope-panel");
    panel.hidden = true;
    root.append(toggle, panel);

    let folders = [];
    let scope = emptyScope();
    let drawn = "";

    // Fixed under its button, so a modal that scrolls (or is short) cannot clip it.
    const place = () => {
        if (!root.isConnected) {
            removeEventListener("scroll", place, true);
            removeEventListener("resize", place);
            return;
        }
        if (panel.hidden) return;
        const r = toggle.getBoundingClientRect();
        // Two columns (labels | folders) when there are labels to pick.
        const width = Math.min(panel.classList.contains("has-labels") ? 680 : 384, innerWidth - 32);
        panel.style.width = `${width}px`;
        panel.style.left = `${Math.max(16, Math.min(r.left, innerWidth - width - 16))}px`;
        panel.style.top = `${r.bottom + 4}px`;
        panel.style.maxHeight = `${Math.max(220, innerHeight - r.bottom - 20)}px`;
    };
    const open = (yes) => {
        panel.hidden = !yes;
        toggle.setAttribute("aria-expanded", String(yes));
        root.classList.toggle("is-open", yes);
        if (yes) {
            place();
            addEventListener("scroll", place, true);
            addEventListener("resize", place);
        } else {
            removeEventListener("scroll", place, true);
            removeEventListener("resize", place);
        }
        onOpen(yes);
    };
    toggle.addEventListener("click", () => open(panel.hidden));
    const outside = (e) => {
        if (!root.isConnected) {
            document.removeEventListener("pointerdown", outside);
            document.removeEventListener("keydown", escape);
            removeEventListener("scroll", place, true);
            removeEventListener("resize", place);
            return;
        }
        if (!panel.hidden && !root.contains(e.target)) open(false);
    };
    const escape = (e) => {
        if (e.key === "Escape" && !panel.hidden) {
            e.stopPropagation();
            open(false);
            toggle.focus();
        }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);

    const set = (next) => {
        scope = next;
        draw(true);
        onChange({
            ...scope, folders: [...scope.folders], labels: [...scope.labels],
            ...(scope.exclude ? {exclude: [...scope.exclude]} : {}),
        });
    };

    /** A change of labels: the folders ticked that no longer carry them are unticked,
     so the labels pick the folders and the ticks only narrow what they let through. */
    const setLabels = (next) => {
        const merged = {...scope, ...next};
        const keep = scopeFolders({...merged, folders: []}, folders);
        if (keep) merged.folders = merged.folders.filter((f) => keep.has(f));
        set(merged);
    };

    const search = el("input", "scope-search");
    search.type = "search";
    search.placeholder = "Buscar carpeta…";
    search.addEventListener("input", () => draw(true));
    const folderList = el("div", "scope-folders");
    // negate: the labels as the ✓ con / ✕ sin table of the Biblioteca's sidebar.
    const labelBox = el("div", negate ? "scope-labels is-table" : "scope-labels");
    let matchBox = el("div", "scope-match");
    const result = el("div", "scope-result muted small");
    const clear = el("button", "btn btn-ghost btn-sm", "Limpiar");
    clear.type = "button";
    clear.addEventListener("click", () => set(negate ? {...emptyScope(), exclude: [], unlabeled: ""} : emptyScope()));
    const done = el("button", "btn btn-primary btn-sm", "Listo");
    done.type = "button";
    done.addEventListener("click", () => open(false));
    const foot = el("div", "scope-foot");
    foot.append(result, clear, done);
    const folderHead = el("div", "scope-head");
    const hiddenNote = el("div", "scope-hidden muted small");
    const labelCol = el("div", "scope-col scope-col-labels");
    labelCol.append(...(negate ? [] : [el("div", "scope-head", "Etiquetas")]), labelBox, matchBox);
    const folderCol = el("div", "scope-col");
    folderCol.append(folderHead, search, folderList, hiddenNote);
    const cols = el("div", "scope-cols");
    cols.append(labelCol, folderCol);
    panel.append(cols, foot);

    const check = (text, checked, onToggle, extra) => {
        const row = el("label", "scope-row");
        const box = el("input");
        box.type = "checkbox";
        box.checked = checked;
        box.addEventListener("change", () => onToggle(box.checked));
        row.append(box, ...extra, el("span", "scope-row-name", text));
        return row;
    };

    function draw(force = false) {
        const signature = JSON.stringify([folders, scope, search.value]);
        if (!force && signature === drawn) return;
        drawn = signature;
        const keep = scopeFolders(scope, folders);
        const kept = keep ? folders.filter((f) => keep.has(f.folder)) : folders;
        const total = kept.reduce((n, f) => n + (f.count || 0), 0);

        toggle.replaceChildren(el("span", "scope-toggle-text", scopeSummary(scope)));
        if (scope.labels.length) toggle.append(labelDots(scope.labels));
        toggle.append(el("span", "scope-caret", "▾"));
        toggle.classList.toggle("is-scoped", !isEmptyScope(scope));
        toggle.title = isEmptyScope(scope) ? "Todas las carpetas"
            : `${kept.length} carpeta(s)${scopeSuffix(scope)} · ${total} ${unit}`;

        // Only the folders the labels let through (and the ticked ones, to untick them).
        const labelled = scopeFolders({...scope, folders: []}, folders);
        const passing = labelled ? folders.filter((f) => labelled.has(f.folder)) : folders;
        const offered = labelled
            ? folders.filter((f) => labelled.has(f.folder) || scope.folders.includes(f.folder)) : folders;
        folderHead.replaceChildren(document.createTextNode("Carpetas"),
            ...(labelled ? [el("span", "scope-head-note", ` · ${passing.length} de ${folders.length} con estas etiquetas`)] : []));
        hiddenNote.hidden = !labelled || passing.length === folders.length;
        hiddenNote.textContent = `${folders.length - passing.length} carpeta(s) ocultas por las etiquetas.`;
        search.hidden = offered.length < 8;
        const needle = search.value.trim().toLowerCase();
        const scroll = folderList.scrollTop;
        folderList.replaceChildren(
            check(labelled ? "Todas las de estas etiquetas" : "Todas las carpetas", !scope.folders.length,
                () => set({...scope, folders: []}), [el("span", "scope-count", allNote(passing))]),
            ...(labelled && !passing.length
                ? [el("div", "scope-none muted small", "Ninguna carpeta cumple estas etiquetas.")] : []),
            ...offered.filter((f) => !needle || f.folder.toLowerCase().includes(needle)).map((f) => {
                const row = check(f.folder, scope.folders.includes(f.folder), (on) => set({
                    ...scope,
                    folders: on ? [...scope.folders, f.folder] : scope.folders.filter((x) => x !== f.folder),
                }), [el("span", "scope-count", f.note ?? String(f.count))]);
                if (f.labels?.length) row.querySelector(".scope-row-name").prepend(labelDots(f.labels));
                // Ticked, but the labels leave it out.
                if (keep && scope.folders.includes(f.folder) && !keep.has(f.folder)) {
                    row.classList.add("is-excluded");
                    row.title = "Marcada, pero no tiene las etiquetas elegidas";
                }
                return row;
            }));
        folderList.scrollTop = scroll;

        const labels = [...new Set(folders.flatMap((f) => f.labels || []))].sort();
        [...scope.labels, ...(scope.exclude || [])].filter((l) => !labels.includes(l)).forEach((l) => labels.push(l));
        const carrying = (label) => folders.filter((f) => (f.labels || []).includes(label)).length;
        if (negate) {
            labelBox.replaceChildren(...(labels.length ? labelFilterRows({
                labels: labels.map((label) => ({label, count: carrying(label)})),
                bare: folders.filter((f) => !f.labels?.length).length,
                scope,
                onChange: setLabels,
            }) : []));
            matchBox.replaceWith(matchBox = labelMatch(scope, (value) => setLabels({match: value})));
        } else {
            labelBox.replaceChildren(...(labels.length ? labels.map((label) => {
                const on = scope.labels.includes(label);
                const chip = labelChip(label, "button");
                chip.type = "button";
                chip.classList.toggle("is-active", on);
                chip.setAttribute("aria-pressed", String(on));
                chip.append(el("small", null, ` ${carrying(label)}`));
                chip.title = on ? "Quitar" : `Sólo las carpetas con «${label}»`;
                chip.addEventListener("click", () => setLabels({
                    labels: on ? scope.labels.filter((x) => x !== label) : [...scope.labels, label],
                }));
                return chip;
            }) : []));
            matchBox.hidden = scope.labels.length < 2;
            matchBox.replaceChildren(el("span", "muted small", "Con "),
                ...[["all", "todas"], ["any", "cualquiera"]].map(([value, text]) => {
                    const b = el("button", `chip${scope.match === value ? " is-active" : ""}`, text);
                    b.type = "button";
                    b.addEventListener("click", () => setLabels({match: value}));
                    return b;
                }),
                el("span", "muted small", " las etiquetas elegidas"));
        }

        // Without labels anywhere, just the folders.
        const hasLabels = labels.length > 0;
        labelCol.hidden = !hasLabels;
        if (panel.classList.contains("has-labels") !== hasLabels) {
            panel.classList.toggle("has-labels", hasLabels);
            place();
        }

        result.textContent = isEmptyScope(scope)
            ? `Todas: ${folders.length} carpeta(s) · ${total} ${unit}`
            : `→ ${kept.length} carpeta(s) · ${total} ${unit}`;
        clear.disabled = isEmptyScope(scope);
    }

    return {
        node: root,
        update(nextFolders, nextScope) {
            folders = nextFolders;
            scope = nextScope || emptyScope();
            draw();
        },
        open,
    };
}
