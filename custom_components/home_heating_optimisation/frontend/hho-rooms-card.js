/* Home Heating Optimisation: room-by-room warmth card.
 *
 * Reads sensor.home_heating_optimisation_rooms_overview (served with the integration), which
 * carries every room's temperature, target, overdrive, Evohome target, valve demand and
 * pre-heat state. Usage: `type: custom:hho-rooms-card`.
 */

const DEFAULT_ENTITY = "sensor.home_heating_optimisation_rooms_overview";
const STATES = {
  manual: "Manual hold",
  window_open: "Window open",
  outside_window: "Outside hours",
  off: "Off",
  shadow: "Shadow",
  no_data: "Waiting for data",
};

const FLAME =
  '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2c1 4 6 6.5 6 12a6 6 0 0 1-12 0c0-3 2-5 3-7 .8 1.6 1.4 2.6 2 3 0-3 1-6 1-8z"/></svg>';
const CLOCK =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>';

const STYLE = `
:host{--below:#5aa9e6;--above:#e8913a;--ok:#58c49a;--heat:#e3a23b;--pre:#b08de8;
 --ink:var(--primary-text-color,#f1f2ef);--muted:var(--secondary-text-color,#a8b0b4);
 --faint:var(--disabled-text-color,#6f787d);--line:var(--divider-color,#3b4247);
 --tile:var(--ha-card-background,var(--card-background-color,#22272b));--track:rgba(127,127,127,.22)}
ha-card{overflow:hidden}
.head{display:flex;align-items:center;gap:12px;padding:16px 20px 12px;flex-wrap:wrap}
.head h2{margin:0;font-size:18px;font-weight:600;color:var(--ink)}
.head .sub{color:var(--muted);font-size:12px;margin-top:2px}
.summary{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
.sum{font-size:11.5px;padding:3px 9px;border-radius:999px;background:var(--track);color:var(--muted)}
.sum b{color:var(--ink)}.sum i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}
.house{display:flex;flex-wrap:wrap;gap:6px 22px;padding:10px 20px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.house b{color:var(--ink);font-size:13px;font-weight:600;margin-left:4px}
.house .on{color:var(--heat)}
.grid{display:grid;grid-template-columns:repeat(var(--cols,3),minmax(0,1fr));gap:1px;background:var(--line)}
.room{background:var(--tile);padding:14px 18px 13px;position:relative;display:flex;flex-direction:column;gap:10px;min-width:0}
.room::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--edge,transparent)}
.room.below{--edge:var(--below)}.room.above{--edge:var(--above)}
.room.preheating{background:linear-gradient(115deg,rgba(176,141,232,.13),var(--tile) 70%)}
.room.quiet{opacity:.62}
.top{display:flex;align-items:center;justify-content:space-between;gap:6px}
.top h3{margin:0;font-size:14.5px;font-weight:600;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--faint);white-space:nowrap}
.st.on{color:var(--heat)}.st.flag{color:var(--muted)}.st svg{width:13px;height:13px}
.pair{display:grid;grid-template-columns:1fr auto 1fr;align-items:end}
.lbl{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.v{font-variant-numeric:tabular-nums;line-height:1.05;color:var(--ink)}
.now .v{font-size:32px;letter-spacing:-1.4px}
.tgt{text-align:right}.tgt .v{font-size:21px;color:var(--muted);letter-spacing:-.5px}
.v sup{font-size:.5em;vertical-align:.7em;margin-left:1px}
.delta{align-self:center;font-size:11.5px;font-weight:600;padding:3px 8px;border-radius:999px;white-space:nowrap;margin:0 6px 4px}
.delta.below{background:rgba(90,169,230,.16);color:var(--below)}.delta.above{background:rgba(232,145,58,.16);color:var(--above)}.delta.ok{background:rgba(88,196,154,.15);color:var(--ok)}
.gap{position:relative;height:6px;border-radius:3px;background:var(--track)}
.gap .c{position:absolute;left:50%;top:-4px;width:2px;height:14px;background:var(--muted);border-radius:1px;transform:translateX(-1px)}
.gap .f{position:absolute;top:0;bottom:0;border-radius:3px}
.gap .d{position:absolute;top:-4px;width:14px;height:14px;border-radius:50%;transform:translateX(-7px);border:3px solid var(--tile);box-sizing:border-box}
.evo{display:flex;align-items:center;justify-content:space-between;gap:6px;font-size:11.5px;color:var(--muted)}
.evo b{color:var(--ink);font-weight:500;font-size:12.5px}
.od{font-size:11px;font-weight:600;padding:2px 7px;border-radius:6px;margin-left:6px}
.od.up{background:rgba(90,169,230,.16);color:var(--below)}.od.dn{background:rgba(232,145,58,.16);color:var(--above)}.od.z{background:var(--track);color:var(--faint)}
.valve{display:flex;align-items:center;gap:10px;font-size:11.5px;color:var(--muted)}
.meter{flex:1;height:5px;border-radius:3px;background:var(--track);overflow:hidden}.meter i{display:block;height:100%;background:var(--heat)}
.valve b{min-width:32px;text-align:right;color:var(--ink);font-weight:500;font-size:12px}
.pre{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--pre);padding-top:8px;border-top:1px solid var(--line)}
.pre svg{width:13px;height:13px}
.empty{padding:24px;color:var(--muted)}
@media (max-width:760px){.grid{--cols:2}.summary{display:none}}
@media (max-width:420px){.grid{--cols:1}}
`;

