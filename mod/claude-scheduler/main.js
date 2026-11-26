// Settings and status for claude-scheduler, per machine. The work is done by
// claude_scheduler.py running as a systemd user service on each machine; this
// popup only reads its status and writes its settings, through the same
// script on that machine (`exec(..., { on })`). The kicker follows the
// machine it was turned on from: saving it on here turns it off elsewhere.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, machines, appHome, theme, props, closeOverlay, notify, checking, readTextFile } from "herdr";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const WEEK = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const SCRIPT = `${appHome()}/mods/claude-scheduler/claude_scheduler.py`;
// The installed copy, wherever XDG puts it on that machine.
const RUN = 'exec python3 "${XDG_DATA_HOME:-$HOME/.local/share}/claude-scheduler/claude_scheduler.py" "$@"';
// Writes the script it is given as argv[1] to the installed spot, then installs it.
const INSTALL = "import os,sys,subprocess\n" +
  "d=os.path.join(os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'),'claude-scheduler')\n" +
  "os.makedirs(d,exist_ok=True)\np=os.path.join(d,'claude_scheduler.py')\n" +
  "open(p,'w').write(sys.argv[1])\nsys.exit(subprocess.call([sys.executable,p,'install']))";
const POLL_MS = 15000;
const SEEN = "claude-scheduler.seen";

const key = (m) => m ?? "";

function cli(machine, args, timeout = 30000) {
  return exec("sh", ["-c", RUN, "sh", ...args], { on: machine, timeout });
}

