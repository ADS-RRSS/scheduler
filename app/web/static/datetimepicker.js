/* A day-and-time picker: a button that opens a month calendar and the hour and
   minute next to it. No library: it speaks the app's colours and Spanish, and
   it works without internet like the rest of the page. */

import {el} from "./shared.js";

const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
const WEEKDAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
const WEEKDAY_ABBR = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
/* Monday first, as a Spanish calendar. */
const HEAD = ["L", "M", "X", "J", "V", "S", "D"];
const MINUTE_STEPS = Array.from({length: 12}, (_, i) => i * 5);

const pad = (n) => String(n).padStart(2, "0");

/** YYYY-MM-DD of a Date, read in local time (the dates here are only calendar days). */
function isoOf(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function dateOf(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d, 12);
}

function nextDay(iso) {
    const date = dateOf(iso);
    date.setDate(date.getDate() + 1);
    return isoOf(date);
}

export function toMinutes(hhmm) {
    if (!hhmm) return 0;
    const [h, m] = hhmm.split(":").map(Number);
    return h * 60 + m;
}

export function hhmm(minutes) {
    return `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
}

/** The day and the minute of the day it is now in a time zone ({day: YYYY-MM-DD, minutes}). */
export function nowIn(timeZone) {
    const at = new Date();
    try {
        const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
            timeZone, year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", hourCycle: "h23",
        }).formatToParts(at).map((p) => [p.type, p.value]));
        return {
            day: `${parts.year}-${parts.month}-${parts.day}`,
            minutes: Number(parts.hour) * 60 + Number(parts.minute)
        };
    } catch {
        return {day: isoOf(at), minutes: at.getHours() * 60 + at.getMinutes()};
    }
}

/** «sáb 17/10» or «sáb 17/10 · 18:30». */
export function shortLabel(day, time) {
    const date = dateOf(day);
    return `${WEEKDAY_ABBR[date.getDay()]} ${pad(date.getDate())}/${pad(date.getMonth() + 1)}`
        + (time ? ` · ${time}` : "");
}

/** «sábado 17 de octubre a las 18:30». */
export function longLabel(day, time) {
    const date = dateOf(day);
    return `${WEEKDAYS[date.getDay()]} ${date.getDate()} de ${MONTHS[date.getMonth()]} a las ${time || "00:00"}`;
}

/**
 * ``dateTimePicker({onChange, now, suggestions, placeholder})``: ``now()`` gives
 * {day, minutes} in the zone the value means (nothing earlier can be picked);
 * ``suggestions()`` some [{label, time}] to pick the hour in one click.
 * ``lead`` opens the panel's summary («Empieza el sábado 17 de octubre a las 18:30»).
 * ``set({day, time})`` shows a value ("" for none); ``onChange({day, time})``
 * gets the one applied with «Aplicar». ``time`` is "HH:MM", "" meaning 00:00.
 */
export function dateTimePicker({
                                   onChange, now, suggestions = () => [],
                                   placeholder = "Elegir día y hora",
                                   lead = "Empieza el",
                               }) {
    const root = el("div", "dtp");
    const toggle = el("button", "dtp-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-haspopup", "dialog");
    const panel = el("div", "dtp-panel");
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Día y hora");
    root.append(toggle, panel);

    let value = {day: "", time: ""};
    // Whether the button shows that value (false: it came from a preset next to it).
    let custom = false;
    // What the panel shows until «Aplicar»: the day and the minute of the day.
    let draft = {day: "", minutes: 0};
    let month = new Date();

    const place = () => {
        if (panel.hidden || !root.isConnected) return;
        const r = toggle.getBoundingClientRect();
        const width = Math.min(560, innerWidth - 32);
        panel.style.width = `${width}px`;
        panel.style.left = `${Math.max(16, Math.min(r.right - width, innerWidth - width - 16))}px`;
        // Under the button; above it if it fits there and not below; else as low as it fits.
        panel.style.maxHeight = `${innerHeight - 32}px`;
        const height = panel.offsetHeight;
        const below = r.bottom + 6;
        const above = r.top - height - 6;
        panel.style.top = `${below + height <= innerHeight - 16 ? below
            : above >= 16 ? above : Math.max(16, innerHeight - height - 16)}px`;
    };
    const open = (yes) => {
        if (yes) {
            draft = {day: value.day, minutes: toMinutes(value.time)};
            // Nothing picked, or a moment already gone: the next o'clock hour.
            if (!draft.day || draft.minutes < firstMinute(draft.day)) {
                const n = now();
                const hour = Math.floor(n.minutes / 60) + 1;
                draft = hour < 24 ? {day: n.day, minutes: hour * 60} : {day: nextDay(n.day), minutes: 0};
            }
            month = dateOf(draft.day);
            month.setDate(1);
            draw();
        }
        panel.hidden = !yes;
        toggle.setAttribute("aria-expanded", String(yes));
        root.classList.toggle("is-open", yes);
        if (yes) {
            toggle.scrollIntoView({block: "nearest"});
            place();
            addEventListener("scroll", place, true);
            addEventListener("resize", place);
            panel.querySelector(".dtp-day.is-picked")?.focus();
        } else {
            removeEventListener("scroll", place, true);
            removeEventListener("resize", place);
        }
    };
    toggle.addEventListener("click", () => open(panel.hidden));
    document.addEventListener("pointerdown", (e) => {
        if (!panel.hidden && !root.contains(e.target)) open(false);
    });
    panel.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            e.stopPropagation();
            open(false);
            toggle.focus();
        }
    });

    /* ---- the parts of the panel, built once and redrawn ---- */
    const title = el("strong", "dtp-month");
    const nav = (text, label, step) => {
        const b = el("button", "dtp-nav", text);
        b.type = "button";
        b.setAttribute("aria-label", label);
        b.addEventListener("click", () => {
            month.setMonth(month.getMonth() + step);
            draw();
        });
        return b;
    };
    const prev = nav("‹", "Mes anterior", -1);
    const next = nav("›", "Mes siguiente", 1);
    const head = el("div", "dtp-head");
    head.append(prev, title, next);
    const grid = el("div", "dtp-grid");
    grid.setAttribute("role", "grid");
    const calendar = el("div", "dtp-calendar");
    calendar.append(head, grid);

    const clock = el("div", "dtp-clock");
    const hours = el("div", "dtp-hours");
    const minutes = el("div", "dtp-minutes");
    const exact = el("input", "dtp-exact");
    exact.type = "time";
    exact.setAttribute("aria-label", "Hora exacta");
    exact.addEventListener("change", () => {
        if (!exact.value) return;
        draft.minutes = toMinutes(exact.value);
        draw();
    });
    const quick = el("div", "dtp-quick");
    const exactRow = el("label", "dtp-exact-row muted small");
    exactRow.append(document.createTextNode("Exacta "), exact);
    clock.append(el("div", "dtp-label", "Hora"), hours, el("div", "dtp-label", "Minuto"), minutes, exactRow, quick);

    const body = el("div", "dtp-body");
    body.append(calendar, clock);

    const summary = el("div", "dtp-summary small");
    const cancel = el("button", "btn btn-ghost btn-sm", "Cancelar");
    cancel.type = "button";
    cancel.addEventListener("click", () => open(false));
    const apply = el("button", "btn btn-primary btn-sm", "Aplicar");
    apply.type = "button";
    apply.addEventListener("click", () => {
        value = {day: draft.day, time: draft.minutes ? hhmm(draft.minutes) : ""};
        custom = true;
        drawToggle();
        open(false);
        onChange({...value});
    });
    const foot = el("div", "dtp-foot");
    foot.append(summary, cancel, apply);
    panel.append(body, foot);

    /** The first minute that can still be picked on a day: 0, or past now today. */
    const firstMinute = (day) => {
        const n = now();
        return day === n.day ? n.minutes + 1 : day < n.day ? Infinity : 0;
    };

    const pickDay = (iso) => {
        draft.day = iso;
        const d = dateOf(iso);
        if (d.getMonth() !== month.getMonth() || d.getFullYear() !== month.getFullYear()) {
            month = new Date(d.getFullYear(), d.getMonth(), 1);
        }
        draw();
        grid.querySelector(".dtp-day.is-picked")?.focus();
    };

    // Arrows move the day, as in any calendar.
    grid.addEventListener("keydown", (e) => {
        const step = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7}[e.key];
        if (!step) return;
        e.preventDefault();
        const d = dateOf(draft.day);
        d.setDate(d.getDate() + step);
        const iso = isoOf(d);
        if (iso >= now().day) pickDay(iso);
    });

    function drawCalendar(today) {
        title.textContent = `${MONTHS[month.getMonth()]} ${month.getFullYear()}`;
        prev.disabled = month.getFullYear() * 12 + month.getMonth()
            <= dateOf(today).getFullYear() * 12 + dateOf(today).getMonth();
        const first = new Date(month.getFullYear(), month.getMonth(), 1, 12);
        const start = new Date(first);
        start.setDate(1 - ((first.getDay() + 6) % 7));
        const cells = HEAD.map((h) => el("span", "dtp-dow", h));
        for (let i = 0; i < 42; i++) {
            const d = new Date(start);
            d.setDate(start.getDate() + i);
            const iso = isoOf(d);
            const b = el("button", "dtp-day", String(d.getDate()));
            b.type = "button";
            b.tabIndex = iso === draft.day ? 0 : -1;
            b.classList.toggle("is-other", d.getMonth() !== month.getMonth());
            b.classList.toggle("is-today", iso === today);
            b.classList.toggle("is-picked", iso === draft.day);
            b.classList.toggle("is-weekend", d.getDay() === 0 || d.getDay() === 6);
            b.disabled = iso < today;
            b.setAttribute("aria-label", longLabel(iso, "").replace(/ a las.*/, ""));
            b.setAttribute("aria-selected", String(iso === draft.day));
            b.addEventListener("click", () => pickDay(iso));
            cells.push(b);
        }
        grid.replaceChildren(...cells);
    }

    function drawClock() {
        const min = firstMinute(draft.day);
        const hour = Math.floor(draft.minutes / 60);
        const minute = draft.minutes % 60;
        hours.replaceChildren(...Array.from({length: 24}, (_, h) => {
            const b = el("button", `dtp-cell${h === hour ? " is-picked" : ""}`, pad(h));
            b.type = "button";
            b.disabled = h * 60 + 59 < min;
            b.addEventListener("click", () => {
                // Same minute in the new hour; this hour's first one still ahead if that passed.
                const ahead = Math.ceil(min / 5) * 5;
                draft.minutes = Math.min(Math.max(h * 60 + minute, ahead), h * 60 + 59);
                draw();
            });
            return b;
        }));
        minutes.replaceChildren(...MINUTE_STEPS.map((m) => {
            const b = el("button", `dtp-cell${m === minute ? " is-picked" : ""}`, pad(m));
            b.type = "button";
            b.disabled = hour * 60 + m < min;
            b.addEventListener("click", () => {
                draft.minutes = hour * 60 + m;
                draw();
            });
            return b;
        }));
        exact.value = hhmm(draft.minutes);
        const options = [{label: "Inicio del día", time: "00:00"}, ...suggestions()];
        const seen = new Set();
        quick.replaceChildren(...options.filter((o) => !seen.has(o.time) && seen.add(o.time)).map((o) => {
            const b = el("button", `chip${toMinutes(o.time) === draft.minutes ? " is-active" : ""}`);
            b.type = "button";
            b.append(document.createTextNode(o.time), el("small", null, o.label));
            b.disabled = toMinutes(o.time) < min;
            b.addEventListener("click", () => {
                draft.minutes = toMinutes(o.time);
                draw();
            });
            return b;
        }));
    }

    function draw() {
        const today = now().day;
        drawCalendar(today);
        drawClock();
        const gone = draft.minutes < firstMinute(draft.day);
        summary.classList.toggle("is-bad", gone);
        summary.textContent = gone ? "Esa hora ya pasó: elige una más tarde u otro día."
            : `${lead} ${longLabel(draft.day, hhmm(draft.minutes))}.`;
        apply.disabled = gone;
        place();
    }

    function drawToggle() {
        toggle.replaceChildren(el("span", "dtp-icon", "📅"),
            el("span", null, custom && value.day ? shortLabel(value.day, value.time) : placeholder));
        toggle.classList.toggle("is-active", custom && Boolean(value.day));
    }

    drawToggle();
    return {
        node: root,
        /** The value in force; ``shown`` false when a preset beside the button holds it
         (the calendar still opens on it). */
        set(next, shown = true) {
            value = {day: next.day || "", time: next.time || ""};
            custom = shown;
            drawToggle();
        },
        open,
    };
}
