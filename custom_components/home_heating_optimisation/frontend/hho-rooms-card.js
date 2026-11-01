/* Home Heating Optimisation: room-by-room warmth card.
 *
 * Reads sensor.home_heating_optimisation_rooms_overview (served with the integration), which
 * carries every room's temperature, target, overdrive, Evohome target, valve demand and
 * pre-heat state. Usage: `type: custom:hho-rooms-card`.
 * Optional `tolerance` sets the on-target band in degrees Celsius (default 0.15).
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
:host{display:block;min-width:0;line-height:1.4;--below:#5aa9e6;--above:#e8913a;--ok:#58c49a;--heat:#e3a23b;--pre:#b08de8;
 --ink:var(--primary-text-color,#f1f2ef);--muted:var(--secondary-text-color,#a8b0b4);
 --line:var(--divider-color,#3b4247);--tile:var(--ha-card-background,var(--card-background-color,#22272b));
 --track:color-mix(in srgb,var(--ink) 12%,var(--tile))}
*{box-sizing:border-box}
ha-card{display:block;overflow:hidden;container:hho / inline-size;color:var(--ink)}
.head{display:flex;flex-direction:column;align-items:flex-start;gap:12px;padding:18px 18px 14px}
.head h2{margin:0;font-size:19px;line-height:1.3;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.heading{display:flex;align-items:baseline;flex-wrap:wrap;gap:3px 12px}
.freshness{font-size:11px;font-weight:400;color:var(--muted);white-space:nowrap}
.freshness.stale{color:color-mix(in srgb,var(--heat) 45%,var(--ink));font-weight:500}
.head .sub{color:var(--muted);font-size:12px;margin-top:3px}
.summary{display:flex;gap:6px;flex-wrap:wrap}
.sum{font-size:12px;padding:4px 9px;border-radius:999px;background:color-mix(in srgb,var(--ink) 5%,var(--tile));color:var(--muted);white-space:nowrap}
.sum b{color:var(--ink);font-weight:600}.sum i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}
.house{display:flex;flex-wrap:wrap;gap:8px 18px;padding:12px 18px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.house>span{white-space:nowrap}.house b{color:var(--ink);font-size:13px;font-weight:600;margin-left:4px}
.house .on::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--heat);margin-right:5px}
.grid{display:grid;grid-template-columns:repeat(var(--cols,1),minmax(0,1fr));gap:1px;background:var(--tile)}
.room{--accent:var(--ok);background:var(--tile);padding:12px 16px;position:relative;display:flex;flex-direction:column;gap:8px;min-width:0;container:hho-room / inline-size;box-shadow:1px 0 0 var(--line),0 1px 0 var(--line)}
.room::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--edge,transparent)}
.room.below{--accent:var(--below);--edge:var(--below)}.room.above{--accent:var(--above);--edge:var(--above)}
.room.no-data{--accent:var(--track)}
.room.preheating{background:color-mix(in srgb,var(--pre) 5%,var(--tile))}
.top{display:flex;align-items:baseline;justify-content:space-between;gap:4px 10px;flex-wrap:wrap}
.top h3{margin:0;font-size:15px;line-height:1.35;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.st{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);white-space:nowrap}
.st.on{color:var(--ink)}.st svg{width:14px;height:14px;flex:none;color:var(--heat)}
.pair{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:baseline;gap:3px 8px}
.now,.tgt{display:contents}.tgt{text-align:right}
.now>*{grid-column:1}.tgt>*{grid-column:3}
.lbl{grid-row:1;font-size:10.5px;line-height:1.5;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.pair .v{grid-row:2}
.v{font-variant-numeric:tabular-nums;line-height:1.1;color:var(--ink);white-space:nowrap}
.now .v{font-size:32px;letter-spacing:-1.2px}.tgt .v{font-size:24px;letter-spacing:-.5px}
.v sup{font-size:.5em;vertical-align:baseline;position:relative;top:-.6em;margin-left:1px}
.delta{grid-column:2;grid-row:2;align-self:center;justify-self:center;font-size:12px;line-height:1.4;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap;color:var(--ink);background:color-mix(in srgb,var(--accent) 18%,var(--tile))}
.gap{position:relative;height:6px;border-radius:3px;background:var(--track);margin:3px 0}
.gap .c{position:absolute;left:50%;top:-4px;width:2px;height:14px;background:var(--muted);border-radius:1px;transform:translateX(-1px)}
.gap .f{position:absolute;top:0;bottom:0;border-radius:3px}
.gap .d{position:absolute;top:-4px;width:14px;height:14px;border-radius:50%;transform:translateX(-7px);border:3px solid var(--tile)}
.evo{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px 8px;font-size:11.5px;color:var(--muted)}
.evo>span{white-space:nowrap}.evo b{color:var(--ink);font-weight:500;font-size:12px}
.evo-drive{margin-left:auto;display:inline-flex;align-items:center;gap:5px}
.od{font-size:12px;line-height:1.4;font-weight:600;padding:2px 6px;border-radius:5px;background:color-mix(in srgb,var(--accent) 18%,var(--tile));color:var(--ink)}
.od.z{background:var(--track);color:var(--muted)}
.valve{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--muted)}
.valve.zero b{color:var(--muted)}.valve.zero .meter{opacity:.5}
.meter{flex:1;min-width:0;height:5px;border-radius:3px;background:var(--track);overflow:hidden}.meter i{display:block;height:100%;background:var(--heat);border-radius:0 3px 3px 0}
.valve b{min-width:32px;text-align:right;color:var(--ink);font-weight:500;font-size:12px;font-variant-numeric:tabular-nums}
.pre{display:flex;align-items:flex-start;gap:6px;font-size:12px;line-height:1.5;color:var(--ink);padding:6px 9px;border-left:2px solid var(--pre);border-radius:0 5px 5px 0;background:color-mix(in srgb,var(--pre) 10%,var(--tile))}
.pre svg{width:14px;height:14px;flex:none;margin-top:2px;color:var(--pre)}
.empty{padding:24px;color:var(--muted)}
/* The dashboard column, not the browser viewport, controls the tile count. */
@container hho (min-width:430px){.grid{--cols:2}}
@container hho (min-width:760px){.grid{--cols:3}.head{flex-direction:row;align-items:center}.summary{margin-left:auto;justify-content:flex-end}}
@container hho (max-width:429px){.house{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}}
/* Keep both temperatures on one baseline; move the chip rather than squeezing them. */
@container hho-room (max-width:249px){
 .pair{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
 .tgt>*{grid-column:2}
 .delta{grid-column:1 / -1;grid-row:3;justify-self:start;margin-top:2px}
}
`;


const hasNumber = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
const fmt = (v, d = 1) => (hasNumber(v) ? Number(v).toFixed(d) : "–");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class HhoRoomsCard extends HTMLElement {
  connectedCallback() {
    this._updateFreshness();
    // Age the timestamp even when Home Assistant stops sending entity updates.
    clearInterval(this._freshnessTimer);
    this._freshnessTimer = setInterval(() => this._updateFreshness(), 60000);
  }

  disconnectedCallback() {
    clearInterval(this._freshnessTimer);
    this._freshnessTimer = undefined;
  }

  _updateFreshness() {
    const label = this.shadowRoot?.querySelector(".freshness");
    if (!label) return;
    const updated = Date.parse(this._last?.last_updated);
    label.hidden = !Number.isFinite(updated);
    if (label.hidden) return;
    const stale = Date.now() - updated > 10 * 60 * 1000;
    label.classList.toggle("stale", stale);
    label.textContent = `Updated ${this._time(this._last.last_updated)}${stale ? " · stale" : ""}`;
    label.title = `Last entity update: ${new Date(updated).toLocaleString(this._hass?.locale?.language || undefined)}${stale ? "; more than 10 minutes ago" : ""}`;
  }

  setConfig(config) {
    this._config = { entity: DEFAULT_ENTITY, title: "Heating", ...config };
    const tolerance = Number(config.tolerance ?? 0.15);
    this._config.tolerance = Number.isFinite(tolerance) && tolerance >= 0 ? tolerance : 0.15;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._last = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._config?.entity];
    if (state === this._last) {
      this._updateFreshness();
      return;
    }
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
    if (!Number.isFinite(d.getTime())) return "";
    return d.toLocaleTimeString(this._hass?.locale?.language || undefined, { hour: "2-digit", minute: "2-digit" });
  }

  _comfort(r) {
    const has = r.state !== "no_data" && hasNumber(r.air) && hasNumber(r.target);
    // Remove floating-point noise without rounding away the tolerance boundary.
    const diff = has ? Math.round((r.air - r.target) * 1e6) / 1e6 : 0;
    const st = !has || diff === 0 || Math.abs(diff) < this._config.tolerance
      ? "ok" : diff < 0 ? "below" : "above";
    return { has, diff, st };
  }

  _room(r) {
    if (r.state === "no_data") {
      r = { ...r, air: null, target: null, evohome: null, overdrive: null, demand: null, calling: false };
    }
    const { has, diff, st } = this._comfort(r);
    const col = st === "below" ? "var(--below)" : st === "above" ? "var(--above)" : "var(--ok)";
    const p = Math.max(0, Math.min(100, 50 + (diff / 1.5) * 50));
    const delta = !has ? "" : st === "ok" ? "on target" : `${Math.abs(diff).toFixed(1)}° ${st}`;
    const od = hasNumber(r.overdrive) ? Number(r.overdrive) : 0;
    const odc = od === 0 ? "z" : st;
    const ods = !hasNumber(r.overdrive) ? "–" : od > 0 ? `+${od.toFixed(1)}` : od < 0 ? `−${Math.abs(od).toFixed(1)}` : "0.0";
    const pre = r.preheat;
    const preheating = pre && pre.status === "active";
    const flag = STATES[r.state] || (!has ? STATES.no_data : null);
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
    const hasDemand = hasNumber(r.demand);
    const demand = hasDemand ? Math.max(0, Math.min(100, Number(r.demand))) : 0;
    const quietValve = hasDemand && demand === 0 && !r.calling && !flag;
    return `<div class="room ${st}${!has ? " no-data" : ""}${preheating ? " preheating" : ""}${["off", "outside_window"].includes(r.state) ? " quiet" : ""}">
      <div class="top"><h3 title="${esc(r.name)}">${esc(r.name)}</h3>${status}</div>
      <div class="pair">
        <div class="now"><div class="lbl">Room</div><div class="v">${fmt(r.air)}<sup>°</sup></div></div>
        ${delta ? `<span class="delta ${st}" title="Room temperature minus HHO target">${delta}</span>` : ""}
        <div class="tgt"><div class="lbl">${preheating ? "Pre-heat to" : "Target"}</div><div class="v">${fmt(r.target)}<sup>°</sup></div></div>
      </div>
      <div class="gap" role="img" aria-label="${esc(has ? `${delta}; centre is target, scale minus to plus 1.5 degrees` : "Temperature difference unavailable")}" title="±1.5 °C around the target"><div class="f" style="left:${Math.min(p, 50)}%;width:${Math.abs(p - 50)}%;background:${col}"></div><div class="c"></div>${has ? `<div class="d" style="left:${p}%;background:${col}"></div>` : ""}</div>
      <div class="evo"><span title="Target written to the Evohome zone: HHO target, sensor-match offset and overdrive">Evohome told <b>${fmt(r.evohome)}°</b></span><span class="evo-drive">${preheating ? "push" : "overdrive"}<span class="od ${odc}" title="Added to the target sent to Evohome so its valve opens harder (positive) or closes sooner (negative)">${ods}</span></span></div>
      <div class="valve${quietValve ? " zero" : ""}"><span>Valve</span><div class="meter"><i style="width:${demand}%"></i></div><b>${hasDemand ? `${Number(r.demand)}%` : "–"}</b></div>
      ${preText ? `<div class="pre">${CLOCK}<span>${esc(preText)}</span></div>` : ""}
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
      const { has, st } = this._comfort(r);
      if (!has) continue;
      if (st === "below") below++;
      else if (st === "above") above++;
      else ok++;
    }
    const columns = Number(this._config.columns);
    const cols = Number.isInteger(columns) && columns > 0 ? `style="--cols:${columns}"` : "";
    const body = !state
      ? `<div class="empty">Entity ${esc(this._config.entity)} not found. Is Home Heating Optimisation 0.20 or later installed?</div>`
      : `<div class="grid" ${cols}>${rooms.map((r) => this._room(r)).join("")}</div>`;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>
      <div class="head">
        <div><div class="heading"><h2>${esc(this._config.title)}</h2><span class="freshness" hidden></span></div><div class="sub">Home Heating Optimisation · ${rooms.length} rooms</div></div>
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
    this._updateFreshness();
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