function when(epoch) {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${WEEK[d.getDay()]} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function pct(w) {
  return w && typeof w.utilization === "number" ? `${Math.round(w.utilization * 100)}%` : "—";
}

export default class ClaudeScheduler extends View {
  init(_props, cx) {
    this.machine = null; // null: this computer
    this.status = {};    // machine key -> { data, error, missing }
    this.draft = null;   // the settings being edited, for this.machine
    this.dirty = false;
    this.busy = "";
    this.error = "";
    this.warn = "";
    this.src = null;

    this.start = {};
    this.end = {};
    for (const d of DAYS) {
      this.start[d] = this.input("06:00", (v) => { this.draft.kicker.days[d].start = v; });
      this.end[d] = this.input("next start", (v) => { this.draft.kicker.days[d].end = v || null; });
    }
    this.until = this.input("never", (v) => { this.draft.resume.until = v || null; });
    this.message = this.input("", (v) => { this.draft.resume.message = v; });
    this.name = InputState.new({ placeholder: "Add a session name, Enter" });
    this.name.on("submit", (_e, cx) => {
      const v = this.name.value().trim();
      if (v && this.draft) this.toggleSession(v, true, cx);
      this.name.set_value("");
    });

    if (checking()) return;
    cx.spawn(async (cx) => {
      this.src = await readSource();
      cx.notify();
    });
    this.refreshAll(cx, true);
    cx.timer.every(POLL_MS, (cx) => this.refreshAll(cx, false));
  }

  input(placeholder, set) {
    const s = InputState.new({ placeholder });
    s.on("change", (_e, cx) => {
      if (!this.draft || this.loading) return;
      set(s.value().trim());
      this.dirty = true;
      cx.notify();
    });
    return s;
  }

  close() {
    closeOverlay(props().overlay);
  }

  machineList() {
    return machines().map((m) => m.name);
  }

  label(m) {
    if (m === null) return this.status[""]?.data?.host ? `${this.status[""].data.host} (here)` : "This computer";
    return m;
  }

  refreshAll(cx, first) {
    cx.spawn(async (cx) => {
      await Promise.all(this.machineList().map((m) => this.refresh(m)));
      if (first) this.toastNew();
      if (!this.dirty) this.load();
      cx.notify();
    });
  }

  async refresh(m) {
    try {
      const r = await cli(m, ["status"]);
      if (r.code === 0) this.status[key(m)] = { data: JSON.parse(r.stdout) };
      else if (/can't open file|No such file/.test(r.stderr)) this.status[key(m)] = { missing: true };
      else this.status[key(m)] = { error: (r.stderr || r.stdout).trim() || `exit ${r.code}` };
    } catch (e) {
      this.status[key(m)] = { error: String(e).replace(/^Error: /, "") };
    }
  }

  // Fresh settings from the selected machine into the form.
  load() {
    const d = this.status[key(this.machine)]?.data;
    if (!d) {
      this.draft = null;
      return;
    }
    this.draft = JSON.parse(JSON.stringify(d.config));
    this.loading = true;
    for (const day of DAYS) {
      this.start[day].set_value(this.draft.kicker.days[day].start ?? "");
      this.end[day].set_value(this.draft.kicker.days[day].end ?? "");
    }
    this.until.set_value(this.draft.resume.until ?? "");
    this.message.set_value(this.draft.resume.message ?? "");
    this.loading = false;
    this.dirty = false;
  }

  // Events the service wanted to tell about, since this popup last opened.
  toastNew() {
    let seen = 0;
    try { seen = Number(localStorage.getItem(SEEN) ?? 0); } catch { /* fine */ }
    let latest = seen;
    for (const m of this.machineList()) {
      for (const e of this.status[key(m)]?.data?.events ?? []) {
        latest = Math.max(latest, e.at);
        if (e.notify && e.at > seen) {
          notify({ title: m === null ? "Claude scheduler" : `Claude scheduler on ${m}`, body: e.text, level: "warning" });
        }
      }
    }
    try { localStorage.setItem(SEEN, String(latest)); } catch { /* fine */ }
  }

  // The other machine running the kicker, if any.
  kickerOwner() {
    for (const m of this.machineList()) {
      if (m === this.machine) continue;
      if (this.status[key(m)]?.data?.config?.kicker?.enabled) return m;
    }
    return undefined;
  }

  select(m, cx) {
    if (this.dirty && m !== this.machine) {
      this.warn = "Save or discard your changes first.";
      cx.notify();
      return;
    }
    this.machine = m;
    this.warn = "";
    this.error = "";
    this.load();
    cx.notify();
  }

  toggleSession(name, on, cx) {
    const list = new Set(this.draft.resume.sessions);
    if (on) list.add(name); else list.delete(name);
    this.draft.resume.sessions = [...list].sort();
    this.dirty = true;
    cx.notify();
  }

  set(path, value, cx) {
    path(this.draft, value);
    this.dirty = true;
    cx.notify();
  }

  save(cx) {
    if (!this.draft || this.busy) return;
    const m = this.machine;
    const cfg = this.draft;
    this.busy = "saving…";
    this.error = "";
    this.warn = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await cli(m, ["config", "set", JSON.stringify(cfg)]);
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim());
        if (cfg.kicker.enabled) await this.standDownOthers(m);
        this.dirty = false;
        await this.refresh(m);
        this.load();
      } catch (e) {
        this.error = String(e).replace(/^Error: /, "");
      }
      this.busy = "";
      cx.notify();
    });
  }

  // The kicker follows the machine it was saved on.
  async standDownOthers(here) {
    const missed = [];
    for (const m of this.machineList()) {
      if (m === here) continue;
      const st = this.status[key(m)];
      if (st?.missing) continue;
      if (st?.data && !st.data.config.kicker.enabled) continue;
      try {
        const r = await cli(m, ["kicker", "off"]);
        if (r.code !== 0) throw new Error(r.stderr);
        await this.refresh(m);
      } catch {
        missed.push(this.label(m));
      }
    }
    if (missed.length) {
      this.warn = `Couldn't reach ${missed.join(", ")} to turn its kicker off; both may kick for a while.`;
    }
  }

  // Take the kicker over from another machine, schedule and all.
  moveHere(owner, cx) {
    const theirs = this.status[key(owner)]?.data?.config?.kicker;
    if (!this.draft || !theirs) return;
    this.draft.kicker = JSON.parse(JSON.stringify(theirs));
    this.loading = true;
    for (const day of DAYS) {
      this.start[day].set_value(theirs.days[day].start ?? "");
      this.end[day].set_value(theirs.days[day].end ?? "");
    }
    this.loading = false;
    this.dirty = true;
    cx.notify();
  }

  install(cx) {
    const m = this.machine;
    if (!this.src || this.busy) return;
    this.busy = "installing…";
    this.error = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("python3", ["-I", "-c", INSTALL, this.src], { on: m, timeout: 60000 });
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim());
        await this.refresh(m);
        this.load();
      } catch (e) {
        this.error = String(e).replace(/^Error: /, "");
      }
      this.busy = "";
      cx.notify();
    });
  }

  render() {
    const t = theme();
    const st = this.status[key(this.machine)];
    const data = st?.data;
    const srcVersion = this.src && /VERSION = "([^"]+)"/.exec(this.src)?.[1];

    const button = (id, text, onClick, { primary = false, disabled = false } = {}) =>
      div().id(id).px(12).h(28).flex_none().rounded(7).flex().items_center().justify_center()
        .text_size(12.5)
        .bg(primary && !disabled ? t.solid : t.raised)
        .text_color(disabled ? t.dim : primary ? t.solid_foreground : t.foreground)
        .when(!disabled, (el) => el.cursor_pointer().hover((s) => s.opacity(0.85)).on_click((_e, cx) => onClick(cx)))
        .child(text);

    const toggle = (id, on, onClick, disabled = false) =>
      div().id(id).w(34).h(18).flex_none().rounded(9).p(2).flex()
        .when(on, (el) => el.justify_end())
        .bg(on && !disabled ? t.accent : t.raised).border_1().border_color(t.border)
        .when(!disabled, (el) => el.cursor_pointer().on_click((_e, cx) => onClick(cx)))
        .child(div().w(12).h(12).rounded(6).bg(on && !disabled ? t.accent_foreground : t.dim));

    const field = (id, state, w, disabled = false) =>
      h_flex().id(id).w(w).h(26).flex_none().px(8).items_center().rounded(6).border_1()
        .border_color(t.input ?? t.border).bg(t.background).when(disabled, (el) => el.opacity(0.5))
        .child(Input.new(state).flex_1().h_full().text_size(12.5).text_color(t.foreground));

    const heading = (text, right) =>
      h_flex().items_center().gap(10).pt(6)
        .child(div().flex_1().text_size(13.5).font_semibold().text_color(t.foreground).child(text))
        .when(!!right, (el) => el.child(right));

    const note = (text, color = t.dim) => div().text_size(11.5).text_color(color).child(text);

    const machineTabs = h_flex().gap(6).flex_wrap().children(this.machineList().map((m) => {
      const on = m === this.machine;
      return div().id(`m-${key(m) || "here"}`).px(10).h(24).rounded(6).flex().items_center().text_size(12)
        .cursor_pointer().bg(on ? t.selected : t.raised).text_color(on ? t.foreground : t.muted)
        .on_click((_e, cx) => this.select(m, cx)).child(this.label(m));
    }));

    const body = [];
    if (!st) {
      body.push(note("Checking…"));
    } else if (st.missing) {
      body.push(note("The service isn't installed on this machine."));
      body.push(h_flex().child(button("install", this.busy || "Install service", (cx) => this.install(cx),
        { primary: true, disabled: !this.src || !!this.busy })));
    } else if (st.error) {
      body.push(note(`Can't reach it: ${st.error}`, t.blocked));
    } else if (data && this.draft) {
      body.push(...this.form(data, srcVersion, { t, button, toggle, field, heading, note }));
    }

    return v_flex()
      .id("claude-scheduler")
      .size_full()
      .min_h(0)
      .p(18)
      .gap(10)
      .bg(t.popover)
      .text_size(13)
      .text_color(t.foreground)
      .child(
        h_flex().items_center().gap(8)
          .child(div().flex_1().text_size(15).font_semibold().child("Claude scheduler"))
          .child(note(this.busy || "Esc")),
      )
      .child(machineTabs)
      .child(v_flex().id("cs-body").h(620).flex_none().overflow_y_scrollbar().gap(8).pr(6).children(body))
      .when(!!this.warn, (el) => el.child(note(this.warn, t.working)))
      .when(!!this.error, (el) => el.child(note(this.error, t.blocked)))
      .when(!!data && !!this.draft, (el) => el.child(
        h_flex().gap(8).justify_end()
          .child(button("discard", "Discard", (cx) => { this.load(); this.warn = ""; this.error = ""; cx.notify(); },
            { disabled: !this.dirty || !!this.busy }))
          .child(button("save", "Save", (cx) => this.save(cx), { primary: true, disabled: !this.dirty || !!this.busy })),
      ));
  }

  form(data, srcVersion, { t, button, toggle, field, heading, note }) {
    const cfg = this.draft;
    const k = data.kicker;
    const out = [];

    // Service line.
    const svc = data.service === "active" ? "running" : data.service;
    const stale = data.heartbeat && Date.now() / 1000 - data.heartbeat > 120;
    out.push(h_flex().gap(10).items_center()
      .child(note(`Service ${svc}${stale ? ", but quiet for over 2 min" : ""} · v${data.version}`,
        data.service === "active" && !stale ? t.dim : t.blocked))
      .when(!!srcVersion && srcVersion !== data.version, (el) =>
        el.child(button("update", `Update to v${srcVersion}`, (cx) => this.install(cx), { disabled: !!this.busy })))
      .when(data.service !== "active", (el) =>
        el.child(button("reinstall", "Install service", (cx) => this.install(cx), { disabled: !!this.busy }))));
    if (data.config_error) out.push(note(`Settings file: ${data.config_error}`, t.blocked));

    // Kicker.
    const owner = this.kickerOwner();
    const elsewhere = owner !== undefined && !cfg.kicker.enabled;
    out.push(heading("Window kicker", toggle("k-on", cfg.kicker.enabled,
      (cx) => this.set((c, v) => { c.kicker.enabled = v; }, !cfg.kicker.enabled, cx))));
    if (elsewhere) {
      out.push(h_flex().gap(10).items_center()
        .child(note(`Kicking runs on ${this.label(owner)}.`, t.muted))
        .child(button("move", "Move kicking here", (cx) => this.moveHere(owner, cx))));
    } else if (owner !== undefined) {
      out.push(note(`Saving turns the kicker off on ${this.label(owner)}.`, t.working));
    }
    if (cfg.kicker.enabled && !elsewhere) {
      out.push(note(k.in_run
        ? `Running · window open until ${when(k.window_ends)} · next kick ${when(k.next_kick)}`
        : `Idle · next run starts ${when(k.next_start)}`));
    }
    out.push(note(`Usage: 5-hour ${pct(k.five_hour)}, week ${pct(k.seven_day)}` +
      (k.seven_day?.resetsAt ? ` (resets ${when(k.seven_day.resetsAt)})` : "")));
    const ro = elsewhere;
    out.push(v_flex().gap(4).children(DAYS.map((d) => {
      const row = cfg.kicker.days[d];
      return h_flex().gap(10).items_center()
        .child(toggle(`d-${d}`, row.enabled, (cx) => this.set((c, v) => { c.kicker.days[d].enabled = v; }, !row.enabled, cx), ro))
        .child(div().w(34).text_color(row.enabled && !ro ? t.foreground : t.dim).child(DAY_NAMES[d]))
        .child(note("from"))
        .child(field(`s-${d}`, this.start[d], 70, ro || !row.enabled))
        .child(note("until"))
        .child(field(`e-${d}`, this.end[d], 104, ro || !row.enabled));
    })));
    out.push(note("A kick that lands before the end goes ahead; its window may run past it."));

    // Resume.
    out.push(heading("Limit resume", toggle("r-on", cfg.resume.enabled,
      (cx) => this.set((c, v) => { c.resume.enabled = v; }, !cfg.resume.enabled, cx))));
    out.push(h_flex().gap(10).items_center()
      .child(note("Turn off at"))
      .child(field("until", this.until, 150))
      .child(note("YYYY-MM-DD HH:MM, empty for never")));
    out.push(note(cfg.resume.sessions.length ? "Resume only these sessions:" : "Resume every session, or pick some:"));
    const live = data.sessions ?? [];
    const count = {};
    for (const s of live) count[s.name] = (count[s.name] ?? 0) + 1;
    const names = [...new Set([...cfg.resume.sessions, ...live.map((s) => s.name)])].sort((a, b) => a.localeCompare(b));
    out.push(h_flex().gap(6).flex_wrap().children(names.map((n) => {
      const on = cfg.resume.sessions.includes(n);
      const dup = (count[n] ?? 0) > 1;
      const gone = !count[n];
      return div().id(`sn-${n}`).px(8).h(22).rounded(11).flex().items_center().text_size(11.5).cursor_pointer()
        .bg(on ? t.selected : t.raised).border_1().border_color(on ? t.accent : t.border)
        .text_color(on ? t.foreground : t.muted)
        .on_click((_e, cx) => this.toggleSession(n, !on, cx))
        .child(`${n}${dup ? ` ×${count[n]}` : ""}${gone ? " (not running)" : ""}`);
    })));
    out.push(field("name", this.name, 260));
    out.push(note("Message sent to the session:"));
    out.push(field("message", this.message, 580));
    const pending = data.resume.pending ?? [];
    if (pending.length) {
      out.push(note(`Waiting: ${pending.map((p) => `${p.name} at ${when(p.at)}`).join(", ")}`, t.working));
    }

    // Recent events.
    const events = (data.events ?? []).filter((e) => e.kind !== "started").slice(-6).reverse();
    if (events.length) {
      out.push(heading("Recent"));
      out.push(v_flex().gap(2).children(events.map((e, i) =>
        h_flex().id(`ev-${i}`).gap(8)
          .child(div().w(70).flex_none().text_size(11.5).text_color(t.dim).child(when(e.at)))
          .child(div().flex_1().truncate().text_size(11.5)
            .text_color(/failed|error|skipped/.test(e.kind) ? t.working : t.muted).child(e.text)))));
    }
    return out;
  }
}

async function readSource() {
  try {
    return await readTextFile(SCRIPT);
  } catch {
    return null;
  }
}
