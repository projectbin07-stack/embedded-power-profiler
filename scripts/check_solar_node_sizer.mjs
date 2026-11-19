// Check an exported load profile against solar-node-sizer's own code.
//
// Usage:
//   node scripts/check_solar_node_sizer.mjs <solar-node-sizer checkout> <exported.json> <analysis.json>
//
// <analysis.json> is the output of `epp analyze ... --json` for the same log (run with
// --capacity-mah so the battery-life comparison is made). The script imports
// solar-node-sizer's modules from the checkout (a pinned tag in CI) and fails unless:
//   1. normalizeConfig() accepts the file and leaves the load states unchanged;
//   2. dailyBudget() reproduces the measured mAh/day at the supply side within 0.1 %;
//   3. autonomyHours() with selfDischargePerHour() gives the same battery life as
//      embedded-power-profiler's port, within 0.1 %.

import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { deepStrictEqual } from "node:assert";

const [snsDir, exportedPath, analysisPath] = process.argv.slice(2);
if (!snsDir || !exportedPath || !analysisPath) {
  console.error("usage: check_solar_node_sizer.mjs <sns-dir> <exported.json> <analysis.json>");
  process.exit(2);
}
const mod = (p) => import(pathToFileURL(resolve(join(snsDir, p))).href);
const { normalizeConfig, batteryLimits } = await mod("src/core/config.js");
const { dailyBudget } = await mod("src/core/load.js");
const { autonomyHours } = await mod("src/core/simulate.js");
const { selfDischargePerHour } = await mod("src/core/battery.js");

const exported = JSON.parse(readFileSync(exportedPath, "utf8"));
const analysis = JSON.parse(readFileSync(analysisPath, "utf8"));
let failures = 0;
const check = (ok, message) => {
  console.log(`${ok ? "ok  " : "FAIL"} ${message}`);
  if (!ok) failures++;
};

const config = normalizeConfig(exported);
check(true, `normalizeConfig accepted ${exportedPath} (${config.load.length} load states)`);
try {
  deepStrictEqual(config.load, exported.load);
  check(true, "load states unchanged by normalizeConfig");
} catch (e) {
  check(false, `load states changed by normalizeConfig: ${e.message}`);
}

const vnom = batteryLimits(config).vnom;
const budget = dailyBudget(config.load, config.regulator, vnom);
const supplyMah = budget.states.reduce((a, s) => a + s.mAhPerDay, 0);
const measured = analysis.budget.mah_per_day;
const rel = Math.abs(supplyMah - measured) / measured;
check(
  rel < 1e-3,
  `dailyBudget supply side ${supplyMah.toFixed(4)} mAh/day vs measured ${measured.toFixed(4)} (${(rel * 100).toFixed(4)} %)`,
);
for (const s of budget.states)
  console.log(`     ${s.name.padEnd(24)} ${s.mAhPerDay.toFixed(4)} mAh/day  ${s.hoursPerDay.toFixed(4)} h/day`);

const b = analysis.battery;
if (b) {
  const cap = b.capacity_mah;
  const floor = (1 - b.depth_of_discharge) * cap;
  const p = b.mah_per_day / 24;
  for (const [label, sd, ours] of [
    ["without self-discharge", 0, b.days_without_self_discharge],
    ["with self-discharge", b.self_discharge_per_month, b.days_with_self_discharge],
  ]) {
    const days = autonomyHours(cap, floor, p, selfDischargePerHour(sd)) / 24;
    const d = Math.abs(days - ours) / days;
    check(d < 1e-3, `battery life ${label}: solar-node-sizer ${days.toFixed(3)} d, epp ${ours} d`);
  }
} else {
  console.log("     (no battery section in the analysis; run epp with --capacity-mah)");
}
process.exit(failures ? 1 : 0);