const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class HhoRoomsCard extends HTMLElement {
  setConfig(config) {
    this._config = { entity: DEFAULT_ENTITY, title: "Heating", ...config };
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._last = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._config?.entity];
    if (state === this._last) return;
    this._last = state;
    this._render();
  }

  getCardSize() {
    return 9;
  }

  static getStubConfig() {
    return { entity: DEFAULT_ENTITY };
  }

  _time(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleTimeString(this._hass?.locale?.language || undefined, { hour: "2-digit", minute: "2-digit" });
  }

  _room(r) {
    const has = r.air !== null && r.air !== undefined && r.target !== null && r.target !== undefined;
    const diff = has ? Math.round((r.air - r.target) * 10) / 10 : 0;
    const st = !has ? "ok" : diff <= -0.1 ? "below" : diff >= 0.1 ? "above" : "ok";
    const col = st === "below" ? "var(--below)" : st === "above" ? "var(--above)" : "var(--ok)";
    const p = Math.max(0, Math.min(100, 50 + (diff / 1.5) * 50));
    const delta = !has ? "" : st === "ok" ? "on target" : `${Math.abs(diff).toFixed(1)}° ${st}`;
    const od = r.overdrive ?? 0;
    const odc = od > 0 ? "up" : od < 0 ? "dn" : "z";
    const ods = od > 0 ? `+${od.toFixed(1)}` : od < 0 ? `−${Math.abs(od).toFixed(1)}` : "0.0";
    const pre = r.preheat;
    const preheating = pre && pre.status === "active";
    const flag = STATES[r.state];
    const status = flag
      ? `<span class="st flag">${esc(flag)}</span>`
      : r.calling
        ? `<span class="st on">${FLAME}Heating</span>`
        : `<span class="st">Idle</span>`;
    const preText = !pre
      ? ""
      : preheating
        ? `Pre-heating${pre.warm_by ? " · warm by " + this._time(pre.warm_by) : ""}`
        : `Pre-heat planned · starts ${this._time(pre.starts)}`;
    const demand = r.demand ?? 0;
    return `<div class="room ${st}${preheating ? " preheating" : ""}${["off", "outside_window"].includes(r.state) ? " quiet" : ""}">
      <div class="top"><h3 title="${esc(r.name)}">${esc(r.name)}</h3>${status}</div>
      <div class="pair">
        <div class="now"><div class="lbl">Room</div><div class="v">${fmt(r.air)}<sup>°</sup></div></div>
        ${delta ? `<span class="delta ${st}">${delta}</span>` : "<span></span>"}
        <div class="tgt"><div class="lbl">${preheating ? "Pre-heat to" : "Target"}</div><div class="v">${fmt(r.target)}<sup>°</sup></div></div>
      </div>
      <div class="gap"><div class="f" style="left:${Math.min(p, 50)}%;width:${Math.abs(p - 50)}%;background:${col}"></div><div class="c"></div>${has ? `<div class="d" style="left:${p}%;background:${col}"></div>` : ""}</div>
      <div class="evo"><span>Evohome told <b>${fmt(r.evohome)}°</b></span><span>${preheating ? "push" : "overdrive"}<span class="od ${odc}">${ods}</span></span></div>
      <div class="valve"><span>Valve</span><div class="meter"><i style="width:${demand}%"></i></div><b>${r.demand ?? "–"}${r.demand === null || r.demand === undefined ? "" : "%"}</b></div>
      ${preText ? `<div class="pre">${CLOCK}${esc(preText)}</div>` : ""}
    </div>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const state = this._last;
    const a = state?.attributes || {};
    const rooms = Array.isArray(a.rooms) ? a.rooms : [];
    const h = a.house || {};
    let below = 0, ok = 0, above = 0;
    for (const r of rooms) {
      if (r.air == null || r.target == null) continue;
      const diff = Math.round((r.air - r.target) * 10) / 10;
      if (diff <= -0.1) below++;
      else if (diff >= 0.1) above++;
      else ok++;
    }
    const cols = this._config.columns ? `style="--cols:${Number(this._config.columns)}"` : "";
    const body = !state
      ? `<div class="empty">Entity ${esc(this._config.entity)} not found. Is Home Heating Optimisation 0.20 or later installed?</div>`
      : `<div class="grid" ${cols}>${rooms.map((r) => this._room(r)).join("")}</div>`;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>
      <div class="head">
        <div><h2>${esc(this._config.title)}</h2><div class="sub">Home Heating Optimisation · ${rooms.length} rooms</div></div>
        <div class="summary">
          <span class="sum"><i style="background:var(--below)"></i><b>${below}</b> below</span>
          <span class="sum"><i style="background:var(--ok)"></i><b>${ok}</b> on target</span>
          <span class="sum"><i style="background:var(--above)"></i><b>${above}</b> above</span>
        </div>
      </div>
      <div class="house">
        <span>Outside<b>${fmt(h.outdoor)}°</b></span>
        <span>Flow<b>${fmt(h.flow, 0)}°</b> / ${fmt(h.flow_target, 0)}°</span>
        <span>Return<b>${fmt(h.return, 0)}°</b></span>
        <span class="${h.burner ? "on" : ""}">Burner<b>${h.burner ? "on" : "off"}</b></span>
        ${String(h.mode || "").startsWith("dhw") ? "<span>Hot water <b>charging</b></span>" : ""}
      </div>
      ${body}
    </ha-card>`;
  }
}

if (!customElements.get("hho-rooms-card")) {
  customElements.define("hho-rooms-card", HhoRoomsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "hho-rooms-card",
    name: "HHO rooms",
    description: "Every room's temperature, target, overdrive, valve and pre-heat (Home Heating Optimisation).",
  });
}
